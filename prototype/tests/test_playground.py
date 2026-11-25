"""The portal's "Try your own message" box (driver.html) runs web/playground.py on the real engine in the browser.

Run:  python3 -I -m unittest discover -s tests -p "test_playground.py" -v      (from prototype/)

  * the playground runs on the Python standard library alone (as in Pyodide): reset, a delay, a booking, a
    Gujarati message, a safety message, and a time that tries to run backwards;
  * the committed playground_bundle.json is up to date with the engine files and the database
    (re-run scripts/build_playground.py when this fails).
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(HERE)

PROBE = r"""
import sys, json; sys.path[:0] = ['.', 'web']
import playground as P
r = json.loads(P.reset())
out = {"guards": r["guards"], "drivers": len(r["drivers"])}
a = json.loads(P.send('DRV013', 'I am late by one hour.', '09:19'))
b = json.loads(P.send('DRV013', '1', '09:21'))
c = json.loads(P.send('DRV004', 'હું એક કલાક મોડો છું.', '09:30'))
d = json.loads(P.send('DRV011', 'bhai accident ho gaya, helper ko chot lagi hai', '09:00'))
out.update(delay=(a["state"], len(a["buttons"])), book=(b["state"], b["trips"][0]["booking"]),
           gu=(c["state"], c["lang"]), safety=(d["state"], d["time"]),
           foreign=sorted(m for m in sys.modules if m.split('.')[0] not in sys.stdlib_module_names
                          and m.split('.')[0] not in ('setuhaul', 'playground', '__main__')))
print(json.dumps(out))
"""


class Playground(unittest.TestCase):
    def test_runs_on_the_standard_library_alone(self):
        env = dict(os.environ, SETUHAUL_PLAYGROUND_DB=os.path.join(tempfile.mkdtemp(), "p.db"))
        p = subprocess.run([sys.executable, "-I", "-S", "-c", PROBE], cwd=HERE, capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        r = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(r["guards"], "ok")
        self.assertGreaterEqual(r["drivers"], 10)
        self.assertEqual(r["delay"], ["OPTIONS_SHOWN", 3])
        self.assertEqual(r["book"][0], "BOOKED")
        self.assertEqual(r["book"][1]["status"], "CONFIRMED")
        self.assertEqual(r["gu"], ["OPTIONS_SHOWN", "gu"])
        self.assertEqual(r["safety"], ["ESCALATED", "09:30"])       # 09:00 after 09:30: time does not run backwards
        self.assertEqual(r["foreign"], [])                          # nothing outside the standard library

    def test_committed_bundle_is_current(self):
        out = os.path.join(tempfile.mkdtemp(), "b.json")
        p = subprocess.run([sys.executable, "-I", os.path.join(HERE, "scripts", "build_playground.py"), out],
                           cwd=HERE, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        with open(out, encoding="utf-8") as f:
            fresh = json.load(f)
        with open(os.path.join(ROOT, "playground_bundle.json"), encoding="utf-8") as f:
            committed = json.load(f)
        self.assertEqual(committed["version"], fresh["version"], "run: python3 -I scripts/build_playground.py")
        self.assertEqual(set(committed["files"]), set(fresh["files"]))


if __name__ == "__main__":
    unittest.main()
