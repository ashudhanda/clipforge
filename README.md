# 🎬 ClipForge

Turn long YouTube videos into viral Shorts — automatically. Pick a niche, pick a
caption style, and ClipForge finds the best moments, crops to 9:16, adds animated
captions, and uploads straight to your channel.

**No server. No monthly cost. Runs on your own computer.**

---

## ✨ What it does

- 🔍 **Auto-discovery** — finds fresh videos in your niches every day
- ✂️ **Smart clipping** — scores moments (hook, energy, emotion) and cuts the best ones
- 📱 **9:16 full-bleed crop** — face-aware, fills the phone screen (no letterboxing)
- 💬 **9 animated caption styles** — karaoke, pop, minimal, hormozi, beast, neon, wordbox, stroke, typewriter (or 🎲 random per clip)
- 🏷️ **Auto titles, descriptions & hashtags** per niche
- 📤 **One-click YouTube upload** via the official YouTube Data API (or manual Studio upload — no login needed)
- 🤖 **3 modes** — Manual (you approve everything), Semi-auto, Full autopilot
- 📊 **Analytics loop** — learns which styles/niches actually get views

## 🚀 Install (5 minutes, beginner-friendly)

**Easiest — one command does everything:**

1. Download this repo (Code → Download ZIP) and unzip it.
2. **Windows:** double-click **`setup.bat`**. **Mac/Linux:** open a terminal in the
   folder and run **`bash setup.sh`**.

That's it — the script checks Python & FFmpeg (installs them automatically if
missing), sets up everything, and opens the dashboard at
**http://127.0.0.1:5057** 🎉 Next time just run the same file again.

<details>
<summary>Manual install (if the script doesn't work for you)</summary>

You need **Python 3.10+** and **FFmpeg** installed first.

**Windows**
1. Install Python from [python.org](https://www.python.org/downloads/) — ✅ tick
   **"Add python.exe to PATH"** during setup.
2. Install FFmpeg: download from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)
   (release full build), unzip, and add its `bin` folder to PATH
   ([how-to](https://www.wikihow.com/Install-FFmpeg-on-Windows)).
3. Download this repo (Code → Download ZIP) and unzip it.

**Mac**
1. Install Python from [python.org](https://www.python.org/downloads/).
2. Install FFmpeg: `brew install ffmpeg`
   (first install [Homebrew](https://brew.sh) if you don't have it).
3. Download this repo (Code → Download ZIP) and unzip it.

**Linux**
```bash
sudo apt install python3 python3-venv ffmpeg   # Debian/Ubuntu
```

**Then, on any system**, open a terminal *inside the unzipped folder* and run:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

The dashboard opens by itself at **http://127.0.0.1:5057** 🎉
</details>

> 💡 **One-click installers:** Windows (`ClipForge-Setup.exe`), macOS
> (`.dmg`) and Linux (tarball) builds are produced automatically by
> [build.yml](.github/workflows/build.yml) — grab them from
> [Releases](https://github.com/ashudhanda/clipforge/releases).
> They're unsigned for now: on Windows click *More info → Run anyway*,
> on macOS right-click → *Open* on first launch. FFmpeg is bundled, so no
> terminal and no Python needed. (To build locally, see `packaging/`.)

## 🧭 First run — no forms, just a 30-second tour

Open the app and you're straight in — no setup questions. Sensible defaults
are pre-filled (every niche selected, karaoke captions, manual mode) and a
quick tour shows what happens where ("yaha se ye hoga").

Everything stays changeable forever in the **⚙️ Settings** card on the same
page: niches, caption style, upload mode, daily schedule, quality gate —
change anything, anytime. Nothing is locked after day one.

## 📤 Connect YouTube (one time, ~5 min)

ClipForge uploads via YouTube's official API. Full click-by-click guide:
**[docs/YOUTUBE_SETUP.md](docs/YOUTUBE_SETUP.md)** — no coding, just clicking.

Short version: create a free Google Cloud project → enable YouTube Data API v3 →
create an OAuth client (Desktop app) → download its JSON → upload it in the
ClipForge dashboard → click **🔗 Connect YouTube** → approve in your browser.

- Free quota: **100 uploads/day** (YouTube's limit for new apps; uploads stay **private** until your app is verified by Google).
- Don't want to connect? Use **Manual mode** — build clips here, upload through YouTube Studio yourself. No login needed.

## 🖥️ Daily use

| Mode | You do |
|---|---|
| **Manual** | Click **Find videos 🔍** → **Make clips 🚀** → preview → download or upload each clip yourself |
| **Semi-auto** | Same, but every clip card has an **⬆ Upload** button |
| **Full autopilot** | Clips build and upload on your schedule; low-scoring ones are skipped |

## 🗂️ Project layout

```
clipforge/
├── app.py              # dashboard: zero forced setup, everything on one page (Flask)
├── core/
│   ├── discovery/      # niche video discovery (yt-dlp search)
│   ├── ingest/         # downloads
│   ├── moments/        # scoring: hook, energy, emotion
│   ├── edit/           # 9:16 crop, 9 caption styles, rendering
│   ├── metadata/       # titles, descriptions, hashtags
│   ├── upload/         # YouTube Data API + OAuth, browser-upload fallback
│   └── analytics/      # view tracking + insights
├── templates/          # dashboard + wizard HTML
├── previews/           # caption style preview videos
└── docs/               # setup guides
```

Your settings and YouTube tokens live **outside** this folder
(`~/.clipforge/`) — never commit them.

## ⚠️ Fair-use note

ClipForge re-edits third-party videos (commentary, captions, crop). Transformative
edits *reduce* but don't eliminate copyright risk — prefer Creative-Commons or
your own source footage for commercial channels, and always add original commentary.

## 🧪 Developers

```bash
.venv/bin/pytest   # full suite (232 tests)
```

## 📄 License

MIT — see [LICENSE](LICENSE).
