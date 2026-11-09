"""Local, model-free chat rules. v1 flags to the streamer; it never bans."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass

from .twitch import ChatMessage

LINK_RE = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|net|org|io|gg|biz|xyz|ru|ly|tv)\b", re.IGNORECASE)
REPEAT_WINDOW = 30.0
REPEAT_LIMIT = 3


@dataclass(frozen=True)
class Flag:
    reason: str
    severity: str  # low | medium | high


class Moderator:
    def __init__(self, blocked_terms: list[str] | tuple[str, ...] = (), caps_ratio: float = 0.7,
                 caps_min_letters: int = 12) -> None:
        self.blocked = [t.lower() for t in blocked_terms if t.strip()]
        self.caps_ratio, self.caps_min_letters = caps_ratio, caps_min_letters
        self._recent: deque[tuple[str, str, float]] = deque(maxlen=500)

    def check(self, msg: ChatMessage) -> Flag | None:
        if msg.is_mod:
            return None
        text = msg.text.lower()
        for term in self.blocked:
            if re.search(rf"\b{re.escape(term)}\b", text):
                return Flag(f"blocked term '{term}'", "high")
        if LINK_RE.search(msg.text) and not msg.is_sub:
            return Flag("link from a first-time chatter" if msg.first_msg else "link from a non-subscriber",
                        "high" if msg.first_msg else "medium")
        letters = [c for c in msg.text if c.isalpha()]
        if len(letters) >= self.caps_min_letters and sum(c.isupper() for c in letters) / len(letters) >= self.caps_ratio:
            return Flag("all caps", "low")
        return self._repeat(msg.user, " ".join(text.split()), msg.ts)

    def _repeat(self, user: str, norm: str, ts: float) -> Flag | None:
        self._recent.append((user, norm, ts))
        count = sum(1 for u, n, t in self._recent if u == user and n == norm and ts - t <= REPEAT_WINDOW)
        return Flag("repeated message", "low") if count >= REPEAT_LIMIT else None
