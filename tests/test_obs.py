import base64
import hashlib
import json
import threading

import pytest
from websockets.sync.server import serve

from cohost.obs import ObsClient, ObsError, auth_string, parse_timecode

PASSWORD, SALT, CHALLENGE = "secret", "c2FsdA==", "Y2hhbGxlbmdl"
JPEG = b"\xff\xd8fakejpeg\xff\xd9"


def _expected_auth() -> str:
    secret = base64.b64encode(hashlib.sha256((PASSWORD + SALT).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + CHALLENGE).encode()).digest()).decode()


def test_auth_string_and_timecode():
    assert auth_string(PASSWORD, SALT, CHALLENGE) == _expected_auth()
    assert parse_timecode("01:02:03.500") == pytest.approx(3723.5)
    assert parse_timecode("") is None


class FakeObs:
    def __init__(self, *, streaming=True, replay=True):
        self.streaming, self.replay = streaming, replay
        self.requests: list[str] = []
        self.identify: dict = {}
        self.server = serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _respond(self, req: dict) -> tuple[bool, dict]:
        kind = req["requestType"]
        self.requests.append(kind)
        if kind == "GetCurrentProgramScene":
            return True, {"sceneName": "Main"}
        if kind == "GetSourceScreenshot":
            assert req["requestData"]["sourceName"] == "Main"
            return True, {"imageData": "data:image/jpg;base64," + base64.b64encode(JPEG).decode()}
        if kind == "GetStreamStatus":
            return True, {"outputActive": self.streaming, "outputTimecode": "00:10:00.000"}
        if kind == "GetRecordStatus":
            return True, {"outputActive": False, "outputTimecode": "00:00:00.000"}
        if kind == "GetReplayBufferStatus":
            return True, {"outputActive": self.replay}
        if kind == "SaveReplayBuffer":
            return True, {}
        return False, {}

    def _handler(self, ws):
        ws.send(json.dumps({"op": 0, "d": {"rpcVersion": 1, "authentication": {"challenge": CHALLENGE, "salt": SALT}}}))
        self.identify = json.loads(ws.recv())["d"]
        ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        for raw in ws:
            req = json.loads(raw)["d"]
            ws.send(json.dumps({"op": 5, "d": {"eventType": "Noise"}}))  # events must be skipped
            ok, data = self._respond(req)
            status = {"result": ok, "code": 100 if ok else 600, "comment": "" if ok else "nope"}
            ws.send(json.dumps({"op": 7, "d": {"requestType": req["requestType"], "requestId": req["requestId"],
                                               "requestStatus": status, "responseData": data}}))

    def close(self):
        self.server.shutdown()


@pytest.fixture
def obs():
    fake = FakeObs()
    yield fake
    fake.close()


def test_connect_authenticates_and_screenshots(obs):
    client = ObsClient(port=obs.port, password=PASSWORD)
    client.connect()
    assert obs.identify["authentication"] == _expected_auth()
    assert client.screenshot(width=640) == JPEG
    assert client.stream_seconds() == pytest.approx(600.0)
    assert client.save_replay() is True
    client.close()


def test_failed_request_raises(obs):
    client = ObsClient(port=obs.port, password=PASSWORD)
    client.connect()
    with pytest.raises(ObsError):
        client.request("Bogus")
    client.close()


def test_not_streaming_and_no_replay():
    fake = FakeObs(streaming=False, replay=False)
    try:
        client = ObsClient(port=fake.port, password=PASSWORD)
        client.connect()
        assert client.stream_seconds() is None
        assert client.save_replay() is False
        assert "SaveReplayBuffer" not in fake.requests
        client.close()
    finally:
        fake.close()
