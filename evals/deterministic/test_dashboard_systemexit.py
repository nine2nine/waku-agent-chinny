"""DETERMINISTIC EVAL — a missing API key must not kill the request thread.

get_client() raises SystemExit (not an Exception subclass) when no provider
key is configured. Before this fix, dashboard.py's `except Exception` let
SystemExit escape past the POST dispatch, killing the request thread mid-
response — the browser got an empty reply (curl: "(52) Empty reply from
server") instead of the helpful "No API key for provider 'anthropic'..."
message, and the dead thread meant the NEXT request on that connection could
hang too.

This drives the REAL Handler over a real loopback socket (no mocked HTTP
layer) so the regression can't come back without tripping a live
request/response cycle. No LLM or network calls beyond localhost.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from waku.ops import dashboard

MISSING_KEY_MESSAGE = (
    "No API key for provider 'anthropic'. Set ANTHROPIC_API_KEY in .env (see .env.example)."
)


def _raise_missing_key(message: str) -> dict:  # matches dashboard.chat's signature
    raise SystemExit(MISSING_KEY_MESSAGE)


@pytest.fixture
def running_server(monkeypatch):
    """The real Handler on an ephemeral port, with chat() swapped for a stand-in
    that raises SystemExit exactly like get_client() does with no key set."""
    monkeypatch.setattr(dashboard, "chat", _raise_missing_key)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        thread.join(timeout=5)


def _post(port: int, path: str, payload: dict):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def test_missing_api_key_surfaces_as_json_error_not_a_dead_connection(running_server):
    port = running_server.server_address[1]
    status, body = _post(port, "/api/chat", {"message": "hi"})

    assert status == 200
    assert isinstance(body, dict)
    assert MISSING_KEY_MESSAGE in body.get("error", "")


def test_the_server_thread_survives_to_answer_a_second_request(running_server):
    """The bug wasn't just a bad response — SystemExit propagating out of the
    handler killed the thread handling the request. A server that only limps
    through ONE request after the fix would still be broken."""
    port = running_server.server_address[1]
    _post(port, "/api/chat", {"message": "first"})

    status, body = _post(port, "/api/chat", {"message": "second"})
    assert status == 200
    assert MISSING_KEY_MESSAGE in body.get("error", "")
