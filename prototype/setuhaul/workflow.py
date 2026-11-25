"""The conversation workflow: a fixed state machine, not a free-roaming agent.

    driver text --> identify (phone) --> dedupe --> language --> read facts
         --> safety? --> which shipment? (ask only if evidence can't decide)
         --> intent: delay | ask-only | early | status | choose | decline
         --> tools (record ETA, re-plan, feasibility, commit/hold, escalate)
         --> reply from the catalogue --> output guard --> send

The model never chooses the next step. Each turn returns the tools it called,
so the scenario transcript doubles as an audit trace.
"""
import json
from . import config as C
from . import nlu, replies as R, timeline as T, capacity as K, escalation as E, planner as P
from .clock import ts, iso, add, hhmm, ampm, minutes_between
from .db import write_txn, one, rows
from .eta import resolve, ETA, ceil5


class ConcurrentTurn(Exception):
    pass


def facility_short(con, fid):
    f = one(con, "SELECT * FROM facilities WHERE facility_id=?", fid)
    return f["city"] + (" DC" if "Distribution" in f["facility_name"] else " Cross-Dock")


def _dedupe(con, driver_id, text, as_of, external_id):
    norm = " ".join(text.lower().split())
    with write_txn(con):
        if external_id and one(con, "SELECT 1 FROM inbound_messages WHERE external_id=?", external_id):
            return external_id
        dup = None
        if len(norm.split()) >= C.DUPLICATE_MIN_WORDS:
            prev = one(con, """SELECT external_id FROM inbound_messages WHERE driver_id=? AND norm_text=? AND duplicate_of IS NULL
                               AND julianday(received_at) >= julianday(?, ?)""", driver_id, norm, iso(as_of), f"-{C.DUPLICATE_WINDOW_MIN} minutes")
            dup = prev and prev["external_id"]
        con.execute("INSERT INTO inbound_messages VALUES (?,?,?,?,?)",
                    (external_id or f"local-{as_of.timestamp()}-{driver_id}", driver_id, iso(as_of), norm, dup))
        return dup


class Session:
    def __init__(self, con, phone, text, as_of, external_id):
        self.con, self.text, self.as_of, self.external_id = con, text, as_of, external_id
        self.driver = one(con, "SELECT * FROM drivers WHERE phone=?", phone)
        self.tools, self.buttons = [], None
        self.evidence = {"docks": set(), "times": set(), "confirmed": False}
        self.extra = {}
        if self.driver:
            c = one(con, "SELECT * FROM conversations WHERE driver_id=?", self.driver["driver_id"])
            if not c:
                con.execute("INSERT INTO conversations(driver_id, state, lang) VALUES (?, 'IDLE', 'en')", (self.driver["driver_id"],))
                c = one(con, "SELECT * FROM conversations WHERE driver_id=?", self.driver["driver_id"])
            self.conv = c
            self.facts = json.loads(c["facts_json"])
            self.offers = json.loads(c["offers_json"])
            self.lang = c["lang"]

    # ---------------------------------------------------------- plumbing
    def tm(self, d):
        x = ampm(d)
        self.evidence["times"].add(x)
        return x

    def dk(self, code):
        self.evidence["docks"].add(code)
        return code

    def say(self, key, **kw):
        return R.t(key, self.lang, **kw)

    def finish(self, text, state):
        bad = R.guard(text, self.evidence) if text else []
        if bad:                     # never send an unbacked fact; hand to a person instead
            self.tools.append("output_guard:BLOCKED " + "; ".join(bad))
            text = self.say("unknown")
        cur = self.con.execute("""UPDATE conversations SET state=?, lang=?, shipment_id=?, facts_json=?, offers_json=?,
                                  clarifications=?, version=version+1, updated_at=? WHERE driver_id=? AND version=?""",
                               (state, self.lang, self.conv["shipment_id"], json.dumps(self.facts, default=str),
                                json.dumps(self.offers, default=str), self.conv["clarifications"], iso(self.as_of),
                                self.driver["driver_id"], self.conv["version"]))
        if cur.rowcount != 1:
            raise ConcurrentTurn(self.driver["driver_id"])
        out = {"driver_id": self.driver["driver_id"], "shipment_id": self.conv["shipment_id"], "lang": self.lang,
               "reply": text, "state": state, "tools": self.tools, "buttons": self.buttons}
        out.update(self.extra)
        return out

    def set_shipment(self, sid, basis=None):
        if self.conv["shipment_id"] != sid:
            self.facts = {k: v for k, v in self.facts.items() if k == "pending"}
            self.offers = []
        self.conv = dict(self.conv, shipment_id=sid)
        if basis:
            self.facts["shipment_basis"] = basis
        self.tools.append(f"resolve_shipment:{sid}" + (f" ({basis})" if basis else ""))

    # ---------------------------------------------------------- entry point
    # run() is the whole turn. It is split into named steps so that graph.py can wire the
    # same steps as LangGraph nodes; each step returns a final result dict, or None to go on.
    def step_verify(self):
        if not self.driver:
            return {"reply": None, "state": "UNKNOWN_SENDER", "tools": ["identify:REJECTED"]}
        return None

    def step_dedupe(self):
        dup = _dedupe(self.con, self.driver["driver_id"], self.text, self.as_of, self.external_id)
        if dup:
            return {"driver_id": self.driver["driver_id"], "reply": None, "state": self.conv["state"],
                    "tools": [f"dedupe:SUPPRESSED (same as {dup})"], "lang": self.lang, "shipment_id": self.conv["shipment_id"], "buttons": None}
        return None

    def step_read(self, llm_fallback=None, history=None):
        """Rules first. Only if the rules return UNKNOWN is the optional LLM fallback asked (llm.py)."""
        self.lang = nlu.detect_lang(self.text, self.lang)
        self.x = x = nlu.extract(self.text, self.as_of)
        if x.intent == "UNKNOWN" and not x.safety and llm_fallback is not None:
            if getattr(llm_fallback, "accepts_driver_key", False):
                got = llm_fallback(self.text, self.as_of, history or [], driver_key=self.driver["driver_id"])
            else:
                got = llm_fallback(self.text, self.as_of, history or [])
            if got is not None:
                self.tools.append("llm_fallback:" + got.intent)
                self.x = x = got
        self.tools.append("read_message:" + x.intent)
        return None

    def step_safety(self):
        if self.x.safety:
            return self.safety()
        if self.x.asks_human:
            return self.to_human("HUMAN_REQUESTED", "MEDIUM")
        return None

    def run(self, llm_fallback=None, history=None):
        for step in (self.step_verify, self.step_dedupe):
            out = step()
            if out is not None:
                return out
        self.step_read(llm_fallback, history)
        return self.step_safety() or self.step_dispatch()

    def step_dispatch(self):
        """Everything after reading: route by conversation state and intent to a tool sequence."""
        x = self.x
        st = self.conv["state"]
        if st == "OPTIONS_SHOWN":
            if x.choice and not x.eta and not x.delay_min:
                if self.facts.get("early") and x.choice == 2:
                    return self.decline()
                return self.pick(x.choice)
            if x.decline:
                return self.decline()
        if st == "NEED_SHIPMENT" and (x.choice or x.hint or x.order_ref):
            sid = self.from_candidates()
            if sid:
                self.set_shipment(sid, "your answer")
                return self.route(self.facts.get("pending", {}).get("intent", "REPORT_DELAY"))
        if st == "NEED_ETA" and (x.eta or x.delay_min) and self.conv["shipment_id"]:
            return self.delay_flow()
        sid, asked = self.resolve_shipment()
        if asked:
            return asked
        return self.route(x.intent)

    def route(self, intent):
        ship = T.shipment(self.con, self.conv["shipment_id"])
        if ship["current_status"] == "CANCELLED":
            return self.cancelled(ship)
        if intent == "EARLY_ARRIVAL":
            return self.early(ship)
        if intent in ("AT_YARD_STATUS", "CHECK_STATUS"):
            return self.status(ship)
        if intent == "ASK_OPTIONS":
            return self.ask_only(ship)
        if intent in ("REPORT_DELAY", "ANSWER") or self.x.eta or self.x.delay_min or self.facts.get("pending"):
            return self.delay_flow()
        return self.finish(self.say("unknown"), self.conv["state"])

    # ---------------------------------------------------------- which shipment?
    def resolve_shipment(self):
        x, con = self.x, self.con
        trips = rows(con, """SELECT s.*, fc.gate_in_ts FROM shipments s LEFT JOIN facility_checkins fc USING(shipment_id)
                             WHERE s.driver_id=? AND s.current_status<>'COMPLETED' ORDER BY s.original_eta_ts""", self.driver["driver_id"])
        live = [t for t in trips if t["current_status"] != "CANCELLED"]
        here = lambda t: t["gate_in_ts"] and ts(t["gate_in_ts"]) <= self.as_of
        pick, basis = None, None
        if x.order_ref:
            m = [t for t in trips if x.order_ref in (t["order_reference"], t["shipment_id"])]
            pick, basis = (m[0], "your reference") if m else (None, None)
        elif self.conv["shipment_id"] and self.conv["state"] in ("NEED_ETA", "OPTIONS_SHOWN", "PENDING_APPROVAL"):
            pick = one(con, "SELECT * FROM shipments WHERE shipment_id=?", self.conv["shipment_id"])
        elif len(live) == 1:
            pick = live[0]
        elif not live and trips:
            pick = trips[-1]
        else:
            m = []
            if x.hint in ("HEAVY", "REEFER"):
                m, basis = [t for t in live if t["required_dock_type"] == x.hint], f"your {x.hint.lower()} load"
            elif x.intent in ("EARLY_ARRIVAL", "AT_YARD_STATUS"):
                m, basis = [t for t in live if here(t)], "the truck at our gate"
            elif x.intent == "REPORT_DELAY":
                m, basis = [t for t in live if t["current_status"] == "IN_TRANSIT" and not here(t)], "the trip you are driving now"
            if len(m) == 1:
                pick = m[0]
        if pick:
            self.set_shipment(pick["shipment_id"], basis)
            if x.hint in ("HEAVY", "REEFER") and len([t for t in live if t["current_status"] == "IN_TRANSIT"]) > 1:
                with write_txn(con):   # driver shown on two moving trucks (audit F11): tell dispatch, don't block the driver
                    E.escalate(con, "DATA_CONFLICT", "LOW", pick["shipment_id"], self.driver["driver_id"], self.as_of,
                               {"conflict": f"{self.driver['driver_id']} is assigned to {len(live)} trips in transit",
                                "trips": [t["shipment_id"] for t in live], "asked": "Who is driving the other truck? Its ETA is unreliable."})
                self.tools.append("escalate:DATA_CONFLICT (non-blocking)")
            return pick["shipment_id"], None
        # Ask. Evidence can still order the question and suggest an answer.
        suggest = ""
        if x.intent == "REPORT_DELAY":
            away = [t for t in live if not here(t)]
            if len(away) == 1 and len(live) > 1:
                suggest = "\n" + R.phrase("other_truck", self.lang, "(Your other truck is already at our gate, so I think you mean {ref}.)",
                                           "(Aapka doosra truck gate par hai, isliye shayad aap {ref} ki baat kar rahe hain.)",
                                           "(आपका दूसरा ट्रक गेट पर है, इसलिए शायद आप {ref} की बात कर रहे हैं।)").format(ref=away[0]['order_reference'])
        lines, self.buttons = [], []
        for i, t in enumerate(live, 1):
            when = ampm(ts(t["original_eta_ts"]))
            state = " - at our gate now" if here(t) else ""
            self.tm(ts(t["original_eta_ts"]))
            lines.append(f"{i}) {t['order_reference']} - {t['origin_city']} load, due {when}{state}")
            self.buttons.append(f"{t['origin_city']} {when}"[:20])
        self.facts["candidates"] = [t["shipment_id"] for t in live]
        self.facts["pending"] = self._pending()
        self.conv = dict(self.conv, clarifications=self.conv["clarifications"] + 1)
        self.tools.append("ask:WHICH_SHIPMENT")
        return None, self.finish(self.say("ask_shipment", n=len(live), choices="\n".join(lines), suggest=suggest), "NEED_SHIPMENT")

    def _pending(self):
        x, p = self.x, dict(self.facts.get("pending", {}))
        for k, v in (("intent", x.intent if x.intent not in ("ANSWER", "CHOOSE") else None), ("eta", x.eta and iso(x.eta)),
                     ("eta_conf", x.eta_confidence), ("delay_min", x.delay_min), ("earliest", x.earliest_start and iso(x.earliest_start)),
                     ("reason", x.reason), ("urgency", x.urgency_claim or None),
                     ("window", x.eta_window and [iso(w) for w in x.eta_window if w])):
            if v:
                p[k] = v
        if x.limits:
            p["limits"] = {**p.get("limits", {}), **{k: iso(v) for k, v in x.limits.items()}}
        return p

    def from_candidates(self):
        cands = [one(self.con, "SELECT * FROM shipments WHERE shipment_id=?", s) for s in self.facts.get("candidates", [])]
        x = self.x
        if x.order_ref:
            m = [c for c in cands if x.order_ref in (c["order_reference"], c["shipment_id"])]
            return m[0]["shipment_id"] if m else None
        if x.choice and 1 <= x.choice <= len(cands):
            return cands[x.choice - 1]["shipment_id"]
        if x.hint in ("EVENING", "MORNING") and cands:
            srt = sorted(cands, key=lambda c: c["original_eta_ts"])
            return (srt[-1] if x.hint == "EVENING" else srt[0])["shipment_id"]
        if x.hint in ("HEAVY", "REEFER"):
            m = [c for c in cands if c["required_dock_type"] == x.hint]
            return m[0]["shipment_id"] if len(m) == 1 else None
        return None

    # ---------------------------------------------------------- flows
    def delay_flow(self):
        con, x, as_of = self.con, self.x, self.as_of
        sid = self.conv["shipment_id"]
        ship = T.shipment(con, sid)
        fid = ship["destination_facility_id"]
        p = self._pending()
        eta = x.eta or ts(p.get("eta"))
        conf = x.eta_confidence or p.get("eta_conf")
        window = x.eta_window or (p.get("window") and tuple(ts(w) for w in p["window"]))
        delay = x.delay_min or p.get("delay_min")
        earliest = x.earliest_start or ts(p.get("earliest")) or ts(self.facts.get("earliest"))
        limits = {**{k: ts(v) for k, v in self.facts.get("limits", {}).items()},
                  **{k: ts(v) for k, v in p.get("limits", {}).items()}, **x.limits}
        cur = K.current_appointment(con, sid)
        basis = None
        if not eta and delay:
            anchor = ts(cur["slot_start_ts"]) if cur else ts(ship["original_eta_ts"])
            L = R.phrase("basis", self.lang, ("your {t} slot", "planned {t}", "now", "min"), ("aapka {t} slot", "plan {t}", "abhi", "min"),
                         ("आपका {t} स्लॉट", "प्लान {t}", "अभी", "मिनट"))
            label = (L[0] if cur else L[1]).format(t=ampm(anchor))
            if add(anchor, delay) < as_of:
                anchor, label = as_of, L[2]
            eta, conf = add(anchor, delay), "MEDIUM"
            basis = f"{label} + {delay} {L[3]}"
            self.tm(anchor) if label != "now" else None
        if not eta:
            if self.conv["clarifications"] >= C.MAX_CLARIFICATIONS:
                return self.to_human("CLARIFICATION_LIMIT", "MEDIUM")
            self.facts["pending"] = p
            self.conv = dict(self.conv, clarifications=self.conv["clarifications"] + 1)
            self.tools.append("ask:ETA")
            return self.finish(self.say("ask_eta", facility=facility_short(con, fid)), "NEED_ETA")

        if (x.next_commitment or p.get("next_commitment")) and not limits and not self.facts.get("asked_limit") \
                and self.conv["clarifications"] < C.MAX_CLARIFICATIONS:
            p.update(eta=iso(eta), eta_conf=conf, next_commitment=True)
            if basis:
                p["basis"] = basis
            self.facts["pending"], self.facts["asked_limit"] = p, True
            self.conv = dict(self.conv, clarifications=self.conv["clarifications"] + 1)
            self.tools.append("ask:LATEST_GATE_OUT (driver mentioned another pickup)")
            eta_text = R.phrase("eta_text", self.lang, "arriving about {t}", "lagbhag {t} tak", "लगभग {t} तक").format(t=self.tm(eta))
            return self.finish(self.say("ask_limit", eta_text=eta_text), "NEED_ETA")
        if x.intent in ("REPORT_DELAY", "ANSWER") and not x.limits and self.facts.get("asked_limit") and x.eta and not x.delay_min \
                and p.get("next_commitment") and "LATEST_GATE_OUT" not in limits:
            limits["LATEST_GATE_OUT"] = x.eta          # the answer to "by what time must you leave?" is a gate-out limit
            eta, conf = ts(p["eta"]), p.get("eta_conf")
            basis = p.get("basis")
        with write_txn(con):        # the driver's statement becomes part of the system of record
            n = one(con, "SELECT COUNT(*) AS n FROM eta_updates WHERE eta_update_id LIKE 'ETA-N%'")["n"] + 1
            con.execute("INSERT INTO eta_updates VALUES (?,?, 'DRIVER_DECLARED', ?,?,?,?,?,?)",
                        (f"ETA-N{n:03d}", sid, self.driver["driver_id"], iso(eta), conf or "MEDIUM", p.get("reason") or x.reason,
                         self.text[:140], iso(as_of)))
            if window and len(window) == 2 and window[1]:
                con.execute("INSERT INTO eta_windows VALUES (?,?,?,?,?)", (sid, iso(window[0]), iso(window[1]), self.text[:140], iso(as_of)))
            con.execute("UPDATE shipments SET latest_eta_ts=?, updated_at=? WHERE shipment_id=?", (iso(eta), iso(as_of), sid))
            for typ, v in limits.items():
                con.execute("INSERT INTO driver_constraints VALUES (?,?,?,?,?)", (sid, typ, iso(v), self.text[:140], iso(as_of)))
            if earliest:
                con.execute("INSERT INTO driver_constraints VALUES (?,?,?,?,?)", (sid, "EARLIEST_START", iso(earliest), self.text[:140], iso(as_of)))
        self.tools.append(f"record_eta:{hhmm(eta)} {conf}")
        self.facts.update({"eta": iso(eta), "eta_conf": conf, "limits": {k: iso(v) for k, v in limits.items()},
                           "earliest": earliest and iso(earliest)})
        self.facts.pop("pending", None)
        P.replan(con, fid, as_of)
        self.tools.append("scheduling_tool:replan+earmarks")

        preface = ""
        if basis:
            preface = self.say("state_back", eta=self.tm(eta), basis=basis)
        if cur:
            tl = T.build(con, fid, as_of)
            st = tl.booking_status.get(cur["appointment_id"], {})
            ok = st.get("status") == "OK"
            if ok and earliest and ts(st["planned_start"]) < earliest:
                ok = False
            if ok and "LATEST_GATE_OUT" in limits:
                ok = add(ts(st["planned_start"]), ship["expected_unload_min"] + C.YARD_TO_DOCK_MIN) <= limits["LATEST_GATE_OUT"]
            if ok:
                self.tools.append("check_current_booking:STILL_FEASIBLE")
                return self.finish(preface + self.say("current_ok", eta=self.tm(eta), dock=self.dk(cur["dock_code"]),
                                                      start=self.tm(ts(st["planned_start"]))), "BOOKED")
            self.tools.append(f"check_current_booking:{st.get('status', 'NOT_FEASIBLE')}")
        opts, rej = T.options(con, sid, as_of, constraints=limits, earliest_start=earliest)
        self.tools.append(f"check_slot_feasibility:{len(opts)} options")
        if not opts:
            return self.no_slot(ship, rej)
        if p.get("urgency") or x.urgency_claim:
            preface += self.say("urgency_note")
            self.tools.append("policy:urgency_claim_ignored")
        return self.show(opts, preface, ship)

    def show(self, opts, preface, ship, key="options", eta=None):
        lines, self.buttons = [], []
        for i, o in enumerate(opts, 1):
            s, e = ts(o["start"]), ts(o["unload_end"])
            note = self.say("approval_note") if o["needs_approval"] else ""
            lines.append(self.say("line", n=i, dock=self.dk(o["dock_code"]), start=self.tm(s), end=self.tm(e), note=note))
            self.buttons.append(f"{o['dock_code']} {ampm(s)}")
        nums = "1" if len(opts) == 1 else ", ".join(str(i) for i in range(1, len(opts))) + f" or {len(opts)}"
        if self.lang != "en":
            nums = nums.replace(" or ", R.pick(R.OR_WORD, self.lang))
        self.offers = [dict(o, offered_at=iso(self.as_of)) for o in opts]
        self.extra["options"] = opts
        kw = dict(preface=preface, facility=facility_short(self.con, ship["destination_facility_id"]), lines="\n".join(lines), nums=nums)
        if eta:
            kw["eta"] = self.tm(eta)
        return self.finish(self.say(key, **kw), "OPTIONS_SHOWN")

    def pick(self, choice):
        con, as_of = self.con, self.as_of
        if not 1 <= choice <= len(self.offers):
            n = len(self.offers)
            return self.finish(self.say("invalid_choice", nums="1" if n == 1 else f"1-{n}"), "OPTIONS_SHOWN")
        o = self.offers[choice - 1]
        sid = self.conv["shipment_id"]
        ship = T.shipment(con, sid)
        key = f"commit:{self.external_id}"
        if o["needs_approval"]:
            res = K.hold_for_approval(con, sid, o["slot_id"], as_of, self.driver["driver_id"], o["needs_approval"], idem_key=key)
            if res["ok"]:
                self.tools.append(f"hold_for_approval:{res['hold_id']}")
                self.facts["hold_id"] = res["hold_id"]
                W_ = R.phrase("approval_why", self.lang, ("start after 9 PM", "our records show this truck on another trip"),
                              ("9 PM ke baad shuru", "record mein yeh truck doosre trip par dikh raha hai"),
                              ("रात 9 बजे के बाद शुरू", "रिकॉर्ड में यह ट्रक दूसरी ट्रिप पर दिख रहा है"))
                why = ", ".join((W_[0] if r.startswith("START_AFTER") else W_[1] if r.startswith("DATA_CONFLICT") else r) for r in o["needs_approval"])
                return self.finish(self.say("approval_requested", dock=self.dk(o["dock_code"]), start=self.tm(ts(o["start"])), why=why,
                                            deadline=self.tm(ts(res["deadline"]))), "PENDING_APPROVAL")
        else:
            res = K.commit(con, sid, o["slot_id"], as_of, idem_key=key)
        if res["ok"] and res["code"] == "CONFIRMED":
            self.evidence["confirmed"] = True
            ob = res["option"]
            self.tools.append(f"book:CONFIRMED {res['appointment_id']}" + (f" (released {', '.join(res['released'])})" if res["released"] else ""))
            self.offers = []
            self.extra["booking"] = res
            gate_by = add(ts(ob["start"]), -C.GATE_TO_DOCK_MIN)
            tail = ""
            if C.PRIORITY_RANK[ship["priority_code"]] >= C.PRIORITY_RANK["HIGH"] and ob["wait_min"] >= 30:
                found = P.swap_proposal(con, sid, as_of)
                prop = found["best"]
                if not prop:
                    self.tools.append("scheduling_tool:swap_search -> no admissible swap" +
                                      (f" ({'; '.join(found['rejected'])})" if found["rejected"] else ""))
                if prop:
                    with write_txn(con):
                        esc = E.escalate(con, "PLAN_CHANGE", "HIGH", sid, self.driver["driver_id"], as_of,
                                         {"proposal": prop, "decision_needed": "Approve moving a lower-priority booking for this load?",
                                          "who_tells_displaced_driver": "the system, from the reply catalogue, after approval"})
                    self.tools.append(f"scheduling_tool:swap_proposal -> {esc['escalation_id']} ({prop['explain']})")
                    self.extra["swap_proposal"] = dict(prop, escalation_id=esc["escalation_id"])
                    tail = self.say("swap_check", by=self.tm(ts(esc["respond_by"])))
            if resolve(con, ship, as_of).at_facility:
                return self.finish(self.say("booked_here", dock=self.dk(ob["dock_code"]), facility=facility_short(con, ship["destination_facility_id"]),
                                            start=self.tm(ts(ob["start"])), ref=res["appointment_id"]), "BOOKED")
            return self.finish(self.say("booked", dock=self.dk(ob["dock_code"]), facility=facility_short(con, ship["destination_facility_id"]),
                                        start=self.tm(ts(ob["start"])), gate_by=self.tm(max(gate_by, as_of)), ref=res["appointment_id"]) + tail, "BOOKED")
        self.tools.append(f"book:{res['code']}" + (f" ({res.get('why')})" if res.get("why") else ""))
        limits = {k: ts(v) for k, v in self.facts.get("limits", {}).items()}
        opts, rej = T.options(con, sid, as_of, constraints=limits, earliest_start=ts(self.facts.get("earliest")), exclude_slots=(o["slot_id"],))
        if not opts:
            return self.no_slot(ship, rej)
        why_key = {"TAKEN_BY_ANOTHER_TRUCK": "why_taken", "HELD_FOR_ANOTHER_TRUCK": "why_taken",
                   "PROTECTED_FOR_PRIORITY_LOAD": "why_protected"}.get(res.get("why"), "why_nofit")
        lines, self.buttons = [], []
        for i, oo in enumerate(opts, 1):
            note = self.say("approval_note") if oo["needs_approval"] else ""
            lines.append(self.say("line", n=i, dock=self.dk(oo["dock_code"]), start=self.tm(ts(oo["start"])), end=self.tm(ts(oo["unload_end"])), note=note))
            self.buttons.append(f"{oo['dock_code']} {ampm(ts(oo['start']))}")
        self.offers = [dict(oo, offered_at=iso(as_of)) for oo in opts]
        nums = "1" if len(opts) == 1 else ", ".join(str(i) for i in range(1, len(opts))) + f" or {len(opts)}"
        return self.finish(self.say("option_gone", dock=self.dk(o["dock_code"]), start=self.tm(ts(o["start"])), why=self.say(why_key),
                                    lines="\n".join(lines), nums=nums), "OPTIONS_SHOWN")

    def decline(self):
        self.offers = []
        self.tools.append("decline:NOTHING_HELD")
        has = K.current_appointment(self.con, self.conv["shipment_id"])
        return self.finish(self.say("declined"), "BOOKED" if has else "CLOSED")

    def early(self, ship):
        cur = K.current_appointment(self.con, ship["shipment_id"])
        if not cur:
            return self.delay_flow()
        old = ts(cur["slot_start_ts"])
        opts, _ = T.options(self.con, ship["shipment_id"], self.as_of, before=old, limit=None)
        opts = [o for o in opts if not o["needs_approval"] and not o["releases"]]
        self.tools.append(f"check_slot_feasibility:{len(opts)} earlier options (no displacement)")
        if not opts:
            return self.finish(self.say("early_policy", old=self.tm(old)), "BOOKED")
        o = opts[0]
        self.offers = [dict(o, offered_at=iso(self.as_of))]
        self.facts["early"] = True
        self.buttons = [f"Take {o['dock_code']} {ampm(ts(o['start']))}", f"Keep {ampm(old)}"]
        return self.finish(self.say("early_offer", dock=self.dk(o["dock_code"]), start=self.tm(ts(o["start"])), old=self.tm(old)), "OPTIONS_SHOWN")

    def status(self, ship):
        con, as_of = self.con, self.as_of
        sid = ship["shipment_id"]
        cur = K.current_appointment(con, sid)
        chk = one(con, "SELECT * FROM facility_checkins WHERE shipment_id=?", sid)
        fid = ship["destination_facility_id"]
        if chk and chk["gate_in_ts"] and ts(chk["gate_in_ts"]) <= as_of and not chk["dock_in_ts"] and cur:
            tl = T.build(con, fid, as_of)
            st = tl.booking_status.get(cur["appointment_id"], {})
            grace = int(T.facility_rules(con, fid).get("NO_SHOW_GRACE_MIN", 30))
            within = ts(chk["gate_in_ts"]) <= add(ts(cur["slot_start_ts"]), grace)
            self.tools.append(f"get_booking_status:{st.get('status')}")
            if st.get("status") in ("OK", "AT_RISK") and within:
                pos = one(con, """SELECT COUNT(*) AS n FROM facility_checkins WHERE facility_id=? AND gate_in_ts<=?
                                  AND (dock_in_ts IS NULL OR dock_in_ts>?)""", fid, chk["gate_in_ts"], iso(as_of))["n"]
                return self.finish(self.say("status_yard", slot=self.tm(ts(cur["slot_start_ts"])), dock=self.dk(cur["dock_code"]),
                                            gate=self.tm(ts(chk["gate_in_ts"])), grace=grace, call=self.tm(ts(st["planned_start"])), pos=pos), "BOOKED")
            self.x.eta = ts(chk["gate_in_ts"])
            return self.delay_flow()
        if cur:
            self.tools.append("get_booking_status:BOOKED")
            return self.finish(self.say("status_booked", dock=self.dk(cur["dock_code"]), facility=facility_short(con, fid),
                                        start=self.tm(ts(cur["slot_start_ts"])), status=cur["appointment_status"].lower()), "BOOKED")
        return self.delay_flow()

    def ask_only(self, ship):
        x, con, as_of = self.x, self.con, self.as_of
        eta = x.eta or ts(self.facts.get("hyp_eta")) or resolve(con, ship, as_of).point
        conf = x.eta_confidence or self.facts.get("hyp_conf") or "MEDIUM"
        self.facts.update({"hyp_eta": iso(eta), "hyp_conf": conf, "ask_only": True})
        if x.earliest_start:
            self.facts["earliest"] = iso(x.earliest_start)
        latest = add(eta, C.ETA_BUFFER_MIN[conf])
        hyp = ETA(ship["shipment_id"], eta, add(eta, -C.ETA_OPTIMISTIC_MIN[conf]), latest, conf, "DRIVER_HYPOTHETICAL", False,
                  ceil5(add(latest, C.GATE_TO_DOCK_MIN)))
        opts, rej = T.options(con, ship["shipment_id"], as_of, earliest_start=ts(self.facts.get("earliest")), eta_override=hyp)
        self.tools.append(f"check_slot_feasibility:{len(opts)} options (read-only, nothing recorded)")
        if not opts:
            return self.no_slot(ship, rej)
        return self.show(opts, "", ship, key="ask_only_options", eta=eta)

    def cancelled(self, ship):
        a = one(self.con, "SELECT * FROM appointments WHERE shipment_id=? ORDER BY updated_at DESC LIMIT 1", ship["shipment_id"])
        car = one(self.con, "SELECT * FROM carriers WHERE carrier_id=?", ship["carrier_id"])
        when = ts((a and a["cancelled_at"]) or ship["updated_at"])
        self.tools.append("get_booking_status:SHIPMENT_CANCELLED")
        self.offers = []
        return self.finish(self.say("cancelled", order=ship["order_reference"], when=self.tm(when),
                                    facility=facility_short(self.con, ship["destination_facility_id"]), phone=car["contact_phone"]), "CLOSED")

    # ---------------------------------------------------------- hand-offs
    def no_slot(self, ship, rej):
        con, as_of = self.con, self.as_of
        wi = P.what_if(con, ship["shipment_id"], as_of)
        need = {"REEFER": "reefer", "HEAVY": "heavy"}.get(ship["required_dock_type"], "suitable")
        with write_txn(con):
            esc = E.escalate(con, "NO_FEASIBLE_SLOT", "HIGH", ship["shipment_id"], self.driver["driver_id"], as_of, {
                "driver_said": self.text, "facts_held": self.facts, "eta": resolve(con, ship, as_of).as_dict(),
                "current_booking": K.current_appointment(con, ship["shipment_id"]),
                "options_tried": rej, "why_no_slot": wi["why"], "recommendations": wi["recommendations"],
                "driver_told": "No slot today; do not go to the dock until contacted."})
        self.tools.append(f"escalate:NO_FEASIBLE_SLOT {esc['escalation_id']} -> {esc['routed_to']['name']} via {esc['routed_to']['channel']}")
        self.extra["escalation"] = esc
        who = f"{esc['routed_to']['name']} ({esc['routed_to']['role'].replace('_', ' ').lower()})"
        for d in wi.get("why_times", []):
            self.tm(d)
        for code in wi.get("why_docks", []):
            self.dk(code)
        return self.finish(self.say("no_slot", need=need, facility=facility_short(con, ship["destination_facility_id"]),
                                    why=wi["why_short"], who=who, by=self.tm(ts(esc["respond_by"]))), "ESCALATED")

    def safety(self):
        with write_txn(self.con):
            esc = E.escalate(self.con, "SAFETY", "CRITICAL", self.conv["shipment_id"], self.driver["driver_id"], self.as_of,
                             {"driver_said": self.text, "agent_action": "Stopped all scheduling talk; advised 112; paged shift manager."})
        self.tools.append(f"escalate:SAFETY {esc['escalation_id']} -> {esc['routed_to']['name']} via {esc['routed_to']['channel']}")
        self.extra["escalation"] = esc
        return self.finish(self.say("safety", who=esc["routed_to"]["name"], sla=E.URGENCY_SLA_MIN["CRITICAL"]), "ESCALATED")

    def to_human(self, reason, urgency):
        with write_txn(self.con):
            esc = E.escalate(self.con, reason, urgency, self.conv["shipment_id"], self.driver["driver_id"], self.as_of,
                             {"driver_said": self.text, "facts_held": self.facts})
        self.tools.append(f"escalate:{reason} {esc['escalation_id']}")
        self.extra["escalation"] = esc
        return self.finish(self.say("human", who=esc["routed_to"]["name"], by=self.tm(ts(esc["respond_by"]))), "ESCALATED")


def notify_decision(con, driver_id, res, as_of):
    """After a coordinator decides, the driver hears the outcome (same catalogue, same guard)."""
    conv = one(con, "SELECT * FROM conversations WHERE driver_id=?", driver_id)
    lang = conv["lang"] if conv else "en"
    if res.get("ok") and res.get("code") == "CONFIRMED":
        o = res["option"]
        ship = T.shipment(con, o.get("shipment_id") or conv["shipment_id"])
        text = R.t("approval_granted", lang, dock=o["dock_code"], facility=facility_short(con, ship["destination_facility_id"]),
                   start=ampm(ts(o["start"])), gate_by=ampm(add(ts(o["start"]), -C.GATE_TO_DOCK_MIN)), ref=res["appointment_id"])
        state = "BOOKED"
    else:
        h = res.get("hold") or {}
        text = R.t("declined", lang) if res.get("code") == "REJECTED" else R.t("approval_failed", lang, dock="-", start="-")
        state = "CLOSED"
    con.execute("UPDATE conversations SET state=?, version=version+1, updated_at=? WHERE driver_id=?", (state, iso(as_of), driver_id))
    return {"driver_id": driver_id, "reply": text, "state": state, "tools": ["notify_driver:" + res.get("code", "?")], "lang": lang}


def notify_swap(con, res, as_of):
    """Tell both drivers after an approved swap: the priority driver first, then an apology to the moved driver."""
    out = []
    for side in ("high", "low"):
        r = res[side]
        o = r["option"]
        sid = res["high_old"]["shipment_id"] if side == "high" else res["low_old"]["shipment_id"]
        ship = T.shipment(con, sid)
        conv = one(con, "SELECT * FROM conversations WHERE driver_id=?", ship["driver_id"])
        lang = conv["lang"] if conv else "en"
        fac = facility_short(con, ship["destination_facility_id"])
        if side == "high":
            text = R.t("upgraded", lang, dock=o["dock_code"], facility=fac, start=ampm(ts(o["start"])),
                       gate_by=ampm(add(ts(o["start"]), -C.GATE_TO_DOCK_MIN)), ref=r["appointment_id"])
        else:
            old = res["low_old"]
            car = one(con, "SELECT contact_phone FROM carriers WHERE carrier_id=?", ship["carrier_id"])
            told = one(con, "SELECT start_ts FROM dock_reservations WHERE ref_id=? AND kind='BOOKING' ORDER BY created_at LIMIT 1", old["appointment_id"])
            tl_start = ampm(ts(told["start_ts"] if told else old["slot_start_ts"]))
            text = R.t("moved", lang, old_dock=old["dock_code"], old_start=tl_start, dock=o["dock_code"], start=ampm(ts(o["start"])),
                       facility=fac, ref=r["appointment_id"], phone=car["contact_phone"])
        out.append({"driver_id": ship["driver_id"], "reply": text, "state": "BOOKED", "tools": [f"notify_driver:SWAP_{side.upper()}"], "lang": lang})
    return out


def handle(con, phone, text, as_of, external_id=None, llm_fallback=None, history=None):
    """One inbound driver message -> one structured turn result.

    llm_fallback: optional callable(text, as_of, history) -> nlu.Facts or None (see llm.py).
    With None (the default, and in every test) the turn is fully deterministic.
    """
    return Session(con, phone, text, as_of, external_id).run(llm_fallback, history)
