"""Every language must reach the same outcome as English, turn by turn, on the whole 4 August morning.

Run:  python3 -I -m unittest discover -s tests -p "test_language_replay.py" -v      (from prototype/)

  * Reader: the native-script words added for early arrival, yard, trailer, reefer, urgent, "leave by",
    "which slots", "and after 3?", "keep my slot"; the words for "by" are firm, not "about"; safety words
    must start a word, and the short ones (आग, chot, fire) must also end one.
  * Replay: scripts/language_replay.py runs run_scenarios.py with every driver message translated
    (Hinglish, Hindi, Gujarati, Marathi, Punjabi, Marwari). Each driver turn must end in the same state as
    the English run (out/scenario_run.json, pinned by test_scenario_determinism.py), the coordinator
    notices must match, and the engine must detect the intended language on every message.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from setuhaul import nlu          # noqa: E402
from setuhaul.clock import ts     # noqa: E402

AS_OF = ts("2026-08-04T09:30:00+05:30")
LANGS = ("hi-Latn", "hi", "gu", "mr", "pa", "mwr")
x = lambda text: nlu.extract(text, AS_OF)


class Reader(unittest.TestCase):
    def test_early_arrival(self):
        for t in ["मैं जल्दी आ गया हूँ, गेट पर खड़ा हूँ।", "હું વહેલો આવી ગયો છું, ગેટ પર ઊભો છું.", "मी लवकर आलो आहे, गेटवर उभा आहे.",
                  "ਮੈਂ ਜਲਦੀ ਆ ਗਿਆ ਹਾਂ, ਗੇਟ ਤੇ ਖੜ੍ਹਾ ਹਾਂ।", "म्हैं जल्दी आग्यो, गेट माथै ऊभो हूं।"]:
            with self.subTest(t=t):
                f = x(t)
                self.assertEqual(f.intent, "EARLY_ARRIVAL")
                self.assertFalse(f.safety)

    def test_in_the_yard(self):
        for t in ["मैं लेट पहुँचा हूँ और यार्ड में खड़ा हूँ।", "હું મોડો પહોંચ્યો અને યાર્ડમાં છું.", "ਮੈਂ ਲੇਟ ਪਹੁੰਚਿਆ ਅਤੇ ਯਾਰਡ ਵਿੱਚ ਹਾਂ।"]:
            with self.subTest(t=t):
                self.assertEqual(x(t).intent, "AT_YARD_STATUS")

    def test_truck_type_and_urgency(self):
        self.assertEqual(x("भारी ट्रेलर है, 12 बजे पहुँचूँगा।").hint, "HEAVY")
        self.assertEqual(x("ਭਾਰੀ ਟ੍ਰੇਲਰ ਹੈ, 12 ਵਜੇ ਪਹੁੰਚਾਂਗਾ।").hint, "HEAVY")
        self.assertIsNone(x("भारी ट्रैफिक है, 12 बजे पहुँचूँगा।").hint)      # heavy traffic is not a heavy truck
        self.assertEqual(x("રીફર આજે રાત્રે અનલોડ થશે?").hint, "REEFER")
        self.assertTrue(x("लोड अर्जंट आहे, पहिला रिकामा डॉक मिळेल का?").urgency_claim)

    def test_leave_by_is_a_gate_out_limit(self):
        for t in ["टायर ठीक हो गया। 11:10 बजे पहुँच जाऊँगा। मुझे 1:30 बजे तक निकलना है, दूसरा लोड उठाना है।",
                  "હું 11:10 વાગ્યે પહોંચીશ. મારે 1:30 વાગ્યા સુધીમાં નીકળવું છે, બીજો લોડ લેવાનો છે.",
                  "मी 11:10 वाजता पोहोचेन. मला 1:30 वाजेपर्यंत निघायचे आहे, दुसरा लोड घ्यायचा आहे."]:
            with self.subTest(t=t):
                f = x(t)
                self.assertEqual((f.eta.hour, f.eta.minute), (11, 10))
                self.assertEqual(f.limits["LATEST_GATE_OUT"].hour, 13)
                self.assertTrue(f.next_commitment)

    def test_by_is_firm_and_about_is_approximate(self):
        for t in ["11:30 સુધી પહોંચી જઈશ", "11:30 पर्यंत पोहोचेन", "11:30 ਤੱਕ ਪਹੁੰਚ ਜਾਵਾਂਗਾ", "11:30 बज्यां तांई पूगसूं"]:
            with self.subTest(t=t):
                self.assertEqual(x(t).eta_confidence, "HIGH")
        for t in ["આશરે 11:30 વાગ્યે પહોંચીશ", "मी सुमारे 11:20 वाजता पोहोचेन", "ਲਗਭਗ 11:20 ਵਜੇ ਪਹੁੰਚਾਂਗਾ", "लगभग 11:20 बज्यां पूगसूं"]:
            with self.subTest(t=t):
                f = x(t)
                if f.eta:
                    self.assertEqual(f.eta_confidence, "MEDIUM")

    def test_which_slots_and_and_after(self):
        for t in ["1 बजे के बाद कौन से स्लॉट मिल सकते हैं?", "1 વાગ્યા પછી કયા સ્લોટ મળી શકે?", "और 3 बजे के बाद?", "आणि 3 नंतर?",
                  "ਤੇ 3 ਵਜੇ ਤੋਂ ਬਾਅਦ?", "अर 3 बज्यां पछै?"]:
            with self.subTest(t=t):
                self.assertEqual(x(t).intent, "ASK_OPTIONS")
        self.assertEqual(x("शहापूरनंतर ट्रॅफिक आहे. 12 नंतर कोणता स्लॉट आहे?").intent, "REPORT_DELAY")   # a delay, not a browse

    def test_keep_my_slot(self):
        for t in ["नहीं चाहिए, मैं अपना स्लॉट ही रखूँगा", "ના, મારો સ્લોટ જ રાખીશ", "नको, माझा स्लॉटच ठेवतो",
                  "ਨਹੀਂ, ਮੈਂ ਆਪਣਾ ਸਲਾਟ ਹੀ ਰੱਖਾਂਗਾ", "कोनी, म्हैं म्हारो स्लॉट ही राखसूं", "nahi chahiye, main apna slot hi rakhunga"]:
            with self.subTest(t=t):
                self.assertEqual(x(t).intent, "DECLINE")

    def test_safety_words_are_whole_words(self):
        for t in ["आगे जाम है", "म्हैं जल्दी आग्यो", "choti gaadi hai", "my boss fired the cleaner", "आगरा से आ रहा हूँ"]:
            with self.subTest(t=t):
                self.assertFalse(x(t).safety)
        for t in ["आग लग गई", "गाड़ी में आग है", "helper ko chot lagi hai", "fire in the cabin", "driver injured", "अपघातात जखमी"]:
            with self.subTest(t=t):
                self.assertTrue(x(t).safety)

    def test_hinglish_detection(self):
        self.assertEqual(nlu.detect_lang("Main 45 minute late ho jaunga."), "hi-Latn")
        self.assertEqual(nlu.detect_lang("The main road is closed near Shahpura"), "en")


class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, "out", "scenario_run.json"), encoding="utf-8") as f:
            cls.en = json.load(f)["log"]
        cls.dir = tempfile.mkdtemp(prefix="sh-replay-")
        cls.runs = {}
        for lang in LANGS:
            out = os.path.join(cls.dir, lang)
            os.makedirs(out)
            p = subprocess.run([sys.executable, "-I", os.path.join(HERE, "scripts", "language_replay.py"), lang, out],
                               capture_output=True, text=True, cwd=HERE)
            assert p.returncode == 0, p.stderr[-2000:]
            with open(os.path.join(out, "scenario_run.json"), encoding="utf-8") as f:
                cls.runs[lang] = json.load(f)["log"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_every_turn_same_state_as_english(self):
        en = {e["ext"]: e["state"] for e in self.en if e["kind"] == "driver"}
        for lang, log in self.runs.items():
            got = {e["ext"]: e["state"] for e in log if e["kind"] == "driver"}
            with self.subTest(lang=lang):
                self.assertEqual(got, en)

    def test_coordinator_notices_match(self):
        key = lambda log: [(e["t"], e.get("case"), e.get("state")) for e in log if e["kind"] == "notify"]
        for lang, log in self.runs.items():
            with self.subTest(lang=lang):
                self.assertEqual(key(log), key(self.en))

    def test_language_detected_on_every_message(self):
        for lang, log in self.runs.items():
            with self.subTest(lang=lang):
                self.assertEqual({e.get("lang") for e in log if e["kind"] == "driver"}, {lang})


if __name__ == "__main__":
    unittest.main()
