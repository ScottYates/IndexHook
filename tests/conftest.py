import json
import re
import socket
import subprocess
import time

import httpx
import pytest
from aiosmtpd.controller import Controller
from starlette.testclient import TestClient

from indexhook.app import create_app


def _ffmpeg(out, source):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source,
                    "-t", "2", "-c:a", "aac", str(out)], check=True)


@pytest.fixture(scope="session")
def tone(tmp_path_factory):
    """Two seconds of audible tone in the ring's container format (M4A)."""
    p = tmp_path_factory.mktemp("audio") / "tone.m4a"
    _ffmpeg(p, "sine=frequency=440:sample_rate=16000")
    return p


@pytest.fixture(scope="session")
def silence(tmp_path_factory):
    p = tmp_path_factory.mktemp("audio") / "silence.m4a"
    _ffmpeg(p, "anullsrc=r=16000:cl=mono")
    return p


class FakeWorld:
    """Stands in for every outside HTTP service. Records what it was sent."""

    def __init__(self):
        self.calls = []
        self.texts = {"minimax": "Note to self, call Shivani about the YunoHost upgrade.",
                      "openai": "Note to self, call Shivani about the Yunohost upgrade.",
                      "elevenlabs": "Note to self, call Shivani about the YunoHost upgrade.",
                      "deepgram": "Note to self call Shivani about the YunoHost upgrade"}
        self.status = {}
        self.llm = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        body = request.content
        self.calls.append({"host": host, "path": path, "query": str(request.url.query, "ascii"),
                           "headers": dict(request.headers), "body": body})
        if path.endswith("/speech_to_text"):
            return self._stt("minimax", {"text": self.texts["minimax"], "duration": 2.0, "trace_id": "t"})
        if path.endswith("/audio/transcriptions"):
            return self._stt("openai", {"text": self.texts["openai"]})
        if host == "api.elevenlabs.io":
            return self._stt("elevenlabs", {"text": self.texts["elevenlabs"], "language_probability": 0.99})
        if host == "api.deepgram.com":
            return self._stt("deepgram", {"results": {"channels": [{"alternatives": [
                {"transcript": self.texts["deepgram"], "confidence": 0.97}]}]}})
        if path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": self.llm}}]})
        return httpx.Response(200, json={"ok": True})

    def _stt(self, name, payload):
        code = self.status.get(name, 200)
        return httpx.Response(code, json=payload if code == 200 else {"error": "nope"})

    def to(self, host):
        return [c for c in self.calls if c["host"] == host]


class Sink:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(envelope)
        return "250 OK"


@pytest.fixture
def smtp():
    sink = Sink()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sink.port = sock.getsockname()[1]
    c = Controller(sink, hostname="127.0.0.1", port=sink.port)
    c.start()
    yield sink
    c.stop()


@pytest.fixture
def world():
    return FakeWorld()


@pytest.fixture
def make_client(tmp_path, world):
    made = []

    def make(root_path="", sub="data"):
        app = create_app(str(tmp_path / sub), root_path, transport=httpx.MockTransport(world.handler))
        c = TestClient(app, base_url="http://testserver", follow_redirects=False)
        c.__enter__()
        c.db = app.state.db
        made.append(c)
        return c

    yield make
    for c in made:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def register(c, name="scott", password="correct horse", email="scott@example.org", root=""):
    r = c.post(root + "/register", data={"username": name, "password": password, "password2": password, "email": email})
    assert r.status_code == 303, r.text
    return csrf_of(c.get(root + "/").text)


def new_token(c, csrf, root=""):
    r = c.post(root + "/token", data={"csrf": csrf})
    return re.search(r'class="secret">(ih_[^<]+)<', r.text).group(1)


def wait_done(c, rec_id, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        rec = c.db.one("SELECT * FROM recordings WHERE id=?", rec_id)
        pending = c.db.one("SELECT COUNT(*) n FROM deliveries WHERE recording_id=? AND status IN ('pending','sending')", rec_id)["n"]
        if rec["status"] not in ("queued", "transcribing") and not pending:
            return rec
        time.sleep(0.1)
    raise AssertionError(f"recording {rec_id} still {rec['status']}")


def set_user(c, user_id, **kv):
    for k, v in kv.items():
        c.db.set_setting(f"user:{user_id}", k, v)


def detail(rec):
    return json.loads(rec["detail"])
