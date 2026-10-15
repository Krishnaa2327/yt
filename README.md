# YT Downloader

A minimal, mobile-first web application for downloading audio (MP3) and video (MP4) from YouTube. Built with Flask + yt-dlp + FFmpeg. Designed for single personal use — no accounts, no database, no nonsense.

---

## Table of Contents

1. [Local Setup on Windows](#1-local-setup-on-windows)
2. [Install FFmpeg](#2-install-ffmpeg)
3. [Run the App Locally](#3-run-the-app-locally)
4. [Push to GitHub](#4-push-to-github)
5. [Deploy to Render (Free)](#5-deploy-to-render-free)
6. [Fix YouTube Bot Detection (Cookies)](#6-fix-youtube-bot-detection-cookies)
7. [Keep yt-dlp Updated](#7-keep-yt-dlp-updated)
8. [Free Hosting Limitations](#8-free-hosting-limitations)
9. [Project Structure](#9-project-structure)

---

## 1. Local Setup on Windows

### Prerequisites
- Python 3.10 or later — download from [python.org](https://www.python.org/downloads/)
  - During install, check **"Add Python to PATH"**
- Git — download from [git-scm.com](https://git-scm.com/)

### Create and activate a virtual environment

Open PowerShell in the project folder:

```powershell
cd d:\Projects\yt-downloader

# Create the virtual environment
python -m venv venv

# Activate it
.\venv\Scripts\Activate.ps1

# If you get an execution-policy error, run this first:
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### Install dependencies

```powershell
pip install -r requirements.txt
```

---

## 2. FFmpeg (Automated & Manual Options)

FFmpeg is required to merge separate video/audio streams (for 720p/1080p) and convert audio to MP3.

### Automated (Default)
`requirements.txt` includes `imageio-ffmpeg`, which automatically bundles a standalone, pre-compiled FFmpeg executable into your Python environment. The app detects this executable automatically on startup without requiring manual Windows PATH configuration!

### Optional System-Wide Install (Alternative)
If you prefer a system-wide FFmpeg installation:

**Via winget (Windows 10/11):**
```powershell
winget install Gyan.FFmpeg
```

**Via Chocolatey:**
```powershell
choco install ffmpeg
```

### Option C — Manual

1. Download a build from [ffmpeg.org/download.html](https://ffmpeg.org/download.html) (e.g., the `essentials` build from gyan.dev)
2. Extract the zip
3. Add the `bin/` folder to your system PATH

### Verify FFmpeg is available

```powershell
ffmpeg -version
```

You should see version information. If you get "command not found", restart your terminal or check your PATH.

---

## 3. Run the App Locally

With the virtual environment active:

```powershell
python app.py
```

Open your browser at **http://localhost:5000**

The app works fully offline from your local machine. No cloud IP blocks, no bot detection — local use is always reliable.

---

## 4. Push to GitHub

```powershell
git init
git add .
git commit -m "Initial commit"

# Create a repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/yt-downloader.git
git branch -M main
git push -u origin main
```

Make sure your `.env` file (if any) and `cookies.txt` are **never committed** — they are already listed in `.gitignore`.

---

## 5. Deploy to Render (Free)

Render's free tier runs the app as a Docker container. It includes 512 MB RAM and spins the container down after 15 minutes of inactivity (cold starts take ~30–50 seconds — normal behaviour).

### Steps

1. Create an account at [render.com](https://render.com) (no credit card required for the free Web Service tier)
2. Click **New → Web Service**
3. Connect your GitHub repository
4. Render will detect `render.yaml` automatically and configure everything
5. Click **Deploy**

The build installs FFmpeg inside the Docker image, so no manual setup is needed on the server.

### Environment variables on Render

In the Render dashboard → your service → **Environment**, you can set:

| Variable | Description |
|---|---|
| `YT_COOKIES_FILE` | Path to a cookies file (see section 6) |
| `MAX_FILE_SIZE_MB` | Hard file-size limit in MB (default: 300) |
| `MAX_CONCURRENT_JOBS` | Max simultaneous downloads (default: 2) |

---

## 6. Fix YouTube Bot Detection (Cookies)

Cloud hosting IP addresses (Render, Railway, etc.) are shared data-centre ranges that YouTube recognises and challenges with bot-detection. If you see errors like:

- *"Sign in to confirm you're not a bot"*
- *"YouTube blocked this request (bot detection)"*

You need to give the server your authenticated YouTube session cookie.

### Export cookies from your browser

**Chrome / Edge (Windows):**
1. Install the browser extension **"Get cookies.txt LOCALLY"** (open source, no account needed)
2. Go to [youtube.com](https://youtube.com) while logged in to your Google account
3. Click the extension icon → **Export** → save as `cookies.txt`

**Firefox:**
1. Install **"cookies.txt"** extension by Rotem Dan
2. Go to [youtube.com](https://youtube.com) while logged in
3. Click the extension icon → download `cookies.txt`

### Upload cookies to Render

1. In the Render dashboard → your service → **Environment**
2. Click **Secret Files → Add Secret File**
3. Filename: `/etc/secrets/cookies.txt`
4. Paste the contents of your `cookies.txt`
5. Set the environment variable:
   - Key: `YT_COOKIES_FILE`
   - Value: `/etc/secrets/cookies.txt`
6. Save and redeploy

> **Security note:** Your cookies.txt grants access to your Google account. Never commit it to Git. Rotate it periodically or create a separate Google account for this purpose.

### Refresh cookies when they expire

YouTube cookies expire after some weeks. If downloads start failing again with bot-detection errors, repeat the export process and update the secret file on Render.

---

## 7. Keep yt-dlp Updated

YouTube regularly changes its internal API. yt-dlp releases updates every few weeks to keep pace. **Running an outdated version is the most common cause of failures.**

### Update locally

```powershell
.\venv\Scripts\Activate.ps1
pip install -U yt-dlp
```

### Update on Render

Render rebuilds the Docker image on every deploy. Redeploy from the dashboard or push any commit to trigger a new build — this installs the latest `yt-dlp` automatically.

You can also pin a specific version in `requirements.txt` for stability:

```
yt-dlp==2025.3.31
```

But prefer keeping the `>=` floor so that routine deploys always pull in recent fixes.

---

## 8. Free Hosting Limitations

| Limitation | Render Free Tier |
|---|---|
| RAM | 512 MB — sufficient for most videos up to ~300 MB |
| Storage | Ephemeral — files are stored in `/tmp` and lost on restart |
| Sleep | Container sleeps after 15 min idle; first request is slow |
| Bandwidth | 100 GB/month outbound (generous for personal use) |
| Build time | 10 min max |
| Concurrent downloads | Capped by `MAX_CONCURRENT_JOBS` (default 2) to avoid OOM |

**What can break on free hosting:**
- YouTube bot detection (see section 6)
- Very large files (>300 MB) may cause OOM — lower `MAX_FILE_SIZE_MB`
- Cold starts on first request after idle period
- yt-dlp version drift (redeploy to fix)

**What always works locally:**
- Everything — local use has none of the above restrictions

---

## 9. Project Structure

```
yt-downloader/
├── app.py              ← Flask backend (job management, yt-dlp, file serving)
├── requirements.txt    ← Python dependencies
├── Procfile            ← Gunicorn start command (PaaS platforms)
├── Dockerfile          ← Container image with FFmpeg
├── render.yaml         ← Render deployment config
├── .gitignore
├── README.md
├── templates/
│   └── index.html      ← Single-page app (served by Flask)
├── static/
│   ├── css/
│   │   └── style.css   ← Dark theme, mobile-first layout
│   └── js/
│       └── app.js      ← State machine, polling, download trigger
└── downloads/
    └── .gitkeep        ← Tracked by git; actual files are gitignored
```

Downloads are processed in the system's temporary directory (`tempfile.mkdtemp`) and deleted automatically after the file is served or after 10 minutes, whichever comes first.

---

## Updating Dependencies

```powershell
# Update all packages
pip install -U -r requirements.txt

# Save the new locked versions (optional but recommended for stability)
pip freeze > requirements.txt
```

Always re-test after updating. yt-dlp updates can occasionally change behaviour.
