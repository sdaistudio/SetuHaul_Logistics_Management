"""Voice for drivers: WhatsApp voice notes in, the same engine, a spoken reply out.

    voice note (audio) --STT--> text --> the unchanged rules engine --> reply text + option buttons
                                                                   \\--TTS--> voice note in the driver's language

Only dedicated speech models are used here, no LLM: speech recognition (STT) and speech synthesis (TTS).
Three interchangeable backends, chosen by the VOICE setting (off by default, so nothing changes unless it is set):

    VOICE=selfhosted   our own speech server (prototype/speech/) running open models on a GPU box:
                       AI4Bharat IndicConformer for STT (MIT; 22 Indian languages) plus an English model,
                       AI4Bharat Indic Parler-TTS for TTS (Apache-2.0). Audio never leaves our infrastructure.
    VOICE=sarvam       Sarvam AI's hosted speech APIs: Saaras (STT) and Bulbul (TTS).
    VOICE=openrouter   OpenRouter's speech endpoints (/audio/transcriptions, /audio/speech) with one OpenRouter key;
                       default models ElevenLabs Scribe v2 (STT) and Eleven v3 (TTS). Both model ids are settings.

Language: the seven driver languages map to the speech languages below. Marwari has no speech model of its own
(it is not a scheduled language): it is heard and spoken through Hindi, which shares the Devanagari script and
much vocabulary. Hinglish is heard as Hindi (the rules read the Devanagari transcript) and spoken in Hindi.

Standard library only; HTTP is injectable (`http=`) so tests run with no network.
"""
import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("setuhaul.voice")

# driver language (languages.py) -> BCP-47 code the speech models use
SPEECH_LANG = {"en": "en-IN", "hi-Latn": "hi-IN", "hi": "hi-IN", "gu": "gu-IN", "mr": "mr-IN", "pa": "pa-IN", "mwr": "hi-IN"}
# BCP-47 code a model reports -> driver language, when the driver's language is not yet known
FROM_SPEECH = {"en-IN": "en", "hi-IN": "hi", "gu-IN": "gu", "mr-IN": "mr", "pa-IN": "pa"}
AUDIO_TYPES = ("audio/",)                       # WhatsApp voice notes arrive as audio/ogg (opus)
MAX_AUDIO_BYTES = 5 * 1024 * 1024               # about 5 minutes of opus; drivers send seconds
MAX_SPOKEN_CHARS = 600                          # a spoken reply stays short


class VoiceError(Exception):
    """Any speech failure. The caller answers in text instead; a voice problem never blocks a reply."""


@dataclass
class Transcript:
    text: str
    speech_lang: Optional[str] = None           # BCP-47 the model reported, if any
    backend: str = ""


def is_audio(content_type):
    return bool(content_type) and content_type.split(";")[0].strip().lower().startswith(AUDIO_TYPES)


def speech_lang(lang):
    return SPEECH_LANG.get(lang or "", "hi-IN")


def spoken(reply):
    """The reply as it should be heard: no booking-reference codes read letter by letter, short."""
    t = re.sub(r"\bAPT-[A-Z0-9-]+\b", "", reply or "")
    t = re.sub(r"[ \t]+", " ", t).strip()
    return t[:MAX_SPOKEN_CHARS]


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:        # noqa: S310 (fixed https endpoints)
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:                                 # keep the provider's error message
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", e.read() or b""


def _multipart(fields, file_field, filename, content_type, data):
    boundary = "----setuhaul" + uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
               f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def _ext(content_type):
    ct = (content_type or "").split(";")[0].strip().lower()
    return {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac", "audio/amr": "amr",
            "audio/wav": "wav", "audio/x-wav": "wav", "audio/webm": "webm"}.get(ct, "bin")


class SarvamSpeech:
    """Sarvam AI hosted speech models. STT: POST /speech-to-text (multipart, header api-subscription-key),
    response {"transcript", "language_code"}; language_code "unknown" lets the model detect the language.
    TTS: POST /text-to-speech (JSON), response {"audios": [base64 ...]}; MP3 is requested for WhatsApp."""
    backend = "sarvam"
    BASE = "https://api.sarvam.ai"

    def __init__(self, api_key, stt_model="saaras:v3", tts_model="bulbul:v3", speaker=None, timeout=12, http=None):
        if not api_key:
            raise VoiceError("SARVAM_API_KEY is not set")
        self.key, self.stt_model, self.tts_model, self.speaker = api_key, stt_model, tts_model, speaker
        self.timeout, self.http = timeout, http or _post

    def transcribe(self, audio, content_type, lang_hint=None):
        fields = {"model": self.stt_model, "mode": "transcribe", "language_code": speech_lang(lang_hint) if lang_hint else "unknown"}
        body, ctype = _multipart(fields, "file", "voice." + _ext(content_type), content_type.split(";")[0], audio)
        try:
            status, _, raw = self.http(self.BASE + "/speech-to-text", body, {"api-subscription-key": self.key, "Content-Type": ctype},
                                       self.timeout)
            d = json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise VoiceError(f"stt: {type(e).__name__}") from None
        if status != 200 or not isinstance(d, dict):
            raise VoiceError(f"stt: http {status}")
        return Transcript(text=(d.get("transcript") or "").strip(), speech_lang=d.get("language_code"), backend=self.backend)

    def speak(self, text, lang):
        payload = {"text": text, "target_language_code": speech_lang(lang), "model": self.tts_model, "output_audio_codec": "mp3"}
        if self.speaker:
            payload["speaker"] = self.speaker
        try:
            status, _, raw = self.http(self.BASE + "/text-to-speech", json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                       {"api-subscription-key": self.key, "Content-Type": "application/json"}, self.timeout)
            d = json.loads(raw.decode("utf-8"))
            audio = base64.b64decode("".join(d.get("audios") or []))
        except (urllib.error.URLError, OSError, ValueError, TypeError) as e:
            raise VoiceError(f"tts: {type(e).__name__}") from None
        if status != 200 or not audio:
            raise VoiceError(f"tts: http {status}")
        return audio, _sniff(audio)


class SelfHostedSpeech:
    """Our own speech server (prototype/speech/server.py): POST {base}/stt multipart (file, language|"auto") ->
    {"transcript", "language_code"}; POST {base}/tts JSON {text, language} -> audio/mpeg bytes."""
    backend = "selfhosted"

    def __init__(self, base_url, token="", timeout=20, http=None):
        if not base_url:
            raise VoiceError("VOICE_SERVER_URL is not set")
        self.base, self.token, self.timeout, self.http = base_url.rstrip("/"), token, timeout, http or _post

    def _auth(self):
        return {"Authorization": "Bearer " + self.token} if self.token else {}

    def transcribe(self, audio, content_type, lang_hint=None):
        body, ctype = _multipart({"language": speech_lang(lang_hint) if lang_hint else "auto"}, "file", "voice." + _ext(content_type),
                                 content_type.split(";")[0], audio)
        try:
            status, _, raw = self.http(self.base + "/stt", body, {**self._auth(), "Content-Type": ctype}, self.timeout)
            d = json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise VoiceError(f"stt: {type(e).__name__}") from None
        if status != 200 or not isinstance(d, dict):
            raise VoiceError(f"stt: http {status}")
        return Transcript(text=(d.get("transcript") or "").strip(), speech_lang=d.get("language_code"), backend=self.backend)

    def speak(self, text, lang):
        try:
            status, ctype, raw = self.http(self.base + "/tts", json.dumps({"text": text, "language": speech_lang(lang)},
                                                                          ensure_ascii=False).encode("utf-8"),
                                           {**self._auth(), "Content-Type": "application/json"}, self.timeout)
        except (urllib.error.URLError, OSError) as e:
            raise VoiceError(f"tts: {type(e).__name__}") from None
        if status != 200 or not raw:
            raise VoiceError(f"tts: http {status}")
        return raw, (ctype.split(";")[0] if ctype.startswith("audio/") else _sniff(raw))


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


# language a model reports (ISO 639-1, 639-3, a name, or BCP-47) -> BCP-47
_LANG_ALIASES = {"en": "en-IN", "eng": "en-IN", "english": "en-IN", "hi": "hi-IN", "hin": "hi-IN", "hindi": "hi-IN",
                 "gu": "gu-IN", "guj": "gu-IN", "gujarati": "gu-IN", "mr": "mr-IN", "mar": "mr-IN", "marathi": "mr-IN",
                 "pa": "pa-IN", "pan": "pa-IN", "punjabi": "pa-IN", "panjabi": "pa-IN"}


def _bcp47(code):
    c = (code or "").strip()
    if c in FROM_SPEECH:
        return c
    c = c.lower()
    return _LANG_ALIASES.get(c) or _LANG_ALIASES.get(c.replace("_", "-").split("-")[0])


class OpenRouterSpeech:
    """OpenRouter speech endpoints (speech models, not chat LLMs), one key for many providers.
    STT: POST /api/v1/audio/transcriptions JSON {model, input_audio: {data: base64, format}, language (ISO 639-1,
         optional: auto-detect), response_format: verbose_json} -> {text, language, usage}.
    TTS: POST /api/v1/audio/speech JSON {model, input, voice?, response_format: "mp3"} -> audio/mpeg bytes.
    ElevenLabs-specific settings travel in provider.options.elevenlabs (OpenRouter drops keys a provider does not know)."""
    backend = "openrouter"
    BASE = "https://openrouter.ai/api/v1"

    def __init__(self, api_key, stt_model="elevenlabs/scribe-v2", tts_model="elevenlabs/eleven-v3", voice=None,
                 data_collection=None, timeout=20, http=None):
        if not api_key:
            raise VoiceError("OPENROUTER_API_KEY is not set")
        self.key, self.stt_model, self.tts_model, self.voice = api_key, stt_model, tts_model, voice
        self.data_collection, self.timeout, self.http = data_collection, timeout, http or _post

    def _headers(self):
        return {"Authorization": "Bearer " + self.key, "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/sdaistudio/SetuHaul_Logistics_Management", "X-Title": "SetuHaul voice"}

    def _provider(self, options=None):
        p = {}
        if self.data_collection:
            p["data_collection"] = self.data_collection
        if options:
            p["options"] = options
        return p

    def transcribe(self, audio, content_type, lang_hint=None):
        body = {"model": self.stt_model, "response_format": "verbose_json",
                "input_audio": {"data": base64.b64encode(audio).decode(), "format": _ext(content_type)}}
        if lang_hint:
            body["language"] = speech_lang(lang_hint)[:2]          # ISO 639-1: hi, gu, mr, pa, en
        if self._provider():
            body["provider"] = self._provider()
        try:
            status, _, raw = self.http(self.BASE + "/audio/transcriptions", json.dumps(body).encode(), self._headers(), self.timeout)
            d = json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise VoiceError(f"stt: {type(e).__name__}") from None
        if status != 200 or not isinstance(d, dict) or "text" not in d:
            err = (d.get("error") or {}).get("message", "") if isinstance(d, dict) else ""
            raise VoiceError(f"stt: http {status} {err}"[:160])
        return Transcript(text=(d.get("text") or "").strip(), speech_lang=_bcp47(d.get("language")), backend=self.backend)

    def speak(self, text, lang):
        body = {"model": self.tts_model, "input": text, "response_format": "mp3"}
        if self.voice:
            body["voice"] = self.voice
        prov = self._provider({"elevenlabs": {"language_code": speech_lang(lang)[:2]}})
        body["provider"] = prov
        try:
            status, ctype, raw = self.http(self.BASE + "/audio/speech", json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                           self._headers(), self.timeout)
        except (urllib.error.URLError, OSError) as e:
            raise VoiceError(f"tts: {type(e).__name__}") from None
        if status != 200 or not raw or ctype.startswith("application/json"):
            raise VoiceError(f"tts: http {status} {raw[:120].decode('utf-8', 'replace') if raw else ''}"[:160])
        return raw, (ctype.split(";")[0] if ctype.startswith("audio/") else _sniff(raw))


def _sniff(audio):
    if audio[:3] == b"ID3" or audio[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if audio[:4] == b"OggS":
        return "audio/ogg"
    if audio[:4] == b"RIFF":
        return "audio/wav"
    return "application/octet-stream"


WHATSAPP_AUDIO = {"audio/mpeg", "audio/ogg", "audio/aac", "audio/mp4", "audio/amr"}   # WAV is not accepted
EXT = {"audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/aac": "aac", "audio/mp4": "m4a", "audio/amr": "amr"}
MEDIA_TTL_H = 24
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def store_audio(con, data, content_type, real_now):
    """Keep one spoken reply for WhatsApp to fetch; drop expired ones. Caller is outside a transaction. -> token."""
    from datetime import timedelta
    from .clock import iso
    from .db import write_txn
    token = uuid.uuid4().hex
    with write_txn(con):
        con.execute("DELETE FROM wa_media WHERE expires_real < ?", (iso(real_now),))
        con.execute("INSERT INTO wa_media VALUES (?,?,?,?,?)",
                    (token, content_type, data, iso(real_now), iso(real_now + timedelta(hours=MEDIA_TTL_H))))
    return token


def load_audio(con, token, real_now):
    """(bytes, content_type) for a live token, else None."""
    from .clock import iso
    if not TOKEN_RE.match(token or ""):
        return None
    r = con.execute("SELECT data, content_type FROM wa_media WHERE token=? AND expires_real >= ?", (token, iso(real_now))).fetchone()
    return None if r is None else (bytes(r[0]), r[1])


def make_voice(env=None):
    """The configured speech backend, or None (VOICE unset or off: voice notes get the usual 'please type')."""
    e = env if env is not None else os.environ
    v = (e.get("VOICE") or "off").strip().lower()
    try:
        if v == "sarvam":
            return SarvamSpeech(e.get("SARVAM_API_KEY", ""), stt_model=e.get("SARVAM_STT_MODEL") or "saaras:v3",
                                tts_model=e.get("SARVAM_TTS_MODEL") or "bulbul:v3", speaker=e.get("SARVAM_SPEAKER") or None)
        if v == "openrouter":
            return OpenRouterSpeech(e.get("OPENROUTER_API_KEY", ""), stt_model=e.get("OPENROUTER_STT_MODEL") or "elevenlabs/scribe-v2",
                                    tts_model=e.get("OPENROUTER_TTS_MODEL") or "elevenlabs/eleven-v3",
                                    voice=e.get("OPENROUTER_TTS_VOICE") or None,
                                    data_collection=e.get("OPENROUTER_DATA_COLLECTION") or None)
        if v == "selfhosted":
            return SelfHostedSpeech(e.get("VOICE_SERVER_URL", ""), token=e.get("VOICE_SERVER_TOKEN", ""))
    except VoiceError as err:
        log.warning("VOICE=%s but %s: voice notes will get 'please type'", v, err)
        return None
    if v not in ("off", ""):
        log.warning("unknown VOICE value: voice off")
    return None
