@echo off
echo Starting MYMevert Backend locally...
uvicorn main:app --host 0.0.0.0 --port 8000
pause
