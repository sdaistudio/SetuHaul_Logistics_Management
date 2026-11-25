"""The optional LLM fallback reader: Claude Haiku through LangChain, strict JSON only.

Core activity: when nlu.extract() returns UNKNOWN, ask a small model to read the words,
then check its answer in code before anything uses it.

What the model may do: turn words into the fact-sheet JSON below.
What it may never do: call a tool, see the database, choose a slot, set a priority,
or decide to escalate. Its output can change what we ask, never what we book.

Message formats
    stored history (Redis / database): [{"role": "human" | "ai", "text": "...", ...}]
    to the model:   SystemMessage + HumanMessage / AIMessage objects   (to_langchain)
    back from it:   a dict matching SCHEMA (structured output)          -> validate() -> nlu.Facts

With no ANTHROPIC_API_KEY, or without langchain-anthropic installed, make_fallback()
returns None and the workflow asks the driver a clarifying question instead.
"""
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import timedelta

from . import config as C
from .clock import at, IST
from .nlu import Facts

MODEL = os.environ.get("SETUHAUL_LLM_MODEL", "claude-haiku-4-5")
log = logging.getLogger("setuhaul.llm")
from .reader_contract import (  # noqa: F401  (moved there; re-exported so llm.X keeps working)
    INTENTS, HINTS, HHMM, SCHEMA, SYSTEM_PROMPT, to_langchain,
    CONTRACT_VERSION, MAX_TEXT_CHARS, trim_history,
)


def from_langchain(messages):
    """LangChain messages -> the JSON form we store (system prompt dropped)."""
    out = []
    for m in messages:
        kind = getattr(m, "type", "")
        if kind in ("human", "ai"):
            out.append({"role": kind, "text": m.content})
    return out


# ---------------------------------------------------------------- validation in code
def _clock(s, as_of):
    if s is None:
        return None
    if not isinstance(s, str) or not re.fullmatch(HHMM, s):
        raise ValueError("time not HH:MM")
    h, m = map(int, s.split(":"))
    return at(as_of, h, m)


def validate(raw, text, as_of):
    """Model output -> nlu.Facts, or None if anything is out of shape. Nothing unchecked gets through."""
    if not isinstance(raw, dict):
        return None
    if set(raw) - set(SCHEMA["properties"]) or set(SCHEMA["required"]) - set(raw):
        return None
    if raw["intent"] not in INTENTS:
        return None
    if raw["shipment_hint"] not in HINTS + [None]:
        return None
    if raw["choice"] is not None and (not isinstance(raw["choice"], int) or not 1 <= raw["choice"] <= C.MAX_OPTIONS_SHOWN):
        return None
    if raw["delay_min"] is not None and (not isinstance(raw["delay_min"], int) or not 0 <= raw["delay_min"] <= 1440):
        return None
    limits = raw.get("limits") or {}
    if set(limits) - {"latest_gate_out"}:
        return None
    try:
        eta = _clock(raw["eta"], as_of)
        gate_out = _clock(limits.get("latest_gate_out"), as_of)
    except ValueError:
        return None
    lo, hi = as_of - timedelta(hours=1), as_of + timedelta(hours=24)
    for d in (eta, gate_out):
        if d is not None and not lo <= d <= hi:
            return None
    ev = raw.get("evidence") or {}
    low = text.lower()
    if not isinstance(ev, dict) or any(not isinstance(v, str) or v.strip().lower() not in low for v in ev.values()):
        return None                                   # every quoted word must really be in the message
    filled = [k for k in ("eta", "delay_min", "shipment_hint", "choice") if raw[k] is not None]
    if any(k not in ev for k in filled):
        return None                                   # a value with no evidence is a guess
    f = Facts()
    f.intent = raw["intent"]
    f.eta, f.eta_confidence = eta, ("MEDIUM" if eta else None)    # never HIGH from a model
    f.delay_min = raw["delay_min"]
    f.hint = raw["shipment_hint"]
    f.choice = raw["choice"]
    if gate_out:
        f.limits["LATEST_GATE_OUT"] = gate_out
    # Safety is OR-ed with the rule list: either source escalates; the model can never clear it.
    f.safety = bool(raw["safety"]) or any(w in low for w in C.SAFETY_WORDS)
    if f.safety:
        f.intent = "SAFETY"
    f.vague_delay = f.intent == "REPORT_DELAY" and not f.eta and not f.delay_min
    f.evidence = {k: v for k, v in ev.items()}
    f.evidence["source"] = "llm"
    return f


# ---------------------------------------------------------------- the callable
class HaikuFallback:
    backend = "anthropic"

    def __init__(self, model=MODEL, timeout_s=8):
        from langchain_anthropic import ChatAnthropic
        llm = ChatAnthropic(model=model, temperature=0, timeout=timeout_s, max_retries=1, max_tokens=400)
        self.reader = llm.with_structured_output(SCHEMA)        # no tools are bound, ever
        self.model = model

    def __call__(self, text, as_of, history):
        try:
            raw = self.reader.invoke(to_langchain(history, text))
        except Exception:
            return None                                          # network or model error: ask the driver instead
        return validate(raw, text, as_of)


def _agentcore_body(resp):
    """invoke_agent_runtime response -> the agent's JSON answer (dict), or None.
    The body is a stream; JSON normally, but read an event stream ("data: {...}" lines) defensively."""
    data = resp.get("response")
    if hasattr(data, "read"):
        data = data.read()
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8")
    if not isinstance(data, str):
        return None
    if "text/event-stream" in (resp.get("contentType") or ""):
        events = [ln[5:].strip() for ln in data.splitlines() if ln.startswith("data:")]
        data = events[-1] if events else ""
    body = json.loads(data)
    if isinstance(body, str):                                    # a JSON string that holds the JSON object
        body = json.loads(body)
    return body if isinstance(body, dict) else None


class AgentCoreFallback:
    """The same reader, hosted on Amazon Bedrock AgentCore Runtime (agent/main.py).

    Sends only the message text (<= MAX_TEXT_CHARS), trimmed history (role + text, last 6), as_of and the
    contract version. Never a phone number, driver id, name or database row: the driver key is only hashed,
    with a salt, into the runtime session id. The agent's raw answer is checked here by validate(),
    exactly as the direct reader's is. Any problem returns None (the driver gets a clarifying question).
    """
    backend = "agentcore"
    accepts_driver_key = True

    def __init__(self, arn, region, salt="", client=None, connect_timeout=2, read_timeout=6):
        self.arn, self.salt = arn, salt or ""
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("bedrock-agentcore", region_name=region, config=Config(
                connect_timeout=connect_timeout, read_timeout=read_timeout,
                retries={"total_max_attempts": 1, "mode": "standard"}))     # one attempt: the driver is waiting
        self.client = client

    def session_id(self, driver_key, as_of):
        """>= 33 characters (AgentCore's minimum); stable per driver per day; not reversible without the salt."""
        if not driver_key:
            return "setuhaul-reader-" + uuid.uuid4().hex
        digest = hashlib.sha256(f"{self.salt}:{driver_key}:{as_of.date()}".encode("utf-8")).hexdigest()[:40]
        return "setuhaul-reader-" + digest

    def payload(self, text, as_of, history):
        return {"contract_version": CONTRACT_VERSION, "text": text[:MAX_TEXT_CHARS],
                "history": trim_history(history), "as_of": as_of.isoformat()}

    def __call__(self, text, as_of, history, driver_key=None):
        try:
            resp = self.client.invoke_agent_runtime(
                agentRuntimeArn=self.arn, runtimeSessionId=self.session_id(driver_key, as_of),
                payload=json.dumps(self.payload(text, as_of, history), ensure_ascii=False).encode("utf-8"),
                contentType="application/json", accept="application/json")
            body = _agentcore_body(resp)
            if not body or body.get("ok") is not True or body.get("contract_version") != CONTRACT_VERSION:
                return None
            return validate(body.get("raw"), text, as_of)
        except Exception as e:                                   # timeout, AWS error, bad JSON: ask the driver instead
            log.warning("agentcore reader failed: %s", type(e).__name__)
            return None


def _agentcore_from_env():
    arn, region = os.environ.get("AGENTCORE_READER_ARN", ""), os.environ.get("AWS_REGION", "")
    if not arn or not region:
        log.warning("READER=agentcore but AGENTCORE_READER_ARN or AWS_REGION is not set: rules only")
        return None
    try:
        return AgentCoreFallback(arn, region, salt=os.environ.get("SETUHAUL_SESSION_SALT", ""))
    except ImportError:
        log.warning("READER=agentcore but boto3 is not installed: rules only")
    except Exception as e:
        log.warning("READER=agentcore but the client could not be created (%s): rules only", type(e).__name__)
    return None


def make_fallback():
    """The fallback callable, or None when the key or the package is missing.

    READER (optional): off = rules only; anthropic = the direct Claude reader (needs ANTHROPIC_API_KEY);
    agentcore = the AgentCore Runtime reader (needs AGENTCORE_READER_ARN and AWS_REGION).
    Unset: exactly the behaviour before READER existed (the direct reader if ANTHROPIC_API_KEY is set).
    """
    reader = (os.environ.get("READER") or "").strip().lower()
    if reader == "off":
        return None
    if reader == "agentcore":
        return _agentcore_from_env()
    if reader not in ("", "anthropic"):
        log.warning("unknown READER value: rules only")
        return None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        return HaikuFallback()
    except ImportError:
        return None
