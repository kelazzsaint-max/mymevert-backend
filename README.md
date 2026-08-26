# MYMevert Backend

Backend API untuk konversi video YouTube ke MP4/MP3 dan konversi file lokal ke MP3. Dirancang untuk berjalan di Render free tier (512MB RAM, 0.1 CPU).

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
MAX_CONCURRENT_JOBS=1                     # Default 1 (free tier) - atur 2-3 untuk RAM lebih besar
JOB_TTL_MINUTES=30                        # Default 30 - TTL cleanup job
CONVERSION_TIMEOUT=300                    # Default 300s - timeout per konversi
MAX_UPLOAD_SIZE_MB=100                    # Default 100MB - batas ukuran upload lokal
AUDIO_BITRATE_KBPS=192                    # Default 192k - bitrate MP3 (turunkan untuk CPU lemah)
ALLOWED_ORIGINS=http://localhost:3000,https://mymevert.id,https://mymevert-id.vercel.app
PORT=8000                                 # Render inject otomatis
```

## Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Atau menggunakan start.py:

```bash
python start.py
```

## Docker

```bash
docker build -t mymevert-backend .
docker run -p 8000:8000 mymevert-backend
```

## Deploy to Render

### Option 1: Using render.yaml (recommended)

Commit `render.yaml` ke repo, kemudian di Render dashboard:
1. Buat Web Service baru
2. Pilih repomu
3. Render akan otomatis mendeteksi `render.yaml`
4. Pilih plan **Free** (512MB RAM)

### Option 2: Manual setup

1. Buat Web Service di Render dashboard
2. Pilih repomu
3. Set Environment ke **Docker**
4. Build Command: *(Render menggunakan Dockerfile)*
5. Start Command: *(Render menggunakan CMD dari Dockerfile)*
6. Pilih plan **Free**

### Render Free Tier Limitations

| Limitation | Impact | Solution |
|---|---|---|
| 512MB RAM | Video panjang bisa OOM | Set `MAX_CONCURRENT_JOBS=1`, `CONVERSION_TIMEOUT=300` |
| 0.1 CPU (shared) | Konversi lambat | Set `AUDIO_BITRATE_KBPS=128` untuk konversi lebih cepat |
| Spin down 15 min | Cold start 30-60s | Upgrade ke Starter ($7/mo) untuk always-on |
| Ephemeral disk | File temp hilang pada redeploy | Semua file diproses di memory/tmp, dihapus setelah download |

### Environment Variables di Render

Set di Render dashboard (bukan di repo):
- `COOKIES_FILE` - path ke cookies.txt untuk YouTube auth (opsional)

## Frontend Connection

Setelah deploy, dapatkan URL backend dan update di frontend:

```
https://<backend-service-url>.onrender.com
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
- Maksimal `MAX_CONCURRENT_JOBS` job berjalan bersamaan (default 1 untuk free tier)
- Progress diupdate secara real-time dari yt-dlp dan ffmpeg
- Graceful shutdown dengan SIGTERM handling
