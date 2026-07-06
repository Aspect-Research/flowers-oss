# Demo WORKER image: one throwaway flowers instance per demo session.
# Build context is the REPO ROOT:
#   flyctl deploy -a flowers-demo-workers --build-only --push \
#     --dockerfile deploy/demo/Dockerfile --image-label worker .
#
# The launcher creates a Machine from this image per session with env:
#   OPENROUTER_API_KEY        the visitor's key (never stored anywhere else)
#   FLOWERS_DEMO_TOKEN        per-session secret checked by the wrapper
#   FLOWERS_ROLE_CONFIG_JSON  optional free-models rerouting
#
# State (sqlite) lives in /tmp — it is MEANT to die with the session.

FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY flowers ./flowers
RUN pip install --no-cache-dir ".[web]"

COPY deploy/demo/wrapper.py ./wrapper.py

ENV FLOWERS_DB=/tmp/flowers.db \
    FLOWERS_TIMERS_DB=/tmp/flowers_timers.db \
    PYTHONUNBUFFERED=1

EXPOSE 8000
# Bind "::" — the launcher reaches workers over Fly's private network, which
# is IPv6 (6PN); an IPv4-only 0.0.0.0 bind refuses those connections.
CMD ["python", "-m", "uvicorn", "wrapper:app", "--host", "::", "--port", "8000"]
