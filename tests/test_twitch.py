import threading
import time

from cohost.twitch import ChatMessage, TwitchChat, parse_line, to_chat_message

PRIVMSG = (
    "@badge-info=;badges=broadcaster/1;display-name=Mohit;first-msg=0;id=abc-123;mod=0;"
    "subscriber=0;user-type= :mohit!mohit@mohit.tmi.twitch.tv PRIVMSG #mohit :@hermes what\\sis this?"
)


def test_parse_privmsg_with_tags():
    tags, prefix, command, params = parse_line(PRIVMSG)
    assert command == "PRIVMSG"
    assert prefix.startswith("mohit!")
    assert params == ["#mohit", "@hermes what\\sis this?"]
    assert tags["display-name"] == "Mohit"
    assert tags["user-type"] == ""


def test_parse_unescapes_tag_values():
    tags, *_ = parse_line(r"@system-msg=a\sb\:c\\d :tmi.twitch.tv USERNOTICE #x")
    assert tags["system-msg"] == "a b;c\\d"


def test_parse_ping_and_numeric_and_empty():
    assert parse_line("PING :tmi.twitch.tv") == ({}, "", "PING", ["tmi.twitch.tv"])
    assert parse_line(":tmi.twitch.tv 001 justinfan1 :Welcome")[2] == "001"
    assert parse_line("") is None
    assert parse_line("   ") is None


def test_to_chat_message_flags():
    tags, prefix, command, params = parse_line(PRIVMSG)
    msg = to_chat_message(tags, prefix, params, now=5.0)
    assert msg == ChatMessage(
        id="abc-123", user="mohit", display="Mohit", text="@hermes what\\sis this?", ts=5.0,
        is_mod=True, is_broadcaster=True, is_sub=False, first_msg=False,
    )


class FakeSocket:
    """In-memory socket: server lines are queued, client writes are recorded."""

    def __init__(self, incoming: list[str]):
        self._incoming = [line.encode() for line in incoming]
        self.sent: list[str] = []
        self.closed = False
        self._lock = threading.Lock()

    def sendall(self, data: bytes) -> None:
        with self._lock:
            self.sent.extend(data.decode().split("\r\n")[:-1])

    def recv(self, _n: int) -> bytes:
        while not self.closed:
            if self._incoming:
                return self._incoming.pop(0)
            time.sleep(0.005)
        return b""

    def settimeout(self, _t) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _run(chat: TwitchChat, sock: FakeSocket, until) -> None:
    chat.start()
    deadline = time.time() + 2
    while time.time() < deadline and not until():
        time.sleep(0.01)
    chat.stop()


def test_anonymous_handshake_and_message_delivery():
    got: list[ChatMessage] = []
    sock = FakeSocket([":tmi.twitch.tv 001 x :Welcome\r\n", PRIVMSG[:60], PRIVMSG[60:] + "\r\n"])
    chat = TwitchChat("#MoHit", on_message=got.append, connect=lambda: sock)
    _run(chat, sock, lambda: got)
    assert sock.sent[0] == "CAP REQ :twitch.tv/tags twitch.tv/commands"
    assert sock.sent[1].startswith("NICK justinfan")
    assert not any(line.startswith("PASS") for line in sock.sent)
    assert "JOIN #mohit" in sock.sent
    assert got[0].text == "@hermes what\\sis this?"
    assert chat.can_send is False
    assert chat.say("hi") is False


def test_token_handshake_ping_and_say_with_reply():
    sock = FakeSocket([":tmi.twitch.tv 001 bot :Welcome\r\n", "PING :tmi.twitch.tv\r\n"])
    chat = TwitchChat("mohit", on_message=lambda m: None, nick="HermesBot", token="abc", connect=lambda: sock)
    chat.start()
    deadline = time.time() + 2
    while time.time() < deadline and "PONG :tmi.twitch.tv" not in sock.sent:
        time.sleep(0.01)
    assert chat.say("line1\r\nline2", reply_to="abc-123") is True
    chat.stop()
    assert "PASS oauth:abc" in sock.sent
    assert "NICK hermesbot" in sock.sent
    assert "PONG :tmi.twitch.tv" in sock.sent
    assert "@reply-parent-msg-id=abc-123 PRIVMSG #mohit :line1 line2" in sock.sent


def test_say_rate_limit_and_length_cap():
    sock = FakeSocket([])
    chat = TwitchChat("c", on_message=lambda m: None, nick="b", token="oauth:t", connect=lambda: sock)
    chat._sock = sock  # connected without the reader thread
    chat._connected = True
    results = [chat.say("x" * 600) for _ in range(21)]
    assert results[:20] == [True] * 20
    assert results[20] is False
    assert len(sock.sent[0].split(" :", 1)[1]) == 500
