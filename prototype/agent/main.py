"""SetuHaul reader agent for Amazon Bedrock AgentCore Runtime ("setuhaul-reader").

One job: read one driver message into the RAW fact-sheet JSON (reader_contract.SCHEMA) with one structured-output
call to Claude on Bedrock. No tools, no database, no network beyond Bedrock (and LangSmith if enabled).
EC2 calls this only when its rules say UNKNOWN, then runs llm.validate() on "raw": the trust boundary stays on EC2.

Payload in:   {"contract_version", "text", "history": [{"role", "text"}], "as_of"}
Answer out:   {"ok": true, "contract_version", "raw", "model", "latency_ms"}
          or  {"ok": false, "contract_version", "error": <short code>}

Environment:  SETUHAUL_BEDROCK_MODEL  Bedrock model id or inference profile id (required)
              AWS_REGION              set by AgentCore Runtime
Logs carry error TYPES only, never the message text.
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reader_contract import CONTRACT_VERSION, MAX_TEXT_CHARS, SCHEMA, to_langchain, trim_history  # noqa: E402

log = logging.getLogger("setuhaul.reader")
MODEL_ID = os.environ.get("SETUHAUL_BEDROCK_MODEL", "")
_reader = None


def build_reader(model_id=None, region=None):
    """ChatBedrockConverse, temperature 0, 400 tokens, structured output to SCHEMA. No tools are bound, ever."""
    from langchain_aws import ChatBedrockConverse
    llm = ChatBedrockConverse(model=model_id or MODEL_ID, region_name=region or os.environ.get("AWS_REGION"),
                              temperature=0, max_tokens=400)
    return llm.with_structured_output(SCHEMA)


def get_reader():
    global _reader
    if _reader is None:                       # built once per runtime session container
        _reader = build_reader()
    return _reader


def _fail(code):
    return {"ok": False, "contract_version": CONTRACT_VERSION, "error": code}


def handle(payload, reader=None):
    """The whole agent, as a plain function (the entrypoint below only wraps it). Never raises."""
    try:
        if not isinstance(payload, dict):
            return _fail("bad_payload")
        if payload.get("contract_version") != CONTRACT_VERSION:
            return _fail("contract_mismatch")
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return _fail("bad_payload")
        if reader is None and not MODEL_ID:
            return _fail("no_model")
        text = text[:MAX_TEXT_CHARS]
        history = trim_history(payload.get("history"))
        r = reader if reader is not None else get_reader()
        t0 = time.monotonic()
        raw = r.invoke(to_langchain(history, text))
        ms = int((time.monotonic() - t0) * 1000)
        if not isinstance(raw, dict):
            return _fail("bad_output")
        return {"ok": True, "contract_version": CONTRACT_VERSION, "raw": raw, "model": MODEL_ID or "custom", "latency_ms": ms}
    except Exception as e:                    # model, network, throttling, anything: EC2 asks the driver instead
        log.warning("reader failed: %s", type(e).__name__)
        return _fail("model_error")


try:
    from bedrock_agentcore import BedrockAgentCoreApp
except ImportError:                           # tests and the EC2 side import handle() without the SDK
    BedrockAgentCoreApp = None

app = BedrockAgentCoreApp() if BedrockAgentCoreApp else None

if app is not None:
    @app.entrypoint
    def invoke(payload):
        return handle(payload)


if __name__ == "__main__":
    app.run()
