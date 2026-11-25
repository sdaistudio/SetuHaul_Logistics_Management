"""Language replay: the whole 4 August morning (run_scenarios.py, unchanged) with every driver message translated.

workflow.handle is wrapped so each driver message is swapped for its translation as it enters the engine. World
events, the 09:36 race and the coordinator approvals run exactly as in the English run, and every reply is the
engine's own (rules only, no model call). Only the driver messages below are hand-written: drafts for review by a
native speaker. Slot picks stay digits, as WhatsApp buttons send them.

Run:    python3 -I scripts/language_replay.py <lang> <out_dir>      (lang: hi-Latn hi gu mr pa mwr)
Writes: <out_dir>/scenario_run.json (as run_scenarios.py) and <out_dir>/sent.json ({ext: [sent text, English]}).
Test:   tests/test_language_replay.py checks every turn ends in the same state as the English run.
"""
import json, os, runpy, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from setuhaul import workflow as W   # noqa: E402

LANGS = ("hi-Latn", "hi", "gu", "mr", "pa", "mwr")

T = {  # English (as run_scenarios.py sends it) -> translation, per language. Draft wording for native review.
 "I am already at the gate. Can I get a dock now instead of waiting for 9?": {
   "hi-Latn": "Main gate pe pahunch gaya hoon, jaldi aa gaya. Abhi dock mil sakta hai kya?",
   "hi": "मैं जल्दी आ गया हूँ, गेट पर खड़ा हूँ। अभी डॉक मिल सकता है क्या?",
   "gu": "હું વહેલો આવી ગયો છું, ગેટ પર ઊભો છું. અત્યારે ડૉક મળી શકે?",
   "mr": "मी लवकर आलो आहे, गेटवर उभा आहे. आता डॉक मिळेल का?",
   "pa": "ਮੈਂ ਜਲਦੀ ਆ ਗਿਆ ਹਾਂ, ਗੇਟ ਤੇ ਖੜ੍ਹਾ ਹਾਂ। ਹੁਣੇ ਡੌਕ ਮਿਲ ਸਕਦਾ ਹੈ?",
   "mwr": "म्हैं जल्दी आग्यो, गेट माथै ऊभो हूं। अबार डॉक मिल सकै कांई?"},
 "I am late by one hour.": {
   "hi-Latn": "Main ek ghanta late hoon.",
   "hi": "मैं एक घंटा लेट हूँ।",
   "gu": "હું એક કલાક મોડો છું.",
   "mr": "मला एक तास उशीर झाला आहे.",
   "pa": "ਮੈਂ ਇੱਕ ਘੰਟਾ ਲੇਟ ਹਾਂ।",
   "mwr": "म्हैं एक घंटो मोड़ो हूं।"},
 "I reached late and am in the yard. Has a new dock been assigned?": {
   "hi-Latn": "Main late pahuncha aur yard mein hoon. Naya dock mila kya?",
   "hi": "मैं लेट पहुँचा हूँ और यार्ड में खड़ा हूँ। नया डॉक मिला क्या?",
   "gu": "હું મોડો પહોંચ્યો અને યાર્ડમાં છું. નવો ડૉક મળ્યો?",
   "mr": "मी उशिरा पोहोचलो आणि यार्डमध्ये आहे. नवीन डॉक मिळाला का?",
   "pa": "ਮੈਂ ਲੇਟ ਪਹੁੰਚਿਆ ਅਤੇ ਯਾਰਡ ਵਿੱਚ ਹਾਂ। ਨਵਾਂ ਡੌਕ ਮਿਲਿਆ?",
   "mwr": "म्हैं मोड़ो पूग्यो अर यार्ड में ऊभो हूं। नयो डॉक मिल्यो कांई?"},
 "Tyre repaired. I can reach 11:10. I need to leave the warehouse before 1:30 for another pickup.": {
   "hi-Latn": "Tyre theek ho gaya. 11:10 baje pahunch jaunga. Mujhe 1:30 tak nikalna hai, doosra load uthana hai.",
   "hi": "टायर ठीक हो गया। 11:10 बजे पहुँच जाऊँगा। मुझे 1:30 बजे तक निकलना है, दूसरा लोड उठाना है।",
   "gu": "ટાયર રિપેર થઈ ગયું. 11:10 વાગ્યે પહોંચીશ. મારે 1:30 વાગ્યા સુધીમાં નીકળવું છે, બીજો લોડ લેવાનો છે.",
   "mr": "टायर दुरुस्त झाला. मी 11:10 वाजता पोहोचेन. मला 1:30 वाजेपर्यंत निघायचे आहे, दुसरा लोड घ्यायचा आहे.",
   "pa": "ਟਾਇਰ ਠੀਕ ਹੋ ਗਿਆ। ਮੈਂ 11:10 ਵਜੇ ਪਹੁੰਚ ਜਾਵਾਂਗਾ। ਮੈਂ 1:30 ਵਜੇ ਤੱਕ ਨਿਕਲਣਾ ਹੈ, ਦੂਜਾ ਲੋਡ ਚੁੱਕਣਾ ਹੈ।",
   "mwr": "टायर ठीक होग्यो। म्हैं 11:10 बज्यां पूगसूं। म्हनै 1:30 बज्यां तांई निकळणो है, दूजो लोड उठाणो है।"},
 "I will be late by 45 minutes.": {
   "hi-Latn": "Main 45 minute late ho jaunga.",
   "hi": "मैं 45 मिनट लेट हो जाऊँगा।",
   "gu": "હું 45 મિનિટ મોડો પડીશ.",
   "mr": "मला 45 मिनिटे उशीर होईल.",
   "pa": "ਮੈਂ 45 ਮਿੰਟ ਲੇਟ ਹੋਵਾਂਗਾ।",
   "mwr": "म्हैं 45 मिनट मोड़ो होसूं।"},
 "शाम वाला": {
   "hi-Latn": "shaam wala", "hi": "शाम वाला", "gu": "સાંજ વાળો", "mr": "संध्याकाळचा", "pa": "ਸ਼ਾਮ ਵਾਲਾ", "mwr": "सांझ वाळो"},
 "Traffic after Shahpura. Reaching around 11:20. Any slot after 12?": {
   "hi-Latn": "Shahpura ke baad traffic hai. Lagbhag 11:20 tak pahunchunga. 12 baje ke baad koi slot hai?",
   "hi": "शाहपुरा के बाद ट्रैफिक है। लगभग 11:20 तक पहुँचूँगा। 12 बजे के बाद कोई स्लॉट है?",
   "gu": "શાહપુરા પછી ટ્રાફિક છે. આશરે 11:20 વાગ્યે પહોંચીશ. 12 વાગ્યા પછી કોઈ સ્લોટ છે?",
   "mr": "शहापूरनंतर ट्रॅफिक आहे. मी सुमारे 11:20 वाजता पोहोचेन. 12 नंतर कोणता स्लॉट आहे?",
   "pa": "ਸ਼ਾਹਪੁਰਾ ਤੋਂ ਬਾਅਦ ਟ੍ਰੈਫਿਕ ਹੈ। ਲਗਭਗ 11:20 ਵਜੇ ਪਹੁੰਚਾਂਗਾ। 12 ਵਜੇ ਤੋਂ ਬਾਅਦ ਕੋਈ ਸਲਾਟ ਹੈ?",
   "mwr": "शाहपुरा पछै ट्रैफिक घणो है। लगभग 11:20 बज्यां पूगसूं। 12 बज्यां पछै कोई स्लॉट है कांई?"},
 "Heavy trailer ETA 12:00. Original heavy-bay slot will be missed. What is next?": {
   "hi-Latn": "Heavy trailer ka ETA 12:00 hai. Heavy-bay wala slot chhoot jayega. Ab kya karein?",
   "hi": "भारी ट्रेलर है, 12 बजे पहुँचूँगा। हैवी-बे वाला स्लॉट छूट जाएगा। अब क्या करूँ?",
   "gu": "ભારે ટ્રેલર છે, 12 વાગ્યે પહોંચીશ. હેવી-બે વાળો સ્લોટ છૂટી જશે. હવે શું કરું?",
   "mr": "जड ट्रेलर आहे, मी 12 वाजता पोहोचेन. हेवी-बेचा स्लॉट चुकेल. आता काय करू?",
   "pa": "ਭਾਰੀ ਟ੍ਰੇਲਰ ਹੈ, 12 ਵਜੇ ਪਹੁੰਚਾਂਗਾ। ਹੈਵੀ-ਬੇ ਵਾਲਾ ਸਲਾਟ ਛੁੱਟ ਜਾਵੇਗਾ। ਹੁਣ ਕੀ ਕਰਾਂ?",
   "mwr": "भारी ट्रेलर है, 12 बज्यां पूगसूं। हैवी-बे वाळो स्लॉट छूट जासी। अबै कांई करूं?"},
 "thoda late hoon bhai, traffic hai": {
   "hi-Latn": "thoda late hoon bhai, traffic hai",
   "hi": "भाई थोड़ा लेट हूँ, ट्रैफिक है",
   "gu": "ભાઈ થોડો મોડો છું, ટ્રાફિક છે",
   "mr": "भाऊ थोडा उशीर झाला आहे, ट्रॅफिक आहे",
   "pa": "ਭਾਜੀ ਥੋੜ੍ਹਾ ਲੇਟ ਹਾਂ, ਟ੍ਰੈਫਿਕ ਹੈ",
   "mwr": "भाई म्हैं थोड़ो मोड़ो हूं, ट्रैफिक घणो है"},
 "11:30 tak pahunch jaunga": {
   "hi-Latn": "11:30 tak pahunch jaunga",
   "hi": "11:30 तक पहुँच जाऊँगा",
   "gu": "11:30 સુધી પહોંચી જઈશ",
   "mr": "11:30 पर्यंत पोहोचेन",
   "pa": "11:30 ਤੱਕ ਪਹੁੰਚ ਜਾਵਾਂਗਾ",
   "mwr": "11:30 बज्यां तांई पूगसूं"},
 "Origin released me late. ETA 11:25. This load is urgent. Can I get the first possible dock?": {
   "hi-Latn": "Origin pe loading late hui. ETA 11:25. Load urgent hai, pehla khali dock mil sakta hai?",
   "hi": "माल लोड होने में देर हुई। 11:25 बजे पहुँचूँगा। लोड अर्जेंट है, सबसे पहला खाली डॉक मिल सकता है?",
   "gu": "માલ લોડ થવામાં મોડું થયું. 11:25 વાગ્યે પહોંચીશ. લોડ અર્જન્ટ છે, પહેલો ખાલી ડૉક મળશે?",
   "mr": "माल लोड व्हायला उशीर झाला. मी 11:25 वाजता पोहोचेन. लोड अर्जंट आहे, पहिला रिकामा डॉक मिळेल का?",
   "pa": "ਮਾਲ ਲੋਡ ਹੋਣ ਵਿੱਚ ਦੇਰ ਹੋਈ। 11:25 ਵਜੇ ਪਹੁੰਚਾਂਗਾ। ਲੋਡ ਅਰਜੈਂਟ ਹੈ, ਪਹਿਲਾ ਖਾਲੀ ਡੌਕ ਮਿਲੇਗਾ?",
   "mwr": "माल लोड होवण में देर होगी। 11:25 बज्यां पूगसूं। लोड अर्जेंट है, पैलो खाली डॉक मिल सकै कांई?"},
 "What slots are possible after 1 PM if I reach around 12:45?": {
   "hi-Latn": "Lagbhag 12:45 pahunchunga. 1 baje ke baad kaun sa slot mil sakta hai?",
   "hi": "लगभग 12:45 पहुँचूँगा। 1 बजे के बाद कौन से स्लॉट मिल सकते हैं?",
   "gu": "આશરે 12:45 વાગ્યે પહોંચીશ. 1 વાગ્યા પછી કયા સ્લોટ મળી શકે?",
   "mr": "मी सुमारे 12:45 वाजता पोहोचेन. 1 नंतर कोणते स्लॉट मिळू शकतात?",
   "pa": "ਲਗਭਗ 12:45 ਵਜੇ ਪਹੁੰਚਾਂਗਾ। 1 ਵਜੇ ਤੋਂ ਬਾਅਦ ਕਿਹੜੇ ਸਲਾਟ ਮਿਲ ਸਕਦੇ ਹਨ?",
   "mwr": "लगभग 12:45 बज्यां पूगसूं। 1 बज्यां पछै कुणसा स्लॉट मिल सकै?"},
 "what about after 3 PM?": {
   "hi-Latn": "aur 3 baje ke baad kaun sa slot hai?",
   "hi": "और 3 बजे के बाद?",
   "gu": "અને 3 વાગ્યા પછી?",
   "mr": "आणि 3 नंतर?",
   "pa": "ਤੇ 3 ਵਜੇ ਤੋਂ ਬਾਅਦ?",
   "mwr": "अर 3 बज्यां पछै?"},
 "ok no thanks, I will keep my slot": {
   "hi-Latn": "nahi chahiye, main apna slot hi rakhunga",
   "hi": "नहीं चाहिए, मैं अपना स्लॉट ही रखूँगा",
   "gu": "ના, મારો સ્લોટ જ રાખીશ",
   "mr": "नको, माझा स्लॉटच ठेवतो",
   "pa": "ਨਹੀਂ, ਮੈਂ ਆਪਣਾ ਸਲਾਟ ਹੀ ਰੱਖਾਂਗਾ",
   "mwr": "कोनी, म्हैं म्हारो स्लॉट ही राखसूं"},
 "bhai accident ho gaya, helper ko chot lagi hai, truck side mein laga diya": {
   "hi-Latn": "bhai accident ho gaya, helper ko chot lagi hai, truck side mein laga diya",
   "hi": "भाई एक्सीडेंट हो गया, हेल्पर घायल है, ट्रक साइड में लगा दिया",
   "gu": "અકસ્માત થયો છે, હેલ્પરને ઈજા થઈ છે, ટ્રક બાજુમાં ઊભો રાખ્યો છે",
   "mr": "अपघात झाला आहे, हेल्पर जखमी आहे, ट्रक बाजूला लावला आहे",
   "pa": "ਭਾਜੀ ਹਾਦਸਾ ਹੋ ਗਿਆ, ਹੈਲਪਰ ਨੂੰ ਸੱਟ ਲੱਗੀ ਹੈ, ਟਰੱਕ ਸਾਈਡ ਤੇ ਲਾ ਦਿੱਤਾ",
   "mwr": "भाई एक्सीडेंट होग्यो, हेल्पर घायल है, ट्रक साइड में लगा दियो"},
 "Is my 4 PM slot still active?": {
   "hi-Latn": "Mera 4 PM slot still active hai kya?",
   "hi": "मेरा 4 बजे वाला स्लॉट अभी भी चालू है?",
   "gu": "મારો 4 વાગ્યાનો સ્લોટ હજી ચાલુ છે?",
   "mr": "माझा 4 वाजताचा स्लॉट अजून चालू आहे का?",
   "pa": "ਮੇਰਾ 4 ਵਜੇ ਵਾਲਾ ਸਲਾਟ ਹਾਲੇ ਵੀ ਚਾਲੂ ਹੈ?",
   "mwr": "म्हारो 4 बज्यां वाळो स्लॉट अबार भी चालू है कांई?"},
 "Evening traffic. ETA 6:30. Can the reefer unload tonight?": {
   "hi-Latn": "Shaam ka traffic hai. ETA 6:30. Reefer aaj raat unload ho jayega?",
   "hi": "शाम का ट्रैफिक है। 6:30 बजे पहुँचूँगा। रीफर आज रात अनलोड हो जाएगा?",
   "gu": "સાંજનો ટ્રાફિક છે. 6:30 વાગ્યે પહોંચીશ. રીફર આજે રાત્રે અનલોડ થશે?",
   "mr": "संध्याकाळचे ट्रॅफिक आहे. मी 6:30 वाजता पोहोचेन. रीफर आज रात्री अनलोड होईल का?",
   "pa": "ਸ਼ਾਮ ਦਾ ਟ੍ਰੈਫਿਕ ਹੈ। 6:30 ਵਜੇ ਪਹੁੰਚਾਂਗਾ। ਰੀਫਰ ਅੱਜ ਰਾਤ ਅਨਲੋਡ ਹੋ ਜਾਵੇਗਾ?",
   "mwr": "सांझ रो ट्रैफिक है। 6:30 बज्यां पूगसूं। रीफर आज रात अनलोड होसी कांई?"},
}

if __name__ == "__main__":
    lang, out = sys.argv[1], sys.argv[2]
    sent = {}                                  # ext id -> (translated text, English original)
    real = W.handle

    def handle(con, phone, text, as_of, ext, *a, **kw):
        t = text.strip()
        if t.isdigit():
            new = text
        elif text in T:
            new = T[text][lang]
        else:
            raise KeyError(f"no translation for: {text!r}")
        sent[ext] = (new, text)
        return real(con, phone, new, as_of, ext, *a, **kw)

    W.handle = handle
    sys.argv = [os.path.join(REPO, "run_scenarios.py"), out]
    cwd = os.getcwd(); os.chdir(REPO)
    try:
        runpy.run_path(os.path.join(REPO, "run_scenarios.py"), run_name="__main__")
    finally:
        os.chdir(cwd)
    with open(os.path.join(out, "sent.json"), "w", encoding="utf-8") as f:
        json.dump(sent, f, ensure_ascii=False, indent=1)
