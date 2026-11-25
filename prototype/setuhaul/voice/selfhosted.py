"""Our own speech server (prototype/speech/): AI4Bharat IndicConformer + Whisper, Indic Parler-TTS. VOICE=selfhosted."""
import json
import urllib.error

from .base import Transcript, VoiceError, _ext, _multipart, _post, _sniff, speech_lang


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
