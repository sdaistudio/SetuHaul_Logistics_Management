"""Pre-flight for the live Twilio Sandbox test. Sends no WhatsApp message and prints no secret.

    set -a; . ../.env; set +a                      # load the environment (this script never reads .env itself)
    .venv/bin/python -I scripts/preflight.py       # add --no-probe to skip the signed POST to your webhook
                                                   # add --twilio-api to also check the credentials (read-only API call)

Checks, each PASS / WARN / FAIL:
  env       required variables present (names only) and well-formed (shape only, never the value)
  url       TWILIO_WEBHOOK_BASE_URL is https and the app answers /healthz through it
  signature a locally signed request validates with the app's own validator; tampered / wrong-URL ones do not
  probe     a signed POST with no sender reaches /webhook/twilio and gets 200 (wrong signature gets 403);
            nothing is processed because there is no From
  stores    the database opens; Redis answers PING if REDIS_URL is set
  clock     CLOCK_MODE and SNAPSHOT_ANCHOR parse
  phones    every mapped demo number is E.164 and unique; which scenarios the mapped handsets can play
  reader    READER=agentcore: ARN and region present and well-formed, and a boto3 client can be built (no call is made)
Exit code 1 if anything FAILs.
"""
import argparse
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from setuhaul.session_store import normalise_phone      # noqa: E402
from setuhaul.tracing import mask                       # noqa: E402

E164 = re.compile(r"^\+[1-9]\d{7,14}$")
SNAPSHOT = os.path.join(HERE, "data", "setuhaul_freight_operations.db")
# What each acceptance case needs on a real handset. C06's approval is done in the console (no handset);
# CON003 only matters for the C12 staff page / WhatsApp ACK.
SCENARIOS = {"C03": ["DRV013"], "C06": ["DRV004"], "C07": ["DRV006"], "C12": ["DRV011"], "C12 staff page + ACK": ["CON003"],
             "C14": ["DRV015"]}
REQUIRED = {"twilio": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "TWILIO_WEBHOOK_BASE_URL",
                       "CLOCK_MODE", "SETUHAUL_DB"],
            "sim": ["TWILIO_AUTH_TOKEN", "CLOCK_MODE"], "console": ["CLOCK_MODE"]}
OPTIONAL = ["SNAPSHOT_ANCHOR", "TWILIO_INTERACTIVE", "TWILIO_SANDBOX", "REDIS_URL", "DUTY_HEAD_PHONE", "SETUHAUL_DEV",
            "TWILIO_CONTENT_QR1", "TWILIO_CONTENT_QR2", "TWILIO_CONTENT_QR3", "TWILIO_TPL_DRIVER_CHECKIN_SID",
            "TWILIO_TPL_DRIVER_BOOKING_UPDATE_SID", "TWILIO_TPL_STAFF_PAGE_SID", "ANTHROPIC_API_KEY",
            "READER", "AGENTCORE_READER_ARN", "AWS_REGION", "SETUHAUL_SESSION_SALT"]


class Report:
    def __init__(self):
        self.rows = []

    def add(self, area, level, text):
        self.rows.append((area, level, text))

    def ok(self, area, text):
        self.add(area, "PASS", text)

    def warn(self, area, text):
        self.add(area, "WARN", text)

    def fail(self, area, text):
        self.add(area, "FAIL", text)

    @property
    def failed(self):
        return any(r[1] == "FAIL" for r in self.rows)

    def print(self):
        for area, level, text in self.rows:
            print(f"{level:4s}  {area:9s}  {text}")
        print("\nRESULT:", "FAIL" if self.failed else "ready (check the WARN lines)")


def check_env(env, rep):
    ch = (env.get("CHANNEL") or "console").lower()
    if ch not in REQUIRED:
        rep.fail("env", f"CHANNEL must be twilio, sim or console (got an unknown value)")
        return ch
    rep.ok("env", f"CHANNEL={ch}")
    for k in REQUIRED[ch]:
        (rep.ok if env.get(k) else rep.fail)("env", f"{k} {'set' if env.get(k) else 'MISSING'}")
    for k in OPTIONAL:
        if env.get(k):
            rep.ok("env", f"{k} set")
    sid, frm = env.get("TWILIO_ACCOUNT_SID", ""), env.get("TWILIO_WHATSAPP_FROM", "")
    if sid and not (sid.startswith("AC") and len(sid) == 34):
        rep.fail("env", "TWILIO_ACCOUNT_SID does not look like an Account SID (AC + 32 hex)")
    if frm and not re.match(r"^whatsapp:\+[1-9]\d{7,14}$", frm):
        rep.fail("env", "TWILIO_WHATSAPP_FROM must look like whatsapp:+<E.164> (the sandbox number)")
    elif frm == "whatsapp:+14155238886":
        rep.ok("env", "TWILIO_WHATSAPP_FROM is the Twilio Sandbox number")
    if (env.get("TWILIO_INTERACTIVE") or "").lower() in ("1", "true", "yes", "on") and not env.get("TWILIO_CONTENT_QR3"):
        rep.warn("env", "TWILIO_INTERACTIVE is on but TWILIO_CONTENT_QR1..3 are not all set: options fall back to numbered text")
    for t in ("DRIVER_CHECKIN", "STAFF_PAGE", "DRIVER_BOOKING_UPDATE"):
        if not env.get(f"TWILIO_TPL_{t}_SID"):
            rep.warn("env", f"TWILIO_TPL_{t}_SID not set: those business-initiated messages will be BLOCKED with an alarm (expected in the sandbox)")
    return ch


def check_url(env, rep, timeout=6):
    base = (env.get("TWILIO_WEBHOOK_BASE_URL") or "").rstrip("/")
    if not base:
        return None
    u = urllib.parse.urlparse(base)
    if u.scheme != "https":
        rep.fail("url", "TWILIO_WEBHOOK_BASE_URL must be https (Twilio and ngrok)")
    if u.hostname in ("localhost", "127.0.0.1") or (u.hostname or "").endswith(".local"):
        rep.fail("url", "TWILIO_WEBHOOK_BASE_URL points at this machine; Twilio cannot reach it (use the ngrok URL)")
    if u.path not in ("", "/"):
        rep.warn("url", "TWILIO_WEBHOOK_BASE_URL has a path; the app appends /webhook/twilio to it")
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=timeout) as r:
            body = r.read(200)
        (rep.ok if b'"ok"' in body else rep.warn)("url", f"{u.hostname} answers /healthz through the public URL")
        return base
    except (urllib.error.URLError, OSError) as e:
        rep.fail("url", f"{u.hostname}/healthz unreachable ({type(e).__name__}): is ngrok running and pointing at the app's port?")
        return None


def check_signature(env, rep):
    token, base = env.get("TWILIO_AUTH_TOKEN"), (env.get("TWILIO_WEBHOOK_BASE_URL") or "https://example.invalid").rstrip("/")
    if not token:
        return
    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        rep.fail("signature", "twilio package not installed (pip install -r requirements-prod.txt)")
        return
    from setuhaul.channels import ChannelRequest
    from setuhaul.channels.twilio_wa import TwilioChannel
    from setuhaul.settings import Settings
    ch = TwilioChannel(Settings(channel="twilio", twilio_auth_token=token))
    url = base + "/webhook/twilio"
    p = {"MessageSid": "SMpreflight00000000000000000000000", "Body": "preflight", "NumMedia": "0"}
    sig = RequestValidator(token).compute_signature(url, p)
    req = lambda u, s, params=p: ChannelRequest(u, {"x-twilio-signature": s}, {k: [v] for k, v in params.items()})
    good = ch.verify(req(url, sig))
    bad_url = ch.verify(req(url.replace("https://", "http://"), sig))
    tampered = ch.verify(req(url, sig, dict(p, Body="changed")))
    if good and not bad_url and not tampered:
        rep.ok("signature", "local round trip: signed request accepted; wrong URL and tampered body refused")
    else:
        rep.fail("signature", f"validator mismatch (good={good}, wrong-url={bad_url}, tampered={tampered})")


def probe(env, rep, base, timeout=8):
    """Signed POST with no From: proves Twilio's URL + token match what the app validates. Nothing is processed."""
    token = env.get("TWILIO_AUTH_TOKEN")
    if not (token and base):
        return
    from twilio.request_validator import RequestValidator
    url = base + "/webhook/twilio"
    p = {"MessageSid": "SMpreflight00000000000000000000000", "AccountSid": env.get("TWILIO_ACCOUNT_SID", ""), "NumMedia": "0"}

    def post(sig):
        req = urllib.request.Request(url, data=urllib.parse.urlencode(p).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": sig})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except OSError:
            return 0
    good, bad = post(RequestValidator(token).compute_signature(url, p)), post("invalid")
    if good == 200 and bad == 403:
        rep.ok("probe", "signed POST to /webhook/twilio -> 200, bad signature -> 403 (URL and token match the app)")
    elif good == 403:
        rep.fail("probe", "signed POST refused (403): the app validates against a different URL or token. "
                          "Set TWILIO_WEBHOOK_BASE_URL to exactly the ngrok URL and restart the app with the same TWILIO_AUTH_TOKEN")
    else:
        rep.fail("probe", f"unexpected answers: signed={good}, unsigned={bad}")


def check_stores(env, rep):
    path = env.get("SETUHAUL_DB")
    if path:
        full = path if os.path.isabs(path) else os.path.join(HERE, path)
        if os.path.abspath(full) == os.path.abspath(SNAPSHOT):
            rep.fail("stores", "SETUHAUL_DB is the supplied snapshot; use the copy made by scripts/seed_demo_phones.py")
        elif not os.path.exists(full):
            rep.fail("stores", f"SETUHAUL_DB not found ({os.path.relpath(full, HERE)}); run scripts/seed_demo_phones.py")
        else:
            try:
                con = sqlite3.connect(f"file:{full}?mode=ro", uri=True)
                con.execute("SELECT COUNT(*) FROM drivers").fetchone()
                has = con.execute("SELECT 1 FROM sqlite_master WHERE name='wa_delivery'").fetchone()
                from setuhaul.db import guard_problems
                problems = guard_problems(con)
                con.close()
                rep.ok("stores", "database opens read-only" + ("" if has else " (channel tables are created when the app starts)"))
                if problems:
                    rep.warn("stores", "booking guards: " + "; ".join(problems) + " (missing triggers are re-created when the app starts; overlaps need a person)")
                else:
                    rep.ok("stores", "booking guards present (3 triggers), no overlapping live bookings")
            except sqlite3.Error as e:
                rep.fail("stores", f"database error: {e}")
    url = env.get("REDIS_URL")
    if url:
        try:
            import redis
            redis.Redis.from_url(url, socket_timeout=3).ping()
            rep.ok("stores", "Redis PING ok")
        except Exception as e:
            rep.fail("stores", f"Redis unreachable ({type(e).__name__})")
    else:
        rep.warn("stores", "REDIS_URL not set: sessions and dedupe in memory (SQLite still dedupes MessageSid)")


def check_clock(env, rep):
    from setuhaul.clock import Clock, iso
    try:
        c = Clock.from_env(env)
        rep.ok("clock", f"CLOCK_MODE={c.mode}" + (f", starts at {iso(c.now())}" if c.mode == "snapshot" else ""))
        if c.mode == "snapshot" and not iso(c.now()).startswith("2026-08-04"):
            rep.warn("clock", "SNAPSHOT_ANCHOR is not on 2026-08-04: the demo world events will not apply")
    except ValueError as e:
        rep.fail("clock", str(e))


def check_phones(env, rep):
    path = env.get("SETUHAUL_DB")
    if not path:
        return {}
    full = path if os.path.isabs(path) else os.path.join(HERE, path)
    if not os.path.exists(full):
        return {}
    orig = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)
    con = sqlite3.connect(f"file:{full}?mode=ro", uri=True)
    base = {**dict(orig.execute("SELECT driver_id, phone FROM drivers")), **dict(orig.execute("SELECT contact_id, phone FROM facility_contacts"))}
    now = {**dict(con.execute("SELECT driver_id, phone FROM drivers")), **dict(con.execute("SELECT contact_id, phone FROM facility_contacts"))}
    orig.close()
    con.close()
    mapped = {k: v for k, v in now.items() if v and v != base.get(k)}
    seen = {}
    for k, v in sorted(mapped.items()):
        if not E164.match(v):
            rep.fail("phones", f"{k} is mapped to a number that is not E.164 ({mask(v)}); re-run seed_demo_phones.py")
        n = normalise_phone(v)
        if n in seen:
            rep.fail("phones", f"{k} and {seen[n]} share one handset ({mask(n)}): one number per role")
        seen[n] = k
    others = [k for k, v in now.items() if v and k not in mapped and normalise_phone(v) in seen]
    for k in others:
        rep.fail("phones", f"{k} still has a snapshot number equal to a mapped handset")
    rep.ok("phones", f"{len(mapped)} ids mapped to handsets: " + (", ".join(f"{k} {mask(v)}" for k, v in sorted(mapped.items())) or "none"))
    playable = {s: all(i in mapped for i in ids) for s, ids in SCENARIOS.items()}
    for s, ok in playable.items():
        (rep.ok if ok else rep.warn)("phones", f"{s}: {'playable' if ok else 'needs a handset for ' + ', '.join(SCENARIOS[s])}")
    return playable


def check_reader(env, rep):
    reader = (env.get("READER") or "").strip().lower()
    if reader in ("", "anthropic"):
        rep.ok("reader", "LLM reader: " + ("Anthropic (direct)" if env.get("ANTHROPIC_API_KEY") else "none, rules only"))
        return
    if reader == "off":
        rep.ok("reader", "READER=off: rules only")
        return
    if reader != "agentcore":
        rep.fail("reader", "READER must be off, anthropic or agentcore")
        return
    arn, region = env.get("AGENTCORE_READER_ARN", ""), env.get("AWS_REGION", "")
    if not arn or not region:
        rep.fail("reader", "READER=agentcore needs AGENTCORE_READER_ARN and AWS_REGION")
        return
    if not re.match(r"^arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/", arn):
        rep.fail("reader", "AGENTCORE_READER_ARN does not look like an AgentCore runtime ARN")
    elif f":{region}:" not in arn:
        rep.warn("reader", "AGENTCORE_READER_ARN is in a different region from AWS_REGION")
    if not env.get("SETUHAUL_SESSION_SALT"):
        rep.warn("reader", "SETUHAUL_SESSION_SALT not set: AgentCore session ids are unsalted hashes of the driver id")
    try:
        import boto3
        boto3.client("bedrock-agentcore", region_name=region)
        rep.ok("reader", "AgentCore reader configured; boto3 client builds (no call made)")
    except ImportError:
        rep.fail("reader", "READER=agentcore but boto3 is not installed (pip install -r requirements-prod.txt)")
    except Exception as e:
        rep.fail("reader", f"boto3 client for bedrock-agentcore failed ({type(e).__name__})")


def check_twilio_api(env, rep):
    try:
        from twilio.rest import Client
        acct = Client(env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"]).api.v2010.accounts(env["TWILIO_ACCOUNT_SID"]).fetch()
        (rep.ok if acct.status == "active" else rep.fail)("twilio", f"credentials accepted; account status {acct.status}")
    except Exception as e:
        rep.fail("twilio", f"credential check failed ({type(e).__name__}); check TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN")
    rep.warn("twilio", "the sandbox join (3 days) cannot be checked by API: each handset must have sent 'join <code>' recently")


def run(env, do_probe=True, twilio_api=False):
    rep = Report()
    ch = check_env(env, rep)
    base = check_url(env, rep) if ch == "twilio" else None
    check_signature(env, rep)
    if do_probe and base:
        probe(env, rep, base)
    check_stores(env, rep)
    check_clock(env, rep)
    check_phones(env, rep)
    check_reader(env, rep)
    if twilio_api and env.get("TWILIO_ACCOUNT_SID") and env.get("TWILIO_AUTH_TOKEN"):
        check_twilio_api(env, rep)
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--twilio-api", action="store_true", help="read-only account fetch to check the credentials")
    a = ap.parse_args()
    rep = run(dict(os.environ), do_probe=not a.no_probe, twilio_api=a.twilio_api)
    rep.print()
    sys.exit(1 if rep.failed else 0)


if __name__ == "__main__":
    main()
