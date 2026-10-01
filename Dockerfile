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

# ffmpeg is required by MoviePy at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
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

# Cloud Run writes to /tmp for ephemeral storage;
# our output/ dir is created at runtime per-job.
RUN mkdir -p output

# Cloud Run injects PORT env var (default 8080)
ENV PORT=8080

# Expose for local testing
EXPOSE 8080

# Non-root user for security
RUN useradd --create-home appuser
USER appuser

CMD exec uvicorn webhook.server:app --host 0.0.0.0 --port ${PORT} --workers 1
