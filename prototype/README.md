# SetuHaul Driver-Exception Agent: working prototype

FDE Academy, Mini Project 2. A WhatsApp-style driver assistant for dock rescheduling at Jaipur DC,
built on the supplied snapshot of 4 August 2026.

The language layer reads words. Code decides capacity. The database refuses anything unsafe.

## Requirements

* Python 3.11 or later (tested on 3.13.18) with the built-in `sqlite3` (tested on SQLite 3.53.1)
* `matplotlib` only for the Gantt charts in `run_scheduler.py` (tested on 3.11.2)
* No network and no other packages

## Run

```
python3 -I scripts/audit.py data/setuhaul_freight_operations.db out/   # deliverable 1: 36 data findings
python3 -I run_scenarios.py out/                                      # deliverable 4: the replayed morning (~1 s)
python3 -I run_integrity.py out/                                      # deliverable 5: 12 tests + 50 races (~15 s)
python3 -I run_scheduler.py out/                                      # scheduling tool: 6 policies (~65 s)
```

Every run works on a fresh copy of `data/setuhaul_freight_operations.db`; the supplied file is never changed.
`-I` (isolated mode) stops Python importing stray files from the working folder.
`RACE_REPEATS=500 python3 -I run_integrity.py out/` gives a longer soak.

## Layout

| Path | What it is |
|---|---|
| `setuhaul/config.py` | Every business number in one place (each one is in the Decision Register) |
| `setuhaul/clock.py` | Explicit as-of time; no hidden "now" |
| `setuhaul/db.py` | Connection, `BEGIN IMMEDIATE` write transaction, schema additions and triggers |
| `setuhaul/nlu.py` | Language detection and rules-first reading of driver messages (English, Hinglish, Hindi, Gujarati, Marathi, Punjabi, Marwari) |
| `setuhaul/eta.py` | The ETA we plan on: source order, confidence, late edge, plausibility checks |
| `setuhaul/timeline.py` | Busy intervals per dock and the 15-point feasibility check |
| `setuhaul/capacity.py` | Commit, hold, approve, sweep, cancel and swap, each in one transaction |
| `setuhaul/planner.py` | Facility scheduling tool: plan, earmarks, health monitor, what-ifs, swap proposals |
| `setuhaul/escalation.py` | Routing to the right person by a working channel |
| `setuhaul/replies.py` | Reply catalogue (English, Hinglish, Hindi; regional texts merged from replies_regional.py) and the output guard |
| `setuhaul/workflow.py` | The conversation state machine: one message in, one reply out |
| `setuhaul/world.py` | Rewind and replay of the snapshot for the scenario run |
| `app.py` | Production entry point (FastAPI): WhatsApp webhook, coordinator console API, sign-in |
| `setuhaul/graph.py` | The same turn as a LangGraph `StateGraph` (verify, dedupe, read, safety, dispatch) |
| `setuhaul/session_store.py` | Sessions, tokens, history, dedupe, per-driver lock, rate limit: Redis, or in memory |
| `setuhaul/llm.py` | Optional LLM fallback reader: Claude Haiku via LangChain, strict JSON, checked in code |
| `setuhaul/reader_contract.py` | Schema, system prompt and payload rules shared with the AgentCore agent |
| `agent/` | `setuhaul-reader`, the AgentCore Runtime agent that hosts only the model call (`READER=agentcore`) |
| `setuhaul/auth.py` | Driver identity from the signed WhatsApp webhook; coordinator sign-in (Cognito, or demo tokens) |
| `setuhaul/tracing.py` | LangSmith tracing with phone numbers masked and guardrail tags |
| `tests/test_production_layer.py` | 24 tests for the production layer (no network, no keys needed) |
| `tests/test_integrity.py` | The 12 integrity tests |
| `run_*.py` | Runners for the scenario, the integrity proof and the scheduler comparison |
| `scripts/audit.py` | The data audit: one proof query per finding |
| `out/` | The results quoted in the Word documents |

## Results in `out/`

| File | Contents |
|---|---|
| `audit_results.json`, `audit_queries.sql` | 36 findings with their rows; every proof query in one file |
| `scenario_run.json`, `scenario_console.txt` | Every turn of the replayed morning: message, tools called, reply, state |
| `scenario_metrics.json` | The brief's measures and four invariants |
| `integrity_results.json`, `integrity_console.txt` | 12 tests, 50 races, negative controls, stress, guard messages |
| `scheduler_results.json`, `gantt_fcfs.png`, `gantt_ours.png` | Six policies on the 09:45 snapshot, with sensitivity to the weights |
| `scenario_world.db`, `scheduler_snapshot.db` | The databases at the end of each run, for inspection |

## Production layer (optional)

The engine above runs with the standard library only. The production layer wraps it without changing
a decision: `tests/test_production_layer.py` checks that the LangGraph path gives exactly the same turns
as `workflow.handle()`, and `run_scenarios.py` output is unchanged.

```
pip install -r requirements-prod.txt
python3 -I -m unittest discover -s tests -p "test_production_layer.py" -v
SETUHAUL_DEV=1 SETUHAUL_REWIND_TO=2026-08-04T08:20:00+05:30 uvicorn app:app --reload
```

Then, for the demo (no Meta account needed):

```
curl -s localhost:8000/dev/driver-message -H 'content-type: application/json' \
  -d '{"phone":"+91-9000010013","text":"I am late by one hour.","as_of":"2026-08-04T09:19:00+05:30"}'
curl -s localhost:8000/auth/dev-login -H 'content-type: application/json' -d '{"user_id":"neha.bansal@setuhaul.example"}'
curl -s localhost:8000/console/holds -H "Authorization: Bearer <token>"
```

| Setting | Effect |
|---|---|
| `REDIS_URL` | Sessions in Redis; in memory if unset |
| `ANTHROPIC_API_KEY` | Turns on the LLM fallback (only for messages the rules mark UNKNOWN); rules only if unset |
| `READER` | Which LLM reader: `off` (rules only), `anthropic` (direct, needs `ANTHROPIC_API_KEY`), `agentcore` (Bedrock AgentCore Runtime, `agent/README.md`). Unset: as before |
| `AGENTCORE_READER_ARN`, `AWS_REGION` | The `setuhaul-reader` runtime for `READER=agentcore`; `/readyz` shows only whether the ARN is set |
| `SETUHAUL_SESSION_SALT` | Salts the AgentCore session id (hash of driver id + date); keep it in a secret store |
| `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY` | One trace per driver turn, phone numbers masked |
| `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_ID` | Live WhatsApp Cloud API |
| `COGNITO_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID` | Coordinator sign-in with Cognito instead of demo tokens |

Not yet exercised against live services: the Twilio WhatsApp Sandbox (played end to end through the local simulator
with Twilio-signed webhooks; the live round trip is in `../docs/twilio_live_test.md`), the WhatsApp Cloud API, Cognito,
a real Redis server, LangSmith and the Anthropic API. Each is behind an environment variable and covered by offline tests or fakes.

## WhatsApp through Twilio (CHANNEL=twilio | sim | console)

The channel layer wraps the same engine; no decision code changed. Offer ids on every option (`opt:{offer}:{n}`),
outbox delivery after COMMIT with retries and receipts, 24 h window vs templates, SAFETY freeze and a missed-SLA chain.

```
cd prototype && python3 -m venv .venv && .venv/bin/pip install -r requirements-prod.txt
.venv/bin/python -I -m unittest discover -s tests -v                          # 76 tests (36 old + 40 channel)

# Local simulator (no Twilio account needed): two terminals
CHANNEL=sim CLOCK_MODE=snapshot SNAPSHOT_ANCHOR=2026-08-04T09:15:00+05:30 SETUHAUL_DEV=1 TWILIO_AUTH_TOKEN=sim-token \
  TWILIO_SANDBOX=false .venv/bin/uvicorn app:app --port 8000
TWILIO_AUTH_TOKEN=sim-token .venv/bin/python -I scripts/sim_server.py          # open http://127.0.0.1:8100
.venv/bin/python -I scripts/sim_play.py                                        # or play C03, C06, C07, C12, C14 automatically

# Live Twilio Sandbox: see ../docs/twilio_live_test.md
```

| Path | What it is |
|---|---|
| `setuhaul/channels/` | `Channel` protocol; `twilio_wa.py` (signature, parsing, send), `sim.py` (simulator), `console.py` (offline) |
| `setuhaul/inbound.py` | One inbound message: dedupe, identity, rate limit, lock, STOP, privacy notice, offer binding, engine, render |
| `setuhaul/outbox.py` | Delivery worker: claim, window/template choice, `send_ref` stored before the call, send, retry (429) / reconcile (5xx, network, crash) instead of blind resend, NEEDS_REVIEW, status callbacks, delivery alarm |
| `setuhaul/offers.py` | Which list a tap or a typed number belongs to; stale taps never commit |
| `setuhaul/safety.py` | SAFETY freeze set/clear (audited) and the missed-SLA chain. The rule itself is in `capacity._commit_core` (SAFETY_FREEZE); a DB trigger in `chan_db.py` is the backstop |
| `setuhaul/chan_db.py` | Channel tables next to the engine's (the engine's `outbox` table is unchanged) |
| `setuhaul/clock.py` `Clock` | `CLOCK_MODE=live|snapshot`; snapshot runs the 4 Aug world forward from `SNAPSHOT_ANCHOR` |
| `templates/twilio_templates.yaml` | Templates to submit for approval; content SIDs come from `TWILIO_TPL_*_SID` |
| `scripts/` | `sim_server.py`, `sim_play.py`, `seed_demo_phones.py`, `create_twilio_content.py`, `preflight.py` (checks the live setup, sends nothing), `twilio_log_tail.py` (recent Twilio messages, error codes, Debugger alerts) |
| `tests/test_twilio_channel.py` | 40 channel tests, including console-vs-Twilio parity on C03 and C06 |


## Driver languages

Drivers can write in English, Hinglish, Hindi, Gujarati, Marathi, Punjabi and Marwari. Detection and reading
are rules only (no model call); replies come from the reviewed catalogue in the driver's language.

| File | Change |
|---|---|
| `setuhaul/languages.py` | Language list, scripts, native digits, Marathi/Marwari marker words, reading vocabulary per language |
| `setuhaul/nlu.py` | Detects the languages; reads times, durations, "after 12", choices, slot references and safety words in each |
| `setuhaul/replies_regional.py` | 30 reply texts per new language (drafts for native review); the rest fall back to Hindi |
| `setuhaul/replies.py` | Merges them; the output guard reads every language's dock word and "booked" words |
| `setuhaul/workflow.py` | Phrases built in code (ETA basis, approval reasons, "or") per language |
| `templates/twilio_templates.yaml`, `setuhaul/templates.py` | Proactive templates per language (each needs its own WhatsApp approval) |
| `tests/test_languages.py` | 16 tests: detection, reading, false-alarm checks, placeholders, guard, full turns |
| `scripts/language_demo.py` | Runs 8 demo conversations in the new languages and in English; writes `out/language_demo.json` and the portal's `lang_data.js` |

```
python3 -I scripts/language_demo.py out/
python3 -I -m unittest discover -s tests -p "test_languages.py" -v
```

Limits: romanised Gujarati, Marathi, Punjabi or Marwari is read as Hinglish and answered in Hinglish. Marwari
varies from district to district; the drafts follow Marwari as written around Jaipur and Jodhpur. All new reply texts need a native
speaker's review before go-live, and each language's WhatsApp templates need separate approval.

## What the prototype does not do

* The scenario run and integrity proof make no live WhatsApp or LLM call. Where the design calls the LLM fallback and no key is set, the workflow asks the driver a clarifying question instead.
* The coordinator console is an API (`app.py`) plus the static portal pages; there is no separate console screen yet.
* SQLite only. The Postgres DDL and the plan to re-run the proof on Postgres are in deliverables 3 and 5.
