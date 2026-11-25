"""Spoken clock times -> digits for voice transcripts ("साढ़े ग्यारह बजे" -> "11:30")."""
import re


# ---- spoken clock times -> digits (voice transcripts only; typed text never passes through here)
# Speech models often write "साढ़े ग्यारह बजे" or "eleven thirty" where a typist writes 11:30. Only a number
# followed by a clock word (बजे, baje, વાગ્યે, वाजता, ਵਜੇ, o'clock) or an English clock phrase is changed,
# so durations ("एक घंटा", "साढ़े तीन घंटे") stay as they are and the reader still sees them as durations.
_NUM = {}
for _n, _ws in enumerate([
        "one ek एक એક ਇੱਕ ਇਕ", "two do दो दोन બે ਦੋ", "three teen tin तीन ત્રણ ਤਿੰਨ", "four char chaar चार ચાર ਚਾਰ",
        "five paanch panch पाँच पांच पाच પાંચ ਪੰਜ", "six chhe che chah छह छः छे सहा છ ਛੇ", "seven saat सात સાત ਸੱਤ",
        "eight aath आठ આઠ ਅੱਠ", "nine nau नौ नऊ નવ ਨੌਂ ਨੌ", "ten das दस दहा દસ ਦਸ",
        "eleven gyarah gyara gyaarah ग्यारह अकरा इग्यारा અગિયાર ਗਿਆਰਾਂ ਗਿਆਰਾਂ ਗਿਆਰਾ",
        "twelve barah baarah bara बारह बारा બાર ਬਾਰਾਂ ਬਾਰਾ"], 1):
    for _w in _ws.split():
        _NUM[_w] = _n
_PLUS = {**{w: 30 for w in "saade sade saadhe sadhe साढ़े साढे साडे સાડા ਸਾਢੇ".split()},
         **{w: 15 for w in "sava savva सवा सव्वा સવા ਸਵਾ".split()},
         **{w: -15 for w in "paune pone पौने पावणे પોણા ਪੌਣੇ".split()}}
_HALF = {**{w: (1, 30) for w in "dedh derh डेढ़ डेढ दीड દોઢ ਡੇਢ".split()},
         **{w: (2, 30) for w in "dhai dhaai ढाई अडीच અઢી ਢਾਈ".split()}}
_CLOCK = r"(?:baje|bajey|बजे|बज्यां|वाजता|वाजेपर्यंत|वाजे|વાગ્યે|વાગે|ਵਜੇ|o'?\s?clock)"
_L = "a-z\u0900-\u0DFF"
_alt = lambda ws: "|".join(sorted(map(re.escape, ws), key=len, reverse=True))
_NUM_RE = _alt(_NUM)
_INDIC_TIME = re.compile(rf"(?<![{_L}])(?:(?P<mod>{_alt(_PLUS)})\s+)?(?P<num>{_NUM_RE}|\d{{1,2}})\s+{_CLOCK}(?![{_L}])", re.I)
_HALF_TIME = re.compile(rf"(?<![{_L}])(?P<half>{_alt(_HALF)})\s+{_CLOCK}(?![{_L}])", re.I)
_EN_PAST = re.compile(rf"\b(?P<q>half past|quarter past|quarter to)\s+(?P<num>{_NUM_RE}|\d{{1,2}})\b(?:\s+o'?\s?clock)?", re.I)
_EN_PAIR = re.compile(rf"\b(?P<num>{_NUM_RE})[\s-]+(?P<min>thirty|fifteen|forty[\s-]five)\b(?!\s+(?:min|minutes|mins|hours?))", re.I)


def _hm(h, m):
    h, m = (h - 1, m + 60) if m < 0 else (h, m)
    return f"{(h - 1) % 12 + 1}:{m:02d}"


def _n(s):
    return int(s) if s.isdigit() else _NUM[s.lower() if s.isascii() else s]


def spoken_times(text):
    """'साढ़े ग्यारह बजे तक' -> '11:30 तक'; 'reaching by eleven thirty' -> 'reaching by 11:30'."""
    t = _HALF_TIME.sub(lambda m: _hm(*_HALF[m["half"].lower() if m["half"].isascii() else m["half"]]), text or "")
    t = _INDIC_TIME.sub(lambda m: _hm(_n(m["num"]), _PLUS.get((m["mod"] or "").lower() if (m["mod"] or "").isascii() else m["mod"], 0)), t)
    t = _EN_PAST.sub(lambda m: _hm(_n(m["num"]),
                                   {"half past": 30, "quarter past": 15, "quarter to": -15}[m["q"].lower()]), t)
    t = _EN_PAIR.sub(lambda m: _hm(_n(m["num"]), {"thirty": 30, "fifteen": 15}.get(m["min"].lower(), 45)), t)
    return t
