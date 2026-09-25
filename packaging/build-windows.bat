@echo off
REM Double-click local build for Windows: fetches FFmpeg, builds with PyInstaller,
REM then wraps dist\ClipForge into a Setup.exe via Inno Setup.
setlocal
cd /d "%~dp0\.."

where python >nul 2>nul || (echo Install Python 3.10+ first: https://www.python.org/downloads/ && pause && exit /b 1)

python -m pip install --quiet -r requirements.txt pyinstaller || exit /b 1

powershell -ExecutionPolicy Bypass -File packaging\fetch-ffmpeg.ps1 || exit /b 1

pyinstaller packaging\clipforge.spec || exit /b 1

where iscc >nul 2>nul
if %errorlevel%==0 (
    iscc packaging\installer.iss
    echo.
    echo DONE: dist\ClipForge-Setup-*.exe
) else (
    echo Inno Setup not found - install it from https://jrsoftware.org/isinfo.php
    echo Portable build ready at: dist\ClipForge\ClipForge.exe
)
pause
