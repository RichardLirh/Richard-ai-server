FROM python:3.10-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
    build-essential ffmpeg libopus0 libsndfile1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
# CPU-only PyTorch avoids downloading CUDA runtimes for this local demo.
RUN pip install --upgrade pip setuptools wheel
RUN pip install torch==2.2.2+cpu torchaudio==2.2.2+cpu --extra-index-url https://download.pytorch.org/whl/cpu
# The pinned Sherpa 1.12.17 wheels are archived on the upstream wheel index.
RUN pip install -r requirements.txt --find-links https://k2-fsa.github.io/sherpa/onnx/cpu.html
COPY . .
EXPOSE 9000 9003
CMD ["python", "app.py"]
