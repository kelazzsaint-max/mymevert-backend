import time
import os
import tempfile
import subprocess
import shutil
import uuid
import asyncio
import logging
import signal
import re
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from urllib.parse import quote
from pydantic import BaseModel
from typing import Dict, Optional, Callable
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from starlette.exceptions import HTTPException as StarletteHTTPException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mymevert")

PORT = int(os.environ.get("PORT", "8000"))
FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "")
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "1"))
JOB_TTL_MINUTES = int(os.environ.get("JOB_TTL_MINUTES", "30"))
CONVERSION_TIMEOUT = int(os.environ.get("CONVERSION_TIMEOUT", "900"))
MAX_UPLOAD_SIZE_MB = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "100"))
MAX_UPLOAD_SIZE = MAX_UPLOAD_SIZE_MB * 1024 * 1024
AUDIO_BITRATE_KBPS = os.environ.get("AUDIO_BITRATE_KBPS", "192")

conversion_status: Dict[str, dict] = {}
job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
_ffmpeg_durations: Dict[str, float] = {}
_shutting_down = False


def _try_acquire_job_slot() -> bool:
    if job_semaphore._value > 0:
        job_semaphore._value -= 1
        return True
    return False
    try:
        return job_semaphore.acquire(blocking=False)
    except ValueError:
        return False


def _release_job_slot():
    try:
        job_semaphore.release()
    except ValueError:
        pass


def _cleanup_job(job_id: str, tmp_dir: Optional[str] = None):
    """Cleanup job status, temp directory, and ffmpeg duration cache."""
    conversion_status.pop(job_id, None)
    _ffmpeg_durations.pop(job_id, None)
    if tmp_dir:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info(f"Cleaned up job {job_id} tmp_dir {tmp_dir}")


async def _periodic_cleanup():
    while not _shutting_down:
        await asyncio.sleep(120)
        if _shutting_down:
            break
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=JOB_TTL_MINUTES)
        to_remove = [
            job_id for job_id, status in conversion_status.items()
            if status.get("completed_at") and
            datetime.fromisoformat(status["completed_at"]) < cutoff
        ]
        for job_id in to_remove:
            status = conversion_status.get(job_id, {})
            _cleanup_job(job_id, status.get("tmp_dir"))
            logger.info(f"Cleaned up expired job {job_id}")


def _cleanup_orphan_dirs():
    """Remove stale temp directories from a previous run."""
    work_dir = os.environ.get("TMPDIR", tempfile.gettempdir())
    try:
        for entry in os.listdir(work_dir):
            full = os.path.join(work_dir, entry)
            if os.path.isdir(full) and re.match(r'^mymevert_', entry):
                try:
                    shutil.rmtree(full, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass


async def startup_event():
    asyncio.create_task(_periodic_cleanup())
    await asyncio.to_thread(_cleanup_orphan_dirs)
    logger.info("MYMevert Backend started")


async def shutdown_event():
    global _shutting_down
    _shutting_down = True
    logger.info("MYMevert Backend shutting down")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup_event()
    yield
    await shutdown_event()


app = FastAPI(lifespan=lifespan)

raw_origins = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,https://mymevert.id,https://mymevert-id.vercel.app"
)
ALLOWED_ORIGINS = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
    allow_credentials=True,
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all incoming requests with method, path, and duration."""
    start_time = time.time()
    try:
        response = await call_next(request)
        duration = time.time() - start_time
        logger.info(
            f"REQUEST {request.method} {request.url.path} | "
            f"status={response.status_code} | "
            f"duration={duration:.2f}s | "
            f"client={request.client.host if request.client else 'unknown'}"
        )
        return response
    except Exception as e:
        duration = time.time() - start_time
        logger.error(
            f"REQUEST {request.method} {request.url.path} | "
            f"status=500 | "
            f"duration={duration:.2f}s | "
            f"error={str(e)} | "
            f"client={request.client.host if request.client else 'unknown'}",
            exc_info=True
        )
        raise


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(
        f"UNHANDLED EXCEPTION {request.method} {request.url.path} | "
        f"error={str(exc)} | "
        f"client={request.client.host if request.client else 'unknown'}",
        exc_info=True
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"}
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    logger.warning(
        f"HTTP EXCEPTION {request.method} {request.url.path} | "
        f"status={exc.status_code} | "
        f"detail={exc.detail} | "
        f"client={request.client.host if request.client else 'unknown'}"
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )


class YtRequest(BaseModel):
    url: str
    resolution: str = "720"


_ytdlp_download_re = re.compile(r'\[download\]\s+(\d+(?:\.\d+)?)%')
_ffmpeg_kv_re = re.compile(r'^(\w+)=(.*)$')


async def _run_command_with_progress(
    cmd: list,
    job_id: str,
    progress_parser: Optional[Callable[[str, str], None]] = None,
    timeout: int = CONVERSION_TIMEOUT,
) -> tuple[int, str]:
    """Run a subprocess, parsing output for progress, with timeout."""
    logger.info(f"Running: {' '.join(cmd[:2])} ...")
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    output_parts: list[str] = []

    async def _reader():
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            output_parts.append(text)
            if progress_parser:
                progress_parser(text, job_id)

    try:
        await asyncio.wait_for(_reader(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        logger.error(f"Command timed out after {timeout}s: {' '.join(cmd[:2])}")
        raise subprocess.TimeoutExpired(cmd, timeout, output="".join(output_parts))

    returncode = await process.wait()
    return returncode, "".join(output_parts)


def _parse_ytdlp_download_progress(text: str, job_id: str):
    """Parse yt-dlp [download] X% progress lines."""
    m = _ytdlp_download_re.search(text)
    if m:
        pct = float(m.group(1))
        progress = int(10 + (pct / 100) * 80)
        conversion_status[job_id]["progress"] = min(max(progress, 10), 95)


def _parse_ffmpeg_progress(text: str, job_id: str):
    """Parse ffmpeg -progress key=value output."""
    for line in text.strip().splitlines():
        m = _ffmpeg_kv_re.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if key == "duration":
            try:
                _ffmpeg_durations[job_id] = float(value)
            except ValueError:
                pass
        elif key == "out_time_ms":
            duration = _ffmpeg_durations.get(job_id, 0)
            if duration > 0:
                try:
                    elapsed_s = float(value) / 1_000_000
                    pct = (elapsed_s / duration) * 100
                    progress = int(60 + (pct / 100) * 30)
                    conversion_status[job_id]["progress"] = min(max(progress, 60), 95)
                except ValueError:
                    pass


def _command_error(returncode: int, output: str, default_msg: str) -> str:
    """Extract error message from subprocess output."""
    if output:
        return f"{default_msg}: {output[-800:].strip()}"
    return f"{default_msg} (exit code {returncode})"


def _is_valid_url(url: str) -> bool:
    if not url:
        return False
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        result = urlparse(url)
        netloc = result.netloc.lower()
        if not netloc or result.scheme not in ("http", "https"):
            return False
        return any(
            netloc == host or netloc.endswith("." + host)
            for host in ("youtube.com", "youtu.be", "m.youtube.com", "www.youtube.com")
        )
    except Exception:
        return False


def _sanitize_filename(filename: str) -> str:
    """Sanitize a filename to prevent path traversal."""
    name = os.path.basename(filename)
    name = re.sub(r'[^\w.\-]', '_', name)
    if not name or name.startswith('.'):
        name = f"upload_{uuid.uuid4().hex}.tmp"
    return name


@app.get("/")
def root():
    return {"status": "MYMevert Backend Running"}


@app.get("/health")
async def health_check():
    """Health check endpoint with dependency status."""
    ffmpeg_ok = False
    try:
        ffmpeg_result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, timeout=5
        )
        ffmpeg_ok = ffmpeg_result.returncode == 0
    except Exception:
        ffmpeg_ok = False

    ytdlp_ok = False
    try:
        ytdlp_result = subprocess.run(
            ["yt-dlp", "--version"], capture_output=True, timeout=5
        )
        ytdlp_ok = ytdlp_result.returncode == 0
    except Exception:
        ytdlp_ok = False

    return {
        "status": "healthy",
        "ffmpeg": ffmpeg_ok,
        "yt-dlp": ytdlp_ok,
        "jobs_pending": len([j for j in conversion_status.values() if j['status'] == 'pending']),
        "jobs_active": len([j for j in conversion_status.values() if j['status'] == 'processing']),
        "jobs_total": len(conversion_status),
        "shutdown": _shutting_down
    }


@app.post("/convert/yt-mp4/start")
async def start_yt_mp4(req: YtRequest):
    if not _is_valid_url(req.url):
        raise HTTPException(status_code=400, detail="Invalid URL format")
    if not _try_acquire_job_slot():
        raise HTTPException(status_code=429, detail="Server sedang sibuk, coba lagi sebentar")

    job_id = str(uuid.uuid4())
    conversion_status[job_id] = {
        "progress": 0,
        "step": "queued",
        "status": "pending",
        "filename": None,
        "tmp_dir": None,
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    asyncio.create_task(process_yt_mp4(job_id, req))
    logger.info(f"Started YT-MP4 job {job_id}")
    return {"job_id": job_id}


@app.post("/convert/yt-mp3/start")
async def start_yt_mp3(req: YtRequest):
    if not _is_valid_url(req.url):
        raise HTTPException(status_code=400, detail="Invalid URL format")
    if not _try_acquire_job_slot():
        raise HTTPException(status_code=429, detail="Server sedang sibuk, coba lagi sebentar")

    job_id = str(uuid.uuid4())
    conversion_status[job_id] = {
        "progress": 0,
        "step": "queued",
        "status": "pending",
        "filename": None,
        "tmp_dir": None,
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    asyncio.create_task(process_yt_mp3(job_id, req))
    logger.info(f"Started YT-MP3 job {job_id}")
    return {"job_id": job_id}


@app.post("/convert/local-mp3/start")
async def start_local_mp3(file: UploadFile = File(...)):
    content_length = getattr(file, 'size', None)
    if content_length and content_length > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File terlalu besar (maks {MAX_UPLOAD_SIZE_MB}MB)"
        )

    if not _try_acquire_job_slot():
        raise HTTPException(status_code=429, detail="Server sedang sibuk, coba lagi sebentar")

    tmp = tempfile.mkdtemp(prefix="mymevert_")
    filename = _sanitize_filename(file.filename or "upload.mp3")
    input_path = os.path.join(tmp, filename)

    written = 0
    try:
        with open(input_path, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File terlalu besar (maks {MAX_UPLOAD_SIZE_MB}MB)"
                    )
                f.write(chunk)
    except HTTPException:
        _release_job_slot()
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as e:
        _release_job_slot()
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Upload gagal: {str(e)}")

    input_size = os.path.getsize(input_path)
    logger.info(f"Saved upload {written} bytes ({input_size} on disk) for local-MP3")

    job_id = str(uuid.uuid4())
    conversion_status[job_id] = {
        "progress": 0,
        "step": "queued",
        "status": "pending",
        "filename": None,
        "tmp_dir": tmp,
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    asyncio.create_task(process_local_mp3(job_id, input_path, filename))
    logger.info(f"Started local-MP3 job {job_id}")
    return {"job_id": job_id}


@app.get("/convert/status/{job_id}")
async def get_status(job_id: str):
    status = conversion_status.get(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Job not found")
    return status


@app.get("/convert/download/{job_id}")
async def download_file(job_id: str, background_tasks: BackgroundTasks):
    status = conversion_status.get(job_id)
    if not status or status["status"] != "completed":
        raise HTTPException(status_code=400, detail="File not ready")

    tmp_dir = status.get("tmp_dir")
    filename = status.get("filename")

    if not tmp_dir or not filename:
        raise HTTPException(status_code=404, detail="File not found")

    actual_out = os.path.join(tmp_dir, filename)

    if not os.path.exists(actual_out):
        raise HTTPException(status_code=404, detail="File not found")

    background_tasks.add_task(shutil.rmtree, tmp_dir, ignore_errors=True)
    background_tasks.add_task(lambda: conversion_status.pop(job_id, None))

    media_type = "video/mp4" if filename.endswith(".mp4") else "audio/mpeg"

    logger.info(f"Downloading file for job {job_id}: {filename}")

    return FileResponse(
        actual_out,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


# ============= PROCESS FUNCTIONS =============

async def process_yt_mp4(job_id: str, req: YtRequest):
    tmp = tempfile.mkdtemp(prefix="mymevert_")
    conversion_status[job_id]["tmp_dir"] = tmp

    try:
        conversion_status[job_id].update({"step": "preparing", "progress": 5, "status": "processing"})
        out = os.path.join(tmp, "%(title)s.%(ext)s")
        conversion_status[job_id].update({"step": "downloading", "progress": 10})

        cmd = [
            "yt-dlp",
            "--js-runtimes", "node",
            "--no-mtime",
            "--no-playlist",
            "--user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "--progress",
            "--newline",
            "-S", f"res:{req.resolution},vcodec:avc1,acodec:aac",
            "-f", "bv*+ba/b",
            "--merge-output-format", "mp4",
            "-o", out,
            req.url,
        ]

        if FFMPEG_PATH:
            cmd.extend(["--ffmpeg-location", FFMPEG_PATH])

        try:
            returncode, output = await _run_command_with_progress(
                cmd, job_id, _parse_ytdlp_download_progress
            )
        except subprocess.TimeoutExpired:
            conversion_status[job_id].update({
                "status": "error",
                "error": f"Download timed out after {CONVERSION_TIMEOUT}s"
            })
            _cleanup_job(job_id, tmp)
            return

        if returncode != 0:
            conversion_status[job_id].update({
                "status": "error",
                "error": _command_error(returncode, output, "Download failed")
            })
            _cleanup_job(job_id, tmp)
            return

        conversion_status[job_id].update({"progress": 96})

        files = [f for f in os.listdir(tmp) if f.endswith(".mp4")]
        if not files:
            conversion_status[job_id].update({
                "status": "error",
                "error": "Output file not found after download"
            })
            _cleanup_job(job_id, tmp)
            return

        conversion_status[job_id].update({
            "status": "completed",
            "progress": 100,
            "filename": files[0],
            "completed_at": datetime.now(timezone.utc).isoformat()
        })
        logger.info(f"Completed YT-MP4 job {job_id}: {files[0]}")

    except Exception as e:
        conversion_status[job_id].update({"status": "error", "error": str(e)})
        logger.error(f"YT-MP4 job {job_id} failed: {e}")
        _cleanup_job(job_id, tmp)
    finally:
        _release_job_slot()


async def process_yt_mp3(job_id: str, req: YtRequest):
    tmp = tempfile.mkdtemp(prefix="mymevert_")
    conversion_status[job_id]["tmp_dir"] = tmp

    try:
        conversion_status[job_id].update({"step": "preparing", "progress": 5, "status": "processing"})
        out = os.path.join(tmp, "%(title)s.%(ext)s")
        conversion_status[job_id].update({"step": "downloading", "progress": 10})

        cmd = [
            "yt-dlp",
            "--js-runtimes", "node",
            "--no-mtime",
            "--no-playlist",
            "--user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "--progress",
            "--newline",
            "-x", "--audio-format", "mp3",
            "--audio-quality", "2",
            "-o", out,
            req.url,
        ]

        if FFMPEG_PATH:
            cmd.extend(["--ffmpeg-location", FFMPEG_PATH])

        try:
            returncode, output = await _run_command_with_progress(
                cmd, job_id, _parse_ytdlp_download_progress
            )
        except subprocess.TimeoutExpired:
            conversion_status[job_id].update({
                "status": "error",
                "error": f"Download timed out after {CONVERSION_TIMEOUT}s"
            })
            _cleanup_job(job_id, tmp)
            return

        if returncode != 0:
            conversion_status[job_id].update({
                "status": "error",
                "error": _command_error(returncode, output, "Download failed")
            })
            _cleanup_job(job_id, tmp)
            return

        conversion_status[job_id].update({"progress": 96})

        files = [f for f in os.listdir(tmp) if f.endswith(".mp3")]
        if not files:
            conversion_status[job_id].update({
                "status": "error",
                "error": "Output file not found after extraction"
            })
            _cleanup_job(job_id, tmp)
            return

        conversion_status[job_id].update({
            "status": "completed",
            "progress": 100,
            "filename": files[0],
            "completed_at": datetime.now(timezone.utc).isoformat()
        })
        logger.info(f"Completed YT-MP3 job {job_id}: {files[0]}")

    except Exception as e:
        conversion_status[job_id].update({"status": "error", "error": str(e)})
        logger.error(f"YT-MP3 job {job_id} failed: {e}")
        _cleanup_job(job_id, tmp)
    finally:
        _release_job_slot()


async def process_local_mp3(job_id: str, input_path: str, original_filename: str):
    tmp = os.path.dirname(input_path)

    try:
        conversion_status[job_id].update({"step": "converting", "progress": 10, "status": "processing"})

        base_name = os.path.splitext(original_filename)[0]
        output_path = os.path.join(tmp, f"{base_name}.mp3")

        input_size = os.path.getsize(input_path)
        logger.info(f"Converting {input_size} bytes for job {job_id}")

        conversion_status[job_id].update({"progress": 25})

        cmd = [
            "ffmpeg", "-hide_banner", "-y",
            "-i", input_path,
            "-vn", "-ar", "44100", "-ac", "2",
            "-b:a", f"{AUDIO_BITRATE_KBPS}k",
            "-progress", "pipe:1",
            output_path,
        ]

        try:
            returncode, output = await _run_command_with_progress(
                cmd, job_id, _parse_ffmpeg_progress
            )
        except subprocess.TimeoutExpired:
            conversion_status[job_id].update({
                "status": "error",
                "error": f"Conversion timed out after {CONVERSION_TIMEOUT}s"
            })
            _cleanup_job(job_id, tmp)
            return

        if returncode != 0:
            conversion_status[job_id].update({
                "status": "error",
                "error": _command_error(returncode, output, "Conversion failed")
            })
            _cleanup_job(job_id, tmp)
            return

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            conversion_status[job_id].update({
                "status": "error",
                "error": "Output file not found or empty"
            })
            _cleanup_job(job_id, tmp)
            return

        output_size = os.path.getsize(output_path)
        logger.info(f"Converted {input_size} -> {output_size} bytes for job {job_id}")

        conversion_status[job_id].update({
            "status": "completed",
            "progress": 100,
            "filename": f"{base_name}.mp3",
            "completed_at": datetime.now(timezone.utc).isoformat()
        })
        logger.info(f"Completed local-MP3 job {job_id}: {base_name}.mp3")

    except Exception as e:
        conversion_status[job_id].update({"status": "error", "error": str(e)})
        logger.error(f"Local-MP3 job {job_id} failed: {e}")
        _cleanup_job(job_id, tmp)
    finally:
        _release_job_slot()


def _handle_shutdown(signum, frame):
    """Set shutdown flag so background tasks can stop cleanly."""
    global _shutting_down
    _shutting_down = True
    logger.info(f"Received signal {signum}, shutting down")


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)
