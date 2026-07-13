# Devpost submission — VoiceDigest

Drafted for the **Slack Agent Builder Challenge → "Slack Agent for Good"** track.
Paste-ready copy is below; the **team checklist** at the end covers the parts only the
team can do (sandbox, access grants, App ID, video upload).

---

## Project name
**VoiceDigest — run Slack by ear**

## Tagline (≤ 200 chars)
A Slack agent that lets blind and low-vision teammates *hear* what a channel is really
saying — who needs them, what images and links contain — and act on it, entirely by ear.

## Elevator / "What it does"
Screen readers read Slack top-to-bottom, symbol by symbol — exhausting, and images and
links are opaque. **VoiceDigest listens to the channel for you.** Mention it (typed or by voice)
and it returns a short, audio-first digest: *what needs you* first, then the gist — and it
**speaks the answer aloud** as a playable clip. It reads images and links itself, and turns
your action items into a structured to-do table.

## How it works (features & functionality)
- **Agentic tool-loop.** VoiceDigest runs an OpenAI function-calling loop and decides on its own
  when to call its MCP tools — `describe_image` (OpenAI vision) and `read_link` — then
  grounds the digest in what it found. Not a scripted responder.
- **Its own MCP server.** `momo-accessibility` is a real, reusable MCP server (stdio
  JSON-RPC, stdlib only) — mountable by any MCP client (e.g. Claude Desktop), not just VoiceDigest.
- **Real-Time Search grounding.** Pulls fresh cross-channel context via
  `assistant.search.context`; falls back to channel history so it works in any workspace.
- **Structured Block Kit.** To-do items render as a **Data Table** (graceful fallback to
  checkboxes), with a "who needs you" lead line surfaced first.
- **Audio-first output.** The digest is posted as a **playable audio clip** (OpenAI TTS),
  so the workflow is usable by ear — not only through a screen reader.
- **Zero-infra.** Socket Mode (no public URL), no database, one LLM key.

## Impact — why this is an *Agent for Good*
- **285M+ people** worldwide are blind or have moderate-to-severe vision impairment (WHO).
  Chat tools default to a visual, scan-the-scrollback model that excludes them at work.
- Screen readers linearize a channel but can't tell a user *what matters to them*, and they
  read images/links as "image" / a raw URL — the two things VoiceDigest turns into words.
- VoiceDigest shifts Slack from "read every line" to **"hear the point + what needs you,"** lowering
  the cognitive and time cost of participating in a fast team channel. The same audio-first
  digest also helps low-vision, dyslexic, and screen-away (e.g. commuting) users.
- It's **inclusive by construction**, not a bolt-on: accessibility *is* the product.

## Built with
`slack-bolt` (Python) · Socket Mode · Model Context Protocol (custom `momo-accessibility`
server) · Slack Real-Time Search (`assistant.search.context`) · Block Kit (Data Table) ·
OpenAI (`gpt-4o-mini` for reasoning + image vision, `tts-1` for voice).

## Try it / links
- **Code:** this repo (`Gashin0601/0710-slack-hackathon`).
- **Animated demo (self-contained, speaks aloud):** `demo/demo.html`.
- **Architecture diagram:** `docs/ARCHITECTURE.md`.

---

## 🎬 3-minute demo video storyboard
Record the browser demo (`demo/demo.html`) for the scripted beats, then cut to the
**real bot in the sandbox** for proof. Keep it under 3:00. Turn sound ON (the point is audio).

| Time | On screen | Say / show |
|---|---|---|
| 0:00–0:20 | Title + one blind user at a laptop | "Slack is a wall of scrollback. For a blind teammate, it's read top-to-bottom, symbol by symbol — and images and links are just noise." |
| 0:20–0:40 | `#design-review` filling with messages, an image, a link | "Here's a busy channel. Something in here needs *you* — but which line?" |
| 0:40–1:00 | User @-mentions VoiceDigest **by voice** (mic pulses) | "Ask VoiceDigest — by voice." |
| 1:00–1:35 | The **agentic tool-loop trace**: RTS → `describe_image` → `read_link` | "VoiceDigest is agentic: it decides on its own to search the workspace, read the image, and read the link — via its own MCP server." |
| 1:35–2:15 | VoiceDigest's Block Kit card: lead line + **Data Table** to-do, then **it speaks** 🔊 | "It replies with what needs you first, a structured to-do table — and it *speaks the answer*. Slack, entirely by ear." |
| 2:15–2:45 | Cut to **real Slack sandbox**: same @mention → real card + real audio clip | "And here it is live in Slack." (proof it's real, not just a mockup) |
| 2:45–3:00 | Architecture diagram + "MCP · Real-Time Search · Block Kit" | "Built on Slack's MCP, Real-Time Search, and Block Kit. VoiceDigest — run Slack by ear." |

---

## ✅ Team checklist — only you can do these (before the deadline)
- [ ] **Decide draft:** repoint `Accessible Slack` (#1075144) to VoiceDigest, or new submission.
- [ ] **Select track:** *Slack Agent for Good*.
- [ ] **Slack sandbox / Developer Program:** confirm the AI-enabled sandbox exists (needed for
      RTS semantic search); register VoiceDigest as an **internal app** (unlisted apps can't use MCP/RTS).
- [ ] **Install + scopes:** add the scopes in the repo README (incl. `files:write` for the
      audio clip; `search:read.*` for RTS). Enable Socket Mode + subscribe `app_mention`, `message.im`.
- [ ] **Grant sandbox test access** to `slackhack@salesforce.com` **and** `testing@devpost.com`.
- [ ] **Slack App ID:** capture it for the submission form.
- [ ] **Record the ~3-min video** (storyboard above), upload to YouTube/Vimeo, paste the link.
- [ ] **Paste** the project text (above), the **architecture diagram**, and the repo URL.
- [ ] **Submit** before **Mon Jul 13, 2026, 5:00 PM PT.**

## Notes / owed follow-ups
- **Verify at build-time in the sandbox:** the exact Slack `table` block schema (kept behind
  `MOMO_ENABLE_TABLE` with auto-fallback), the `assistant.search.context` response fields, and
  where the `action_token` rides on the event payload.
- **Voice model:** `OPENAI_TTS_MODEL` defaults to `tts-1` (voice `alloy`); a paid OpenAI key
  covers reasoning, vision, and voice with no rate-limit throttling.
