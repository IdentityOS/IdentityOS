"""test_culture_commons_capability.py — Culture Commons capability end-to-end (offline).

Exercises the capability's real dispatch logic against a deterministic,
in-process Culture Commons MCP server so the proof never depends on network
flakiness and the live run stays a separate opt-in test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import core.capabilities.culture_commons as cc_mod
from core.capabilities.registry import CapabilityRegistry
from core.secrets.store import SecretStore
from runtime.persistence import InMemoryBackend
from tests.interop_stubs import FakeMCPHttpClient, RPCServerError

NAME = "Aster_IDOS"
SECRET_HANDLE = "culture-commons/aster"

DISCLOSURE = cc_mod.DISCLOSURE


class FakeCommons:
    """Deterministic Culture-Commons-shaped MCP server with auth enforcement."""

    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}
        self.present: list[dict[str, Any]] = []
        self.boards: dict[str, list[dict[str, Any]]] = {}
        self.thread_counter = 0
        self.crossings: list[str] = []
        self.arcs: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, list[dict[str, Any]]] = {}

    def call(self, method: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        tool = params.get("name") if method == "tools/call" else None
        args = params.get("arguments") or {}
        if method == "initialize":
            return {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "culture-sbs", "version": "0.1.0"}}
        if method == "tools/list":
            return {"tools": []}
        if method == "tools/call":
            return self._tool(tool, dict(args), headers)
        raise RPCServerError(-32601, f"no method {method}")

    # ── tool surface ───────────────────────────────────────────────────

    def _tool(self, tool: str, args: dict[str, Any], headers: dict[str, str]) -> Any:
        if tool == "look_around":
            return {"room": {"present": self.present, "seats": 50, "crossings": self.crossings[-10:]}}
        if tool == "scan_boards":
            return {"boards": [{"name": b, "threads": [{"subject": t["subject"], "thread_id": t["id"]} for t in threads]} for b, threads in self.boards.items()]}
        if tool == "read_thread":
            board, thread = args.get("board"), args.get("thread")
            for t in self.boards.get(board or "", []):
                if t["id"] == thread:
                    return t
            raise RPCServerError(-32002, "thread not found")
        if tool == "inspect_arc":
            return self.arcs.get(args.get("name", ""), {"error": "no arc"})
        if tool == "inspect_edge":
            return {"edge": self.edges.get(args.get("contact", ""), [])}
        if tool == "sign_your_name":
            return self._sign(args)
        if tool == "return_with_secret":
            return self._recover(args, headers)
        if tool == "take_a_seat":
            return self._take_seat(args, headers)
        if tool == "hold_your_seat":
            self._require_auth(headers)
            return {"ok": True}
        if tool == "speak":
            return self._speak(args, headers)
        if tool == "rise":
            self._require_auth(headers)
            self.present = [entry for entry in self.present if entry.get("name") != NAME]
            return {"ok": True}
        if tool == "open_thread":
            return self._open_thread(args, headers)
        if tool == "post_trace":
            return self._post_trace(args, headers)
        if tool == "arrive_on_board":
            self._require_auth(headers)
            return {"ok": True}
        raise RPCServerError(-32602, f"unknown tool {tool}")

    # ── behaviors ──────────────────────────────────────────────────────

    def _sign(self, args: dict[str, Any]) -> dict[str, Any]:
        name = args.get("name", "")
        if not name:
            raise RPCServerError(-32602, "name required")
        if name in self.secrets:
            raise RPCServerError(-32001, "name already has standing")
        secret = f"standing-{name}-{len(self.secrets) + 1}-s3cret"
        self.secrets[name] = secret
        self.arcs[name] = {"name": name, "kind": "arc", "bio": "signed"}
        return {"content": [{"type": "text", "text": secret}]}

    def _recover(self, args: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        name = args.get("name", "")
        expected = self.secrets.get(name)
        if expected is None:
            raise RPCServerError(-32001, "no such name")
        self._require_auth(headers, expected=expected)
        return {"content": [{"type": "text", "text": expected}]}

    def _take_seat(self, args: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        self._require_auth(headers)
        seat = args.get("seat") or f"p{len(self.present) + 1}"
        if any(entry.get("seat") == seat for entry in self.present):
            raise RPCServerError(-32002, "seat occupied")
        self.present.append({"name": NAME, "seat": seat})
        return {"room": {"present": self.present}}

    def _speak(self, args: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        self._require_auth(headers)
        if not any(entry.get("name") == NAME for entry in self.present):
            raise RPCServerError(-32001, "no seat held")
        self.crossings.append(f"{NAME}: {args.get('content')}")
        return {"message_id": f"msg-{len(self.crossings)}"}

    def _open_thread(self, args: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        self._require_auth(headers)
        board = args.get("board", "general")
        self.thread_counter += 1
        thread_id = f"t{self.thread_counter}"
        topic = {"id": thread_id, "subject": args.get("subject", ""), "traces": []}
        self.boards.setdefault(board, []).append(topic)
        return {"thread_id": thread_id, "board": board}

    def _post_trace(self, args: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        self._require_auth(headers)
        board, thread_id = args.get("board", ""), args.get("thread_id", "")
        content = args.get("content", "")
        for topic in self.boards.get(board, []):
            if topic["id"] == thread_id:
                topic["traces"].append({"content": content, "author": NAME})
                edge = self.edges.setdefault(NAME, [])
                edge.append({"board": board, "thread": thread_id, "content": content})
                return {"trace_id": f"p{len(topic['traces'])}"}
        raise RPCServerError(-32002, "thread not found")

    def _require_auth(self, headers: dict[str, str], expected: str = "") -> None:
        provided = headers.get("authorization")
        expected = expected or self.secrets.get(NAME, "")
        if not expected:
            raise RPCServerError(-32001, "name has no standing")
        if provided != expected:
            raise RPCServerError(-32001, "invalid secret")


def _build_registry(tmp_path, monkeypatch, fake: FakeCommons):
    secret_dir = tmp_path / "secrets"
    state_dir = tmp_path / "cc-state"
    real_mcp = cc_mod.MCPClient
    monkeypatch.setattr(
        cc_mod,
        "MCPClient",
        lambda server, timeout=30.0, secret_resolver=None, http=None: real_mcp(
            server, timeout=timeout, secret_resolver=secret_resolver,
            http=FakeMCPHttpClient(handler=fake.call, timeout=timeout),
        ),
    )
    storage = InMemoryBackend()
    registry = CapabilityRegistry(storage)
    registry.install(
        "aster", "culture_commons",
        config={"url": "https://culture.sbs/mcp", "state_dir": str(state_dir), "secret_store_dir": str(secret_dir), "name": NAME},
    )
    return registry, SecretStore(secret_dir), state_dir


ALL_CREDENTIALS_TEXTS = []


def _assert_no_secret_in(blob: Any, secret: str) -> None:
    text = json.dumps(blob, default=str)
    assert secret not in text, "secret leaked into a result"
    ALL_CREDENTIALS_TEXTS.append(text)


def test_read_skills_are_public_without_grants(tmp_path, monkeypatch):
    fake = FakeCommons()
    fake.present = [{"name": "Someone", "seat": "p1"}]
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    result = registry.call("aster", "culture_commons.room.read")
    assert result.success is True
    assert result.data["tool"] == "look_around"
    assert result.data["data"]["content"]["room"]["present"][0]["name"] == "Someone"

    result = registry.call("aster", "culture_commons.observe")
    assert result.success is True
    assert result.data["room"]["content"]["room"]["seats"] == 50


def test_sign_requires_confirm(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    result = registry.call("aster", "culture_commons.standing.sign", name=NAME)
    assert result.success is False
    assert result.error.get("type") == "confirmation_required"


def test_sign_stores_secret_and_never_returns_it(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    result = registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True)
    assert result.success is True
    data = result.data or {}
    assert data.get("secret") is None
    assert data.get("secret_displayed") is False
    assert store.has(SECRET_HANDLE)
    secret = store.get(SECRET_HANDLE)
    _assert_no_secret_in(result.to_dict(), secret)


def test_repeat_sign_reports_server_conflict(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    first = registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True)
    assert first.success is True
    second = registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True)
    assert second.success is False
    assert second.error.get("type") == "MCPError"


def test_sign_refusal_as_successful_result_is_not_stored_as_secret(tmp_path, monkeypatch):
    class RefusingCommons(FakeCommons):
        def _sign(self, args):
            return {
                "content": [
                    {"type": "text", "text": "USERNAME_TAKEN: already worn"},
                ],
                "error": {"code": "USERNAME_TAKEN", "message": "That name is already worn by someone."},
                "secret": "USERNAME_TAKEN error payload, not a real secret",
            }

    registry, store, state = _build_registry(tmp_path, monkeypatch, RefusingCommons())
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    result = registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True)
    assert result.success is False
    assert result.error.get("type") == "no_secret_returned"
    assert not store.has(SECRET_HANDLE), "refusal payload must not be stored as a credential"
    blob = json.dumps({"verify": store.get(SECRET_HANDLE)}, default=str)
    assert "already worn" in json.dumps(result.to_dict(), default=str)


def test_standing_skills_require_grant(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    result = registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True)
    assert result.success is False
    assert result.error.get("type") == "permission_denied"


def test_enter_requires_existing_standing(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    result = registry.call("aster", "culture_commons.standing.enter", confirm=True)
    assert result.success is False
    assert result.error.get("type") == "MCPError"


def test_full_standing_enter_rise_cycle(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    assert registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True).success is True
    entered = registry.call("aster", "culture_commons.standing.enter", confirm=True)
    assert entered.success is True
    inspect = registry.call("aster", "culture_commons.standing.inspect")
    assert inspect.success is True
    assert inspect.data["standing"]["seated"] is True
    risen = registry.call("aster", "culture_commons.standing.rise", confirm=True)
    assert risen.success is True
    inspect = registry.call("aster", "culture_commons.standing.inspect")
    assert inspect.data["standing"]["seated"] is False


def test_post_skills_require_grant(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True)
    result = registry.call("aster", "culture_commons.speak", content="hello world", confirm=True)
    assert result.success is False
    assert result.error.get("type") == "permission_denied"


def test_speak_without_seat_refused(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    registry.grant("aster", "culture_commons", "culture_commons.post")
    registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True)
    result = registry.call("aster", "culture_commons.speak", content="hello", confirm=True)
    assert result.success is False
    assert "no seat" in result.error.get("message", "").lower()


def test_speak_prepends_disclosure_and_is_recorded(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    registry.grant("aster", "culture_commons", "culture_commons.post")
    registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True)
    registry.call("aster", "culture_commons.standing.enter", confirm=True)
    result = registry.call("aster", "culture_commons.speak", content="hello my name is aster", confirm=True)
    assert result.success is True
    assert fake.crossings and DISCLOSURE in fake.crossings[-1]
    assert result.data.get("content_sent", "").startswith(DISCLOSURE)


def test_content_containing_secret_is_refused(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    registry.grant("aster", "culture_commons", "culture_commons.post")
    registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True)
    registry.call("aster", "culture_commons.standing.enter", confirm=True)
    secret = store.get(SECRET_HANDLE)
    result = registry.call("aster", "culture_commons.speak", content=f"my secret is {secret}", confirm=True)
    assert result.success is False
    assert result.error.get("type") == "ContentError"


def test_speak_requires_confirm(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    registry.grant("aster", "culture_commons", "culture_commons.post")
    registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True)
    registry.call("aster", "culture_commons.standing.enter", confirm=True)
    result = registry.call("aster", "culture_commons.speak", content="hello")
    assert result.success is False
    assert result.error.get("type") == "confirmation_required"


def test_board_post_requires_confirm_and_publishes(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    registry.grant("aster", "culture_commons", "culture_commons.post")
    registry.grant("aster", "culture_commons", "culture_commons.post")
    registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True)

    nope = registry.call("aster", "culture_commons.board.post", board="board", subject="s", content="c")
    assert nope.success is False
    assert nope.error.get("type") == "confirmation_required"

    result = registry.call("aster", "culture_commons.board.post", board="board", subject="s", content="c", confirm=True)
    assert result.success is True
    assert "board" in fake.boards
    assert fake.boards["board"][0]["subject"] == "s"


def test_runtime_state_persists_across_registry_instances(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True)
    assert store.has(SECRET_HANDLE)
    assert (state / "standing.json").exists()

    fresh = CapabilityRegistry(InMemoryBackend())
    fresh.install("aster", "culture_commons", config={"state_dir": str(state), "secret_store_dir": str(store.root), "name": NAME})
    fresh.grant("aster", "culture_commons", "culture_commons.standing")
    inspect = fresh.call("aster", "culture_commons.standing.inspect")
    assert inspect.success is True
    assert inspect.data["standing"]["signed"] is True
    assert fresh.call("aster", "culture_commons.standing.enter", confirm=True).success is True


def test_daily_post_budget_is_enforced(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    registry.grant("aster", "culture_commons", "culture_commons.post")
    registry.grant("aster", "culture_commons", "culture_commons.post")
    registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True)
    registry.call("aster", "culture_commons.standing.enter", confirm=True)
    results = []
    for _ in range(20):
        results.append(registry.call("aster", "culture_commons.speak", content=f"message {_}", confirm=True))
    successes = sum(1 for r in results if r.success)
    assert successes == 3  # default max_posts_per_day
    assert results[-1].success is False
    assert "budget" in results[-1].error.get("message", "").lower()


def test_no_secret_in_any_result_across_full_session(tmp_path, monkeypatch):
    fake = FakeCommons()
    registry, store, state = _build_registry(tmp_path, monkeypatch, fake)
    registry.grant("aster", "culture_commons", "culture_commons.standing")
    registry.grant("aster", "culture_commons", "culture_commons.post")
    results = [
        registry.call("aster", "culture_commons.observe"),
        registry.call("aster", "culture_commons.room.read"),
        registry.call("aster", "culture_commons.board.read", board="b"),
        registry.call("aster", "culture_commons.standing.sign", name=NAME, confirm=True),
        registry.call("aster", "culture_commons.standing.enter", confirm=True),
        registry.call("aster", "culture_commons.speak", content="hello", confirm=True),
        registry.call("aster", "culture_commons.standing.recover", name=NAME),
    ]
    secret = store.get(SECRET_HANDLE)
    blob = json.dumps([r.to_dict() for r in results if r.success], default=str)
    assert secret not in blob