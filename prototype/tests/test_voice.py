"""Voice notes: speech-to-text in, the unchanged engine, text + buttons out, then a spoken reply.

No network: a fake speech backend stands in for Sarvam / the self-hosted server, and the HTTP
layer of the real backends is stubbed so their request and response shapes are still checked.
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from setuhaul import db, chan_db, voice as V                                   # noqa: E402
from setuhaul import replies as R                                              # noqa: E402
from setuhaul.channels.console import ConsoleChannel                           # noqa: E402
from setuhaul.channels.twilio_wa import TwilioChannel, sent_kind               # noqa: E402
from setuhaul.channels.base import OutboundMessage                             # noqa: E402
from setuhaul.settings import Settings                                         # noqa: E402

MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


class FakeSpeech:
    backend = "fake"

    def __init__(self, heard="", fail_tts=False):
        self.heard, self.fail_tts, self.calls = heard, fail_tts, []

    def transcribe(self, audio, content_type, lang_hint=None):
        self.calls.append(("stt", content_type, lang_hint))
        return V.Transcript(text=self.heard, speech_lang="hi-IN", backend=self.backend)

    def speak(self, text, lang):
        self.calls.append(("tts", text, lang))
        if self.fail_tts:
            raise V.VoiceError("tts: down")
        return MP3, "audio/mpeg"


class Backends(unittest.TestCase):
    def test_language_map_covers_all_seven(self):
        for lang in ("en", "hi-Latn", "hi", "gu", "mr", "pa", "mwr"):
            self.assertIn(lang, V.SPEECH_LANG)
        self.assertEqual(V.speech_lang("mwr"), "hi-IN")            # Marwari is heard and spoken through Hindi
        self.assertEqual(V.speech_lang("gu"), "gu-IN")

    def test_make_voice(self):
        self.assertIsNone(V.make_voice({}))
        self.assertIsNone(V.make_voice({"VOICE": "sarvam"}))         # no key: off, with a warning
        self.assertEqual(V.make_voice({"VOICE": "sarvam", "SARVAM_API_KEY": "k"}).backend, "sarvam")
        self.assertEqual(V.make_voice({"VOICE": "selfhosted", "VOICE_SERVER_URL": "http://s:9000"}).backend, "selfhosted")

    def test_sarvam_request_shapes(self):
        seen = []

        def http(url, body, headers, timeout):
            seen.append((url, body, headers))
            if url.endswith("/speech-to-text"):
                return 200, "application/json", json.dumps({"transcript": "1 घंटा लेट", "language_code": "hi-IN"}).encode()
            return 200, "application/json", json.dumps({"audios": [base64.b64encode(MP3).decode()]}).encode()

        s = V.SarvamSpeech("key", http=http)
        tr = s.transcribe(b"OggS..", "audio/ogg; codecs=opus")
        self.assertEqual((tr.text, tr.speech_lang), ("1 घंटा लेट", "hi-IN"))
        url, body, headers = seen[0]
        self.assertEqual(headers["api-subscription-key"], "key")
        self.assertIn(b'name="language_code"\r\n\r\nunknown', body)    # first message: the model detects the language
        self.assertIn(b'name="model"\r\n\r\nsaaras:v3', body)
        self.assertIn(b'filename="voice.ogg"', body)
        audio, ctype = s.speak("नमस्ते", "mwr")
        self.assertEqual((audio, ctype), (MP3, "audio/mpeg"))
        p = json.loads(seen[1][1])
        self.assertEqual((p["target_language_code"], p["model"], p["output_audio_codec"]), ("hi-IN", "bulbul:v3", "mp3"))

    def test_sarvam_errors_become_voice_error(self):
        s = V.SarvamSpeech("key", http=lambda *a: (403, "application/json", b'{"error":"bad key"}'))
        with self.assertRaises(V.VoiceError):
            s.transcribe(b"x", "audio/ogg")
        with self.assertRaises(V.VoiceError):
            s.speak("x", "hi")

    def test_openrouter_request_shapes(self):
        seen = []

        def http(url, body, headers, timeout):
            seen.append((url, json.loads(body), headers))
            if url.endswith("/audio/transcriptions"):
                return 200, "application/json", json.dumps({"text": " 1 કલાક મોડું ", "language": "guj",
                                                             "usage": {"seconds": 2.1}}).encode()
            return 200, "audio/mpeg", MP3

        s = V.OpenRouterSpeech("sk-or-x", http=http)
        tr = s.transcribe(b"OggS..", "audio/ogg; codecs=opus")
        self.assertEqual((tr.text, tr.speech_lang, tr.backend), ("1 કલાક મોડું", "gu-IN", "openrouter"))
        url, body, headers = seen[0]
        self.assertEqual(url, "https://openrouter.ai/api/v1/audio/transcriptions")
        self.assertEqual(headers["Authorization"], "Bearer sk-or-x")
        self.assertEqual((body["model"], body["input_audio"]["format"], body["response_format"]),
                         ("elevenlabs/scribe-v2", "ogg", "verbose_json"))
        self.assertEqual(base64.b64decode(body["input_audio"]["data"]), b"OggS..")
        self.assertNotIn("language", body)                          # first message: the model detects the language
        s.transcribe(b"x", "audio/ogg", lang_hint="mwr")
        self.assertEqual(seen[1][1]["language"], "hi")              # ISO 639-1; Marwari through Hindi
        self.assertEqual(s.speak("ठीक है", "pa"), (MP3, "audio/mpeg"))
        body = seen[2][1]
        self.assertEqual((body["model"], body["response_format"], body["input"]), ("elevenlabs/eleven-v3", "mp3", "ठीक है"))
        self.assertEqual(body["provider"]["options"]["elevenlabs"]["language_code"], "pa")
        self.assertNotIn("voice", body)                             # provider default unless OPENROUTER_TTS_VOICE is set

    def test_openrouter_settings_and_errors(self):
        env = {"VOICE": "openrouter", "OPENROUTER_API_KEY": "k", "OPENROUTER_STT_MODEL": "openai/whisper-large-v3",
               "OPENROUTER_TTS_MODEL": "elevenlabs/eleven-v4", "OPENROUTER_TTS_VOICE": "v1", "OPENROUTER_DATA_COLLECTION": "deny"}
        s = V.make_voice(env)
        self.assertEqual((s.backend, s.stt_model, s.tts_model, s.voice, s.data_collection),
                         ("openrouter", "openai/whisper-large-v3", "elevenlabs/eleven-v4", "v1", "deny"))
        self.assertIsNone(V.make_voice({"VOICE": "openrouter"}))    # no key: voice stays off
        bad = V.OpenRouterSpeech("k", http=lambda *a: (402, "application/json", b'{"error":{"code":402,"message":"Insufficient credits"}}'))
        with self.assertRaisesRegex(V.VoiceError, "402 Insufficient credits"):
            bad.transcribe(b"x", "audio/ogg")
        with self.assertRaises(V.VoiceError):
            bad.speak("x", "hi")
        for code, want in (("hin", "hi-IN"), ("Hindi", "hi-IN"), ("mr", "mr-IN"), ("pa-IN", "pa-IN"), ("eng", "en-IN"), ("xx", None)):
            self.assertEqual(V._bcp47(code), want, code)

    def test_selfhosted_shapes(self):
        seen = []

        def http(url, body, headers, timeout):
            seen.append((url, headers))
            if url.endswith("/stt"):
                return 200, "application/json", b'{"transcript": "reaching 11:30", "language_code": "en-IN"}'
            return 200, "audio/mpeg", MP3

        s = V.SelfHostedSpeech("http://speech:9000/", token="t", http=http)
        self.assertEqual(s.transcribe(b"x", "audio/ogg", lang_hint="en").text, "reaching 11:30")
        self.assertEqual(s.speak("ok", "gu"), (MP3, "audio/mpeg"))
        self.assertEqual(seen[0][0], "http://speech:9000/stt")
        self.assertEqual(seen[0][1]["Authorization"], "Bearer t")

    def test_spoken_clock_times_become_digits(self):
        cases = {"reaching by eleven thirty": "reaching by 11:30", "half past eleven": "11:30", "quarter to twelve": "11:45",
                 "eleven o clock": "11:00", "साढ़े ग्यारह बजे तक": "11:30 तक", "पौने बारह बजे": "11:45", "डेढ़ बजे": "1:30",
                 "saade gyarah baje tak": "11:30 tak", "સાડા અગિયાર વાગ્યે": "11:30", "साडे अकरा वाजता": "11:30",
                 "ਸਾਢੇ ਗਿਆਰਾਂ ਵਜੇ ਤੱਕ": "11:30 ਤੱਕ", "11 बजे तक": "11:00 तक"}
        for said, want in cases.items():
            self.assertEqual(V.spoken_times(said), want, said)
        for keep in ("एक घंटा लेट", "साढ़े तीन घंटे लेट", "do ghante late", "one thirty minutes", "1 hour late"):
            self.assertEqual(V.spoken_times(keep), keep)              # durations are not clock times

    def test_spoken_text_drops_booking_codes(self):
        self.assertEqual(V.spoken("Booked D3 at 11:30 AM. Ref APT-SH-1042."), "Booked D3 at 11:30 AM. Ref .")


from test_twilio_channel import World, C03, C12, settings, creq, form, need_twilio          # noqa: E402
SAFETY_PHONE = C12


@need_twilio
class Channels(unittest.TestCase):
    def test_twilio_captures_media_url_and_sends_audio(self):
        from setuhaul.channels import get_channel
        ch = get_channel(settings(), client=object())
        p = form("SM1", C03, "", NumMedia="1", MediaContentType0="audio/ogg",
                 MediaUrl0="https://api.twilio.com/2010-04-01/Accounts/AC1/Messages/SM1/Media/ME1")
        m = ch.parse_inbound(creq(p))[0]
        self.assertEqual((m.type, m.media_types, m.media_urls[0][:24]), ("media", ["audio/ogg"], "https://api.twilio.com/2"))
        msg = OutboundMessage(to_e164=C03, kind="media", media_url="https://h/media/abc.mp3")
        params = ch.build_params(msg)
        self.assertEqual(params["media_url"], ["https://h/media/abc.mp3"])
        self.assertNotIn("body", params)
        self.assertEqual(sent_kind(params, msg), "media")

    def test_twilio_refuses_media_from_other_hosts(self):
        from setuhaul.channels import get_channel
        ch = get_channel(settings(), client=object())
        for bad in ("http://api.twilio.com/x", "https://evil.example/x", "https://twilio.com.evil.example/x", "file:///etc/passwd"):
            with self.assertRaises(ValueError):
                ch.fetch_media(bad, 1000)


@need_twilio
class EndToEnd(unittest.TestCase):
    """A driver sends a voice note through the real Twilio adapter, channel service and engine."""

    def world(self, speech):
        w = World(self, media_base_url="https://demo.example", voice="fake")
        w.svc.voice = speech
        w.ch.fetch_media = lambda url, max_bytes: (b"OggS" + b"\x00" * 200, "audio/ogg")
        return w

    def note(self, w, frm=C03, hm="09:20"):
        return w.post(frm, "", hm=hm, NumMedia="1", MediaContentType0="audio/ogg",
                      MediaUrl0="https://api.twilio.com/2010-04-01/Accounts/AC1/Messages/SMx/Media/ME1")

    def test_voice_note_gets_text_options_and_spoken_reply(self):
        speech = FakeSpeech(heard="1 घंटा लेट हूँ")
        w = self.world(speech)
        tr = self.note(w)
        self.assertEqual(tr["status"], "REPLIED", tr)
        self.assertTrue(tr["voice_in"]["ok"])
        self.assertEqual(tr["lang"], "hi")                              # the transcript decides the language, as typed text does
        d = w.deliveries(C03)
        replies = [x for x in d if x["kind"] == "DRIVER_REPLY"]
        spoken = [x for x in d if x["kind"] == "DRIVER_VOICE_REPLY"]
        self.assertTrue(replies[-1]["payload"]["body"])                 # the normal written reply, with its options
        self.assertEqual(len(spoken), 1)
        self.assertGreater(d.index(spoken[0]), d.index(replies[-1]))    # voice goes after the text
        url = spoken[0]["payload"]["media_url"]
        self.assertRegex(url, r"^https://demo\.example/media/[0-9a-f]{32}\.mp3$")
        self.assertEqual(speech.calls[-1][2], "hi")                     # spoken in the driver's language
        token = url.rsplit("/", 1)[1].split(".")[0]
        now = w.clock.real_now()
        self.assertEqual(V.load_audio(w.con, token, now)[0], MP3)
        self.assertIsNone(V.load_audio(w.con, "../../etc/passwd", now))
        body = w.con.execute("SELECT body FROM wa_inbound ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertEqual(body, "[voice] 1 घंटा लेट हूँ")
        w.drain()                                                       # the audio really goes out as a Twilio media message
        sent = [c for c in w.fake.calls if "media_url" in c]
        self.assertEqual(sent[-1]["media_url"], [url])

    def test_unclear_voice_note(self):
        w = self.world(FakeSpeech(heard=""))
        tr = self.note(w)
        self.assertEqual(tr["status"], "VOICE_UNCLEAR")
        self.assertIn(R.t("voice_unclear", "en"), [x["payload"]["body"] for x in w.deliveries(C03)])

    def test_tts_failure_still_sends_the_text_reply(self):
        w = self.world(FakeSpeech(heard="late by 1 hour", fail_tts=True))
        tr = self.note(w)
        self.assertEqual(tr["status"], "REPLIED")
        self.assertFalse(tr["voice_out"]["ok"])
        self.assertFalse([x for x in w.deliveries(C03) if x["kind"] == "DRIVER_VOICE_REPLY"])

    def test_voice_off_keeps_please_type(self):
        w = self.world(None)
        self.assertEqual(self.note(w)["status"], "UNSUPPORTED_TYPE")

    def test_spoken_safety_message_escalates(self):
        w = self.world(FakeSpeech(heard="accident ho gaya, driver ko chot lagi hai"))
        tr = self.note(w, frm=SAFETY_PHONE)
        self.assertTrue(any(t.startswith("escalate:SAFETY") for t in tr.get("tools") or []), tr)

    def test_spoken_time_in_words_is_read(self):
        w = self.world(FakeSpeech(heard="एक घंटा लेट हूँ, साढ़े ग्यारह बजे तक पहुँचूँगा"))
        tr = self.note(w)
        self.assertEqual(tr["voice_in"]["normalised"], "एक घंटा लेट हूँ, 11:30 तक पहुँचूँगा")
        self.assertEqual(tr["status"], "REPLIED")

    def test_spoken_stop_opts_out(self):
        w = self.world(FakeSpeech(heard="Stop."))
        self.assertEqual(self.note(w)["status"], "OPTED_OUT")

    def test_typed_turn_unchanged(self):
        speech = FakeSpeech(heard="should not be used")
        w = self.world(speech)
        tr = w.post(C03, "late by 1 hour", hm="09:20")
        self.assertNotIn("voice_in", tr)
        self.assertEqual(speech.calls, [])
        self.assertFalse([x for x in w.deliveries(C03) if x["kind"] == "DRIVER_VOICE_REPLY"])


class Replies(unittest.TestCase):
    def test_voice_unclear_in_seven_languages(self):
        seen = set()
        for lang in ("en", "hi-Latn", "hi", "gu", "mr", "pa", "mwr"):
            t = R.t("voice_unclear", lang)
            self.assertTrue(t)
            self.assertEqual(R.guard(t, {}), [])
            seen.add(t)
        self.assertEqual(len(seen), 7)


if __name__ == "__main__":
    unittest.main()
