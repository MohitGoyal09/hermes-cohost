"""One live co-host session: chat in, frames in, answers/flags/highlights out, recap at the end."""

from __future__ import annotations

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


@dataclass(frozen=True)
class Settings:
    channel: str
    bot_nick: str = ""
    bot_token: str | None = None
    names: tuple[str, ...] = ("hermes",)
    persona: str = ""
    title: str = ""
    obs_host: str = "127.0.0.1"
    obs_port: int = 4455
    obs_password: str | None = None
    frame_interval: float = 5.0
    screen_fallback: bool = True
    blocked_terms: tuple[str, ...] = ()
    answers_per_minute: int = 6
    per_user_cooldown: float = 30.0
    answer_flags: tuple[str, ...] = field(default=("medium", "high"))


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
        self._pending = 0
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._recent_chat: deque[str] = deque(maxlen=30)
        self._stop = threading.Event()
        self._obs: Any = None
        self._obs_base: tuple[float, float] | None = None  # (stream seconds, wall time) at last poll
        self.latest_frame: bytes | None = None
        self.frame_source = "none"
        self.started_at = 0.0
        self.running = False

    # lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        (self.dir / "frames").mkdir(parents=True, exist_ok=True)
        self.started_at = time.time()
        self.running = True
        self.chat.start()
        threading.Thread(target=self._frame_loop, name="cohost-frames", daemon=True).start()

    def stop(self) -> str:
        self._stop.set()
        self.running = False
        self.chat.stop()
        self._answers.shutdown(wait=True, cancel_futures=True)
        if self._obs is not None:
            self._obs.close()
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

    # streamer actions ----------------------------------------------------------
    def mark(self, note: str = "") -> dict[str, Any]:
        return self._record({"type": "mark", "t": self.stream_time(), "note": note.strip() or "marked"})

    def say(self, text: str) -> bool:
        return self.chat.say(clean_reply(text))

    def events(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)

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

    def stream_time(self) -> float:
        if self._obs_base is not None:
            base, at = self._obs_base
            return base + (time.time() - at)
        return time.time() - self.started_at if self.started_at else 0.0

    # chat pipeline -------------------------------------------------------------
    def on_message(self, msg: ChatMessage) -> None:
        if msg.user == self.settings.bot_nick.lower():
            return
        t = self.stream_time()
        self._recent_chat.append(f"{msg.display}: {msg.text}")
        flag = self._moderator.check(msg)
        if flag is not None:
            self._record({"type": "flag", "t": t, "user": msg.user, "text": msg.text,
                          "reason": flag.reason, "severity": flag.severity})
            self._safe_notify(f"🚩 Flag ({flag.severity}) {fmt_ts(t)} {msg.display}: {msg.text[:120]} — {flag.reason}")
            return
        hype = self._hype.feed(msg.text, t, is_mod=msg.is_mod)
        if hype is not None:
            self._highlight(hype.reason, t)
        if is_addressed(msg.text, self._names) and self.chat.can_send and self._limiter.allow(msg.user, msg.ts):
            with self._lock:
                if self._pending >= MAX_PENDING_ANSWERS:
                    return
                self._pending += 1
            self._answers.submit(self._answer, msg, t)
        else:
            self._record({"type": "chat", "t": t, "user": msg.user, "text": msg.text})

    def _answer(self, msg: ChatMessage, t: float) -> None:
        try:
            question = strip_address(msg.text, self._names)
            req = build_answer_request(question, msg.display, list(self._recent_chat)[:-1], self.latest_frame,
                                       self.settings.persona, self.settings.title,
                                       name=self.settings.bot_nick or "Hermes")
            parsed = self._llm.complete_structured(**req).parsed or {}
            reply = clean_reply(str(parsed.get("reply", "")))
            if parsed.get("answerable") and reply and self.chat.say(reply, reply_to=msg.id or None):
                self._record({"type": "answer", "t": t, "user": msg.user, "text": msg.text, "reply": reply})
            else:
                self._record({"type": "chat", "t": t, "user": msg.user, "text": msg.text})
        except Exception as exc:
            logger.warning("cohost: answer failed: %s", exc)
        finally:
            with self._lock:
                self._pending -= 1

    def _highlight(self, reason: str, t: float) -> None:
        clip = bool(self._obs is not None and self._obs.save_replay())
        frame_name = ""
        if self.latest_frame:
            frame_name = f"frames/{int(t)}.jpg"
            (self.dir / frame_name).write_bytes(self.latest_frame)
        self._record({"type": "highlight", "t": t, "reason": reason, "clip": clip, "frame": frame_name})
        self._safe_notify(f"🔥 Highlight {fmt_ts(t)}: {reason}" + (" — replay clip saved" if clip else ""))

    # frames ----------------------------------------------------------------------
    def _frame_loop(self) -> None:
        next_obs_try = 0.0
        while not self._stop.is_set():
            if self._obs is None and time.time() >= next_obs_try:
                self._obs = self._connect_obs()
                next_obs_try = time.time() + OBS_RETRY
            frame = self._poll_obs() if self._obs is not None else None
            if frame is None and self.settings.screen_fallback:
                frame = self._screen()
                self.frame_source = "screen" if frame else "none"
            if frame is not None:
                self.latest_frame = frame
            self._stop.wait(self.settings.frame_interval)

    def _connect_obs(self) -> Any:
        s = self.settings
        try:
            obs = self._obs_factory(host=s.obs_host, port=s.obs_port, password=s.obs_password)
            obs.connect()
            return obs
        except Exception as exc:
            logger.info("cohost: OBS not reachable (%s); using screen fallback", exc)
            return None

    def _poll_obs(self) -> bytes | None:
        try:
            frame = self._obs.screenshot()
            seconds = self._obs.stream_seconds()
            if seconds is not None:
                self._obs_base = (seconds, time.time())
            self.frame_source = "obs"
            return frame
        except Exception as exc:
            logger.info("cohost: lost OBS connection: %s", exc)
            self._obs.close()
            self._obs = None
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
