# cohost: Hermes co-hosts your Twitch stream

Hermes joins your live stream as a co-host. It watches what your viewers see, answers their
questions in chat, flags bad messages to you, marks highlight moments when chat explodes, and
writes a recap with VOD chapters when you end the stream.

```
viewer:  @hermes what language is this?
hermes:  That's Rust: he's writing an async TCP server with tokio, the accept loop is on screen now.
```

You stay in control from any Hermes surface (Telegram, Discord, Desktop, CLI): one live status
card updates in place while you stream.

## What it does

| | |
|---|---|
| **Sees the stream** | Reads the OBS program output through OBS WebSocket (exactly what viewers see), or, if you opt in, a full screen capture. |
| **Answers viewers** | When someone writes `@hermes …`, `hermes, …` or `!ask …`, Hermes answers in a threaded reply using the current frame and recent chat. |
| **Flags, never bans** | Local rules flag blocked terms, links from first-time chatters, caps spam and repeats. You get a 🚩 card with the message; nothing is deleted. |
| **Marks highlights** | A chat spike or a "clip it" burst saves a highlight with a frame and, if the OBS replay buffer is on, a real clip file. You can also `/cohost mark`. |
| **Writes the recap** | `/cohost stop` returns a summary, YouTube-ready chapters, the highlight list and stats, saved as `recap.md`. |

## Safety model

- **Viewers can never reach tools.** Viewer answers go through `ctx.llm` (one host-owned, tool-less model call). No viewer message starts an agent turn.
- **Chat is untrusted data.** Prompts mark it as such; replies are cleaned (no links, no leading `/` `.` `!` commands, blocked terms dropped). Flagged messages never reach the model.
- **Cost is bounded.** Only addressed messages call the model, with a per-viewer cooldown (30 s) and a global cap (6 answers per minute by default).
- **The streamer's agent sees a digest.** The `cohost_status` tool returns counts, highlights and short viewer lines marked as untrusted, never flagged text.

## Install

```bash
hermes plugins install MohitGoyal09/hermes-cohost
hermes plugins enable cohost
```

Requires Hermes `>=0.23.0`. No extra Python packages: Twitch chat uses the standard library, and
`websockets` and `Pillow` already ship with Hermes.

## Set up

1. **Read-only works with no setup.** `/cohost start <channel>` reads chat anonymously; Hermes flags, marks highlights and writes the recap, but does not reply.
2. **To reply in chat**, create a bot account (or use your own) and a chat token with `chat:read chat:edit`, then set it in Desktop → Settings → Plugins → cohost (**Bot chat token**, stored as `COHOST_TWITCH_TOKEN`) and set **Bot username**.
3. **To see the stream**, enable OBS → Tools → WebSocket Server Settings and put the password in **OBS WebSocket password** (`COHOST_OBS_PASSWORD`). Turn on OBS's Replay Buffer to get clip files for highlights.

Other settings: channel, names viewers can use, persona, stream title, blocked terms, answers per minute, frame interval.

## Use

| Command | |
|---|---|
| `/cohost start [channel]` | Start co-hosting; posts the live status card here |
| `/cohost status` | Current counts and stream time |
| `/cohost mark [note]` | Mark this moment as a highlight |
| `/cohost say <text>` | Post to chat as the bot |
| `/cohost stop` | End the session and get the recap |

Your Hermes agent also gets three tools: `cohost_status`, `cohost_mark`, `cohost_say`. So you can
ask "how's chat doing?" or "mark that" in plain words.

## Disclosure

- **Network:** connects to `irc.chat.twitch.tv:6697` (TLS) and to OBS WebSocket on the host you configure (default `127.0.0.1:4455`).
- **Model calls:** viewer questions (text plus one stream frame) and the recap go to your active Hermes model through `ctx.llm`.
- **Screen capture (off by default):** if you turn on `screen_fallback`, it captures your whole screen every few seconds when OBS is not reachable (macOS asks for Screen Recording permission), and you get a warning card when it starts.
- **Background threads:** while a session runs, threads in the gateway process read chat, poll frames and update the status card. `/cohost stop` ends them.
- **Stored data:** chat events (`events.jsonl`), highlight frames and `recap.md` under `<HERMES_HOME>/plugin-data/cohost/sessions/`. Credentials are read only through Hermes's secret scope.
- **No telemetry.**

## Develop

```bash
uv run --with pytest --with websockets==15.0.1 --with Pillow python -m pytest -q tests
hermes plugins validate .
```

## License

MIT
