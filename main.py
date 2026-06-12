import os
import tempfile
import subprocess
import shutil
import uuid
import asyncio
import logging
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from urllib.parse import quote
from pydantic import BaseModel
from typing import Dict, Optional
from urllib.parse import urlparse
from datetime import datetime, timedelta

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("mymevert")

app = FastAPI()

# CORS configuration for frontend connection
ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:3000,https://mymevert.id"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"]
)

FFMPEG_PATH = os.environ.get("FFMPEG_PATH", "")
# Cleanup jobs older than 1 hour
JOB_TTL_MINUTES = int(os.environ.get("JOB_TTL_MINUTES", "60"))