@echo off
rem ===================================================================
rem  Maison Neuf launcher
rem  NOTE: keep this file ASCII-only. cmd.exe reads .bat as the system
rem        codepage (CP932 here), so UTF-8 Japanese text corrupts the
rem        commands themselves. Japanese belongs in README.md, not here.
rem ===================================================================
cd /d "%~dp0"
title Maison Neuf launcher

echo.
echo   Maison Neuf
echo   ------------------------------
echo.

rem --- optional extra image folder ---
rem     Put one bare path on line 1 of image_dir.txt. That file is git-ignored,
rem     so a personal folder path never reaches the public repository.
set IMAGE_DIR=
set IMAGE_ARG=
if exist "image_dir.txt" (
    for /f "usebackq delims=" %%A in ("image_dir.txt") do set IMAGE_DIR=%%A
)
if defined IMAGE_DIR set IMAGE_ARG=--image-dir "%IMAGE_DIR%"

rem --- watcher: skip if status.json was touched within the last 15s ---
set WATCHER=0
for /f %%A in ('powershell -NoProfile -Command "if ((Test-Path status.json) -and ((((Get-Date) - (Get-Item status.json).LastWriteTime).TotalSeconds) -lt 15)) { 1 } else { 0 }"') do set WATCHER=%%A

if "%WATCHER%"=="1" (
    echo   [1/2] watcher ... already running
) else (
    echo   [1/2] watcher ... starting
    start "Maison Neuf - watcher" python watcher.py %IMAGE_ARG%
)

rem --- server: skip if port 8744 is already listening ---
set SERVER=0
for /f %%A in ('powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8744 -State Listen -ErrorAction SilentlyContinue) { 1 } else { 0 }"') do set SERVER=%%A

if "%SERVER%"=="1" (
    echo   [2/2] server  ... already running
) else (
    echo   [2/2] server  ... starting
    start "Maison Neuf - server" python -m http.server 8744 --bind 127.0.0.1
)

timeout /t 3 /nobreak >nul
start "" "http://localhost:8744/preview.html"

echo.
echo   Opened in your browser.
echo   To stop: close the two black windows.
echo.
timeout /t 5 /nobreak >nul
