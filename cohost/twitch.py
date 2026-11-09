"""Twitch chat over IRC (TLS). Anonymous read; sending needs a bot token."""

from __future__ import annotations

import logging
import random
import socket
import ssl
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

SEND_LIMIT = 20  # messages per window, Twitch's limit for a normal user
SEND_WINDOW = 30.0
MAX_TEXT = 500
_UNESCAPE = {"s": " ", ":": ";", "\\": "\\", "r": "\r", "n": "\n"}


@dataclass(frozen=True)
class ChatMessage:
    id: str
    user: str
    display: str
    text: str
    ts: float
    is_mod: bool
    is_broadcaster: bool
    is_sub: bool
    first_msg: bool


def _unescape(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            out.append(_UNESCAPE.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def parse_line(line: str) -> tuple[dict[str, str], str, str, list[str]] | None:
    """Split an IRCv3 line into (tags, prefix, command, params); trailing is the last param."""
    line = line.strip("\r\n")
    if not line.strip():
        return None
    tags: dict[str, str] = {}
    if line.startswith("@"):
        raw, _, line = line[1:].partition(" ")
        for part in raw.split(";"):
            key, _, value = part.partition("=")
            tags[key] = _unescape(value)
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    head, sep, trailing = line.partition(" :")
    parts = head.split()
    params = parts[1:] + ([trailing] if sep else [])
    return tags, prefix, parts[0] if parts else "", params


def to_chat_message(tags: dict[str, str], prefix: str, params: list[str], now: float) -> ChatMessage | None:
    if len(params) < 2:
        return None
    user = prefix.split("!", 1)[0].lower()
    broadcaster = "broadcaster/" in tags.get("badges", "")
    return ChatMessage(
        id=tags.get("id", ""),
        user=user,
        display=tags.get("display-name") or user,
        text=params[-1],
        ts=now,
        is_mod=tags.get("mod") == "1" or broadcaster,
        is_broadcaster=broadcaster,
        is_sub=tags.get("subscriber") == "1",
        first_msg=tags.get("first-msg") == "1",
    )


class TwitchChat:
    def __init__(
        self,
        channel: str,
        *,
        on_message: Callable[[ChatMessage], None],
        nick: str | None = None,
        token: str | None = None,
        host: str = "irc.chat.twitch.tv",
        port: int = 6697,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        self.channel = channel.lstrip("#").lower()
        self._on_message = on_message
        self._token = token
        self._nick = nick.lower() if (nick and token) else f"justinfan{random.randint(10000, 99999)}"
        self._host, self._port = host, port
        self._connect = connect or self._default_connect
        self._sock: Any = None
        self._connected = False
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._sent_at: deque[float] = deque()
        self._thread: threading.Thread | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def can_send(self) -> bool:
        return bool(self._token)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"cohost-twitch-{self.channel}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def say(self, text: str, reply_to: str | None = None) -> bool:
        if not self.can_send or not self._connected:
            return False
        text = " ".join(text.replace("\r", " ").replace("\n", " ").split())[:MAX_TEXT]
        if not text:
            return False
        with self._send_lock:
            now = time.monotonic()
            while self._sent_at and now - self._sent_at[0] > SEND_WINDOW:
                self._sent_at.popleft()
            if len(self._sent_at) >= SEND_LIMIT:
                return False
            tag = f"@reply-parent-msg-id={reply_to} " if reply_to else ""
            if not self._send(f"{tag}PRIVMSG #{self.channel} :{text}"):
                return False
            self._sent_at.append(now)
        return True

    def _default_connect(self) -> Any:
        raw = socket.create_connection((self._host, self._port), timeout=15)
        return ssl.create_default_context().wrap_socket(raw, server_hostname=self._host)

    def _send(self, line: str) -> bool:
        try:
            self._sock.sendall(f"{line}\r\n".encode())
            return True
        except (OSError, AttributeError) as exc:
            logger.warning("cohost: twitch send failed: %s", exc)
            return False

    def _close(self) -> None:
        self._connected = False
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 1.0
            except Exception as exc:  # network errors must never kill the worker
                logger.warning("cohost: twitch connection error: %s", exc)
            self._close()
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 30.0)

    def _session(self) -> None:
        self._sock = self._connect()
        self._send("CAP REQ :twitch.tv/tags twitch.tv/commands")
        if self._token:
            token = self._token if self._token.startswith("oauth:") else f"oauth:{self._token}"
            self._send(f"PASS {token}")
        self._send(f"NICK {self._nick}")
        self._send(f"JOIN #{self.channel}")
        buf = b""
        while not self._stop.is_set():
            chunk = self._sock.recv(4096)
            if not chunk:
                return
            buf += chunk
            *lines, buf = buf.split(b"\r\n")
            for raw in lines:
                if self._handle(raw.decode("utf-8", "replace")) == "reconnect":
                    return

    def _handle(self, line: str) -> str | None:
        parsed = parse_line(line)
        if parsed is None:
            return None
        tags, prefix, command, params = parsed
        if command == "PING":
            self._send(f"PONG :{params[-1] if params else 'tmi.twitch.tv'}")
        elif command == "001":
            self._connected = True
        elif command == "RECONNECT":
            return "reconnect"
        elif command == "NOTICE" and params and "authentication failed" in params[-1].lower():
            logger.error("cohost: twitch rejected the bot token")
        elif command == "PRIVMSG":
            msg = to_chat_message(tags, prefix, params, time.time())
            if msg is not None:
                try:
                    self._on_message(msg)
                except Exception:
                    logger.exception("cohost: chat handler failed")
        return None
