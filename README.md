# MYMevert Backend

Backend API untuk konversi video YouTube ke MP4/MP3 dan konversi file lokal ke MP3. Saat ini dijalankan secara lokal dan diekspos ke internet via ngrok tunnel.

## Requirements

- Python 3.12+
- FFmpeg (untuk penggabungan video/audio dan ekstraksi audio)
- yt-dlp (untuk mengunduh dari YouTube)
- Node.js (untuk JavaScript runtime yt-dlp - diperlukan untuk YouTube)

## Installation

```bash
pip install -r requirements.txt
```

## Configuration

Buat file `.env` atau set environment variable:

```
FFMPEG_PATH=/path/to/ffmpeg/bin          # Optional: default pakai system PATH
MAX_CONCURRENT_JOBS=1                     # Default 1 - atur sesuai kapasitas CPU/RAM
JOB_TTL_MINUTES=30                        # Default 30 - TTL cleanup job
CONVERSION_TIMEOUT=900                    # Default 900s (15 menit) - timeout per konversi
MAX_UPLOAD_SIZE_MB=100                    # Default 100MB - batas ukuran upload lokal
AUDIO_BITRATE_KBPS=192                    # Default 192k - bitrate MP3
ALLOWED_ORIGINS=http://localhost:3000,https://mymevert.id,https://mymevert-id.vercel.app
PORT=8000                                 # Port untuk uvicorn
```

## Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Menjalankan Lokal via ngrok

Untuk mengekspos backend ke internet:

1. **Terminal 1** - Jalankan uvicorn:
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```

2. **Terminal 2** - Jalankan ngrok tunnel:
   ```bash
   ./ngrok.exe http --url=<domain-static> 8000
   ```
   Ganti `<domain-static>` dengan domain static kamu (misalnya `stauroscopically-fluorescent-shelli.ngrok-free.dev`).

3. Update `NEXT_PUBLIC_API_URL` di Vercel dengan URL ngrok kamu (misalnya `https://stauroscopically-fluorescent-shelli.ngrok-free.dev`).

## Docker

```bash
docker build -t mymevert-backend .
docker run -p 8000:8000 mymevert-backend
```

## Frontend Connection

Setelah ngrok tunnel aktif, update URL backend di frontend:

```
https://<domain-ngrok>.ngrok-free.dev
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
| `/` | GET | Health check sederhana |
| `/health` | GET | Health check dengan status ffmpeg & yt-dlp |
| `/convert/yt-mp4/start` | POST | Mulai konversi YouTube ke MP4 |
| `/convert/yt-mp3/start` | POST | Mulai konversi YouTube ke MP3 |
| `/convert/local-mp3/start` | POST | Upload file lokal, konversi ke MP3 |
| `/convert/status/{job_id}` | GET | Polling status konversi |
| `/convert/download/{job_id}` | GET | Download hasil konversi |

## Request Body (YouTube)

```json
{
  "url": "https://www.youtube.com/watch?v=...",
  "resolution": "720"
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
  "progress": 45,
  "step": "downloading",
  "status": "processing",
  "filename": null,
  "tmp_dir": null,
  "error": null,
  "created_at": "2024-01-01T00:00:00+00:00"
}
```

## Notes

- Job akan otomatis dibersihkan setelah `JOB_TTL_MINUTES` (default 30 menit)
- File temp akan dihapus setelah download selesai
- Error jobs akan otomatis dibersihkan dari memory
- Maksimal `MAX_CONCURRENT_JOBS` job berjalan bersamaan (default 1)
- Progress diupdate secara real-time dari yt-dlp dan ffmpeg
- Graceful shutdown dengan SIGTERM handling
