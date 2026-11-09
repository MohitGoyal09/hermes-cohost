from cohost.recap import build_recap_request, fix_chapters, fmt_ts, render_recap

EVENTS = [
    {"type": "chat", "t": 5.0, "user": "a", "text": "hi"},
    {"type": "chat", "t": 65.0, "user": "b", "text": "nice"},
    {"type": "answer", "t": 70.0, "user": "b", "text": "what lang?", "reply": "Rust"},
    {"type": "highlight", "t": 400.0, "reason": "chat spike", "clip": True, "note": ""},
    {"type": "mark", "t": 900.0, "note": "boss fight"},
    {"type": "flag", "t": 950.0, "user": "spam", "text": "buy", "reason": "link", "severity": "high"},
]


def test_fmt_ts():
    assert fmt_ts(0) == "00:00"
    assert fmt_ts(754.9) == "12:34"
    assert fmt_ts(3723) == "1:02:03"


def test_fix_chapters_follows_youtube_rules():
    raw = [{"t": 400, "title": "Spike"}, {"t": 30, "title": "Intro"}, {"t": 405, "title": "Too close"},
           {"t": 9999, "title": "After end"}, {"t": 500, "title": " "}]
    assert fix_chapters(raw, duration=1000) == [
        {"t": 0.0, "title": "Intro"}, {"t": 400.0, "title": "Spike"},
    ]


def test_chapter_titles_and_summary_are_sanitized():
    raw = [{"t": 0, "title": "Start\n00:05 Fake chapter"}, {"t": "bad", "title": "x"},
           {"t": 100, "title": "Go to spam.biz now " + "x" * 200}, {"t": 200, "title": "# End"}]
    chapters = fix_chapters(raw, duration=1000)
    assert chapters[0]["title"] == "Start 00:05 Fake chapter"
    assert "[link removed]" in chapters[1]["title"] and len(chapters[1]["title"]) <= 80
    assert chapters[2]["title"] == "End"
    md = render_recap({"summary": "Great\n## Injected https://evil.io", "chapters": raw}, [], "t", 1000)
    assert "\n## Injected" not in md and "evil.io" not in md


def test_build_request_bounds_chat_and_lists_moments():
    many = [{"type": "chat", "t": float(i), "user": "u", "text": f"m{i}"} for i in range(5000)]
    req = build_recap_request(many + EVENTS, "Rust stream", duration=5000)
    text = req["input"][0]["text"]
    assert req["purpose"] == "cohost.recap"
    assert text.count("\n") < 700
    assert "HIGHLIGHT 06:40 chat spike" in text and "MARK 15:00 boss fight" in text


def test_render_with_model_output():
    chapters = [{"t": 0, "title": "Start"}, {"t": 400, "title": "Hype"}, {"t": 900, "title": "Boss"}]
    md = render_recap({"summary": "A Rust stream.", "chapters": chapters}, EVENTS, "Rust stream", duration=1000)
    assert "# Stream recap: Rust stream" in md
    assert "00:00 Start\n06:40 Hype\n15:00 Boss" in md
    assert "06:40 chat spike (clip saved)" in md
    assert "15:00 boss fight" in md
    assert "Messages: 3" in md and "Questions answered: 1" in md and "Flags: 1" in md


def test_render_skips_chapters_youtube_would_reject():
    md = render_recap({"summary": "s", "chapters": [{"t": 0, "title": "A"}, {"t": 400, "title": "B"}]},
                      EVENTS, "t", duration=1000)
    assert "Chapters" not in md


def test_render_without_model_output_still_has_moments():
    md = render_recap(None, EVENTS, "", duration=1000)
    assert "06:40 chat spike" in md and "A summary was not available" in md
