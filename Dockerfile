FROM python:3.12-slim

# docker CLI (the shim spawns ephemeral runner containers via /var/run/docker.sock)
RUN apt-get update && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src

# concurrency-1 event-driven shim; subscribes + spawns runners.
CMD ["python", "-m", "src.main"]
