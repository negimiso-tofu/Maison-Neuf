@echo off
rem ===================================================================
rem  Munder Difflin launcher
rem  NOTE: keep this file ASCII-only. cmd.exe reads .bat as the system
rem        codepage (CP932 here), so UTF-8 Japanese text corrupts the
rem        commands themselves. Japanese belongs in README.md, not here.
rem ===================================================================
cd /d "%~dp0"
title Munder Difflin launcher

echo.
echo   Munder Difflin
echo   ------------------------------
echo.

rem --- watcher: skip if status.json was touched within the last 15s ---
set WATCHER=0
for /f %%A in ('powershell -NoProfile -Command "if ((Test-Path status.json) -and ((((Get-Date) - (Get-Item status.json).LastWriteTime).TotalSeconds) -lt 15)) { 1 } else { 0 }"') do set WATCHER=%%A

if "%WATCHER%"=="1" (
    echo   [1/2] watcher ... already running
) else (
    echo   [1/2] watcher ... starting
    start "Munder Difflin - watcher" python watcher.py --image-dir "C:\Users\user\Documents\Codex\2026-09-09\realtime-voice-chat\outputs"
)

rem --- server: skip if port 8744 is already listening ---
set SERVER=0
for /f %%A in ('powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8744 -State Listen -ErrorAction SilentlyContinue) { 1 } else { 0 }"') do set SERVER=%%A

if "%SERVER%"=="1" (
    echo   [2/2] server  ... already running
) else (
    echo   [2/2] server  ... starting
    start "Munder Difflin - server" python -m http.server 8744 --bind 127.0.0.1
)

timeout /t 3 /nobreak >nul
start "" "http://localhost:8744/preview.html"

echo.
echo   Opened in your browser.
echo   To stop: close the two black windows.
echo.
timeout /t 5 /nobreak >nul
