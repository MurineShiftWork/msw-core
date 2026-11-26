"""Trial relay: the rig-side process that forwards session events to the log server.

One process per session, ``daemon=True`` so it dies with the task process:
  1. ``run()`` POSTs the session start payload to ``/ingest/{setup}/start``.
  2. It then reads the queue; each item is an opaque trial event the task put with
     ``put_nowait()`` (see ``TaskRunner.emit_trial``) and is POSTed to ``/ingest/{setup}/trial``
     as ``{"trial_data": event}``.
  3. It stops on ``None`` (put by ``TaskProcess.exit_safely``) or on a dict with
     ``__stop__=True`` (a task sentinel carrying a summary), after POSTing ``/ingest/{setup}/stop``.

Fire and forget: the task only ever does ``put_nowait()`` on a bounded queue, so a slow or down
server never blocks a trial. Every POST has a 0.5 s timeout; a failure is logged at DEBUG and the
event is dropped, never retried. The session UUID, when set, is merged into every payload, and the
bearer token, when set, is sent as ``Authorization: Bearer``.

Standard library only, so the rig needs nothing beyond this package to relay. The server side
(ingest endpoints and dashboard) is a separate install.

Configured in the machine config as ``logagent_api_url`` and ``logagent_api_token`` (see
``machine_config.read_log_config``).
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import signal
import urllib.request
from datetime import UTC, datetime

#: POST timeout per request, seconds. Short so the relay keeps up with the trial rate.
POST_TIMEOUT_S = 0.5


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class TrialRelay(multiprocessing.Process):
    def __init__(
        self,
        queue: multiprocessing.Queue,
        log_url: str,
        setup: str,
        session_start_payload: dict,
        session_uuid: str = "",
        bearer_token: str = "",
    ) -> None:
        super().__init__(daemon=True)
        self._queue = queue
        self._log_url = log_url.rstrip("/")
        self._setup = setup
        self._session_uuid = session_uuid
        self._bearer_token = bearer_token
        self._session_start_payload = session_start_payload

    def run(self) -> None:
        # Ctrl+C reaches every process in the console; the parent stops this one.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        self._post("start", self._session_start_payload)
        while True:
            try:
                event = self._queue.get(block=True, timeout=5)
            except Exception:
                continue
            if event is None:
                self._post("stop", {"ended_at": _now_iso()})
                break
            if isinstance(event, dict) and event.get("__stop__"):
                summary = dict(event.get("summary", {}))
                summary["ended_at"] = _now_iso()
                self._post("stop", summary)
                break
            self._post("trial", {"trial_data": event})

    def _post(self, endpoint: str, payload: dict) -> None:
        url = f"{self._log_url}/ingest/{self._setup}/{endpoint}"
        if self._session_uuid:
            payload = {**payload, "session_uuid": self._session_uuid}
        data = json.dumps(payload, default=str).encode()
        headers = {"Content-Type": "application/json"}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            urllib.request.urlopen(req, timeout=POST_TIMEOUT_S)
        except Exception as exc:
            logging.debug("TrialRelay POST failed (%s): %s", endpoint, exc)
