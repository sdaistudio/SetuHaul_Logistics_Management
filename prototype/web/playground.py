"""Browser playground: the real SetuHaul engine answering messages typed on the portal (driver.html, "Try your own").

Runs inside Pyodide (Python compiled to WebAssembly) on the viewer's own machine. The same setuhaul package and the
same 4 August database as the prototype; nothing is sent anywhere. Rules only: no LLM call, so a message the rules
cannot read gets the usual clarifying question.

Differences from the server, both forced by the browser:
  * the database opens with an in-memory journal (the browser's file system has no shared memory for WAL);
  * the yard starts as it stood at 08:20 on 4 August (world.rewind); the morning's later events (D3 breakdown,
    gate-ins) are not replayed. Each driver's own conversation carries on from turn to turn until Reset.

API (called from JavaScript, every result is a JSON string): reset(), drivers(), send(driver_id, text, "HH:MM").
"""
import json
import os
import shutil
import sqlite3

from setuhaul import db, world, workflow as W
from setuhaul.clock import ts

DAY = "2026-08-04"
START = "08:20"
LIVE = os.environ.get("SETUHAUL_PLAYGROUND_DB") or os.path.join(os.path.dirname(db.SNAPSHOT), "playground.db")
_state = {"con": None, "last": START, "n": 0}


def _connect(path):
    con = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = MEMORY")
    return con


def _at(hhmm):
    return ts(f"{DAY}T{hhmm}:00+05:30")


def reset():
    """A fresh copy of the 4 August database, rewound to 08:20, with the booking guards in place."""
    if _state["con"] is not None:
        _state["con"].close()
    if os.path.exists(LIVE):
        os.remove(LIVE)
    shutil.copy(db.SNAPSHOT, LIVE)
    con = _connect(LIVE)
    con.executescript(db.EXTENSIONS)
    world.rewind(con, _at(START))
    problems = db.guard_problems(con)
    _state.update(con=con, last=START, n=0)
    return json.dumps({"start": START, "guards": "ok" if not problems else problems, "drivers": json.loads(drivers())})


def _booking(con, shipment_id):
    r = con.execute("""SELECT d.dock_code, s.slot_start_ts, a.appointment_status FROM appointments a
                        JOIN appointment_slots s ON s.slot_id = a.slot_id JOIN docks d ON d.dock_id = s.dock_id
                       WHERE a.shipment_id = ? AND a.is_current = 1""", (shipment_id,)).fetchone()
    return None if r is None else {"dock": r["dock_code"], "start": r["slot_start_ts"][11:16], "status": r["appointment_status"]}


def drivers():
    """Drivers with a Jaipur DC trip on 4 August, each with their trips and current booking."""
    con = _state["con"]
    out = {}
    for r in con.execute("""SELECT s.shipment_id, s.driver_id, d.driver_name, d.phone, s.original_eta_ts, s.required_dock_type,
                                   s.current_status, s.customer_name, s.product_category
                              FROM shipments s JOIN drivers d USING (driver_id)
                             WHERE s.destination_facility_id = 'FAC-JAI-01' ORDER BY s.driver_id, s.original_eta_ts"""):
        d = out.setdefault(r["driver_id"], {"driver_id": r["driver_id"], "name": r["driver_name"], "trips": []})
        d["trips"].append({"shipment_id": r["shipment_id"], "eta": r["original_eta_ts"][11:16], "dock_type": r["required_dock_type"],
                           "status": r["current_status"], "customer": r["customer_name"], "product": r["product_category"],
                           "booking": _booking(con, r["shipment_id"])})
    return json.dumps(list(out.values()))


def send(driver_id, text, hhmm):
    """One driver message through workflow.handle, exactly as a WhatsApp message would go. Time never runs backwards."""
    con = _state["con"]
    if hhmm < _state["last"]:
        hhmm = _state["last"]
    _state["last"] = hhmm
    _state["n"] += 1
    phone = con.execute("SELECT phone FROM drivers WHERE driver_id = ?", (driver_id,)).fetchone()["phone"]
    r = W.handle(con, phone, text, _at(hhmm), f"web-{_state['n']}")
    trips = [t for d in json.loads(drivers()) if d["driver_id"] == driver_id for t in d["trips"]]
    return json.dumps({"time": hhmm, "state": r.get("state"), "lang": r.get("lang"), "reply": r.get("reply"),
                       "buttons": r.get("buttons") or [], "tools": r.get("tools") or [], "shipment_id": r.get("shipment_id"),
                       "trips": trips}, ensure_ascii=False)
