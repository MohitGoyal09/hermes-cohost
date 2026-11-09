from cohost.highlights import HypeDetector


def test_quiet_chat_never_triggers():
    det = HypeDetector()
    assert all(det.feed("hello", t * 5.0) is None for t in range(60))


def test_rate_spike_triggers_once_then_cools_down():
    det = HypeDetector(window=10, ratio=3, min_msgs=8, cooldown=60)
    for t in range(0, 120, 10):  # baseline: one message every 10 s
        assert det.feed("chatting", float(t)) is None
    hits = [det.feed("WOW", 120 + i * 0.5) for i in range(12)]
    fired = [h for h in hits if h]
    assert len(fired) == 1
    assert fired[0].reason.startswith("chat spike")
    assert fired[0].ts == 120 + 7 * 0.5  # eighth message in the window
    assert all(det.feed("WOW", 125 + i * 0.2) is None for i in range(30))  # cooldown


def test_clip_keyword_burst_triggers():
    det = HypeDetector(keyword_min=3)
    assert det.feed("clip it", 1.0) is None
    assert det.feed("CLIP IT!!", 2.0) is None
    hype = det.feed("someone clip that", 3.0)
    assert hype is not None and "clip" in hype.reason


def test_mod_clip_command_triggers_immediately():
    det = HypeDetector()
    hype = det.feed("!clip", 5.0, is_mod=True)
    assert hype is not None and hype.reason == "mod asked for a clip"
    assert det.feed("!clip", 6.0, is_mod=False) is None
