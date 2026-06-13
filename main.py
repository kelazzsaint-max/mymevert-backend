import time
import os
import json
import tempfile
import subprocess
import shutil
import uuid
import asyncio
import logging
import redis
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from urllib.parse import quote
from pydantic import BaseModel
from typing import Optional
from urllib.parse import urlparse
from datetime import datetime
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

# Redis client — initialized on startup
redis_client: Optional[redis.Redis] = None

def _redis_key(job_id: str) -> str:
    return f"job:{job_id}"

def _set_job(job_id: str, data: dict) -> None:
    """Persist job status dict to Redis with TTL."""
    try:
        redis_client.set(
            _redis_key(job_id),
            json.dumps(data),
            ex=JOB_TTL_MINUTES * 60
        )
        logger.debug(f"Redis SET job {job_id}")
    except Exception as e:
        logger.error(f"Redis SET failed for job {job_id}: {e}")

def _get_job(job_id: str) -> Optional[dict]:
    """Retrieve job status dict from Redis. Returns None if not found."""
    try:
        raw = redis_client.get(_redis_key(job_id))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Redis GET failed for job {job_id}: {e}")
        return None

def _update_job(job_id: str, updates: dict) -> None:
    """Fetch, merge updates, and re-persist job status to Redis."""
    data = _get_job(job_id) or {}
    data.update(updates)
    _set_job(job_id, data)

def _delete_job(job_id: str) -> None:
    """Remove job key from Redis."""
    try:
        redis_client.delete(_redis_key(job_id))
        logger.debug(f"Redis DEL job {job_id}")
    except Exception as e:
        logger.error(f"Redis DEL failed for job {job_id}: {e}")

def _cleanup_job(job_id: str, tmp_dir: Optional[str] = None):
    """Remove job from Redis and delete temporary directory."""
    _delete_job(job_id)
    if tmp_dir:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info(f"Cleaned up job {job_id} and tmp_dir {tmp_dir}")

@app.on_event("startup")
async def startup_event():
    global redis_client
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    try:
        redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
        redis_client.ping()
        logger.info(f"Connected to Redis at {redis_url}")
    except Exception as e:
        logger.error(f"Failed to connect to Redis at {redis_url}: {e}")
        raise RuntimeError(f"Redis connection failed: {e}")
    logger.info("MYMevert Backend started")

@app.get("/")
def root():
    return {"status": "MYMevert Backend Running"}

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
            logger.error(f"Command failed: {result.stderr}")
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
    _set_job(job_id, {
        "progress": 0,
        "step": "queued",
        "status": "pending",
        "filename": None,
        "tmp_dir": None,
        "error": None,
        "created_at": datetime.now().isoformat()
    })
    asyncio.create_task(process_yt_mp4(job_id, req))
    logger.info(f"Started YT-MP4 job {job_id}")
    return {"job_id": job_id}

@app.post("/convert/yt-mp3/start")
async def start_yt_mp3(req: YtRequest):
    if not _is_valid_url(req.url):
        raise HTTPException(status_code=400, detail="Invalid URL format")
    
    job_id = str(uuid.uuid4())
    _set_job(job_id, {
        "progress": 0,
        "step": "queued",
        "status": "pending",
        "filename": None,
        "tmp_dir": None,
        "error": None,
        "created_at": datetime.now().isoformat()
    })
    asyncio.create_task(process_yt_mp3(job_id, req))
    logger.info(f"Started YT-MP3 job {job_id}")
    return {"job_id": job_id}

@app.post("/convert/local-mp3/start")
async def start_local_mp3(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    
    # Read file now (before background task)
    file_content = await file.read()
    filename = file.filename
    
    _set_job(job_id, {
        "progress": 0,
        "step": "queued",
        "status": "pending",
        "filename": None,
        "tmp_dir": None,
        "error": None,
        "created_at": datetime.now().isoformat()
    })
    
    asyncio.create_task(process_local_mp3(job_id, file_content, filename))
    logger.info(f"Started local-MP3 job {job_id}")
    return {"job_id": job_id}

@app.get("/convert/status/{job_id}")
async def get_status(job_id: str):
    status = _get_job(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Job not found")
    return status

@app.get("/convert/download/{job_id}")
async def download_file(job_id: str, background_tasks: BackgroundTasks):
    status = _get_job(job_id)
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
    background_tasks.add_task(_delete_job, job_id)
    
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
    _update_job(job_id, {"tmp_dir": tmp})
    
    try:
        _update_job(job_id, {"step": "preparing", "progress": 5})
        out = os.path.join(tmp, "%(title)s.%(ext)s")
        _update_job(job_id, {"step": "downloading", "progress": 10})
        
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
        
        for prog in [30, 50, 70, 85]:
            await asyncio.sleep(1)
            _update_job(job_id, {"progress": prog})
        
        if result.returncode != 0:
            _update_job(job_id, {"status": "error", "error": "Output file not found"})
            _cleanup_job(job_id, tmp)
            return
        
        _update_job(job_id, {
            "status": "completed",
            "progress": 100,
            "filename": files[0],
            "completed_at": datetime.now().isoformat()
        })
        logger.info(f"Completed YT-MP4 job {job_id}")
        
    except Exception as e:
        _update_job(job_id, {"status": "error", "error": str(e)})
        logger.error(f"YT-MP4 job {job_id} failed: {e}")
        _cleanup_job(job_id, tmp)

async def process_yt_mp3(job_id: str, req: YtRequest):
    tmp = tempfile.mkdtemp()
    _update_job(job_id, {"tmp_dir": tmp})
    
    try:
        _update_job(job_id, {"step": "preparing", "progress": 5})
        out = os.path.join(tmp, "%(title)s.%(ext)s")
        _update_job(job_id, {"step": "downloading", "progress": 10})
        
        cmd = [
            "yt-dlp",
            "--js-runtimes", "node",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "-x", "--audio-format", "mp3",
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
        
        for prog in [30, 50, 70, 85]:
            await asyncio.sleep(0.5)
            _update_job(job_id, {"progress": prog})
        
        if result.returncode != 0:
            _update_job(job_id, {"status": "error", "error": _command_error(result, "Download failed")})
            _cleanup_job(job_id, tmp)
            return
        
        _update_job(job_id, {"step": "finalizing", "progress": 95})
        
        files = [f for f in os.listdir(tmp) if f.endswith(".mp3")]
        if not files:
            _update_job(job_id, {"status": "error", "error": "Output file not found"})
            _cleanup_job(job_id, tmp)
            return
        
        _update_job(job_id, {
            "status": "completed",
            "progress": 100,
            "filename": files[0],
            "completed_at": datetime.now().isoformat()
        })
        logger.info(f"Completed YT-MP3 job {job_id}")
        
    except Exception as e:
        _update_job(job_id, {"status": "error", "error": str(e)})
        logger.error(f"YT-MP3 job {job_id} failed: {e}")
        _cleanup_job(job_id, tmp)

async def process_local_mp3(job_id: str, file_content: bytes, original_filename: str):
    tmp = tempfile.mkdtemp()
    _update_job(job_id, {"tmp_dir": tmp})
    
    try:
        _update_job(job_id, {"step": "preparing", "progress": 5})
        
        input_path = os.path.join(tmp, original_filename)
        original_name = os.path.splitext(original_filename)[0]
        output_path = os.path.join(tmp, f"{original_name}.mp3")
        
        with open(input_path, "wb") as f:
            f.write(file_content)
        
        _update_job(job_id, {"step": "converting", "progress": 20})
        
        cmd = [
            "ffmpeg", "-i", input_path,
            "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k",
            output_path
        ]
        result = await _run_command(cmd)
        
        for prog in [40, 60, 80]:
            await asyncio.sleep(0.5)
            _update_job(job_id, {"progress": prog})
        
        if result.returncode != 0:
            _update_job(job_id, {
                "status": "error",
                "error": _command_error(result, "Conversion failed")
            })
            _cleanup_job(job_id, tmp)
            return
        
        if not os.path.exists(output_path):
            _update_job(job_id, {
                "status": "error",
                "error": "Output file not found"
            })
            _cleanup_job(job_id, tmp)
            return
        
        _update_job(job_id, {
            "status": "completed",
            "progress": 100,
            "filename": f"{original_name}.mp3",
            "completed_at": datetime.now().isoformat()
        })
        logger.info(f"Completed local-MP3 job {job_id}")
        
    except Exception as e:
        _update_job(job_id, {"status": "error", "error": str(e)})
        logger.error(f"Local-MP3 job {job_id} failed: {e}")
        _cleanup_job(job_id, tmp)