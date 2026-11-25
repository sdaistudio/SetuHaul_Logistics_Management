# GPU image for the SetuHaul speech server. Build and run on the GPU box (e.g. g5.xlarge / L4):
#   docker build -t setuhaul-speech prototype/speech
#   docker run --gpus all -p 9000:9000 -e HF_TOKEN=... -e VOICE_SERVER_TOKEN=... -v hf:/root/.cache/huggingface setuhaul-speech
FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
EXPOSE 9000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9000", "--workers", "1"]
