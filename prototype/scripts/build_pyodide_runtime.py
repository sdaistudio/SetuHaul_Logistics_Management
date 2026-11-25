"""Make the portal's own copy of the Pyodide runtime (the pyodide/ folder next to driver.html).

Why: the claude.ai viewer lets a page load scripts from the Pyodide CDN but blocks fetch() to it, so Python's
WebAssembly runtime never downloads there. The portal therefore serves Pyodide 0.27.7 itself. The viewer serves
.js, .wasm and .json but not .zip or .whl, so the standard library zip and the sqlite3 wheel go in as base64 JSON;
driver.html decodes them in memory when Pyodide asks for those two files. Unmodified Pyodide files otherwise.

    python3 -I scripts/build_pyodide_runtime.py <pyodide-0.27.7 dist dir> <sqlite3 wheel> <out dir>

The dist dir is the `pyodide` npm package (npm install pyodide@0.27.7) or the GitHub release
(https://github.com/pyodide/pyodide/releases/tag/0.27.7), which also holds the sqlite3 wheel.
Pyodide is MPL-2.0 (https://github.com/pyodide/pyodide); CPython is under the PSF licence.
"""
import base64, hashlib, json, os, shutil, sys

dist, wheel, out = sys.argv[1:4]
os.makedirs(out, exist_ok=True)
for f in ("pyodide.js", "pyodide.asm.js", "pyodide.asm.wasm", "pyodide-lock.json"):
    shutil.copy(os.path.join(dist, f), os.path.join(out, f))
lock = json.load(open(os.path.join(dist, "pyodide-lock.json"), encoding="utf-8"))
assert lock["info"]["version"] == "0.27.7", lock["info"]
pkg = lock["packages"]["sqlite3"]
raw = open(wheel, "rb").read()
assert os.path.basename(wheel) == pkg["file_name"] and hashlib.sha256(raw).hexdigest() == pkg["sha256"], "wrong sqlite3 wheel"
std = open(os.path.join(dist, "python_stdlib.zip"), "rb").read()
blobs = {"python_stdlib.zip": std, pkg["file_name"]: raw}
for name, data in blobs.items():
    with open(os.path.join(out, name + ".json"), "w", encoding="ascii") as f:
        json.dump({"file": name, "sha256": hashlib.sha256(data).hexdigest(), "b64": base64.b64encode(data).decode("ascii")}, f)
with open(os.path.join(out, "LICENSE.txt"), "w", encoding="utf-8") as f:
    f.write("Pyodide 0.27.7, unmodified runtime files (https://github.com/pyodide/pyodide), Mozilla Public License 2.0.\n"
            "Includes CPython 3.12 (Python Software Foundation License) and SQLite (public domain).\n"
            "python_stdlib.zip and the sqlite3 wheel are stored base64-encoded in .json files, byte for byte.\n")
print(sorted(os.listdir(out)))
