"""Streamer-facing messages: one live status card edited in place, plus alerts.

ctx.post_message only reaches the chat bound to the calling context, and a thread the plugin
starts does not inherit that binding. So the slash command starts this loop inside a copy of
its own context, and every post/edit happens on this one thread.
"""

from __future__ import annotations

import contextvars
import logging
import queue
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)
MAX_QUEUED = 50  # alerts beyond this are dropped rather than delaying the card for minutes


class Notifier:
    def __init__(self, post: Callable[[str], Any], status: Callable[[], str], interval: float = 20.0) -> None:
        self._post, self._status, self._interval = post, status, interval
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=MAX_QUEUED)
        self._final: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        ctx = contextvars.copy_context()
        self._thread = threading.Thread(target=ctx.run, args=(self._loop,), name="cohost-notify", daemon=True)
        self._thread.start()

    def send(self, text: str) -> None:
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            logger.info("cohost: alert dropped, notifier backlog full")

    def stop(self, final_status: str) -> None:
        self._final = final_status
        try:
            self._queue.put(None, timeout=5)  # the loop drains the queue; never hang /cohost stop
        except queue.Full:
            logger.warning("cohost: notifier did not drain; final status not posted")
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        last = self._safe_status("")
        card = self._call(self._post, last)
        while True:
            try:
                item = self._queue.get(timeout=self._interval)
            except queue.Empty:
                item = ""
            if item is None:
                if card is not None:
                    self._call(card.edit, self._final or last)
                return
            if item:
                self._call(self._post, item)
            current = self._safe_status(last)
            if card is not None and current != last:
                self._call(card.edit, current)
                last = current

    def _safe_status(self, fallback: str) -> str:
        try:
            return self._status()
        except Exception as exc:
            logger.warning("cohost: status text failed: %s", exc)
            return fallback

    @staticmethod
    def _call(fn: Callable[[str], Any], text: str) -> Any:
        try:
            return fn(text)
        except Exception as exc:
            logger.warning("cohost: could not message the streamer: %s", exc)
            return None
