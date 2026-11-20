"""Deployment settings from the environment (never from code or the repository).

Secrets: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, ANTHROPIC_API_KEY, SETUHAUL_SESSION_SALT. They are read here,
used by the adapters, and never logged or traced (see tracing.mask and Settings.public()).
"""
import os
from dataclasses import dataclass, field


def _b(v, default=False):
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    channel: str = "console"                       # twilio | sim | console
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = ""                 # e.g. whatsapp:+14155238886 (the sandbox number)
    twilio_webhook_base_url: str = ""              # public base, e.g. https://abcd.ngrok-free.app
    twilio_interactive: bool = False               # quick-reply / list-picker via Content API, else numbered text
    twilio_sandbox: bool = True                    # 1 message / 3 s pacing; templates limited to the sandbox's own
    content_sids: dict = field(default_factory=dict)   # TWILIO_CONTENT_QR1..3, TWILIO_CONTENT_LIST
    template_sids: dict = field(default_factory=dict)  # TWILIO_TPL_<NAME>_SID
    sim_url: str = "http://127.0.0.1:8100"
    clock_mode: str = "live"
    snapshot_anchor: str = ""
    redis_url: str = ""
    duty_head_phone: str = ""
    worker: bool = True
    reader: str = ""                               # READER: off | anthropic | agentcore; unset = ANTHROPIC_API_KEY decides
    agentcore_reader_arn: str = ""                 # AGENTCORE_READER_ARN (shown only as set / not set)
    aws_region: str = ""                           # AWS_REGION
    session_salt: str = ""                         # SETUHAUL_SESSION_SALT (salts the AgentCore session id; never shown)

    @classmethod
    def from_env(cls, env=None):
        e = env if env is not None else os.environ
        return cls(
            channel=(e.get("CHANNEL") or "console").lower(),
            twilio_account_sid=e.get("TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=e.get("TWILIO_AUTH_TOKEN", ""),
            twilio_whatsapp_from=e.get("TWILIO_WHATSAPP_FROM", ""),
            twilio_webhook_base_url=(e.get("TWILIO_WEBHOOK_BASE_URL") or "").rstrip("/"),
            twilio_interactive=_b(e.get("TWILIO_INTERACTIVE")),
            twilio_sandbox=_b(e.get("TWILIO_SANDBOX"), True),
            content_sids={k[len("TWILIO_CONTENT_"):]: v for k, v in e.items() if k.startswith("TWILIO_CONTENT_") and v},
            template_sids={k[len("TWILIO_TPL_"):-len("_SID")].lower(): v for k, v in e.items()
                           if k.startswith("TWILIO_TPL_") and k.endswith("_SID") and v},
            sim_url=(e.get("SIM_URL") or "http://127.0.0.1:8100").rstrip("/"),
            clock_mode=e.get("CLOCK_MODE", "live"),
            snapshot_anchor=e.get("SNAPSHOT_ANCHOR", ""),
            redis_url=e.get("REDIS_URL", ""),
            duty_head_phone=e.get("DUTY_HEAD_PHONE", ""),
            worker=_b(e.get("OUTBOX_WORKER"), True),
            reader=(e.get("READER") or "").strip().lower(),
            agentcore_reader_arn=e.get("AGENTCORE_READER_ARN", ""),
            aws_region=e.get("AWS_REGION", ""),
            session_salt=e.get("SETUHAUL_SESSION_SALT", ""),
        )

    def public(self):
        """Safe to show in /readyz and traces: no secrets, no full numbers."""
        from .tracing import mask
        return {"channel": self.channel, "twilio_interactive": self.twilio_interactive, "twilio_sandbox": self.twilio_sandbox,
                "twilio_from": mask(self.twilio_whatsapp_from), "account_sid_set": bool(self.twilio_account_sid),
                "auth_token_set": bool(self.twilio_auth_token), "webhook_base_url": self.twilio_webhook_base_url,
                "content_sids": sorted(self.content_sids), "template_sids": sorted(self.template_sids),
                "clock_mode": self.clock_mode,
                "reader": self.reader or "default", "agentcore_arn_set": bool(self.agentcore_reader_arn)}
