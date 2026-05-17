# Runtime Manager — FastAPI service above OpenSandbox.
#
# Build context is the project root so we can copy `harness/` (the Manager
# imports SandboxClient from there).

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends tini curl && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        "fastapi>=0.110,<1" \
        "uvicorn[standard]>=0.27,<1" \
        "pydantic>=2.5,<3" \
        "requests>=2.32,<3"

# Copy just what the Manager needs from the harness package
COPY harness/__init__.py             /app/harness/__init__.py
COPY harness/sandbox.py              /app/harness/sandbox.py
COPY harness/manager/                /app/harness/manager/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    AI_HARNESS_INSIDE_COMPOSE=1 \
    MANAGER_HOST=0.0.0.0 \
    MANAGER_PORT=8090

EXPOSE 8090

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -fsS http://localhost:8090/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", \
            "uvicorn", "harness.manager.main:app", \
            "--host", "0.0.0.0", "--port", "8090"]
