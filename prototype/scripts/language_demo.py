"""Language demo: driver conversations in all seven languages (English, Hinglish, Hindi, Gujarati,
Marathi, Punjabi and Marwari).

Each conversation runs twice through the real engine on a fresh copy of the 4 August world
(rewound to 08:20): once in the driver's language and once with the English wording, so the
portal can show the English meaning beside every reply. Nothing here is hand-written except
the driver messages.

Run:  python3 -I scripts/language_demo.py out/
Writes out/language_demo.json (and ../lang_data.js for the portal when run from the repo).
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from setuhaul import db, world, workflow as W, languages as L
from setuhaul.clock import ts

START = ts("2026-08-04T08:20:00+05:30")
D = lambda hm: ts(f"2026-08-04T{hm}:00+05:30")

# (id, language, title, mirrors case, driver, [(time, text in language, same text in English)])
CONVERSATIONS = [
    ("en-delay", "en", "Late by an hour, then books", "C03", "DRV013", [
        ("09:19", "I am late by one hour.", "I am late by one hour."),
        ("09:21", "1", "1")]),
    ("en-later", "en", "Asks for a slot after 12, books", "C07", "DRV006", [
        ("09:34", "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?",
         "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?"),
        ("09:35", "1", "1")]),
    ("hl-vague", "hi-Latn", "Vague delay: the bot asks for a time first", "C09", "DRV010", [
        ("09:42", "thoda late hoon bhai, traffic hai", "I am a bit late, lots of traffic"),
        ("09:43", "11:30 tak pahunch jaunga", "I will reach by 11:30")]),
    ("hl-safety", "hi-Latn", "Accident: scheduling stops, shift manager paged", "C12", "DRV011", [
        ("10:05", "bhai accident ho gaya, helper ko chot lagi hai, truck side mein laga diya",
         "accident happened, helper is hurt, truck parked on the side")]),
    ("hi-delay", "hi", "Late by an hour, then books", "C03", "DRV013", [
        ("09:19", "मैं एक घंटा लेट हूँ।", "I am late by one hour."),
        ("09:21", "1", "1")]),
    ("hi-later", "hi", "Asks for a slot after 12, books", "C07", "DRV006", [
        ("09:34", "शाहपुरा के बाद ट्रैफिक है। लगभग 11:20 तक पहुँचूँगा। 12 बजे के बाद कोई स्लॉट है?",
         "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?"),
        ("09:35", "पहला", "1")]),
    ("gu-delay", "gu", "Late by an hour, then books", "C03", "DRV013", [
        ("09:19", "હું એક કલાક મોડો છું.", "I am late by one hour."),
        ("09:21", "1", "1")]),
    ("mr-later", "mr", "Asks for a slot after 12, compares, books", "C07", "DRV006", [
        ("09:34", "शहापूरनंतर ट्रॅफिक आहे. मी सुमारे 11:20 वाजता पोहोचेन. 12 नंतर कोणता स्लॉट आहे?",
         "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?"),
        ("09:35", "पहिला", "1")]),
    ("mr-status", "mr", "Asks if the 4 PM slot is still active", "C13", "DRV008", [
        ("10:12", "माझा 4 वाजताचा स्लॉट अजून चालू आहे का?", "Is my 4 PM slot still active?")]),
    ("pa-delay", "pa", "Tyre repaired, gives an arrival time, books", "C05", "DRV012", [
        ("09:29", "ਟਾਇਰ ਠੀਕ ਹੋ ਗਿਆ। ਮੈਂ 11:10 ਵਜੇ ਤੱਕ ਪਹੁੰਚ ਜਾਵਾਂਗਾ।", "Tyre repaired. I can reach 11:10."),
        ("09:30", "ਪਹਿਲਾ", "1")]),
    ("mwr-vague", "mwr", "Vague delay: the bot asks for a time first", "C09", "DRV010", [
        ("09:42", "भाई म्हैं थोड़ो मोड़ो हूं, ट्रैफिक घणो है", "I am a bit late, lots of traffic"),
        ("09:43", "11:30 बज्यां तांई पूगसूं", "I will reach by 11:30")]),
    ("gu-safety", "gu", "Accident: scheduling stops, shift manager paged", "C12", "DRV011", [
        ("10:05", "અકસ્માત થયો છે, હેલ્પરને ઈજા થઈ છે, ટ્રક બાજુમાં ઊભો રાખ્યો છે", "accident happened, helper is hurt, truck parked on the side")]),
    ("pa-safety", "pa", "Accident in Punjabi", "C12", "DRV011", [
        ("10:05", "ਭਾਜੀ ਹਾਦਸਾ ਹੋ ਗਿਆ, ਹੈਲਪਰ ਨੂੰ ਸੱਟ ਲੱਗੀ ਹੈ", "accident happened, the helper is hurt")]),
    ("mwr-later", "mwr", "Marwari: arrival time with 'after 12'", "C07", "DRV006", [
        ("09:34", "शाहपुरा पछै ट्रैफिक घणो है। लगभग 11:20 बज्यां पूगसूं। 12 बज्यां पछै कोई स्लॉट है कांई?",
         "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?"),
        ("09:35", "दूजो", "2")]),
]


def run(drv, turns, use_lang):
    d = tempfile.mkdtemp(prefix="sh-lang-")
    try:
        con = db.connect(db.fresh_copy(os.path.join(d, "w.db")))
        world.rewind(con, START)
        phone = db.one(con, "SELECT phone FROM drivers WHERE driver_id=?", drv)["phone"]
        name = db.one(con, "SELECT driver_name FROM drivers WHERE driver_id=?", drv)["driver_name"]
        out = []
        for i, (hm, text, en) in enumerate(turns):
            r = W.handle(con, phone, text if use_lang else en, D(hm), f"lang-{drv}-{i}")
            out.append({"t": hm, "text": text if use_lang else en, "reply": r.get("reply"), "state": r.get("state"),
                        "lang": r.get("lang"), "tools": r.get("tools")})
        con.close()
        return name, out
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "out")
    os.makedirs(out_dir, exist_ok=True)
    convs = []
    for cid, lang, title, case, drv, turns in CONVERSATIONS:
        name, native = run(drv, turns, True)
        _, english = run(drv, turns, False)
        convs.append({"id": cid, "lang": lang, "language": L.LANGS[lang], "native": L.NATIVE[lang], "title": title,
                      "mirrors": case, "driver": drv, "name": name,
                      "turns": [dict(n, text_en=e["text"], reply_en=e["reply"], same_outcome=n["state"] == e["state"])
                                for n, e in zip(native, english)]})
    data = {"generated_from": "scripts/language_demo.py on the 4 August 2026 snapshot", "conversations": convs}
    with open(os.path.join(out_dir, "language_demo.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    site = os.path.join(os.path.dirname(HERE), "lang_data.js")
    if os.path.exists(os.path.join(os.path.dirname(HERE), "driver.html")):
        with open(site, "w", encoding="utf-8") as f:
            f.write("window.LANGDEMO=" + json.dumps(data, ensure_ascii=False) + ";\n")
    for c in convs:
        flags = ", ".join(f"{t['state']}{'' if t['same_outcome'] else ' (differs from English)'}" for t in c["turns"])
        print(f"{c['id']:<10} {c['language']:<11} detected={c['turns'][0]['lang']:<7} {flags}")


if __name__ == "__main__":
    main()
