"""Driver languages: what we can read and what we reply in.

Core activity: one place that lists every language, its script, and the words the
rules need. nlu.py reads this table; replies.py holds the reply text per language.

    code     language            script         replies
    en       English             Latin          own
    hi-Latn  Hinglish            Latin          own
    hi       Hindi               Devanagari     own
    gu       Gujarati            Gujarati       own (rest fall back to Hindi)
    mr       Marathi             Devanagari     own (rest fall back to Hindi)
    pa       Punjabi             Gurmukhi       own (rest fall back to Hindi)
    mwr      Marwari             Devanagari     own (rest fall back to Hindi)

How a language is recognised (no model call):
  * Gujarati and Gurmukhi have their own Unicode blocks, so the script decides.
  * Marathi and Marwari are written in Devanagari, like Hindi, so marker words decide
    (e.g. Marathi "आहे", "उशीर", "वाजता"; Marwari "कोनी", "मोड़ो", "म्हारो", "बज्यां").
    No markers: the thread keeps its earlier language, else Hindi.
  * Romanised Gujarati, Marathi, Punjabi or Marwari is read as Hinglish (replies in Hinglish).

The new reply texts are drafts and must be reviewed by a native speaker before go-live.
Marwari varies from district to district; these drafts follow Marwari as written around Jaipur and Jodhpur.
"""

LANGS = {
    "en": "English", "hi-Latn": "Hinglish", "hi": "Hindi",
    "gu": "Gujarati", "mr": "Marathi", "pa": "Punjabi", "mwr": "Marwari",
}
NATIVE = {"en": "English", "hi-Latn": "Hinglish", "hi": "हिन्दी", "gu": "ગુજરાતી", "mr": "मराठी", "pa": "ਪੰਜਾਬੀ", "mwr": "मारवाड़ी"}
FALLBACK = {"gu": "hi", "mr": "hi", "pa": "hi", "mwr": "hi", "hi": "en", "hi-Latn": "en"}

SCRIPTS = {                       # Unicode blocks
    "deva": ("ऀ", "ॿ"),
    "gujr": ("઀", "૿"),
    "guru": ("਀", "੿"),
}
DIGITS = str.maketrans("०१२३४५६७८९૦૧૨૩૪૫૬૭૮૯੦੧੨੩੪੫੬੭੮੯", "012345678901234567890123456789")

# Devanagari marker words that tell Marathi and Marwari apart from Hindi.
MARKERS = {
    "mr": set("""आहे आहेत आहोत उशीर उशिरा वाजता वाजेपर्यंत मी माझा माझी माझे माझ्या नाही नको येईन पोहोचेन पोहोचतो
पोहोचते अपघात जखमी तास झाला झाली झाले होईल किती तुम्ही आणि आता सकाळी संध्याकाळी गाडी नंतर पहिला दुसरा तिसरा""".split()),
    "mwr": set("""म्हारो म्हारी म्हारा म्हारै म्हैं थारो थारी थांको थांकी कोनी छूं छो छै पूगसूं पूगूं पूगूंला पूगस्यूं
आसूं मोड़ो मोड़ा घणो घणी कांई कठे होग्यो होगी होसी लागसी बज्यां घंटो पछै अबार पैलो दूजो तीजो""".split()),
}

# Extra words the reading rules use, per language (all are matched on the lower-cased text).
TIME_WORDS = r"baje|bje|बजे|बज्यां|बज्या|वाजता|वाजेपर्यंत|वाजे|વાગ્યે|વાગે|વાગ્યા|ਵਜੇ"
AFTER_WORDS = r"ke baad|ke bad|के बाद|बाद|नंतर|પછી|ਤੋਂ ਬਾਅਦ|ਬਾਅਦ|पछै"
APPROX_WORDS = r"around|about|approx|approximately|lagbhag|~|tak|लगभग|सुमारे|આશરે|લગભગ|ਲਗਭਗ"
APPROX_AFTER = r"सुमारे|આશરે|ਲਗਭਗ"   # new languages only: "11:20 ਲਗਭਗ". The words for "by" (ਤੱਕ, तांई, पर्यंत,
                                      # સુધી) are NOT here: "11:30 ਤੱਕ" is a firm "by 11:30", like English "by" and Hinglish "tak"
DUR_WORDS = [                     # (pattern, minutes), most specific first
    (r"dedh ghant[ae]|डेढ़ घंट[ाेो]|दीड तास|દોઢ કલાક|ਡੇਢ ਘੰਟ[ਾੇ]", 90),
    (r"aadha ghanta|आधा घंटा|आधो घंटो|अर्धा तास|અડધો કલાક|અડધા કલાક|ਅੱਧਾ ਘੰਟਾ", 30),
    (r"do ghante|दो घंटे|दो घंटा|दोन तास|બે કલાક|ਦੋ ਘੰਟੇ", 120),
    (r"dhai ghante|ढाई घंटे|अडीच तास|અઢી કલાક|ਢਾਈ ਘੰਟੇ", 150),
    (r"ek ghanta|एक घंटा|एक घंटो|एक तास|એક કલાક|ਇੱਕ ਘੰਟਾ|ਇਕ ਘੰਟਾ", 60),
]
HOUR_UNITS = r"hours?|hrs?|hr|ghante|ghanta|घंटे|घंटा|घंटो|तास|કલાક|ਘੰਟੇ|ਘੰਟਾ"
MIN_UNITS = r"minutes?|mins?|min|मिनट|मिनिटे|मिनिटं|मिनिट|મિનિટ|ਮਿੰਟ"
LATE_WORDS = r"लेट|देर|उशीर|उशिरा|मोड़ो|मोड़ा|મોડું|મોડા|મોડો|લેટ|ਲੇਟ|ਦੇਰ|ਦੇਰੀ|ट्रॅफिक|ટ્રાફિક|ਟ੍ਰੈਫਿਕ|पंक्चर|પંક્ચર|ਪੰਕਚਰ"
ARRIVE_WORDS = r"पोहोच|पूग|પહોંચ|ਪਹੁੰਚ"          # Hindi/Hinglish forms stay in nlu.py as before
EVENING = r"\bevening\b|\bshaam\b|\bsham\b|शाम|सांझ|संध्याकाळ|સાંજ|ਸ਼ਾਮ"
MORNING = r"\bmorning\b|\bsubah\b|सुबह|सकाळ|दिनूगै|સવાર|ਸਵੇਰ"
HUMAN = r"talk to (a )?(person|human|someone|manager)|call me|insaan se|manager se baat|माणसाशी बोल|मॅनेजरशी बोल|માણસ સાથે વાત|મેનેજર સાથે વાત|ਬੰਦੇ ਨਾਲ ਗੱਲ|ਮੈਨੇਜਰ ਨਾਲ ਗੱਲ|मिनख सूं बात"
# Intent words in native scripts (Hindi, Gujarati, Marathi, Punjabi, Marwari). nlu.py adds each to the matching
# English / romanised rule, so the same request reads the same in every language. Matched on the lower-cased text.
EARLY = (r"जल्दी आ गया|जल्दी पहुँच गया|पहले पहुँच गया|वहेलो आवी|વહેલો આવ|વહેલા આવ|વહેલો પહોંચ|लवकर आलो|लवकर आले|"
         r"लवकर पोहोचलो|ਜਲਦੀ ਆ ਗਿਆ|ਜਲਦੀ ਪਹੁੰਚ ਗਿਆ|जल्दी आग्यो|जल्दी पूग्यो|वेगो आग्यो|वेगो पूग्यो")
YARD = r"यार्ड|યાર્ડ|ਯਾਰਡ"
HEAVY = r"ट्रेलर|ટ્રેલર|ਟ੍ਰੇਲਰ|ਟਰੇਲਰ|हैवी|હેવી|ਹੈਵੀ"          # not भारी/ભારે alone: "भारी ट्रैफिक" is heavy traffic
REEFER = r"रीफर|રીફર|ਰੀਫਰ"
URGENT = r"अर्जेंट|अर्जंट|અર્જન્ટ|ਅਰਜੈਂਟ|तुरंत चाहिए"
LEAVE_AFTER = r"निकल|નીકળ|निघ|ਨਿਕਲ|निकळ"                     # "1:30 बजे तक निकलना है" -> latest gate-out
BY_WORDS = r"तक|સુધીમાં|સુધી|पर्यंत|ਤੱਕ|तांई"
NEXT_LOAD = r"दूसरा लोड|दूसरी पिकअप|બીજો લોડ|दुसरा लोड|ਦੂਜਾ ਲੋਡ|दूजो लोड"
ASK_OPTIONS = (r"कौन से स्लॉट|कौन सा स्लॉट|કયા સ્લોટ|કયો સ્લોટ|कोणते स्लॉट|कोणता स्लॉट|ਕਿਹੜੇ ਸਲਾਟ|ਕਿਹੜਾ ਸਲਾਟ|"
               r"कुणसा स्लॉट|कुणसो स्लॉट|कुणसी स्लॉट")
AND_AFTER = r"^\s*(?:aur|और|અને|आणि|ਤੇ|ਅਤੇ|अर)\s"                 # "और 3 बजे के बाद?" = "what about after 3?"
DECLINE = (r"apna slot (?:hi )?rakh|स्लॉट ही रख|अपना स्लॉट रख|नहीं चाहिए|સ્લોટ જ રાખ|મારો સ્લોટ રાખ|નથી જોઈતું|"
           r"स्लॉटच ठेव|स्लॉट ठेव|नको आहे|ਸਲਾਟ ਹੀ ਰੱਖ|ਆਪਣਾ ਸਲਾਟ ਰੱਖ|ਨਹੀਂ ਚਾਹੀਦਾ|स्लॉट ही राख|म्हारो स्लॉट राख|कोनी चाइजै")
SLOT_REF = (r"^\s*(?:" + TIME_WORDS + r")?\S{0,4}\s*(?:वाला|वाली|वाळो|ਵਾਲਾ|ਵਾਲੀ|વાળો|વાળી)?\s*(?:स्लॉट|સ્લોટ|ਸਲਾਟ)")
STATUS = r"चालू आहे|ચાલુ છે|ਚਾਲੂ ਹੈ|चालू है"
ORDINALS = {                      # whole tokens only
    "पहला": 1, "पहली": 1, "पहिला": 1, "पहिली": 1, "पहिले": 1, "પહેલો": 1, "પહેલું": 1, "પહેલી": 1, "ਪਹਿਲਾ": 1, "ਪਹਿਲੀ": 1, "पैलो": 1, "पैली": 1, "पहलो": 1,
    "दूसरा": 2, "दूसरी": 2, "दुसरा": 2, "दुसरी": 2, "दुसरे": 2, "બીજો": 2, "બીજું": 2, "બીજી": 2, "ਦੂਜਾ": 2, "ਦੂਜੀ": 2, "दूजो": 2, "दूजी": 2,
    "तीसरा": 3, "तीसरी": 3, "तिसरा": 3, "तिसरी": 3, "तिसरे": 3, "ત્રીજો": 3, "ત્રીજું": 3, "ત્રીજી": 3, "ਤੀਜਾ": 3, "ਤੀਜੀ": 3, "तीजो": 3, "तीजी": 3,
}
YES = {"हो", "ठीक", "बरं", "हां", "હા", "બરાબર", "ઠીક", "ਹਾਂ", "ਠੀਕ"}
NO = {"नको", "ना", "ના", "ਨਹੀਂ", "कोनी"}
OR_WORD = {"hi-Latn": " ya ", "hi": " या ", "gu": " અથવા ", "mr": " किंवा ", "pa": " ਜਾਂ ", "mwr": " या "}
SAFETY_WORDS = ["घायल", "एक्सीडेंट", "अपघात", "जखमी", "आग लागली", "पोलीस", "दवाखान",
                "અકસ્માત", "ઈજા", "ઇજા", "આગ લાગી", "પોલીસ", "હોસ્પિટલ",
                "ਹਾਦਸਾ", "ਐਕਸੀਡੈਂਟ", "ਸੱਟ", "ਜ਼ਖਮੀ", "ਜ਼ਖ਼ਮੀ", "ਅੱਗ ਲੱਗ", "ਪੁਲਿਸ", "ਹਸਪਤਾਲ"]
# Every safety word must start a word (nlu.py). These must also END one, because longer everyday words begin with
# them: "आग" (fire) vs "आगे" (ahead) and Marwari "आग्यो" (I have come); "chot" (injury) vs "choti" (small);
# "fire" vs "fired". The others may take endings ("injured", "ਸੱਟਾਂ", "दवाखान्यात").
SAFETY_WHOLE_WORDS = {"आग", "chot", "fire"}


def script_share(text):
    letters = [c for c in text if c.isalpha() or "ऀ" <= c <= "૿"]
    if not letters:
        return {}, 0
    share = {k: sum(lo <= c <= hi for c in letters) / len(letters) for k, (lo, hi) in SCRIPTS.items()}
    return share, len(letters)


def pick(table, lang):
    """table[lang], else its fallback chain (gu/mr/pa/mwr -> hi -> en)."""
    while lang not in table and lang in FALLBACK:
        lang = FALLBACK[lang]
    return table[lang] if lang in table else table["en"]
