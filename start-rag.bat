@echo off
title HCA Assistant Launcher

:: Check if backend is already running
netstat -an | find "0.0.0.0:3001" >nul 2>&1
if %errorlevel%==0 (
  echo Backend already running on port 3001.
) else (
  echo Starting backend...
  start /min "HCA Backend" cmd /c "cd /d "%~dp0backend" && node server.js"
  timeout /t 3 /nobreak >nul
)

echo Opening HCA Assistant...
start "" "%~dp0rag.html"
exit
