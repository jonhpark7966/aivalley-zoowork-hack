"""The free-form path: an agent edits the ad itself inside its ZooWork sandbox.

The sandbox cannot download from YouTube (datacenter IPs are blocked) and this deployment's
workspace file API is unavailable, so files move over a small handoff server exposed through a
Cloudflare quick tunnel: the sandbox pulls the inputs with curl and pushes the finished ad back.
The tunnel starts on first use and each job is reachable only under its own random token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from . import media, zoo

log = logging.getLogger("ugc.director")

STYLE_KEY = "director"
HANDOFF_PORT = 4601
# Only the opening of each video is handed over; it keeps uploads small and the edit quick.
PROXY_SECONDS = 45
SHEET_COLUMNS = 9
TURN_TIMEOUT = 15 * 60
MAX_UPLOAD = 200 * 1024 * 1024

handoff = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_tokens: dict[str, Path] = {}  # token -> that job's handoff directory
_tunnel: asyncio.Task | None = None
_cloudflared: asyncio.subprocess.Process | None = None


def _dir(token: str) -> Path:
    if token not in _tokens:
        raise HTTPException(404)
    return _tokens[token]


@handoff.get("/{token}/in/{name}")
async def pull(token: str, name: str) -> FileResponse:
    path = _dir(token) / "in" / Path(name).name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@handoff.put("/{token}/out/{name}")
async def push(token: str, name: str, request: Request) -> dict:
    out = _dir(token) / "out" / Path(name).name
    out.parent.mkdir(exist_ok=True)
    size = 0
    with out.open("wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise HTTPException(413)
            f.write(chunk)
    log.info("received %s (%d bytes)", out.name, size)
    return {"ok": True, "bytes": size}


async def _start_tunnel() -> str:
    """Serve the handoff app and open a public URL for it. Runs until the process exits."""
    global _cloudflared
    server = uvicorn.Server(uvicorn.Config(handoff, host="127.0.0.1", port=HANDOFF_PORT, log_level="warning"))
    asyncio.create_task(server.serve())
    proc = await asyncio.create_subprocess_exec(
        "cloudflared", "tunnel", "--url", f"http://localhost:{HANDOFF_PORT}", "--no-autoupdate",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _cloudflared = proc
    url = None
    async with asyncio.timeout(40):
        while url is None:
            line = (await proc.stderr.readline()).decode()
            if not line:
                raise RuntimeError("cloudflared exited before giving a URL")
            if m := re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line):
                url = m.group(0)

    async def drain() -> None:  # keep the pipe from filling up
        while await proc.stderr.readline():
            pass

    asyncio.create_task(drain())
    await asyncio.sleep(4)  # the hostname takes a moment to start resolving
    log.info("handoff tunnel ready at %s", url)
    return url


async def tunnel_url() -> str:
    global _tunnel
    if _tunnel is None or (_tunnel.done() and _tunnel.exception()):
        _tunnel = asyncio.create_task(_start_tunnel())
    return await _tunnel


async def close() -> None:
    """Take the public URL down."""
    if _cloudflared and _cloudflared.returncode is None:
        _cloudflared.terminate()


def _make_proxy(src: Path, out: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-t", str(PROXY_SECONDS), "-i", str(src),
            "-vf", "scale='min(720,iw)':-2,fps=30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out),
        ],
        check=True,
    )


def _make_sheet(src: Path, out: Path, seconds: float) -> None:
    """Contact sheet: one frame per second with its timestamp, so the agent can skim the video."""
    frames = max(1, int(min(seconds, PROXY_SECONDS)))
    rows = -(-frames // SHEET_COLUMNS)
    label = r"drawtext=text='%{pts\:hms}':x=6:y=6:fontsize=20:fontcolor=white:box=1:boxcolor=black@0.75:boxborderw=4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-t", str(frames), "-i", str(src),
            "-vf", f"fps=1,scale=-2:300,{label},tile={SHEET_COLUMNS}x{rows}",
            "-frames:v", "1", "-q:v", "4", str(out),
        ],
        check=True,
    )


def _describe(event) -> str | None:
    """One line for the live log: what the director just did."""
    call = zoo.tool_call(event)
    if call and call.phase == "start":
        args = call.args or {}
        if call.tool_name == "exec":
            command = str(args.get("command") or args.get("cmd") or "").strip().splitlines()
            return "$ " + (command[0] if command else "")[:160]
        if call.tool_name in ("write", "edit", "apply_patch"):
            return f"Writing {args.get('path') or args.get('file_path') or 'a file'}"
        if call.tool_name in ("read", "image"):
            target = args.get("path") or args.get("image") or args.get("file_path") or ""
            return f"Looking at {str(target).rsplit('/', 1)[-1] or 'frames'}"
        if call.tool_name == "report_cut":
            return None
        return f"Using {call.tool_name}"
    text = zoo.assistant_text(event).strip()
    return text[:300] if text else None


FetchSource = Callable[[Any, dict], Awaitable[tuple[Path, media.Probe]]]


async def run(
    job,
    keys: list[str],
    *,
    client: zoo.ZooworkClient,
    agent_id: str,
    fetch_source: FetchSource,
    publish: Callable[[Any, Path, list[dict], str], None],
    note: str = "",
) -> None:
    """Hand the selected videos to the director agent and collect the ad it uploads."""
    token = secrets.token_urlsafe(18)
    box = job.dir / "handoff"
    (box / "in").mkdir(parents=True, exist_ok=True)
    _tokens[token] = box
    videos = [job.videos[k] for k in keys]

    async def prepare(video: dict) -> dict | None:
        key = video["key"]
        try:
            path, info = await fetch_source(job, video)
        except Exception as e:
            # One video that will not download should not sink the whole ad.
            log.warning("could not fetch %s: %s", key, e)
            job.emit("clip", key=key, state="error", detail="Could not download this video")
            return None
        await asyncio.to_thread(_make_proxy, path, box / "in" / f"{key}.mp4")
        await asyncio.to_thread(_make_sheet, path, box / "in" / f"{key}_sheet.jpg", info.duration)
        job.emit("clip", key=key, state="handed", detail="")
        return {
            "id": key, "video_file": f"{key}.mp4", "contact_sheet": f"{key}_sheet.jpg",
            "seconds": round(min(info.duration, PROXY_SECONDS), 1),
            "platform": video["platform"], "handle": video["handle"], "creator": video["creator"],
            "followers": video.get("followers"), "views": video.get("views"), "likes": video.get("likes"),
            "official_account": bool(video.get("official")), "title": video["title"],
        }

    try:
        url, *briefs = await asyncio.gather(tunnel_url(), *(prepare(v) for v in videos))
        briefs = [b for b in briefs if b]
        if not briefs:
            raise RuntimeError("none of the selected videos could be downloaded")
        product = job.product or {"name": job.query}
        brief = {
            "product": {k: product.get(k) for k in ("name", "brand", "category", "visual_description")},
            "videos": briefs,
        }
        message = (
            f"Job directory: /workspace/jobs/{job.id}\n"
            f"Input URL: {url}/{token}/in\n"
            f"Upload URL: {url}/{token}/out/ad.mp4\n"
            + (f"Style note from the seller: {note}\n" if note else "")
            + f"\nBrief:\n```json\n{json.dumps(brief, indent=2, ensure_ascii=False)}\n```"
        )
        job.emit("status", stage="render", text="The director is editing in the ZooWork sandbox")
        cuts: dict[str, dict] = {}

        async def report_cut(args: dict) -> list[dict]:
            key = str(args.get("video_id") or "")
            if key not in job.videos:
                raise ValueError(f"unknown video_id; use one of {keys}")
            start, end = float(args["start_s"]), float(args["end_s"])
            caption = str(args.get("caption") or "")[:60]
            cuts[key] = {"key": key, "start": start, "end": end, "caption": caption}
            job.emit("clip", key=key, state="planned", detail=caption,
                     segment={"start": round(start, 1), "end": round(end, 1)})
            return zoo.json_result({"ok": True})

        def on_event(event) -> None:
            if line := _describe(event):
                job.emit("director", text=line)

        outcome, _ = await zoo.run_turn(
            client, agent_id, message, {"report_cut": report_cut}, on_event=on_event, timeout=TURN_TIMEOUT
        )
        result = box / "out" / "ad.mp4"
        if not result.is_file() or result.stat().st_size == 0:
            raise RuntimeError(f"the director finished ({outcome}) without uploading an ad")
        info = await asyncio.to_thread(media.probe, result)
        job.renders += 1
        final = job.dir / f"ad_{job.renders}.mp4"
        result.replace(final)
        log.info("director ad: %.1fs %dx%d", info.duration, info.width, info.height)
        for key in keys:
            job.emit("clip", key=key, state="done" if key in cuts else "skipped",
                     detail=cuts[key]["caption"] if key in cuts else "Not used by the director")
        publish(job, final, [cuts[k] for k in keys if k in cuts], STYLE_KEY)
    finally:
        _tokens.pop(token, None)
