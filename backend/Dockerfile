FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MYRA_MODELS_DIR=/app/backend/models

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake curl git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu \
      -r requirements.txt

COPY . /app/backend
RUN mkdir -p /app/database /app/backend/models /app/backend/logs && \
    chmod +x scripts/*.sh

EXPOSE 8000
CMD ["bash", "scripts/start.sh"]
