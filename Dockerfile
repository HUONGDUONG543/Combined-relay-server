FROM python:3.11-slim

# ffmpeg is a system package, not a pip package - combined_relay_server.py
# shells out to it directly for transcoding
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "combined_relay_server.py"]
