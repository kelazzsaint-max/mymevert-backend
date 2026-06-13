import time
import os
import tempfile
import subprocess
import shutil
import uuid
import asyncio
import logging
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from urllib.parse import quote
from pydantic import BaseModel
from typing import Dict, Optional
from urllib.parse import urlparse
from datetime import datetime, timedelta
from starlette.exceptions import HTTPException as StarletteHTTPException

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mymevert")

app = FastAPI()

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
    """Log all unhandled exceptions with full traceback."""
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
    """Log HTTP exceptions (404, 400, etc)."""
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

# CORS configuration for frontend connection
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,https://mymevert.id,https://mymevert-id.vercel.app"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"]
)

FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "")
COOKIES_BROWSER = os.environ.get("COOKIES_BROWSER", "")  # chrome, firefox, edge
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")  # path to cookies.txt
# Cleanup jobs older than 1 hour
JOB_TTL_MINUTES = int(os.environ.get("JOB_TTL_MINUTES", "60"))

class YtRequest(BaseModel):
    url: str
    resolution: str = "720"

# Store conversion status with timestamps
conversion_status: Dict[str, dict] = {}

def _cleanup_job(job_id: str, tmp_dir: Optional[str] = None):
    """Cleanup job status and temporary directory."""
    conversion_status.pop(job_id, None)
    if tmp_dir:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info(f"Cleaned up job {job_id} and tmp_dir {tmp_dir}")

async def _periodic_cleanup():
    """Periodically clean up old jobs to prevent memory leaks."""
    while True:
        await asyncio.sleep(300)  # Run every 5 minutes
        cutoff = datetime.now() - timedelta(minutes=JOB_TTL_MINUTES)
        to_remove = [
            job_id for job_id, status in conversion_status.items()
            if status.get("completed_at") and 
            datetime.fromisoformat(status["completed_at"]) < cutoff
        ]
        for job_id in to_remove:
            status = conversion_status.get(job_id, {})
            _cleanup_job(job_id, status.get("tmp_dir"))
            logger.info(f"Cleaned up expired job {job_id}")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(_periodic_cleanup())
    logger.info("MYMevert Backend started")

@app.get("/")
def root():
    return {"status": "MYMevert Backend Running"}

@app.get("/health")
async def health_check():
    """Health check endpoint with dependency status."""
    # Check ffmpeg
    try:
        ffmpeg_result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        ffmpeg_ok = ffmpeg_result.returncode == 0
    except:
        ffmpeg_ok = False
    
    # Check yt-dlp
    try:
        ytdlp_result = subprocess.run(["yt-dlp", "--version"], capture_output=True, timeout=5)
        ytdlp_ok = ytdlp_result.returncode == 0
    except:
        ytdlp_ok = False
    
    return {
        "status": "healthy",
        "ffmpeg": ffmpeg_ok,
        "yt-dlp": ytdlp_ok,
        "jobs_pending": len([j for j in conversion_status.values() if j['status'] == 'pending']),
        "jobs_total": len(conversion_status)
    }

# ============= POLLING ENDPOINTS =============

def _is_valid_url(url: str) -> bool:
    if not url:
        return False
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        result = urlparse(url)
        return all([result.scheme in ("http", "https"), result.netloc])
    except Exception:
        return False

async def _run_command(cmd: list, capture: bool = True) -> subprocess.CompletedProcess:
    """Run subprocess command asynchronously."""
    logger.info(f"Running command: {' '.join(cmd[:3])}...")
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=capture,
            text=True,
            timeout=300  # 5 minute timeout
        )
        if result.returncode != 0:
            logger.error(f"Command failed: {result.stderr[:500]}")
        return result
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out: {' '.join(cmd[:3])}")
        raise

def _command_error(result: subprocess.CompletedProcess, default_msg: str) -> str:
    """Extract error message from subprocess result."""
    if result.stderr:
        return f"{default_msg}: {result.stderr[-500:]}"
    if result.stdout:
        return f"{default_msg}: {result.stdout[-500:]}"
    return default_msg

@app.post("/convert/yt-mp4/start")
async def start_yt_mp4(req: YtRequest):
    if not _is_valid_url(req.url):
        raise HTTPException(status_code=400, detail="Invalid URL format")
    
    job_id = str(uuid.uuid4())
    conversion_status[job_id] = {
        "progress": 0,
        "step": "queued",
        "status": "pending",
        "filename": None,
        "tmp_dir": None,
        "error": None,
        "created_at": datetime.now().isoformat()
    }
    asyncio.create_task(process_yt_mp4(job_id, req))
    logger.info(f"Started YT-MP4 job {job_id}")
    return {"job_id": job_id}

@app.post("/convert/yt-mp3/start")
async def start_yt_mp3(req: YtRequest):
    if not _is_valid_url(req.url):
        raise HTTPException(status_code=400, detail="Invalid URL format")
    
    job_id = str(uuid.uuid4())
    conversion_status[job_id] = {
        "progress": 0,
        "step": "queued",
        "status": "pending",
        "filename": None,
        "tmp_dir": None,
        "error": None,
        "created_at": datetime.now().isoformat()
    }
    asyncio.create_task(process_yt_mp3(job_id, req))
    logger.info(f"Started YT-MP3 job {job_id}")
    return {"job_id": job_id}

@app.post("/convert/local-mp3/start")
async def start_local_mp3(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    
    # Read file now (before background task)
    file_content = await file.read()
    filename = file.filename
    
    conversion_status[job_id] = {
        "progress": 0,
        "step": "queued",
        "status": "pending",
        "filename": None,
        "tmp_dir": None,
        "error": None,
        "created_at": datetime.now().isoformat()
    }
    
    asyncio.create_task(process_local_mp3(job_id, file_content, filename))
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
    tmp = tempfile.mkdtemp()
    conversion_status[job_id]["tmp_dir"] = tmp
    
    try:
        conversion_status[job_id].update({"step": "preparing", "progress": 5})
        out = os.path.join(tmp, "%(title)s.%(ext)s")
        conversion_status[job_id].update({"step": "downloading", "progress": 10})
        
        cmd = [
            "yt-dlp",
            "--js-runtimes", "node",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "-f", f"bestvideo[height<={req.resolution}][vcodec^=avc]+bestaudio[ext=m4a]/bestvideo[height<={req.resolution}][ext=mp4]+bestaudio[ext=m4a]/best[height<={req.resolution}]/best",
            "--merge-output-format", "mp4",
            "-o", out,
            req.url
        ]
        if COOKIES_BROWSER:
            cmd.extend(["--cookies-from-browser", COOKIES_BROWSER])
        if COOKIES_FILE:
            cmd.extend(["--cookies", COOKIES_FILE])
        
        if FFMPEG_PATH:
            cmd.extend(["--ffmpeg-location", FFMPEG_PATH, "--postprocessor-args", "ffmpeg:-movflags +faststart"])
        
        result = await _run_command(cmd)
        
        if result.returncode != 0:
            conversion_status[job_id].update({
                "status": "error", 
                "error": _command_error(result, "Download failed")
            })
            _cleanup_job(job_id, tmp)
            return
        
        # Find the downloaded file
        files = [f for f in os.listdir(tmp) if f.endswith(".mp4")]
        if not files:
            conversion_status[job_id].update({
                "status": "error", 
                "error": "Output file not found"
            })
            _cleanup_job(job_id, tmp)
            return
        
        for prog in [30, 50, 70, 85]:
            await asyncio.sleep(0.5)
            conversion_status[job_id].update({"progress": prog})
        
        conversion_status[job_id].update({
            "status": "completed",
            "progress": 100,
            "filename": files[0],
            "completed_at": datetime.now().isoformat()
        })
        logger.info(f"Completed YT-MP4 job {job_id}")
        
    except Exception as e:
        conversion_status[job_id].update({"status": "error", "error": str(e)})
        logger.error(f"YT-MP4 job {job_id} failed: {e}")
        _cleanup_job(job_id, tmp)

async def process_yt_mp3(job_id: str, req: YtRequest):
    tmp = tempfile.mkdtemp()
    conversion_status[job_id]["tmp_dir"] = tmp
    
    try:
        conversion_status[job_id].update({"step": "preparing", "progress": 5})
        out = os.path.join(tmp, "%(title)s.%(ext)s")
        conversion_status[job_id].update({"step": "downloading", "progress": 10})
        
        cmd = [
            "yt-dlp",
            "--js-runtimes", "node",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "-x", "--audio-format", "mp3",
            "--audio-quality", "0",
            "-o", out,
            req.url
        ]
        if COOKIES_BROWSER:
            cmd.extend(["--cookies-from-browser", COOKIES_BROWSER])
        if COOKIES_FILE:
            cmd.extend(["--cookies", COOKIES_FILE])
        
        if FFMPEG_PATH:
            cmd.extend(["--ffmpeg-location", FFMPEG_PATH])
        
        result = await _run_command(cmd)
        
        if result.returncode != 0:
            conversion_status[job_id].update({
                "status": "error", 
                "error": _command_error(result, "Download failed")
            })
            _cleanup_job(job_id, tmp)
            return
        
        conversion_status[job_id].update({"step": "finalizing", "progress": 95})
        
        files = [f for f in os.listdir(tmp) if f.endswith(".mp3")]
        if not files:
            conversion_status[job_id].update({
                "status": "error", 
                "error": "Output file not found"
            })
            _cleanup_job(job_id, tmp)
            return
        
        for prog in [30, 50, 70, 85]:
            await asyncio.sleep(0.5)
            conversion_status[job_id].update({"progress": prog})
        
        conversion_status[job_id].update({
            "status": "completed",
            "progress": 100,
            "filename": files[0],
            "completed_at": datetime.now().isoformat()
        })
        logger.info(f"Completed YT-MP3 job {job_id}")
        
    except Exception as e:
        conversion_status[job_id].update({"status": "error", "error": str(e)})
        logger.error(f"YT-MP3 job {job_id} failed: {e}")
        _cleanup_job(job_id, tmp)

async def process_local_mp3(job_id: str, file_content: bytes, original_filename: str):
    tmp = tempfile.mkdtemp()
    conversion_status[job_id]["tmp_dir"] = tmp
    
    try:
        conversion_status[job_id].update({"step": "preparing", "progress": 5})
        
        input_path = os.path.join(tmp, original_filename)
        original_name = os.path.splitext(original_filename)[0]
        output_path = os.path.join(tmp, f"{original_name}.mp3")
        
        with open(input_path, "wb") as f:
            f.write(file_content)
        
        conversion_status[job_id].update({"step": "converting", "progress": 20})
        
        cmd = [
            "ffmpeg", "-i", input_path,
            "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k",
            output_path
        ]
        result = await _run_command(cmd)
        
        if result.returncode != 0:
            conversion_status[job_id].update({
                "status": "error",
                "error": _command_error(result, "Conversion failed")
            })
            _cleanup_job(job_id, tmp)
            return
        
        for prog in [40, 60, 80]:
            await asyncio.sleep(0.5)
            conversion_status[job_id].update({"progress": prog})
        
        if not os.path.exists(output_path):
            conversion_status[job_id].update({
                "status": "error",
                "error": "Output file not found"
            })
            _cleanup_job(job_id, tmp)
            return
        
        conversion_status[job_id].update({
            "status": "completed",
            "progress": 100,
            "filename": f"{original_name}.mp3",
            "completed_at": datetime.now().isoformat()
        })
        logger.info(f"Completed local-MP3 job {job_id}")
        
    except Exception as e:
        conversion_status[job_id].update({"status": "error", "error": str(e)})
        logger.error(f"Local-MP3 job {job_id} failed: {e}")
        _cleanup_job(job_id, tmp)