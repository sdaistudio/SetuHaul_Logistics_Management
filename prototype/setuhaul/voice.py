"""Voice for drivers: WhatsApp voice notes in, the same engine, a spoken reply out.

    voice note (audio) --STT--> text --> the unchanged rules engine --> reply text + option buttons
                                                                   \\--TTS--> voice note in the driver's language

Only dedicated speech models are used here, no LLM: speech recognition (STT) and speech synthesis (TTS).
Two interchangeable backends, chosen by the VOICE setting (off by default, so nothing changes unless it is set):

    VOICE=selfhosted   our own speech server (prototype/speech/) running open models on a GPU box:
                       AI4Bharat IndicConformer for STT (MIT; 22 Indian languages) plus an English model,
                       AI4Bharat Indic Parler-TTS for TTS (Apache-2.0). Audio never leaves our infrastructure.
    VOICE=sarvam       Sarvam AI's hosted speech APIs: Saaras (STT) and Bulbul (TTS). Fastest way to pilot.

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
    with urllib.request.urlopen(req, timeout=timeout) as r:            # noqa: S310 (fixed https endpoints)
        return r.status, r.headers.get("Content-Type", ""), r.read()


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
        if v == "selfhosted":
            return SelfHostedSpeech(e.get("VOICE_SERVER_URL", ""), token=e.get("VOICE_SERVER_TOKEN", ""))
    except VoiceError as err:
        log.warning("VOICE=%s but %s: voice notes will get 'please type'", v, err)
        return None
    if v not in ("off", ""):
        log.warning("unknown VOICE value: voice off")
    return None
