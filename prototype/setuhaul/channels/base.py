"""The channel contract. The engine never sees a provider; a provider never sees the engine.

    verify(request)          -> bool              signature check, before any processing
    parse_inbound(request)   -> [InboundMessage]  identity = the verified sender number only
    parse_status(request)    -> DeliveryStatus    delivery receipts for our outbound messages
    send(OutboundMessage)    -> SendResult        called only by the outbox worker, after COMMIT

A "request" here is a ChannelRequest: url (the public URL the provider called), headers and
the raw form params, so adapters are testable without a web framework.
"""
from dataclasses import dataclass, field, asdict
from typing import Optional, Protocol, List


@dataclass
class ChannelRequest:
    url: str
    headers: dict
    params: dict                       # name -> list of values (urllib.parse.parse_qs form)

    def get(self, name, default=None):
        v = self.params.get(name)
        if v is None:
            return default
        return v[0] if isinstance(v, (list, tuple)) else v


@dataclass
class InboundMessage:
    provider: str
    message_id: str
    from_e164: str
    ts: Optional[str] = None           # provider timestamp if any (Twilio sends none)
    type: str = "text"                 # text | choice | location | media | other
    text: str = ""
    choice_id: Optional[str] = None
    reply_to_id: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    media_types: list = field(default_factory=list)
    media_urls: list = field(default_factory=list)  # provider URLs of the media (Twilio: MediaUrlN, needs account auth)
    voice: bool = False                # set by the channel service when the text came from a voice note

    def as_dict(self):
        return asdict(self)


@dataclass
class Option:
    id: str
    title: str
    desc: str = ""


@dataclass
class OutboundMessage:
    to_e164: str
    kind: str = "text"                 # text | choices | list | template | media (a voice reply: media_url)
    body: str = ""
    options: List[Option] = field(default_factory=list)
    template_ref: Optional[str] = None
    template_vars: dict = field(default_factory=dict)
    lang: str = "en"
    offer_id: Optional[str] = None
    outbox_id: Optional[str] = None
    proactive: bool = False            # business-initiated: template only, whatever the window
    send_ref: Optional[str] = None     # per-attempt key, stored BEFORE the API call; travels in the status callback URL
    media_url: Optional[str] = None    # kind=media: public https URL of the audio (served by /media/<token>)

    def as_dict(self):
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        d["options"] = [Option(**o) for o in d.get("options") or []]
        return cls(**d)


@dataclass
class DeliveryStatus:
    provider_id: str
    status: str                        # queued | sent | delivered | read | failed | undelivered
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    to_e164: Optional[str] = None
    ref: Optional[str] = None          # our send_ref, from the ?ref= on the status callback URL


@dataclass
class SendResult:
    ok: bool
    provider_id: Optional[str] = None
    status: str = "sent"
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    retryable: bool = False            # 429, 5xx, network: try again later
    sent_kind: Optional[str] = None    # what actually went: text | interactive | template
    ambiguous: bool = False            # 5xx / network: the provider MAY have accepted it -> reconcile, never blind retry


class Channel(Protocol):
    name: str

    def verify(self, request: ChannelRequest) -> bool: ...
    def parse_inbound(self, request: ChannelRequest) -> List[InboundMessage]: ...
    def parse_status(self, request: ChannelRequest) -> DeliveryStatus: ...
    def send(self, msg: OutboundMessage) -> SendResult: ...
    # optional: lookup(to_e164, since, until) -> [{"sid", "body", "date_created", "status"}] for reconciliation
