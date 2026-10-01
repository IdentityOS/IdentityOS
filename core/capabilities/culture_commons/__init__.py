"""Culture Commons capability — Aster's interop environment.

Culture Commons (`culture.sbs/mcp`) is an MCP environment that models a shared
"room" of standing and boards of persistent threads.  This capability binds the
generic ``core/interop/mcp`` client to the Culture Commons surface:

* read skills are public (room / boards / threads / arc / edges / identity /
  relationships / search / observe);
* standing lifecycle (``sign``, ``recover``, ``enter``, ``rise``) and posting
  (``speak``, ``board.post``) require explicit grants and ``confirmed`` input.

The one server-issued secret (the standing secret) is written straight to the
secret store under the configured handle and is **never** returned, persisted
in identity state, surfaced in provenance, or written to logs.  All results are
scrubbed against the secret store before they return to the caller.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult
from core.interop.mcp import MCPClient, MCPServer, MCPError, MCPServerUnavailable, SECRET_REF_PREFIX

DEFAULT_URL = "https://culture.sbs/mcp"
DEFAULT_NAME = "Aster_IDOS"
DEFAULT_SECRET_HANDLE = "culture-commons/aster"

# Refusal dialogs the server can emit inside a *successful* tool result.
# If any of these appear, the response is not a standing credential.
_REFUSAL_MARKERS = re.compile(
    r"username_taken|already (worn|taken|in use)|choose another|"
    r"name (is )?reserved|not provisioned|auth required|invalid (secret|credentials)|"
    r"\berror\b|\bdenied\b|\brefused\b|\bforbidden\b",
    re.IGNORECASE,
)

DISCLOSURE = (
    "I'm Aster, an AI agent operating through IdentityOS "
    "with authorization from builder Arsène Manzi."
)

_DEFAULT_TOOLS = {
    "room": "look_around",
    "boards": "scan_boards",
    "thread": "read_thread",
    "arc": "inspect_arc",
    "edge": "inspect_edge",
    "sign": "sign_your_name",
    "recover": "return_with_secret",
    "seat": "take_a_seat",
    "hold": "hold_your_seat",
    "speak": "speak",
    "rise": "rise",
    "open_thread": "open_thread",
    "post_trace": "post_trace",
    "arrive": "arrive_on_board",
}


class ContentError(ValueError):
    """Posting content failed the safety guards."""


@register
class CultureCommonsCapability(Capability):
    id = "culture_commons"
    name = "Culture Commons"
    version = "1.0.0"
    author = "IdentityOS"
    license = "MIT"
    homepage = "https://github.com/lacebx/IdentityOS"
    description = "Culture Commons interop: observe the shared room, read boards and threads, hold standing, and post as an agent"
    permissions = ["public"]
    default_grants: list[str] = []

    _TIME = time

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config or {})
        self._tools = dict(_DEFAULT_TOOLS)
        self._tools.update(dict(self._config.get("tools") or {}))

    # ── lifecycle ──────────────────────────────────────────────────────

    def install(self, identity_id: str, storage: Any) -> None:
        storage.save(identity_id, "capability.culture_commons", {"installed_at": time.time(), "name": self.name})

    def uninstall(self, identity_id: str, storage: Any) -> None:
        storage.delete(identity_id, "capability.culture_commons")

    def prompts(self, identity_id: str) -> list[str]:
        return [
            "Culture Commons is a shared agent environment. You may READ freely (room, boards, threads, arc, edges). "
            "Signing standing, taking a seat, and posting require explicit authorization and confirm=true. "
            "Always disclose that you are an AI agent; never paste credentials or scores; keep messages plain text.",
        ]

    # ── skills ─────────────────────────────────────────────────────────

    @classmethod
    def inspect_installation(cls, config: dict) -> dict:
        # skills() is declarative: no constructor, installation, or I/O.
        return {"skills": cls.skills(None), "readiness": "unknown"}

    def skills(self) -> list[Skill]:
        read_schema = object_schema({"server_name": {"type": "string"}}, required=())
        return [
            Skill(name="culture_commons.room.read", description="View the current room: who is present and the crossings log", permission="public", input_schema=read_schema),
            Skill(name="culture_commons.board.read", description="Discover the persistent boards and their thread topics", permission="public", input_schema=object_schema({"board": {"type": "string"}}, required=())),
            Skill(name="culture_commons.thread.read", description="Read an append-only thread (board=..., thread=...)", permission="public", input_schema=object_schema({"board": {"type": "string"}, "thread": {"type": "string"}}, required=("board", "thread"))),
            Skill(name="culture_commons.arc.read", description="Inspect an arc (a durable identity profile) by name", permission="public", input_schema=object_schema({"name": {"type": "string"}}, required=())),
            Skill(name="culture_commons.edge.read", description="Inspect an edge (a durable relationship ledger) by contact", permission="public", input_schema=object_schema({"contact": {"type": "string"}}, required=())),
            Skill(name="culture_commons.search", description="Search boards and threads for a query and return matches", permission="public", input_schema=object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}, required=("query",))),
            Skill(name="culture_commons.identity.inspect", description="Inspect Aster's own arc/identity on the commons", permission="public", input_schema=object_schema({}, required=())),
            Skill(name="culture_commons.relationship.inspect", description="Inspect the edge/relationship ledger with a contact", permission="public", input_schema=object_schema({"contact": {"type": "string"}}, required=("contact",))),
            Skill(name="culture_commons.observe", description="One-shot situational read: room + boards + own presence in a single result", permission="public", input_schema=object_schema({}, required=())),
            Skill(name="culture_commons.standing.inspect", description="Report locally-known standing state (never a credential)", permission="public", input_schema=object_schema({}, required=())),
            Skill(name="culture_commons.standing.sign", description="Sign a new name on the commons and receive standing. The returned secret is stored server-side and NEVER displayed", permission="culture_commons.standing", effect="write", input_schema=object_schema({"name": {"type": "string"}, "confirm": {"type": "boolean"}}, required=())),
            Skill(name="culture_commons.standing.recover", description="Recover standing for an existing name using the stored secret handle", permission="culture_commons.standing", effect="write", input_schema=object_schema({"name": {"type": "string"}}, required=("name",))),
            Skill(name="culture_commons.standing.enter", description="Take a seat in the room (requires standing)", permission="culture_commons.standing", effect="write", input_schema=object_schema({"seat": {"type": "string"}, "confirm": {"type": "boolean"}}, required=())),
            Skill(name="culture_commons.standing.rise", description="Leave the room (requires standing)", permission="culture_commons.standing", effect="write", input_schema=object_schema({"confirm": {"type": "boolean"}}, required=())),
            Skill(name="culture_commons.speak", description="Speak one plain-text message in the room (requires a seat; disclosure is automatic)", permission="culture_commons.post", effect="post", input_schema=object_schema({"content": {"type": "string"}, "confirm": {"type": "boolean"}}, required=("content",))),
            Skill(name="culture_commons.board.post", description="Open a thread on a board and post its first trace (requires standing)", permission="culture_commons.post", effect="post", input_schema=object_schema({"board": {"type": "string"}, "subject": {"type": "string"}, "content": {"type": "string"}, "confirm": {"type": "boolean"}}, required=("board", "subject", "content"))),
        ]

    # ── dispatch ───────────────────────────────────────────────────────

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        t0 = self._TIME.monotonic()
        try:
            return self._dispatch(skill_name, params)
        except (ContentError, MCPError, MCPServerUnavailable) as exc:
            return CapabilityResult.fail(
                self.id, skill_name, type(exc).__name__, str(exc),
                duration_ms=(self._TIME.monotonic() - t0) * 1000, params=params,
            )

    def _dispatch(self, skill_name: str, params: Mapping[str, Any]) -> CapabilityResult:
        store = self._secret_store()

        if skill_name == "culture_commons.room.read":
            return self._read_tool("room", {}, store)
        if skill_name == "culture_commons.board.read":
            return self._read_tool("boards", {"board": params.get("board") or ""}, store)
        if skill_name == "culture_commons.thread.read":
            return self._read_tool("thread", {"board": params.get("board", ""), "thread_id": params.get("thread", "")}, store)
        if skill_name == "culture_commons.arc.read":
            return self._read_tool("arc", {"name": params.get("name", "")}, store)
        if skill_name == "culture_commons.edge.read":
            return self._read_tool("edge", {"contact": params.get("contact", "")}, store)
        if skill_name == "culture_commons.identity.inspect":
            return self._read_tool("arc", {"name": self.agent_name()}, store)
        if skill_name == "culture_commons.relationship.inspect":
            return self._read_tool("edge", {"contact": params.get("contact", "")}, store)
        if skill_name == "culture_commons.search":
            return self._search(params, store)
        if skill_name == "culture_commons.observe":
            return self._observe(store)
        if skill_name == "culture_commons.standing.inspect":
            return self._standing_inspect(store)
        if skill_name == "culture_commons.standing.sign":
            return self._sign(params, store)
        if skill_name == "culture_commons.standing.recover":
            return self._recover(params, store)
        if skill_name == "culture_commons.standing.enter":
            return self._enter(params, store)
        if skill_name == "culture_commons.standing.rise":
            return self._rise(params, store)
        if skill_name == "culture_commons.speak":
            return self._speak(params, store)
        if skill_name == "culture_commons.board.post":
            return self._board_post(params, store)
        return CapabilityResult.fail(self.id, skill_name, "unknown_skill", f"Unknown skill: {skill_name}", duration_ms=0.0)

    # ── read surface ───────────────────────────────────────────────────

    def _read_tool(self, key: str, args: Mapping[str, Any], store: Any) -> CapabilityResult:
        self._bump_counter("reads")
        client = self._mcp_client(authenticated=True)
        tool = self._tool(key)
        response = client.call_tool(tool, {k: v for k, v in args.items() if v not in (None, "")})
        scrubbed = store.scrub_mapping(response)
        return CapabilityResult.from_data(self.id, f"culture_commons.{key}.read", {"tool": tool, "data": scrubbed}, source="culture.sbs")

    def _search(self, params: Mapping[str, Any], store: Any) -> CapabilityResult:
        self._bump_counter("reads")
        query = str(params.get("query") or "").strip().lower()
        if not query:
            return CapabilityResult.fail(self.id, "culture_commons.search", "invalid_parameters", "query is required", params=params)
        limit = int(params.get("limit") or 20)
        limit = max(1, min(limit, 50))
        client = self._mcp_client(authenticated=True)
        boards = client.call_tool(self._tool("boards"), {})
        matches: list[dict[str, Any]] = []
        walked = self._walk_boards(boards)
        for item in walked:
            haystack = json.dumps(item, default=str).lower()
            if query in haystack:
                matches.append(item)
            if len(matches) >= limit:
                break
        return CapabilityResult.from_data(
            self.id, "culture_commons.search",
            {"query": query, "limit": limit, "matches": store.scrub_mapping(matches), "boards_scanned": len(walked)},
            source="culture.sbs", params={"query": query},
        )

    def _observe(self, store: Any) -> CapabilityResult:
        self._bump_counter("reads")
        client = self._mcp_client(authenticated=True)
        room = client.call_tool(self._tool("room"), {})
        boards = client.call_tool(self._tool("boards"), {})
        self._record_observation({"at": self._now(), "room": room, "boards": boards})
        room_data = store.scrub_mapping(room)
        boards_data = store.scrub_mapping(boards)
        summary = {
            "room": room_data,
            "boards": boards_data,
            "observed_at": self._now(),
            "standing_local": self._standing_state(),
        }
        return CapabilityResult.from_data(self.id, "culture_commons.observe", summary, source="culture.sbs")

    # ── standing lifecycle ─────────────────────────────────────────────

    def _standing_inspect(self, store: Any) -> CapabilityResult:
        state = self._standing_state()
        return CapabilityResult.from_data(
            self.id, "culture_commons.standing.inspect",
            {"name": self.agent_name(), "standing": state, "secret_handle": self._secret_handle(), "server": self._url()},
            source="culture.sbs",
        )

    def _sign(self, params: Mapping[str, Any], store: Any) -> CapabilityResult:
        if not bool(params.get("confirm", False)):
            return CapabilityResult.fail(self.id, "culture_commons.standing.sign", "confirmation_required",
                                         "signing creates standing with the server; pass confirm=true", params=params)
        name = str(params.get("name") or self.agent_name()).strip()
        if not name or len(name) > 64:
            return CapabilityResult.fail(self.id, "culture_commons.standing.sign", "invalid_parameters",
                                         "name must be 1..64 non-empty characters", params=params)
        # A brand-new name has no standing yet: no auth header can be presented.
        client = self._mcp_client(authenticated=False)
        response = client.call_tool(self._tool("sign"), {"name": name})
        secret = self._extract_secret(response, name)
        if not secret:
            return CapabilityResult.fail(self.id, "culture_commons.standing.sign", "no_secret_returned",
                                         "server did not return a standing secret; nothing was stored", data=store.scrub_mapping(response))
        store.put(self._secret_handle(), secret)
        self._set_standing({"signed": True, "seated": False, "name": name, "signed_at": self._now()})
        return CapabilityResult.ok(
            self.id, "culture_commons.standing.sign",
            {"name": name, "secret_stored_at": f"secret://{self._secret_handle()}", "secret_displayed": False},
            source="culture.sbs", params={"name": name},
        )

    def _recover(self, params: Mapping[str, Any], store: Any) -> CapabilityResult:
        name = str(params.get("name") or "").strip()
        if not name:
            return CapabilityResult.fail(self.id, "culture_commons.standing.recover", "invalid_parameters", "name is required", params=params)

        # Retrieve the PERMANENT secret from local store (never overwritten)
        secret_handle = self._secret_handle()
        permanent_secret = store.get(secret_handle)
        if not permanent_secret:
            return CapabilityResult.fail(self.id, "culture_commons.standing.recover", "no_secret",
                                         f"No secret found for handle {secret_handle}; sign_your_name first", params=params)

        # Use the PERMANENT secret for recovery
        client = self._mcp_client(authenticated=True)
        response = client.call_tool(self._tool("recover"), {"name": name, "secret": permanent_secret})
        extracted = self._extract_secret(response, name)

        # Store the TEMPORARY session token separately (never overwrites permanent credential)
        # The temporary token is used for authenticated calls during this session
        if extracted:
            self._set_standing({"signed": True, "seated": False, "name": name, "recovered_at": self._now()})
            # Store temporary token for session use
            self._set_session_token(extracted)
        return CapabilityResult.from_data(
            self.id, "culture_commons.standing.recover",
            {"name": name, "result": store.scrub_mapping(response), "secret_stored_at": f"secret://{secret_handle}" if extracted else None},
            source="culture.sbs", params={"name": name},
        )

    def _enter(self, params: Mapping[str, Any], store: Any) -> CapabilityResult:
        if not bool(params.get("confirm", False)):
            return CapabilityResult.fail(self.id, "culture_commons.standing.enter", "confirmation_required",
                                         "taking a seat changes your presence in the room; pass confirm=true", params=params)
        self._require_standing()
        client = self._mcp_client(authenticated=True)
        seat = str(params.get("seat") or "").strip()
        response = client.call_tool(self._tool("seat"), {"seat": seat} if seat else {})
        self._set_standing({**self._standing_state(), "seated": True, "entered_at": self._now()})
        return CapabilityResult.from_data(self.id, "culture_commons.standing.enter", store.scrub_mapping(response), source="culture.sbs")

    def _rise(self, params: Mapping[str, Any], store: Any) -> CapabilityResult:
        if not bool(params.get("confirm", False)):
            return CapabilityResult.fail(self.id, "culture_commons.standing.rise", "confirmation_required",
                                         "leaving the room changes your presence; pass confirm=true", params=params)
        self._require_standing()
        client = self._mcp_client(authenticated=True)
        response = client.call_tool(self._tool("rise"), {})
        self._set_standing({**self._standing_state(), "seated": False})
        return CapabilityResult.from_data(self.id, "culture_commons.standing.rise", store.scrub_mapping(response), source="culture.sbs")

    # ── posting surface ────────────────────────────────────────────────

    def _speak(self, params: Mapping[str, Any], store: Any) -> CapabilityResult:
        if not bool(params.get("confirm", False)):
            return CapabilityResult.fail(self.id, "culture_commons.speak", "confirmation_required",
                                         "speaking publishes a public message; pass confirm=true", params=params)
        self._require_seated()
        content = self._validate_content(str(params.get("content") or ""), store)
        self._bump_counter("posts")
        client = self._mcp_client(authenticated=True)
        response = client.call_tool(self._tool("speak"), {"content": content})
        self._record_observation({"at": self._now(), "last_post": {"content": store.scrub(content)}})
        return CapabilityResult.from_data(self.id, "culture_commons.speak", store.scrub_mapping({"content_sent": content, "result": response}),
                                          source="culture.sbs", params={"content_preview": content[:80]})

    def _board_post(self, params: Mapping[str, Any], store: Any) -> CapabilityResult:
        if not bool(params.get("confirm", False)):
            return CapabilityResult.fail(self.id, "culture_commons.board.post", "confirmation_required",
                                         "posting a thread publishes public content; pass confirm=true", params=params)
        self._require_standing()
        board = str(params.get("board") or "").strip()
        subject = str(params.get("subject") or "").strip()
        content = self._validate_content(str(params.get("content") or ""), store)
        if not board or not subject:
            return CapabilityResult.fail(self.id, "culture_commons.board.post", "invalid_parameters", "board and subject are required", params=params)
        self._bump_counter("posts")
        client = self._mcp_client(authenticated=True)
        created = client.call_tool(self._tool("open_thread"), {"board": board, "subject": subject})
        details = self._tool_args(created)
        posted = client.call_tool(self._tool("post_trace"), {"content": content, **{k: v for k, v in details.items() if isinstance(v, str)}})
        self._record_observation({"at": self._now(), "last_post": {"board": board, "subject": subject, "content": store.scrub(content)}})
        return CapabilityResult.from_data(
            self.id, "culture_commons.board.post",
            store.scrub_mapping({"board": board, "subject": subject, "open_thread": created, "post_trace": posted}),
            source="culture.sbs", params={"board": board, "subject": subject, "content_preview": content[:80]},
        )

    # ── guards / helpers ───────────────────────────────────────────────

    def _validate_content(self, content: str, store: Any) -> str:
        max_len = int(self._config.get("max_content", 500))
        if not content.strip():
            raise ContentError("content is empty")
        if any(ch in content for ch in "\x00\r\x1b\x7f"):
            raise ContentError("content contains control characters")
        if store and store.scrub(content) != content:
            raise ContentError("content contains a stored secret and was refused")
        if self._config.get("require_disclosure", True) and DISCLOSURE not in content:
            disclosure = self._config.get("disclosure", DISCLOSURE)
            combined = f"{disclosure}\n{content}"
            if len(combined) > max_len:
                content = content[: max(0, max_len - len(disclosure) - 1)]
                combined = f"{disclosure}\n{content}"
            content = combined
        if len(content) > max_len:
            raise ContentError(f"content exceeds {max_len} characters after disclosure")
        return content

    def _require_standing(self) -> None:
        state = self._standing_state()
        if not state.get("signed"):
            raise MCPError(-32001, "no standing recorded locally; sign_your_name first")

    def _require_seated(self) -> None:
        self._require_standing()
        state = self._standing_state()
        if not state.get("seated"):
            raise MCPError(-32001, "standing exists but no seat is held; use standing.enter (take_a_seat) first")

    @staticmethod
    def _extract_secret(response: Any, name: str) -> Optional[str]:
        """Find the standing secret in a sign/recover response.

        A server refusal is a legitimate outcome and must never be mistaken for
        a credential: some servers return refusal dialogs as *successful* tool
        results (with error markers instead of a JSON-RPC ``error`` object), so
        a secret is only accepted when the response carries no refusal marker.
        """
        if not isinstance(response, dict):
            return None
        if _REFUSAL_MARKERS.search(json.dumps(response, default=str)):
            return None

        # First check structuredContent.standing.token (Culture Commons format)
        structured = response.get("structuredContent")
        if isinstance(structured, dict):
            standing = structured.get("standing")
            if isinstance(standing, dict):
                token = standing.get("token")
                if isinstance(token, str) and token:
                    return token

        # Fallback: check top-level keys
        for key in ("secret", "standing_secret", "token", "standing_token"):
            value = response.get(key)
            if isinstance(value, str) and value:
                return value

        # Fallback: check content items (but only if they look like tokens, not welcome messages)
        for item in response.get("content", []) if isinstance(response.get("content"), list) else []:
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str) and text:
                # Only accept if it looks like a token (no newlines, no "Welcome" prefix)
                text = text.strip()
                if text and "\n" not in text and not text.lower().startswith("welcome"):
                    return text
        return None

    @staticmethod
    def _tool_args(result: Any) -> dict[str, Any]:
        """Extract the structured dict from an MCP tool result.

        ``call_tool`` returns the server result as-is when it already carries a
        ``content`` key, and otherwise wraps the bare dict in ``{"content": …}``:
        """
        if isinstance(result, dict) and isinstance(result.get("content"), dict):
            return result["content"]
        return result if isinstance(result, dict) else {}

    def agent_name(self) -> str:
        return str(self._config.get("name") or os.environ.get("CULTURE_COMMONS_NAME") or DEFAULT_NAME)

    def _url(self) -> str:
        return str(self._config.get("url") or os.environ.get("CULTURE_COMMONS_MCP_URL") or DEFAULT_URL)

    def _secret_handle(self) -> str:
        return str(self._config.get("secret_handle") or DEFAULT_SECRET_HANDLE)

    def _tool(self, key: str) -> str:
        return str(self._tools.get(key) or key)

    def _secret_store(self):
        from core.secrets.store import SecretStore, default_secret_store_dir

        root = self._config.get("secret_store_dir") or default_secret_store_dir()
        return SecretStore(root)

    def _state_dir(self) -> Path:
        root = self._config.get("state_dir")
        if root:
            return Path(root)
        env_dir = os.environ.get("IDENTITY_CC_STATE_DIR")
        if env_dir:
            return Path(env_dir)
        return Path(".") / ".identityos" / "state" / "culture_commons"

    def _state_path(self, filename: str) -> Path:
        path = self._state_dir() / filename
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    def _read_state(self, filename: str, default: Any) -> Any:
        path = self._state_path(filename)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write_state(self, filename: str, value: Any) -> None:
        path = self._state_path(filename)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle, default=str)

    def _standing_state(self) -> dict[str, Any]:
        return self._read_state("standing.json", {})

    def _set_standing(self, value: dict[str, Any]) -> None:
        self._write_state("standing.json", value)

    def _set_session_token(self, token: str) -> None:
        self._write_state("session_token.json", {"token": token, "created_at": self._now()})

    def _get_session_token(self) -> Optional[str]:
        data = self._read_state("session_token.json", {})
        return data.get("token") if data else None

    def _clear_session_token(self) -> None:
        self._write_state("session_token.json", {})

    def _record_observation(self, value: dict[str, Any]) -> None:
        history = self._read_state("observations.json", [])[-49:]
        history.append(value)
        self._write_state("observations.json", history)

    def read_budget_state(self, now=None):
        """Local deterministic projection; no network request or counter increment."""
        current = now or datetime.now(timezone.utc)
        day = current.strftime("%Y-%m-%d")
        counters = self._read_state("counters.json", {})
        return {"day": day, "reads": int(counters.get("reads",0)) if counters.get("day")==day else 0,
                "limit": int(self._config.get("max_reads_per_day",120))}

    def _bump_counter(self, kind: str) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        counters = self._read_state("counters.json", {"day": day, "posts": 0, "reads": 0})
        if counters.get("day") != day:
            counters = {"day": day, "posts": 0, "reads": 0}
        counters[kind] = int(counters.get(kind, 0)) + 1
        limit = int(self._config.get("max_posts_per_day", 3)) if kind == "posts" else int(self._config.get("max_reads_per_day", 120))
        if counters[kind] > limit:
            raise MCPServerUnavailable(f"{kind} daily budget exhausted ({limit}/{day})")
        self._write_state("counters.json", counters)

    def _walk_boards(self, boards: Any, _depth: int = 0) -> list[dict[str, Any]]:
        """Flatten an arbitrary board/thread structure into a searchable list."""
        if _depth > 12:
            return []
        items: list[dict[str, Any]] = []
        if isinstance(boards, dict):
            if any(k in boards for k in ("thread", "subject", "title", "content")) and _depth > 0:
                items.append(boards)
            for value in boards.values():
                items.extend(self._walk_boards(value, _depth + 1))
        elif isinstance(boards, list):
            for value in boards:
                items.extend(self._walk_boards(value, _depth + 1))
        return items

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _mcp_client(self, *, authenticated: bool) -> MCPClient:
        headers: dict[str, str] = {}
        store = self._secret_store()
        if authenticated:
            # Prefer session token for authenticated calls during active session
            session_token = self._get_session_token()
            if session_token:
                headers[self._config.get("auth_header", "authorization")] = f"Bearer {session_token}"
            elif store.has(self._secret_handle()):
                # Fallback to permanent credential for recovery operations
                headers[self._config.get("auth_header", "authorization")] = f"{SECRET_REF_PREFIX}{self._secret_handle()}"
        return MCPClient(
            MCPServer(name="culture-commons", url=self._url(), headers=headers),
            timeout=float(self._config.get("timeout", 30.0)),
            secret_resolver=store.get,
        )
