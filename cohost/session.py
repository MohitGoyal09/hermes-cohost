"""One live co-host session: chat in, frames in, answers/flags/highlights out, recap at the end."""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .brain import ReplyLimiter, build_answer_request, clean_reply, is_addressed, strip_address
from .highlights import HypeDetector
from .moderation import Moderator
from .obs import ObsClient
from .recap import build_recap_request, fmt_ts, render_recap
from .screen import grab_screen
from .twitch import ChatMessage, TwitchChat

logger = logging.getLogger(__name__)

OBS_RETRY = 30.0
MAX_PENDING_ANSWERS = 3
FLAG_ALERT_GAP = 15.0  # a link raid becomes one alert plus a count, not hundreds of phone pings


@dataclass(frozen=True)
class Settings:
    channel: str
    bot_nick: str = ""
    bot_token: str | None = field(default=None, repr=False)
    names: tuple[str, ...] = ("hermes",)
    persona: str = ""
    title: str = ""
    obs_host: str = "127.0.0.1"
    obs_port: int = 4455
    obs_password: str | None = field(default=None, repr=False)
    frame_interval: float = 5.0
    screen_fallback: bool = False  # whole-desktop capture can show private windows; opt in only
    blocked_terms: tuple[str, ...] = ()
    answers_per_minute: int = 6
    per_user_cooldown: float = 30.0


def _defang(text: str) -> str:
    """Viewer text shown to the streamer: no live links, no mass mentions, no code fences."""
    return clean_reply(text, max_len=120).replace("@", "@​").replace("`", "'")


class CohostSession:
    def __init__(self, settings: Settings, *, llm: Any, notify: Callable[[str], Any], data_dir: Path,
                 chat_factory: Callable[..., Any] = TwitchChat, obs_factory: Callable[..., Any] = ObsClient,
                 screen: Callable[[], bytes | None] = grab_screen) -> None:
        self.settings = settings
        self._llm, self._notify, self._screen = llm, notify, screen
        self._obs_factory = obs_factory
        self.dir = Path(data_dir) / f"{settings.channel}-{time.strftime('%Y%m%d-%H%M%S')}"
        self.chat = chat_factory(settings.channel, on_message=self.on_message,
                                 nick=settings.bot_nick or None, token=settings.bot_token)
        self._names = [n for n in (*settings.names, settings.bot_nick) if n]
        self._moderator = Moderator(settings.blocked_terms)
        self._hype = HypeDetector()
        self._limiter = ReplyLimiter(settings.per_user_cooldown, settings.answers_per_minute)
        self._answers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cohost-answer")
        self._ctx: contextvars.Context | None = None
        self._pending = 0
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._recent_chat: deque[str] = deque(maxlen=30)
        self._stop = threading.Event()
        self._frames: threading.Thread | None = None
        self._obs: Any = None
        self._live_offset: float | None = None  # session seconds at which the broadcast started
        self._last_flag_alert: dict[str, float] = {}  # per severity: noise never mutes a serious flag
        self._muted_flags: dict[str, int] = {}
        self.latest_frame: bytes | None = None
        self.frame_source = "none"
        self._started_mono = 0.0
        self.running = False

    # lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        (self.dir / "frames").mkdir(parents=True, exist_ok=True)
        self._started_mono = time.monotonic()
        self._ctx = contextvars.copy_context()  # profile/secret scope for ctx.llm on the answer thread
        self.running = True
        self.chat.start()
        self._frames = threading.Thread(target=self._frame_loop, name="cohost-frames", daemon=True)
        self._frames.start()

    def stop(self) -> str:
        self._stop.set()
        self.running = False
        self.chat.stop()
        self._answers.shutdown(wait=False, cancel_futures=True)
        if self._frames is not None:
            self._frames.join(timeout=10)
        duration = max(self.stream_time(), 1.0)
        events = self.events()
        parsed = None
        try:
            parsed = self._llm.complete_structured(**build_recap_request(events, self.settings.title, duration)).parsed
        except Exception as exc:
            logger.warning("cohost: recap model call failed: %s", exc)
        md = render_recap(parsed, events, self.settings.title, duration)
        (self.dir / "recap.md").write_text(md)
        return md

    # clocks --------------------------------------------------------------------
    def _elapsed(self) -> float:
        return time.monotonic() - self._started_mono if self._started_mono else 0.0

    def _vod(self, t: float) -> float:
        """Session seconds -> broadcast (VOD) seconds once OBS has reported the stream going live."""
        return t - self._live_offset if self._live_offset is not None else t

    def stream_time(self) -> float:
        return max(self._vod(self._elapsed()), 0.0)

    # streamer actions ----------------------------------------------------------
    def mark(self, note: str = "") -> dict[str, Any]:
        event = self._record({"type": "mark", "t": self._elapsed(), "note": note.strip() or "marked"})
        return {**event, "t": self._vod(event["t"])}

    def say(self, text: str) -> bool:
        reply = clean_reply(text)
        if not reply or self._moderator.blocked_term(reply):
            return False
        return self.chat.say(reply)

    def events(self) -> list[dict[str, Any]]:
        """Events on the broadcast clock; pre-broadcast chatter is left out once the stream is live."""
        with self._lock:
            raw = list(self._events)
        mapped = [{**e, "t": self._vod(e["t"])} for e in raw]
        return [e for e in mapped if e["t"] >= 0]

    def highlights(self) -> list[dict[str, Any]]:
        return [e for e in self.events() if e["type"] in ("highlight", "mark")]

    def status(self) -> dict[str, Any]:
        events = self.events()
        count = lambda kind: sum(1 for e in events if e["type"] == kind)  # noqa: E731
        return {
            "channel": self.settings.channel, "running": self.running, "chat_connected": self.chat.connected,
            "can_reply": self.chat.can_send, "frame_source": self.frame_source,
            "stream_time": fmt_ts(self.stream_time()), "messages": count("chat") + count("answer"),
            "answers": count("answer"), "highlights": count("highlight") + count("mark"), "flags": count("flag"),
        }

    def agent_digest(self) -> dict[str, Any]:
        """What the streamer's tool-holding agent sees: counts and highlights, never viewer text,
        so public chat cannot steer the agent into cohost_say or other tools."""
        return {**self.status(), "highlights": [
            {"t": fmt_ts(e["t"]), "note": e.get("note") or e.get("reason", ""), "clip": e.get("clip", False)}
            for e in self.highlights()]}

    # chat pipeline -------------------------------------------------------------
    def on_message(self, msg: ChatMessage) -> None:
        if self._stop.is_set() or msg.user == self.settings.bot_nick.lower():
            return
        t = self._elapsed()
        flag = self._moderator.check(msg)
        if flag is not None:  # flagged text never reaches the model's context
            self._record({"type": "flag", "t": t, "user": msg.user, "text": msg.text,
                          "reason": flag.reason, "severity": flag.severity})
            self._flag_alert(msg, flag.reason, flag.severity, t)
            return
        context = list(self._recent_chat)  # snapshot before this line, for the answer prompt
        self._recent_chat.append(f"{msg.display}: {msg.text}")
        hype = self._hype.feed(msg.text, msg.ts, is_mod=msg.is_mod)  # wall clock: never jumps backwards
        if hype is not None:
            self._highlight(hype.reason, t)
        if self._queue_answer(msg, t, context):
            return
        self._record({"type": "chat", "t": t, "user": msg.user, "text": msg.text})

    def _queue_answer(self, msg: ChatMessage, t: float, context: list[str]) -> bool:
        if not (is_addressed(msg.text, self._names) and self.chat.can_send):
            return False
        with self._lock:
            if self._pending >= MAX_PENDING_ANSWERS or not self._limiter.allow(msg.user, msg.ts):
                return False
            self._pending += 1
        try:
            self._answers.submit(self._ctx.run if self._ctx else _call, self._answer, msg, t, context)
            return True
        except RuntimeError:  # executor already shut down by stop()
            with self._lock:
                self._pending -= 1
            return False

    def _answer(self, msg: ChatMessage, t: float, context: list[str]) -> None:
        try:
            question = strip_address(msg.text, self._names)
            req = build_answer_request(question, msg.display, context, self.latest_frame,
                                       self.settings.persona, self.settings.title,
                                       name=self.settings.bot_nick or "Hermes")
            parsed = self._llm.complete_structured(**req).parsed or {}
            reply = clean_reply(str(parsed.get("reply", "")))
            safe = bool(reply) and self._moderator.blocked_term(reply) is None
            if parsed.get("answerable") and safe and self.chat.say(reply, reply_to=msg.id or None):
                self._record({"type": "answer", "t": t, "user": msg.user, "text": msg.text, "reply": reply})
                return
        except Exception as exc:
            logger.warning("cohost: answer failed: %s", exc)
        finally:
            with self._lock:
                self._pending -= 1
        self._record({"type": "chat", "t": t, "user": msg.user, "text": msg.text})

    def _flag_alert(self, msg: ChatMessage, reason: str, severity: str, t: float) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_flag_alert.get(severity, float("-inf")) < FLAG_ALERT_GAP:
                self._muted_flags[severity] = self._muted_flags.get(severity, 0) + 1
                return
            self._last_flag_alert[severity] = now
            muted = self._muted_flags.pop(severity, 0)
        more = f" (+{muted} more flagged)" if muted else ""
        self._safe_notify(f"🚩 Flag ({severity}) {fmt_ts(self._vod(t))} {msg.display}: {_defang(msg.text)} "
                          f"— {reason}{more}")

    def _highlight(self, reason: str, t: float) -> None:
        obs = self._obs  # the frame thread may clear it at any moment
        clip = False
        if obs is not None:
            try:
                clip = obs.save_replay()
            except Exception as exc:
                logger.info("cohost: replay save failed: %s", exc)
        frame_name = ""
        frame = self.latest_frame
        if frame:
            frame_name = f"frames/{int(t)}.jpg"
            (self.dir / frame_name).write_bytes(frame)
        self._record({"type": "highlight", "t": t, "reason": reason, "clip": clip, "frame": frame_name})
        self._safe_notify(f"🔥 Highlight {fmt_ts(self._vod(t))}: {reason}" + (" — replay clip saved" if clip else ""))

    # frames ----------------------------------------------------------------------
    def _frame_loop(self) -> None:
        next_obs_try = 0.0
        try:
            while not self._stop.is_set():
                if self._obs is None and time.monotonic() >= next_obs_try:
                    self._obs = self._connect_obs()
                    next_obs_try = time.monotonic() + OBS_RETRY
                frame = self._poll_obs() if self._obs is not None else None
                if frame is None and self.settings.screen_fallback:
                    frame = self._screen()
                    if frame and self.frame_source != "screen":
                        self._safe_notify("🖥️ OBS not reachable: using full screen capture. Viewers' answers "
                                          "can describe anything on your screen, not only the stream.")
                    self.frame_source = "screen" if frame else "none"
                if frame is None:  # never answer from a frame that is no longer on stream
                    self.frame_source = "none"
                self.latest_frame = frame
                self._stop.wait(self.settings.frame_interval)
        finally:
            obs, self._obs = self._obs, None
            if obs is not None:
                obs.close()

    def _connect_obs(self) -> Any:
        s = self.settings
        try:
            obs = self._obs_factory(host=s.obs_host, port=s.obs_port, password=s.obs_password)
            obs.connect()
            return obs
        except Exception as exc:
            logger.info("cohost: OBS not reachable (%s)", exc)
            return None

    def _poll_obs(self) -> bytes | None:
        obs = self._obs
        try:
            frame = obs.screenshot()
            seconds = obs.stream_seconds()
            if seconds is not None:
                self._live_offset = self._elapsed() - seconds
            self.frame_source = "obs"
            return frame
        except Exception as exc:
            logger.info("cohost: lost OBS connection: %s", exc)
            self._obs = None
            try:
                obs.close()
            except Exception:
                pass
            return None

    # helpers ---------------------------------------------------------------------
    def _record(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._events.append(event)
            with (self.dir / "events.jsonl").open("a") as fh:
                fh.write(json.dumps(event) + "\n")
        return event

    def _safe_notify(self, text: str) -> None:
        try:
            self._notify(text)
        except Exception as exc:
            logger.warning("cohost: notify failed: %s", exc)


def _call(fn: Callable[..., Any], *args: Any) -> Any:
    return fn(*args)
