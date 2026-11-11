from types import SimpleNamespace

from cohost.llm import PinnedLlm


class Recorder:
    def __init__(self):
        self.kwargs = None

    def complete_structured(self, **kw):
        self.kwargs = kw
        return SimpleNamespace(parsed={}, text="")


def test_pin_adds_provider_and_model():
    rec = Recorder()
    PinnedLlm(rec, "gemini", "gemini-3.6-flash").complete_structured(instructions="x", input=[])
    assert rec.kwargs == {"instructions": "x", "input": [], "provider": "gemini", "model": "gemini-3.6-flash"}


def test_empty_pin_passes_through():
    rec = Recorder()
    llm = PinnedLlm(rec)
    llm.complete_structured(purpose="p")
    assert rec.kwargs == {"purpose": "p"} and llm.pinned == {}
