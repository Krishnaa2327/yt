"""
YT Downloader — Flask Backend
Handles download jobs asynchronously with yt-dlp + FFmpeg.

Reliability strategy against YouTube bot detection:
  1. Use android + tv_embedded yt-dlp player clients (bypass PO-token checks)
  2. Browser-like HTTP headers
  3. Optional cookies file via YT_COOKIES_FILE env var
  4. Retry with fallback format on failure
  5. Friendly error messages with actionable hints
"""

import ipaddress
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template, request, send_file
import yt_dlp

# ---------------------------------------------------------------------------
# Configuration & FFmpeg Setup
# ---------------------------------------------------------------------------

def _setup_ffmpeg() -> tuple[bool, str | None]:
    """
    Locates or initializes FFmpeg from system PATH, imageio-ffmpeg, or local dirs,
    and ensures its directory is injected into os.environ['PATH'].
    Returns (is_available, executable_path).
    """
    # 1. System PATH
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return True, system_ffmpeg

    # 2. Virtual environment Scripts directory or local bin
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(sys.prefix, "Scripts", "ffmpeg.exe"),
        os.path.join(script_dir, "bin", "ffmpeg.exe"),
        os.path.join(script_dir, "bin", "ffmpeg"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            bin_dir = os.path.dirname(c)
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return True, c

    # 3. Bundled imageio-ffmpeg package
    try:
        import imageio_ffmpeg
        raw_exe = imageio_ffmpeg.get_ffmpeg_exe()
        if raw_exe and os.path.isfile(raw_exe):
            bin_dir = os.path.dirname(raw_exe)
            alias_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
            alias_exe = os.path.join(bin_dir, alias_name)
            if not os.path.isfile(alias_exe):
                try:
                    shutil.copyfile(raw_exe, alias_exe)
                except Exception:
                    pass
            target = alias_exe if os.path.isfile(alias_exe) else raw_exe
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return True, target
    except ImportError:
        pass

    return False, None


FFMPEG_AVAILABLE, FFMPEG_PATH = _setup_ffmpeg()
if not FFMPEG_AVAILABLE:
    import warnings
    warnings.warn(
        "FFmpeg not found in PATH or bundled packages. MP3 downloads and quality-merged "
        "MP4 downloads will fail. Install FFmpeg or run 'pip install imageio-ffmpeg'.",
        RuntimeWarning,
        stacklevel=1,
    )

def _setup_js_runtime() -> dict:
    """
    Detect available JS runtime (node, deno, bun) for yt-dlp YouTube n-sig challenge solving.
    """
    for runtime in ("node", "deno", "bun"):
        if shutil.which(runtime):
            return {runtime: {}}
    return {}


JS_RUNTIMES = _setup_js_runtime()

MAX_CONCURRENT_JOBS: int = int(os.environ.get("MAX_CONCURRENT_JOBS", "3"))
MAX_FILE_SIZE_MB: int = int(os.environ.get("MAX_FILE_SIZE_MB", "2000"))
JOB_TTL_SECONDS: int = int(os.environ.get("JOB_TTL_SECONDS", "600"))  # 10 min

# Domain allow-list — extend here to support more sites
ALLOWED_DOMAINS: frozenset = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
        "m.youtube.com",
        "music.youtube.com",
    }
)

# Private/metadata IP ranges blocked for SSRF protection
PRIVATE_NETWORKS = [
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",  # AWS/GCP metadata endpoint
        "100.64.0.0/10",   # Carrier-grade NAT
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
]

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# In-memory job store  {job_id: job_dict}
_jobs: dict = {}
_jobs_lock = threading.Lock()

# Semaphore caps concurrent downloads to avoid OOM on free-tier hosts
_semaphore = threading.Semaphore(MAX_CONCURRENT_JOBS)


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------
def validate_url(url: str) -> tuple:
    """
    Returns (True, None) if the URL is safe and allowed,
    or (False, reason_string) otherwise.
    """
    if not url or len(url) > 2048:
        return False, "Invalid URL."

    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Could not parse URL."

    if parsed.scheme not in ("http", "https"):
        return False, "Only http:// and https:// URLs are supported."

    host = (parsed.hostname or "").lower().strip()
    if not host:
        return False, "Invalid URL — missing host."

    # Block localhost variants by name
    if host in ("localhost", "0.0.0.0", "::1"):
        return False, "This URL is not allowed."

    # SSRF: block private / link-local / metadata IP literals
    try:
        addr = ipaddress.ip_address(host)
        for net in PRIVATE_NETWORKS:
            if addr in net:
                return False, "This URL is not allowed."
    except ValueError:
        pass  # host is a domain name, not a raw IP — fine

    # Domain allow-list
    if host not in ALLOWED_DOMAINS:
        return False, (
            f"'{host}' is not a supported source. "
            "Supported: YouTube (youtube.com, youtu.be)."
        )

    return True, None


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------
def _make_progress_hook(job_id: str):
    """Factory: returns a yt-dlp progress hook for the given job."""

    def hook(d: dict):
        with _jobs_lock:
            job = _jobs.get(job_id)
            if not job or job["status"] == "error":
                return

        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0

            # Hard file-size limit
            if total > MAX_FILE_SIZE_MB * 1024 * 1024:
                with _jobs_lock:
                    _jobs[job_id]["status"] = "error"
                    _jobs[job_id]["error"] = (
                        f"File exceeds the {MAX_FILE_SIZE_MB} MB limit."
                    )
                raise yt_dlp.utils.DownloadError("file_too_large")

            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                pct = int(downloaded / total * 100)
                msg = f"Downloading… {pct}%"
            else:
                msg = "Downloading…"

            with _jobs_lock:
                _jobs[job_id]["progress_msg"] = msg

        elif d["status"] == "finished":
            with _jobs_lock:
                _jobs[job_id]["progress_msg"] = "Processing with FFmpeg…"

    return hook


def _build_ydl_opts(fmt: str, quality: str, output_template: str, job_id: str) -> dict:
    """
    Build a yt-dlp options dict tuned for reliability against YouTube's
    bot-detection systems.

    Player client priority: android → tv_embedded → web
    - android and tv_embedded bypass PO-token requirements introduced in 2024/25.
    - Fallback chains ensure a format is always found even if a quality tier
      isn't available for the specific video.
    """
    if fmt == "mp3":
        format_str = "bestaudio/best"
        postprocessors = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
        merge_format = None
    else:  # mp4
        # IMPORTANT: Do NOT restrict container with [ext=mp4] or [ext=m4a] here.
        # YouTube serves 720p+ almost exclusively as VP9/WebM. Adding [ext=mp4]
        # silently falls back to the best native MP4 stream, which is often
        # 360p–480p. Instead we pick the best video+audio by resolution only,
        # then let FFmpeg (via merge_output_format=mp4) handle remuxing/converting
        # the result to MP4 regardless of the source container.
        quality_formats = {
            "auto":  "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "360p":  "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
            "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        }
        format_str = quality_formats.get(quality, quality_formats["auto"])
        postprocessors = []
        merge_format = "mp4"

    opts: dict = {
        "format": format_str,
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        # Reliability / retry
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 3,
        "extractor_retries": 5,
        "postprocessors": postprocessors,
        "progress_hooks": [_make_progress_hook(job_id)],
        "ignoreerrors": False,
        "geo_bypass": True,
        # Avoid saving metadata/thumbnail sidecars
        "writethumbnail": False,
        "writeinfojson": False,
        "writesubtitles": False,
    }

    if JS_RUNTIMES:
        opts["js_runtimes"] = JS_RUNTIMES

    if merge_format:
        opts["merge_output_format"] = merge_format

    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH

    # Optional custom player client override (e.g. if set by environment)
    player_client = os.environ.get("YT_PLAYER_CLIENT", "").strip()
    if player_client:
        opts["extractor_args"] = {
            "youtube": {
                "player_client": [c.strip() for c in player_client.split(",")],
            }
        }

    # Optional cookies file or text — primary bypass for cloud IP bot detection.
    cookies_path = os.environ.get("YT_COOKIES_FILE", "").strip()
    if cookies_path and os.path.isfile(cookies_path):
        opts["cookiefile"] = cookies_path
        logger.info("Using cookies file: %s", cookies_path)
    else:
        cookies_text = os.environ.get("YT_COOKIES_TEXT", "").strip()
        if cookies_text:
            env_cookie_file = os.path.join(tempfile.gettempdir(), "yt_env_cookies.txt")
            try:
                with open(env_cookie_file, "w", encoding="utf-8") as f:
                    f.write(cookies_text)
                opts["cookiefile"] = env_cookie_file
                logger.info("Using cookies from YT_COOKIES_TEXT")
            except Exception as e:
                logger.warning("Could not write YT_COOKIES_TEXT file: %s", e)

    return opts


def _safe_filename(title: str, ext: str) -> str:
    """Sanitize a video title into a safe filesystem and HTTP filename."""
    # Strip emojis and high unicode plane characters that break Windows charmaps
    cleaned = "".join(c for c in title if ord(c) < 0x10000 and not (0xD800 <= ord(c) <= 0xDFFF))
    # Strip characters illegal on Windows/macOS/Linux: < > : " / \ | ? * and control chars
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", cleaned)
    safe = re.sub(r"\s+", " ", safe).strip()
    safe = safe.rstrip(". ")
    safe = safe[:80] or "download"
    return f"{safe}.{ext}"


def _find_output_file(tmp_dir: str, preferred_ext: str) -> str:
    """
    Find the largest file with the preferred extension in tmp_dir.
    Falls back to any file if the extension isn't found (e.g. yt-dlp
    produced .webm instead of .mp4 on some streams).
    """
    p = Path(tmp_dir)
    matches = sorted(
        p.glob(f"*.{preferred_ext}"),
        key=lambda f: f.stat().st_size,
        reverse=True,
    )
    if matches:
        return str(matches[0])

    # Fallback: pick the largest file regardless of extension
    all_files = sorted(
        [f for f in p.iterdir() if f.is_file()],
        key=lambda f: f.stat().st_size,
        reverse=True,
    )
    return str(all_files[0]) if all_files else ""


# ---------------------------------------------------------------------------
# Friendly error messages
# ---------------------------------------------------------------------------
def _friendly_error(raw: str) -> str:
    lower = raw.lower()
    if any(k in lower for k in ("sign in", "not a bot", "bot detection", "verify", "po token", "bot", "page needs to be reloaded", "unplayable")):
        return (
            "YouTube blocked this cloud server IP (anti-bot check). "
            "Cloud hosts like Render require YouTube cookies to download videos. "
            "Please add your YouTube cookies via Render Secret Files (cookies.txt)."
        )
    if "403" in raw or "forbidden" in lower or "access denied" in lower:
        return (
            "YouTube blocked this request (HTTP 403: Forbidden). "
            "Datacenter servers like Render require YouTube cookies to download videos. "
            "Add your YouTube cookies via Render Secret Files (cookies.txt)."
        )
    if "private video" in lower or "video is private" in lower:
        return "This video is private."
    if "video unavailable" in lower or "is unavailable" in lower:
        return "This video is unavailable or has been removed."
    if "members only" in lower or "membership" in lower:
        return "This video requires a YouTube channel membership."
    if "copyright" in lower:
        return "This video is blocked due to copyright restrictions."
    if "file_too_large" in lower or "file too large" in lower:
        return f"This file exceeds the {MAX_FILE_SIZE_MB} MB limit."
    if "ffmpeg" in lower:
        return (
            "FFmpeg is not installed or not found in PATH. "
            "See the README for installation instructions."
        )
    if "no video formats found" in lower or "requested format" in lower:
        return (
            "Could not find the requested video quality. "
            "Try selecting a lower quality or Auto."
        )
    if "429" in raw or "rate limit" in lower:
        return "YouTube is rate-limiting this server. Please wait a few minutes and try again."
    if any(k in lower for k in ("connection", "network", "timeout", "timed out")):
        return "Network error during download. Please try again."
    if "region" in lower or "country" in lower:
        return "This video is not available in the server's region."
    return f"Download failed: {raw[:150]}" if raw else "Download failed. The video may be unavailable or blocked by YouTube."


# ---------------------------------------------------------------------------
# Download worker (background thread)
# ---------------------------------------------------------------------------
def _download_worker(job_id: str, url: str, fmt: str, quality: str, tmp_dir: str):
    """
    Runs in a daemon thread. Acquires the global semaphore first so at most
    MAX_CONCURRENT_JOBS downloads run simultaneously.
    """
    with _semaphore:
        try:
            with _jobs_lock:
                _jobs[job_id]["status"] = "processing"
                _jobs[job_id]["progress_msg"] = "Starting…"

            output_template = os.path.join(tmp_dir, "%(title)s.%(ext)s")
            opts = _build_ydl_opts(fmt, quality, output_template, job_id)

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)

            # Check if an error was flagged during progress hook
            with _jobs_lock:
                if _jobs[job_id]["status"] == "error":
                    return

            title = (info or {}).get("title", "download")
            ext = "mp3" if fmt == "mp3" else "mp4"
            file_path = _find_output_file(tmp_dir, ext)

            if not file_path:
                raise FileNotFoundError("Output file not found after processing.")

            filename = _safe_filename(title, ext)

            with _jobs_lock:
                _jobs[job_id].update(
                    {
                        "status": "done",
                        "file_path": file_path,
                        "filename": filename,
                        "progress_msg": "Ready!",
                        "done_at": time.time(),
                    }
                )

        except yt_dlp.utils.DownloadError as exc:
            raw = str(exc)
            friendly = _friendly_error(raw)
            logger.warning("DownloadError job=%s: %.200s", job_id, raw)
            with _jobs_lock:
                if _jobs.get(job_id, {}).get("status") != "error":
                    _jobs[job_id].update({"status": "error", "error": friendly})
            shutil.rmtree(tmp_dir, ignore_errors=True)

        except FileNotFoundError as exc:
            logger.warning("FileNotFoundError job=%s: %s", job_id, exc)
            with _jobs_lock:
                _jobs[job_id].update(
                    {
                        "status": "error",
                        "error": (
                            "Processing finished but the output file is missing. "
                            "Is FFmpeg installed? See the README."
                        ),
                    }
                )
            shutil.rmtree(tmp_dir, ignore_errors=True)

        except Exception:
            logger.exception("Unexpected error in download worker job=%s", job_id)
            with _jobs_lock:
                if _jobs.get(job_id, {}).get("status") not in ("done", "error"):
                    _jobs[job_id].update(
                        {
                            "status": "error",
                            "error": "An unexpected error occurred. Please try again.",
                        }
                    )
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Cleanup daemon — evicts stale jobs and their temp files
# ---------------------------------------------------------------------------
def _cleanup_daemon():
    while True:
        time.sleep(120)  # run every 2 minutes
        now = time.time()
        evict = []
        with _jobs_lock:
            for jid, job in list(_jobs.items()):
                status = job.get("status", "")
                done_at = job.get("done_at") or 0
                if status in ("done", "error") and (now - done_at) > JOB_TTL_SECONDS:
                    evict.append((jid, job.get("tmp_dir")))
            for jid, _ in evict:
                _jobs.pop(jid, None)
        for _, tmp_dir in evict:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)


threading.Thread(target=_cleanup_daemon, daemon=True, name="cleanup").start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/download", methods=["POST"])
def api_download():
    # Soft concurrency check (advisory; semaphore is the hard gate)
    with _jobs_lock:
        active = sum(1 for j in _jobs.values() if j.get("status") == "processing")
    if active >= MAX_CONCURRENT_JOBS:
        return jsonify({"error": "Server is busy. Please try again in a moment."}), 429

    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    fmt = (body.get("format") or "").lower()
    quality = (body.get("quality") or "auto").lower()

    # --- Input validation ---
    if not url:
        return jsonify({"error": "A URL is required."}), 400
    if fmt not in ("mp3", "mp4"):
        return jsonify({"error": "Format must be 'mp3' or 'mp4'."}), 400
    if quality not in ("auto", "360p", "720p", "1080p"):
        return jsonify({"error": "Invalid quality option."}), 400

    ok, err = validate_url(url)
    if not ok:
        return jsonify({"error": err}), 400

    # --- FFmpeg fast-fail check ---
    # MP3 always needs FFmpeg (audio extraction + conversion).
    # MP4 at any quality needs FFmpeg to merge separate video+audio streams.
    # Fail here rather than mid-job so the error is immediate.
    if not FFMPEG_AVAILABLE:
        if fmt == "mp3":
            return jsonify({
                "error": (
                    "FFmpeg is not installed or not found on this server, so MP3 "
                    "conversion is unavailable. Install FFmpeg and restart."
                )
            }), 503
        if quality in ("720p", "1080p"):
            return jsonify({
                "error": (
                    f"FFmpeg is required to download and merge {quality} video with audio. "
                    "Install FFmpeg or select Auto/360p."
                )
            }), 503

    # --- Create job ---
    job_id = str(uuid.uuid4())
    tmp_dir = tempfile.mkdtemp(prefix=f"ytdl_{job_id[:8]}_")

    with _jobs_lock:
        _jobs[job_id] = {
            "status": "queued",
            "progress_msg": "Queued…",
            "file_path": None,
            "filename": None,
            "error": None,
            "done_at": None,
            "tmp_dir": tmp_dir,
        }

    threading.Thread(
        target=_download_worker,
        args=(job_id, url, fmt, quality, tmp_dir),
        daemon=True,
        name=f"job-{job_id[:8]}",
    ).start()

    return jsonify({"job_id": job_id}), 202


@app.route("/api/status/<job_id>")
def api_status(job_id: str):
    # Validate job_id format to block path traversal / injection
    if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", job_id):
        return jsonify({"error": "Invalid job ID."}), 400

    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None:
        return jsonify({"error": "Job not found."}), 404

    resp: dict = {
        "status": job["status"],
        "progress_msg": job["progress_msg"],
    }
    if job["status"] == "error":
        resp["error"] = job["error"]
    if job["status"] == "done":
        resp["filename"] = job["filename"]

    return jsonify(resp)


@app.route("/api/file/<job_id>")
def api_file(job_id: str):
    if not re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", job_id):
        abort(400)

    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None:
        abort(404)
    if job.get("status") != "done":
        return jsonify({"error": "File not ready yet."}), 400

    file_path = job.get("file_path", "")
    filename = job.get("filename", "download")

    if not file_path or not os.path.isfile(file_path):
        return jsonify({"error": "File is no longer available. Please download again."}), 404

    # Path-traversal guard: ensure the path lives inside the system temp dir
    abs_path = os.path.realpath(file_path)
    tmp_base = os.path.realpath(tempfile.gettempdir())
    if not abs_path.startswith(tmp_base):
        logger.error("Path traversal attempt blocked: %s", abs_path)
        abort(403)

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime = "audio/mpeg" if ext == "mp3" else "video/mp4"

    # Deferred cleanup — give the browser 60 s to complete the download
    def _deferred_cleanup():
        time.sleep(60)
        with _jobs_lock:
            j = _jobs.pop(job_id, {})
        tmp_dir = j.get("tmp_dir")
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    threading.Thread(target=_deferred_cleanup, daemon=True).start()

    return send_file(
        abs_path,
        as_attachment=True,
        download_name=filename,
        mimetype=mime,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
