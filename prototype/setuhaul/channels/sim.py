"""Local simulator adapter: Twilio-shaped in every way except where outbound goes.

Inbound and status callbacks arrive at the real /webhook/twilio endpoints, signed exactly as
Twilio signs them (scripts/sim_server.py signs with the same auth token). Outbound messages are
POSTed to the simulator (SIM_URL/api/deliver) instead of Twilio's REST API; the simulator then
sends signed sent/delivered/read callbacks back, so the outbox, ticks and alarms run for real.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from .base import SendResult
from .twilio_wa import TwilioChannel


class SimChannel(TwilioChannel):
    name = "sim"

    def send(self, msg):
        try:
            params = self.build_params(msg)
        except LookupError as e:
            return SendResult(ok=False, status="blocked", error_code="NO_TEMPLATE", error_message=str(e))
        payload = dict(params, kind=msg.kind, options=[o.__dict__ for o in msg.options], offer_id=msg.offer_id,
                       template_ref=msg.template_ref, template_vars=msg.template_vars, outbox_id=msg.outbox_id,
                       ref=msg.send_ref, media_url=msg.media_url)
        req = urllib.request.Request(self.s.sim_url + "/api/deliver", data=json.dumps(payload, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                out = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code >= 500 or e.code == 429:
                return SendResult(ok=False, status="error", error_code=str(e.code), error_message="simulator", retryable=True,
                                  ambiguous=e.code >= 500)
            body = {}
            try:
                body = json.loads(e.read() or b"{}")
            except ValueError:
                pass
            return SendResult(ok=False, status="error", error_code=str(body.get("code", e.code)),
                              error_message=body.get("message", "simulator refused"))
        except (urllib.error.URLError, OSError):
            return SendResult(ok=False, status="error", error_code="NETWORK", error_message="simulator unreachable", retryable=True,
                              ambiguous=True)
        from .twilio_wa import sent_kind
        return SendResult(ok=True, provider_id=out["sid"], status=out.get("status", "queued"), sent_kind=sent_kind(params, msg))

    def lookup(self, to_e164, since, until):
        from ..clock import ts
        from ..session_store import normalise_phone
        q = urllib.parse.urlencode({"to": normalise_phone(to_e164)})
        with urllib.request.urlopen(self.s.sim_url + "/api/lookup?" + q, timeout=5) as r:
            rows = json.loads(r.read() or b"[]")
        return [dict(m, date_created=ts(m["date_created"])) for m in rows if since <= ts(m["date_created"]) <= until]
