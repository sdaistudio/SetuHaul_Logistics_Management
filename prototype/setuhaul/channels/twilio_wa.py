"""Twilio WhatsApp adapter (works with the Twilio WhatsApp Sandbox and a production sender).

Inbound: application/x-www-form-urlencoded, signed with X-Twilio-Signature (HMAC-SHA1 of the
exact public URL + sorted POST params, keyed by the auth token). verify() uses the SDK's
RequestValidator, which compares in constant time. Field names used (Twilio webhook reference):
    From "whatsapp:+91...", Body, MessageSid, NumMedia, MediaContentType0, MediaUrl0, Latitude, Longitude,
    ButtonPayload / ButtonText (quick-reply tap), OriginalRepliedMessageSid (quoted reply).
    ListId / InteractiveData: read defensively; the list-picker reply field is not documented.
Status callback: MessageSid, MessageStatus, ErrorCode, ErrorMessage, To.
Outbound: client.messages.create(from_, to, body | content_sid + content_variables | media_url, status_callback).
Media (voice notes): MediaUrlN is fetched with the account SID and auth token (HTTP basic), only from Twilio hosts.
"""
import json
import logging
from urllib.parse import parse_qs, urlparse, quote

from .base import ChannelRequest, InboundMessage, OutboundMessage, DeliveryStatus, SendResult
from .. import config as C
from ..session_store import normalise_phone
from ..tracing import mask

log = logging.getLogger("setuhaul.twilio")
STATUS_PATH = "/webhook/twilio/status"


def e164(addr):
    """'whatsapp:+919812345678' -> '+919812345678'."""
    if not addr:
        return ""
    a = addr.split(":", 1)[1] if addr.lower().startswith("whatsapp:") else addr
    return normalise_phone(a)


class _Multi(dict):
    """Repeated form keys, in the shape RequestValidator understands (it calls getall())."""
    def getall(self, k):
        v = self[k]
        return list(v) if isinstance(v, (list, tuple)) else [v]


def _choice_from_interactive(raw):
    try:
        d = json.loads(raw)
    except (TypeError, ValueError):
        return None
    for path in (("list_reply", "id"), ("button_reply", "id"), ("id",)):
        cur = d
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        if isinstance(cur, str) and cur:
            return cur
    return None


class TwilioChannel:
    name = "twilio"

    def __init__(self, settings, client=None):
        self.s = settings
        self._client = client

    # ------------------------------------------------------------ inbound
    def verify(self, request: ChannelRequest) -> bool:
        sig = request.headers.get("x-twilio-signature") or request.headers.get("X-Twilio-Signature")
        if not sig or not self.s.twilio_auth_token:
            return False
        from twilio.request_validator import RequestValidator, get_signature_algorithm
        algo = get_signature_algorithm(request.headers.get("x-twilio-webhooks-signature-settings"))
        return RequestValidator(self.s.twilio_auth_token).validate(request.url, _Multi(request.params), sig, algorithm=algo)

    def parse_inbound(self, request: ChannelRequest):
        g = request.get
        sid = g("MessageSid") or g("SmsMessageSid")
        frm = e164(g("From"))
        if not sid or not frm:
            return []
        m = InboundMessage(provider="twilio", message_id=sid, from_e164=frm, text=(g("Body") or "").strip(),
                           reply_to_id=g("OriginalRepliedMessageSid"))
        choice = g("ButtonPayload") or g("ListId") or _choice_from_interactive(g("InteractiveData"))
        nmedia = int(g("NumMedia") or 0)
        if choice:
            m.type, m.choice_id = "choice", choice
        elif g("Latitude") and g("Longitude"):
            m.type = "location"
            try:
                m.lat, m.lng = float(g("Latitude")), float(g("Longitude"))
            except ValueError:
                m.type = "other"
        elif nmedia > 0:
            m.type = "media"
            m.media_types = [g(f"MediaContentType{i}") for i in range(nmedia) if g(f"MediaContentType{i}")]
            m.media_urls = [g(f"MediaUrl{i}") for i in range(nmedia) if g(f"MediaUrl{i}")]
        elif m.text:
            m.type = "text"
        else:
            m.type = "other"
        return [m]

    def parse_status(self, request: ChannelRequest) -> DeliveryStatus:
        g = request.get
        ref = (parse_qs(urlparse(request.url).query).get("ref") or [None])[0]
        return DeliveryStatus(provider_id=g("MessageSid") or g("SmsSid") or "", status=(g("MessageStatus") or g("SmsStatus") or "").lower(),
                              error_code=g("ErrorCode") or None, error_message=g("ErrorMessage") or None, to_e164=e164(g("To")), ref=ref)

    def lookup(self, to_e164, since, until):
        """Messages we sent to this number around a send attempt (Messages list, filtered by To and From;
        the window is applied here on date_created). Raises on any API error: the caller decides."""
        out = []
        for m in self.client.messages.list(to="whatsapp:" + normalise_phone(to_e164), from_=self.s.twilio_whatsapp_from, limit=50):
            created = m.date_created
            if created is not None and since <= created <= until:
                out.append({"sid": m.sid, "body": m.body, "date_created": created, "status": m.status})
        return out

    def fetch_media(self, url, max_bytes):
        """Download an inbound media file (a voice note). Only https Twilio hosts; size-capped. -> (bytes, content_type)."""
        import base64
        import urllib.request
        u = urlparse(url or "")
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not (host == "api.twilio.com" or host.endswith(".twilio.com") or host.endswith(".twiliocdn.com")):
            raise ValueError("media URL is not a Twilio https URL")
        tok = base64.b64encode(f"{self.s.twilio_account_sid}:{self.s.twilio_auth_token}".encode()).decode()
        req = urllib.request.Request(url, headers={"Authorization": "Basic " + tok})
        with urllib.request.urlopen(req, timeout=10) as r:      # noqa: S310 (host checked above; redirects stay on Twilio's CDN)
            data = r.read(max_bytes + 1)
            ctype = r.headers.get("Content-Type", "")
        if len(data) > max_bytes:
            raise ValueError("media too large")
        return data, ctype

    # ------------------------------------------------------------ outbound
    @property
    def client(self):
        if self._client is None:
            from twilio.rest import Client
            self._client = Client(self.s.twilio_account_sid, self.s.twilio_auth_token)
        return self._client

    def build_params(self, msg: OutboundMessage):
        """The exact create() arguments. Separate from send() so tests can inspect it."""
        p = {"from_": self.s.twilio_whatsapp_from, "to": "whatsapp:" + normalise_phone(msg.to_e164)}
        if self.s.twilio_webhook_base_url:
            p["status_callback"] = self.s.twilio_webhook_base_url + STATUS_PATH + (f"?ref={quote(msg.send_ref)}" if msg.send_ref else "")
        if msg.kind == "media":                               # a spoken reply: the audio, no text (the text went just before)
            if not msg.media_url:
                raise LookupError("media message without media_url")
            p["media_url"] = [msg.media_url]
            return p
        if msg.kind == "template":
            sid = self.s.template_sids.get(msg.template_ref or "")
            if not sid:
                raise LookupError(f"no content SID for template {msg.template_ref}")
            p["content_sid"] = sid
            p["content_variables"] = json.dumps({str(k): str(v) for k, v in msg.template_vars.items()}, ensure_ascii=False)
            return p
        if msg.kind in ("choices", "list") and self.s.twilio_interactive and msg.options:
            n = len(msg.options)
            key = f"QR{n}" if msg.kind == "choices" and n <= C.TWILIO_QR_MAX_BUTTONS else f"LIST{n}"
            sid = self.s.content_sids.get(key)
            if sid:
                # Variables layout fixed by scripts/create_twilio_content.py: 1 = body, then (title, id) per option;
                # list-picker adds a description after each id.
                v = {"1": msg.body[:C.TWILIO_BODY_MAX]}
                i = 2
                for o in msg.options:
                    is_list = key.startswith("LIST")
                    lim = C.TWILIO_LIST_ITEM_MAX if is_list else C.TWILIO_QR_TITLE_MAX
                    v[str(i)], v[str(i + 1)] = o.title[:lim], o.id[:C.TWILIO_QR_ID_MAX]
                    i += 2
                    if is_list:
                        v[str(i)] = (o.desc or "-")[:C.TWILIO_LIST_DESC_MAX]
                        i += 1
                p["content_sid"] = sid
                p["content_variables"] = json.dumps(v, ensure_ascii=False)
                return p
        p["body"] = msg.body                                     # numbered text: "Reply 1, 2 or 3"
        return p

    def send(self, msg: OutboundMessage) -> SendResult:
        try:
            params = self.build_params(msg)
        except LookupError as e:
            return SendResult(ok=False, status="blocked", error_code="NO_TEMPLATE", error_message=str(e))
        try:
            m = self.client.messages.create(**params)
        except Exception as e:                                     # classify; never log the number or body
            return classify_error(e)
        log.info("twilio send %s -> %s status=%s", getattr(m, "sid", "?"), mask(msg.to_e164), getattr(m, "status", "?"))
        return SendResult(ok=True, provider_id=m.sid, status=(getattr(m, "status", None) or "queued"), sent_kind=sent_kind(params, msg))


def sent_kind(params, msg):
    if "media_url" in params:
        return "media"
    if "content_sid" not in params:
        return "text"
    return "template" if msg.kind == "template" else "interactive"


def classify_error(e):
    """429 / 5xx / network -> retry later. Other 4xx (bad number, not joined, no template) -> FAILED."""
    try:
        from twilio.base.exceptions import TwilioRestException
    except ImportError:                                            # pragma: no cover
        TwilioRestException = ()
    if TwilioRestException and isinstance(e, TwilioRestException):
        http = int(getattr(e, "status", 0) or 0)
        code = getattr(e, "code", None)
        return SendResult(ok=False, status="error", error_code=str(code or http), error_message=str(getattr(e, "msg", e))[:300],
                          retryable=http == 429 or http >= 500, ambiguous=http >= 500)
    status = getattr(e, "status", None)
    if isinstance(status, int):
        return SendResult(ok=False, status="error", error_code=str(getattr(e, "code", status)), error_message=str(e)[:300],
                          retryable=status == 429 or status >= 500, ambiguous=status >= 500)
    # network error / timeout: the request may have reached Twilio
    return SendResult(ok=False, status="error", error_code="NETWORK", error_message=type(e).__name__, retryable=True, ambiguous=True)
