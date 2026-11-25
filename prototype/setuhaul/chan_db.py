"""Channel tables: added next to the engine's tables, never instead of them.

The engine's outbox table (msg_id, channel, recipient, kind, body, status, created_at) stays
exactly as it is: capacity.py and escalation.py insert into it by position. Delivery details
live in wa_delivery, one row per outbox row that goes out on WhatsApp.

Times: *_real columns are wall-clock (the 24 h window, retries, delivery alarms are real-world
rules); as_of columns are the business clock (snapshot or live, see clock.Clock).
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS wa_contacts (
    e164 TEXT PRIMARY KEY, role TEXT NOT NULL,                 -- DRIVER | STAFF
    driver_id TEXT, contact_id TEXT, lang TEXT,
    last_inbound_real TEXT,                                    -- opens the 24 h window
    notice_sent_at TEXT, opted_out_at TEXT,
    unsupported_streak INTEGER NOT NULL DEFAULT 0,
    last_lat REAL, last_lng REAL, last_location_at TEXT
);
CREATE TABLE IF NOT EXISTS wa_unknown_senders (
    e164_hash TEXT PRIMARY KEY, last_refused_real TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS wa_inbound (
    message_sid TEXT PRIMARY KEY, provider TEXT NOT NULL, e164 TEXT, msg_type TEXT,
    body TEXT, choice_id TEXT, received_real TEXT NOT NULL, as_of TEXT, status TEXT NOT NULL,
    trace_id TEXT
);
CREATE TABLE IF NOT EXISTS wa_offers (
    offer_id TEXT PRIMARY KEY, driver_id TEXT NOT NULL, kind TEXT NOT NULL,   -- SLOTS | SHIPMENT | EARLY
    conv_state TEXT NOT NULL, options_json TEXT NOT NULL, body TEXT NOT NULL,
    shown_at TEXT NOT NULL, expires_at TEXT NOT NULL, shown_real TEXT NOT NULL,
    status TEXT NOT NULL,                                      -- LIVE | SUPERSEDED | CLOSED
    closed_at TEXT, used_by_sid TEXT, used_choice INTEGER
);
CREATE INDEX IF NOT EXISTS ix_wa_offers_driver ON wa_offers(driver_id, status);
CREATE TABLE IF NOT EXISTS wa_delivery (
    msg_id TEXT PRIMARY KEY REFERENCES outbox(msg_id),
    to_e164 TEXT NOT NULL, role TEXT NOT NULL, payload_json TEXT NOT NULL,
    proactive INTEGER NOT NULL DEFAULT 0, template_ref TEXT,
    status TEXT NOT NULL,                                      -- QUEUED | SENDING | SENT | DELIVERED | READ | FAILED | UNDELIVERED | BLOCKED
    provider TEXT, provider_sid TEXT UNIQUE, sent_kind TEXT,   -- what actually went: text | choices | list | template
    attempts INTEGER NOT NULL DEFAULT 0, next_attempt_real TEXT NOT NULL, claimed_until_real TEXT,
    error_code TEXT, error_message TEXT,
    created_real TEXT NOT NULL, sent_real TEXT, delivered_real TEXT, read_real TEXT, updated_real TEXT,
    alarm_raised INTEGER NOT NULL DEFAULT 0, trace_id TEXT,
    send_ref TEXT UNIQUE, send_started_real TEXT, sent_body TEXT,   -- written BEFORE the provider call
    reconcile_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_wa_delivery_due ON wa_delivery(status, next_attempt_real);
CREATE TABLE IF NOT EXISTS ops_alarms (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at_real TEXT NOT NULL, as_of TEXT,
    kind TEXT NOT NULL, ref TEXT, detail_json TEXT, cleared_at TEXT
);
CREATE TABLE IF NOT EXISTS safety_freezes (
    escalation_id TEXT NOT NULL, driver_id TEXT NOT NULL, shipment_id TEXT NOT NULL,
    frozen_at TEXT NOT NULL, cleared_at TEXT, cleared_by TEXT,
    PRIMARY KEY (escalation_id, shipment_id)
);
CREATE TABLE IF NOT EXISTS escalation_pages (
    escalation_id TEXT NOT NULL, level INTEGER NOT NULL, contact_id TEXT, name TEXT, role TEXT,
    channel TEXT NOT NULL, address TEXT, paged_at TEXT NOT NULL, due_at TEXT NOT NULL,
    acked_at TEXT, acked_by TEXT, msg_id TEXT,
    PRIMARY KEY (escalation_id, level)
);
CREATE TABLE IF NOT EXISTS channel_traces (
    trace_id TEXT PRIMARY KEY, at_real TEXT NOT NULL, kind TEXT NOT NULL, json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demo_world_applied (
    event_key TEXT PRIMARY KEY, applied_at TEXT NOT NULL
);
-- Spoken replies (setuhaul/voice/). WhatsApp fetches a media message from a public URL, so each reply audio is kept
-- here under an unguessable token for a day and served at /media/<token>.<ext>.
CREATE TABLE IF NOT EXISTS wa_media (
    token TEXT PRIMARY KEY, content_type TEXT NOT NULL, data BLOB NOT NULL, created_real TEXT NOT NULL, expires_real TEXT NOT NULL
);

-- SAFETY freeze, second layer. The rule lives in capacity._commit_core (an option that would release a
-- frozen booking returns OPTION_GONE / SAFETY_FREEZE before any write). This trigger is the backstop for
-- any path that bypasses it: nothing may release a frozen booking ("Released by planner ...") or mark it
-- NO_SHOW while a person has not cleared the case; such a write is rolled back.
CREATE TRIGGER IF NOT EXISTS trg_safety_freeze_no_release
BEFORE UPDATE OF appointment_status ON appointments
WHEN NEW.appointment_status IN ('CANCELLED', 'NO_SHOW')
 AND (NEW.appointment_status = 'NO_SHOW' OR COALESCE(NEW.cancellation_reason, '') LIKE 'Released by planner%')
 AND EXISTS (SELECT 1 FROM safety_freezes f WHERE f.shipment_id = OLD.shipment_id AND f.cleared_at IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'SAFETY_FREEZE: booking belongs to a driver with an open safety case');
END;
"""


MIGRATIONS = [("wa_delivery", "send_ref", "TEXT"), ("wa_delivery", "send_started_real", "TEXT"),
              ("wa_delivery", "sent_body", "TEXT"), ("wa_delivery", "reconcile_attempts", "INTEGER NOT NULL DEFAULT 0")]


def ensure(con):
    con.executescript(SCHEMA)
    for table, col, decl in MIGRATIONS:                  # databases made by the first version of this layer
        if col not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_wa_delivery_send_ref ON wa_delivery(send_ref)")


def alarm(con, kind, ref, detail, real_now, as_of=None):
    """An ops alarm: shown on the console, logged with numbers masked. Call inside a transaction."""
    import json
    import logging
    from .clock import iso
    from .tracing import mask
    con.execute("INSERT INTO ops_alarms(at_real, as_of, kind, ref, detail_json) VALUES (?,?,?,?,?)",
                (iso(real_now), iso(as_of) if as_of else None, kind, ref, json.dumps(detail, default=str, ensure_ascii=False)))
    logging.getLogger("setuhaul.alarm").warning("ALARM %s %s %s", kind, ref, mask(json.dumps(detail, default=str))[:300])
