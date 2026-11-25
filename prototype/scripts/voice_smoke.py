"""Voice smoke test against the real speech backend (run this with your key before switching VOICE on).

For each language: speak a typical SetuHaul reply (TTS) -> turn it into a WhatsApp-style OGG/Opus voice note
(ffmpeg) -> transcribe it back (STT) -> run the transcript through the rules engine's reader. Prints what was
heard, the language reported, timings and whether the engine reads the same meaning as from the typed text.

    VOICE=openrouter OPENROUTER_API_KEY=sk-or-... python3 -I scripts/voice_smoke.py
    VOICE=openrouter ... python3 -I scripts/voice_smoke.py gu mr      # only some languages
    ... --keep /tmp/voice                                              # keep the audio files to listen to

Uses real API credits (a few seconds of audio and ~400 characters per language).
"""
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from setuhaul import nlu, voice as V                                            # noqa: E402

LINES = {
    "en": "I am late by 1 hour, reaching by 11:30.",
    "hi-Latn": "Main 1 ghanta late hoon, 11:30 tak pahunchunga.",
    "hi": "मैं 1 घंटा लेट हूँ, 11:30 तक पहुँचूँगा।",
    "gu": "હું 1 કલાક મોડો છું, 11:30 સુધી પહોંચીશ.",
    "mr": "मला 1 तास उशीर होईल, 11:30 पर्यंत पोहोचेन.",
    "pa": "ਮੈਂ 1 ਘੰਟਾ ਲੇਟ ਹਾਂ, 11:30 ਤੱਕ ਪਹੁੰਚਾਂਗਾ।",
    "mwr": "म्हैं 1 घंटो मोड़ो हूं, 11:30 तांई पूगसूं।",
}
AS_OF = datetime.fromisoformat("2026-08-04T09:20:00+05:30")


def meaning(text):
    x = nlu.extract(text, AS_OF)
    out = {k: getattr(x, k) for k in ("intent", "delay_min", "eta", "safety", "choice") if getattr(x, k) not in (None, False)}
    if out.get("eta"):
        out["eta"] = out["eta"].strftime("%H:%M")
    return out


def main(argv):
    keep = None
    if "--keep" in argv:
        i = argv.index("--keep")
        keep = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
        os.makedirs(keep, exist_ok=True)
    langs = argv or list(LINES)
    speech = V.make_voice()
    if not speech:
        sys.exit("Set VOICE (openrouter | sarvam | selfhosted) and its key first; see deploy/.env.template.")
    ff = shutil.which("ffmpeg")
    print(f"backend: {speech.backend}" + (f"  stt={getattr(speech, 'stt_model', '')}  tts={getattr(speech, 'tts_model', '')}"
                                          if hasattr(speech, "stt_model") else "") + ("" if ff else "  (no ffmpeg: sending MP3, not OGG)"))
    ok = 0
    for lang in langs:
        line = LINES[lang]
        print(f"\n== {lang}  typed: {line}")
        try:
            t0 = time.monotonic()
            mp3, ctype = speech.speak(V.spoken(line), lang)
            t1 = time.monotonic()
            audio, actype = mp3, ctype
            if ff:                                                 # what WhatsApp actually delivers: OGG/Opus
                audio = subprocess.run([ff, "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "24k",
                                        "-ac", "1", "-f", "ogg", "pipe:1"], input=mp3, capture_output=True, check=True).stdout
                actype = "audio/ogg"
            if keep:
                open(os.path.join(keep, f"{lang}.mp3"), "wb").write(mp3)
            tr = speech.transcribe(audio, actype, lang_hint=None)
            t2 = time.monotonic()
        except (V.VoiceError, subprocess.CalledProcessError) as e:
            print(f"   FAILED: {e}")
            continue
        heard = V.spoken_times(tr.text)                     # exactly what inbound.py hands the engine
        same = meaning(heard) == meaning(line)
        ok += same
        print(f"   tts {len(mp3) // 1024} KB {ctype} in {t1 - t0:.1f}s | stt {t2 - t1:.1f}s, language {tr.speech_lang}")
        print(f"   heard: {tr.text}")
        print(f"   engine reads: {meaning(heard)}  {'SAME as typed' if same else 'DIFFERENT from typed ' + str(meaning(line))}")
    print(f"\n{ok}/{len(langs)} languages: the engine read the same meaning from the voice note as from the typed text.")
    return 0 if ok == len(langs) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
