"""The WhatsApp channel service: one inbound message -> zero or more outbound messages in the outbox.

    webhook (signature already verified) -> accept(): dedupe by MessageSid (Redis + SQLite), log
    background -> process():
        identity (verified sender number only) -> rate limit -> per-driver lock
        -> 24 h window opens -> STOP/START -> first-contact privacy notice
        -> voice note? speech-to-text (voice.py) -> SAFETY freeze? -> unsupported types -> offer binding
        -> graph.run_turn (unchanged engine; it only ever sees text)
        -> SAFETY freeze on a new safety escalation -> render (guard body + option titles)
        -> outbox rows in ONE transaction (+ a spoken reply when the driver spoke) -> worker sends after COMMIT

The engine never sees the provider. Nothing here decides a booking.
"""
import hashlib
import json
import logging
import time
import uuid
from datetime import timedelta

from . import auth as A
from . import config as C
from . import nlu
from . import offers as OF
from . import replies as R
from . import safety as S
from . import escalation as E
from . import voice as V
from .chan_db import alarm
from .channels.base import OutboundMessage, Option
from .clock import iso, ts, ampm, add
from .db import write_txn, one
from .graph import run_turn
from .outbox import enqueue
from .session_store import normalise_phone
from .tracing import mask, traced

log = logging.getLogger("setuhaul.inbound")
STOP_WORDS = {"STOP", "STOPALL", "STOP ALL", "UNSUBSCRIBE", "OPT OUT", "OPTOUT"}
START_WORDS = {"START", "UNSTOP", "OPT IN"}


def _hash(phone):
    return hashlib.sha256(normalise_phone(phone).encode()).hexdigest()[:32]


class ChannelService:
    def __init__(self, connect, store, channel, settings, clock, llm=None, demo_world=False, voice=None):
        self.connect, self.store, self.channel, self.s, self.clock, self.llm = connect, store, channel, settings, clock, llm
        self.voice = voice                      # speech backend (voice.make_voice) or None: voice notes get 'please type'
        self.demo_world = demo_world
        self.on_enqueued = None                 # app sets this to wake the outbox worker

    # ------------------------------------------------------------ fast path (inside the webhook)
    def accept(self, msgs):
        """Dedupe by provider message id. Returns the messages that are new. Fast: no engine work."""
        fresh = []
        con = self.connect()
        try:
            for m in msgs:
                if not self.store.first_time(f"{m.provider}:{m.message_id}"):
                    continue
                with write_txn(con):
                    cur = con.execute("""INSERT OR IGNORE INTO wa_inbound(message_sid, provider, e164, msg_type, body, choice_id,
                                         received_real, status) VALUES (?,?,?,?,?,?,?, 'RECEIVED')""",
                                      (m.message_id, m.provider, _hash(m.from_e164), m.type, m.text, m.choice_id, iso(self.clock.real_now())))
                if cur.rowcount == 1:                 # SQLite is the durable fallback when Redis was flushed
                    fresh.append(m)
        finally:
            con.close()
        return fresh

    # ------------------------------------------------------------ slow path (background)
    @traced("whatsapp_turn")
    def process(self, msg):
        as_of, real = self.clock.now(), self.clock.real_now()
        trace = {"trace_id": uuid.uuid4().hex, "message_sid": msg.message_id, "from": mask(msg.from_e164), "type": msg.type,
                 "provider": msg.provider, "clock_mode": self.clock.mode, "as_of": iso(as_of), "outbox_ids": [], "status": None}
        con = self.connect()
        try:
            if self.demo_world:
                from . import demo_world
                applied = demo_world.apply_due(con, as_of)
                if applied:
                    trace["world_events"] = applied
            self._process(con, msg, as_of, real, trace)
        except Exception as e:                        # never leak; never leave the driver without an answer
            log.exception("turn failed %s", msg.message_id)
            trace["status"] = "ERROR"
            trace["error"] = type(e).__name__
            try:
                with write_txn(con):
                    alarm(con, "TURN_ERROR", msg.message_id, {"error": type(e).__name__, "detail": mask(str(e))[:200]}, real, as_of)
            except Exception:
                log.exception("could not raise alarm")
        finally:
            try:
                with write_txn(con):
                    con.execute("UPDATE wa_inbound SET status=?, as_of=?, trace_id=? WHERE message_sid=?",
                                (trace["status"] or "PROCESSED", iso(as_of), trace["trace_id"], msg.message_id))
                    con.execute("INSERT INTO channel_traces VALUES (?,?,?,?)",
                                (trace["trace_id"], iso(real), "inbound", json.dumps(trace, default=str, ensure_ascii=False)))
            finally:
                con.close()
        log.info("turn %s", json.dumps({k: trace.get(k) for k in ("trace_id", "message_sid", "from", "type", "status", "state",
                                                                  "window_open", "outbox_ids", "clock_mode", "as_of")}, default=str))
        if trace["outbox_ids"] and self.on_enqueued:
            self.on_enqueued()
        return trace

    def _process(self, con, msg, as_of, real, trace):
        drv = A.driver_for_phone(con, msg.from_e164)
        staff = None if drv else self._staff_for(con, msg.from_e164)
        if not drv and not staff:
            return self._unknown(con, msg, as_of, real, trace)
        if not self.store.allow(msg.from_e164):
            trace["status"] = "RATE_LIMITED"
            return
        tok = self._lock(msg.from_e164)
        if not tok:
            trace["status"] = "BUSY"
            with write_txn(con):
                alarm(con, "LOCK_TIMEOUT", msg.message_id, {"from": mask(msg.from_e164)}, real, as_of)
            self.store.r.delete(f"dedupe:{msg.provider}:{msg.message_id}")     # let a provider retry through
            con.execute("DELETE FROM wa_inbound WHERE message_sid=?", (msg.message_id,))
            return
        try:
            role = "DRIVER" if drv else "STAFF"
            contact = self._touch_contact(con, msg, role, drv, staff, real)
            trace["window_open"] = True                                   # an inbound message always (re)opens it
            if role == "STAFF":
                return self._staff(con, msg, staff, as_of, real, trace)
            return self._driver(con, msg, drv, contact, as_of, real, trace)
        finally:
            self.store.unlock(msg.from_e164, tok)

    # ------------------------------------------------------------ helpers
    def _lock(self, phone):
        deadline = time.monotonic() + C.LOCK_WAIT_S
        while True:
            tok = self.store.lock(phone)
            if tok or time.monotonic() >= deadline:
                return tok
            time.sleep(0.2)

    def _staff_for(self, con, phone):
        want = normalise_phone(phone)
        for c in con.execute("SELECT * FROM facility_contacts WHERE active_flag=1 AND phone IS NOT NULL"):
            if normalise_phone(c["phone"]) == want:
                return dict(c)
        return None

    def _touch_contact(self, con, msg, role, drv, staff, real):
        e = normalise_phone(msg.from_e164)
        with write_txn(con):
            con.execute("""INSERT INTO wa_contacts(e164, role, driver_id, contact_id, last_inbound_real) VALUES (?,?,?,?,?)
                           ON CONFLICT(e164) DO UPDATE SET last_inbound_real=excluded.last_inbound_real, role=excluded.role,
                           driver_id=excluded.driver_id, contact_id=excluded.contact_id""",
                        (e, role, drv and drv["driver_id"], staff and staff["contact_id"], iso(real)))
        return one(con, "SELECT * FROM wa_contacts WHERE e164=?", e)

    def _lang(self, con, driver_id):
        c = one(con, "SELECT lang FROM conversations WHERE driver_id=?", driver_id)
        return (c and c["lang"]) or "en"

    def _queue(self, con, as_of, real, trace, msgs, role="DRIVER"):
        """All outbound messages of one turn, in one transaction. Each body passes the guard first."""
        with write_txn(con):
            for m, kind, ev, extra in msgs:
                bad = R.guard(m.body, ev) if m.body else []
                if bad:                                       # a channel-layer text must never state an unbacked fact
                    trace.setdefault("guard", []).extend(bad)
                    m.body, m.options, m.kind = R.t("unknown", m.lang), [], "text"
                trace["outbox_ids"].append(enqueue(con, as_of, real, m, role=role, kind=kind, trace_id=trace["trace_id"], **extra))

    def _text(self, to, lang, body, kind="text"):
        return OutboundMessage(to_e164=normalise_phone(to), kind=kind, body=body, lang=lang)

    # ------------------------------------------------------------ unknown senders
    def _unknown(self, con, msg, as_of, real, trace):
        trace["status"] = "UNKNOWN_SENDER"
        h = _hash(msg.from_e164)
        r = one(con, "SELECT * FROM wa_unknown_senders WHERE e164_hash=?", h)
        log.warning("unknown sender %s", mask(msg.from_e164))
        if r and real - ts(r["last_refused_real"]) < timedelta(hours=C.UNKNOWN_SENDER_REFUSAL_EVERY_H):
            with write_txn(con):
                con.execute("UPDATE wa_unknown_senders SET count=count+1 WHERE e164_hash=?", (h,))
            return
        with write_txn(con):
            con.execute("""INSERT INTO wa_unknown_senders VALUES (?,?,1) ON CONFLICT(e164_hash) DO UPDATE SET
                           last_refused_real=excluded.last_refused_real, count=count+1""", (h, iso(real)))
            con.execute("""INSERT INTO wa_contacts(e164, role, last_inbound_real) VALUES (?, 'UNKNOWN', ?)
                           ON CONFLICT(e164) DO UPDATE SET last_inbound_real=excluded.last_inbound_real""",
                        (normalise_phone(msg.from_e164), iso(real)))
        self._queue(con, as_of, real, trace, [(self._text(msg.from_e164, "en", R.t("unknown_sender", "en")), "UNKNOWN_SENDER_REFUSAL",
                                               {}, {})], role="UNKNOWN")

    # ------------------------------------------------------------ staff
    def _staff(self, con, msg, staff, as_of, real, trace):
        ref = S.staff_ack(con, staff, msg.text, as_of)
        trace["status"] = "STAFF_ACK" if ref else "STAFF_HELP"
        body = R.t("staff_ack", "en", name=staff["contact_name"], ref=ref) if ref else R.t("staff_help", "en")
        self._queue(con, as_of, real, trace, [(self._text(msg.from_e164, "en", body), "STAFF_REPLY", {}, {})], role="STAFF")

    # ------------------------------------------------------------ drivers
    def _driver(self, con, msg, drv, contact, as_of, real, trace):
        did, lang = drv["driver_id"], self._lang(con, drv["driver_id"])
        word = (msg.text or "").strip().upper()
        out = []
        if word in STOP_WORDS:
            with write_txn(con):
                con.execute("UPDATE wa_contacts SET opted_out_at=? WHERE e164=?", (iso(real), contact["e164"]))
            trace["status"] = "OPTED_OUT"
            return self._queue(con, as_of, real, trace, [(self._text(msg.from_e164, lang, R.t("opted_out", lang)), "OPT_OUT", {},
                                                          {"ignore_opt_out": True})])
        if contact["opted_out_at"]:
            if word in START_WORDS:
                with write_txn(con):
                    con.execute("UPDATE wa_contacts SET opted_out_at=NULL WHERE e164=?", (contact["e164"],))
                trace["status"] = "OPTED_IN"
                return self._queue(con, as_of, real, trace, [(self._text(msg.from_e164, lang, R.t("opted_in", lang)), "OPT_IN", {}, {})])
            trace["status"] = "IGNORED_OPTED_OUT"
            return
        # ---- a voice note becomes text here; from now on the turn is an ordinary typed turn
        if msg.type == "media" and self.voice and any(V.is_audio(t) for t in msg.media_types) and msg.media_urls:
            if not self._hear(con, msg, drv, trace):
                trace["status"] = "VOICE_UNCLEAR"
                return self._queue(con, as_of, real, trace,
                                   [(self._text(msg.from_e164, lang, R.t("voice_unclear", lang)), "VOICE_UNCLEAR", {}, {})])
            word = msg.text.strip().upper().rstrip(".!")
            if word in STOP_WORDS:                       # "stop" said aloud works like typed STOP
                with write_txn(con):
                    con.execute("UPDATE wa_contacts SET opted_out_at=? WHERE e164=?", (iso(real), contact["e164"]))
                trace["status"] = "OPTED_OUT"
                return self._queue(con, as_of, real, trace, [(self._text(msg.from_e164, lang, R.t("opted_out", lang)), "OPT_OUT",
                                                              {}, {"ignore_opt_out": True})])
        if not contact["notice_sent_at"]:
            with write_txn(con):
                con.execute("UPDATE wa_contacts SET notice_sent_at=? WHERE e164=?", (iso(real), contact["e164"]))
            nl = nlu.detect_lang(msg.text or "", lang)            # read-only: the notice in the language they wrote in
            out.append((self._text(msg.from_e164, nl, R.t("privacy_notice", nl)), "PRIVACY_NOTICE", {}, {}))

        # ---- SAFETY freeze: no scheduling talk until a person clears it
        frozen = S.is_frozen(con, did)
        if frozen and not nlu.extract(msg.text or "", as_of).safety:
            S.add_followup(con, frozen, msg.text or f"[{msg.type}]", as_of)
            trace["status"] = "SAFETY_HOLD"
            out.append((self._text(msg.from_e164, lang, R.t("safety_hold", lang, ref=frozen)), "SAFETY_HOLD", {}, {}))
            return self._queue(con, as_of, real, trace, out)

        # ---- types the engine cannot read
        if msg.type in ("media", "other", "location"):
            return self._non_text(con, msg, drv, contact, lang, as_of, real, trace, out)
        if contact["unsupported_streak"]:
            with write_txn(con):
                con.execute("UPDATE wa_contacts SET unsupported_streak=0 WHERE e164=?", (contact["e164"],))

        # ---- offer binding
        b = OF.bind(con, did, msg, as_of)
        trace["binding"] = {k: b.get(k) for k in ("action", "why", "offer_id", "expired")}
        if b["action"] == "stale":
            trace["status"] = "STALE_CHOICE"
            out.append(self._resend_live(con, msg, did, lang, b, as_of, real))
            return self._queue(con, as_of, real, trace, out)

        # ---- the engine (unchanged)
        cap = {}
        history = self.store.history(msg.from_e164)
        self.store.append_history(msg.from_e164, "human", b["text"], ts=iso(as_of), wa_id=msg.message_id)
        try:
            res = run_turn(con, drv["phone"], b["text"], as_of, msg.message_id, history=history, llm_fallback=self.llm, capture=cap)
        except Exception as e:
            return self._turn_failed(con, msg, drv, lang, e, as_of, real, trace, out)
        trace.update(state=res.get("state"), tools=res.get("tools"), lang=res.get("lang"))
        trace["status"] = "DUPLICATE_TEXT" if res.get("reply") is None else "REPLIED"
        lang = res.get("lang") or lang

        # ---- SAFETY: freeze on a new safety escalation
        esc = res.get("escalation") or {}
        if any(t.startswith("escalate:SAFETY") for t in res.get("tools") or []):
            loc = contact["last_lat"] is not None and {"lat": contact["last_lat"], "lng": contact["last_lng"], "at": contact["last_location_at"]}
            trace["frozen"] = S.freeze(con, did, esc["escalation_id"], as_of, loc or None)

        # ---- offers follow the conversation
        used = (msg.message_id, b.get("choice") or (int(b["text"]) if b.get("offer_id") and b["text"].strip().isdigit() else None))
        oid, opts = OF.after_turn(con, did, res, as_of, real, used=used)
        if res.get("reply"):
            m = self._render(msg.from_e164, lang, res["reply"], oid, opts, cap.get("evidence") or {}, trace)
            out.append((m, "DRIVER_REPLY", cap.get("evidence") or {}, {}))
            if msg.voice:                                   # they spoke, so the answer is spoken too (after the text + buttons)
                sm = self._speak(con, msg.from_e164, lang, res["reply"], real, trace)
                if sm:
                    out.append((sm, "DRIVER_VOICE_REPLY", {}, {}))
            self.store.append_history(msg.from_e164, "ai", res["reply"], ts=iso(as_of), state=res.get("state"), lang=lang, tools=res.get("tools"))
        self.store.save_driver(msg.from_e164, {"driver_id": did, "shipment_id": res.get("shipment_id"), "state": res.get("state"),
                                               "lang": lang, "offer_id": oid, "last_seen": iso(as_of)})
        if out:
            self._queue(con, as_of, real, trace, out)

    # ------------------------------------------------------------ voice
    def _hear(self, con, msg, drv, trace):
        """Voice note -> msg.text (msg.type becomes 'text', msg.voice True). False when it cannot be understood.
        The transcript is the driver's message from here on: the same rules, guards and logs as typed text."""
        known = one(con, "SELECT lang FROM conversations WHERE driver_id=?", drv["driver_id"])
        hint = known and known["lang"]                       # None on a first message: the model detects the language
        t0 = time.monotonic()
        try:
            audio, ctype = self.channel.fetch_media(msg.media_urls[0], V.MAX_AUDIO_BYTES)
            tr = self.voice.transcribe(audio, ctype or msg.media_types[0], lang_hint=hint)
        except (V.VoiceError, ValueError, OSError, AttributeError) as e:
            trace["voice_in"] = {"ok": False, "error": type(e).__name__, "detail": str(e)[:120]}
            return False
        trace["voice_in"] = {"ok": bool(tr.text), "backend": tr.backend, "speech_lang": tr.speech_lang,
                             "ms": int((time.monotonic() - t0) * 1000), "transcript": tr.text[:300]}
        if not tr.text:
            return False
        text = V.spoken_times(tr.text)                         # "साढ़े ग्यारह बजे" -> "11:30" (voice only)
        if text != tr.text:
            trace["voice_in"]["normalised"] = text[:300]
        msg.text, msg.type, msg.voice = text, "text", True
        with write_txn(con):                                   # the log shows what was heard
            con.execute("UPDATE wa_inbound SET body=? WHERE message_sid=?", ("[voice] " + text, msg.message_id))
        return True

    def _speak(self, con, to, lang, reply, real, trace):
        """Reply text -> a voice note message, or None (the text reply has already been queued either way)."""
        if not self.s.media_base_url:
            trace["voice_out"] = {"ok": False, "error": "MEDIA_BASE_URL not set"}
            return None
        t0 = time.monotonic()
        try:
            audio, ctype = self.voice.speak(V.spoken(reply), lang)
            if ctype not in V.WHATSAPP_AUDIO:
                raise V.VoiceError(f"tts gave {ctype}, WhatsApp needs mp3/ogg")
            token = V.store_audio(con, audio, ctype, real)
        except V.VoiceError as e:
            trace["voice_out"] = {"ok": False, "error": str(e)[:120]}
            return None
        url = f"{self.s.media_base_url}/media/{token}.{V.EXT[ctype]}"
        trace["voice_out"] = {"ok": True, "bytes": len(audio), "ms": int((time.monotonic() - t0) * 1000), "lang": lang}
        return OutboundMessage(to_e164=normalise_phone(to), kind="media", body="", lang=lang, media_url=url)

    def _render(self, to, lang, body, offer_id, opts, evidence, trace):
        """Reply -> OutboundMessage. Options become buttons (<=3) or a list (4-10), ids bound to the offer."""
        m = OutboundMessage(to_e164=normalise_phone(to), kind="text", body=body, lang=lang, offer_id=offer_id)
        if not opts:
            return m
        options = [Option(id=o["id"], title=o["title"][:C.TWILIO_QR_TITLE_MAX if len(opts) <= C.TWILIO_QR_MAX_BUTTONS else C.TWILIO_LIST_ITEM_MAX])
                   for o in opts]
        bad = R.guard_options([o.__dict__ for o in options], evidence)
        if bad:                                             # numbered text only; the body was already guarded by the engine
            trace.setdefault("guard", []).extend(bad)
            return m
        m.options = options
        m.kind = "choices" if len(options) <= C.TWILIO_QR_MAX_BUTTONS else "list" if len(options) <= C.TWILIO_LIST_MAX_ITEMS else "text"
        return m

    def _resend_live(self, con, msg, did, lang, b, as_of, real):
        lv = b.get("live")
        if lv:
            with write_txn(con):                            # several live lists: keep only the newest
                con.execute("UPDATE wa_offers SET status='SUPERSEDED', closed_at=? WHERE driver_id=? AND status='LIVE' AND offer_id<>?",
                            (iso(as_of), did, lv["offer_id"]))
            body = R.t("options_changed", lang) + lv["body"]
            opts = json.loads(lv["options_json"])
            ev = R.evidence_from_text(lv["body"])
            m = self._render(msg.from_e164, lang, body, lv["offer_id"], opts, ev, {})
            return (m, "STALE_CHOICE_RESEND", ev, {})
        return (self._text(msg.from_e164, lang, R.t("offer_closed", lang)), "STALE_CHOICE_CLOSED", {}, {})

    def _non_text(self, con, msg, drv, contact, lang, as_of, real, trace, out):
        if msg.type == "location":
            with write_txn(con):
                con.execute("UPDATE wa_contacts SET last_lat=?, last_lng=?, last_location_at=?, unsupported_streak=0 WHERE e164=?",
                            (msg.lat, msg.lng, iso(as_of), contact["e164"]))
            trace["status"] = "LOCATION_NOTED"
            out.append((self._text(msg.from_e164, lang, R.t("location_noted", lang)), "LOCATION_NOTED", {}, {}))
            return self._queue(con, as_of, real, trace, out)
        streak = contact["unsupported_streak"] + 1
        with write_txn(con):
            con.execute("UPDATE wa_contacts SET unsupported_streak=? WHERE e164=?", (streak, contact["e164"]))
        if streak < C.UNSUPPORTED_BEFORE_HANDOFF:
            trace["status"] = "UNSUPPORTED_TYPE"
            out.append((self._text(msg.from_e164, lang, R.t("please_type", lang)), "PLEASE_TYPE", {}, {}))
            return self._queue(con, as_of, real, trace, out)
        conv = one(con, "SELECT shipment_id FROM conversations WHERE driver_id=?", drv["driver_id"])
        with write_txn(con):
            esc = E.escalate(con, "HUMAN_REQUESTED", "MEDIUM", conv and conv["shipment_id"], drv["driver_id"], as_of,
                             {"driver_sent": f"{streak} {msg.type} messages in a row ({', '.join(msg.media_types) or msg.type})",
                              "why": "the assistant reads typed text only"})
            con.execute("UPDATE wa_contacts SET unsupported_streak=0 WHERE e164=?", (contact["e164"],))
        trace["status"] = "UNSUPPORTED_HANDOFF"
        by = ts(esc["respond_by"])
        out.append((self._text(msg.from_e164, lang, R.t("human", lang, who=esc["routed_to"]["name"], by=ampm(by))), "HANDOFF",
                    {"times": {ampm(by)}, "docks": set(), "confirmed": False}, {}))
        return self._queue(con, as_of, real, trace, out)

    def _turn_failed(self, con, msg, drv, lang, e, as_of, real, trace, out):
        trace["status"] = "TURN_FAILED"
        trace["error"] = type(e).__name__
        frozen_guard = "SAFETY_FREEZE" in str(e)
        conv = one(con, "SELECT shipment_id FROM conversations WHERE driver_id=?", drv["driver_id"])
        with write_txn(con):
            alarm(con, "TURN_FAILED", msg.message_id, {"error": type(e).__name__, "safety_freeze": frozen_guard}, real, as_of)
            E.escalate(con, "HUMAN_REQUESTED", "HIGH", conv and conv["shipment_id"], drv["driver_id"], as_of,
                       {"driver_said": msg.text, "why": "the turn could not finish" + (" (a frozen safety booking blocked it)" if frozen_guard else "")})
        out.append((self._text(msg.from_e164, lang, R.t("turn_error", lang)), "TURN_ERROR", {}, {}))
        self._queue(con, as_of, real, trace, out)

    # ------------------------------------------------------------ messages not triggered by the driver
    def notify_driver(self, con, driver_id, text, evidence, as_of, kind="DRIVER_NOTICE", order=None):
        """Coordinator decisions and swaps. Free-form inside the 24 h window, else the booking-update template."""
        real = self.clock.real_now()
        d = one(con, "SELECT phone FROM drivers WHERE driver_id=?", driver_id)
        lang = self._lang(con, driver_id)
        m = OutboundMessage(to_e164=normalise_phone(d["phone"]), kind="text", body=text, lang=lang,
                            template_ref="driver_booking_update", template_vars={"order": order or "-"})
        trace = {"trace_id": uuid.uuid4().hex, "outbox_ids": [], "kind": kind, "clock_mode": self.clock.mode, "as_of": iso(as_of)}
        self._queue(con, as_of, real, trace, [(m, kind, evidence, {})])
        with write_txn(con):
            con.execute("INSERT INTO channel_traces VALUES (?,?,?,?)", (trace["trace_id"], iso(real), "notify", json.dumps(trace, default=str)))
        with write_txn(con):
            OF.close_all(con, driver_id, as_of)
        if self.on_enqueued:
            self.on_enqueued()
        return trace

    def proactive_checkin(self, con, shipment_id, as_of):
        """Health monitor 'are you on track?'. Always a template (business-initiated). Frozen drivers are not nudged."""
        real = self.clock.real_now()
        s = one(con, """SELECT s.*, d.phone, d.driver_name, d.driver_id AS did FROM shipments s JOIN drivers d ON d.driver_id=s.driver_id
                        WHERE s.shipment_id=?""", shipment_id)
        if not s:
            return {"status": "NO_SHIPMENT"}
        if S.frozen_shipment(con, shipment_id):
            return {"status": "FROZEN"}
        from .capacity import current_appointment
        from .workflow import facility_short
        cur = current_appointment(con, shipment_id)
        slot = ampm(ts(cur["slot_start_ts"])) if cur else "-"
        values = {"name": s["driver_name"].split()[0], "order": s["order_reference"],
                  "facility": facility_short(con, s["destination_facility_id"]), "slot": slot, "slot_day": "today"}
        from . import templates as TPL
        lang = self._lang(con, s["did"])
        m = OutboundMessage(to_e164=normalise_phone(s["phone"]), kind="template", template_ref="driver_checkin", template_vars=values,
                            body=TPL.preview("driver_checkin", values, lang), lang=lang, proactive=True)
        trace = {"trace_id": uuid.uuid4().hex, "outbox_ids": [], "kind": "proactive", "clock_mode": self.clock.mode, "as_of": iso(as_of)}
        self._queue(con, as_of, real, trace, [(m, "PROACTIVE_CHECKIN", {"times": {slot}, "docks": set(), "confirmed": False}, {})])
        if self.on_enqueued:
            self.on_enqueued()
        return {"status": "QUEUED", "outbox_ids": trace["outbox_ids"]}
