# MYMevert Backend

Backend API untuk konversi video YouTube ke MP4/MP3 dan konversi file lokal ke MP3.

## Requirements

- Python 3.12+
- FFmpeg (diperlukan untuk penggabungan video/audio)
- yt-dlp
- Node.js (untuk JavaScript runtime yt-dlp)

## Upgrade Changelog

- Python 3.11 → 3.12
- FastAPI 0.100+ → 0.111+ (lifespan replacement for on_event)
- Uvicorn 0.23+ → 0.30+ (standard extras)
- yt-dlp 2023+ → 2024.7.1+ (YouTube compatibility)
- python-multipart 0.0.6+ → 0.0.9+
- Fixed deprecated `text=True` in subprocess
- Timezone-aware datetime handling

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Buat file `.env` atau set environment variable:

```
FFMPEG_PATH=/path/to/ffmpeg/bin  # Optional: default uses system PATH
JOB_TTL_MINUTES=60  # Optional: default 60 minutes
MAX_CONCURRENT_JOBS=2  # Optional: default 2
ALLOWED_ORIGINS=http://localhost:3000,https://mymevert.id,https://mymevert-id.vercel.app
COOKIES_FILE=cookies.txt  # Optional: for YouTube authentication
```

## Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Docker

```bash
docker build -t mymevert-backend .
docker run -p 8000:8000 mymevert-backend
```

## Deployment

Siap deploy ke platform pilihan kamu. Pastikan Dockerfile sudah ikut di-repo dan environment variables sudah diset di platform deployer.

## Frontend Connection

Setelah deploy, dapatkan URL backend dan update di frontend:

```
https://<backend-service-url>
```

API endpoints yang dipanggil frontend:
- `POST /convert/yt-mp4/start`
- `POST /convert/yt-mp3/start`
- `POST /convert/local-mp3/start`
- `GET /convert/status/{job_id}`
- `GET /convert/download/{job_id}`

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check |
| `/health` | GET | Detailed health check with dependency status |
| `/convert/yt-mp4/start` | POST | Mulai konversi YouTube ke MP4 |
| `/convert/yt-mp3/start` | POST | Mulai konversi YouTube ke MP3 |
| `/convert/local-mp3/start` | POST | Mulai konversi file lokal ke MP3 |
| `/convert/status/{job_id}` | GET | Cek status konversi |
| `/convert/download/{job_id}` | GET | Download hasil konversi |

## Request Body (YouTube)

```json
{
  "url": "https://www.youtube.com/watch?v=...",
  "resolution": "720"  // Optional: default 720p
}
```

## Response

### Start conversion
```json
{
  "job_id": "uuid"
}
```

### Status
```json
{
  "progress": 0,
  "step": "queued",
  "status": "pending",
  "filename": null,
  "tmp_dir": null,
  "error": null,
  "created_at": "2024-01-01T00:00:00"
}
```

## Notes

- Job akan otomatis dibersihkan setelah `JOB_TTL_MINUTES`
- File temp akan dihapus setelah download selesai
- Error jobs akan otomatis dibersihkan dari memory
- Maksimal `MAX_CONCURRENT_JOBS` job berjalan bersamaan (default 2)