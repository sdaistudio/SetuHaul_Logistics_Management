"""MVP 1 runs on SQLite (decision of 20 Nov 2026); these tests pin the safeguards that decision relies on.

Run:  python3 -I -m unittest discover -s tests -p "test_sqlite_guards.py" -v      (from prototype/)

  * a fresh world has all three booking-guard triggers and no overlapping live bookings
  * a database that lost a trigger is reported, and ensure_extensions() puts it back (idempotent)
  * an overlap that slipped in while a trigger was missing is reported (it is never auto-fixed)
  * /readyz fails when a guard is missing, and the app's start-up restores it
  * deploy/server/restore-check.sh fails a backup without the guards
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from setuhaul import db   # noqa: E402

has = lambda m: importlib.util.find_spec(m) is not None
RESTORE_CHECK = os.path.join(os.path.dirname(HERE), "deploy", "server", "restore-check.sh")


class Guards(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sh-guards-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = db.fresh_copy(os.path.join(self.dir, "w.db"))
        self.con = db.connect(self.path)
        self.addCleanup(self.con.close)

    def test_fresh_world_is_safe(self):
        self.assertEqual(db.guard_problems(self.con), [])

    def test_missing_trigger_reported_and_restored(self):
        self.con.execute("DROP TRIGGER trg_no_overlap")
        self.assertEqual(db.guard_problems(self.con), ["missing trigger trg_no_overlap"])
        db.ensure_extensions(self.con)
        db.ensure_extensions(self.con)                    # idempotent: a second run changes nothing
        self.assertEqual(db.guard_problems(self.con), [])

    def test_overlap_that_slipped_in_is_reported(self):
        self.con.execute("DROP TRIGGER trg_no_overlap")
        dock = self.con.execute("SELECT dock_id FROM docks LIMIT 1").fetchone()[0]
        for rid, s, e in (("T-A", "2026-08-04T10:00:00+05:30", "2026-08-04T11:00:00+05:30"),
                          ("T-B", "2026-08-04T10:30:00+05:30", "2026-08-04T11:30:00+05:30")):
            self.con.execute("INSERT INTO dock_reservations (reservation_id, dock_id, start_ts, end_ts, kind, live, created_at)"
                             " VALUES (?, ?, ?, ?, 'BOOKING', 1, ?)", (rid, dock, s, e, s))
        db.ensure_extensions(self.con)                    # the trigger comes back ...
        self.assertEqual(db.guard_problems(self.con), ["1 overlapping live booking/hold pair(s)"])   # ... the overlap stays visible

    def test_touching_intervals_are_not_overlaps(self):
        dock = self.con.execute("SELECT dock_id FROM docks LIMIT 1").fetchone()[0]
        for rid, s, e in (("T-C", "2026-08-05T10:00:00+05:30", "2026-08-05T11:00:00+05:30"),
                          ("T-D", "2026-08-05T11:00:00+05:30", "2026-08-05T11:30:00+05:30")):
            self.con.execute("INSERT INTO dock_reservations (reservation_id, dock_id, start_ts, end_ts, kind, live, created_at)"
                             " VALUES (?, ?, ?, ?, 'BOOKING', 1, ?)", (rid, dock, s, e, s))
        self.assertEqual(db.guard_problems(self.con), [])

    @unittest.skipUnless(os.path.exists(RESTORE_CHECK) and shutil.which("bash"), "restore-check.sh not available")
    def test_restore_check_fails_without_guards(self):
        good = subprocess.run(["bash", RESTORE_CHECK, self.path], capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertIn("booking guards: all 3 present", good.stdout)
        self.con.execute("DROP TRIGGER trg_appt_guard")
        self.con.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # a backup is a whole file; fold the WAL in like one
        bad = subprocess.run(["bash", RESTORE_CHECK, self.path], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("MISSING trg_appt_guard", bad.stdout)


@unittest.skipUnless(has("fastapi") and has("httpx"), "fastapi / httpx not installed")
class Readyz(unittest.TestCase):
    def test_readyz_reports_guards_and_startup_restores_them(self):
        d = tempfile.mkdtemp(prefix="sh-readyz-")
        self.addCleanup(shutil.rmtree, d, True)
        path = db.fresh_copy(os.path.join(d, "live.db"))
        c = db.connect(path)
        c.execute("DROP TRIGGER trg_active_must_be_current")
        c.close()
        old = {k: os.environ.get(k) for k in ("SETUHAUL_DB", "CHANNEL", "OUTBOX_WORKER")}
        os.environ.update(SETUHAUL_DB=path, CHANNEL="console", OUTBOX_WORKER="0")

        def restore():
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
            sys.modules.pop("app", None)
        self.addCleanup(restore)
        sys.modules.pop("app", None)
        import app                                         # start-up runs _init_db -> ensure_extensions
        from fastapi.testclient import TestClient
        body = TestClient(app.app).get("/readyz").json()
        self.assertEqual(body["checks"]["db"], {"ok": True, "detail": "ok, booking guards present"})
        c = db.connect(path)
        c.execute("DROP TRIGGER trg_no_overlap")           # lost while running
        c.close()
        r = TestClient(app.app).get("/readyz")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["checks"]["db"], {"ok": False, "detail": "missing trigger trg_no_overlap"})


if __name__ == "__main__":
    unittest.main()
