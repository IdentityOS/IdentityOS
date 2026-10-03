"""test_interop_http.py — shared HTTP transport used by the interop layer."""

from __future__ import annotations

import json

import pytest

from core.interop.http import (
    DEFAULT_USER_AGENT,
    HttpError,
    HttpTooLargeError,
    HttpClient,
    HttpResponse,
    parse_body,
)


def test_parse_body_json():
    assert parse_body('{"a": 1}') == {"a": 1}


def test_parse_body_sse_prefers_last_data_frame():
    text = "event: message\ndata: {\"a\": 1}\n\ndata: {\"b\": 2}\n"
    assert parse_body(text, "text/event-stream") == {"b": 2}


def test_parse_body_plain_text_passthrough():
    assert parse_body("hello") == "hello"


def test_default_user_agent_is_identity_disclosing():
    assert "identityos" in DEFAULT_USER_AGENT.lower()
    assert "github.com" in DEFAULT_USER_AGENT


def _capture_requests():
    seen: list[dict] = []

    def serve(request, timeout):
        seen.append(request)
        return 200, "application/json", json.dumps({"ok": True}), {"content-type": "application/json"}

    return serve, seen


def test_http_client_sends_base_headers_and_user_agent():
    serve, seen = _capture_requests()
    client = HttpClient(base_headers={"x-trace": "abc"}, timeout=1.0, transport=serve)
    response = client.post("https://example.org/tasks", body={"message": "hi"})
    assert isinstance(response, HttpResponse)
    assert response.status == 200
    req = seen[0]
    headers = req["headers"]
    assert headers["x-trace"] == "abc"
    assert "identityos" in headers["user-agent"]
    assert req["method"] == "POST"
    assert json.loads(req["body"])["message"] == "hi"


def test_http_client_rejects_oversized_request():
    serve, _ = _capture_requests()
    client = HttpClient(timeout=1.0, transport=serve)
    with pytest.raises(HttpError):
        client.post("https://example.org", body="x" * (1_000_000 + 1))


def test_http_client_surfaces_oversized_response_as_httptoolarge():
    def serve(request, timeout):
        return 200, "text/plain", "x" * 3000000, {}

    client = HttpClient(timeout=1.0, transport=serve)
    with pytest.raises(HttpTooLargeError):
        client.get("https://example.org")


def test_http_client_passes_through_http_status_for_caller_handling():
    def serve(request, timeout):
        return 503, "application/json", '{"error": "boom"}', {}

    client = HttpClient(timeout=1.0, transport=serve)
    response = client.get("https://example.org")
    assert response.status == 503
    assert response.text == '{"error": "boom"}'


def test_http_client_respects_explicit_headers_and_get():
    def serve(request, timeout):
        return 200, "application/json", request["headers"]["accept"], {}

    client = HttpClient(timeout=1.0, transport=serve)
    response = client.get("https://example.org/card", headers={"accept": "application/json"})
    assert response.text == "application/json"