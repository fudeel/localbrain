# One container holding both the brain and Ollama, so hardware limits are measured correctly.
# On first start it installs Ollama onto the /data volume, which is about a 1.5 GB download.
FROM python:3.15-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends zstd ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY brain /app/brain
WORKDIR /app

RUN useradd --create-home --uid 1000 brain && mkdir /data && chown brain /data
USER brain

ENV BRAIN_DATA_DIR=/data/brain \
    BRAIN_HOST=0.0.0.0 \
    BRAIN_AUTO_INSTALL_OLLAMA=true \
    OLLAMA_MODELS=/data/models

VOLUME /data
EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8484/api/v1/health', timeout=4)"]

CMD ["python", "-m", "brain", "serve"]
