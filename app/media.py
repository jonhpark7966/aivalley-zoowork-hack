"""Local video work: URL parsing, metadata lookup, search, download, frame sampling."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx
import yt_dlp

YOUTUBE_ID = r"[A-Za-z0-9_-]{11}"
YOUTUBE_PATTERNS = [
    re.compile(rf"youtube\.com/shorts/({YOUTUBE_ID})"),
    re.compile(rf"youtube\.com/watch\?(?:.*&)?v=({YOUTUBE_ID})"),
    re.compile(rf"youtu\.be/({YOUTUBE_ID})"),
]
TIKTOK_PATTERN = re.compile(r"tiktok\.com/@([A-Za-z0-9._-]+)/video/(\d{10,25})")

# Shorts can run up to three minutes.
MAX_SHORT_SECONDS = 180


@dataclass
class VideoRef:
    platform: str  # "youtube" | "tiktok"
    video_id: str
    url: str

    @property
    def key(self) -> str:
        return f"{self.platform}_{self.video_id}"


def parse_video_url(url: str) -> VideoRef | None:
    for pattern in YOUTUBE_PATTERNS:
        if m := pattern.search(url):
            return VideoRef("youtube", m.group(1), f"https://www.youtube.com/shorts/{m.group(1)}")
    if m := TIKTOK_PATTERN.search(url):
        return VideoRef("tiktok", m.group(2), f"https://www.tiktok.com/@{m.group(1)}/video/{m.group(2)}")
    return None


async def is_short(http: httpx.AsyncClient, video_id: str) -> bool:
    """YouTube serves /shorts/<id> only for Shorts and redirects regular videos to /watch."""
    try:
        res = await http.head(
            f"https://www.youtube.com/shorts/{video_id}", follow_redirects=False, timeout=10
        )
    except httpx.HTTPError:
        return False
    return res.status_code == 200


class Rejected(Exception):
    """The video cannot be used; the message says why."""


async def lookup(http: httpx.AsyncClient, ref: VideoRef) -> dict:
    """Confirm the video exists and is embeddable; return its public metadata."""
    endpoint = (
        "https://www.youtube.com/oembed"
        if ref.platform == "youtube"
        else "https://www.tiktok.com/oembed"
    )
    if ref.platform == "youtube" and not await is_short(http, ref.video_id):
        raise Rejected("not a YouTube Short (regular landscape video)")
    try:
        res = await http.get(endpoint, params={"url": ref.url, "format": "json"}, timeout=12)
        data = res.json() if res.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        data = {}
    if not data.get("title") and not data.get("author_name"):
        raise Rejected("video not found or not embeddable")

    if ref.platform == "youtube":
        handle = (data.get("author_url") or "").rstrip("/").rsplit("/", 1)[-1]
        thumbnail = f"https://i.ytimg.com/vi/{ref.video_id}/oar2.jpg"
    else:
        handle = "@" + (data.get("author_unique_id") or ref.url.split("@")[1].split("/")[0])
        thumbnail = data.get("thumbnail_url") or ""
    return {
        "title": data.get("title") or "",
        "creator": data.get("author_name") or handle,
        "handle": handle if handle.startswith("@") else f"@{handle}",
        "creator_url": data.get("author_url") or "",
        "thumbnail": thumbnail,
    }


def _search_youtube(query: str, limit: int) -> list[dict]:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit * 3}:{query} #shorts", download=False)
    found = []
    for entry in info.get("entries") or []:
        duration = entry.get("duration")
        if not entry.get("id") or not duration or duration > MAX_SHORT_SECONDS:
            continue
        found.append(
            {
                "id": entry["id"],
                "url": f"https://www.youtube.com/shorts/{entry['id']}",
                "title": entry.get("title"),
                "channel": entry.get("channel") or entry.get("uploader"),
                "duration_s": int(duration),
                "views": entry.get("view_count"),
            }
        )
    return found


async def search_youtube(http: httpx.AsyncClient, query: str, limit: int = 10) -> list[dict]:
    """Short-length search results, keeping only real (vertical) Shorts."""
    found = await asyncio.to_thread(_search_youtube, query, limit)
    checks = await asyncio.gather(*(is_short(http, v.pop("id")) for v in found))
    return [v for v, ok in zip(found, checks) if ok][:limit]


def _download(ref: VideoRef, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{ref.key}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "outtmpl": str(dest_dir / f"{ref.key}.%(ext)s"),
        "format": "bv*+ba/b",
        # Prefer 720p-wide H.264 so decoding and rendering stay fast.
        "format_sort": ["res:720", "vcodec:h264", "acodec:m4a"],
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([ref.url])
    if not out.exists():
        raise RuntimeError(f"download produced no file for {ref.url}")
    return out


async def download(ref: VideoRef, dest_dir: Path) -> Path:
    return await asyncio.to_thread(_download, ref, dest_dir)


@dataclass
class Probe:
    duration: float
    width: int
    height: int
    has_audio: bool


def probe(path: Path) -> Probe:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        check=True,
    ).stdout
    data = json.loads(out)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    return Probe(
        duration=float(data["format"]["duration"]),
        width=int(video["width"]),
        height=int(video["height"]),
        has_audio=any(s["codec_type"] == "audio" for s in data["streams"]),
    )


FRAME_HEIGHT = 640


def _grab_frame(path: Path, t: float, label: str) -> bytes:
    """One JPEG frame at time t, with its label burned into the top-left corner."""
    draw = (
        f"drawtext=text='{label}':x=8:y=8:fontsize=26:fontcolor=white:"
        "box=1:boxcolor=black@0.75:boxborderw=6"
    )
    return subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
            "-frames:v", "1", "-vf", f"scale=-2:{FRAME_HEIGHT},{draw}",
            "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout


async def sample_frames(path: Path, times: list[float]) -> list[bytes]:
    """Frames at the given times; the label is the frame's index and timestamp."""
    jobs = [
        asyncio.to_thread(_grab_frame, path, t, f"#{i}  t={t:.1f}s".replace(":", r"\:"))
        for i, t in enumerate(times)
    ]
    return list(await asyncio.gather(*jobs))
