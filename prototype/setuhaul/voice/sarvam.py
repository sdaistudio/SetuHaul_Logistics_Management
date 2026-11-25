"""Sarvam AI hosted speech models: Saaras (speech-to-text) and Bulbul (text-to-speech). VOICE=sarvam."""
import base64
import json
import urllib.error

from .base import Transcript, VoiceError, _ext, _multipart, _post, _sniff, speech_lang


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
