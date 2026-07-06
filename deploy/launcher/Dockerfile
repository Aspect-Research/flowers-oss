# Demo LAUNCHER image. Build context is this directory:
#   flyctl deploy -c deploy/launcher/fly.toml   (from the repo root)
FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir "starlette>=0.37" "uvicorn>=0.30" "httpx>=0.27"
COPY launcher.py ./launcher.py

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "launcher:app", "--host", "0.0.0.0", "--port", "8080"]
