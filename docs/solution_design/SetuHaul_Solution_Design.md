# SetuHaul — Logistic Management: Solution Design

Nov 18, 2026

## Purpose and scope

SetuHaul answers late-running truck drivers on WhatsApp, rebooks their
dock slot in one checked transaction, and hands only the cases that need
judgement to a coordinator. This document is the full design behind the
working prototype, the portal and the MVP 2 GPS control tower.

**In scope:** the driver conversation, slot feasibility and booking, the
facility scheduling tool, escalation to people, the data model, the
integrity proof, and MVP 2 (GPS-based ETAs and proactive messages).

**Out of scope:** carrier billing, route planning, yard management
inside the gate, and outbound (dispatch-side) scheduling.

**Evidence base:** the 4 August 2026 snapshot (18 tables, Jaipur DC and
Gurugram Cross-Dock), the FDE Academy brief, and the deliverables 00–06
in the repository. Every number below comes from a run of the prototype
or a query on the snapshot.

| Audience             | Read first                                                 |
|----------------------|------------------------------------------------------------|
| Head of operations   | Problem, Users and journeys, Limits and risks              |
| Warehouse planner    | Capacity and booking, Scheduling tool, Escalation          |
| Engineering reviewer | Architecture, Conversation workflow, Data model, Integrity |

## Problem and operating context

One late truck breaks a chain of promises: the load, the arrival time, a
dock, the unloading crew and the customer waiting for stock. Only the
dock is scarce, and in a disruption many drivers chase the same few
compatible doors at once.

| Measure                               | Value                 | Design consequence                                                      |
|---------------------------------------|-----------------------|-------------------------------------------------------------------------|
| Truckloads per day                    | 280–360               | Planner and earmarks run per facility, one write path per facility lock |
| Destination facilities                | 6 (2 in the snapshot) | Every service takes facility_id and as_of                               |
| Dock doors                            | 32                    | About 357 truck-starts a day of capacity against 190–240 bookings       |
| Driver exception messages, normal day | 15–25                 | Rules read them; the LLM only on UNKNOWN                                |
| Messages in a 30-minute spike         | 20–35                 | Per-driver queue; facility re-plan on every ETA; 50-race proof          |
| Coordinators per shift                | 5                     | Only 3 of 13 cases reached a person, each with a full record            |
| Live GPS feeds                        | 0                     | Late-edge ETAs plus plausibility checks; GPS is MVP 2                   |

**What the snapshot shows (4 August 2026, Jaipur DC):** 13 of 20 Jaipur
trucks ran late, the worst SHP1016 by 90 minutes. Dock D3's leveller
broke at 09:15. A reefer booking for 17:00 on D5 had been impossible
since maintenance was planned on 1 August. The data itself has 36
findings, 5 critical and 15 high, including bookings above the door's
weight limit and no single "now" (rows run from 07:10 to 16:30).

**Boundaries from the brief:** the agent never decides penalties,
compensation, disputes or hazardous loads; it never invents a slot; it
hands safety straight to a person.

## Design principles and key decisions

The model reads, code decides, the database refuses, and a person
decides what code must not. SetuHaul is a workflow, not an agent loop:
the steps are known in advance, and a wrong step means a double booking,
so code chooses every next step and the model never picks a tool.

1.  **Language at the edges, deterministic code in the middle.** Rules
    read English, Hinglish, Hindi, Gujarati, Marathi, Punjabi and
    Marwari; a small LLM is a fallback that
    must return strict JSON.

2.  **Nothing is promised until one transaction books it.** Showing
    options reserves nothing; the slot is re-checked under the write
    lock.

3.  **The database is the last line of defence.** Triggers and unique
    indexes refuse an overlap, a wrong door or an overweight load even
    if the code has a bug.

4.  **Every number in a reply is backed by evidence.** An output guard
    blocks any dock or time the database did not produce.

5.  **Plan on the late edge.** ETAs carry a confidence; low confidence
    asks before booking.

6.  **Explicit time.** Every function takes an as_of time, so runs are
    reproducible.

| \#  | Decision                                 | Our answer                                                                                                   | Evidence                          |
|-----|------------------------------------------|--------------------------------------------------------------------------------------------------------------|-----------------------------------|
| D1  | What to collect before options           | Which shipment (only if evidence can't decide), a plannable arrival time, the latest leave time if mentioned | C06, C09; 2 questions in 13 cases |
| D2  | Linking chat to driver and shipment      | Phone gives the driver; shipment by an evidence ladder, else ask with a suggestion                           | C06                               |
| D3  | Revised ETA and confidence               | Gate-in, then newest ETA record, then plan; HIGH/MEDIUM/LOW; late edge; plausibility checks                  | MSG001, ETA012                    |
| D4  | What makes a slot feasible               | A 15-point check in code, re-run at commit; 6 points also database triggers                                  | SHP1014–1016                      |
| D5  | "Available" while another driver decides | Feasible now and not booked, held or protected; showing reserves nothing                                     | C03 vs C07 race                   |
| D6  | Hold, request, confirmation              | SHOWN → PICKED → CONFIRMED in one transaction, or HELD_FOR_APPROVAL with a deadline                          | C01, C06                          |
| D7  | Simultaneous requests                    | First valid commit wins; scarce capacity earmarked for priority and deadline trucks                          | C10                               |
| D8  | When the schedule recalculates           | Every ETA update, check-in, dock event, cancellation, and every 15 minutes                                   | 09:16 re-plan                     |
| D9  | Stale options, duplicates, retries       | Re-check at commit; dedupe; idempotency keys                                                                 | C03, C07                          |
| D10 | No feasible slot                         | Never invent one; escalate with a full record and what-ifs                                                   | C14, ESC-0004                     |
| D11 | Refusing a preferred slot                | Say what happened, why, and what is still possible, in the driver's language                                 | C03, C10                          |
| D12 | Human approval or takeover               | Eight rule-based triggers, routed by role with an SLA                                                        | ESC-0001 to 0004                  |
| D13 | Proving no double booking                | Five defence layers; 10-driver race × 50; negative controls; stress test                                     | T01–T12                           |
| D14 | What a slot means                        | A start window; gate-in by start + grace; unloads may run past slot end                                      | F04, F05                          |
| D15 | Architecture and cost                    | MVP 1: one server with SQLite; MVP 2: Postgres or MongoDB (chosen at MVP 2 kickoff)                           | Review pack                       |
| D16 | Where the LLM is used                    | Reading words only, after rules, strict JSON; replies from a catalogue                                       | C09                               |
| D17 | Languages                                | Deterministic detection; 7 languages (EN, Hinglish, Hindi, Gujarati, Marathi, Punjabi, Marwari)             | C06, C09, C12                     |
| D18 | Earmarks                                 | Soft 20-minute protection for HIGH/CRITICAL and deadline trucks                                              | 09:45                             |
| D19 | Swaps                                    | Coordinator only; trialled in a rolled-back transaction                                                      | C10, T12                          |
| D20 | Proactive monitoring                     | Re-check every booking on every event                                                                        | 09:16, 09:56                      |
| D21 | What "now" is                            | Explicit as_of; scenario rewinds the snapshot and replays                                                    | F15                               |

## Users and journeys

The driver gets an answer in about a minute without a phone call; the
coordinator sees only the cases that need judgement, each with options
already computed.

| Moment                     | Driver today                          | Driver with SetuHaul                                                   |
|----------------------------|---------------------------------------|------------------------------------------------------------------------|
| Running late               | Calls the warehouse, often no answer  | Sends one WhatsApp line in any of seven languages                      |
| Getting a new slot         | Waits for a call back; arrives hoping | Gets 2–3 feasible slots in about a minute and books with a tap         |
| Slot taken by someone else | Finds out at the gate                 | Told "just taken", nothing booked, fresh options in the same reply     |
| Accident or breakdown      | Same queue as everything else         | Scheduling stops; 112 advice; shift manager paged within 5 minutes     |
| MVP 2                      | Remembers the plan                    | Day-before brief, hour-before traffic nudge, delay alerts with options |

| Moment             | Coordinator today                   | Coordinator with SetuHaul                                                 |
|--------------------|-------------------------------------|---------------------------------------------------------------------------|
| Message spike      | 20–35 calls and chats in 30 minutes | Only hand-offs arrive (3 of 13 cases on 4 August)                         |
| Dock breakdown     | Rebuilds the plan by hand           | Health monitor flags affected bookings; planner proposes a new order      |
| Approval           | Ad hoc, on the phone                | Hold with a deadline: min(15 min, start − 30 min), re-checked on approval |
| Impossible booking | Discovered at 17:00                 | Flagged 7 hours early (09:16 for the 16:24 reefer)                        |
| Audit              | Who said what is lost               | Every decision, guard result and message is logged                        |

**What changes:** efficiency (median 1 minute to an answer, 2 messages
per case), communication (the driver's own language, plain reasons),
transparency (every booking has history and a reason code) and control
(people approve swaps, late starts and data conflicts).

## Architecture

One container, one database, one write path: messages enter through
channels, pass a deterministic workflow, call read-only services, and
reach the record only through the booking service.

<img src="./media/image1.png" style="width:6in;height:4.95536in"
alt="SetuHaul architecture · 5 layers" />

SetuHaul architecture · 5 layers

| Layer         | Components                                                                            | Code                                                        |
|---------------|---------------------------------------------------------------------------------------|-------------------------------------------------------------|
| 1 Channels    | Driver WhatsApp, coordinator console, GPS feed (MVP 2)                                | Portal pages; webhook                                       |
| 2 API backend | Sender verification, dedupe, idempotency key, language detection                      | workflow.py (entry)                                         |
| 3 Workflow    | Extract → validate → resolve trip → clarify → offer, book or escalate → reply + guard | workflow.py handle(), nlu.py, replies.py                    |
| 4 Services    | ETA resolver, feasibility engine, scheduling tool, booking service, escalation router | eta.py, timeline.py, planner.py, capacity.py, escalation.py |
| 5 Record      | Supplied snapshot, added tables, triggers                                             | db.py                                                       |

The prototype is 2,659 lines of Python using only the standard library
and SQLite.

## Conversation workflow

Each inbound message becomes one structured turn: workflow.handle(con,
phone, text, as_of) runs a fixed path, and asks the driver at most what
the backend cannot work out (2 clarifying questions across 13 cases).

<img src="./media/image2.png" style="width:6in;height:2.50893in"
alt="Conversation states · workflow.py" />

Conversation states · workflow.py

**One turn, step by step**

1.  Verify the sender by phone and drop duplicates (same WhatsApp id, or
    the same text within 10 minutes).

2.  Detect the language in code: English, Hinglish (Latin script),
    Hindi, Marathi or Marwari (Devanagari, told apart by word lists),
    Gujarati or Punjabi (by script).

3.  Extract with rules: clock times (11:30, 7 baje, 7 बजे), durations
    (one hour, dedh ghanta), ranges, order references, hints (evening,
    heavy, reefer), choices (1, 2, 3, pehla), declines, safety words,
    urgency claims. Each value keeps the exact text it came from.

4.  Only if rules return UNKNOWN, call the model with no tools and no
    database access; it must return the strict JSON shape (intent, eta,
    limits, shipment_hint, choice, safety, evidence), which code
    validates.

5.  Resolve the shipment by an evidence ladder (truck at our gate, trip
    being driven, load hints); if evidence can't decide, ask with a
    suggestion.

6.  Resolve the ETA: gate-in beats everything, then the newest ETA
    record, then the plan; confidence HIGH, MEDIUM or LOW, planned on
    the late edge.

7.  Re-plan the facility, check the current booking (still feasible, at
    risk, certain miss), and list up to three feasible options.

8.  Book, hold for approval, or escalate.

9.  Write the reply from reviewed templates in the driver's language,
    pass it through the output guard, and send.

**Intents read:** REPORT_DELAY, EARLY_ARRIVAL, ASK_OPTIONS,
CHECK_STATUS, CHOOSE, ANSWER, DECLINE, SAFETY, UNKNOWN. An urgency claim
never changes dock order; priority comes from the shipment record.

## Capacity and booking

A slot is offered only if it passes 15 checks, and booked only if it
passes them again inside one write transaction. The feasibility engine
(timeline.py) builds each door's busy intervals — live bookings plus a
10-minute changeover, holds, trucks still in a dock (with a 10-minute
buffer once they overrun) and dock events — and finds the earliest start
in each OPEN slot that fits the unload.

### The 15-point feasibility check

| \#  | Check                                                                                                                         | Also enforced by     |
|-----|-------------------------------------------------------------------------------------------------------------------------------|----------------------|
| 1   | Shipment is not CANCELLED or COMPLETED                                                                                        | Trigger              |
| 2   | Slot is at the shipment's destination facility                                                                                | Trigger              |
| 3   | Dock type: reefer load to a reefer door; over 25 t or HEAVY to the heavy door; standard loads stay off them unless overridden | Trigger              |
| 4   | Load within the door's weight limit                                                                                           | Trigger              |
| 5   | Slot status is OPEN                                                                                                           | Trigger              |
| 6   | No breakdown, maintenance or manual block overlaps the unload                                                                 | Code                 |
| 7   | No live booking on the slot (a dead booking may be released)                                                                  | Unique index         |
| 8   | No live hold by another truck                                                                                                 | Partial unique index |
| 9   | Not earmarked for a truck of equal or higher priority                                                                         | Code at commit       |
| 10  | Start inside the window: max(slot start, release, driver's earliest) to before slot end                                       | Code                 |
| 11  | Door free for unload + 10-minute changeover, including overruns                                                               | Overlap trigger      |
| 12  | Unload ends by facility close                                                                                                 | Code                 |
| 13  | A start at or after 21:00 needs a coordinator                                                                                 | Approval             |
| 14  | Driver limits: latest start, latest unload end, latest gate-out                                                               | Code                 |
| 15  | An ETA flagged as a data conflict needs a coordinator                                                                         | Approval             |

Each rejected slot keeps a reason code, so the no-slot hand-off lists
exactly why each door failed.

### The booking transaction

capacity.commit() is the only code that writes a booking.

1.  BEGIN IMMEDIATE takes the write lock before reading anything, so two
    check-then-insert sequences never interleave.

2.  The idempotency key (the WhatsApp message id) returns the first
    result if the request was seen before.

3.  Driver limits from earlier messages are reloaded.

4.  The full feasibility check re-runs under the lock; a lost race
    returns OPTION_GONE with a reason, never an error.

5.  A rule that needs a person returns NEEDS_APPROVAL, and the workflow
    creates a hold instead.

6.  Dead bookings (trucks that certainly cannot arrive) are released and
    their dispatcher told.

7.  The shipment's old booking is retired, busy intervals refreshed,
    then the appointment and its time interval inserted; the triggers
    run here.

8.  A database refusal becomes a typed GuardTripped error, never shown
    raw to a driver.

9.  History, the coordinator notice and the audit row are written;
    consumed earmarks are removed; leaving the block commits.

### Holds, approvals and earmarks

- **Hold:** when a pick needs approval, the slot is held with a deadline
  of min(15 minutes, start − 30 minutes). Approval re-runs the check and
  can still fail safely.

- **Earmark:** a soft 20-minute protection of scarce capacity for HIGH
  or CRITICAL loads and deadline-limited trucks, enforced at commit.

- **Swap:** coordinator only, trialled inside a rolled-back transaction,
  never breaking a driver limit; a failed swap changes nothing (test
  T12).

## Scheduling tool

The facility planner (planner.py) proposes a dock order for every truck
still to arrive; it never books. The coordinator approves, and the
capacity layer commits each move.

**Model.** Trucks are jobs, doors are machines. Fixed inputs: trucks
already in a dock, kept bookings and dock events (for example the D3
breakdown, 09:15–13:00).

**Objective, compared in strict order:**

1.  Trucks left with no door.

2.  Priority-weighted wait: minutes × 8 (CRITICAL), 4 (HIGH), 2
    (NORMAL), 1 (LOW).

3.  Number of changes to what drivers were already told.

**Method.** List scheduling in a priority order, then pairwise repair:
swap two jobs in the order and keep the swap if the objective improves,
until nothing improves. With 8 or fewer jobs it also tries every order
as a benchmark (BEST_ORDER).

**Triggers.** Every ETA update, check-in, dock event and cancellation,
and every 15 minutes. planner.health() re-checks each live booking as
OK, AT_RISK, CERTAIN_MISS or INVALID.

| Policy (8 trucks, Jaipur, 09:45)       | Trucks with no door | Weighted wait | CRITICAL wait | Max HIGH wait |
|----------------------------------------|---------------------|---------------|---------------|---------------|
| First come, first served               | 2                   | 1,040         | —             | —             |
| Ours: priority + repair (= best order) | 1                   | 1,120         | 40 min        | 65 min        |
| Strict priority                        | —                   | —             | 0 min         | 165 min       |

Ours accepts a little more total wait to strand one fewer truck and cap
the wait of high-priority loads. **Upgrade path:** at more facilities or
new constraints (yard space, labour crews), Google OR-Tools CP-SAT with
interval variables and no-overlap per door, behind the same interface.

## Escalation and hand-offs

Eight rule-based triggers send a case to a person; a model never decides
to escalate. escalation.escalate() writes one record with the driver's
own words, the trip, every door tried and why it failed, computed
what-ifs and a response deadline, then routes it by role and channel
health (vacant roles are skipped).

| Trigger                                               | Example                                | Goes to                                       | Respond within          |
|-------------------------------------------------------|----------------------------------------|-----------------------------------------------|-------------------------|
| Safety words (accident, injury, fire, police, chot…)  | C12 at 10:05                           | Shift manager, phone + WhatsApp               | 5 min                   |
| No feasible slot                                      | C14 at 16:24 (ESC-0004)                | Warehouse coordinator; night role after 18:00 | 15 min                  |
| Start at or after 21:00                               | RULE005                                | Coordinator approval                          | min(15 min, start − 30) |
| ETA data conflict (vehicle on another trip)           | C06, SHP1020                           | Coordinator approval                          | Same                    |
| Swap of a confirmed booking                           | C10                                    | Coordinator                                   | 15 min                  |
| Data conflict, non-blocking                           | DRV007 on two moving trucks (ESC-0002) | Coordinator or dispatch                       | 60 min                  |
| Two unanswered questions, or driver asks for a person | Rule                                   | Coordinator                                   | 30 min                  |
| Output guard blocked a reply                          | Never sent to a driver                 | Ops alarm                                     | Immediate               |

Penalties, compensation, disputes and hazardous or regulated loads
always go to a person. While a case waits, the driver is told what
happens next and is not sent to the dock.

## Data model

The 18 supplied tables stay as they are; the prototype adds 12 tables
and 3 triggers for safe booking and history, and the production design
proposes 22 tables in five groups for language, GPS, messaging and
commercial clocks.

**Supplied (18):** facilities, docks, appointment_slots, appointments,
shipments, carriers, drivers, vehicles, vehicle_types, eta_updates,
facility_checkins, dock_status_events, driver_exceptions, chat_threads,
chat_messages, operational_messages, facility_contacts, facility_rules.

### Added in the prototype

| Table or object                    | Purpose                                                                                                 |
|------------------------------------|---------------------------------------------------------------------------------------------------------|
| dock_reservations                  | Every booking, hold, truck-in-dock and dock event as a time interval on a door                          |
| slot_holds (+ 2 unique indexes)    | A slot waiting for a coordinator; one live hold per slot and per shipment                               |
| slot_earmarks                      | Capacity protected for HIGH/CRITICAL or deadline-limited trucks, 20 minutes                             |
| appointment_events                 | Append-only booking history                                                                             |
| conversations                      | State per driver thread                                                                                 |
| inbound_messages, idempotency_keys | Dedupe and retry safety                                                                                 |
| eta_windows, driver_constraints    | What the driver said, typed (arrival window, leave-by time)                                             |
| escalations                        | Hand-off records with SLA                                                                               |
| outbox, audit_log                  | Every outbound message and every decision                                                               |
| trg_no_overlap                     | Refuses overlapping BOOKING or HOLD intervals on one door                                               |
| trg_appt_guard                     | Refuses a booking for a cancelled shipment, wrong site, overweight load, wrong door type or closed slot |
| trg_active_must_be_current         | Keeps one current active booking per shipment                                                           |

### Proposed for production (22 tables)

| Group         | Tables                                                                                                                             | Why                                                                                                                      |
|---------------|------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------|
| Language      | driver_preferences                                                                                                                 | Language, script, consent and channel per driver                                                                         |
| Communication | message_templates, comm_rules, comm_schedule                                                                                       | Reviewed texts per language; when to send what (R1–R8); every planned and sent message with delivery status              |
| GPS           | telematics_devices, gps_pings, trip_tracking, route_plans, geofences, traffic_conditions                                           | Device per truck; raw positions; one live row per trip; corridors; gate and yard zones; traffic for start-on-time nudges |
| Capacity      | slot_holds, dock_reservations, slot_earmarks, conversations, inbound_messages + idempotency_keys, eta_windows + driver_constraints | Safe booking, as in the prototype                                                                                        |
| Governance    | appointment_events, escalations, audit_log + guardrail_events + outbox                                                             | History, hand-offs, guard results                                                                                        |
| Commercial    | detention_terms, eway_bills, coordinator_shifts                                                                                    | Free time and charges; the e-way bill's legal clock; who is on duty with a working number                                |

MVP 1 keeps SQLite and its overlap trigger. In MVP 2, on Postgres the
trigger becomes a constraint: EXCLUDE USING gist (dock_id WITH =, during
WITH &&) WHERE (live AND kind IN ('BOOKING','HOLD')). MongoDB has no
equivalent, so there the rule moves into code: a per-dock lock document
updated inside the same transaction as the booking.

## Integrity and concurrency

Zero double bookings: in 50 races of ten drivers choosing the same slot
at the same instant, each race had exactly one winner and 450 clean
refusals. With the guards switched off (the negative control), all ten
drivers book the same slot, which proves the test can fail.

**Five defence layers**

1.  Single write path: only capacity.commit() writes bookings.

2.  BEGIN IMMEDIATE: the write lock is taken before the re-check.

3.  Unique indexes: one live booking per slot, one live hold per slot
    and per shipment.

4.  Triggers: trg_no_overlap on time intervals, trg_appt_guard on door,
    weight, site and slot status.

5.  Idempotency keys: the same WhatsApp message delivered twice books
    once.

| Test                              | What it proves                        | Result                                                     |
|-----------------------------------|---------------------------------------|------------------------------------------------------------|
| 10 drivers, 1 slot, × 50          | Exactly one winner per race           | 50 of 50                                                   |
| Negative control, no guards       | The test can detect a double booking  | All 10 book                                                |
| Random stress, 8 threads × 25     | No errors under load                  | 45 confirmed, 143 option gone, 12 needs approval, 0 errors |
| test_11: feasibility bug injected | The database still refuses an overlap | DOCK_INTERVAL_OVERLAP raised, 0 rows written               |
| test_12: invalid swap             | A failed swap changes nothing         | Before = after                                             |

All 12 integrity tests and the race proof run in about 15 seconds:
python3 -I run_integrity.py out/.

## Guardrails, safety and privacy

The model can change what we ask, never what we book. Every guard below
is code, not a prompt.

- **Output guard (replies.guard).** Every dock and every time in a reply
  must appear in the evidence from the database; "Booked" or "Confirmed"
  (in any of the seven languages) needs a CONFIRMED commit. A blocked
  reply is never sent and raises an ops alarm.

- **LLM output checks.** Strict JSON schema with no extra fields; every
  time must lie between as_of − 1 hour and as_of + 24 hours; every
  evidence string must appear in the driver's message. Anything else is
  treated as UNKNOWN and the driver is asked.

- **Prompt injection.** Driver text is data. It reaches a tool only
  through typed, range-checked fields.

- **Urgency claims.** Dock order follows the shipment record's priority,
  not the message (C10: "this load is urgent" is politely declined).

- **Safety.** A safety word from the rules or a SAFETY intent from the
  model stops scheduling, gives 112 advice and pages the shift manager.
  The driver's shipments are then frozen until a coordinator clears the
  case: the rule lives in `capacity._commit_core` (an option that would
  release a frozen booking returns OPTION_GONE / SAFETY_FREEZE), with a
  database trigger as the backstop (`chan_db.py`).

- **Identity.** The sender is verified by phone number; a chat is bound
  to its own driver.

- **Privacy.** The model sees only the message text and a minimal fact
  sheet; phone numbers and licence numbers never go to it. Messages and
  decisions are retained in the audit log under the client's retention
  policy, still to be agreed.

- **Consent.** Proactive WhatsApp messages (MVP 2) use approved utility
  templates and the driver's recorded opt-in in driver_preferences.

## MVP 2: GPS control tower

MVP 2 adds a GPS feed to the same engine: ETAs come from position and
speed, each trip is flagged red, amber or green against its own
facility's slot, and the driver hears about a problem before the
coordinator has to call. The portal page simulates it from the
snapshot's departure times and latest ETAs, for all facilities or one at
a time.

**Summary on top:** trips today, on the road, on track, at risk, action
needed, arrived, average delay on road, messages sent — plus a live map
and the incoming GPS stream.

**Flag rules**

| Flag  | When                                                                                                                                                        |
|-------|-------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Red   | Gate time (GPS ETA + 15 min) is more than 30 minutes past the slot start; or the truck has not moved after its planned departure; or it has no live booking |
| Amber | Inside the check-in grace; delay over 60 minutes while the slot still holds; stationary 15 minutes or more; or delay over 30 minutes                        |
| Green | Everything else                                                                                                                                             |

**Driver by driver:** each row shows the driver, trip, progress, GPS ETA
against plan, dock slot and flag, with WhatsApp and call buttons and 2–3
reschedule options from free slots at that facility, bookable in one
click.

**The message series (rules R1–R8)**

| Rule                     | Sent when                                                                          |
|--------------------------|------------------------------------------------------------------------------------|
| R1 Day-before trip brief | 18:00 the evening before: pickup, departure time, dock and slot                    |
| R2 Start on time         | 60 minutes before departure, with live traffic on the corridor and a leave-by time |
| R3 Not moving check      | 20 minutes after planned departure with no GPS movement                            |
| R4 Delay over 60 minutes | GPS ETA slips 60+ minutes: the delay and 2–3 fitting slots                         |
| R5 Slot at risk          | First time a trip turns amber: "Are you on track?" with quick replies              |
| R6 Stationary alert      | No movement for 20 minutes mid-route, with 112 advice                              |
| R7 Arrival prep          | 30 minutes out: e-way bill, invoice and LR ready; dock and gate time               |
| R8 Booking change        | Any booking, move or release                                                       |

Templates exist in English and Hinglish, with Gujarati, Marathi,
Punjabi and Marwari bodies for check-in and booking updates; drivers who wrote in Hindi or
Hinglish on 4 August (DRV004, DRV010, DRV011) get Hinglish. **Data
sources in production:** a GPS box, FASTag through ULIP, SIM location or
a driver app, stored in telematics_devices, gps_pings and trip_tracking.

## Testing, scenario run and results

The engine resolved 10 of 13 driver cases without a person, with a
median of 1 minute to an answer and 2 messages per case, and no double
bookings. The scenario rewinds the snapshot to 08:20 and replays each
later row at its own time, so every run gives the same result.

| Case | Driver message (short)                    | Outcome                                                          |
|------|-------------------------------------------|------------------------------------------------------------------|
| C01  | Already at the gate, can I dock now?      | Moved earlier to D4 08:35                                        |
| C03  | Late by one hour                          | Loses race for D4 at 09:36, gets fresh options, books D1 11:30   |
| C04  | In the yard, new dock?                    | Booking still valid inside grace; queue position given           |
| C05  | Tyre repaired, 11:10, must leave by 13:30 | Options respect the leave-by limit; booked D2 11:25              |
| C06  | Late 45 minutes (two trips)               | Asks which trip; answered in Hindi; hold approved by coordinator |
| C07  | Traffic, any slot after 12?               | Wins the race for D4 12:40; duplicate message suppressed         |
| C08  | Heavy trailer, slot missed                | Heavy door D6 12:50; data conflict flagged                       |
| C09  | "thoda late hoon bhai"                    | Asks ETA in Hinglish; current booking still fits                 |
| C10  | Urgent load, first dock please            | Urgency claim declined politely; booked D2 12:20                 |
| C11  | Slots after 1 PM if I reach 12:45?        | Later options compared                                           |
| C12  | Accident, helper injured                  | Escalated to the shift manager; scheduling stopped               |
| C13  | Is my 4 PM slot still active?             | Status answered                                                  |
| C14  | Reefer, ETA 18:30, unload tonight?        | No feasible door; ESC-0004 with what-ifs                         |

| Command (from prototype/)                                            | What it runs              | Time    |
|----------------------------------------------------------------------|---------------------------|---------|
| python3 -I run_scenarios.py out/                                     | The 13 cases              | ~1 s    |
| python3 -I run_integrity.py out/                                     | 12 tests + 50-race proof  | ~15 s   |
| python3 -I run_scheduler.py out/                                     | Planner policy comparison | ~65 s   |
| python3 -I scripts/audit.py data/setuhaul_freight_operations.db out/ | The 36 data findings      | seconds |

The proactive health monitor also flagged the impossible reefer booking
at 09:16, 7 hours before the driver's 16:24 message.

## Deployment, operations and cost

The pilot (MVP 1) runs on one small server with SQLite, Redis and
nightly backups for about ₹3,000 a month, with WhatsApp messaging adding
a few hundred rupees a month at pilot volume. MVP 2 moves the system of
record to managed Postgres or MongoDB (about ₹5,000 a month more),
because GPS pings, the control tower and more facilities need many
writers.

| Component          | Pilot                                                                                         | Scale path                                                         |
|--------------------|-----------------------------------------------------------------------------------------------|--------------------------------------------------------------------|
| Webhook + workflow | One container (Python 3, standard library + SQLite in the prototype)                          | AWS serverless functions behind a queue, one consumer per facility |
| System of record   | SQLite on the server: one writer, overlap trigger, nightly backups                            | MVP 2: Postgres (EXCLUDE constraint) or MongoDB, with read replicas |
| Language fallback  | Claude Haiku, no tools, strict JSON                                                           | Same; rules cover most messages                                    |
| Channel            | WhatsApp Business API; 1,000 free service messages a month per number, then about ₹0.145 each | Utility templates for proactive messages                           |
| Portal             | Static site (GitHub Pages)                                                                    | Coordinator console behind sign-in                                 |

**Operations.** The coordinator console shows the hand-off queue, the
day board per facility and approvals with deadlines. Ops alarms fire on
a blocked reply, a GuardTripped error or a channel failure. Each run
writes metrics files; the audit log answers "who changed this booking
and why".

**Run it locally.** Site: python3 -m http.server 8000 in the repository
root. Engine: the commands in the testing section. Repository:
[sdaistudio/SetuHaul_Logistics_Management](https://github.com/sdaistudio/SetuHaul_Logistics_Management).

## Limits, risks and roadmap

The prototype is proven on one facility-day of data; the main risks are
data quality and live position, not the booking logic.

| Risk or limit                                     | Effect                                                                                               | Mitigation                                                                      |
|---------------------------------------------------|------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------|
| No GPS in the snapshot                            | ETAs depend on the driver and the carrier                                                            | Late-edge planning and plausibility checks now; GPS or FASTag via ULIP in MVP 2 |
| Data quality (36 findings, 5 critical)            | Bookings above door weight limits, slots open on a broken door, a reefer booking dead since 1 August | Checks enforced in code and triggers; findings go to the data owner             |
| One driver or vehicle on two live trips (F11–F14) | Wrong ETAs and false no-shows                                                                        | Confidence lowered; dispatch alerted; coordinator approval                      |
| Two facilities in the snapshot, six in operation  | Planner not yet tested at full network scale                                                         | Per-facility locks and planners; CP-SAT upgrade path                            |
| Escalation channel gaps (vacant night role)       | A hand-off could reach nobody                                                                        | Routing skips vacant roles; ops alarm on no acknowledgement                     |
| LLM misreading a message                          | Wrong question asked                                                                                 | Model never books; schema and evidence checks; clarifying question              |

**Open questions for SetuHaul:** who signs off the 21:00 rule and
earmark policy; how fresh gate and yard events are in production;
message retention period; which facilities join the pilot first.

**Roadmap**

1.  **Pilot (MVP 1):** Jaipur DC, WhatsApp driver chat, coordinator
    console, SQLite.

2.  **MVP 2:** GPS or FASTag feed, red/amber/green control tower,
    message series R1–R8, all facilities; system of record moves
    to Postgres or MongoDB.

3.  **Scale:** remaining facilities, CP-SAT planner, detention and e-way
    bill clocks, more languages (Marathi, Gujarati, Punjabi, Bengali).

## Sources

- Repository:
  [sdaistudio/SetuHaul_Logistics_Management](https://github.com/sdaistudio/SetuHaul_Logistics_Management)
  — prototype code, run scripts and outputs

- Deliverables 00–06 in docs/deliverables: review pack, problem
  definition and data audit, decision register, working prototype,
  scenario run, integrity proof, client readout

- Portal: [SetuHaul — Logistic
  Management](https://sdaistudio.github.io/SetuHaul_Logistics_Management/)
