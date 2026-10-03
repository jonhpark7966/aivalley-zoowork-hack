# aivalley-zoowork-hack

Project repo for **The AI Commerce Gallery – Hackathon**, hosted by [ZooWork](https://zoowork.ai) and [AI Valley](https://aivalley.io).

> Project idea: **TBD** — see [Idea notes](#idea-notes).

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

## Idea notes

TBD.
