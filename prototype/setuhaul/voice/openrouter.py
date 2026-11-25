"""OpenRouter speech endpoints, one OpenRouter key (default ElevenLabs Scribe v2 + Eleven v3). VOICE=openrouter."""
import base64
import json
import urllib.error

from .base import Transcript, VoiceError, _bcp47, _ext, _post, _sniff, speech_lang


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
