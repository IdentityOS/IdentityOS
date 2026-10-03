"""End-to-end HTTP test of the Gmail push endpoint.

Exercises the real socket, the real request parser, and the real auth check,
because an endpoint that works only when called in-process is not an endpoint.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any, Optional

import pytest

from core.capabilities.email.gmail_push import GmailPushConfig, GmailPushService
from runtime.persistence import JSONFileBackend

TOKEN = "verify-me"
ENV = {
    "ASTER_GMAIL_PUSH_CLIENT_ID": "cid",
    "ASTER_GMAML_PUSH_CLIENT_SECRET": "unused",
    "ASTER_GMAIL_PUSH_CLIENT_SECRET": "sec",
    "ASTER_GMAIL_PUSH_REFRESH_TOKEN": "ref",
    "ASTER_GMAIL_PUSH_TOPIC": "projects/p/topics/t",
    "ASTER_GMAIL_PUSH_VERIFICATION_TOKEN": TOKEN,
}


class _NoopClient:
    def start_watch(self):
        return {"id": "w1", "expiration": "0", "historyId": "100"}

    def stop_watch(self):
        return {}

    def history(self, *_a, **_k):
        return [], "100", False

    def get_message(self, _mid):
        raise AssertionError("not reached")


@pytest.fixture()
def push_server(tmp_path):
    from runtime import health_server

    backend = JSONFileBackend(root_dir=str(tmp_path / "store"))
    service = GmailPushService(
        GmailPushConfig.from_env(ENV), storage=backend, identity_id="aster",
        ingest=lambda items: {"ok": True}, client=_NoopClient(),
    )
    handler = type("H", (health_server._HealthHandler,), {})
    handler.presence_store = None
    handler.gmail_push_service = service
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", service
    finally:
        server.shutdown()
        server.server_close()


def _post(url: str, body: bytes, *, token: Optional[str] = TOKEN) -> tuple[int, dict]:
    if token is not None:
        url = f"{url}?token={token}"
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def _envelope(history_id: str) -> bytes:
    inner = json.dumps({"emailAddress": "a@eagles.oc.edu", "historyId": history_id})
    return json.dumps({"message": {
        "data": base64.b64encode(inner.encode()).decode()}}).encode()


def test_push_endpoint_queues_and_acks(push_server):
    url, service = push_server
    status, payload = _post(f"{url}/api/gmail/push", _envelope("1000"))
    assert status == 200
    assert payload["accepted"] is True
    assert len(service._load_queue()) == 1


def test_push_endpoint_rejects_missing_token(push_server):
    url, service = push_server
    status, _ = _post(f"{url}/api/gmail/push", _envelope("1000"), token=None)
    assert status == 401
    assert service._load_queue() == [], "an unauthorized push must not queue"


def test_push_endpoint_rejects_wrong_token(push_server):
    url, service = push_server
    status, _ = _post(f"{url}/api/gmail/push", _envelope("1000"), token="nope")
    assert status == 401
    assert service._load_queue() == []


def test_push_endpoint_503_when_service_absent(tmp_path):
    from runtime import health_server

    handler = type("H", (health_server._HealthHandler,), {})
    handler.presence_store = None
    handler.gmail_push_service = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    try:
        status, _ = _post(f"http://{host}:{port}/api/gmail/push", _envelope("1"))
        assert status == 503
    finally:
        server.shutdown()
        server.server_close()


def test_push_endpoint_acks_garbage_rather_than_retrying_forever(push_server):
    url, service = push_server
    status, payload = _post(f"{url}/api/gmail/push", b"not-a-pubsub-message")
    assert status == 200
    assert payload["accepted"] is False
    assert service._load_queue() == []


def test_push_endpoint_survives_restart(push_server):
    """A notification accepted before a restart must still be drainable."""
    url, service = push_server
    _post(f"{url}/api/gmail/push", _envelope("1000"))
    reloaded = GmailPushService(GmailPushConfig.from_env(ENV),
                                storage=service.storage, identity_id="aster",
                                ingest=lambda items: None)
    assert [q.history_id for q in reloaded._load_queue()] == ["1000"]


def test_ready_endpoint_reports_store_readable(push_server):
    url, service = push_server
    handler_url = url
    # /ready needs presence_store for _backend(); build a minimal one.
    from runtime import health_server

    class _P:
        def __init__(self, storage):
            self._storage = storage
            self.identity_id = "aster"

    handler = type("H", (health_server._HealthHandler,), {})
    handler.presence_store = _P(service.storage)
    handler.gmail_push_service = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/ready", timeout=10) as r:
            body = json.loads(r.read().decode())
        assert body["ready"] is True
        assert body["identity_id"] == "aster"
    finally:
        server.shutdown()
        server.server_close()
