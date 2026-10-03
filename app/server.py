"""Local web demo: a ZooWork agent scouts videos, then selected ones are cut into one ad."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import director, hype, media, render, zoo

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ugc")

STATIC_DIR = Path(__file__).parent / "static"
JOBS_DIR = Path("data/jobs")
MAX_VIDEOS = 15
MAX_PER_PLATFORM = 9
LANE_TARGET = 5
MAX_SELECTED = 6
OVERVIEW_FRAMES = 14
DENSE_FRAMES = 12
# Only the opening of a long video is scanned; hooks and product shots come early.
SCAN_SECONDS = 60
MIN_CLIP, MAX_CLIP = 2.5, 6.0
# A box this large is the whole shot, not something to draw a ring around.
MAX_BOX_AREA = 0.55
SPOTTER_TIMEOUT = 90
# A healthy spotter session never goes this long without an event; a stalled one never recovers.
SPOTTER_IDLE = 30
# Opt-in: let the Claude Code CLI on this machine edit the ad. Only meaningful when self-hosting.
LOCAL_CLAUDE_KEY = "local"
LOCAL_CLAUDE = os.environ.get("LOCAL_CLAUDE") == "1" and shutil.which("claude") is not None


@dataclass
class Job:
    id: str
    query: str
    dir: Path
    product: dict[str, Any] | None = None
    videos: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    renders: int = 0
    busy: bool = False

    def emit(self, type: str, **data: Any) -> None:
        event = {"type": type, **data}
        self.events.append(event)
        for queue in self.subscribers:
            queue.put_nowait(len(self.events) - 1)


class State:
    client: zoo.ZooworkClient
    http: httpx.AsyncClient
    agents: asyncio.Task  # resolves to {"scout": agent_id, "spotter": agent_id}
    jobs: dict[str, Job] = {}


state = State()


async def _provision() -> dict[str, str]:
    specs = zoo.specs()

    async def ensure(spec: zoo.AgentSpec) -> str:
        # The platform sometimes answers "temporarily unavailable" right after a deploy or delete.
        for attempt in range(4):
            try:
                return await zoo.ensure_agent(state.client, spec)
            except zoo.ZooworkError as e:
                if attempt == 3:
                    raise
                log.warning("provisioning %s failed, retrying: %s", spec.key, e)
                await asyncio.sleep(2 + 2 * attempt)
        raise AssertionError("unreachable")

    ids = await asyncio.gather(*(ensure(s) for s in specs))
    log.info("agents ready: %s", dict(zip((s.key for s in specs), ids)))
    return {s.key: agent_id for s, agent_id in zip(specs, ids)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    state.client = zoo.make_client()
    state.http = httpx.AsyncClient(
        follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (Macintosh) ugc-ad-maker"}
    )
    state.agents = asyncio.create_task(_provision())
    yield
    await director.close()
    await state.http.aclose()
    await state.client.aclose()


app = FastAPI(lifespan=lifespan)

# The page can also be hosted elsewhere (e.g. on Vercel) and drive this engine on localhost.
# Only the origins listed in HOSTED_ORIGINS may do that.
HOSTED_ORIGINS = [o.strip() for o in os.environ.get("HOSTED_ORIGINS", "").split(",") if o.strip()]
if HOSTED_ORIGINS:
    # allow_private_network: Chrome asks for it before a public page may call a server on localhost.
    app.add_middleware(
        CORSMiddleware, allow_origins=HOSTED_ORIGINS, allow_methods=["*"], allow_headers=["*"],
        allow_private_network=True,
    )


# --- Scout: find videos -------------------------------------------------------------------

STEP_TITLES = {
    "web_search": "Searching the web",
    "web_fetch": "Reading page",
    "search_youtube_shorts": "Searching YouTube Shorts",
    "tavily_search": "Searching with Tavily",
    "image": "Looking at an image",
}


def _step_detail(args: dict[str, Any] | None) -> str:
    args = args or {}
    for key in ("query", "q", "url"):
        if isinstance(args.get(key), str):
            return args[key]
    return next((v for v in args.values() if isinstance(v, str)), "")


def _progress_hook(job: Job):
    """Turn the agent's tool activity into progress steps for the page."""

    def hook(event) -> None:
        call = zoo.tool_call(event)
        if call is None or call.tool_name not in STEP_TITLES:
            return
        if call.phase == "start":
            job.emit("step", id=call.tool_call_id, state="running",
                     title=STEP_TITLES[call.tool_name], detail=_step_detail(call.args))
        else:
            failed = call.phase == "blocked" or bool(call.is_error)
            job.emit("step", id=call.tool_call_id, state="error" if failed else "done")

    return hook


# Each lane is one scout session; they run side by side so the list fills quickly.
SEARCH_LANES = [
    ("youtube", "YouTube Shorts", "honest reviews and unboxings by independent creators", None),
    ("youtube", "YouTube Shorts", "everyday use, hauls and 'is it worth it' takes, plus anything from the brand's own channel", None),
    ("tiktok", "TikTok", "honest reviews and unboxings by independent creators", None),
    ("tiktok", "TikTok", "everyday use, hauls and viral moments, plus anything from the brand's own account", None),
]
# Two more scouts search through Tavily when a key is configured.
TAVILY_LANES = [
    ("youtube", "YouTube Shorts", "popular creator videos: favourites, comparisons, 'things I love' lists", "tavily_search"),
    ("tiktok", "TikTok", "popular creator videos: favourites, restocks, day-in-the-life clips", "tavily_search"),
]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _is_official(job: Job, video: dict) -> bool:
    """The scout said so, or the account is named after the brand."""
    brand = _norm((job.product or {}).get("brand") or "")
    named_after_brand = len(brand) >= 4 and (brand in _norm(video["handle"]) or brand in _norm(video["creator"]))
    return bool(video.get("flagged_official")) or named_after_brand


async def run_scout(job: Job) -> None:
    job.busy = True
    claimed: set[str] = set()
    per_platform = {"youtube": 0, "tiktok": 0}
    stats_limit = asyncio.Semaphore(5)
    background: set[asyncio.Task] = set()

    async def set_product(args: dict) -> list[dict]:
        job.product = {
            "name": str(args.get("name") or job.query)[:60],
            "brand": args.get("brand") or "",
            "category": args.get("category") or "",
            "visual_description": args.get("visual_description") or "",
            "keywords": args.get("search_keywords") or [],
            "url": args.get("product_url") or "",
        }
        job.emit("product", product=job.product)
        # The brand is known now: videos that arrived earlier may turn out to be official.
        for video in job.videos.values():
            if not video["official"] and _is_official(job, video):
                video["official"] = True
                job.emit("video_update", key=video["key"], official=True)
        return zoo.json_result({"ok": True})

    async def search_youtube_shorts(args: dict) -> list[dict]:
        limit = max(1, min(int(args.get("max_results") or 10), 15))
        return zoo.json_result(await media.search_youtube(state.http, str(args["query"]), limit))

    async def add_stats(ref: media.VideoRef, video: dict) -> None:
        async with stats_limit:
            found = await media.stats(state.http, ref, video["handle"])
        video.update(found)
        job.emit("video_update", key=video["key"], **found)

    async def submit_video(args: dict, via: str = "") -> list[dict]:
        ref = media.parse_video_url(str(args.get("url") or ""))
        if ref is None:
            return zoo.json_result({"accepted": False, "reason": "not a YouTube Shorts or TikTok video URL"})
        if len(claimed) >= MAX_VIDEOS:
            return zoo.json_result({"accepted": False, "reason": "enough videos collected, stop searching"})
        if per_platform[ref.platform] >= MAX_PER_PLATFORM:
            return zoo.json_result({"accepted": False, "reason": f"enough {ref.platform} videos collected, stop searching"})
        if ref.key in claimed:
            return zoo.json_result({"accepted": False, "reason": "already submitted"})
        claimed.add(ref.key)
        per_platform[ref.platform] += 1
        try:
            meta = await media.lookup(state.http, ref)
        except media.Rejected as e:
            claimed.discard(ref.key)
            per_platform[ref.platform] -= 1
            return zoo.json_result({"accepted": False, "reason": str(e)})
        video = {
            "key": ref.key, "platform": ref.platform, "video_id": ref.video_id, "url": ref.url,
            "reason": str(args.get("reason") or ""), "flagged_official": bool(args.get("is_official")),
            "views": None, "likes": None, "followers": None, "duration": None, "verified": False,
            "via": via, **meta,
        }
        video["official"] = _is_official(job, video)
        job.videos[ref.key] = video
        job.emit("video", video=video)
        task = asyncio.create_task(add_stats(ref, video))
        background.add(task)
        task.add_done_callback(background.discard)
        return zoo.json_result({"accepted": True, "total": len(job.videos)})

    async def tavily_search(args: dict) -> list[dict]:
        return zoo.json_result(await media.tavily_search(
            state.http, os.environ["TAVILY_API_KEY"], str(args["query"]), str(args.get("platform") or "tiktok"),
        ))

    handlers = {
        "set_product": set_product, "search_youtube_shorts": search_youtube_shorts,
        "tavily_search": tavily_search, "submit_video": submit_video,
    }
    lanes = SEARCH_LANES + (TAVILY_LANES if os.environ.get("TAVILY_API_KEY") else [])

    async def identify() -> None:
        await zoo.run_turn(
            state.client, agents["scout"],
            f'Task: IDENTIFY\n\nProduct input from the seller:\n"""\n{job.query}\n"""',
            handlers, on_event=_progress_hook(job), timeout=120,
        )

    async def search(platform: str, label: str, angle: str, tool: str | None) -> str:
        product = job.product["name"] if job.product else job.query
        hints = ", ".join((job.product or {}).get("keywords") or [])
        message = (
            f"Task: SEARCH\n\nProduct: {product}\n"
            + (f"Also known as: {hints}\n" if hints else "")
            + f"Platform: {label}\nAngle: {angle}\n"
            + (f"Search tool: {tool} (platform \"{platform}\")\n" if tool else "")
            + f"Submit up to {LANE_TARGET} videos."
        )
        # Remember which search found each video, so the card can say so.
        lane_handlers = handlers
        if tool == "tavily_search":
            lane_handlers = {**handlers, "submit_video": lambda args: submit_video(args, via="Tavily")}
        try:
            outcome, _ = await zoo.run_turn(
                state.client, agents["scout"], message, lane_handlers, on_event=_progress_hook(job), timeout=150
            )
            return outcome
        except Exception as e:
            log.warning("scout lane %s/%s failed: %s", platform, angle[:20], e)
            return "failed"

    try:
        job.emit("status", stage="scout", text="Waking up the scout agents")
        agents = await state.agents
        job.emit("status", stage="scout", text=f"{len(lanes)} scouts are searching in parallel")
        if "http://" in job.query or "https://" in job.query:
            # A link has to be read before anyone knows what to search for.
            await identify()
            outcomes = await asyncio.gather(*(search(*lane) for lane in lanes))
        else:
            outcomes = (await asyncio.gather(identify(), *(search(*lane) for lane in lanes)))[1:]
        if not job.videos:
            raise RuntimeError(f"the scouts found nothing ({', '.join(outcomes)})")
        if background:
            await asyncio.wait(background, timeout=20)
        official = sum(1 for v in job.videos.values() if v["official"])
        summary = (
            f"Found {len(job.videos)} videos: {per_platform['youtube']} YouTube Shorts and "
            f"{per_platform['tiktok']} TikToks" + (f", {official} from official accounts." if official else ".")
        )
        job.emit("scout_done", summary=summary, count=len(job.videos))
    except Exception as e:
        log.exception("scout failed")
        job.emit("error", stage="scout", message=f"{type(e).__name__}: {e}")
    finally:
        job.busy = False


# --- Spotter and render: make the ad -------------------------------------------------------


def _image_block(jpeg: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()},
    }


async def spot_product(job: Job, video: dict, path: Path, info: media.Probe, agent_id: str) -> dict | None:
    """Ask the spotter agent for the best segment and the product's box in it."""
    scan = min(info.duration, SCAN_SECONDS)
    result: dict[str, Any] = {}

    async def get_frames(args: dict) -> list[dict]:
        if args.get("start_s") is None or args.get("end_s") is None:
            times = [scan * (i + 0.5) / OVERVIEW_FRAMES for i in range(OVERVIEW_FRAMES)]
            job.emit("clip", key=video["key"], state="spotting", detail="watching the whole video")
        else:
            start = max(0.0, min(float(args["start_s"]), info.duration - 1))
            end = max(start + 1, min(float(args["end_s"]), info.duration, start + MAX_CLIP))
            times = [start + (end - start) * i / (DENSE_FRAMES - 1) for i in range(DENSE_FRAMES)]
            job.emit("clip", key=video["key"], state="spotting",
                     detail=f"tracking the product from {start:.1f}s to {end:.1f}s",
                     candidate={"start": round(start, 1), "end": round(end, 1)})
        frames = await media.sample_frames(path, times)
        listing = ", ".join(f"#{i} t={t:.1f}s" for i, t in enumerate(times))
        return [{"type": "text", "text": f"{len(frames)} frames in order: {listing}"}, *map(_image_block, frames)]

    async def submit_highlight(args: dict) -> list[dict]:
        if args.get("product_visible") is False:
            result.update(visible=False)
            return zoo.json_result({"ok": True})
        start, end = float(args["start_s"]), float(args["end_s"])
        if not 0 <= start < end:
            raise ValueError("start_s must be before end_s")
        keyframes = []
        for item in args.get("boxes") or []:
            ymin, xmin, ymax, xmax = (max(0.0, min(float(v) / 1000, 1.0)) for v in item["box_2d"])
            area = (xmax - xmin) * (ymax - ymin)
            if xmax - xmin < 0.02 or ymax - ymin < 0.02 or area > MAX_BOX_AREA:
                continue
            keyframes.append(render.Keyframe(float(item["t"]), (xmin, ymin, xmax, ymax)))
        result.update(visible=True, start=start, end=end, keyframes=keyframes,
                      caption=str(args.get("caption") or "")[:40])
        return zoo.json_result({"ok": True, "boxes_used": len(keyframes)})

    product = job.product or {"name": job.query, "visual_description": ""}
    message = (
        f"Product: {product['name']}\n"
        f"What it looks like: {product.get('visual_description') or 'unknown'}\n\n"
        f"Video id: {video['key']}\n"
        f"Title: {video['title']}\n"
        f"Length: {info.duration:.1f}s\n"
    )
    # A session occasionally stalls; a fresh one almost always goes through.
    for attempt in range(2):
        try:
            outcome, _ = await zoo.run_turn(
                state.client, agent_id, message,
                {"get_frames": get_frames, "submit_highlight": submit_highlight},
                timeout=SPOTTER_TIMEOUT, idle_timeout=SPOTTER_IDLE,
            )
        except TimeoutError:
            outcome = "timed out"
        log.info("spotter %s attempt %d -> %s %s", video["key"], attempt + 1, outcome,
                 {k: v for k, v in result.items() if k != "keyframes"})
        if result:
            break
    return result or None


def _clip_update(job: Job, key: str):
    def update(state_: str, detail: str = "", **extra: Any) -> None:
        job.emit("clip", key=key, state=state_, detail=detail, **extra)

    return update


def _credit(video: dict) -> render.Credit:
    return render.Credit(
        platform=video["platform"], handle=video["handle"], followers=video.get("followers"),
        views=video.get("views"), official=bool(video.get("official")),
    )


async def fetch_source(job: Job, video: dict) -> tuple[Path, media.Probe]:
    """Download the video and publish its length and filmstrip, so the page can show where cuts land."""
    update = _clip_update(job, video["key"])
    update("downloading")
    ref = media.VideoRef(video["platform"], video["video_id"], video["url"])
    path = await media.download(ref, job.dir / "source")
    info = await asyncio.to_thread(media.probe, path)
    strip = job.dir / f"strip_{video['key']}.jpg"
    await asyncio.to_thread(media.filmstrip, path, strip, info.duration)
    update("downloaded", duration=round(info.duration, 1), filmstrip=f"/media/{job.id}/{strip.name}")
    return path, info


async def plan_clip(job: Job, video: dict, agent_id: str, limit: asyncio.Semaphore) -> dict | None:
    """Download one video and have the spotter choose its segment and product boxes."""
    update = _clip_update(job, video["key"])
    async with limit:
        try:
            path, info = await fetch_source(job, video)
            update("spotting")
            found = await spot_product(job, video, path, info, agent_id)
            if found and found.get("visible") is False:
                update("skipped", "The exact product was not found in this video")
                return None
            if found:
                start = max(0.0, min(found["start"], info.duration - MIN_CLIP))
                end = min(info.duration, max(start + MIN_CLIP, min(found["end"], start + MAX_CLIP)))
                keyframes, caption = found["keyframes"], found["caption"]
            else:
                # The spotter gave no answer: use an early stretch without a highlight.
                start = min(1.0, info.duration * 0.1)
                end = min(info.duration, start + 4.0)
                keyframes, caption = [], ""
            update("planned", caption, segment={"start": round(start, 1), "end": round(end, 1)})
            return {
                "key": video["key"], "video": video, "path": path, "info": info, "caption": caption,
                "start": start, "end": end, "keyframes": keyframes,
            }
        except Exception as e:
            log.exception("clip %s failed", video["key"])
            update("error", f"{type(e).__name__}: {e}"[:200])
            return None


def _credits(job: Job, cuts: list[dict]) -> list[dict]:
    """What the seller needs for permission requests: who, which video, which seconds."""
    rows = []
    for cut in cuts:
        v = job.videos[cut["key"]]
        rows.append({
            "key": v["key"], "handle": v["handle"], "creator": v["creator"], "platform": v["platform"],
            "url": v["url"], "creator_url": v["creator_url"], "followers": v.get("followers"),
            "views": v.get("views"), "official": v.get("official", False),
            "start": round(cut["start"], 1), "end": round(cut["end"], 1), "caption": cut.get("caption", ""),
        })
    return rows


def _publish(job: Job, final: Path, cuts: list[dict], style: str) -> None:
    credits = _credits(job, cuts)
    (job.dir / "credits.json").write_text(json.dumps(credits, indent=2, ensure_ascii=False))
    job.emit("final", url=f"/media/{job.id}/{final.name}", credits=credits, style=style)


async def run_preset(job: Job, keys: list[str], style: render.Style) -> None:
    agents = await state.agents
    limit = asyncio.Semaphore(4)
    videos = [job.videos[k] for k in keys]
    plans = [p for p in await asyncio.gather(*(plan_clip(job, v, agents["spotter"], limit) for v in videos)) if p]
    if not plans:
        raise RuntimeError("none of the selected videos produced a clip")

    product_name = (job.product or {}).get("name") or job.query

    async def draw(index: int, plan: dict) -> Path:
        update = _clip_update(job, plan["key"])
        update("rendering")
        out = job.dir / f"clip_{plan['key']}.mp4"
        info = plan["info"]
        await asyncio.to_thread(
            render.render_clip, plan["path"], out,
            src_w=info.width, src_h=info.height, has_audio=info.has_audio,
            start=plan["start"], end=plan["end"], keyframes=plan["keyframes"],
            product=product_name, credit=_credit(plan["video"]), caption=plan["caption"],
            style=style, index=index, total=len(plans),
        )
        update("done", plan["caption"])
        return out

    clips = await asyncio.gather(*(draw(i, plan) for i, plan in enumerate(plans)))
    job.emit("status", stage="render", text="Joining clips")
    job.renders += 1
    end_card = job.dir / "end_card.mp4"
    final = job.dir / f"ad_{job.renders}.mp4"
    credits = [_credit(plan["video"]) for plan in plans]
    await asyncio.to_thread(render.render_end_card, end_card, product_name, credits, style)
    await asyncio.to_thread(render.concat, [*clips, end_card], final)
    _publish(job, final, plans, style.key)


async def run_hype(job: Job, keys: list[str]) -> None:
    """The beat-synced cut: the spotter picks each moment, hype.py does the rest."""
    agents = await state.agents
    limit = asyncio.Semaphore(4)
    videos = [job.videos[k] for k in keys]
    plans = [p for p in await asyncio.gather(*(plan_clip(job, v, agents["spotter"], limit) for v in videos)) if p]
    if not plans:
        raise RuntimeError("none of the selected videos produced a clip")

    clips, cuts = [], []
    for plan in plans:
        # Every clip runs the same number of beats: take that window from the middle of the spotter's pick.
        length = min(hype.CLIP_SECONDS, plan["info"].duration)
        middle = (plan["start"] + plan["end"]) / 2
        start = max(0.0, min(middle - length / 2, plan["info"].duration - length))
        end = start + length
        video = plan["video"]
        clips.append({
            "src": str(plan["path"].resolve()), "start": start, "end": end,
            "boxes": [[k.t, *k.box] for k in plan["keyframes"] if start - 0.3 <= k.t <= end + 0.3],
            "handle": video["handle"], "platform": video["platform"], "followers": video.get("followers"),
            "views": video.get("views"), "official": bool(video.get("official")), "caption": plan["caption"],
        })
        cuts.append({"key": plan["key"], "start": start, "end": end, "caption": plan["caption"]})
        _clip_update(job, plan["key"])("rendering", segment={"start": round(start, 1), "end": round(end, 1)})

    job.emit("status", stage="render", text="Cutting to the beat")
    job.renders += 1
    final = job.dir / f"ad_{job.renders}.mp4"
    product_name = (job.product or {}).get("name") or job.query
    await asyncio.to_thread(hype.render, hype.build_plan(product_name, clips), final)
    for cut in cuts:
        _clip_update(job, cut["key"])("done", cut["caption"])
    _publish(job, final, cuts, hype.STYLE["key"])


async def run_render(job: Job, keys: list[str], style: str) -> None:
    job.busy = True
    try:
        job.emit("status", stage="render", text="Making your ad")
        if style == director.STYLE_KEY:
            agents = await state.agents
            await director.run(
                job, keys, client=state.client, agent_id=agents["director"],
                fetch_source=fetch_source, publish=_publish,
            )
        elif style == hype.STYLE["key"]:
            await run_hype(job, keys)
        elif style == LOCAL_CLAUDE_KEY:
            raise RuntimeError("My Claude is switched on but not built yet; pick another style")
        else:
            await run_preset(job, keys, render.STYLES[style])
    except Exception as e:
        log.exception("render failed")
        job.emit("error", stage="render", message=f"{type(e).__name__}: {e}")
    finally:
        job.busy = False


# --- HTTP ----------------------------------------------------------------------------------


class SearchRequest(BaseModel):
    query: str


class RenderRequest(BaseModel):
    keys: list[str]
    style: str = "spotlight"


def _job(job_id: str) -> Job:
    job = state.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return job


@app.get("/api/health")
async def health() -> dict:
    task = state.agents
    if not task.done():
        return {"ready": False}
    if task.exception():
        return {"ready": False, "error": str(task.exception())}
    return {"ready": True, "agents": task.result()}


@app.post("/api/jobs")
async def create_job(body: SearchRequest) -> dict:
    query = body.query.strip()
    if not query:
        raise HTTPException(400, "describe the product first")
    job_id = uuid.uuid4().hex[:10]
    job = Job(id=job_id, query=query[:2000], dir=JOBS_DIR / job_id)
    job.dir.mkdir(parents=True)
    state.jobs[job_id] = job
    asyncio.create_task(run_scout(job))
    return {"id": job_id}


@app.post("/api/jobs/{job_id}/render")
async def start_render(job_id: str, body: RenderRequest) -> dict:
    job = _job(job_id)
    keys = [k for k in dict.fromkeys(body.keys) if k in job.videos]
    if not keys:
        raise HTTPException(400, "select at least one video")
    if len(keys) > MAX_SELECTED:
        raise HTTPException(400, f"select at most {MAX_SELECTED} videos")
    if body.style not in {s["key"] for s in _styles() if s["enabled"]}:
        raise HTTPException(400, "unknown or unavailable style")
    if job.busy:
        raise HTTPException(409, "this job is still working")
    asyncio.create_task(run_render(job, keys, body.style))
    return {"ok": True}


def _styles() -> list[dict]:
    presets = [
        {"key": st.key, "label": st.label, "description": st.description, "kind": "preset", "enabled": True}
        for st in render.STYLES.values()
    ]
    return [
        {**hype.STYLE, "kind": "preset", "enabled": True, "badge": "New"},
        *presets,
        {
            "key": director.STYLE_KEY, "label": "AI Director", "kind": "agent", "enabled": True, "badge": "Opus 5.5",
            "description": "Claude Opus 5.5 edits the ad itself inside a ZooWork sandbox (takes a few minutes)",
        },
        {
            "key": LOCAL_CLAUDE_KEY, "label": "My Claude", "kind": "local", "enabled": LOCAL_CLAUDE, "badge": "Local",
            "description": (
                "Claude Code on this machine edits the ad with your own Claude plan"
                if LOCAL_CLAUDE else "Start the app on your own machine with LOCAL_CLAUDE=1 to turn this on"
            ),
        },
    ]


@app.get("/api/styles")
async def styles() -> list[dict]:
    return _styles()


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    job = _job(job_id)
    last = request.headers.get("last-event-id")
    sent = int(last) + 1 if last and last.isdigit() else 0

    async def stream():
        nonlocal sent
        queue: asyncio.Queue = asyncio.Queue()
        job.subscribers.append(queue)
        try:
            while True:
                while sent < len(job.events):
                    yield f"id: {sent}\ndata: {json.dumps(job.events[sent], ensure_ascii=False)}\n\n"
                    sent += 1
                try:
                    await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            job.subscribers.remove(queue)

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


JOBS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=JOBS_DIR), name="media")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
