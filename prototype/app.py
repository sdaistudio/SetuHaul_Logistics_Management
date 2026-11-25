"""SetuHaul HTTP entry point (FastAPI).

Core activity: receive an HTTP request, prove who sent it, load the session, run one
turn through the graph, send the reply, save the session. No booking logic lives here.

Driver side (WhatsApp Business Cloud API)
    GET  /webhook/whatsapp          Meta's subscription check (hub.challenge)
    POST /webhook/whatsapp          inbound messages; body signed by Meta (X-Hub-Signature-256)
Coordinator side (console)
    POST /auth/dev-login            offline demo sign-in by email or mobile -> token
    POST /auth/session              register a Cognito token (production)
    POST /auth/logout
    GET  /console/handoffs          open escalations for my facility
    GET  /console/holds             holds waiting for my approval
    POST /console/holds/{id}/decide approve or reject; the driver is told at once
    GET  /console/board             booking health for my facility
Driver side (Twilio WhatsApp, incl. the Sandbox)
    POST /webhook/twilio            inbound; X-Twilio-Signature checked before anything else; 200 + empty TwiML
                                    at once, the turn runs in the background, replies go out via the outbox
    POST /webhook/twilio/status     delivery receipts (queued/sent/delivered/read/failed/undelivered + ErrorCode)
Coordinator side (additions)
    POST /console/handoffs/{id}/ack     take an escalation (stops the missed-SLA chain)
    POST /console/safety/{id}/clear     clear a SAFETY freeze
    POST /console/proactive/{shipment}  health-monitor check-in (template only)
    GET  /console/alarms, /console/outbox
Demo and ops
    POST /dev/driver-message        send a driver message without Meta (SETUHAUL_DEV=1 only)
    POST /dev/clock                 move a snapshot clock forward (SETUHAUL_DEV=1, CLOCK_MODE=snapshot)
    GET  /health, /healthz, /readyz
    GET  /media/<token>.mp3        a spoken reply for WhatsApp to fetch (VOICE on; 24 h, unguessable token)

Run:  pip install -r requirements-prod.txt
      SETUHAUL_DEV=1 uvicorn app:app --reload            (from the prototype folder)

Environment
    SETUHAUL_DB             database path (default: a fresh copy of the snapshot in out/app_world.db)
    SETUHAUL_REWIND_TO      e.g. 2026-08-04T08:20:00+05:30, to replay the snapshot day (demo)
    REDIS_URL               sessions in Redis; in-memory store if unset
    ANTHROPIC_API_KEY       turns on the LLM fallback reader (llm.py); rules only if unset
    WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN, WHATSAPP_TOKEN, WHATSAPP_PHONE_ID
    CHANNEL=twilio|sim|console, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM,
    TWILIO_WEBHOOK_BASE_URL, TWILIO_INTERACTIVE, TWILIO_SANDBOX, TWILIO_CONTENT_*, TWILIO_TPL_*_SID   (see .env.example)
    CLOCK_MODE=live|snapshot, SNAPSHOT_ANCHOR       snapshot: the 4 Aug world runs forward from the anchor
    COGNITO_REGION, COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID   (production coordinator sign-in)
    SETUHAUL_DEV_SECRET     signing key for demo tokens
    LANGSMITH_TRACING=true, LANGSMITH_API_KEY                 (tracing)
"""
import asyncio
import contextlib
import json
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qs

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from setuhaul import db, capacity, planner, world, chan_db, demo_world, outbox as OB, safety as SF, config as CFG
from setuhaul import auth as A
from setuhaul.channels import ChannelRequest, get_channel
from setuhaul.channels.twilio_wa import TwilioChannel
from setuhaul.clock import IST, ts, iso, ampm, add, Clock
from setuhaul.inbound import ChannelService
from setuhaul.settings import Settings
from setuhaul.tracing import mask
from setuhaul.graph import run_turn
from setuhaul.llm import make_fallback
from setuhaul.voice import make_voice, load_audio
from setuhaul.session_store import SessionStore, normalise_phone
from setuhaul.workflow import notify_decision

HERE = os.path.dirname(os.path.abspath(__file__))
DEV = os.environ.get("SETUHAUL_DEV") == "1"
SETTINGS = Settings.from_env()
CLOCK = Clock.from_env()                   # one mode per process, shown in every trace
DEMO_WORLD = CLOCK.mode == "snapshot" and os.environ.get("DEMO_WORLD", "1") == "1"
log = logging.getLogger("setuhaul.app")


def _init_db():
    path = os.environ.get("SETUHAUL_DB")
    if not path:
        path = db.fresh_copy(os.path.join(HERE, "out", "app_world.db"))
        rewind = os.environ.get("SETUHAUL_REWIND_TO")
        con = db.connect(path)
        chan_db.ensure(con)
        if rewind:
            world.rewind(con, ts(rewind))
        elif DEMO_WORLD:                   # the 4 Aug world: rewind to 08:20, replay fixed events up to the anchor
            demo_world.start(con)
            demo_world.apply_due(con, CLOCK.now())
        con.close()
    con = db.connect(path)
    db.ensure_extensions(con)              # a persistent or restored DB gets any missing guard back
    chan_db.ensure(con)
    con.close()
    return path


DB_PATH = _init_db()
STORE = SessionStore.from_env()
LLM = make_fallback()                      # None unless ANTHROPIC_API_KEY is set
CHANNEL = get_channel(SETTINGS)
VOICE = make_voice()                       # None unless VOICE=sarvam|selfhosted (speech models, not an LLM)
SERVICE = ChannelService(lambda: db.connect(DB_PATH), STORE, CHANNEL, SETTINGS, CLOCK, llm=LLM, demo_world=DEMO_WORLD, voice=VOICE)
_WAKE = {"event": None, "loop": None}


@contextlib.asynccontextmanager
async def lifespan(_app):
    task = None
    if SETTINGS.worker and os.environ.get("OUTBOX_WORKER", "1") == "1":
        _WAKE["event"], _WAKE["loop"] = asyncio.Event(), asyncio.get_running_loop()
        SERVICE.on_enqueued = lambda: _WAKE["loop"].call_soon_threadsafe(_WAKE["event"].set)
        task = asyncio.create_task(_worker_loop())
    log.info("start: %s", json.dumps(dict(SETTINGS.public(), **CLOCK.describe())))
    yield
    if task:
        task.cancel()


app = FastAPI(title="SetuHaul driver-exception service", lifespan=lifespan)


def _con():
    return db.connect(DB_PATH)             # one connection per request; SQLite WAL in MVP 1 (a pool once MVP 2 moves to Postgres or MongoDB)


def _as_of(override=None, epoch=None):
    if override:
        return ts(override)
    if epoch:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone(IST)
    return CLOCK.now()                     # live: real IST time; snapshot: the 4 Aug clock


# ---------------------------------------------------------------- outbound WhatsApp
def send_whatsapp(con, to_phone, text, as_of):
    """Record in the outbox (always), then deliver through the Cloud API when configured."""
    capacity.outbox(con, as_of, "WHATSAPP", to_phone, "DRIVER_REPLY", text)
    token, phone_id = os.environ.get("WHATSAPP_TOKEN"), os.environ.get("WHATSAPP_PHONE_ID")
    if not (token and phone_id):
        return "QUEUED"
    req = urllib.request.Request(
        f"https://graph.facebook.com/v21.0/{phone_id}/messages",
        data=json.dumps({"messaging_product": "whatsapp", "to": normalise_phone(to_phone).lstrip("+"),
                         "type": "text", "text": {"body": text}}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        return "SENT"
    except Exception:
        return "FAILED"                    # the outbox row stays QUEUED for a retry worker


# ---------------------------------------------------------------- one driver turn
def driver_turn(phone, text, as_of, external_id=None):
    """Identity -> rate limit -> dedupe -> lock -> history -> graph -> reply -> session."""
    if not STORE.allow(phone):
        return {"state": "RATE_LIMITED", "reply": None, "tools": ["rate_limit:BLOCKED"]}
    if not STORE.first_time(external_id):
        return {"state": "DUPLICATE", "reply": None, "tools": [f"dedupe:SUPPRESSED ({external_id})"]}
    con = _con()
    try:
        drv = A.driver_for_phone(con, phone)
        if not drv:
            return {"state": "UNKNOWN_SENDER", "reply": None, "tools": ["identify:REJECTED"]}
        tok = STORE.lock(phone)
        if not tok:
            return {"state": "BUSY", "reply": None, "tools": ["lock:BUSY (previous turn still running)"]}
        try:
            history = STORE.history(phone)
            STORE.append_history(phone, "human", text, ts=iso(as_of), wa_id=external_id)
            out = run_turn(con, drv["phone"], text, as_of, external_id, history=history, llm_fallback=LLM)
            if out.get("reply"):
                out["delivery"] = send_whatsapp(con, drv["phone"], out["reply"], as_of)
                STORE.append_history(phone, "ai", out["reply"], ts=iso(as_of), state=out.get("state"),
                                     lang=out.get("lang"), tools=out.get("tools"))
            STORE.save_driver(phone, {"driver_id": drv["driver_id"], "shipment_id": out.get("shipment_id"),
                                      "state": out.get("state"), "lang": out.get("lang"), "last_seen": iso(as_of)})
            return out
        finally:
            STORE.unlock(phone, tok)
    finally:
        con.close()


@app.get("/webhook/whatsapp", response_class=PlainTextResponse)
def wa_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == os.environ.get("WHATSAPP_VERIFY_TOKEN"):
        return q.get("hub.challenge", "")
    raise HTTPException(403, "verification failed")


@app.post("/webhook/whatsapp")
async def wa_inbound(request: Request, x_hub_signature_256: str = Header(default=""),
                     x_setuhaul_as_of: str = Header(default="")):
    body = await request.body()
    secret = os.environ.get("WHATSAPP_APP_SECRET")
    if secret and not A.verify_whatsapp_signature(body, x_hub_signature_256, secret):
        raise HTTPException(401, "bad signature")
    if not secret and not DEV:
        raise HTTPException(500, "WHATSAPP_APP_SECRET not configured")
    payload = json.loads(body or b"{}")
    results = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []):
                if m.get("type") != "text":
                    continue                # voice notes and images: a later release
                as_of = _as_of(x_setuhaul_as_of if DEV else None, m.get("timestamp"))
                results.append(driver_turn(m["from"], m["text"]["body"], as_of, m.get("id")))
    return {"processed": len(results), "results": results}


@app.post("/dev/driver-message")
def dev_driver_message(msg: dict):
    if not DEV:
        raise HTTPException(404)
    return driver_turn(msg["phone"], msg["text"], _as_of(msg.get("as_of")), msg.get("external_id"))


# ---------------------------------------------------------------- coordinator auth
def _coordinator(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "sign in first")
    try:
        return A.verify_coordinator(authorization[7:], STORE)
    except A.AuthError as e:
        raise HTTPException(401, str(e))


@app.post("/auth/dev-login")
def dev_login(body: dict):
    if not DEV:
        raise HTTPException(404)
    con = _con()
    try:
        who = A.coordinator_for(con, body.get("user_id", ""))
    finally:
        con.close()
    if not who:
        raise HTTPException(403, "not a coordinator")
    token, claims = A.issue_dev_token(who, os.environ.get("SETUHAUL_DEV_SECRET", "dev-only-secret"))
    STORE.start_coordinator(who["user_id"], claims, A.TOKEN_TTL_S)
    return {"token": token, "expires_at": claims["exp"], "role": who["role"], "facility_id": who["facility_id"]}


@app.post("/auth/session")
def cognito_session(authorization: str = Header(default="")):
    """Production: the console signs in with Cognito, then registers the token here."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(401)
    try:
        claims = A.verify_cognito(authorization[7:])
    except (A.AuthError, KeyError) as e:
        raise HTTPException(401, str(e))
    con = _con()
    try:
        who = A.coordinator_for(con, claims.get("email") or claims.get("phone_number", ""))
    finally:
        con.close()
    if not who:
        raise HTTPException(403, "not a coordinator")
    STORE.start_coordinator(who["user_id"], dict(who, jti=claims["jti"]), max(60, claims["exp"] - int(datetime.now().timestamp())))
    return who


@app.post("/auth/logout")
def logout(authorization: str = Header(default="")):
    c = _coordinator(authorization)
    STORE.logout(c["jti"])
    return {"ok": True}


# ---------------------------------------------------------------- coordinator console
@app.get("/console/handoffs")
def handoffs(authorization: str = Header(default="")):
    c = _coordinator(authorization)
    con = _con()
    try:
        rs = con.execute("""SELECT e.* FROM escalations e LEFT JOIN shipments s USING(shipment_id)
                            WHERE e.status='OPEN' AND COALESCE(s.destination_facility_id, ?)=? ORDER BY e.sla_due_at""",
                         (c["facility_id"], c["facility_id"])).fetchall()
        return [dict(r, handoff=json.loads(r["handoff_json"])) for r in rs]
    finally:
        con.close()


@app.get("/console/holds")
def holds(authorization: str = Header(default="")):
    c = _coordinator(authorization)
    con = _con()
    try:
        rs = con.execute("""SELECT h.* FROM slot_holds h JOIN appointment_slots s USING(slot_id)
                            WHERE h.hold_status='PENDING_APPROVAL' AND s.facility_id=?""", (c["facility_id"],)).fetchall()
        return [dict(r) for r in rs]
    finally:
        con.close()


@app.post("/console/holds/{hold_id}/decide")
def decide(hold_id: str, body: dict, authorization: str = Header(default=""), x_setuhaul_as_of: str = Header(default="")):
    c = _coordinator(authorization)
    as_of = _as_of(x_setuhaul_as_of if DEV else None)
    con = _con()
    try:
        h = con.execute("""SELECT h.*, s.facility_id FROM slot_holds h JOIN appointment_slots s USING(slot_id)
                           WHERE hold_id=?""", (hold_id,)).fetchone()
        if not h or h["facility_id"] != c["facility_id"]:
            raise HTTPException(404, "no such hold at your facility")
        res = capacity.decide(con, hold_id, bool(body.get("approve")), c["name"], as_of, body.get("note", ""))
        if res.get("code") == "HOLD_NOT_LIVE":
            return res
        note = notify_decision(con, h["driver_id"], res, as_of)
        phone = con.execute("SELECT phone FROM drivers WHERE driver_id=?", (h["driver_id"],)).fetchone()["phone"]
        ev = _decision_evidence(res)
        order = con.execute("SELECT order_reference FROM shipments WHERE shipment_id=?", (h["shipment_id"],)).fetchone()[0]
        tr = SERVICE.notify_driver(con, h["driver_id"], note["reply"], ev, as_of, kind="APPROVAL_OUTCOME", order=order)
        note["delivery"], note["outbox_ids"] = "QUEUED", tr["outbox_ids"]
        STORE.append_history(phone, "ai", note["reply"], ts=iso(as_of), state=note["state"], tools=note["tools"])
        return {"decision": res.get("code"), "driver_told": note}
    finally:
        con.close()


@app.get("/console/board")
def board(authorization: str = Header(default=""), x_setuhaul_as_of: str = Header(default="")):
    c = _coordinator(authorization)
    con = _con()
    try:
        out = planner.health(con, c["facility_id"], _as_of(x_setuhaul_as_of if DEV else None))
        frozen = capacity.frozen_shipments(con, [b.get("shipment_id") for b in out])
        for b in out:                          # a frozen booking is never released or marked no-show
            if b.get("shipment_id") in frozen:
                b.update(frozen=True, action="SAFETY case open: do not release or mark no-show; clear the freeze first.")
        return out
    finally:
        con.close()


def _decision_evidence(res):
    """What the coordinator's decision proves, for the output guard (booked only if CONFIRMED)."""
    o = res.get("option") or {}
    ev = {"docks": set(), "times": set(), "confirmed": res.get("ok") and res.get("code") == "CONFIRMED"}
    if o.get("dock_code"):
        ev["docks"].add(o["dock_code"])
    if o.get("start"):
        st = ts(o["start"])
        ev["times"] |= {ampm(st), ampm(add(st, -CFG.GATE_TO_DOCK_MIN))}
    return ev


@app.get("/health")
def health():
    return {"ok": True, "sessions": STORE.backend, "llm_fallback": LLM is not None,
            "db": os.path.basename(DB_PATH)}


# ---------------------------------------------------------------- Twilio WhatsApp
def _public_url(request: Request):
    """The exact URL Twilio signed: TWILIO_WEBHOOK_BASE_URL + path, else the forwarded scheme/host (ngrok)."""
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if SETTINGS.twilio_webhook_base_url:
        return SETTINGS.twilio_webhook_base_url + path
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}{path}"


async def _twilio_request(request: Request):
    body = (await request.body()).decode("utf-8", "replace")
    params = parse_qs(body, keep_blank_values=True)
    return ChannelRequest(url=_public_url(request), headers={k.lower(): v for k, v in request.headers.items()}, params=params)


def _webhook_adapter():
    return CHANNEL if isinstance(CHANNEL, TwilioChannel) else TwilioChannel(SETTINGS)


EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


@app.post("/webhook/twilio")
async def twilio_inbound(request: Request, background: BackgroundTasks):
    req = await _twilio_request(request)
    adapter = _webhook_adapter()
    if not adapter.verify(req):                                  # before ANY processing
        log.warning("twilio signature rejected for %s", req.url)
        raise HTTPException(403, "invalid signature")
    msgs = adapter.parse_inbound(req)
    for m in SERVICE.accept(msgs):                               # dedupe only; the turn runs after the response
        background.add_task(SERVICE.process, m)
    return Response(EMPTY_TWIML, media_type="application/xml")


@app.post("/webhook/twilio/status")
async def twilio_status(request: Request):
    req = await _twilio_request(request)
    adapter = _webhook_adapter()
    if not adapter.verify(req):
        raise HTTPException(403, "invalid signature")
    st = adapter.parse_status(req)
    con = _con()
    try:
        OB.apply_status(con, st, CLOCK.real_now())
    finally:
        con.close()
    return Response(EMPTY_TWIML, media_type="application/xml")


# ---------------------------------------------------------------- outbox worker
class _Pacer:
    """Sandbox: one message every TWILIO_SANDBOX_SEND_INTERVAL_S (Twilio Sandbox docs)."""
    def __init__(self, interval):
        self.interval, self.last, self.lock = interval, 0.0, threading.Lock()

    def __call__(self):
        with self.lock:
            wait = self.last + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()


PACE = _Pacer(CFG.TWILIO_SANDBOX_SEND_INTERVAL_S) if SETTINGS.twilio_sandbox and SETTINGS.channel in ("twilio", "sim") else None
_last_purge = {"t": 0.0}


def worker_tick():
    """One pass: page new escalations, walk missed SLAs, send due messages, raise delivery alarms."""
    con = _con()
    try:
        as_of, real = CLOCK.now(), CLOCK.real_now()
        if DEMO_WORLD:
            demo_world.apply_due(con, as_of)
        SF.page_new(con, as_of, real, SETTINGS)
        SF.sla_sweep(con, as_of, real, SETTINGS)
        OB.reconcile(con, CHANNEL, SETTINGS, CLOCK)          # rows left in SENDING: prove, never resend blindly
        sent = OB.drain(con, CHANNEL, SETTINGS, CLOCK, pace=PACE)
        OB.check_delivery_sla(con, CLOCK.real_now())
        if time.time() - _last_purge["t"] > 3600:
            OB.purge_bodies(con, real)
            _last_purge["t"] = time.time()
        return sent
    finally:
        con.close()


async def _worker_loop():
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(None, worker_tick)        # SDK calls never block the event loop
        except Exception:
            log.exception("outbox worker tick failed")
        try:
            await asyncio.wait_for(_WAKE["event"].wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        _WAKE["event"].clear()


# ---------------------------------------------------------------- health
@app.get("/healthz")
def healthz():
    return {"ok": True}


_TWILIO_CHECK = {"at": 0.0, "ok": None, "detail": None}


def _twilio_ready():
    if SETTINGS.channel != "twilio":
        return True, f"n/a (CHANNEL={SETTINGS.channel})"
    if not (SETTINGS.twilio_account_sid and SETTINGS.twilio_auth_token and SETTINGS.twilio_whatsapp_from):
        return False, "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM missing"
    if time.time() - _TWILIO_CHECK["at"] > 300:
        try:
            acct = CHANNEL.client.api.v2010.accounts(SETTINGS.twilio_account_sid).fetch()
            _TWILIO_CHECK.update(ok=acct.status == "active", detail=f"account {acct.status}")
        except Exception as e:
            _TWILIO_CHECK.update(ok=False, detail=type(e).__name__)
        _TWILIO_CHECK["at"] = time.time()
    return _TWILIO_CHECK["ok"], _TWILIO_CHECK["detail"]


@app.get("/media/{name}")
def media(name: str):
    """A spoken reply, fetched by WhatsApp. Unguessable token, kept 24 h; nothing else is served here."""
    token = name.split(".")[0]
    con = _con()
    try:
        hit = load_audio(con, token, CLOCK.real_now())
    finally:
        con.close()
    if not hit:
        raise HTTPException(404, "not found")
    return Response(content=hit[0], media_type=hit[1], headers={"Cache-Control": "private, max-age=3600"})


@app.get("/readyz")
def readyz():
    checks = {}
    try:
        c = _con()
        c.execute("SELECT 1 FROM wa_delivery LIMIT 1").fetchall()
        problems = db.guard_problems(c)    # SQLite (MVP 1): the booking guarantee lives in triggers
        c.close()
        checks["db"] = (False, "; ".join(problems)) if problems else (True, "ok, booking guards present")
    except Exception as e:
        checks["db"] = (False, type(e).__name__)
    try:
        checks["redis"] = (True, "ping ok") if STORE.backend == "redis" and STORE.r.ping() else (True, "memory store")
    except Exception as e:
        checks["redis"] = (False, type(e).__name__)
    checks["twilio"] = _twilio_ready()
    if SETTINGS.voice != "off":
        checks["voice"] = (True, f"{VOICE.backend} speech, replies at {SETTINGS.media_base_url}/media/") if VOICE and SETTINGS.media_base_url \
            else (False, "VOICE is set but the backend or MEDIA_BASE_URL is missing")
    ok = all(v[0] for v in checks.values())
    body = {"ok": ok, "checks": {k: {"ok": v[0], "detail": v[1]} for k, v in checks.items()}, **CLOCK.describe(),
            "settings": SETTINGS.public()}
    return JSONResponse(body, status_code=200 if ok else 503)


# ---------------------------------------------------------------- console additions
@app.post("/console/handoffs/{esc_id}/ack")
def ack_handoff(esc_id: str, authorization: str = Header(default="")):
    c = _coordinator(authorization)
    con = _con()
    try:
        if not SF.ack(con, esc_id, c["name"], CLOCK.now()):
            raise HTTPException(404, "no page for this escalation")
        return {"acked": esc_id, "by": c["name"]}
    finally:
        con.close()


@app.post("/console/safety/{esc_id}/clear")
def clear_safety(esc_id: str, authorization: str = Header(default="")):
    c = _coordinator(authorization)
    con = _con()
    try:
        n = SF.clear(con, esc_id, f"{c['contact_id']} {c['name']}" if c.get("contact_id") else c["name"], CLOCK.now())
        if not n:
            raise HTTPException(404, "no open freeze for this escalation")
        return {"cleared": esc_id, "shipments": n, "by": c["name"]}
    finally:
        con.close()


@app.post("/console/proactive/{shipment_id}")
def proactive(shipment_id: str, authorization: str = Header(default="")):
    _coordinator(authorization)
    con = _con()
    try:
        return SERVICE.proactive_checkin(con, shipment_id, CLOCK.now())
    finally:
        con.close()


@app.get("/console/alarms")
def alarms(authorization: str = Header(default="")):
    _coordinator(authorization)
    con = _con()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM ops_alarms ORDER BY id DESC LIMIT 200")]
    finally:
        con.close()


@app.get("/console/outbox")
def outbox_view(authorization: str = Header(default="")):
    _coordinator(authorization)
    con = _con()
    try:
        rs = con.execute("""SELECT msg_id, role, status, provider_sid, sent_kind, attempts, error_code, error_message, template_ref,
                            proactive, created_real, sent_real, delivered_real, read_real, to_e164 FROM wa_delivery ORDER BY created_real DESC LIMIT 200""")
        return [dict(dict(r), to_e164=mask(r["to_e164"])) for r in rs]
    finally:
        con.close()


@app.post("/console/outbox/{msg_id}/resolve")
def resolve_outbox(msg_id: str, body: dict, authorization: str = Header(default="")):
    """Settle a NEEDS_REVIEW message after checking Twilio's log: {"action": "sent", "provider_sid": "SM..."} or {"action": "resend"}."""
    c = _coordinator(authorization)
    con = _con()
    try:
        ok = OB.resolve_review(con, msg_id, body.get("action"), CLOCK.real_now(), body.get("provider_sid"), who=c["name"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        con.close()
    if not ok:
        raise HTTPException(404, "no message awaiting review with this id")
    return {"resolved": msg_id, "action": body.get("action")}


@app.post("/dev/clock")
def dev_clock(body: dict):
    if not DEV:
        raise HTTPException(404)
    try:
        CLOCK.jump_to(body["as_of"])
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    con = _con()
    try:
        applied = demo_world.apply_due(con, CLOCK.now()) if DEMO_WORLD else []
    finally:
        con.close()
    return dict(CLOCK.describe(), world_events=applied)


@app.get("/dev/trace/{message_sid}")
def dev_trace(message_sid: str):
    if not DEV:
        raise HTTPException(404)
    con = _con()
    try:
        r = con.execute("""SELECT t.json FROM wa_inbound i JOIN channel_traces t ON t.trace_id=i.trace_id WHERE i.message_sid=?""",
                        (message_sid,)).fetchone()
        return json.loads(r[0]) if r else {"status": "unknown or not processed yet"}
    finally:
        con.close()
