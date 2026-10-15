FROM python:3.12-slim

# Install FFmpeg & Node.js — required for media processing and YouTube n-sig challenge solving
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create the downloads placeholder directory
RUN mkdir -p downloads

EXPOSE 10000

CMD gunicorn app:app \
    --bind 0.0.0.0:$PORT \
    --workers 1 \
    --worker-class gthread \
    --threads 8 \
    --timeout 300
