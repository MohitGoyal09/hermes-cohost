"""Spot highlight moments from chat: message-rate spikes and 'clip it' bursts."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass

CLIP_RE = re.compile(r"\bclip\b|\bclipped\b", re.IGNORECASE)
MIN_BASELINE_RATE = 0.2  # msgs/s; stops a silent chat from making every burst a "spike"


@dataclass(frozen=True)
class Hype:
    reason: str
    ts: float


class HypeDetector:
    def __init__(self, window: float = 10.0, baseline_window: float = 120.0, ratio: float = 3.0,
                 min_msgs: int = 8, keyword_min: int = 3, cooldown: float = 60.0) -> None:
        self.window, self.baseline_window = window, baseline_window
        self.ratio, self.min_msgs, self.keyword_min = ratio, min_msgs, keyword_min
        self.cooldown = cooldown
        self._times: deque[float] = deque()
        self._clips: deque[float] = deque()
        self._last_hype = float("-inf")

    def feed(self, text: str, ts: float, *, is_mod: bool = False) -> Hype | None:
        self._times.append(ts)
        while self._times and ts - self._times[0] > self.baseline_window + self.window:
            self._times.popleft()
        if CLIP_RE.search(text):
            self._clips.append(ts)
        while self._clips and ts - self._clips[0] > self.window:
            self._clips.popleft()

        if ts - self._last_hype < self.cooldown:
            return None
        reason = self._reason(text, ts, is_mod)
        if reason:
            self._last_hype = ts
            self._clips.clear()
            return Hype(reason, ts)
        return None

    def _reason(self, text: str, ts: float, is_mod: bool) -> str | None:
        if is_mod and text.strip().lower().startswith("!clip"):
            return "mod asked for a clip"
        if len(self._clips) >= self.keyword_min:
            return f"chat asked to clip it ({len(self._clips)} messages)"
        recent = sum(1 for t in self._times if ts - t <= self.window)
        older = len(self._times) - recent
        baseline = max(older / self.baseline_window, MIN_BASELINE_RATE)
        if recent >= self.min_msgs and recent / self.window >= self.ratio * baseline:
            return f"chat spike ({recent} messages in {self.window:.0f}s)"
        return None
