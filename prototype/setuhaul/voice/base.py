"""Shared pieces of the voice layer: errors, the transcript type, the language map, HTTP and audio helpers,
and the 24 h store for spoken replies. Each speech provider lives in its own module next to this one."""
import logging
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


def _sniff(audio):
    if audio[:3] == b"ID3" or audio[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if audio[:4] == b"OggS":
        return "audio/ogg"
    if audio[:4] == b"RIFF":
        return "audio/wav"
    return "application/octet-stream"


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


WHATSAPP_AUDIO = {"audio/mpeg", "audio/ogg", "audio/aac", "audio/mp4", "audio/amr"}   # WAV is not accepted
EXT = {"audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/aac": "aac", "audio/mp4": "m4a", "audio/amr": "amr"}
MEDIA_TTL_H = 24
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def store_audio(con, data, content_type, real_now):
    """Keep one spoken reply for WhatsApp to fetch; drop expired ones. Caller is outside a transaction. -> token."""
    from datetime import timedelta
    from ..clock import iso
    from ..db import write_txn
    token = uuid.uuid4().hex
    with write_txn(con):
        con.execute("DELETE FROM wa_media WHERE expires_real < ?", (iso(real_now),))
        con.execute("INSERT INTO wa_media VALUES (?,?,?,?,?)",
                    (token, content_type, data, iso(real_now), iso(real_now + timedelta(hours=MEDIA_TTL_H))))
    return token


def load_audio(con, token, real_now):
    """(bytes, content_type) for a live token, else None."""
    from ..clock import iso
    if not TOKEN_RE.match(token or ""):
        return None
    r = con.execute("SELECT data, content_type FROM wa_media WHERE token=? AND expires_real >= ?", (token, iso(real_now))).fetchone()
    return None if r is None else (bytes(r[0]), r[1])
