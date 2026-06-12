# MYMevert Backend

Backend API untuk konversi video YouTube ke MP4/MP3 dan konversi file lokal ke MP3.

## Requirements

- Python 3.8+
- FFmpeg (diperlukan untuk penggabungan video/audio)
- yt-dlp

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Buat file `.env` atau set environment variable:

```
FFMPEG_PATH=/path/to/ffmpeg/bin  # Optional: default uses system PATH
JOB_TTL_MINUTES=60  # Optional: default 60 minutes
ALLOWED_ORIGINS=http://localhost:3000,https://mymevert.id,https://mymevert-id.vercel.app
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

## Railway Deployment

1. Connect repository ke Railway
2. Railway akan otomatis detect Dockerfile
3. Set environment variables:
   - `ALLOWED_ORIGINS` = `http://localhost:3000,https://mymevert.id`
   - `FFMPEG_PATH` (optional)
   - `JOB_TTL_MINUTES` (optional, default 60)
4. Deploy

## Frontend Connection

Frontend (`mymevert.id`) harus connect ke backend Railway URL:

```
https://mymevert-backend-production.up.railway.app
```

API endpoints yang dipanggil frontend:
- `POST /convert/yt-mp4/start`
- `POST /convert/yt-mp3/start`
- `POST /convert/local-mp3/start`
- `GET /convert/status/{job_id}`
- `GET /convert/download/{job_id}`

Contoh base URL di frontend:
```javascript
const API_URL = "https://mymevert-backend-production.up.railway.app";
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check |
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