"""OBS WebSocket v5 client: what viewers see (program scene), stream time, replay clips."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import uuid
from typing import Any

from websockets.sync.client import connect as ws_connect

logger = logging.getLogger(__name__)

OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED, OP_REQUEST, OP_RESPONSE = 0, 1, 2, 6, 7


class ObsError(Exception):
    pass


def auth_string(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def parse_timecode(code: str) -> float | None:
    """'HH:MM:SS.mmm' -> seconds."""
    try:
        h, m, s = code.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return None


class ObsClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 4455, password: str | None = None,
                 timeout: float = 5.0) -> None:
        self._url = f"ws://{host}:{port}"
        self._password = password
        self._timeout = timeout
        self._ws: Any = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        ws = ws_connect(self._url, open_timeout=self._timeout, max_size=32 * 1024 * 1024)
        hello = json.loads(ws.recv(timeout=self._timeout))
        if hello.get("op") != OP_HELLO:
            ws.close()
            raise ObsError("OBS did not send Hello")
        identify: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = hello["d"].get("authentication")
        if auth:
            if not self._password:
                ws.close()
                raise ObsError("OBS WebSocket needs a password (set obs_password)")
            identify["authentication"] = auth_string(self._password, auth["salt"], auth["challenge"])
        ws.send(json.dumps({"op": OP_IDENTIFY, "d": identify}))
        reply = json.loads(ws.recv(timeout=self._timeout))
        if reply.get("op") != OP_IDENTIFIED:
            ws.close()
            raise ObsError("OBS refused the connection (wrong password?)")
        self._ws = ws

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            ws.close()

    def request(self, request_type: str, data: dict | None = None) -> dict:
        if self._ws is None:
            raise ObsError("not connected")
        request_id = uuid.uuid4().hex
        payload = {"requestType": request_type, "requestId": request_id, "requestData": data or {}}
        with self._lock:
            self._ws.send(json.dumps({"op": OP_REQUEST, "d": payload}))
            while True:  # skip events until our response arrives
                msg = json.loads(self._ws.recv(timeout=self._timeout))
                if msg.get("op") == OP_RESPONSE and msg["d"].get("requestId") == request_id:
                    break
        status = msg["d"].get("requestStatus", {})
        if not status.get("result"):
            raise ObsError(f"{request_type} failed: {status.get('comment') or status.get('code')}")
        return msg["d"].get("responseData") or {}

    def screenshot(self, width: int = 1024) -> bytes:
        scene = self.request("GetCurrentProgramScene")
        name = scene.get("sceneName") or scene.get("currentProgramSceneName")
        shot = self.request("GetSourceScreenshot", {
            "sourceName": name, "imageFormat": "jpg", "imageWidth": width, "imageCompressionQuality": 70,
        })
        return base64.b64decode(shot["imageData"].split(",", 1)[-1])

    def stream_seconds(self) -> float | None:
        for kind in ("GetStreamStatus", "GetRecordStatus"):
            status = self.request(kind)
            if status.get("outputActive"):
                return parse_timecode(status.get("outputTimecode", ""))
        return None

    def save_replay(self) -> bool:
        try:
            if not self.request("GetReplayBufferStatus").get("outputActive"):
                return False
            self.request("SaveReplayBuffer")
            return True
        except ObsError as exc:
            logger.info("cohost: replay buffer unavailable: %s", exc)
            return False
