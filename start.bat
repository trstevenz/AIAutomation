@echo off
SETLOCAL ENABLEDELAYEDEXPANSION
title AI Web Automation Chat
color 0B

echo.
echo  ============================================================
echo   AI Web Automation Chat  ^|  Playwright + FastAPI + Multi-AI
echo  ============================================================
echo.

:: Always stay in the script's directory
cd /d "%~dp0"

:: ────────────────────────────────────────────────────────────
:: STEP 1 — Python check
:: ────────────────────────────────────────────────────────────
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo  [ERROR] Python not found. Install from https://python.org
    echo.
    pause
    exit /b 1
)
FOR /f "tokens=*" %%v IN ('python --version 2^>^&1') DO (
    echo  [OK] %%v detected.
)

:: ────────────────────────────────────────────────────────────
:: STEP 2 — Create venv if missing
:: ────────────────────────────────────────────────────────────
IF NOT EXIST "venv\Scripts\python.exe" (
    echo  [INFO] Creating virtual environment...
    python -m venv venv
    IF %ERRORLEVEL% NEQ 0 (
        echo  [ERROR] Could not create venv.
        pause
        exit /b 1
    )
    echo  [OK] Virtual environment created.
) ELSE (
    echo  [OK] Virtual environment exists.
)

:: ────────────────────────────────────────────────────────────
:: STEP 3 — Upgrade pip silently
:: ────────────────────────────────────────────────────────────
echo  [INFO] Ensuring pip is up to date...
venv\Scripts\python.exe -m pip install --upgrade pip -q >nul 2>&1
echo  [OK] pip ready.

:: ────────────────────────────────────────────────────────────
:: STEP 4 — Install requirements
:: ────────────────────────────────────────────────────────────
echo  [INFO] Installing Python packages (first run takes 1-2 min)...
venv\Scripts\python.exe -m pip install -r requirements.txt -q
IF %ERRORLEVEL% NEQ 0 (
    echo  [WARN] Silent install had issues — retrying with output...
    venv\Scripts\python.exe -m pip install -r requirements.txt
    IF %ERRORLEVEL% NEQ 0 (
        echo  [ERROR] Package installation failed. See above.
        pause
        exit /b 1
    )
)
echo  [OK] All packages installed.

:: ────────────────────────────────────────────────────────────
:: STEP 5 — Install Playwright Chromium ONLY (no --quiet flag)
:: ────────────────────────────────────────────────────────────
echo  [INFO] Installing Playwright Chromium browser (downloads once ~150MB)...
venv\Scripts\python.exe -m playwright install chromium
echo  [OK] Playwright Chromium ready. (Exit code ignored — already installed is fine)

:: ────────────────────────────────────────────────────────────
:: STEP 6 — Ensure data folders exist
:: ────────────────────────────────────────────────────────────
IF NOT EXIST "data\downloads\"   mkdir "data\downloads"
IF NOT EXIST "data\screenshots\" mkdir "data\screenshots"
echo  [OK] Data folders ready.

:: ────────────────────────────────────────────────────────────
:: STEP 7 — Open browser after 3-second delay (background)
:: ────────────────────────────────────────────────────────────
start "" /b cmd /c "timeout /t 3 /nobreak >nul 2>&1 && start http://localhost:8000"

:: ────────────────────────────────────────────────────────────
:: STEP 8 — START SERVER (window stays open)
:: ────────────────────────────────────────────────────────────
echo.
echo  ============================================================
echo   Server: http://localhost:8000
echo   Press Ctrl+C to stop
echo  ============================================================
echo.

:: IMPORTANT: Use server.py NOT uvicorn CLI.
:: server.py sets WindowsProactorEventLoopPolicy BEFORE uvicorn creates the event loop.
:: This is required for Playwright to spawn its browser subprocess on Windows.
venv\Scripts\python.exe server.py

:: ────────────────────────────────────────────────────────────
:: Server stopped — keep window open so user can read any error
:: ────────────────────────────────────────────────────────────
echo.
echo  [INFO] Server has stopped.
echo  Press any key to close this window...
pause >nul
ENDLOCAL
