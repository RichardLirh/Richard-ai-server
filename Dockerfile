FROM python:3.10-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ffmpeg libopus0 libsndfile1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
# CPU-only PyTorch avoids downloading CUDA runtimes for this local demo.
RUN pip install torch==2.2.2 torchaudio==2.2.2 --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt
COPY . .
EXPOSE 9000 9003
CMD ["python", "app.py"]
