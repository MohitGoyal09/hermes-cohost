"""Optional model pin for co-host calls (e.g. a cheap, fast vision model for viewer answers)."""

from __future__ import annotations

from typing import Any


class PinnedLlm:
    """Wraps ctx.llm so every co-host call carries the streamer's chosen provider/model.
    Hermes only honours them when the plugin holds the llm.*_override capabilities."""

    def __init__(self, llm: Any, provider: str = "", model: str = "") -> None:
        self._llm = llm
        self._pin = {k: v for k, v in (("provider", provider), ("model", model)) if v}

    @property
    def pinned(self) -> dict[str, str]:
        return dict(self._pin)

    def complete_structured(self, **kwargs: Any) -> Any:
        return self._llm.complete_structured(**{**kwargs, **self._pin})
