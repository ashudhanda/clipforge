@echo off
setlocal
title ClipForge Setup
echo ================================================
echo   ClipForge - one-click setup
echo ================================================
echo.

REM ---------- Step 1: Python ----------
call :FindPython
if not defined PYBIN (
    echo [*] Python nahi mila - winget se install kar raha hoon...
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements >nul 2>&1
    call :RefreshPath
    call :FindPython
)
if not defined PYBIN (
    echo [ERROR] Python install nahi ho paya.
    echo manually install karo: https://www.python.org/downloads/
    echo install ke time "Add python.exe to PATH" wala tick zaroor lagana.
    pause
    exit /b 1
)
echo [OK] Python mil gaya.

REM ---------- Step 2: FFmpeg ----------
call :FindFFmpeg
if not defined FFBIN (
    echo [*] FFmpeg nahi mila - winget se install kar raha hoon...
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements >nul 2>&1
    call :RefreshPath
    call :FindFFmpeg
)
if not defined FFBIN (
    echo [WARN] FFmpeg auto-install nahi ho paya.
    echo manually: https://www.gyan.dev/ffmpeg/builds/ se download karke
    echo uske "bin" folder ko PATH me add karo. Bina FFmpeg ke ClipForge nahi chalega.
    pause
) else (
    echo [OK] FFmpeg mil gaya.
    REM app ko ffmpeg ka path bata do (PATH me na ho tab bhi chalega)
    for %%f in ("%FFBIN%") do (
        set "CLIPFORGE_FFMPEG=%%~ff"
        if exist "%%~dpffprobe.exe" set "CLIPFORGE_FFPROBE=%%~dpffprobe.exe"
    )
)

REM ---------- Step 3: virtual environment ----------
if not exist ".venv\Scripts\python.exe" (
    echo [*] Virtual environment bana raha hoon (pehli baar, 1 min)...
    "%PYBIN%" -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Virtual environment nahi bana. Python reinstall karke dobara chalao.
        pause
        exit /b 1
    )
)
echo [OK] Virtual environment ready.

REM ---------- Step 4: libraries ----------
echo [*] Libraries install ho rahi hain (pehli baar 2-5 min lag sakte hain)...
.venv\Scripts\python -m pip install --upgrade pip >nul 2>&1
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Libraries install nahi huin. Internet check karke dobara chalao.
    pause
    exit /b 1
)
echo [OK] Sab libraries install ho gayin.

REM ---------- Step 5: run ----------
echo.
echo ================================================
echo   Sab ready! ClipForge start ho raha hai...
echo   Browser me khulega: http://127.0.0.1:5057
echo   Band karne ke liye yahan Ctrl+C dabao.
echo ================================================
echo.
.venv\Scripts\python app.py
pause
goto :eof

REM ---------- subroutines ----------
:FindPython
set "PYBIN="
python --version >nul 2>&1
if not errorlevel 1 set "PYBIN=python" & goto :eof
py --version >nul 2>&1
if not errorlevel 1 set "PYBIN=py" & goto :eof
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYBIN=%LocalAppData%\Programs\Python\Python312\python.exe" & goto :eof
if exist "%ProgramFiles%\Python312\python.exe" set "PYBIN=%ProgramFiles%\Python312\python.exe" & goto :eof
goto :eof

:FindFFmpeg
set "FFBIN="
ffmpeg -version >nul 2>&1
if not errorlevel 1 set "FFBIN=ffmpeg" & goto :eof
for /d %%d in ("%LocalAppData%\Microsoft\WinGet\Packages\Gyan.FFmpeg_*") do (
    for /d %%e in ("%%d\ffmpeg-*-full_build") do (
        if exist "%%e\bin\ffmpeg.exe" set "FFBIN=%%e\bin\ffmpeg.exe"
    )
)
goto :eof

:RefreshPath
set "UPATH="
set "MPATH="
for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "UPATH=%%b"
for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "MPATH=%%b"
if defined MPATH set "PATH=%MPATH%;%UPATH%"
if not defined MPATH if defined UPATH set "PATH=%UPATH%"
goto :eof
