"""Database access: connections, the write transaction, and schema extensions.

Rules this module enforces:
  * The supplied snapshot is never modified; every run works on a copy.
  * Every write runs inside write_txn(), which opens BEGIN IMMEDIATE.
    In SQLite that takes the single write lock up front, so two bookings can
    never interleave their 'check' and 'insert' steps.
  * The schema extensions add three database-level guards that the supplied
    unique indexes do not give (see data audit F09):
      - trg_appt_guard: no active booking for a cancelled/completed shipment,
        a wrong facility, an overweight load, a wrong dock type or a non-OPEN slot
      - trg_active_must_be_current: an active booking must have is_current = 1
      - trg_no_overlap: no two live reservations may overlap in time on one dock
"""
import contextlib, os, shutil, sqlite3

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(HERE, "data", "setuhaul_freight_operations.db")

EXTENSIONS = r"""
-- 1. Holds: a short, owned reservation between 'driver picked' and 'commit' or 'coordinator decided'
CREATE TABLE IF NOT EXISTS slot_holds (
    hold_id TEXT PRIMARY KEY,
    slot_id TEXT NOT NULL REFERENCES appointment_slots(slot_id),
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    driver_id TEXT,
    dock_id TEXT NOT NULL,
    planned_start_ts TEXT NOT NULL,
    planned_end_ts TEXT NOT NULL,
    hold_status TEXT NOT NULL CHECK (hold_status IN ('ACTIVE','PENDING_APPROVAL','CONVERTED','RELEASED','EXPIRED','REJECTED')),
    reason TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_live_hold_per_slot ON slot_holds(slot_id)
    WHERE hold_status IN ('ACTIVE','PENDING_APPROVAL');
CREATE UNIQUE INDEX IF NOT EXISTS ux_live_hold_per_shipment ON slot_holds(shipment_id)
    WHERE hold_status IN ('ACTIVE','PENDING_APPROVAL');

-- 2. Time intervals on each dock. The trigger is the SQLite stand-in for
--    Postgres: EXCLUDE USING gist (dock_id WITH =, tstzrange(start_ts,end_ts) WITH &&)
CREATE TABLE IF NOT EXISTS dock_reservations (
    reservation_id TEXT PRIMARY KEY,
    dock_id TEXT NOT NULL REFERENCES docks(dock_id),
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('BOOKING','HOLD','KEPT_BOOKING','IN_DOCK','DOCK_EVENT')),
    ref_id TEXT,
    live INTEGER NOT NULL DEFAULT 1 CHECK (live IN (0,1)),
    created_at TEXT NOT NULL,
    CHECK (julianday(end_ts) > julianday(start_ts))
);
CREATE INDEX IF NOT EXISTS ix_resv_dock ON dock_reservations(dock_id, live);
CREATE TRIGGER IF NOT EXISTS trg_no_overlap
BEFORE INSERT ON dock_reservations
WHEN NEW.live = 1 AND NEW.kind IN ('BOOKING','HOLD')
BEGIN
    SELECT RAISE(ABORT, 'DOCK_INTERVAL_OVERLAP')
    WHERE EXISTS (SELECT 1 FROM dock_reservations r
                  WHERE r.live = 1 AND r.dock_id = NEW.dock_id
                    AND julianday(r.start_ts) < julianday(NEW.end_ts)
                    AND julianday(NEW.start_ts) < julianday(r.end_ts));
END;

-- 3. Database-level compatibility guard on every new active booking
CREATE TRIGGER IF NOT EXISTS trg_appt_guard
BEFORE INSERT ON appointments
WHEN NEW.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS')
BEGIN
    SELECT RAISE(ABORT, 'SHIPMENT_NOT_BOOKABLE')
    WHERE (SELECT current_status FROM shipments WHERE shipment_id = NEW.shipment_id) IN ('CANCELLED','COMPLETED');
    SELECT RAISE(ABORT, 'WRONG_FACILITY')
    WHERE (SELECT facility_id FROM appointment_slots WHERE slot_id = NEW.slot_id)
       <> (SELECT destination_facility_id FROM shipments WHERE shipment_id = NEW.shipment_id);
    SELECT RAISE(ABORT, 'SLOT_NOT_OPEN')
    WHERE (SELECT slot_status FROM appointment_slots WHERE slot_id = NEW.slot_id) <> 'OPEN';
    SELECT RAISE(ABORT, 'DOCK_WEIGHT_EXCEEDED')
    WHERE (SELECT load_weight_kg FROM shipments WHERE shipment_id = NEW.shipment_id)
        > (SELECT d.max_vehicle_weight_kg FROM appointment_slots s JOIN docks d ON d.dock_id = s.dock_id WHERE s.slot_id = NEW.slot_id);
    SELECT RAISE(ABORT, 'DOCK_TYPE_MISMATCH')
    WHERE (SELECT required_dock_type FROM shipments WHERE shipment_id = NEW.shipment_id) IN ('REEFER','HEAVY')
      AND (SELECT required_dock_type FROM shipments WHERE shipment_id = NEW.shipment_id)
       <> (SELECT d.dock_type FROM appointment_slots s JOIN docks d ON d.dock_id = s.dock_id WHERE s.slot_id = NEW.slot_id);
    SELECT RAISE(ABORT, 'DOCK_TYPE_MISMATCH')
    WHERE (SELECT required_dock_type FROM shipments WHERE shipment_id = NEW.shipment_id) = 'STANDARD'
      AND (SELECT d.dock_type FROM appointment_slots s JOIN docks d ON d.dock_id = s.dock_id WHERE s.slot_id = NEW.slot_id) <> 'STANDARD'
      AND NEW.booking_source <> 'MANUAL_OVERRIDE';
END;

CREATE TRIGGER IF NOT EXISTS trg_active_must_be_current
BEFORE INSERT ON appointments
WHEN NEW.appointment_status IN ('PENDING_CONFIRMATION','CONFIRMED','IN_PROGRESS') AND NEW.is_current = 0
BEGIN
    SELECT RAISE(ABORT, 'ACTIVE_APPOINTMENT_MUST_BE_CURRENT');
END;

-- 4. Append-only history of every booking state change (audit F08: the
--    supplied table overwrites status, so a past double booking is invisible)
CREATE TABLE IF NOT EXISTS appointment_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id TEXT NOT NULL,
    from_status TEXT, to_status TEXT NOT NULL,
    actor TEXT NOT NULL, reason TEXT, at TEXT NOT NULL
);

-- 5. Conversation memory: one row per driver, versioned (optimistic concurrency)
CREATE TABLE IF NOT EXISTS conversations (
    driver_id TEXT PRIMARY KEY,
    thread_id TEXT, state TEXT NOT NULL, lang TEXT NOT NULL DEFAULT 'en',
    shipment_id TEXT, facts_json TEXT NOT NULL DEFAULT '{}', offers_json TEXT NOT NULL DEFAULT '[]',
    clarifications INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0, updated_at TEXT
);

-- 6. Inbound dedupe and idempotent writes
CREATE TABLE IF NOT EXISTS inbound_messages (
    external_id TEXT PRIMARY KEY, driver_id TEXT NOT NULL, received_at TEXT NOT NULL,
    norm_text TEXT NOT NULL, duplicate_of TEXT
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY, result_json TEXT NOT NULL, created_at TEXT NOT NULL
);

-- 7. ETA windows and typed driver limits (audit F18, F31)
CREATE TABLE IF NOT EXISTS eta_windows (
    shipment_id TEXT NOT NULL, earliest_ts TEXT NOT NULL, latest_ts TEXT NOT NULL,
    source_text TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS driver_constraints (
    shipment_id TEXT NOT NULL,
    constraint_type TEXT NOT NULL CHECK (constraint_type IN ('EARLIEST_START','LATEST_START','LATEST_UNLOAD_END','LATEST_GATE_OUT')),
    limit_ts TEXT NOT NULL, source_text TEXT, created_at TEXT NOT NULL
);

-- 8. Planner earmarks: soft protection of capacity for a planned truck (not a promise)
CREATE TABLE IF NOT EXISTS slot_earmarks (
    slot_id TEXT PRIMARY KEY, shipment_id TEXT NOT NULL, priority_code TEXT NOT NULL,
    plan_run_id TEXT NOT NULL, expires_at TEXT NOT NULL
);

-- 9. Hand-offs, outbound messages and the audit trail
CREATE TABLE IF NOT EXISTS escalations (
    escalation_id TEXT PRIMARY KEY, shipment_id TEXT, driver_id TEXT,
    reason_code TEXT NOT NULL, urgency TEXT NOT NULL,
    routed_to TEXT, channel TEXT, status TEXT NOT NULL,
    handoff_json TEXT NOT NULL, created_at TEXT NOT NULL, sla_due_at TEXT
);
CREATE TABLE IF NOT EXISTS outbox (
    msg_id TEXT PRIMARY KEY, channel TEXT NOT NULL, recipient TEXT NOT NULL,
    kind TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'QUEUED',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, detail_json TEXT
);
"""


def fresh_copy(dest):
    """Copy the read-only snapshot to dest and apply the extensions."""
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(dest + suffix):
            os.remove(dest + suffix)
    shutil.copy(SNAPSHOT, dest)
    con = connect(dest)
    con.executescript(EXTENSIONS)
    con.close()
    return dest


def connect(path):
    con = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")      # readers never block the writer
    con.execute("PRAGMA busy_timeout = 30000")
    return con


# MVP 1 runs on SQLite (decision of 20 Nov 2026; MVP 2 moves to Postgres or MongoDB). In SQLite the
# no-double-booking promise lives in these triggers, so a database without them must never serve traffic.
GUARDS = ("trg_no_overlap", "trg_appt_guard", "trg_active_must_be_current")

_LIVE_OVERLAPS = """
SELECT COUNT(*) FROM dock_reservations a JOIN dock_reservations b
  ON a.dock_id = b.dock_id AND a.reservation_id < b.reservation_id
 WHERE a.live = 1 AND b.live = 1 AND a.kind IN ('BOOKING','HOLD') AND b.kind IN ('BOOKING','HOLD')
   AND julianday(a.start_ts) < julianday(b.end_ts) AND julianday(b.start_ts) < julianday(a.end_ts)"""


def ensure_extensions(con):
    """Re-apply the schema extensions (every statement is IF NOT EXISTS). Used at start-up on a persistent
    or restored database, so a copy that lost a trigger or table gets it back before serving."""
    con.executescript(EXTENSIONS)


def guard_problems(con):
    """What would break the booking guarantee: missing guard triggers, or live BOOKING/HOLD intervals that
    already overlap on a dock. An empty list means the database is safe to serve."""
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    problems = ["missing trigger " + g for g in GUARDS if g not in have]
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "dock_reservations" not in tables:
        return problems + ["missing table dock_reservations"]
    n = con.execute(_LIVE_OVERLAPS).fetchone()[0]
    if n:
        problems.append(f"{n} overlapping live booking/hold pair(s)")
    return problems


@contextlib.contextmanager
def write_txn(con):
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on any exception."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def one(con, sql, *args):
    r = con.execute(sql, args).fetchone()
    return dict(r) if r else None


def rows(con, sql, *args):
    return [dict(r) for r in con.execute(sql, args).fetchall()]
