import os
import subprocess
import sys

port = os.environ.get("PORT", "8000")
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))

cmd = [
    sys.executable,
    "-m",
    "uvicorn",
    "main:app",
    "--host",
    "0.0.0.0",
    "--port",
    port,
    "--workers",
    str(workers),
]

subprocess.run(cmd)
