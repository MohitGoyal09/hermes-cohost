# Stream Co-host — build plan (hackathon, due 2026-11-11 17:00 EST)

## Intent
Hermes co-hosts a live Twitch stream. It reads chat, sees what viewers see (OBS program
output), answers viewers who address it, flags bad messages to the streamer, marks
highlights when chat spikes, and writes a timestamped recap with VOD chapters after the
stream. Viewers get read-only answers; only the streamer can run tools.

## Non-goals (v1)
- YouTube Live (v2 connector if time remains).
- Acting on moderation (ban/timeout). Twitch removed IRC mod commands in 2023; v1 only
  flags to the streamer.
- Viewers triggering any tool, file access or agent turn.

## Architecture (sidecar, approach A)
`/cohost start <channel>` (any Hermes surface) starts a worker thread inside the gateway
(context copied with `contextvars.copy_context()` so `ctx.post_message` reaches the
streamer's chat). Viewer answers use `ctx.llm.complete_structured` with an image block:
host-owned, tool-less, so viewers can never reach tools.

```
TwitchChat (IRC/TLS, stdlib) ──msg──► Session ──► Moderator ──flag──► post_message (streamer)
                                         ├──► HypeDetector ──hype──► highlight (+OBS replay clip)
ObsClient / grab_screen ──frame──► Brain (is_addressed → ctx.llm → clean_reply) ──► TwitchChat.say
                                         └──► events.jsonl ──/cohost stop──► Recap (ctx.llm) ──► recap.md
```

## Files
| File | Owner | Purpose |
|---|---|---|
| `cohost/twitch.py` | agent A | IRC parser + client (anonymous read, token send, rate limit, reconnect) |
| `cohost/obs.py` | agent A | OBS WebSocket v5 client: auth, program screenshot, timecode, replay save |
| `cohost/screen.py` | agent A | fallback screen grab via Pillow `ImageGrab` |
| `cohost/highlights.py` | agent B | chat-rate spike + keyword hype detector |
| `cohost/moderation.py` | agent B | local rules: blocked terms, caps, repeats, links |
| `cohost/brain.py` | agent B | addressed-detection, answer request builder, reply sanitiser, limiter |
| `cohost/recap.py` | agent B | recap request builder + markdown renderer |
| `cohost/session.py`, `__init__.py`, `plugin.yaml`, `README.md` | orchestrator | wiring, commands, tools, state |

## Dependencies
None new. `websockets` and `Pillow` are Hermes core dependencies; Twitch IRC is stdlib.

## Tests
`uv run --with pytest --with websockets==15.0.1 --with Pillow python -m pytest -q`
