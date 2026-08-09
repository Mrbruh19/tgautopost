FROM python:3.12-slim
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py batch_export.py queue_seed.json content_seed.json ./
ENV PORT=8000
CMD ["sh", "-c", "uvicorn batch_export:app --host 0.0.0.0 --port ${PORT}"]
