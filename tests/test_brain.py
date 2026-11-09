from cohost.brain import (
    ANSWER_SCHEMA, ReplyLimiter, build_answer_request, clean_reply, is_addressed, strip_address,
)

NAMES = ["hermes", "HermesBot"]


def test_is_addressed():
    assert is_addressed("@hermes what game is this?", NAMES)
    assert is_addressed("hey Hermes, what language?", NAMES)
    assert is_addressed("hermesbot: help", NAMES)
    assert is_addressed("!ask what is on screen", NAMES)
    assert not is_addressed("hermes is a cool name", NAMES[:0])
    assert not is_addressed("I like thermes", NAMES)
    assert not is_addressed("gg", NAMES)


def test_strip_address():
    assert strip_address("@hermes what game is this?", NAMES) == "what game is this?"
    assert strip_address("hey Hermes, what language?", NAMES) == "what language?"
    assert strip_address("!ask what is on screen", NAMES) == "what is on screen"


def test_build_request_has_image_and_treats_chat_as_data():
    req = build_answer_request("what is this?", "viewer1", ["a: hi", "b: lol"], b"JPEG", "friendly", "Rust stream")
    assert req["json_schema"] == ANSWER_SCHEMA
    assert req["purpose"] == "cohost.answer"
    kinds = [block["type"] for block in req["input"]]
    assert kinds == ["text", "image"]
    assert req["input"][1]["data"] == b"JPEG"
    assert "untrusted" in req["instructions"].lower()
    assert "viewer1" in req["input"][0]["text"] and "Rust stream" in req["input"][0]["text"]


def test_build_request_without_frame():
    req = build_answer_request("hi", "v", [], None, "", "")
    assert [b["type"] for b in req["input"]] == ["text"]


def test_clean_reply_blocks_commands_links_and_length():
    assert clean_reply("/ban someone") == "ban someone"
    assert clean_reply(".timeout x") == "timeout x"
    assert clean_reply("!drop all") == "drop all"
    assert clean_reply("see https://evil.example/x now") == "see [link removed] now"
    assert clean_reply("a\n\nb") == "a b"
    long = clean_reply("word " * 200, max_len=50)
    assert len(long) <= 50 and long.endswith("…")
    assert clean_reply("   ") == ""
    assert clean_reply("/ /ban x") == "ban x"
    assert clean_reply(". !/timeout y") == "timeout y"
    assert clean_reply("go to spam.biz/free or Free-Stuff.co.uk") == "go to [link removed] or [link removed]"
    assert clean_reply("version 1.5 is out") == "version 1.5 is out"
    assert clean_reply("open main.rs and index.js, then README.md") == "open main.rs and index.js, then README.md"
    assert clean_reply("join discord.gg/abc") == "join [link removed]"


def test_reply_limiter():
    lim = ReplyLimiter(per_user_cooldown=30, per_minute=2)
    assert lim.allow("a", 0.0)
    assert not lim.allow("a", 10.0)  # same user cooldown
    assert lim.allow("b", 11.0)
    assert not lim.allow("c", 12.0)  # global cap
    assert lim.allow("c", 70.0)
