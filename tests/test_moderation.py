from cohost.moderation import Moderator
from cohost.twitch import ChatMessage


def msg(text, user="viewer", *, mod=False, sub=False, first=False, ts=0.0):
    return ChatMessage(id="1", user=user, display=user, text=text, ts=ts, is_mod=mod,
                       is_broadcaster=False, is_sub=sub, first_msg=first)


def test_clean_message_passes():
    assert Moderator().check(msg("great play!")) is None


def test_blocked_term_is_high():
    flag = Moderator(blocked_terms=["badword"]).check(msg("you BADWORD"))
    assert flag.severity == "high" and "badword" in flag.reason


def test_link_rules():
    mod = Moderator()
    assert mod.check(msg("cheap viewers at www.spam.biz", first=True)).severity == "high"
    assert mod.check(msg("see https://x.io/a")).severity == "medium"
    assert mod.check(msg("see https://x.io/a", sub=True)) is None


def test_caps_and_repeats():
    mod = Moderator()
    assert mod.check(msg("WHY IS THIS SO LOUD OMG")).severity == "low"
    assert mod.check(msg("lol")) is None  # short caps are fine
    flags = [mod.check(msg("buy my stuff", ts=float(i))) for i in range(3)]
    assert flags[:2] == [None, None] and flags[2].reason == "repeated message"


def test_mods_are_exempt():
    assert Moderator(blocked_terms=["x"]).check(msg("x https://a.b", mod=True)) is None
