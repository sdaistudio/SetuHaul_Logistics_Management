"""SetuHaul self-hosted speech server: open speech models (not LLMs) on our own GPU box.

    POST /stt   multipart: file (any audio ffmpeg reads; WhatsApp sends OGG/Opus), language (BCP-47 or "auto")
                -> {"transcript": "...", "language_code": "hi-IN", "model": "..."}
    POST /tts   JSON {"text": "...", "language": "gu-IN"}  -> audio/mpeg (MP3, which WhatsApp accepts)
    GET  /healthz

Models (downloaded from Hugging Face on first start; both AI4Bharat repos are gated: accept the terms
on the model page with the account whose HF_TOKEN is used):
    STT Indian languages  ai4bharat/indic-conformer-600m-multilingual   MIT     22 Indian languages incl. hi, gu, mr, pa
    STT English + "auto"  openai/whisper-small                           MIT     English transcripts and language ID
    TTS                   ai4bharat/indic-parler-tts                     Apache-2.0  hi, gu, mr, en (pa supported, unofficial)

Marwari has no model of its own: the app sends hi-IN for it (setuhaul/voice/base.py SPEECH_LANG).

Status: written against the model cards' published usage; NOT run in the build environment (the models
are gated and need a GPU). Run the smoke test in README.md on the target box before switching VOICE=selfhosted.
"""
import io
import os
import subprocess
import tempfile

import numpy as np
import torch
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

TOKEN = os.environ.get("VOICE_SERVER_TOKEN", "")
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
SR = 16000
MAX_BYTES = 5 * 1024 * 1024
MAX_TEXT = 600

# BCP-47 -> IndicConformer language id
INDIC = {"hi-IN": "hi", "gu-IN": "gu", "mr-IN": "mr", "pa-IN": "pa"}
# Whisper language name -> BCP-47 (only the languages SetuHaul drivers use)
WHISPER_LANG = {"english": "en-IN", "hindi": "hi-IN", "gujarati": "gu-IN", "marathi": "mr-IN", "punjabi": "pa-IN"}
# Indic Parler-TTS named speakers (model card): one clear voice per language
SPEAKER = {"hi-IN": "Rohit", "gu-IN": "Yash", "mr-IN": "Sanjay", "pa-IN": "Divjot", "en-IN": "Thoma"}

app = FastAPI(title="SetuHaul speech")
M = {}


def _auth(authorization):
    if TOKEN and authorization != "Bearer " + TOKEN:
        raise HTTPException(401, "bad token")


def _load():
    if M:
        return M
    from transformers import AutoModel, AutoTokenizer, pipeline
    from parler_tts import ParlerTTSForConditionalGeneration
    M["indic"] = AutoModel.from_pretrained("ai4bharat/indic-conformer-600m-multilingual", trust_remote_code=True)
    M["whisper"] = pipeline("automatic-speech-recognition", model="openai/whisper-small", device=DEVICE)
    tts = ParlerTTSForConditionalGeneration.from_pretrained("ai4bharat/indic-parler-tts").to(DEVICE)
    M["tts"], M["tts_tok"] = tts, AutoTokenizer.from_pretrained("ai4bharat/indic-parler-tts")
    M["desc_tok"] = AutoTokenizer.from_pretrained(tts.config.text_encoder._name_or_path)
    return M


@app.on_event("startup")
def warm():
    if os.environ.get("SPEECH_LAZY") != "1":
        _load()


def _decode(data):
    """Any audio -> 16 kHz mono float32 via ffmpeg (WhatsApp voice notes are OGG/Opus)."""
    p = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"],
                       input=data, capture_output=True, timeout=30)
    if p.returncode != 0 or not p.stdout:
        raise HTTPException(400, "could not decode audio")
    return np.frombuffer(p.stdout, dtype=np.float32)


def _whisper(wav, language=None):
    kw = {"generate_kwargs": {"task": "transcribe", **({"language": language} if language else {})}, "return_language": True}
    out = M["whisper"]({"raw": wav, "sampling_rate": SR}, **kw)
    chunks = out.get("chunks") or [{}]
    return out["text"].strip(), WHISPER_LANG.get((chunks[0].get("language") or "").lower())


@app.get("/healthz")
def healthz():
    return {"ok": True, "device": DEVICE, "loaded": sorted(M)}


@app.post("/stt")
async def stt(file: UploadFile = File(...), language: str = Form("auto"), authorization: str = Header(default="")):
    _auth(authorization)
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "audio too large")
    _load()
    wav = _decode(data)
    lang = language if language in INDIC or language == "en-IN" else None
    if lang is None:                                    # first message: Whisper identifies the language
        text, lang = _whisper(wav)
        lang = lang or "hi-IN"
        if lang == "en-IN":
            return {"transcript": text, "language_code": lang, "model": "whisper-small"}
    if lang == "en-IN":
        text, _ = _whisper(wav, "english")
        return {"transcript": text, "language_code": lang, "model": "whisper-small"}
    with torch.inference_mode():
        text = M["indic"](torch.from_numpy(wav).unsqueeze(0), INDIC[lang], "ctc")
    return {"transcript": (text or "").strip(), "language_code": lang, "model": "indic-conformer-600m"}


class Speak(BaseModel):
    text: str
    language: str = "hi-IN"


@app.post("/tts")
def tts(req: Speak, authorization: str = Header(default="")):
    _auth(authorization)
    text = req.text.strip()[:MAX_TEXT]
    if not text:
        raise HTTPException(400, "empty text")
    _load()
    who = SPEAKER.get(req.language, "Rohit")
    desc = f"{who} speaks clearly at a moderate pace with a calm tone. The recording is very clear with no background noise."
    d = M["desc_tok"](desc, return_tensors="pt").to(DEVICE)
    p = M["tts_tok"](text, return_tensors="pt").to(DEVICE)
    with torch.inference_mode():
        audio = M["tts"].generate(input_ids=d.input_ids, attention_mask=d.attention_mask,
                                  prompt_input_ids=p.input_ids, prompt_attention_mask=p.attention_mask)
    wav = audio.cpu().numpy().squeeze().astype(np.float32)
    rate = M["tts"].config.sampling_rate
    enc = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                          "-codec:a", "libmp3lame", "-b:a", "48k", "-f", "mp3", "pipe:1"], input=wav.tobytes(), capture_output=True, timeout=30)
    if enc.returncode != 0:
        raise HTTPException(500, "encode failed")
    return Response(content=enc.stdout, media_type="audio/mpeg")
