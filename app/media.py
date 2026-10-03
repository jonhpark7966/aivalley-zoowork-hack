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


BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _video_stats(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "views": info.get("view_count"),
        "likes": info.get("like_count"),
        "followers": info.get("channel_follower_count"),
        "duration": info.get("duration"),
        "verified": bool(info.get("channel_is_verified")),
    }


async def stats(http: httpx.AsyncClient, ref: VideoRef, handle: str) -> dict:
    """Views and likes for the video, follower count for its creator. Missing values are None."""
    try:
        found = await asyncio.to_thread(_video_stats, ref.url)
    except Exception:
        found = {}
    if ref.platform == "tiktok":
        # The video page has no follower count; the creator's profile page embeds it.
        try:
            res = await http.get(
                f"https://www.tiktok.com/{handle}", headers={"User-Agent": BROWSER_UA}, timeout=12
            )
            if m := re.search(r'"followerCount":(\d+)', res.text):
                found["followers"] = int(m.group(1))
            if m := re.search(r'"verified":(true|false)', res.text):
                found["verified"] = m.group(1) == "true"
        except httpx.HTTPError:
            pass
    return found


# YouTube search filter "Type: Shorts".
SHORTS_FILTER = "EgIQCQ=="


async def search_youtube(http: httpx.AsyncClient, query: str, limit: int = 10) -> list[dict]:
    """Shorts matching the query, read from YouTube's own search results page."""
    res = await http.get(
        "https://www.youtube.com/results",
        params={"search_query": query, "sp": SHORTS_FILTER},
        headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
        timeout=15,
    )
    found: dict[str, dict] = {}
    for m in re.finditer(rf"/shorts/({YOUTUBE_ID})", res.text):
        video_id = m.group(1)
        if video_id in found:
            continue
        # Each result carries a label like "Title, 74 thousand views - play Short".
        at = res.text.find(video_id)
        label = re.search(r'"accessibilityText":"((?:[^"\\]|\\.){5,300})"', res.text[at:at + 2500])
        title, _, views = (label.group(1) if label else "").rpartition(", ")
        found[video_id] = {
            "url": f"https://www.youtube.com/shorts/{video_id}",
            "title": title,
            "views": views.removesuffix(" - play Short"),
        }
        if len(found) >= limit:
            break
    return list(found.values())


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


def filmstrip(path: Path, out: Path, duration: float, frames: int = 12) -> Path:
    """One wide image of evenly spaced frames, used to show where a clip was cut from."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(path),
            "-vf", f"fps={frames / max(duration, 0.1):.5f},scale=-2:128,tile={frames}x1",
            "-frames:v", "1", "-q:v", "4", str(out),
        ],
        check=True,
    )
    return out


async def sample_frames(path: Path, times: list[float]) -> list[bytes]:
    """Frames at the given times; the label is the frame's index and timestamp."""
    jobs = [
        asyncio.to_thread(_grab_frame, path, t, f"#{i}  t={t:.1f}s".replace(":", r"\:"))
        for i, t in enumerate(times)
    ]
    return list(await asyncio.gather(*jobs))
