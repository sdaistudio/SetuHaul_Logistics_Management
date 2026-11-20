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
import os
import re
from datetime import timedelta

from . import config as C
from .clock import at, IST
from .nlu import Facts

MODEL = os.environ.get("SETUHAUL_LLM_MODEL", "claude-haiku-4-5")
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


def make_fallback():
    """The fallback callable, or None when the key or the package is missing."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        return HaikuFallback()
    except ImportError:
        return None
