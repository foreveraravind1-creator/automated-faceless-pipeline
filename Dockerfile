# ============================================================
# Dockerfile
# Multi-stage build for Cloud Run deployment
# ============================================================

# ── Stage 1: dependency installer ────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools needed by some Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: runtime image ───────────────────────────────────
FROM python:3.12-slim AS runtime

# Python stays on 3.12: Kokoro-82M does not support 3.13.
# ffmpeg renders the reel. espeak-ng is required by Kokoro. fonts-dejavu-core
# supplies the caption font. libsndfile1 lets soundfile write the wav.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    espeak-ng \
    fonts-dejavu-core \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder stage
COPY --from=builder /install /usr/local

# Copy application source
COPY agents/   agents/
COPY core/     core/
COPY webhook/  webhook/
COPY niches/   niches/
COPY main.py   .

# Cloud Run injects PORT env var (default 8080)
ENV PORT=8080
# appuser cannot write a root-owned /app/output. Render into /tmp/output.
ENV OUTPUT_DIR=/tmp/output

# Expose for local testing
EXPOSE 8080

# Non-root user. chown the job directory after the user exists.
RUN useradd --create-home appuser \
    && mkdir -p /tmp/output \
    && chown appuser:appuser /tmp/output
USER appuser

CMD exec uvicorn webhook.server:app --host 0.0.0.0 --port ${PORT} --workers 1
