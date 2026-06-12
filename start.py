import os
import subprocess
import sys

port = os.environ.get("PORT", "8000")

cmd = [
    sys.executable,
    "-m",
    "uvicorn",
    "main:app",
    "--host",
    "0.0.0.0",
    "--port",
    port
]

subprocess.run(cmd)