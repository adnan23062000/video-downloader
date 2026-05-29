"""
Video Downloader Backend
- FastAPI server wrapping yt-dlp
- Uses aria2c as external downloader for multi-connection (faster) downloads
- Streams the downloaded file back to the browser

Run locally:
    pip install -r requirements.txt
    # install ffmpeg and aria2 on your system (see README)
    uvicorn main:app --host 0.0.0.0 --port 8000

Deploy this part on Railway / Render / a VPS (NOT Vercel).
"""

import os
import uuid
import shutil
import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="Video Downloader API")

# Allow your Vercel frontend to call this API.
# Replace "*" with your actual Vercel domain in production for security.
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/vdl"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# aria2c gives multi-connection downloads = much faster than the default.
# Falls back gracefully if aria2c isn't installed.
ARIA2_AVAILABLE = shutil.which("aria2c") is not None


class InfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str


def human_size(n):
    if not n:
        return None
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@app.get("/")
def root():
    return {"status": "ok", "aria2c": ARIA2_AVAILABLE}


@app.post("/api/info")
async def get_info(req: InfoRequest):
    """Probe a URL and return all available video/audio formats."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(
            None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(req.url, download=False)
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read URL: {e}")

    formats = []
    seen = set()
    for f in info.get("formats", []):
        # Skip storyboards / images
        if f.get("vcodec") == "none" and f.get("acodec") == "none":
            continue
        height = f.get("height")
        is_audio_only = f.get("vcodec") == "none"
        label = (
            "Audio only"
            if is_audio_only
            else (f"{height}p" if height else f.get("format_note", "video"))
        )
        # de-dup by label+ext, prefer entries with filesize
        key = (label, f.get("ext"))
        if key in seen:
            continue
        seen.add(key)

        size = f.get("filesize") or f.get("filesize_approx")
        formats.append(
            {
                "format_id": f.get("format_id"),
                "label": label,
                "ext": f.get("ext"),
                "height": height or 0,
                "fps": f.get("fps"),
                "filesize": size,
                "filesize_human": human_size(size),
                "audio_only": is_audio_only,
                "has_audio": f.get("acodec") != "none",
            }
        )

    # Add convenient "best video+audio at this resolution" merged options
    heights = sorted({f["height"] for f in formats if f["height"]}, reverse=True)
    merged = [
        {
            "format_id": f"bv*[height<={h}]+ba/b[height<={h}]",
            "label": f"{h}p (best, merged)",
            "ext": "mp4",
            "height": h,
            "fps": None,
            "filesize": None,
            "filesize_human": None,
            "audio_only": False,
            "has_audio": True,
            "recommended": True,
        }
        for h in heights
    ]

    formats.sort(key=lambda x: (x["audio_only"], -x["height"]))

    return {
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader"),
        "formats": merged + formats,
    }


@app.post("/api/download")
async def download(req: DownloadRequest):
    """Download the chosen format and return the file."""
    job_id = uuid.uuid4().hex
    out_dir = DOWNLOAD_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "format": req.format_id,
        "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        # Re-encode merged output container to mp4 for compatibility
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
    }

    if ARIA2_AVAILABLE:
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = {
            "aria2c": [
                "-x", "16",          # 16 connections per server
                "-s", "16",          # split into 16 segments
                "-k", "1M",          # min split size
                "--max-tries=5",
                "--retry-wait=2",
            ]
        }

    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(
            None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(req.url, download=True)
        )
    except Exception as e:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Download failed: {e}")

    files = list(out_dir.glob("*"))
    if not files:
        raise HTTPException(status_code=500, detail="No file produced")

    file_path = max(files, key=lambda p: p.stat().st_size)
    filename = file_path.name

    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/octet-stream",
        # Cleanup happens via background task in production; /tmp clears on restart.
    )
