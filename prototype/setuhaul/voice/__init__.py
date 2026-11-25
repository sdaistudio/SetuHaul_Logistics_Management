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

Layout: base.py (shared), openrouter.py, sarvam.py, selfhosted.py (one per provider), spoken_times.py.
Standard library only; HTTP is injectable (`http=`) so tests run with no network.
"""
import logging
import os

from .base import (AUDIO_TYPES, EXT, FROM_SPEECH, MAX_AUDIO_BYTES, MAX_SPOKEN_CHARS, MEDIA_TTL_H, SPEECH_LANG,  # noqa: F401
                   TOKEN_RE, WHATSAPP_AUDIO, Transcript, VoiceError, _bcp47, _ext, _multipart, _post, _sniff, is_audio,
                   load_audio, speech_lang, spoken, store_audio)
from .openrouter import OpenRouterSpeech
from .sarvam import SarvamSpeech
from .selfhosted import SelfHostedSpeech
from .spoken_times import spoken_times  # noqa: F401

__all__ = ["AUDIO_TYPES", "EXT", "FROM_SPEECH", "MAX_AUDIO_BYTES", "MAX_SPOKEN_CHARS", "MEDIA_TTL_H", "SPEECH_LANG", "TOKEN_RE", "WHATSAPP_AUDIO", "Transcript", "VoiceError", "is_audio", "load_audio", "speech_lang", "spoken", "store_audio", "spoken_times", "OpenRouterSpeech", "SarvamSpeech", "SelfHostedSpeech", "make_voice", "_bcp47", "_ext", "_multipart", "_post", "_sniff"]
log = logging.getLogger("setuhaul.voice")


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
