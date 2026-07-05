# Optional: run flowers locally in a container (the README quickstart just uses `flowers serve`).
#
# Serves the ASGI app `flowers.app:app` (the single build_app() factory). One worker only: each
# worker process would run its own timer poller + crash-recovery sweep against the same sqlite files.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the package + the web extra (Starlette/uvicorn). Add ,postgres / ,browser if you wire those
# optional adapters (the browser runs in Browserbase's CLOUD over CDP, so no local chromium is needed).
COPY pyproject.toml README.md ./
COPY flowers ./flowers
RUN pip install --upgrade pip && pip install ".[web]"

# Least privilege: run as a non-root user.
RUN useradd --create-home flowers && chown -R flowers /app
USER flowers

ENV FLOWERS_TICK_SECONDS=15
EXPOSE 8000

# Liveness via the unauthenticated /health endpoint (web.py). /ready additionally probes the store.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"

# The console script pins workers=1 itself. Pass your provider keys via the environment
# (`docker run --env-file .env ...`), NOT baked into the image. There is no auth, so don't expose
# the published port to an untrusted network.
CMD ["flowers", "serve", "--host", "0.0.0.0", "--port", "8000"]
