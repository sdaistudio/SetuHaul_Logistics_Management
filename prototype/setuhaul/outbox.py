"""Outbound delivery: the only code that hands a message to a channel adapter.

    turn (one DB transaction) --> outbox row + wa_delivery row (QUEUED) --> COMMIT
    worker: claim (QUEUED/RETRY, due) -> choose free-form or template -> channel.send()
            -> SENT + provider SID      | RETRY (429/5xx/network, backoff + jitter) | FAILED (+ alarm)
            | BLOCKED (window closed / proactive with no approved template / opted out) (+ alarm)
    status webhook: queued -> sent -> delivered -> read, or failed/undelivered + ErrorCode (+ alarm)
    delivery SLA: SENT but not delivered after DELIVERY_ALARM_MIN -> reply-time alarm + phone fallback

No double send. Twilio's Create Message has no idempotency key or client reference (Message resource
docs, checked 2026-11-19), so we make our own:
    claim            QUEUED/RETRY -> SENDING (claimed_until)
    before the call  send_ref (unique, per attempt), send_started_real and the exact body are COMMITTED
    the call         the status callback URL carries ?ref=<send_ref> (Twilio signs the full URL)
    after the call   provider SID stored -> SENT
A row left in SENDING (crash, timeout, 5xx) is never re-claimed for sending. reconcile() decides:
    no send_ref                  the call was never made                -> RETRY
    a signed callback with ref   proof it went out                      -> SENT (SID from the callback)
    Twilio lookup (To, From, time window, body): exactly one match      -> SENT with that SID
                                                  none (lookup succeeded) -> RETRY (proven not accepted)
                                                  several / lookup keeps failing / no lookup -> NEEDS_REVIEW + alarm
A person resolves NEEDS_REVIEW (console): mark sent with a SID, or release for one more send.
"""
import json
import logging
import random
import uuid
from datetime import timedelta

from . import config as C
from . import capacity as K
from . import templates as TPL
from .chan_db import alarm
from .channels.base import OutboundMessage
from .clock import iso, ts
from .db import write_txn, one, rows
from .tracing import mask

log = logging.getLogger("setuhaul.outbox")
CLAIM_S = 60
RECONCILE_AFTER_S = 30            # give the provider time to record an in-flight request before looking it up
RECONCILE_MAX_LOOKUPS = 3
RANK = {"queued": 0, "accepted": 0, "sending": 0, "sent": 1, "delivered": 2, "read": 3}
TERMINAL_BAD = {"failed", "undelivered"}


def window_open(con, e164, real_now):
    c = one(con, "SELECT last_inbound_real FROM wa_contacts WHERE e164=?", e164)
    if not c or not c["last_inbound_real"]:
        return False
    return real_now - ts(c["last_inbound_real"]) < timedelta(hours=C.WHATSAPP_SESSION_WINDOW_H)


def enqueue(con, as_of, real_now, msg: OutboundMessage, role="DRIVER", kind="DRIVER_REPLY", trace_id=None, ignore_opt_out=False):
    """Queue one WhatsApp message. MUST run inside the caller's write transaction."""
    text = msg.body or (f"[voice note {msg.media_url}]" if msg.kind == "media" else TPL.preview(msg.template_ref, msg.template_vars, msg.lang))
    msg_id = K.outbox(con, as_of, "WHATSAPP", msg.to_e164, kind, text)
    msg.outbox_id = msg_id
    payload = msg.as_dict()
    payload["ignore_opt_out"] = ignore_opt_out
    con.execute("""INSERT INTO wa_delivery(msg_id, to_e164, role, payload_json, proactive, template_ref, status,
                   next_attempt_real, created_real, updated_real, trace_id) VALUES (?,?,?,?,?,?, 'QUEUED', ?,?,?,?)""",
                (msg_id, msg.to_e164, role, json.dumps(payload, ensure_ascii=False, default=str), int(msg.proactive), msg.template_ref,
                 iso(real_now), iso(real_now), iso(real_now), trace_id))
    return msg_id


def _backoff(attempt):
    base = min(C.SEND_BACKOFF_CAP_S, C.SEND_BACKOFF_BASE_S * (2 ** max(0, attempt - 1)))
    return base / 2 + random.uniform(0, base / 2)              # "equal jitter"


def claim(con, real_now, limit=20):
    with write_txn(con):
        due = rows(con, """SELECT msg_id FROM wa_delivery WHERE provider_sid IS NULL AND status IN ('QUEUED','RETRY')
                           AND next_attempt_real<=? ORDER BY created_real, msg_id LIMIT ?""", iso(real_now), limit)
        ids = [d["msg_id"] for d in due]
        for mid in ids:
            con.execute("""UPDATE wa_delivery SET status='SENDING', attempts=attempts+1, claimed_until_real=?, updated_real=?,
                           send_ref=NULL, send_started_real=NULL, sent_body=NULL
                           WHERE msg_id=? AND provider_sid IS NULL""",
                        (iso(real_now + timedelta(seconds=CLAIM_S)), iso(real_now), mid))
        return ids


def _final_form(con, d, msg, real_now, settings):
    """Decide what may legally go out now. Returns (msg, None) or (None, (code, reason))."""
    payload = json.loads(d["payload_json"])
    if not payload.get("ignore_opt_out"):
        c = one(con, "SELECT opted_out_at FROM wa_contacts WHERE e164=?", d["to_e164"])
        if c and c["opted_out_at"]:
            return None, ("OPTED_OUT", "recipient sent STOP")
    if msg.kind == "template":
        need = msg.template_ref
    elif msg.proactive:
        need = msg.template_ref or "(none)"
    elif window_open(con, d["to_e164"], real_now):
        return msg, None
    else:
        need = msg.template_ref or "(none)"
    if need not in settings.template_sids:
        return None, ("NO_TEMPLATE", f"{'proactive' if msg.proactive else 'outside 24 h window'}; no approved template SID for {need}")
    values = dict(msg.template_vars)
    tmsg = OutboundMessage(to_e164=msg.to_e164, kind="template", template_ref=need, lang=msg.lang, outbox_id=msg.outbox_id,
                           template_vars=TPL.variables(need, values, sandbox=settings.twilio_sandbox), proactive=msg.proactive)
    return tmsg, None


def drain(con, channel, settings, clock, limit=20, pace=None):
    """Send everything due. Returns a list of (msg_id, outcome). Safe to call from many workers."""
    out = []
    for mid in claim(con, clock.real_now(), limit):
        real_now = clock.real_now()
        d = one(con, "SELECT * FROM wa_delivery WHERE msg_id=?", mid)
        msg = OutboundMessage.from_dict({k: v for k, v in json.loads(d["payload_json"]).items() if k != "ignore_opt_out"})
        final, blocked = _final_form(con, d, msg, real_now, settings)
        if blocked:
            code, why = blocked
            with write_txn(con):
                con.execute("""UPDATE wa_delivery SET status='BLOCKED', error_code=?, error_message=?, updated_real=? WHERE msg_id=?""",
                            (code, why, iso(real_now), mid))
                con.execute("UPDATE outbox SET status='BLOCKED' WHERE msg_id=?", (mid,))
                if code != "OPTED_OUT":
                    alarm(con, "SEND_BLOCKED", mid, {"code": code, "why": why, "to": mask(d["to_e164"]), "role": d["role"]}, real_now)
            out.append((mid, "BLOCKED:" + code))
            continue
        if pace:
            pace()
        final.send_ref = f"{mid}.{d['attempts']}.{uuid.uuid4().hex[:12]}"
        with write_txn(con):                         # the reference exists BEFORE the provider can accept anything
            con.execute("""UPDATE wa_delivery SET send_ref=?, send_started_real=?, sent_body=?, updated_real=? WHERE msg_id=?""",
                        (final.send_ref, iso(clock.real_now()), final.body or "", iso(clock.real_now()), mid))
        res = channel.send(final)
        real_now = clock.real_now()
        with write_txn(con):
            if res.ok:
                st = "SENT" if (res.status or "").lower() not in ("delivered", "read") else res.status.upper()
                con.execute("""UPDATE wa_delivery SET status=?, provider=?, provider_sid=?, sent_kind=?, sent_real=?, updated_real=?,
                               claimed_until_real=NULL, error_code=NULL, error_message=NULL WHERE msg_id=?""",
                            (st, channel.name, res.provider_id, res.sent_kind or final.kind, iso(real_now), iso(real_now), mid))
                con.execute("UPDATE outbox SET status='SENT' WHERE msg_id=?", (mid,))
                out.append((mid, "SENT"))
            elif res.ambiguous:                      # 5xx / network: it MAY have gone out; reconcile() decides, never a blind retry
                con.execute("""UPDATE wa_delivery SET claimed_until_real=?, error_code=?, error_message=?, updated_real=? WHERE msg_id=?""",
                            (iso(real_now), res.error_code, res.error_message, iso(real_now), mid))
                out.append((mid, "UNCERTAIN:" + str(res.error_code)))
            elif res.retryable and d["attempts"] < C.SEND_MAX_ATTEMPTS:
                nxt = real_now + timedelta(seconds=_backoff(d["attempts"]))
                con.execute("""UPDATE wa_delivery SET status='RETRY', next_attempt_real=?, claimed_until_real=NULL, error_code=?,
                               error_message=?, updated_real=? WHERE msg_id=?""", (iso(nxt), res.error_code, res.error_message, iso(real_now), mid))
                out.append((mid, "RETRY:" + str(res.error_code)))
            else:
                st = "BLOCKED" if res.status == "blocked" else "FAILED"
                con.execute("""UPDATE wa_delivery SET status=?, error_code=?, error_message=?, claimed_until_real=NULL, updated_real=?
                               WHERE msg_id=?""", (st, res.error_code, res.error_message, iso(real_now), mid))
                con.execute("UPDATE outbox SET status=? WHERE msg_id=?", (st, mid))
                alarm(con, "SEND_" + st, mid, {"error_code": res.error_code, "error": res.error_message, "attempts": d["attempts"],
                                               "to": mask(d["to_e164"]), "role": d["role"]}, real_now)
                _phone_fallback(con, d, real_now, "send " + st.lower())
                out.append((mid, f"{st}:{res.error_code}"))
        log.info("outbox %s -> %s", mid, out[-1][1])
    return out


def apply_status(con, st, real_now):
    """Twilio status callback -> wa_delivery. Statuses never move backwards (callbacks can arrive out of order)."""
    if not st.provider_id:
        return False
    with write_txn(con):
        d = one(con, "SELECT * FROM wa_delivery WHERE provider_sid=?", st.provider_id)
        if not d and st.ref:                         # a callback for an attempt whose SID we never stored: proof it went out
            d = one(con, "SELECT * FROM wa_delivery WHERE send_ref=? AND provider_sid IS NULL", st.ref)
            if d:
                con.execute("""UPDATE wa_delivery SET provider_sid=?, status='SENT', sent_real=COALESCE(sent_real, ?), claimed_until_real=NULL,
                               updated_real=? WHERE msg_id=?""", (st.provider_id, iso(real_now), iso(real_now), d["msg_id"]))
                con.execute("UPDATE outbox SET status='SENT' WHERE msg_id=?", (d["msg_id"],))
                d = one(con, "SELECT * FROM wa_delivery WHERE msg_id=?", d["msg_id"])
        if not d:
            return False
        new = (st.status or "").lower()
        cur = d["status"].lower()
        if new in TERMINAL_BAD:
            if cur in ("delivered", "read"):
                return True
            con.execute("""UPDATE wa_delivery SET status=?, error_code=?, error_message=?, updated_real=? WHERE msg_id=?""",
                        (new.upper(), st.error_code, st.error_message, iso(real_now), d["msg_id"]))
            con.execute("UPDATE outbox SET status=? WHERE msg_id=?", (new.upper(), d["msg_id"]))
            if not d["alarm_raised"]:
                alarm(con, "DELIVERY_" + new.upper(), d["msg_id"], {"error_code": st.error_code, "error": st.error_message,
                                                                    "to": mask(d["to_e164"]), "role": d["role"]}, real_now)
                con.execute("UPDATE wa_delivery SET alarm_raised=1 WHERE msg_id=?", (d["msg_id"],))
                _phone_fallback(con, d, real_now, f"WhatsApp {new} ({st.error_code})")
            return True
        if new not in RANK or cur in ("failed", "undelivered", "blocked"):
            return True
        if RANK[new] <= RANK.get(cur, 0):
            return True
        col = {"delivered": "delivered_real", "read": "read_real"}.get(new)
        con.execute(f"UPDATE wa_delivery SET status=?, updated_real=?{', ' + col + '=?' if col else ''} WHERE msg_id=?",
                    (new.upper(), iso(real_now), *( [iso(real_now)] if col else []), d["msg_id"]))
        con.execute("UPDATE outbox SET status=? WHERE msg_id=?", (new.upper(), d["msg_id"]))
        return True


def _phone_fallback(con, d, real_now, why):
    """F27/F28: a message that did not land is not a hand-off. Queue a phone call (stub) to the same person."""
    con.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?)",
                (K._next_id(con, "outbox", "msg_id", "OUT-"), "PHONE_CALL", d["to_e164"], "PHONE_FALLBACK",
                 f"Call needed: WhatsApp {d['msg_id']} to this {d['role'].lower()} did not land ({why}).", "STUB", iso(real_now)))


def _set(con, mid, real_now, **cols):
    sets = ", ".join(f"{k}=?" for k in cols)
    con.execute(f"UPDATE wa_delivery SET {sets}, updated_real=? WHERE msg_id=?", (*cols.values(), iso(real_now), mid))


def reconcile(con, channel, settings, clock, min_age_s=RECONCILE_AFTER_S):
    """Rows left in SENDING (crash, timeout, 5xx). Prove what happened; never resend on a guess."""
    out = []
    real_now = clock.real_now()
    stale = rows(con, """SELECT * FROM wa_delivery WHERE status='SENDING' AND provider_sid IS NULL AND claimed_until_real<=?
                         AND (send_started_real IS NULL OR send_started_real<=?)""",
                 iso(real_now), iso(real_now - timedelta(seconds=min_age_s)))
    lookup = getattr(channel, "lookup", None)
    for d in stale:
        mid = d["msg_id"]
        if not d["send_ref"]:                        # died before the reference was stored: the call never happened
            with write_txn(con):
                _set(con, mid, real_now, status="RETRY", next_attempt_real=iso(real_now), claimed_until_real=None)
            out.append((mid, "RETRY:NEVER_CALLED"))
            continue
        why, cands = None, None
        if lookup is None:
            why = f"adapter {channel.name} cannot look up sent messages"
        else:
            try:
                started = ts(d["send_started_real"])
                cands = lookup(d["to_e164"], started - timedelta(seconds=5), started + timedelta(seconds=CLAIM_S + 60))
            except Exception as e:                    # lookup failed: try again later, then a person decides
                n = d["reconcile_attempts"] + 1
                with write_txn(con):
                    if n >= RECONCILE_MAX_LOOKUPS:
                        why = f"lookup failed {n} times ({type(e).__name__})"
                    else:
                        _set(con, mid, real_now, reconcile_attempts=n,
                             claimed_until_real=iso(real_now + timedelta(seconds=_backoff(n))))
                        out.append((mid, "LOOKUP_FAILED"))
                        continue
        if cands is not None:
            taken = {r["provider_sid"] for r in rows(con, "SELECT provider_sid FROM wa_delivery WHERE provider_sid IS NOT NULL")}
            cands = [c for c in cands if c["sid"] not in taken]
            if d["sent_body"]:
                cands = [c for c in cands if (c.get("body") or "") == d["sent_body"]]
            if len(cands) == 1:
                with write_txn(con):
                    _set(con, mid, real_now, status="SENT", provider_sid=cands[0]["sid"], sent_real=iso(real_now), claimed_until_real=None,
                         provider=channel.name)
                    con.execute("UPDATE outbox SET status='SENT' WHERE msg_id=?", (mid,))
                out.append((mid, "RECONCILED_SENT"))
                continue
            if not cands:                             # the lookup worked and nothing of ours is there: not accepted
                with write_txn(con):
                    _set(con, mid, real_now, status="RETRY", next_attempt_real=iso(real_now + timedelta(seconds=_backoff(d["attempts"]))),
                         claimed_until_real=None, reconcile_attempts=0)
                out.append((mid, "RETRY:NOT_FOUND"))
                continue
            why = f"{len(cands)} possible matches"
        with write_txn(con):
            _set(con, mid, real_now, status="NEEDS_REVIEW", claimed_until_real=None)
            con.execute("UPDATE outbox SET status='NEEDS_REVIEW' WHERE msg_id=?", (mid,))
            alarm(con, "SEND_NEEDS_REVIEW", mid, {"why": why, "send_ref": d["send_ref"], "to": mask(d["to_e164"]), "role": d["role"]}, real_now)
        out.append((mid, "NEEDS_REVIEW"))
    return out


def resolve_review(con, msg_id, action, real_now, provider_sid=None, who="ops"):
    """A person settles a NEEDS_REVIEW row: 'sent' (with the SID they found in Twilio) or 'resend' (one more attempt)."""
    with write_txn(con):
        d = one(con, "SELECT * FROM wa_delivery WHERE msg_id=? AND status='NEEDS_REVIEW'", msg_id)
        if not d:
            return False
        if action == "sent":
            _set(con, msg_id, real_now, status="SENT", provider_sid=provider_sid, sent_real=iso(real_now))
            con.execute("UPDATE outbox SET status='SENT' WHERE msg_id=?", (msg_id,))
        elif action == "resend":
            _set(con, msg_id, real_now, status="RETRY", next_attempt_real=iso(real_now))
        else:
            raise ValueError("action must be sent or resend")
        K._audit(con, real_now, who, "OUTBOX_REVIEW_" + action.upper(), {"msg_id": msg_id, "provider_sid": provider_sid})
    return True


def check_delivery_sla(con, real_now):
    """Reply-time alarm: SENT but not delivered within DELIVERY_ALARM_MIN -> alarm + phone fallback, once per message."""
    cutoff = iso(real_now - timedelta(minutes=C.DELIVERY_ALARM_MIN))
    hit = []
    with write_txn(con):
        for d in rows(con, "SELECT * FROM wa_delivery WHERE status='SENT' AND alarm_raised=0 AND sent_real<=?", cutoff):
            alarm(con, "DELIVERY_SLA", d["msg_id"], {"minutes": C.DELIVERY_ALARM_MIN, "to": mask(d["to_e164"]), "role": d["role"]}, real_now)
            con.execute("UPDATE wa_delivery SET alarm_raised=1 WHERE msg_id=?", (d["msg_id"],))
            _phone_fallback(con, d, real_now, f"not delivered after {C.DELIVERY_ALARM_MIN} min")
            hit.append(d["msg_id"])
    return hit


def purge_bodies(con, real_now, days=C.MESSAGE_BODY_RETENTION_DAYS):
    """DPDP: after `days`, message bodies go; facts (ids, times, statuses, decisions) stay."""
    cutoff = iso(real_now - timedelta(days=days))
    with write_txn(con):
        n1 = con.execute("UPDATE wa_inbound SET body='[purged]' WHERE received_real<? AND body<>'[purged]'", (cutoff,)).rowcount
        old = [r["msg_id"] for r in rows(con, "SELECT msg_id FROM wa_delivery WHERE created_real<?", cutoff)]
        for mid in old:
            p = json.loads(one(con, "SELECT payload_json FROM wa_delivery WHERE msg_id=?", mid)["payload_json"])
            if p.get("body") != "[purged]":
                p.update(body="[purged]", template_vars={}, options=[{"id": o["id"], "title": "[purged]", "desc": ""} for o in p.get("options", [])])
                con.execute("UPDATE wa_delivery SET payload_json=? WHERE msg_id=?", (json.dumps(p), mid))
                con.execute("UPDATE outbox SET body='[purged]' WHERE msg_id=?", (mid,))
        n3 = con.execute("UPDATE wa_offers SET body='[purged]' WHERE shown_real<? AND body<>'[purged]'", (cutoff,)).rowcount
    return {"inbound": n1, "outbound": len(old), "offers": n3}
