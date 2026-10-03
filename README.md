# aivalley-zoowork-hack

Project repo for **The AI Commerce Gallery – Hackathon**, hosted by [ZooWork](https://zoowork.ai) and [AI Valley](https://aivalley.io).

## As Seen On

A seller types in a product. A ZooWork agent finds YouTube Shorts and TikToks in which creators
show that product, the seller ticks the ones they like, and the app cuts those moments into one
vertical ad with the product highlighted and each creator credited. The seller gets the ad plus
a list of creators and the exact seconds used, ready for permission requests.

### How it works

```
browser ──► local server (FastAPI) ──► ZooWork Managed Agents
                 │                         Scout    x6 sessions: web_search, web_fetch, Tavily + custom tools
                 │                         Spotter  looks at frames, returns product boxes
                 │                         Director Claude Opus 5.5 editing in the sandbox
                 └─► yt-dlp, ffmpeg, Pillow (download, cut, preset overlays, join)
```

1. **Scout agent.** One session identifies the product (reading the product page when given a
   link) while four more search YouTube Shorts and TikTok in parallel, each from its own
   angle. With `TAVILY_API_KEY` set, two further sessions search through Tavily, and the
   videos they find are marked "via Tavily". Every find comes back through the `submit_video` custom tool, so cards appear one by
   one, up to 15. Each card then fills in the creator's follower count and the video's views,
   and videos from the brand's own account are marked Official.
2. The seller plays the embedded videos, selects up to six and picks a style.
3. The selected videos are downloaded locally. What happens next depends on the style:
   - **Hype.** The Spotter picks each moment as below, then `app/hype.py` cuts everything to a
     120 BPM grid: a three-word hook, camera punch-ins and whip transitions, a glowing ring
     that tracks the product, captions that pop on the beat, follower and view counters, and
     a synthesized beat with impacts, whooshes and a riser into the end card.
   - **Presets (Spotlight, Clean, Bold).** The **Spotter agent** asks for frames through the
     `get_frames` custom tool, which returns them as images, picks the best 3 to 5 seconds and
     returns a bounding box for the product in each frame. The clip is then drawn locally with
     that preset's overlay: creator handle and reach, product highlight, caption.
   - **AI Director.** The **Director agent** (Claude Opus 5.5) gets the videos, contact sheets
     and a brief with every creator's numbers, and edits the ad itself in its ZooWork sandbox
     with ffmpeg and Python, then uploads the result. Its commands stream to the page live.
4. While this runs, the page shows each creator's whole video as a filmstrip with the chosen
   segment as a bright window, and the final cut as blocks in order.

ZooWork features in use: Managed Agent API (Python SDK), three agents with persona documents,
parallel sessions on one agent, built-in `web_search` and `web_fetch`, application-executed
custom tools, image blocks in custom tool results, the managed sandbox (`exec`, file and image
tools), session event streaming for live progress, and `tool_policy` to keep the two agents
that do not need it out of the sandbox.

#### Getting files in and out of the sandbox

YouTube blocks downloads from the sandbox's IP range, and the workspace file endpoints
returned 502 on this deployment, so the Director's inputs and output travel over HTTP instead.
When the AI Director style is first used, the server starts a second listener on port 4601
that only serves handoff files, and opens a Cloudflare quick tunnel to it (`cloudflared` must
be installed). Each job is reachable only under its own random token, and only while it runs.

### Run it

Requires [uv](https://docs.astral.sh/uv/), `ffmpeg`, a funded ZooWork Project key, and
`cloudflared` for the AI Director style.

```bash
cp .env.example .env        # then set ZOOWORK_API_KEY
uv sync
uv run uvicorn app.server:app --port 4600
```

Open http://localhost:4600. The first start creates the agents and records their IDs in
`.local/agents.json`; later starts reuse them, and recreate one when its definition in
`app/zoo.py` changes. Downloads and rendered videos go to `data/jobs/`.

Set `ZOOWORK_SCOUT_MODEL`, `ZOOWORK_SPOTTER_MODEL` or `ZOOWORK_DIRECTOR_MODEL` to change a
model. The defaults are `litellm/gemini-3.8-flash` for the Scout and Spotter and
`litellm/claude-opus-5-5` for the Director.

### Hosted page, local engine

The page in `app/static/` is plain static files and can be hosted anywhere (it is deployed on
Vercel). Video download and editing cannot run there: YouTube blocks datacenter IPs, jobs run
for minutes, and they need ffmpeg. So a hosted page looks for the engine on
`http://localhost:4600` and drives it from the browser. To allow that, list the page's origin
in `.env`:

```bash
HOSTED_ORIGINS=https://your-deployment.vercel.app
```

Chrome asks once for permission to reach apps on this device. Safari does not allow an HTTPS
page to call `http://localhost`. Without an engine the page shows how to start one.

`LOCAL_CLAUDE=1` is reserved for letting the Claude Code CLI on the same machine edit the ad
("My Claude" in the style picker). The switch and the button exist; the edit itself is not
built yet.

### Layout

| Path | |
|---|---|
| `app/zoo.py` | Agent definitions (personas, custom tools), provisioning, running one turn |
| `app/server.py` | HTTP API, job events (SSE), the scout and preset pipelines |
| `app/director.py` | The AI Director pipeline and the sandbox file handoff |
| `app/media.py` | URL parsing, oEmbed lookup, reach stats, Shorts search, download, frames |
| `app/render.py` | Preset styles: clip cutting, overlays, end card, concat |
| `app/hype.py` | Hype style: beat grid, camera, tracked glow, kinetic type, synthesized sound |
| `showcase/` | A reference Hype ad and the plan that produced it |
| `app/static/` | The page |

## Event

| | |
|---|---|
| **Event** | [The AI Commerce Gallery – Hackathon](https://www.aivalley.io/events/6bbloggr) ([Luma](https://luma.com/6bbloggr)) |
| **Date** | Saturday, October 3, 2026, 9:00 AM – 9:00 PM PDT |
| **Venue** | The Walt Disney Family Museum, San Francisco, CA (private, independent event; the museum is not affiliated) |
| **Hosts** | ZooWork, AI Valley |
| **Size** | 100–150 curated builders, founders, investors and industry leaders; 25–35 teams |

### Theme

> What does the next generation of AI-powered commerce look like?

Build areas:

- E-commerce and storefront experiences
- Merchant operations, inventory, fulfillment and payments
- Restaurants, retail, POS and physical commerce
- AI agents, MCP integrations and workflow automation

### Awards

- Best Use of ZooWork
- Best Use of Band AI
- Best Use of Entire.io
- People's Choice

The AI Valley page lists all four; the Luma page lists only Best Use of ZooWork and People's Choice. Confirm on site.

### Schedule (PDT)

| Time | |
|---|---|
| 9:00 AM | Registration and breakfast |
| 10:00 AM | Opening and sponsor talks |
| 11:00 AM | Workshops and hacking |
| 12:30 PM | Lunch, hacking continues |
| 4:00 PM | Progress review |
| **5:00 PM** | **Project submission deadline**, dinner and presentation prep |
| 6:00 – 7:30 PM | Project presentations and judging (10 presenters) |
| 7:30 PM | Awards and reception, private gallery access |
| 9:00 PM | Close |

### Sponsors and partners

| Sponsor | What it is |
|---|---|
| [ZooWork](https://zoowork.ai) | AI agent delivery platform: Agent Builder, managed agent runtime, model gateway, data and integrations |
| [BAND](https://band.ai) | Shared environment where agents built with different tools communicate, delegate and keep context in sync |
| [Entire](https://entire.io) | Agentic Git forge: stores prompts, outputs and tool calls alongside code in Git |
| [Moss](https://moss.dev) | Real-time semantic search for agents (sub-10ms retrieval) |
| [Tavily](https://tavily.com) | Web search, extraction and crawling APIs for agents |
| [Novita AI](https://novita.ai) | Model API and GPU compute |

Community: [Discord](https://discord.gg/QESCnSUPC9) · [Events](https://lu.ma/aivalley) · community@aivalley.io

## ZooWork in brief

ZooWork (by SerendipityOne, the team behind ZooClaw) is a platform for building, deploying and operating AI agents that do business work. There are two ways in:

- **Agent Builder** ([zoowork.ai](https://zoowork.ai)): create a role-based agent by chatting — role, company knowledge, SOPs, delivery standards, tool permissions — then publish it. Agents can be reached from Slack, Teams, WhatsApp or an embedded sidebar.
- **Managed Agent API** ([platform.zoowork.ai](https://platform.zoowork.ai), [docs](https://zoowork.ai/docs/)): a hosted agent runtime you call from your own backend. Developer Preview; the API may change.

### Managed Agent API concepts

| Resource | Meaning |
|---|---|
| Agent | Configuration you create and start: model, tools, MCP servers, skills, tool policy |
| Session | A durable conversation with an agent; survives restarts |
| Events | Messages, tool activity and turn results, streamed from a session |

What the runtime provides:

- **Built-in tools** in an isolated sandbox: `read`, `write`, `edit`, `apply_patch`, `exec`, `process`, `web_fetch`, `web_search`, `web_image_search`
- **Custom tools** executed by your application (for private or authenticated systems)
- **Remote MCP servers**: up to 16 per agent, `streamable-http` or SSE; the endpoint must be public and unauthenticated
- **Permission policies**: require human approval before selected tool calls
- **Skills**: packaged, versioned task instructions attached to an agent
- **Files and artifacts**, **Agent Database** (`agent_db`), **memory** across sessions
- **Schedules** and **webhooks**
- **Model catalog** across providers (`client.listModels()`)

### Quickstart

Requires Node.js 22.20+ (or Python 3.10+), a Project API key (`zwp_live_...`) from Platform Console → Project → API keys, and a funded organization balance.

```bash
npm install @zoowork-ai/sdk        # or: python -m pip install zoowork
export ZOOWORK_API_KEY='zwp_live_...'
```

```ts
import { createZooworkClient } from '@zoowork-ai/sdk'

const client = createZooworkClient()
const agent = await client.createAgent({ resource: { name: 'my-agent' } })
await client.startAgent(agent.agent_id)
const session = await client.createSession(agent.agent_id, {})
```

HTTP base URL: `https://clawapi.ecap.gsmo.ai/service/v1`, with `Authorization: Bearer $ZOOWORK_API_KEY`.

Keep the key server-side; never commit it or ship it to the browser.

### References

- [Docs](https://zoowork.ai/docs/) ([index for LLMs](https://zoowork.ai/docs/llms.txt))
- [Quickstart templates](https://github.com/SerendipityOneInc/zoowork-platform-quickstarts): customer support (custom tools), product advisor (MCP), knowledge assistant (RAG), research assistant (Vercel Chat SDK)
- [TypeScript SDK source](https://github.com/SerendipityOneInc/zoowork-sdk-typescript)
- [Pricing](https://zoowork.ai/pricing)

