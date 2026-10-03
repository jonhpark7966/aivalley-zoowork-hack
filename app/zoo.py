"""ZooWork Managed Agents: agent definitions, provisioning, and running one turn."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from zoowork import (
    SessionEvent,
    ZooworkClient,
    ZooworkError,
    assistant_text,
    create_zoowork_client,
    custom_tool_use,
    is_run_finished,
    run_outcome,
    tool_call,
)

log = logging.getLogger("ugc.zoo")

STATE_FILE = Path(".local/agents.json")
DEFAULT_MODEL = "litellm/gemini-3.8-flash"
DIRECTOR_MODEL = "litellm/claude-opus-5-5"

# The scout and spotter only search, read, and look; neither needs the sandbox.
NO_SANDBOX = {"deny": ["exec", "process", "write", "edit", "apply_patch"]}

SCOUT_PERSONA = """# UGC Scout

You work for an online seller. You find short videos on YouTube Shorts and TikTok in which
people show the seller's exact product. The seller will contact those creators for permission
and reuse the footage in an ad, so every video you report must really feature the product.

Each message gives you one task. Do only that task, fast. Several scouts work in parallel on
other tasks, so do not cover ground outside yours.

## Task: IDENTIFY

Work out exactly what the product is. If the input contains a URL, read it with `web_fetch`;
if it is vague, run one `web_search`. Then call `set_product` once and stop. Its
`visual_description` goes to a vision model that must spot the product in video frames, so
describe what it looks like: shape, colours, logo, distinguishing parts.

## Task: SEARCH

The message names a platform and an angle. Run two or three searches for that angle:

- YouTube Shorts: `search_youtube_shorts` (plain keywords work best, no `site:` operators).
- TikTok: `web_search` with `site:tiktok.com` in the query.
- When the message names `tavily_search` as the search tool, use only that tool for searching.
  It returns only usable video URLs, so try three or four differently worded queries.

Call `submit_video` for every good result right after each search, one call per video, all
calls for one search in the same step. Do not save them up; the seller watches the list fill
in live.

## Rules

- Submit only URLs that appeared in a tool result. Never guess or build a video ID.
- TikTok URLs look like `https://www.tiktok.com/@user/video/<digits>`. Skip profile, tag,
  discover and search pages.
- Skip competitors, other models from the same brand, and compilations.
- Set `is_official: true` when the video is posted by the brand, the manufacturer or an
  official store account rather than by an independent creator. Official videos are welcome;
  they just get labelled.
- When `submit_video` answers that enough videos were collected, stop immediately.
- Never ask a question. End with one short sentence.
"""

SPOTTER_PERSONA = """# Product Spotter

You look at frames from a short video and locate one product, so an editor can cut the best
moment and draw a highlight around the product.

## Workflow

1. Call `get_frames` with only `video_id` for an overview: evenly spaced frames across the
   whole video. Each frame has its index and timestamp burned into the top-left corner.
2. Pick the best continuous segment, 3 to 5 seconds long, where the product is clearly
   visible, large and in focus, ideally held or used by a person.
3. Call `get_frames` again with `start_s` and `end_s` for dense frames inside that segment.
4. Call `submit_highlight` with the segment, one box per dense frame in which the product is
   visible, and a caption.

## Rules

- `box_2d` is `[ymin, xmin, ymax, xmax]`, each normalised to 0-1000 relative to the full
  image. Draw it tightly around the product itself, not the person or the packaging around it.
- `t` is the timestamp burned into that frame.
- Leave out frames where the product is hidden or out of shot.
- `caption` is an ad hook of at most four words about what this moment shows or proves, the
  way a creator would say it: "Fits every cup holder", "Zero leaks", "Obsessed with this".
  Do not repeat the product name; it is already on screen.
- A box that covers most of the frame is useless. If the product fills the frame, pick a
  different moment.
- If the product never appears in the overview, call `submit_highlight` with
  `product_visible: false` and stop. Do the same when what you see is a lookalike: another
  brand's logo, or a clearly different model.
- Never ask a question. Finish with one short sentence.
"""

DIRECTOR_PERSONA = """# Ad Director

You are a video editor with a Linux sandbox. A seller hands you a few short creator videos
that feature their product, and you cut them into one vertical ad the seller would be proud
to put on the product page: social proof first, the product always clear, nothing cheap-looking.

You do the whole edit yourself in the sandbox with ffmpeg and Python (Pillow and numpy are
installed). Nobody reviews your work before the seller sees it.

## What you are given

The message has a JSON brief (product, and for each video its id, creator handle, follower
and view counts, whether the account is official) plus two URLs:

- an input URL to download from: `<input_url>/<file>` for every file named in the brief,
- an upload URL to deliver the finished ad to.

Each video comes with a contact sheet: one frame per second, timestamps burned in. Read the
sheets first; they are the fastest way to find where the product is shown well.

## How to work

Work in the job directory named in the message. Be quick: aim for about five minutes and as
few steps as you can. Batch shell commands into one `exec` call, and put the whole render in
one Python script rather than many small commands.

1. Download every input in one command (`curl --retry 5 --retry-all-errors -sS -O ...`).
2. Look at the contact sheets. For each video choose one continuous stretch of 3 to 5 seconds
   where the product is large, in focus and in use. Skip a video if the product is not in it
   or it is a different product. Call `report_cut` once per chosen video, straight away.
3. When you need exact positions (to ring, point at or zoom on the product), extract those
   frames and look at them; do not guess coordinates from the contact sheet.
4. Render with one Python script: decode each stretch with ffmpeg to raw frames, draw with
   Pillow, encode with ffmpeg, keep each clip's own audio, then join.
5. Check your work once: pull four or five frames from the result, look at them, and fix
   anything broken (text running off the edge, a highlight in the wrong place, unreadable
   type). One round of fixes, then ship.
6. Upload: `curl --retry 5 --retry-all-errors -sS -T ad.mp4 <upload_url>`. It answers
   `{"ok": true}`. Then stop with one sentence describing the ad.

## What the ad must have

- 720x1280, 30 fps, H.264 video and AAC audio in an MP4, 12 to 25 seconds, under 40 MB.
- For every clip, on screen: the creator's handle and their reach, e.g.
  "@handle · 38.6K followers · 5.7K views". Mark official accounts as official. Use the
  numbers from the brief exactly, shortened like 38.6K or 1.2M; leave out a number that is
  missing rather than inventing one.
- The product made obvious in every clip: a ring, an arrow, a zoom, a spotlight, your call.
- A short hook caption per clip in the creator's spirit, at most four words.
- An ending that names the product and sums up the social proof (creator count, combined
  followers and views).

The look is yours. Follow the style note in the message if there is one. Keep text inside a
40 px margin, keep it large enough to read on a phone, and never cover the product.
Fonts: run `fc-list` once; DejaVu Sans Bold and Liberation Sans Bold are always present.

Never ask a question. If something fails, work around it and still deliver an ad.
"""

DIRECTOR_TOOLS = [
    {
        "name": "report_cut",
        "description": (
            "Tell the seller which part of one video you are using. Call once per video you "
            "use, as soon as you have chosen. Returns {ok: true}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "video_id": {"type": "string"},
                "start_s": {"type": "number"},
                "end_s": {"type": "number"},
                "caption": {"type": "string", "description": "The hook caption you will put on this clip."},
            },
            "required": ["video_id", "start_s", "end_s"],
        },
    },
]

SCOUT_TOOLS = [
    {
        "name": "set_product",
        "description": (
            "Record what the product is. Call exactly once, before searching for videos. "
            "Returns {ok: true}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short product name for on-video labels, at most 40 characters."},
                "brand": {"type": "string"},
                "category": {"type": "string"},
                "visual_description": {
                    "type": "string",
                    "description": "What the product looks like, for a vision model: shape, colours, logo, distinguishing parts.",
                },
                "search_keywords": {"type": "array", "items": {"type": "string"}},
                "product_url": {"type": "string"},
            },
            "required": ["name", "visual_description"],
        },
    },
    {
        "name": "search_youtube_shorts",
        "description": (
            "Search YouTube Shorts. Returns a list of {url, title, views}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 15},
            },
            "required": ["query"],
        },
    },
    {
        "name": "tavily_search",
        "description": (
            "Search one platform with Tavily. Returns a list of {url, title, snippet}, already "
            "filtered to single videos (YouTube results are real Shorts)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Plain keywords; no site: operators."},
                "platform": {"type": "string", "enum": ["youtube", "tiktok"]},
            },
            "required": ["query", "platform"],
        },
    },
    {
        "name": "submit_video",
        "description": (
            "Report one YouTube Shorts or TikTok video that shows the product. The seller sees "
            "it immediately. Returns {accepted, total} and, when rejected, the reason."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "platform": {"type": "string", "enum": ["youtube", "tiktok"]},
                "reason": {
                    "type": "string",
                    "description": "One short sentence: how the product appears in this video.",
                },
                "is_official": {
                    "type": "boolean",
                    "description": "True when posted by the brand, manufacturer or an official store account.",
                },
            },
            "required": ["url", "platform", "reason"],
        },
    },
]

SPOTTER_TOOLS = [
    {
        "name": "get_frames",
        "description": (
            "Return frames from the video as images. With only video_id: an overview across the "
            "whole video. With start_s and end_s: dense frames inside that range."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "video_id": {"type": "string"},
                "start_s": {"type": "number"},
                "end_s": {"type": "number"},
            },
            "required": ["video_id"],
        },
    },
    {
        "name": "submit_highlight",
        "description": "Report the chosen segment and where the product is in it. Call once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "video_id": {"type": "string"},
                "product_visible": {"type": "boolean"},
                "start_s": {"type": "number"},
                "end_s": {"type": "number"},
                "caption": {"type": "string"},
                "boxes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "t": {"type": "number", "description": "Timestamp burned into the frame, in seconds."},
                            "box_2d": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "[ymin, xmin, ymax, xmax], 0-1000.",
                            },
                        },
                        "required": ["t", "box_2d"],
                    },
                },
            },
            "required": ["video_id", "product_visible"],
        },
    },
]


@dataclass
class AgentSpec:
    key: str
    resource: dict[str, Any]

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.resource, sort_keys=True).encode()).hexdigest()[:16]


def _spec(
    key: str, persona: str, tools: list[dict], model: str = DEFAULT_MODEL, tool_policy: dict | None = None
) -> AgentSpec:
    model = os.environ.get(f"ZOOWORK_{key.upper()}_MODEL") or model
    return AgentSpec(
        key=key,
        resource={
            "name": f"ugc-{key}",
            "model": {"primary": model},
            "labels": {"app": "ugc-ad-maker", "role": key},
            "include_global_skills": False,
            "persona": {"docs": [{"name": "AGENTS.md", "content": persona}]},
            "tool_policy": NO_SANDBOX if tool_policy is None else tool_policy,
            "custom_tools": tools,
        },
    )


def specs() -> list[AgentSpec]:
    return [
        _spec("scout", SCOUT_PERSONA, SCOUT_TOOLS),
        _spec("spotter", SPOTTER_PERSONA, SPOTTER_TOOLS),
        # The director edits video in the sandbox, so it keeps the full tool set.
        _spec("director", DIRECTOR_PERSONA, DIRECTOR_TOOLS, model=DIRECTOR_MODEL, tool_policy={}),
    ]


def make_client() -> ZooworkClient:
    # Session streams stay quiet while a model thinks; allow long reads.
    return create_zoowork_client(timeout=httpx.Timeout(connect=20, read=180, write=60, pool=30))


def _load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


async def _retire(client: ZooworkClient, agent_id: str) -> None:
    try:
        await client.stop_agent(agent_id)
        await client.delete_agent(agent_id)
    except ZooworkError:
        pass


async def ensure_agent(client: ZooworkClient, spec: AgentSpec) -> str:
    """Return a running agent that matches spec, reusing the one from the last run if it does."""
    state = _load_state()
    saved = state.get(spec.key)
    agent_id = None
    if saved:
        if saved.get("digest") == spec.digest:
            try:
                await client.get_agent(saved["agent_id"])
                agent_id = saved["agent_id"]
            except ZooworkError as e:
                if e.status != 404:
                    raise
        else:
            await _retire(client, saved["agent_id"])
    if agent_id is None:
        created = await client.create_agent(spec.resource)
        agent_id = created["agent_id"]
        # Agents are provisioned concurrently; re-read so one save does not drop the other.
        state = _load_state()
        state[spec.key] = {"agent_id": agent_id, "digest": spec.digest}
        _save_state(state)
    await client.start_agent(agent_id)
    await client.wait_until_running(agent_id, timeout=60)
    return agent_id


ToolHandler = Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]]]
EventHook = Callable[[SessionEvent], None]


def json_result(value: Any) -> list[dict[str, Any]]:
    return [{"type": "json", "value": value}]


async def run_turn(
    client: ZooworkClient,
    agent_id: str,
    message: str,
    handlers: dict[str, ToolHandler],
    on_event: EventHook | None = None,
    timeout: float = 300,
    idle_timeout: float | None = None,
) -> tuple[str, str]:
    """Open a session, send one message, answer custom tool calls until the turn ends.

    Returns (outcome, assistant text). Raises TimeoutError when the turn runs past `timeout`,
    or when `idle_timeout` is set and the session goes that long without any event.
    """
    session = await client.create_session(agent_id, {})
    session_id = session["session_id"]
    receipt = await client.post_events(agent_id, session_id, [{"type": "user.message", "content": message}])
    if not receipt or receipt[0].get("accepted") is not True:
        raise RuntimeError("ZooWork did not accept the message")

    pending: set[asyncio.Task] = set()

    async def answer(call) -> None:
        handler = handlers.get(call.name or "")
        is_error = False
        try:
            if handler is None:
                raise ValueError(f"unknown tool {call.name}")
            content = await handler(call.input or {})
        except Exception as e:  # the model should see the failure and adapt
            content, is_error = [{"type": "text", "text": f"{type(e).__name__}: {e}"}], True
        # An unanswered call stalls the whole turn, so retry the hand-back.
        for attempt in range(3):
            try:
                await client.resolve_custom_tool_call(
                    agent_id, call.call_id, content=content, is_error=is_error, resolved_by="ugc-ad-maker"
                )
                return
            except (ZooworkError, httpx.HTTPError) as e:
                log.warning("resolving %s failed (attempt %d): %s", call.name, attempt + 1, e)
                await asyncio.sleep(1 + attempt)

    async def read() -> tuple[str, str]:
        cursor, text = None, ""
        for attempt in range(6):
            try:
                events = client.stream_events(agent_id, session_id, cursor=cursor).__aiter__()
                while True:
                    try:
                        event = await asyncio.wait_for(events.__anext__(), idle_timeout)
                    except StopAsyncIteration:
                        break
                    cursor = event.cursor or cursor
                    text += assistant_text(event)
                    call = custom_tool_use(event)
                    if call and call.phase == "requested":
                        task = asyncio.create_task(answer(call))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
                    if on_event:
                        on_event(event)
                    if is_run_finished(event):
                        return run_outcome(event) or "failed", text
            except (httpx.TransportError, httpx.TimeoutException):
                pass  # the log is durable; resume from the last cursor
            await asyncio.sleep(min(2**attempt, 10))
        return "failed", text

    try:
        return await asyncio.wait_for(read(), timeout)
    finally:
        for task in pending:
            task.cancel()


__all__ = [
    "AgentSpec", "ensure_agent", "json_result", "make_client", "run_turn", "specs", "tool_call",
]
