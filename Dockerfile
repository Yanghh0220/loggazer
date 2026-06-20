# ═══════════════════════════════════════════════════════════════════════
# LogPilot / LogGazer — Multi-stage Docker Build
# ═══════════════════════════════════════════════════════════════════════
#
# Build:  docker build -t logpilot:latest .
# Run:    docker run -p 8000:8000 -p 8501:8501 --env-file .env logpilot:latest
#
# Build args:
#   INCLUDE_QDRANT=true   Install qdrant-client for embedded mode (default)
#   INCLUDE_QDRANT=false  Skip qdrant-client, connect to external Qdrant
# ═══════════════════════════════════════════════════════════════════════

# ── Stage 1: Builder ──────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# Build dependencies (only needed for compiling wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user early for --user pip install
RUN useradd -m -u 1000 builder

# Copy requirements first for layer caching
COPY requirements.txt /tmp/requirements.txt

# Install Python dependencies to /home/builder/.local
RUN pip install --user --no-cache-dir --upgrade pip && \
    pip install --user --no-cache-dir -r /tmp/requirements.txt

# Pre-download sentence-transformers model (all-MiniLM-L6-v2)
# This bakes the ~90MB model into the image so it never downloads at runtime
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# ── Stage 2: Runtime ──────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Metadata
LABEL org.opencontainers.image.title="LogPilot"
LABEL org.opencontainers.image.description="AI-powered CI/CD log failure analyzer"
LABEL org.opencontainers.image.version="2.0.0"
LABEL org.opencontainers.image.authors="LogPilot Team"

# Build-time argument: include embedded Qdrant?
ARG INCLUDE_QDRANT=true

# Create non-root user (UID 1000)
RUN useradd -m -u 1000 logpilot && \
    mkdir -p /app /home/logpilot/.cache && \
    chown -R logpilot:logpilot /app /home/logpilot

# Copy Python packages from builder
COPY --from=builder /home/builder/.local /home/logpilot/.local

# Copy pre-downloaded sentence-transformers model
COPY --from=builder /root/.cache/torch/sentence_transformers /home/logpilot/.cache/torch/sentence_transformers

# Set PATH to include user-installed packages
ENV PATH="/home/logpilot/.local/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Application environment defaults
ENV LOGPILOT_CACHE_DIR=/home/logpilot/.cache
ENV QDRANT_MODE=memory
ENV RATE_LIMITER_BACKEND=memory

# Copy application code
WORKDIR /app
COPY --chown=logpilot:logpilot . /app

# Copy healthcheck script
COPY --chown=logpilot:logpilot docker/healthcheck.sh /app/docker/healthcheck.sh
RUN chmod +x /app/docker/healthcheck.sh

# Switch to non-root user
USER logpilot

# Expose ports
# 8000: FastAPI backend
# 8501: Streamlit frontend
EXPOSE 8000 8501

# Health check — uses FastAPI /healthz endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD /app/docker/healthcheck.sh

# Default command: start the FastAPI backend
# Override with CMD or docker-compose to start Streamlit or both
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
