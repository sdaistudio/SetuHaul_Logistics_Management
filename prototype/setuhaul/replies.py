"""Reviewed reply catalogue in English, Hinglish (Latin script) and Hindi.

Every driver-facing sentence comes from here, filled with facts the tools
returned. guard() then checks the finished text: every dock code and clock
time must appear in the evidence, and 'booked' words only appear when the
commit actually returned CONFIRMED. If a future LLM rephrases replies, the
same guard runs on its output.
"""
import re

T = {
 "ask_shipment": {
  "en": "You have {n} trips today. Which one is this about?\n{choices}{suggest}",
  "hi-Latn": "Aaj aapke {n} trip hain. Kis trip ki baat hai?\n{choices}{suggest}",
  "hi": "आज आपकी {n} ट्रिप हैं। किस ट्रिप की बात है?\n{choices}{suggest}"},
 "ask_eta": {
  "en": "Thanks for telling us. What time do you expect to reach {facility}? A clock time like 11:30 lets me find a slot you can make.",
  "hi-Latn": "Batane ke liye shukriya. {facility} kitne baje tak pahunchoge? Jaise 11:30 likhiye, taaki sahi slot mil sake.",
  "hi": "बताने के लिए धन्यवाद। {facility} कितने बजे तक पहुँचेंगे? जैसे 11:30 लिखिए।"},
 "ask_limit": {
  "en": "Got it: {eta_text}. You mentioned another pickup: by what time must you leave our gate? I'll only offer slots that let you make it.",
  "hi-Latn": "Samajh gaya: {eta_text}. Aapne doosre pickup ki baat ki: hamare gate se kitne baje tak nikalna hai? Usi hisaab se slot dunga.",
  "hi": "समझ गया: {eta_text}। आपने दूसरे पिकअप की बात की: हमारे गेट से कितने बजे तक निकलना है? उसी हिसाब से स्लॉट दूँगा।"},
 "state_back": {
  "en": "I've noted your arrival as about {eta} ({basis}). If that's wrong, just send the time.\n",
  "hi-Latn": "Aapka pahunchne ka time lagbhag {eta} maana hai ({basis}). Galat ho to time bhej dijiye.\n",
  "hi": "आपके पहुँचने का समय लगभग {eta} माना है ({basis})। गलत हो तो समय भेज दीजिए।\n"},
 "urgency_note": {
  "en": "Dock order follows the priority on the shipment record, not the message, so I can't move you ahead for urgency. These are the earliest slots that fit.\n",
  "hi-Latn": "Dock ka kram shipment record ki priority se tay hota hai, message se nahi. Yeh sabse pehle fit hone wale slot hain.\n",
  "hi": "डॉक का क्रम शिपमेंट रिकॉर्ड की प्राथमिकता से तय होता है, संदेश से नहीं। ये सबसे पहले फिट होने वाले स्लॉट हैं।\n"},
 "options": {
  "en": "{preface}Slots your truck can make at {facility} (nothing is booked until you choose):\n{lines}\nReply {nums} to book.",
  "hi-Latn": "{preface}{facility} mein yeh slot aapke truck ke liye hain (aap chunenge tabhi book hoga):\n{lines}\nBook karne ke liye {nums} bhejiye.",
  "hi": "{preface}{facility} में ये स्लॉट आपके ट्रक के लिए हैं (आपके चुनने पर ही बुक होगा):\n{lines}\nबुक करने के लिए {nums} भेजिए।"},
 "ask_only_options": {
  "en": "Just options for now; nothing is booked or held. If you reach about {eta}, these slots at {facility} would work:\n{lines}\nReply {nums} if you want one booked.",
  "hi-Latn": "Abhi sirf options hain; kuch book ya hold nahi hua. Agar aap lagbhag {eta} tak pahunchte hain to {facility} mein yeh slot chalenge:\n{lines}\nBook karna ho to {nums} bhejiye.",
  "hi": "अभी सिर्फ़ विकल्प हैं; कुछ बुक या होल्ड नहीं हुआ। अगर आप लगभग {eta} तक पहुँचते हैं तो {facility} में ये स्लॉट चलेंगे:\n{lines}\nबुक करना हो तो {nums} भेजिए।"},
 "line": {
  "en": "{n}) Dock {dock} - start {start}, unloading done by about {end}{note}",
  "hi-Latn": "{n}) Dock {dock} - {start} se shuru, lagbhag {end} tak khaali{note}",
  "hi": "{n}) डॉक {dock} - {start} से शुरू, लगभग {end} तक खाली{note}"},
 "approval_note": {"en": " (needs coordinator OK)", "hi-Latn": " (coordinator ki manzoori chahiye)", "hi": " (कोऑर्डिनेटर की मंज़ूरी चाहिए)"},
 "booked": {
  "en": "Booked: Dock {dock} at {facility}, start {start}. Please be at the gate by {gate_by}. Booking ref {ref}.",
  "hi-Latn": "Book ho gaya: {facility}, Dock {dock}, {start} se. Gate par {gate_by} tak pahunchiye. Ref {ref}.",
  "hi": "बुक हो गया: {facility}, डॉक {dock}, {start} से। गेट पर {gate_by} तक पहुँचिए। रेफ़ {ref}।"},
 "booked_here": {
  "en": "Booked: Dock {dock} at {facility}, start {start}. You're already checked in; the yard team will call you to Dock {dock}. Booking ref {ref}.",
  "hi-Latn": "Book ho gaya: {facility}, Dock {dock}, {start} se. Aap gate par aa chuke hain; yard team aapko Dock {dock} par bulayegi. Ref {ref}.",
  "hi": "बुक हो गया: {facility}, डॉक {dock}, {start} से। आप गेट पर आ चुके हैं; यार्ड टीम आपको डॉक {dock} पर बुलाएगी। रेफ़ {ref}।"},
 "approval_granted": {
  "en": "Confirmed: the coordinator approved Dock {dock} at {facility}, start {start}. Please be at the gate by {gate_by}. Booking ref {ref}.",
  "hi-Latn": "Confirm ho gaya: coordinator ne {facility}, Dock {dock}, {start} manzoor kar diya. Gate par {gate_by} tak pahunchiye. Ref {ref}.",
  "hi": "पक्का हो गया: कोऑर्डिनेटर ने {facility}, डॉक {dock}, {start} मंज़ूर कर दिया। गेट पर {gate_by} तक पहुँचिए। रेफ़ {ref}।"},
 "approval_failed": {
  "en": "The coordinator's approval could not be applied: Dock {dock} at {start} no longer fits. Nothing is booked. I'll send fresh options.",
  "hi-Latn": "Manzoori lagu nahi ho payi: Dock {dock} {start} ab fit nahi hota. Kuch book nahi hua. Naye options bhejta hoon.",
  "hi": "मंज़ूरी लागू नहीं हो पाई: डॉक {dock} {start} अब फिट नहीं होता। कुछ बुक नहीं हुआ।"},
 "swap_check": {
  "en": "\nThis is a priority load, so a coordinator is checking whether an earlier dock can be freed. You'll hear by {by}; until then this booking stands.",
  "hi-Latn": "\nYeh priority load hai, isliye coordinator dekh rahe hain ki kya pehle ka dock khaali ho sakta hai. {by} tak bataenge; tab tak yeh booking pakki hai.",
  "hi": "\nयह प्राथमिकता वाला लोड है, इसलिए कोऑर्डिनेटर देख रहे हैं कि क्या पहले का डॉक खाली हो सकता है। {by} तक बताएँगे।"},
 "upgraded": {
  "en": "Good news: the coordinator freed an earlier dock. New booking: Dock {dock} at {facility}, start {start}. Please be at the gate by {gate_by}. Booking ref {ref}.",
  "hi-Latn": "Achhi khabar: coordinator ne pehle ka dock khaali kiya. Nayi booking: {facility}, Dock {dock}, {start} se. Gate par {gate_by} tak pahunchiye. Ref {ref}.",
  "hi": "अच्छी खबर: कोऑर्डिनेटर ने पहले का डॉक खाली किया। नई बुकिंग: {facility}, डॉक {dock}, {start} से। रेफ़ {ref}।"},
 "moved": {
  "en": "Change to your booking, sorry: to make room for an urgent delivery the coordinator moved you from Dock {old_dock} at {old_start} to Dock {dock} at {start} ({facility}). Booking ref {ref}. If this does not work for you, call {phone}.",
  "hi-Latn": "Booking mein badlav, maaf kijiye: ek zaroori delivery ke liye coordinator ne aapko Dock {old_dock} {old_start} se Dock {dock} {start} par kiya hai ({facility}). Ref {ref}. Dikkat ho to {phone} par call kijiye.",
  "hi": "बुकिंग में बदलाव, माफ़ कीजिए: एक ज़रूरी डिलीवरी के लिए कोऑर्डिनेटर ने आपको डॉक {old_dock} {old_start} से डॉक {dock} {start} पर किया है। रेफ़ {ref}।"},
 "option_gone": {
  "en": "Sorry, Dock {dock} at {start} {why}. Nothing was booked for you yet. Fresh options:\n{lines}\nReply {nums} to book.",
  "hi-Latn": "Maaf kijiye, Dock {dock} {start} {why}. Aapke liye abhi kuch book nahi hua. Naye options:\n{lines}\nBook karne ke liye {nums} bhejiye.",
  "hi": "माफ़ कीजिए, डॉक {dock} {start} {why}। आपके लिए अभी कुछ बुक नहीं हुआ। नए विकल्प:\n{lines}\nबुक करने के लिए {nums} भेजिए।"},
 "why_taken": {"en": "was just taken by another truck", "hi-Latn": "abhi doosre truck ne le liya", "hi": "अभी दूसरे ट्रक ने ले लिया"},
 "why_protected": {"en": "is now reserved for a higher-priority load", "hi-Latn": "ab zyada priority wale load ke liye rakha gaya hai", "hi": "अब ज़्यादा प्राथमिकता वाले लोड के लिए रखा गया है"},
 "why_nofit": {"en": "no longer fits because the dock schedule changed", "hi-Latn": "ab fit nahi hota, dock ka schedule badal gaya", "hi": "अब फिट नहीं होता, डॉक का शेड्यूल बदल गया"},
 "approval_requested": {
  "en": "I've asked the coordinator to approve Dock {dock} at {start} ({why}). This is NOT confirmed yet. You'll hear by {deadline}; until then keep to your current plan.",
  "hi-Latn": "Dock {dock}, {start} ke liye coordinator se manzoori maangi hai ({why}). Abhi CONFIRM nahi hai. {deadline} tak jawab milega.",
  "hi": "डॉक {dock}, {start} के लिए कोऑर्डिनेटर से मंज़ूरी माँगी है ({why})। अभी पक्का नहीं है। {deadline} तक जवाब मिलेगा।"},
 "no_slot": {
  "en": "I'm sorry - I can't find a {need} dock at {facility} that your truck can reach today ({why}). I've passed your case to {who}, who will contact you by {by}. Please don't go to the dock until they do.",
  "hi-Latn": "Maaf kijiye - aaj {facility} mein aapke truck ke liye koi {need} dock nahi mil raha ({why}). Aapka case {who} ko de diya hai, woh {by} tak sampark karenge. Tab tak dock par mat jaiye.",
  "hi": "माफ़ कीजिए - आज {facility} में आपके ट्रक के लिए कोई {need} डॉक नहीं मिल रहा ({why})। आपका केस {who} को दे दिया है, वे {by} तक संपर्क करेंगे।"},
 "cancelled": {
  "en": "Shipment {order} and its appointment were cancelled by dispatch at {when}. Please don't travel to {facility}; call your dispatcher on {phone}.",
  "hi-Latn": "Shipment {order} aur uska appointment dispatch ne {when} par cancel kar diya. {facility} mat jaiye; apne dispatcher ko {phone} par call kijiye.",
  "hi": "शिपमेंट {order} और उसका अपॉइंटमेंट डिस्पैच ने {when} पर रद्द कर दिया। {facility} मत जाइए; डिस्पैचर को {phone} पर कॉल कीजिए।"},
 "status_yard": {
  "en": "Your {slot} booking on Dock {dock} is still valid: you checked in at {gate}, inside the {grace}-minute grace. Dock {dock} is running late because the truck before you is still unloading. Expected call to dock: about {call}. Queue position {pos}.",
  "hi-Latn": "Aapki {slot} wali booking Dock {dock} par abhi bhi valid hai: aap {gate} par aaye, {grace} minute ki chhoot ke andar. Pichhla truck abhi khaali ho raha hai, isliye Dock {dock} late hai. Dock par bulawa lagbhag {call}. Line mein number {pos}.",
  "hi": "आपकी {slot} वाली बुकिंग डॉक {dock} पर अभी भी मान्य है: आप {gate} पर आए, {grace} मिनट की छूट के अंदर। पिछला ट्रक अभी खाली हो रहा है। डॉक पर बुलावा लगभग {call}।"},
 "status_booked": {
  "en": "Your booking: Dock {dock} at {facility}, start {start} ({status}).",
  "hi-Latn": "Aapki booking: {facility}, Dock {dock}, {start} se ({status}).",
  "hi": "आपकी बुकिंग: {facility}, डॉक {dock}, {start} से ({status})।"},
 "early_offer": {
  "en": "Welcome - you're early. Dock {dock} is free now: I can move you from {old} to {start}, and it does not delay any other booking.\n1) Take Dock {dock} at {start}\n2) Keep my {old} slot",
  "hi-Latn": "Swagat hai - aap jaldi aaye. Dock {dock} abhi khaali hai: aapko {old} se {start} par le ja sakte hain, kisi aur ki booking late nahi hogi.\n1) Dock {dock} {start} lein\n2) {old} wala slot rakhein",
  "hi": "स्वागत है - आप जल्दी आए। डॉक {dock} अभी खाली है: आपको {old} से {start} पर ले जा सकते हैं।\n1) डॉक {dock} {start} लें\n2) {old} वाला स्लॉट रखें"},
 "early_policy": {
  "en": "You're early. Arriving early doesn't move you ahead of trucks already booked, and no dock is free before your {old} slot without delaying someone. Please wait in the yard; we'll call you.",
  "hi-Latn": "Aap jaldi aaye hain. Jaldi aane se pehle se booked truckon se aage nahi ja sakte, aur {old} se pehle koi dock bina kisi ko late kiye khaali nahi. Yard mein rukiye; hum bulayenge.",
  "hi": "आप जल्दी आए हैं। जल्दी आने से पहले से बुक ट्रकों से आगे नहीं जा सकते। यार्ड में रुकिए; हम बुलाएँगे।"},
 "current_ok": {
  "en": "Thanks. With arrival at about {eta}, your current booking (Dock {dock}, start {start}) still works. No change needed.",
  "hi-Latn": "Shukriya. Lagbhag {eta} pahunchne par bhi aapki booking (Dock {dock}, {start} se) chalegi. Kuch badalne ki zarurat nahi.",
  "hi": "धन्यवाद। लगभग {eta} पहुँचने पर भी आपकी बुकिंग (डॉक {dock}, {start} से) चलेगी।"},
 "declined": {
  "en": "No problem - nothing has been booked or held. Your current plan stays as it is.",
  "hi-Latn": "Koi baat nahi - kuch book ya hold nahi hua. Aapka plan jaisa tha waisa hai.",
  "hi": "कोई बात नहीं - कुछ बुक या होल्ड नहीं हुआ।"},
 "safety": {
  "en": "Please make sure everyone is safe first. If anyone is hurt, call 112 now. I've alerted our shift manager {who}, who will call you within {sla} minutes. Don't worry about the dock slot.",
  "hi-Latn": "Sabse pehle sabki suraksha dekhiye. Koi ghayal ho to abhi 112 par call kijiye. Shift manager {who} ko bata diya hai, woh {sla} minute mein call karenge. Dock slot ki chinta mat kijiye.",
  "hi": "सबसे पहले सबकी सुरक्षा देखिए। कोई घायल हो तो अभी 112 पर कॉल कीजिए। शिफ़्ट मैनेजर {who} को बता दिया है, वे {sla} मिनट में कॉल करेंगे।"},
 "human": {
  "en": "I've asked {who} to contact you by {by}.",
  "hi-Latn": "{who} ko bol diya hai, woh {by} tak sampark karenge.",
  "hi": "{who} को बता दिया है, वे {by} तक संपर्क करेंगे।"},
 "invalid_choice": {"en": "Please reply with {nums}.", "hi-Latn": "Kripya {nums} bhejiye.", "hi": "कृपया {nums} भेजिए।"},
 "unknown": {
  "en": "I can help with delays, arrival times and dock slots. What is happening with your trip?",
  "hi-Latn": "Main delay, pahunchne ka time aur dock slot mein madad kar sakta hoon. Aapke trip mein kya ho raha hai?",
  "hi": "मैं देरी, पहुँचने के समय और डॉक स्लॉट में मदद कर सकता हूँ। आपकी ट्रिप में क्या हो रहा है?"},
 "proactive": {
  "en": "Hi {name}, checking on {order} to {facility}: your slot starts {slot}. Are you on track?\n1) On time  2) Running late  3) Need help",
  "hi-Latn": "Namaste {name}, {order} ({facility}) ka slot {slot} se hai. Kya aap time par hain?\n1) Time par  2) Late hoon  3) Madad chahiye",
  "hi": "नमस्ते {name}, {order} ({facility}) का स्लॉट {slot} से है। क्या आप समय पर हैं?\n1) समय पर  2) देर हो रही है  3) मदद चाहिए"},
}

# Channel-layer messages (WhatsApp delivery, privacy, opt-out, safety hold). Same three core languages (regional ones fall back to Hindi),
# same guard. None of them states a dock, a time or a booking unless the caller passes evidence.
T.update({
 "privacy_notice": {
  "en": "SetuHaul dock assistant. We use your mobile number, your messages and your truck's trip details only to plan your dock slot and keep you safe. Messages are kept 90 days. Reply STOP to stop, or ask for a person any time.",
  "hi-Latn": "SetuHaul dock sahayak. Aapka mobile number, message aur trip ki jaankari sirf dock slot tay karne aur aapki suraksha ke liye use hoti hai. Message 90 din rakhe jaate hain. Band karne ke liye STOP bhejiye, ya kabhi bhi insaan se baat maangiye.",
  "hi": "SetuHaul डॉक सहायक। आपका मोबाइल नंबर, संदेश और ट्रिप की जानकारी सिर्फ़ डॉक स्लॉट तय करने और आपकी सुरक्षा के लिए उपयोग होती है। संदेश 90 दिन रखे जाते हैं। बंद करने के लिए STOP भेजिए, या कभी भी किसी व्यक्ति से बात माँगिए।"},
 "please_type": {
  "en": "Sorry, I can only read typed messages for now. Please type your message (for example: late by 1 hour, or reaching 11:30).",
  "hi-Latn": "Maaf kijiye, abhi main sirf likha hua message padh sakta hoon. Kripya message type kijiye (jaise: 1 ghanta late, ya 11:30 tak pahunchunga).",
  "hi": "माफ़ कीजिए, अभी मैं सिर्फ़ लिखा हुआ संदेश पढ़ सकता हूँ। कृपया संदेश टाइप कीजिए (जैसे: 1 घंटा देर, या 11:30 तक पहुँचूँगा)।"},
 "voice_unclear": {
  "en": "Sorry, I could not make out your voice note. Please say it again slowly, or type it (for example: late by 1 hour, or reaching 11:30).",
  "hi-Latn": "Maaf kijiye, aapka voice note samajh nahi aaya. Kripya dheere se dobara boliye, ya likh dijiye (jaise: 1 ghanta late, ya 11:30 tak pahunchunga).",
  "hi": "माफ़ कीजिए, आपका वॉइस नोट समझ नहीं आया। कृपया धीरे से दोबारा बोलिए, या लिख दीजिए (जैसे: 1 घंटा देर, या 11:30 तक पहुँचूँगा)।"},
 "location_noted": {
  "en": "Thanks, I've noted your location. Please also type the time you expect to reach us.",
  "hi-Latn": "Shukriya, aapki location note kar li. Kripya pahunchne ka time bhi likhiye.",
  "hi": "धन्यवाद, आपकी लोकेशन नोट कर ली। कृपया पहुँचने का समय भी लिखिए।"},
 "options_changed": {
  "en": "Those options have changed, so nothing was booked from that tap. These are the current ones:\n",
  "hi-Latn": "Woh options badal gaye hain, isliye us tap se kuch book nahi hua. Abhi ke options:\n",
  "hi": "वे विकल्प बदल गए हैं, इसलिए उस टैप से कुछ बुक नहीं हुआ। अभी के विकल्प:\n"},
 "offer_closed": {
  "en": "Those options are closed, so nothing new was booked from that tap. Ask \"Is my slot still active?\" to see where you stand.",
  "hi-Latn": "Woh options band ho chuke hain, us tap se kuch naya book nahi hua. \"Mera slot active hai?\" puchhiye.",
  "hi": "वे विकल्प बंद हो चुके हैं, उस टैप से कुछ नया बुक नहीं हुआ। \"मेरा स्लॉट चालू है?\" पूछिए।"},
 "opted_out": {
  "en": "You will get no more messages from the SetuHaul dock assistant. Send START to turn them back on. For anything urgent call your dispatcher.",
  "hi-Latn": "Ab aapko SetuHaul dock sahayak se message nahi aayenge. Wapas chalu karne ke liye START bhejiye. Zaroori kaam ho to dispatcher ko call kijiye.",
  "hi": "अब आपको SetuHaul डॉक सहायक से संदेश नहीं आएँगे। फिर से चालू करने के लिए START भेजिए।"},
 "opted_in": {
  "en": "Messages are on again. Tell me what is happening with your trip.",
  "hi-Latn": "Message phir se chalu hain. Batayiye trip mein kya ho raha hai.",
  "hi": "संदेश फिर से चालू हैं। बताइए ट्रिप में क्या हो रहा है।"},
 "unknown_sender": {
  "en": "This number is not registered with SetuHaul. Please contact your dispatcher. / Yeh number SetuHaul mein registered nahi hai. Kripya apne dispatcher se sampark kijiye.",
  "hi-Latn": "This number is not registered with SetuHaul. Please contact your dispatcher. / Yeh number SetuHaul mein registered nahi hai. Kripya apne dispatcher se sampark kijiye.",
  "hi": "This number is not registered with SetuHaul. Please contact your dispatcher. / Yeh number SetuHaul mein registered nahi hai. Kripya apne dispatcher se sampark kijiye."},
 "safety_hold": {
  "en": "A person is handling your safety case ({ref}). I've added your message to it. If anyone is hurt, call 112. Scheduling is paused for you until they clear it.",
  "hi-Latn": "Aapka safety case ({ref}) ek insaan dekh rahe hain. Aapka message usme jod diya hai. Koi ghayal ho to 112 par call kijiye. Unke clear karne tak scheduling ruki hai.",
  "hi": "आपका सुरक्षा केस ({ref}) एक व्यक्ति देख रहे हैं। आपका संदेश उसमें जोड़ दिया है। कोई घायल हो तो 112 पर कॉल कीजिए।"},
 "turn_error": {
  "en": "Sorry, I could not finish that on our side. Nothing new was booked. I've asked a coordinator to contact you.",
  "hi-Latn": "Maaf kijiye, hamari taraf se yeh poora nahi ho paya. Kuch naya book nahi hua. Coordinator ko aapse sampark karne ko bola hai.",
  "hi": "माफ़ कीजिए, हमारी तरफ़ से यह पूरा नहीं हो पाया। कुछ नया बुक नहीं हुआ। कोऑर्डिनेटर को आपसे संपर्क करने को कहा है।"},
 "staff_page": {
  "en": "[{urgency}] {reason} {ref}: {summary} Respond by {by}. Reply ACK {ref} to take it."},
 "staff_ack": {"en": "Thanks {name}: {ref} is yours. The next person in the chain will not be paged."},
 "staff_help": {"en": "SetuHaul staff line. Reply ACK <case id> to take a case. The driver chat is for drivers only."},
})


# Gujarati, Marathi, Punjabi and Marwari (drafts for native review); missing keys fall back to Hindi.
from .replies_regional import REGIONAL, PHRASES, BOOKED_WORDS, DOCK_WORDS   # noqa: E402
from .languages import pick, OR_WORD                                       # noqa: E402

for _lang, _texts in REGIONAL.items():
    for _k, _v in _texts.items():
        T[_k][_lang] = _v

DOCK_RE = r"(?:Dock|डॉक|" + "|".join(DOCK_WORDS) + r")\s+(D\d)"


def t(key, lang, **kw):
    return pick(T[key], lang).format(**kw)


def phrase(key, lang, en, hi_latn, hi):
    """A small phrase built in workflow.py, in the driver's language (Hindi fallback for the new ones)."""
    table = dict(PHRASES.get(key, {}), en=en, **{"hi-Latn": hi_latn, "hi": hi})
    return pick(table, lang)


def guard(text, evidence):
    """Return [] if every dock and time in text is backed by evidence, else the violations."""
    bad = []
    for d in re.findall(DOCK_RE, text):
        if d not in evidence.get("docks", set()):
            bad.append("dock " + d)
    for tm in re.findall(r"\b\d{1,2}:\d{2} (?:AM|PM)\b", text):
        if tm not in evidence.get("times", set()):
            bad.append("time " + tm)
    if re.search(r"^Booked|^Confirmed|Book ho gaya|बुक हो गया|पक्का हो गया|Confirm ho gaya|" + "|".join(BOOKED_WORDS), text) \
            and not evidence.get("confirmed"):
        bad.append("claims booked without CONFIRMED commit")
    return bad


OPTION_DOCK = re.compile(r"\b(D\d)\b")
OPTION_TIME = re.compile(r"\b\d{1,2}:\d{2} (?:AM|PM)\b")


def guard_options(options, evidence):
    """Every dock and time in a button title or list description must be backed by the turn's evidence."""
    bad = []
    for o in options:
        for field_ in (o.get("title", ""), o.get("desc", "")):
            for d in OPTION_DOCK.findall(field_ or ""):
                if d not in evidence.get("docks", set()):
                    bad.append(f"option dock {d}")
            for tm in OPTION_TIME.findall(field_ or ""):
                if tm not in evidence.get("times", set()):
                    bad.append(f"option time {tm}")
            if re.search(r"\bbooked\b|confirm", field_ or "", re.I) and not evidence.get("confirmed"):
                bad.append("option claims booked")
    return bad


def evidence_from_text(text):
    """Docks and times stated in an already-guarded text (used to re-check a re-sent offer)."""
    return {"docks": set(re.findall(DOCK_RE, text or "")), "times": set(OPTION_TIME.findall(text or "")),
            "confirmed": False}
