"""Console adapter: no network. Used by CHANNEL=console (the default), the offline tests and the
parity test. Inbound requests are plain params (From, Body, MessageSid, ChoiceId); outbound
messages are kept in .sent and printed with the number masked."""
import itertools

from .base import InboundMessage, DeliveryStatus, SendResult
from ..session_store import normalise_phone
from ..tracing import mask


class ConsoleChannel:
    name = "console"
    _ids = itertools.count(1)

    def __init__(self, settings=None, echo=False):
        self.sent, self.echo = [], echo
        self.media = {}                                # url -> (bytes, content_type): voice notes for local runs and tests

    def verify(self, request):
        return True                                   # local only; app.py refuses console inbound unless SETUHAUL_DEV=1

    def parse_inbound(self, request):
        g = request.get
        choice = g("ChoiceId")
        m = InboundMessage(provider="console", message_id=g("MessageSid") or f"console-{next(self._ids)}",
                           from_e164=normalise_phone(g("From") or ""), text=(g("Body") or "").strip(),
                           type="choice" if choice else "text", choice_id=choice)
        if g("MediaUrl0"):                            # a voice note, Twilio-shaped
            m.type, m.media_urls, m.media_types = "media", [g("MediaUrl0")], [g("MediaContentType0") or "audio/ogg"]
        return [m]

    def fetch_media(self, url, max_bytes):
        if url not in self.media:
            raise ValueError("unknown media")
        data, ctype = self.media[url]
        if len(data) > max_bytes:
            raise ValueError("media too large")
        return data, ctype

    def parse_status(self, request):
        return DeliveryStatus(provider_id=request.get("MessageSid", ""), status=request.get("MessageStatus", ""))

    def send(self, msg):
        sid = f"CONSOLE{next(self._ids):06d}"
        self.sent.append((sid, msg))
        if self.echo:
            print(f"[console -> {mask(msg.to_e164)}] {msg.body or ''}" + (f" [voice {msg.media_url}]" if msg.media_url else "")
                  + "".join(f"\n   [{o.title}]" for o in msg.options))
        kind = "template" if msg.kind == "template" else "media" if msg.kind == "media" else "text"
        return SendResult(ok=True, provider_id=sid, status="delivered", sent_kind=kind)
