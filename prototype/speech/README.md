# SetuHaul speech server (self-hosted voice)

Voice notes from drivers are turned into text, run through the **unchanged** rules engine, and the reply is
sent back as text with option buttons **and** as a voice note in the driver's language. The speech parts are
dedicated speech models, not an LLM.

| Job | Model | Licence | Languages |
|---|---|---|---|
| Speech-to-text, Indian languages | `ai4bharat/indic-conformer-600m-multilingual` | MIT | 22 incl. Hindi, Gujarati, Marathi, Punjabi |
| Speech-to-text, English + language ID | `openai/whisper-small` | MIT | English; detects hi/gu/mr/pa/en on a first message |
| Text-to-speech | `ai4bharat/indic-parler-tts` | Apache-2.0 | Hindi, Gujarati, Marathi, English; Punjabi works (unofficial) |

Marwari and Hinglish: heard and spoken through Hindi (`hi-IN`). Meta MMS was considered and rejected:
its licence (CC-BY-NC) does not allow commercial use.

**Status:** written against the model cards; not run in the build environment (gated models, GPU needed).
Do the smoke test below before setting `VOICE=selfhosted` in production.

## Run

1. Accept the terms on both AI4Bharat model pages on Hugging Face; create a read token (`HF_TOKEN`).
2. On a GPU box (NVIDIA, 24 GB is comfortable; an L4 / g5.xlarge is enough):
   ```
   docker build -t setuhaul-speech prototype/speech
   docker run -d --gpus all -p 9000:9000 -e HF_TOKEN=$HF_TOKEN -e VOICE_SERVER_TOKEN=$TOKEN \
       -v hf:/root/.cache/huggingface setuhaul-speech
   ```
3. Smoke test:
   ```
   curl -s localhost:9000/healthz
   curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
        -d '{"text":"आपका स्लॉट 11:30 बजे D3 पर है।","language":"hi-IN"}' localhost:9000/tts -o reply.mp3
   curl -s -H "Authorization: Bearer $TOKEN" -F file=@reply.mp3 -F language=auto localhost:9000/stt
   ```
4. In the app's environment: `VOICE=selfhosted`, `VOICE_SERVER_URL=http://<box>:9000`, the token in SSM
   `/setuhaul/VOICE_SERVER_TOKEN`. Keep port 9000 private (security group: the app host only).

## The other backend

`VOICE=sarvam` uses Sarvam AI's hosted speech models (Saaras for speech-to-text, Bulbul for text-to-speech).
Fastest to pilot (just an API key, `SARVAM_API_KEY`); audio goes to Sarvam's servers.
