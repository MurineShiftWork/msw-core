"""TrialRelay against a stub ingest server: endpoints, headers, payloads, timeouts, stop paths."""

from __future__ import annotations

import json
import multiprocessing
import queue
import signal
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import murineshiftwork.logic.trial_relay as relay_mod
from murineshiftwork.logic.task_process import TaskProcess
from murineshiftwork.logic.trial_relay import POST_TIMEOUT_S, TrialRelay


class _StubIngest:
    """A local HTTP server that records every POST (path, headers, JSON body)."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict, dict]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 (http.server API)
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                stub.requests.append((self.path, dict(self.headers), body))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    s = _StubIngest()
    yield s
    s.close()


@pytest.fixture
def keep_sigint():
    # run() ignores SIGINT for its own process; restore pytest's handler after an in-process run
    previous = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGINT, previous)


class _Opaque:
    def __str__(self) -> str:
        return "opaque"


def test_session_posts_start_trials_and_stop(stub, keep_sigint):
    q: queue.Queue = queue.Queue()
    for event in ({"trial_index": 0, "outcome": "hit"}, {"trial_index": 1}, None):
        q.put(event)
    relay = TrialRelay(
        q,
        stub.url + "/",
        setup="rig-a",
        session_start_payload={"subject": "m1", "task": "demo"},
        session_uuid="uuid-1",
        bearer_token="secret",
    )
    relay.run()

    paths = [r[0] for r in stub.requests]
    assert paths == [
        "/ingest/rig-a/start",
        "/ingest/rig-a/trial",
        "/ingest/rig-a/trial",
        "/ingest/rig-a/stop",
    ]
    for _, headers, _ in stub.requests:
        assert headers["Content-Type"] == "application/json"
        assert headers["Authorization"] == "Bearer secret"
    bodies = [r[2] for r in stub.requests]
    assert bodies[0] == {"subject": "m1", "task": "demo", "session_uuid": "uuid-1"}
    assert bodies[1] == {
        "trial_data": {"trial_index": 0, "outcome": "hit"},
        "session_uuid": "uuid-1",
    }
    assert bodies[2] == {"trial_data": {"trial_index": 1}, "session_uuid": "uuid-1"}
    assert set(bodies[3]) == {"ended_at", "session_uuid"}


def test_stop_sentinel_posts_summary_with_end_time(stub, keep_sigint):
    q: queue.Queue = queue.Queue()
    q.put({"__stop__": True, "summary": {"n_trials": 3}})
    TrialRelay(q, stub.url, "rig-a", {"subject": "m1"}).run()

    path, headers, body = stub.requests[-1]
    assert path == "/ingest/rig-a/stop"
    assert body["n_trials"] == 3
    assert "ended_at" in body
    # no token and no session uuid configured: neither is sent
    assert "Authorization" not in headers
    assert "session_uuid" not in body


def test_unserialisable_values_are_sent_as_strings(stub, keep_sigint):
    q: queue.Queue = queue.Queue()
    q.put({"value": _Opaque()})
    q.put(None)
    TrialRelay(q, stub.url, "rig-a", {}).run()
    assert stub.requests[1][2] == {"trial_data": {"value": "opaque"}}


def test_post_uses_short_timeout_and_swallows_errors(monkeypatch, keep_sigint):
    seen: list[float] = []

    def fail(req, timeout):
        seen.append(timeout)
        raise OSError("server down")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    q: queue.Queue = queue.Queue()
    q.put({"trial_index": 0})
    q.put(None)
    TrialRelay(q, "http://127.0.0.1:9", "rig-a", {}).run()  # must not raise
    assert POST_TIMEOUT_S == 0.5
    assert seen == [POST_TIMEOUT_S] * 3


def test_runs_as_a_daemon_process(stub):
    q = multiprocessing.Queue(maxsize=500)
    relay = TrialRelay(q, stub.url, "rig-a", {"subject": "m1"})
    assert relay.daemon is True
    relay.start()
    q.put_nowait({"trial_index": 0})
    q.put_nowait(None)
    relay.join(timeout=30)
    assert relay.exitcode == 0
    assert [r[0] for r in stub.requests] == [
        "/ingest/rig-a/start",
        "/ingest/rig-a/trial",
        "/ingest/rig-a/stop",
    ]


def test_task_process_starts_the_relay_when_a_server_is_configured(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "murineshiftwork.logic.machine_config.read_log_config",
        lambda: {"log_url": "http://ingest.invalid", "log_bearer_token": "t"},
    )
    started: list[dict] = []

    class FakeRelay:
        def __init__(self, queue, log_url, **kwargs):
            started.append({"log_url": log_url, **kwargs})

        def start(self):
            pass

    monkeypatch.setattr(relay_mod, "TrialRelay", FakeRelay)
    tp = TaskProcess(
        serial_port_bpod=None,
        out_path=str(tmp_path / "data"),
        subject="_test_subject",
        task="demo_task",
        auto_init=False,
        auto_start=False,
        require_bpod=False,
    )
    tp._start_relay()
    assert started and started[0]["log_url"] == "http://ingest.invalid"
    assert started[0]["bearer_token"] == "t"
    assert tp.input_kwargs["relay_queue"] is tp._relay_queue
    tp.exit_safely()
    assert tp._relay_queue is None
