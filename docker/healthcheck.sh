#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# LogPilot Container Health Check
# Checks the FastAPI /healthz endpoint.
# Exit 0 = healthy, Exit 1 = unhealthy
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

# Try FastAPI health endpoint (primary)
if curl -sf http://localhost:8000/healthz > /dev/null 2>&1; then
    exit 0
fi

# Fallback: check if uvicorn process is running
if pgrep -f "uvicorn" > /dev/null 2>&1; then
    exit 0
fi

exit 1
