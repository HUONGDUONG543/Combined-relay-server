FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg nodejs ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /opt/pw

COPY combined_relay_server.py start.sh ./
RUN chmod +x start.sh && chown -R 1000:1000 /app
USER 1000

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["./start.sh"]
