# VoiceDigest

**A Slack agent that lets totally-blind users run Slack by ear.**
Slack Agent Builder Challenge — Track: *Slack Agent for Good*. Team: gashin, owais, dave.

Screen readers read a Slack channel top-to-bottom, symbol by symbol. VoiceDigest instead
*listens to the channel for you* and speaks back the point: who said what, what needs
**your** reply, and what the images and links actually contain.

## What it does

- **Mention `@vd` in a channel / thread, or DM it** → it gathers the relevant context,
  resolves user IDs to names, and returns an **audio-first digest**: anything addressed
  to *you* (requests, questions, deadlines) comes first, then the rest as short bullets.
- **It actually speaks.** Every digest is also posted as a **playable audio clip** (OpenAI
  TTS), so a blind user can literally run Slack by ear — not only via a screen reader.
- **Agentic, not scripted.** The model runs a **tool-calling loop** and decides *for itself*
  when to describe an image or read a link, then grounds its answer in what it found.
- **Images & links become words** via the MCP tools `describe_image` / `read_link`.
- **Structured Block Kit** built for screen readers: a header + digest + a **"To-do list"**
  that renders as a **Data Table** (graceful fallback to checkboxes), plus 👍 / 👎 feedback.

## Required technology — MCP server integration (+ Real-Time Search)

VoiceDigest ships its own MCP server, **`vd-accessibility`** (`src/mcp_server.py`), speaking
stdio JSON-RPC 2.0 with the standard library only. It exposes two tools that a blind
Slack user needs most:

| tool | what it does |
|---|---|
| `describe_image(image_url, context?)` | OpenAI vision (`gpt-4o`) → blind-friendly description. Slack `url_private` files are fetched with the bot's Bearer token. |
| `read_link(url)` | Fetches a web page, strips HTML, summarizes it for audio. |

The bot is the **MCP client** (`src/mcp_client.py`): on startup it spawns the server as
a subprocess, exposes its `tools/list` to the model as function declarations, and runs an
**agentic tool-loop** so the model calls tools on its own. It is a real, reusable MCP
server — any MCP client (e.g. Claude Desktop) can mount it.

**Real-Time Search (optional grounding):** when available, VoiceDigest pulls fresh cross-channel
context via Slack's `assistant.search.context` and falls back transparently to
`conversations.history` when RTS isn't authorized (so it always works in any workspace).

## Architecture

```
Slack (Socket Mode)
      │  app_mention / message.channels / message.groups / message.im
      ▼
  bot.py (Slack Bolt, Python)
      │  Real-Time Search (assistant.search.context)  ─┐  fresh cross-channel context
      │  └─ fallback: conversations.history/replies    │  (transparent if RTS unavailable)
      ▼                                                 ▼
  OpenAI agentic tool-loop  ◀──function calls──▶  MCP client --stdio JSON-RPC-->
      │                                            mcp_server.py (vd-accessibility)
      │                                              ├─ describe_image -> OpenAI vision
      │                                              └─ read_link      -> fetch + OpenAI
      ▼
  ├─ Block Kit reply (header + digest + Data Table to-do [fallback: checkboxes] + 👍/👎)
  └─ 🎧 audio clip (OpenAI TTS -> mp3 -> files_upload_v2)   ← "run Slack by ear"
```

- **LLM**: OpenAI `gpt-4o` (reasoning + function-calling + image vision) via the Chat
  Completions API; **`tts-1`** for voice. One `OPENAI_API_KEY` covers reasoning, vision, and speech.
- No public URL needed (Socket Mode). No web framework, no DB — standard library + `slack_bolt`.

## Demo

- `demo/demo.html` — self-contained animated demo (real in-browser TTS); the basis for the video.
- `demo/summary-ja.html` / `demo/summary-en.html` — rendered examples of a `@vd` digest (JA / EN).
- `docs/accessibility-notes.html` — the blind-user UX fixes (ambiguity / overload / notifications).
- `demo/seed_channel.py` — seeds a demo channel with a realistic conversation to reproduce the demo.

## Setup

```bash
cd src
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in the tokens
set -a; source .env; set +a
./.venv/bin/python bot.py
```

Required Slack bot scopes: `app_mentions:read`, `chat:write`, `im:history`, `im:write`,
`channels:history`, `groups:history`, `users:read`, `files:read`,
`files:write` (post the audio clip). For Real-Time Search add `search:read.public`
(+ `search:read.private`/`.im`/`.mpim` as needed) — optional; the bot works without it.
Enable Socket Mode (App-Level Token) and subscribe to
`app_mention` + `message.channels` + `message.groups` + `message.im`.

## Files

```
src/    bot.py · mcp_server.py · mcp_client.py · requirements.txt · .env.example
docs/   ARCHITECTURE.md · SUBMISSION.md · accessibility-notes.html
demo/   demo.html · summary-ja.html · summary-en.html · seed_channel.py · mockup.png
```

- `src/bot.py` — Slack Bolt bot: history -> digest -> Block Kit, buttons, image/link weaving.
- `src/mcp_server.py` — the `vd-accessibility` MCP server (`describe_image`, `read_link`).
- `src/mcp_client.py` — minimal stdio MCP client used by the bot.
