#!/usr/bin/env bash
# ClipForge one-click setup (Mac / Linux).
# Run:  bash setup.sh
set -e
cd "$(dirname "$0")"

echo "================================================"
echo "  ClipForge - one-click setup"
echo "================================================"
echo ""

# ---------- Step 1: Python ----------
if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] python3 nahi mila."
    echo "Mac: https://www.python.org/downloads/ se install karo"
    echo "Linux: sudo apt install python3 python3-venv"
    exit 1
fi
echo "[OK] $(python3 --version 2>&1)"

# ---------- Step 2: FFmpeg ----------
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[*] FFmpeg nahi mila, install kar raha hoon..."
    if command -v brew >/dev/null 2>&1; then
        brew install ffmpeg
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y ffmpeg
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y ffmpeg
    else
        echo "[ERROR] FFmpeg manually install karo: https://ffmpeg.org/download.html"
        exit 1
    fi
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[ERROR] FFmpeg install nahi ho paya. Manually install karke dobara chalao."
    exit 1
fi
echo "[OK] $(ffmpeg -version 2>/dev/null | head -n 1)"

# ---------- Step 3: virtual environment ----------
if [ ! -x ".venv/bin/python" ]; then
    echo "[*] Virtual environment bana raha hoon (pehli baar, 1 min)..."
    python3 -m venv .venv
fi
echo "[OK] Virtual environment ready."

# ---------- Step 4: libraries ----------
echo "[*] Libraries install ho rahi hain (pehli baar 2-5 min lag sakte hain)..."
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r requirements.txt
echo "[OK] Sab libraries install ho gayin."

# ---------- Step 5: run ----------
echo ""
echo "================================================"
echo "  Sab ready! ClipForge start ho raha hai..."
echo "  Browser me khulega: http://127.0.0.1:5057"
echo "  Band karne ke liye Ctrl+C dabao."
echo "================================================"
echo ""
exec .venv/bin/python app.py
