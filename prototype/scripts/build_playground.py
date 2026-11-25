"""Bundle the engine for the portal's "Try your own message" box (driver.html): playground_bundle.json at the repo root.

    python3 -I scripts/build_playground.py [out.json]          (from prototype/; default: <repo>/playground_bundle.json)

The bundle holds web/playground.py, the setuhaul modules it imports (found by importing it with the standard library
only, as Pyodide will), and the 4 August database (base64). Re-run after any change to those files and commit the bundle.
"""
import base64, hashlib, json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(HERE)
probe = ("import sys, json; sys.path[:0] = ['.', 'web']; import playground; playground.reset(); "
         "playground.send('DRV013', 'I am late by one hour.', '09:19'); "
         "print(json.dumps(sorted(m for m in sys.modules if m.startswith('setuhaul'))))")
mods = json.loads(subprocess.run([sys.executable, "-I", "-S", "-c", probe], cwd=HERE, capture_output=True, text=True, check=True,
                                 env=dict(os.environ, SETUHAUL_PLAYGROUND_DB=os.path.join(tempfile.mkdtemp(), "probe.db"))).stdout.strip().splitlines()[-1])
files = {"playground.py": open(os.path.join(HERE, "web", "playground.py"), encoding="utf-8").read()}
for m in mods:
    rel = m.replace(".", "/") + ("/__init__.py" if m == "setuhaul" else ".py")
    files[rel] = open(os.path.join(HERE, rel), encoding="utf-8").read()
data = open(os.path.join(HERE, "data", "setuhaul_freight_operations.db"), "rb").read()
digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode() + data).hexdigest()[:12]
bundle = {"version": digest, "files": files, "db": base64.b64encode(data).decode("ascii")}
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "playground_bundle.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(bundle, f, ensure_ascii=False, separators=(",", ":"))
print(f"{out}: {len(files)} files, db {len(data)//1024} KB, bundle {os.path.getsize(out)//1024} KB, version {digest}")
