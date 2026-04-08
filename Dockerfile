FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt \
        "httpx-retries>=0.4.6" \
        "unplayplay>=0.0.1" \
        "librespot>=0.0.1"

COPY pyproject.toml ./pyproject.toml
COPY README.md ./README.md
COPY votify ./votify

RUN useradd -m -u 10001 appuser \
    && mkdir -p /config /data \
    && chown -R appuser:appuser /app /config /data

USER appuser

CMD ["python", "-m", "votify.spotify_telegram_bot.main", "--config", "/config/spotify_bot.config.toml", "--log-level", "INFO"]
