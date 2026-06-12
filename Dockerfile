FROM python:3.11-slim

WORKDIR /app

# Install ffmpeg and nodejs (for yt-dlp JS runtime)
RUN apt-get update && apt-get install -y ffmpeg nodejs && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
EXPOSE 8000

CMD ["python", "start.py"]