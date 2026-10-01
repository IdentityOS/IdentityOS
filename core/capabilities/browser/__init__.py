"""Browser capability — person-like web surfing for IdentityOS identities.

Comet-inspired agentic browsing: search, judge usefulness, navigate,
interact with pages (click/type/login), and keep a persistent session so
logged-in work continues across skill calls.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import time
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from core.capabilities.base import Capability, Skill, object_schema
from core.capabilities.registry import register
from core.capabilities.result import CapabilityResult

from .session import (
    BrowserUnavailable,
    close_identity_sessions,
    close_session,
    close_tab,
    create_checkpoint,
    ensure_session,
    export_session,
    get_session,
    identity_browser_dir,
    import_session,
    list_checkpoints,
    list_tabs,
    new_tab,
    page_snapshot,
    restore_checkpoint,
    run_in_browser_thread,
    switch_tab,
)
from .firefox_profiles import (
    list_firefox_profiles,
    prepare_firefox_profile,
    resolve_firefox_profile,
)
from .url_policy import validate_navigation_url
from .nl_interface import BrowserNLInterface, create_nl_interface

_SECRET_KEYS = {"password", "passwd", "pass", "secret", "token", "api_key"}
_EXECUTION_SCOPE: ContextVar[str] = ContextVar(
    "identityos_browser_execution_scope",
    default="sdk",
)


def _read_exact(stream: Any, n: int) -> bytes:
    """Read exactly *n* bytes from a pipe, tolerating partial reads.

    ``stream.read(n)`` may return fewer bytes than requested — for example
    when a large native-messaging frame exceeds the OS pipe buffer — so
    framing code must loop until the whole frame has arrived.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def _write_all(stream: Any, data: bytes) -> None:
    """Write all of *data*, tolerating partial writes on unbuffered pipes."""
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if written is None:
            break
        view = view[written:]
    stream.flush()


def _redact(params: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        if k.lower() in _SECRET_KEYS or "password" in k.lower():
            out[k] = "***"
        else:
            out[k] = v
    return out


def _score_result(task: str, title: str, snippet: str, url: str) -> dict[str, Any]:
    """Heuristic usefulness score for a search hit against a task."""
    task_l = (task or "").lower()
    blob = f"{title} {snippet} {url}".lower()
    tokens = [t for t in re.findall(r"[a-z0-9]{3,}", task_l) if t not in {
        "the", "and", "for", "with", "from", "that", "this", "into", "about",
        "open", "find", "search", "please", "using", "what", "when", "where",
    }]
    if not tokens:
        tokens = re.findall(r"[a-z0-9]{3,}", task_l)[:8]

    hits = sum(1 for t in tokens if t in blob)
    coverage = hits / max(len(tokens), 1)
    # Prefer primary sources / docs / official pages slightly
    bonus = 0.0
    if any(x in url.lower() for x in ("docs.", "github.com", "wikipedia.org", "gov", "edu")):
        bonus += 0.08
    if any(x in blob for x in ("login", "sign in", "account")) and "login" in task_l:
        bonus += 0.1
    score = min(1.0, round(coverage * 0.85 + bonus, 3))
    # Short tasks / strong title hits still count as useful
    if coverage >= 0.25 and hits >= 1:
        score = max(score, 0.36)
    title_l = (title or "").lower()
    if any(t in title_l for t in tokens[:4]) and hits >= 1:
        score = max(score, 0.4)
    useful = score >= 0.35
    reason_parts = []
    if hits:
        matched = [t for t in tokens if t in blob][:6]
        reason_parts.append(f"matched terms: {', '.join(matched)}")
    else:
        reason_parts.append("no task keywords matched")
    if bonus:
        reason_parts.append("trusted/domain bonus applied")
    return {
        "useful": useful,
        "score": score,
        "reason": "; ".join(reason_parts),
        "matched_terms": [t for t in tokens if t in blob],
    }




class _NativeMessagingClient:
    """Client for communicating with the Firefox Native Messaging host."""

    def __init__(self, host_script: Optional[str] = None):
        self._host_script = host_script or os.environ.get(
            "IDENTITYOS_NATIVE_HOST",
            str(Path(__file__).parent.parent.parent.parent / "browser_extension" / "native_host.py")
        )
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._pending: dict[int, threading.Event] = {}
        self._responses: dict[int, dict] = {}
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False

    def _ensure_started(self) -> None:
        if self._running:
            return
        try:
            self._process = subprocess.Popen(
                ["python3", "-u", self._host_script, "--relay"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            self._running = True
            self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._reader_thread.start()
            time.sleep(0.3)
        except Exception as e:
            raise RuntimeError(f"Failed to start native messaging host: {e}")

    def _read_loop(self) -> None:
        """Read length-prefixed JSON messages from stdout."""
        while self._running and self._process and self._process.stdout:
            try:
                length_bytes = _read_exact(self._process.stdout, 4)
                if len(length_bytes) < 4:
                    break
                length = struct.unpack("@I", length_bytes)[0]
                message_bytes = _read_exact(self._process.stdout, length)
                if len(message_bytes) < length:
                    break
                message = json.loads(message_bytes.decode("utf-8"))
                msg_id = message.get("id")
                if msg_id in self._pending:
                    self._responses[msg_id] = message
                    self._pending[msg_id].set()
            except Exception:
                break

    def send(self, msg_type: str, **kwargs) -> dict:
        """Send a message and wait for response."""
        self._ensure_started()

        with self._lock:
            self._request_id += 1
            req_id = self._request_id
            event = threading.Event()
            self._pending[req_id] = event

            message = {"id": req_id, "type": "browser", **kwargs}
            encoded = json.dumps(message).encode("utf-8")
            length = struct.pack("@I", len(encoded))

            try:
                if self._process and self._process.stdin:
                    _write_all(self._process.stdin, struct.pack("@I", len(encoded)) + encoded)
            except Exception as e:
                self._pending.pop(req_id, None)
                raise RuntimeError(f"Failed to send message: {e}")

        # Wait for response with timeout
        if event.wait(timeout=30.0):
            response = self._responses.pop(req_id, None)
            self._pending.pop(req_id, None)
            if response and "error" in response:
                raise RuntimeError(response["error"])
            return response.get("result", {}) if response else {}
        else:
            self._pending.pop(req_id, None)
            raise TimeoutError("Native messaging host request timed out")

    def close(self) -> None:
        self._running = False
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                pass
            self._process = None


# Global client instance
_native_client: Optional[_NativeMessagingClient] = None


def _get_native_client() -> _NativeMessagingClient:
    global _native_client
    if _native_client is None:
        _native_client = _NativeMessagingClient()
    return _native_client


def _call_native(msg_type: str, **kwargs) -> dict:
    """Call the native messaging host."""
    try:
        client = _get_native_client()
        return client.send("browser", type=msg_type, **kwargs)
    except Exception as e:
        return {"error": f"Native messaging error: {e}"}


@register
class BrowserCapability(Capability):
    id = "browser"
    name = "Browser"
    version = "1.0.0"
    author = "IdentityOS"
    license = "MIT"
    homepage = "https://github.com/lacebx/IdentityOS"
    description = (
        "Person-like web surfing: search, judge result usefulness, open pages, "
        "click/type/login, and act inside authenticated sessions (Comet-style)."
    )
    permissions = [
        "browser:read",
        "browser:write",
        "browser:credentials",
        "browser:live_read",
        "browser:live_tabs",
        "browser:live_write",
    ]
    # Live-browser scopes are granted by default because the bridge is inert
    # until the user explicitly loads the Firefox WebExtension and the native
    # host is running. ``browser:credentials`` stays an explicit grant.
    default_grants = [
        "browser:read",
        "browser:write",
        "browser:live_read",
        "browser:live_tabs",
        "browser:live_write",
    ]

    def __init__(self, config: Optional[dict] = None) -> None:
        super().__init__(config)
        self._storage_root = (config or {}).get("storage_root", ".identity_store")
        self._headless = bool((config or {}).get("headless", True))
        self._allow_private_network = bool(
            (config or {}).get("allow_private_network", False)
        )
        self._browser_type = (config or {}).get("browser_type", "chromium")
        self._user_profile_dir = (config or {}).get("user_profile_dir")
        if self._user_profile_dir:
            self._user_profile_dir = Path(self._user_profile_dir).expanduser().resolve()
        self._identity_id = ""
        self._http = httpx.Client(
            timeout=20,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            },
        )

    def install(self, identity_id: str, storage: Any) -> None:
        self._identity_id = identity_id
        storage.save(
            identity_id,
            "capability.browser",
            {"installed_at": time.time(), "headless": self._headless},
        )

    def uninstall(self, identity_id: str, storage: Any) -> None:
        close_identity_sessions(identity_id, storage_root=self._storage_root)
        profile_root = identity_browser_dir(self._storage_root, identity_id)
        if profile_root.is_dir():
            shutil.rmtree(profile_root)
        storage.delete(identity_id, "capability.browser")

    def on_identity_loaded(self, identity_id: str) -> None:
        self._identity_id = identity_id

    @property
    def nl(self) -> "BrowserNLInterface":
        """Natural language interface for high-level browser commands."""
        if not hasattr(self, "_nl_interface"):
            self._nl_interface = create_nl_interface(self)
        return self._nl_interface

    def prompts(self, identity_id: str) -> list[str]:
        return [
            "## Browser Skills (MANDATORY for web tasks)",
            "You can surf the web like a person using browser.* skills.",
            "Workflow for research:",
            "1) browser.search(query, task=...) to get results with usefulness scores",
            "2) Prefer results marked useful=true / higher score for the given task",
            "3) browser.open or browser.navigate to visit a promising URL",
            "4) browser.snapshot to read the page and see clickable elements",
            "5) browser.click / browser.type / browser.fill / browser.press to interact",
            "6) browser.use_profile(profile='lace') to reuse a real Firefox login session",
            "7) browser.login only after browser:credentials is explicitly granted",
            "8) browser.eval_js to read structured data from the page / call site APIs",
            "9) Keep using browser.snapshot after each action to verify what changed",
            "Do NOT claim you cannot browse, search, or log in. You CAN — use these skills.",
            "Never invent page content. Only report what browser.snapshot / search / eval_js returned.",
            "Chat credentials must use password=<value>; the runtime replaces the value with an ephemeral reference.",
            "Never invent or echo passwords. Preserve secret-ref:// values exactly in tool arguments.",
            "Only log into sites the user explicitly authorized with credentials they provided.",
            "",
            "## Live vs. isolated browser",
            "There are TWO browsers, and picking the right one matters:",
            "- browser.live.* controls the user's REAL Firefox — the windows and tabs "
            "they already have open on their screen. Use it whenever the request refers "
            "to \"my browser\", \"my tabs\", \"what do I have open\", or the user's own "
            "logged-in session.",
            "- browser.* (without .live) controls Comet's own isolated automated browser "
            "session. Use it for autonomous research and pages Comet opens itself.",
            "Do not answer questions about the user's own tabs using browser.list_tabs; "
            "use browser.live.list_tabs. If browser.live.status reports the bridge is not "
            "connected, say so truthfully instead of substituting the isolated session.",
        ]

    _SKILLS = [
        Skill(
            name="browser.open",
            description="Open a URL in a persistent browser session (starts browser if needed)",
            permission="browser:read",
            effect="write",
            input_schema=object_schema(
                {
                    "url": {"type": "string", "minLength": 1},
                    "headless": {"type": "boolean"},
                    "wait_until": {"type": "string"},
                },
                required=("url",),
            ),
        ),
        Skill(
            name="browser.navigate",
            description="Navigate the current tab to a URL",
            permission="browser:write",
            effect="write",
            input_schema=object_schema(
                {
                    "url": {"type": "string", "minLength": 1},
                    "wait_until": {"type": "string"},
                },
                required=("url",),
            ),
        ),
        Skill(
            name="browser.search",
            description=(
                "Search the web and return ranked results. Pass task= so each result "
                "includes a usefulness score relative to the user's goal."
            ),
            permission="browser:read",
            effect="read",
            input_schema=object_schema(
                {
                    "query": {"type": "string", "minLength": 1},
                    "task": {"type": "string"},
                    "max_results": {"type": "integer"},
                    "engine": {"type": "string"},
                },
                required=("query",),
            ),
        ),
        Skill(
            name="browser.evaluate_results",
            description=(
                "Score previously obtained search results against a task and recommend "
                "which URLs are worth opening."
            ),
            permission="browser:read",
            effect="read",
            input_schema=object_schema(
                {
                    "task": {"type": "string", "minLength": 1},
                    "results": {"type": "array"},
                    "min_score": {"type": "number"},
                },
                required=("task", "results"),
            ),
        ),
        Skill(
            name="browser.snapshot",
            description=(
                "Read the current page in Comet's isolated automated browser session: "
                "URL, title, visible text, and interactive elements"
            ),
            permission="browser:read",
            effect="read",
            input_schema=object_schema(
                {"max_chars": {"type": "integer"}},
            ),
        ),
        Skill(
            name="browser.click",
            description="Click an element by CSS selector, visible text, or role+name",
            permission="browser:write",
            effect="write",
            input_schema=object_schema(
                {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                    "role": {"type": "string"},
                    "name": {"type": "string"},
                    "exact": {"type": "boolean"},
                },
            ),
        ),
        Skill(
            name="browser.type",
            description="Type text into a focused or selected input (optionally clear first)",
            permission="browser:write",
            effect="write",
            input_schema=object_schema(
                {
                    "text": {"type": "string"},
                    "selector": {"type": "string"},
                    "clear": {"type": "boolean"},
                    "press_enter": {"type": "boolean"},
                },
                required=("text",),
            ),
        ),
        Skill(
            name="browser.fill",
            description="Fill a form field by selector (clears existing value)",
            permission="browser:write",
            effect="write",
            input_schema=object_schema(
                {
                    "selector": {"type": "string", "minLength": 1},
                    "value": {"type": "string"},
                },
                required=("selector", "value"),
            ),
        ),
        Skill(
            name="browser.login",
            description=(
                "Log into a site with user-provided credentials. Optionally pass "
                "username_selector/password_selector/submit_selector; otherwise auto-detect."
            ),
            permission="browser:credentials",
            effect="write",
            input_schema=object_schema(
                {
                    "url": {"type": "string", "minLength": 1},
                    "username": {"type": "string", "minLength": 1},
                    "password": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Opaque secret-ref:// value supplied by the runtime",
                    },
                    "username_selector": {"type": "string"},
                    "password_selector": {"type": "string"},
                    "submit_selector": {"type": "string"},
                    "success_url_contains": {"type": "string"},
                    "success_selector": {"type": "string"},
                    "wait_ms": {"type": "integer"},
                },
                required=("url", "username", "password"),
            ),
        ),
        Skill(
            name="browser.press",
            description="Press a keyboard key (Enter, Tab, Escape, etc.)",
            permission="browser:write",
            effect="write",
            input_schema=object_schema(
                {"key": {"type": "string", "minLength": 1}},
                required=("key",),
            ),
        ),
        Skill(
            name="browser.wait",
            description="Wait for a selector, URL substring, or fixed milliseconds",
            permission="browser:read",
            effect="read",
            input_schema=object_schema(
                {
                    "selector": {"type": "string"},
                    "url_contains": {"type": "string"},
                    "ms": {"type": "integer"},
                    "timeout_ms": {"type": "integer"},
                },
            ),
        ),
        Skill(
            name="browser.status",
            description=(
                "Report whether Comet's isolated automated browser session is open "
                "(not the user's own Firefox) and its current URL/title"
            ),
            permission="browser:read",
            effect="read",
            input_schema=object_schema({}),
        ),
        Skill(
            name="browser.close",
            description="Close the browser session (cookies in profile may persist on disk)",
            permission="browser:write",
            effect="write",
            input_schema=object_schema({}),
        ),
        Skill(
            name="browser.list_profiles",
            description="List local Firefox profiles (name, path, locked, canvas history hint)",
            permission="browser:read",
            effect="read",
            input_schema=object_schema({}),
        ),
        Skill(
            name="browser.use_profile",
            description=(
                "Attach a local Firefox profile by name or path (e.g. lace, original, "
                "default-release). Copies the profile when Firefox currently has it locked."
            ),
            permission="browser:write",
            effect="write",
            input_schema=object_schema(
                {
                    "profile": {"type": "string", "minLength": 1},
                    "browser_type": {"type": "string"},
                    "headless": {"type": "boolean"},
                    "force_copy": {"type": "boolean"},
                },
                required=("profile",),
            ),
        ),
        Skill(
            name="browser.eval_js",
            description="Run JavaScript in the active page and return the JSON-serializable result",
            permission="browser:write",
            effect="write",
            input_schema=object_schema(
                {
                    "expression": {"type": "string", "minLength": 1},
                },
                required=("expression",),
            ),
        ),
        # Multi-tab
        Skill(
            name="browser.new_tab",
            description="Create a new tab in Comet's isolated automated browser session",
            permission="browser:write",
            effect="write",
            input_schema=object_schema(
                {
                    "url": {"type": "string"},
                },
            ),
        ),
        Skill(
            name="browser.switch_tab",
            description="Switch to a different tab in Comet's isolated automated browser session by tab_id",
            permission="browser:write",
            effect="write",
            input_schema=object_schema(
                {
                    "tab_id": {"type": "string", "minLength": 1},
                },
                required=("tab_id",),
            ),
        ),
        Skill(
            name="browser.close_tab",
            description="Close a specific tab by tab_id",
            permission="browser:write",
            effect="write",
            input_schema=object_schema(
                {
                    "tab_id": {"type": "string", "minLength": 1},
                },
                required=("tab_id",),
            ),
        ),
        Skill(
            name="browser.list_tabs",
            description=(
                "List tabs in Comet's isolated automated browser session. This is NOT "
                "the user's own Firefox — for the user's real open tabs use browser.live.list_tabs"
            ),
            permission="browser:read",
            effect="read",
            input_schema=object_schema({}),
        ),
        # Checkpoints
        Skill(
            name="browser.checkpoint_create",
            description="Create a checkpoint of the current session state (cookies, localStorage, tabs)",
            permission="browser:write",
            effect="write",
            input_schema=object_schema({}),
        ),
        Skill(
            name="browser.checkpoint_restore",
            description="Restore session from a checkpoint (default: latest)",
            permission="browser:write",
            effect="write",
            input_schema=object_schema(
                {
                    "checkpoint_index": {"type": "integer"},
                },
            ),
        ),
        Skill(
            name="browser.checkpoint_list",
            description="List available checkpoints for the session",
            permission="browser:read",
            effect="read",
            input_schema=object_schema({}),
        ),
        Skill(
            name="browser.session_export",
            description="Export complete session state for backup/migration",
            permission="browser:read",
            effect="read",
            input_schema=object_schema({}),
        ),
        Skill(
            name="browser.session_import",
            description="Import session state from backup",
            permission="browser:write",
            effect="write",
            input_schema=object_schema(
                {
                    "session_data": {"type": "object"},
                },
                required=("session_data",),
            ),
        ),
        # Live browser mode (Firefox WebExtension bridge)
        Skill(
            name="browser.live.status",
            description="Check whether Comet is connected to the user's real Firefox browser (live bridge status)",
            permission="browser:live_read",
            effect="read",
            input_schema=object_schema({}),
        ),
        Skill(
            name="browser.live.list_tabs",
            description=(
                "List all tabs currently open in the user's real Firefox browser "
                "(their own windows and tabs). Use this for \"my tabs\", \"my browser\", "
                "and \"what do I have open\" questions"
            ),
            permission="browser:live_tabs",
            effect="read",
            input_schema=object_schema({}),
        ),
        Skill(
            name="browser.live.active_tab",
            description="Get the tab the user currently has focused in their real Firefox browser",
            permission="browser:live_tabs",
            effect="read",
            input_schema=object_schema({}),
        ),
        Skill(
            name="browser.live.activate_tab",
            description="Activate a specific tab in the user's real Firefox browser",
            permission="browser:live_write",
            effect="write",
            input_schema=object_schema(
                {
                    "tab_id": {"type": "string", "minLength": 1},
                },
                required=("tab_id",),
            ),
        ),
        Skill(
            name="browser.live.create_tab",
            description="Create a new tab in the user's real Firefox browser",
            permission="browser:live_write",
            effect="write",
            input_schema=object_schema(
                {
                    "url": {"type": "string"},
                },
            ),
        ),
        Skill(
            name="browser.live.close_tab",
            description="Close a specific tab in the user's real Firefox browser",
            permission="browser:live_write",
            effect="write",
            input_schema=object_schema(
                {
                    "tab_id": {"type": "string", "minLength": 1},
                },
                required=("tab_id",),
            ),
        ),
        Skill(
            name="browser.live.snapshot",
            description="Read a tab in the user's real Firefox browser (URL, title, visible text, interactive elements)",
            permission="browser:live_read",
            effect="read",
            input_schema=object_schema(
                {
                    "tab_id": {"type": "string", "minLength": 1},
                    "max_chars": {"type": "integer"},
                },
                required=("tab_id",),
            ),
        ),
        Skill(
            name="browser.live.navigate",
            description="Navigate a tab in the user's real Firefox browser to a URL",
            permission="browser:live_write",
            effect="write",
            input_schema=object_schema(
                {
                    "tab_id": {"type": "string", "minLength": 1},
                    "url": {"type": "string", "minLength": 1},
                    "wait_until": {"type": "string"},
                },
                required=("tab_id", "url"),
            ),
        ),
        Skill(
            name="browser.live.click",
            description="Click an element in a tab of the user's real Firefox browser",
            permission="browser:live_write",
            effect="write",
            input_schema=object_schema(
                {
                    "tab_id": {"type": "string", "minLength": 1},
                    "selector": {"type": "string", "minLength": 1},
                },
                required=("tab_id", "selector"),
            ),
        ),
        Skill(
            name="browser.live.fill",
            description="Fill a form field in a tab of the user's real Firefox browser",
            permission="browser:live_write",
            effect="write",
            input_schema=object_schema(
                {
                    "tab_id": {"type": "string", "minLength": 1},
                    "selector": {"type": "string", "minLength": 1},
                    "value": {"type": "string"},
                },
                required=("tab_id", "selector", "value"),
            ),
        ),
        Skill(
            name="browser.live.type",
            description="Type text into an element in a tab of the user's real Firefox browser",
            permission="browser:live_write",
            effect="write",
            input_schema=object_schema(
                {
                    "tab_id": {"type": "string", "minLength": 1},
                    "selector": {"type": "string", "minLength": 1},
                    "text": {"type": "string"},
                },
                required=("tab_id", "selector", "text"),
            ),
        ),
        Skill(
            name="browser.live.press",
            description="Press a keyboard key in a tab of the user's real Firefox browser",
            permission="browser:live_write",
            effect="write",
            input_schema=object_schema(
                {
                    "tab_id": {"type": "string", "minLength": 1},
                    "key": {"type": "string", "minLength": 1},
                },
                required=("tab_id", "key"),
            ),
        ),


        Skill(
            name="browser.autonomous_task",
            description="Execute an autonomous browser task from a high-level goal. The planner observes, reasons, acts, and verifies until the goal is achieved or escalation is needed.",
            permission="browser:write",
            effect="execute",
            input_schema=object_schema(
                {
                    "goal": {"type": "string", "minLength": 1},
                    "max_steps": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                    "allowed_effects": {"type": "array", "items": {"type": "string"}, "default": ["READ_ONLY", "REVERSIBLE"]},
                    "stop_conditions": {"type": "array", "items": {"type": "string"}},
                    "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 3600, "default": 300},
                },
                required=("goal",),
            ),
        ),
        Skill(
            name="browser.wait_for_human",
            description="Pause autonomous execution and wait for human intervention (CAPTCHA, MFA, ambiguous target, etc.). Provide a reason and optional screenshot.",
            permission="browser:write",
            effect="escalate",
            input_schema=object_schema(
                {
                    "reason": {"type": "string", "minLength": 1},
                    "escalation_type": {"type": "string", "enum": ["CAPTCHA", "MFA", "AUTH_REQUIRED", "AMBIGUOUS_TARGET", "CONSEQUENTIAL_ACTION", "PAYMENT_CONFIRMATION", "PERMISSION_REQUIRED", "MANUAL_INTERACTION", "UNKNOWN_BLOCKER"]},
                    "screenshot_path": {"type": "string"},
                    "resume_token": {"type": "string"},
                },
                required=("reason", "escalation_type"),
            ),
        ),
    ]

    def skills(self) -> list[Skill]:
        return list(self._SKILLS)

    def call(self, skill_name: str, **params: Any) -> CapabilityResult:
        return self._call(skill_name, **params)

    def call_scoped(
        self,
        skill_name: str,
        *,
        execution_scope: Optional[str] = None,
        **params: Any,
    ) -> CapabilityResult:
        token = _EXECUTION_SCOPE.set(execution_scope or "sdk")
        try:
            return self._call(skill_name, **params)
        finally:
            _EXECUTION_SCOPE.reset(token)

    def _call(self, skill_name: str, **params: Any) -> CapabilityResult:
        t0 = time.monotonic()
        safe = _redact(params)
        try:
            dispatch = {
                "browser.open": self._open,
                "browser.navigate": self._navigate,
                "browser.search": self._search,
                "browser.evaluate_results": self._evaluate_results,
                "browser.snapshot": self._snapshot,
                "browser.click": self._click,
                "browser.type": self._type,
                "browser.fill": self._fill,
                "browser.login": self._login,
                "browser.press": self._press,
                "browser.wait": self._wait,
                "browser.status": self._status,
                "browser.close": self._close,
                "browser.list_profiles": self._list_profiles,
                "browser.use_profile": self._use_profile,
                "browser.eval_js": self._eval_js,
                # Multi-tab
                "browser.new_tab": self._new_tab,
                "browser.switch_tab": self._switch_tab,
                "browser.close_tab": self._close_tab,
                "browser.list_tabs": self._list_tabs,
                # Checkpoints
                "browser.checkpoint_create": self._checkpoint_create,
                "browser.checkpoint_restore": self._checkpoint_restore,
                "browser.checkpoint_list": self._checkpoint_list,
                "browser.session_export": self._session_export,
                "browser.session_import": self._session_import,
                # Live browser mode
                "browser.live.status": self._live_status,
                "browser.live.list_tabs": self._live_list_tabs,
                "browser.live.active_tab": self._live_active_tab,
                "browser.live.activate_tab": self._live_activate_tab,
                "browser.live.create_tab": self._live_create_tab,
                "browser.live.close_tab": self._live_close_tab,
                "browser.live.snapshot": self._live_snapshot,
                "browser.live.navigate": self._live_navigate,
                "browser.live.click": self._live_click,
                "browser.live.fill": self._live_fill,
                "browser.live.type": self._live_type,
                "browser.live.press": self._live_press,

            "browser.autonomous_task": self._autonomous_task,
            "browser.wait_for_human": self._wait_for_human,
}
            handler = dispatch.get(skill_name)
            if handler is None:
                return CapabilityResult.fail(
                    "browser", skill_name, "unknown_skill", f"Unknown skill: {skill_name}",
                    params=safe,
                )
            data = handler(**params)
            return CapabilityResult.from_data(
                "browser",
                skill_name,
                data,
                source="browser session",
                duration_ms=(time.monotonic() - t0) * 1000,
                params=safe,
            )
        except BrowserUnavailable as e:
            return CapabilityResult.fail(
                "browser", skill_name, "BrowserUnavailable", str(e),
                source="browser session",
                duration_ms=(time.monotonic() - t0) * 1000,
                params=safe,
            )
        except Exception as e:
            return CapabilityResult.fail(
                "browser", skill_name, type(e).__name__, str(e),
                source="browser session",
                duration_ms=(time.monotonic() - t0) * 1000,
                params=safe,
            )

    # ── helpers ────────────────────────────────────────────────────────

    def _require_identity(self) -> str:
        if self._identity_id:
            return self._identity_id
        # Fallback for direct calls before on_identity_loaded
        return "default"

    def _page(self, *, headless: Optional[bool] = None):
        identity_id = self._require_identity()
        state = ensure_session(
            identity_id,
            storage_root=self._storage_root,
            headless=self._headless if headless is None else headless,
            execution_scope=_EXECUTION_SCOPE.get(),
            allow_private_network=self._allow_private_network,
            browser_type=self._browser_type,
            user_profile_dir=self._user_profile_dir,
        )
        return state

    def _session(self):
        return get_session(
            self._require_identity(),
            storage_root=self._storage_root,
            execution_scope=_EXECUTION_SCOPE.get(),
            browser_type=self._browser_type,
            user_profile_dir=self._user_profile_dir,
        )

    def _goto(self, url: str, wait_until: str = "domcontentloaded") -> dict[str, Any]:
        url = validate_navigation_url(
            url,
            allow_private_network=self._allow_private_network,
        )
        allowed = {"load", "domcontentloaded", "networkidle", "commit"}
        wu = (wait_until or "domcontentloaded").strip().lower()
        # Models sometimes emit Puppeteer names (networkidle0/2).
        if wu in {"networkidle0", "networkidle2"}:
            wu = "networkidle"
        if wu not in allowed:
            wu = "domcontentloaded"

        def _do() -> dict[str, Any]:
            state = self._page()
            with state.lock:
                page = state.page
                page.goto(url, wait_until=wu, timeout=45000)
                state.last_url = page.url
                state.history.append(page.url)
                snap = page_snapshot(page, max_chars=4000)
                return {
                    "ok": True,
                    "url": page.url,
                    "title": snap["title"],
                    "text_preview": snap["text"][:1500],
                    "interactive_count": snap["interactive_count"],
                }

        return run_in_browser_thread(_do)

    # ── skills ─────────────────────────────────────────────────────────

    def _open(self, url: str = "", headless: Optional[bool] = None, wait_until: str = "domcontentloaded", **_: Any) -> dict[str, Any]:
        if not url:
            return {"error": "url is required"}
        if headless is not None:
            self._headless = bool(headless)
        return self._goto(url, wait_until=wait_until)

    def _navigate(self, url: str = "", wait_until: str = "domcontentloaded", **_: Any) -> dict[str, Any]:
        if not url:
            return {"error": "url is required"}
        state = self._session()
        if state is None or not state.started:
            return self._goto(url, wait_until=wait_until)
        return self._goto(url, wait_until=wait_until)

    def _search(
        self,
        query: str = "",
        task: str = "",
        max_results: int = 8,
        engine: str = "duckduckgo",
        **_: Any,
    ) -> dict[str, Any]:
        if not query:
            return {"error": "query is required"}
        max_results = max(1, min(int(max_results or 8), 15))
        task = task or query
        engine = (engine or "duckduckgo").lower()

        results = self._search_duckduckgo(query, max_results=max_results)
        if not results and engine != "html":
            # Fallback: open DDG in real browser and scrape links
            results = self._search_browser(query, max_results=max_results)

        scored = []
        for r in results:
            judgment = _score_result(task, r.get("title", ""), r.get("snippet", ""), r.get("url", ""))
            scored.append({**r, **judgment})

        scored.sort(key=lambda x: x.get("score", 0), reverse=True)
        useful = [r for r in scored if r.get("useful")]
        return {
            "query": query,
            "task": task,
            "engine": "duckduckgo",
            "result_count": len(scored),
            "useful_count": len(useful),
            "recommended": useful[:3] or scored[:2],
            "results": scored,
            "guidance": (
                "Open recommended URLs with browser.open / browser.navigate, "
                "then browser.snapshot to verify content before acting."
            ),
        }

    def _search_duckduckgo(self, query: str, max_results: int) -> list[dict[str, Any]]:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        resp = self._http.get(url)
        resp.raise_for_status()
        html = resp.text
        results: list[dict[str, Any]] = []

        anchors = re.findall(
            r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            html,
            flags=re.I | re.S,
        )
        if not anchors:
            anchors = re.findall(
                r'<a[^>]*href="([^"]+)"[^>]*class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>',
                html,
                flags=re.I | re.S,
            )

        snippets = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div)',
            html,
            flags=re.I | re.S,
        )

        for idx, (href, title_html) in enumerate(anchors):
            if len(results) >= max_results:
                break
            title = re.sub(r"<[^>]+>", "", title_html).strip()
            snippet = ""
            if idx < len(snippets):
                snippet = re.sub(r"<[^>]+>", "", snippets[idx]).strip()
            final_url = self._unwrap_ddg(href)
            if not final_url or "duckduckgo.com" in final_url:
                continue
            results.append({"title": title, "url": final_url, "snippet": snippet[:400]})
        return results

    def _unwrap_ddg(self, href: str) -> str:
        href = href.replace("&amp;", "&")
        if "uddg=" in href:
            qs = parse_qs(urlparse(href).query)
            if "uddg" in qs:
                return unquote(qs["uddg"][0])
        if href.startswith("//"):
            href = "https:" + href
        return href

    def _search_browser(self, query: str, max_results: int) -> list[dict[str, Any]]:
        def _do() -> list[dict[str, Any]]:
            state = self._page()
            with state.lock:
                page = state.page
                page.goto(
                    f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                results: list[dict[str, Any]] = []
                anchors = page.query_selector_all("a.result__a")
                for a in anchors[:max_results]:
                    try:
                        title = (a.inner_text() or "").strip()
                        href = a.get_attribute("href") or ""
                        url = self._unwrap_ddg(href)
                        results.append({"title": title, "url": url, "snippet": ""})
                    except Exception:
                        continue
                state.last_url = page.url
                return results

        return run_in_browser_thread(_do)

    def _evaluate_results(
        self,
        task: str = "",
        results: Optional[list] = None,
        min_score: float = 0.35,
        **_: Any,
    ) -> dict[str, Any]:
        if not task:
            return {"error": "task is required"}
        results = results or []
        if not isinstance(results, list):
            return {"error": "results must be a list of {title,url,snippet} objects"}

        scored = []
        for r in results:
            if not isinstance(r, dict):
                continue
            judgment = _score_result(
                task,
                str(r.get("title", "")),
                str(r.get("snippet", r.get("description", ""))),
                str(r.get("url", r.get("link", ""))),
            )
            item = {
                "title": r.get("title", ""),
                "url": r.get("url", r.get("link", "")),
                "snippet": r.get("snippet", r.get("description", "")),
                **judgment,
            }
            scored.append(item)
        scored.sort(key=lambda x: x.get("score", 0), reverse=True)
        useful = [r for r in scored if r.get("score", 0) >= float(min_score)]
        return {
            "task": task,
            "min_score": min_score,
            "useful_count": len(useful),
            "recommended_urls": [r["url"] for r in useful[:5] if r.get("url")],
            "results": scored,
            "verdict": (
                f"{len(useful)} of {len(scored)} results look useful for the task"
                if scored else "no results to evaluate"
            ),
        }

    def _snapshot(self, max_chars: int = 12000, **_: Any) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            state = self._session()
            if state is None or not state.started or state.page is None:
                return {"error": "no open browser session — call browser.open first"}
            with state.lock:
                snap = page_snapshot(state.page, max_chars=int(max_chars or 12000))
                snap["history_len"] = len(state.history)
                return snap

        return run_in_browser_thread(_do)

    def _resolve_locator(self, page: Any, *, selector: str = "", text: str = "", role: str = "", name: str = "", exact: bool = False):
        if selector:
            return page.locator(selector).first
        if role and name:
            return page.get_by_role(role, name=name, exact=exact).first
        if text:
            return page.get_by_text(text, exact=exact).first
        if name:
            return page.get_by_label(name, exact=exact).first
        raise ValueError("Provide selector, text, or role+name")

    def _click(
        self,
        selector: str = "",
        text: str = "",
        role: str = "",
        name: str = "",
        exact: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            state = self._session()
            if state is None or state.page is None:
                return {"error": "no open browser session — call browser.open first"}
            with state.lock:
                page = state.page
                loc = self._resolve_locator(page, selector=selector, text=text, role=role, name=name, exact=exact)
                loc.click(timeout=15000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                state.last_url = page.url
                snap = page_snapshot(page, max_chars=3000)
                return {
                    "ok": True,
                    "clicked": {"selector": selector, "text": text, "role": role, "name": name},
                    "url": page.url,
                    "title": snap["title"],
                    "text_preview": snap["text"][:1200],
                }

        return run_in_browser_thread(_do)

    def _type(
        self,
        text: str = "",
        selector: str = "",
        clear: bool = False,
        press_enter: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        if text is None:
            return {"error": "text is required"}

        def _do() -> dict[str, Any]:
            state = self._session()
            if state is None or state.page is None:
                return {"error": "no open browser session — call browser.open first"}
            with state.lock:
                page = state.page
                if selector:
                    loc = page.locator(selector).first
                    if clear:
                        loc.fill("")
                    loc.type(str(text), delay=20)
                else:
                    if clear:
                        page.keyboard.press("Control+A")
                        page.keyboard.press("Backspace")
                    page.keyboard.type(str(text), delay=20)
                if press_enter:
                    page.keyboard.press("Enter")
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
                state.last_url = page.url
                return {"ok": True, "url": page.url, "typed_len": len(str(text)), "pressed_enter": press_enter}

        return run_in_browser_thread(_do)

    def _fill(self, selector: str = "", value: str = "", **_: Any) -> dict[str, Any]:
        if not selector:
            return {"error": "selector is required"}

        def _do() -> dict[str, Any]:
            state = self._session()
            if state is None or state.page is None:
                return {"error": "no open browser session — call browser.open first"}
            with state.lock:
                page = state.page
                page.locator(selector).first.fill(str(value), timeout=15000)
                return {"ok": True, "selector": selector, "value_len": len(str(value)), "url": page.url}

        return run_in_browser_thread(_do)

    def _login(
        self,
        url: str = "",
        username: str = "",
        password: str = "",
        username_selector: str = "",
        password_selector: str = "",
        submit_selector: str = "",
        success_url_contains: str = "",
        success_selector: str = "",
        wait_ms: int = 2500,
        **_: Any,
    ) -> dict[str, Any]:
        if not url or not username or not password:
            return {"error": "url, username, and password are required"}
        if not success_url_contains and not success_selector:
            return {
                "error": (
                    "login requires success_url_contains or success_selector so the "
                    "runtime can verify authentication"
                )
            }

        # Navigate first (already worker-threaded)
        nav = self._goto(url)
        if nav.get("error"):
            return nav

        def _do() -> dict[str, Any]:
            state = self._page()
            with state.lock:
                page = state.page

                user_sel = username_selector or self._autodetect_username(page)
                pass_sel = password_selector or self._autodetect_password(page)
                if not user_sel or not pass_sel:
                    snap = page_snapshot(page, max_chars=2000)
                    return {
                        "error": "Could not auto-detect login fields; pass username_selector and password_selector",
                        "url": page.url,
                        "interactive": snap.get("interactive", [])[:20],
                    }

                # Fill only credential fields - leave hidden fields (CSRF tokens, etc.) alone
                page.locator(user_sel).first.fill(username, timeout=15000)
                page.locator(pass_sel).first.fill(password, timeout=15000)

                submitted = False
                # Use form.requestSubmit() when possible - it properly submits hidden fields
                if submit_selector:
                    try:
                        page.locator(submit_selector).first.click(timeout=15000)
                        submitted = True
                    except Exception:
                        pass
                if not submitted:
                    # Try form.requestSubmit() - the standard way that preserves hidden fields
                    try:
                        page.evaluate("""
                            () => {
                                const userSel = arguments[0];
                                const passSel = arguments[1];
                                const userInput = document.querySelector(userSel);
                                const passInput = document.querySelector(passSel);
                                if (userInput && passInput) {
                                    const form = userInput.closest('form') || passInput.closest('form');
                                    if (form && typeof form.requestSubmit === 'function') {
                                        form.requestSubmit();
                                        return true;
                                    }
                                }
                                return false;
                            }
                        """, user_sel, pass_sel)
                        submitted = True
                    except Exception:
                        pass
                if not submitted:
                    # Fallback: click common submit buttons
                    for cand in (
                        "button[type='submit']",
                        "input[type='submit']",
                        "button:has-text('Log in')",
                        "button:has-text('Sign in')",
                        "button:has-text('Login')",
                        "[type='submit']",
                    ):
                        try:
                            loc = page.locator(cand).first
                            if page.locator(cand).count() > 0:
                                loc.click(timeout=3000)
                                submitted = True
                                break
                        except Exception:
                            continue
                if not submitted:
                    # Final fallback: press Enter in password field
                    page.locator(pass_sel).first.press("Enter")
                    submitted = True

                # Wait for navigation
                page.wait_for_timeout(int(wait_ms or 2500))
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass

                state.last_url = page.url
                snap = page_snapshot(page, max_chars=4000)
                success = True
                if success_url_contains:
                    success = success and success_url_contains in page.url
                if success_selector:
                    try:
                        success = success and (
                            page.locator(success_selector).count() > 0
                            and page.locator(success_selector).first.is_visible()
                        )
                    except Exception:
                        success = False

                body = (snap.get("text") or "").lower()
                failure_hints = [
                    "incorrect password",
                    "invalid credentials",
                    "login failed",
                    "sign in failed",
                    "wrong password",  # ggignore: authentication failure text, not a credential
                    "authentication failed",
                ]
                if any(h in body for h in failure_hints):
                    success = False

                safe_text = (snap.get("text") or "").replace(password, "***")

                if not success:
                    return {
                        "ok": False,
                        "error": "login post-condition was not satisfied",
                        "submitted": submitted,
                        "url": page.url,
                        "title": snap.get("title"),
                        "text_preview": safe_text[:1500],
                    }

                return {
                    "ok": success,
                    "submitted": submitted,
                    "username_selector": user_sel,
                    "password_selector": pass_sel,  # ggignore: selector name, never a credential
                    "url": page.url,
                    "title": snap.get("title"),
                    "text_preview": safe_text[:1500],
                    "note": (
                        "Login attempt finished. Verify with browser.snapshot before continuing "
                        "authenticated tasks. Password was not stored in the result."
                    ),
                }

        return run_in_browser_thread(_do)

    def _autodetect_username(self, page: Any) -> str:
        candidates = [
            "input[type='email']",
            "input[name='email']",
            "input[id='email']",
            "input[name='username']",
            "input[id='username']",
            "input[autocomplete='username']",
            "input[name='login']",
            "input[type='text']",
        ]
        for sel in candidates:
            try:
                if page.locator(sel).count() > 0:
                    return sel
            except Exception:
                continue
        return ""

    def _autodetect_password(self, page: Any) -> str:
        candidates = [
            "input[type='password']",
            "input[name='password']",
            "input[id='password']",
            "input[autocomplete='current-password']",
        ]
        for sel in candidates:
            try:
                if page.locator(sel).count() > 0:
                    return sel
            except Exception:
                continue
        return ""

    def _press(self, key: str = "", **_: Any) -> dict[str, Any]:
        if not key:
            return {"error": "key is required"}

        def _do() -> dict[str, Any]:
            state = self._session()
            if state is None or state.page is None:
                return {"error": "no open browser session — call browser.open first"}
            with state.lock:
                state.page.keyboard.press(key)
                return {"ok": True, "key": key, "url": state.page.url}

        return run_in_browser_thread(_do)

    def _wait(
        self,
        selector: str = "",
        url_contains: str = "",
        ms: int = 0,
        timeout_ms: int = 15000,
        **_: Any,
    ) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            state = self._session()
            if state is None or state.page is None:
                return {"error": "no open browser session — call browser.open first"}
            with state.lock:
                page = state.page
                if selector:
                    page.wait_for_selector(selector, timeout=int(timeout_ms or 15000))
                if url_contains:
                    page.wait_for_url(f"**/*{url_contains}*", timeout=int(timeout_ms or 15000))
                if ms:
                    page.wait_for_timeout(int(ms))
                if not selector and not url_contains and not ms:
                    page.wait_for_timeout(500)
                state.last_url = page.url
                return {"ok": True, "url": page.url}

        return run_in_browser_thread(_do)

    def _status(self, **_: Any) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            state = self._session()
            if state is None or not state.started or state.page is None:
                return {"open": False, "identity_id": self._require_identity()}
            with state.lock:
                try:
                    title = state.page.title()
                    url = state.page.url
                except Exception:
                    title, url = "", state.last_url
                return {
                    "open": True,
                    "identity_id": self._require_identity(),
                    "url": url,
                    "title": title,
                    "history_len": len(state.history),
                    "headless": state.headless,
                }

        return run_in_browser_thread(_do)

    def _close(self, **_: Any) -> dict[str, Any]:
        closed = close_session(
            self._require_identity(),
            storage_root=self._storage_root,
            execution_scope=_EXECUTION_SCOPE.get(),
        )
        return {"closed": closed, "identity_id": self._require_identity()}

    def _list_profiles(self, **_: Any) -> dict[str, Any]:
        from .firefox_profiles import profile_is_locked, _canvas_score

        profiles = []
        for p in list_firefox_profiles():
            profiles.append(
                {
                    "name": p.name,
                    "path": str(p.path),
                    "is_default": p.is_default,
                    "locked": profile_is_locked(p.path),
                    "canvas_history_hits": _canvas_score(p.path),
                }
            )
        return {"profiles": profiles, "count": len(profiles)}

    def _use_profile(
        self,
        profile: str = "",
        browser_type: str = "firefox",
        headless: Optional[bool] = None,
        force_copy: bool = False,
        **_: Any,
    ) -> dict[str, Any]:
        """Attach login state from a local Firefox profile.

        System Firefox profiles are often newer than Playwright's bundled
        Firefox, so we import cookies into a fresh Playwright profile instead
        of launching the raw on-disk profile directory.
        """
        if not profile:
            return {"error": "profile is required (name, alias like 'lace'/'original', or path)"}

        from .firefox_cookies import auth_relevant_cookies, extract_firefox_cookies
        from .firefox_profiles import resolve_firefox_profile

        close_identity_sessions(self._require_identity(), storage_root=self._storage_root)

        resolved = resolve_firefox_profile(profile)
        # Prefer Chromium for cookie replay reliability; allow firefox if asked.
        engine = (browser_type or "chromium").lower()
        if engine == "firefox":
            # Playwright's Firefox cannot open modern system profiles; still allow
            # cookie import into a fresh Playwright Firefox profile.
            pass
        self._browser_type = engine if engine in {"chromium", "firefox", "webkit"} else "chromium"
        self._user_profile_dir = None  # fresh Playwright profile + imported cookies
        if headless is not None:
            self._headless = bool(headless)

        all_cookies = extract_firefox_cookies(resolved.path)
        auth_cookies = auth_relevant_cookies(resolved.path)

        state = ensure_session(
            self._require_identity(),
            storage_root=self._storage_root,
            headless=self._headless,
            execution_scope=_EXECUTION_SCOPE.get(),
            allow_private_network=self._allow_private_network,
            browser_type=self._browser_type,
            user_profile_dir=None,
        )

        # Seed cookies into the fresh context.
        # Playwright accepts bulk add_cookies before navigation when domain is set.
        sample_errors: list[str] = []

        def _seed() -> dict[str, Any]:
            added = 0
            skipped = 0
            with state.lock:
                # Prefer auth-related cookies first, then the rest.
                ordered = list(auth_cookies) + [
                    c for c in all_cookies if c not in auth_cookies
                ]
                # Try bulk insert in chunks — more reliable than one-by-one for some engines.
                chunk: list[dict] = []
                for cookie in ordered:
                    chunk.append(cookie)
                    if len(chunk) >= 50:
                        try:
                            state.context.add_cookies(chunk)
                            added += len(chunk)
                        except Exception as exc:
                            # Fall back to per-cookie
                            for one in chunk:
                                try:
                                    state.context.add_cookies([one])
                                    added += 1
                                except Exception as exc2:
                                    skipped += 1
                                    if len(sample_errors) < 5:
                                        sample_errors.append(
                                            f"{one.get('domain')} {one.get('name')}: {exc2}"
                                        )
                        chunk = []
                if chunk:
                    try:
                        state.context.add_cookies(chunk)
                        added += len(chunk)
                    except Exception:
                        for one in chunk:
                            try:
                                state.context.add_cookies([one])
                                added += 1
                            except Exception as exc2:
                                skipped += 1
                                if len(sample_errors) < 5:
                                    sample_errors.append(
                                        f"{one.get('domain')} {one.get('name')}: {exc2}"
                                    )
                return {
                    "ok": True,
                    "cookies_seen": len(all_cookies),
                    "cookies_added": added,
                    "cookies_skipped": skipped,
                    "auth_cookie_names": sorted({c["name"] for c in auth_cookies}),
                    "sample_errors": sample_errors,
                }

        seeded = run_in_browser_thread(_seed)
        return {
            "ok": True,
            "mode": "cookie_import",
            "browser_type": self._browser_type,
            "profile_name": resolved.name,
            "source_path": str(resolved.path),
            "copied": False,
            "locked": False,
            "headless": state.headless,
            "session_started": state.started,
            **seeded,
            "note": (
                "Imported cookies from the Firefox profile into a fresh Playwright "
                "browser (system Firefox profiles are incompatible with Playwright's "
                "bundled Firefox binary)."
            ),
        }

    def _eval_js(self, expression: str = "", **_: Any) -> dict[str, Any]:
        if not expression:
            return {"error": "expression is required"}

        def _do() -> dict[str, Any]:
            state = self._session()
            if state is None or state.page is None:
                return {"error": "no open browser session — call browser.open or browser.use_profile first"}
            with state.lock:
                value = state.page.evaluate(expression)
                return {"ok": True, "url": state.page.url, "result": value}

        return run_in_browser_thread(_do)


    def _screenshot(self, full_page: bool = False, **_: Any) -> dict[str, Any]:
        """Take a screenshot of the current page."""
        def _do() -> dict[str, Any]:
            state = self._session()
            if state is None or state.page is None:
                return {"error": "no open browser session — call browser.open or browser.use_profile first"}
            import os
            import tempfile
            from datetime import datetime

            # Create screenshot directory
            screenshot_dir = os.path.join(self._storage_root, self._require_identity(), "screenshots")
            os.makedirs(screenshot_dir, exist_ok=True)

            # Generate filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{timestamp}.png"
            filepath = os.path.join(screenshot_dir, filename)

            with state.lock:
                state.page.screenshot(path=filepath, full_page=full_page)

            return {"ok": True, "path": filepath, "full_page": full_page}

        return run_in_browser_thread(_do)

    # ─── Multi-tab Handlers ──────────────────────────────────────────────

    def _new_tab(self, url: str = "about:blank", **_: Any) -> dict[str, Any]:
        return new_tab(
            self._require_identity(),
            storage_root=self._storage_root,
            execution_scope=_EXECUTION_SCOPE.get(),
            browser_type=self._browser_type,
            user_profile_dir=self._user_profile_dir,
            url=url,
        )

    def _switch_tab(self, tab_id: str, **_: Any) -> dict[str, Any]:
        return switch_tab(
            self._require_identity(),
            tab_id,
            storage_root=self._storage_root,
            execution_scope=_EXECUTION_SCOPE.get(),
            browser_type=self._browser_type,
            user_profile_dir=self._user_profile_dir,
        )

    def _close_tab(self, tab_id: str, **_: Any) -> dict[str, Any]:
        return close_tab(
            self._require_identity(),
            tab_id,
            storage_root=self._storage_root,
            execution_scope=_EXECUTION_SCOPE.get(),
            browser_type=self._browser_type,
            user_profile_dir=self._user_profile_dir,
        )

    def _list_tabs(self, **_: Any) -> list[dict]:
        return list_tabs(
            self._require_identity(),
            storage_root=self._storage_root,
            execution_scope=_EXECUTION_SCOPE.get(),
            browser_type=self._browser_type,
            user_profile_dir=self._user_profile_dir,
        )

    # ─── Checkpoint Handlers ─────────────────────────────────────────────

    def _checkpoint_create(self, **_: Any) -> dict[str, Any]:
        return create_checkpoint(
            self._require_identity(),
            storage_root=self._storage_root,
            execution_scope=_EXECUTION_SCOPE.get(),
            browser_type=self._browser_type,
            user_profile_dir=self._user_profile_dir,
        )

    def _checkpoint_restore(self, checkpoint_index: int = -1, **_: Any) -> dict[str, Any]:
        return restore_checkpoint(
            self._require_identity(),
            checkpoint_index,
            storage_root=self._storage_root,
            execution_scope=_EXECUTION_SCOPE.get(),
            browser_type=self._browser_type,
            user_profile_dir=self._user_profile_dir,
        )

    def _checkpoint_list(self, **_: Any) -> list[dict]:
        return list_checkpoints(
            self._require_identity(),
            storage_root=self._storage_root,
            execution_scope=_EXECUTION_SCOPE.get(),
            browser_type=self._browser_type,
            user_profile_dir=self._user_profile_dir,
        )

    def _session_export(self, **_: Any) -> dict[str, Any]:
        return export_session(
            self._require_identity(),
            storage_root=self._storage_root,
            execution_scope=_EXECUTION_SCOPE.get(),
            browser_type=self._browser_type,
            user_profile_dir=self._user_profile_dir,
        )

    def _session_import(self, session_data: dict, **_: Any) -> dict[str, Any]:
        return import_session(
            self._require_identity(),
            session_data,
            storage_root=self._storage_root,
            execution_scope=_EXECUTION_SCOPE.get(),
            browser_type=self._browser_type,
            user_profile_dir=self._user_profile_dir,
        )

    # ─── Live Browser Mode Handlers (Firefox WebExtension Bridge) ────────────

    def _live_status(self, **_: Any) -> dict[str, Any]:
        """Check live browser bridge connection status."""
        return _call_native("status")

    def _live_list_tabs(self, **_: Any) -> list[dict]:
        """List all tabs in the user's live Firefox browser."""
        result = _call_native("list_tabs")
        if isinstance(result, dict) and "error" in result:
            return [{"error": result["error"]}]
        return result

    def _live_active_tab(self, **_: Any) -> dict[str, Any]:
        """Get the currently active tab in the live browser."""
        return _call_native("active_tab")

    def _live_activate_tab(self, tab_id: str, **_: Any) -> dict[str, Any]:
        """Activate a specific tab in the user's real Firefox browser."""
        return _call_native("activate_tab", tabId=tab_id)

    def _live_create_tab(self, url: str = "about:blank", **_: Any) -> dict[str, Any]:
        """Create a new tab in the user's real Firefox browser."""
        return _call_native("create_tab", url=url)

    def _live_close_tab(self, tab_id: str, **_: Any) -> dict[str, Any]:
        """Close a specific tab in the user's real Firefox browser."""
        return _call_native("close_tab", tabId=tab_id)

    def _live_snapshot(self, tab_id: str, max_chars: int = 4000, **_: Any) -> dict[str, Any]:
        """Get a snapshot of a live browser tab."""
        return _call_native("snapshot", tabId=tab_id, maxChars=max_chars)

    def _live_navigate(self, tab_id: str, url: str, wait_until: str = "domcontentloaded", **_: Any) -> dict[str, Any]:
        """Navigate a tab in the user's real Firefox browser to a URL."""
        return _call_native("navigate", tabId=tab_id, url=url, waitUntil=wait_until)

    def _live_click(self, tab_id: str, selector: str, **_: Any) -> dict[str, Any]:
        """Click an element in a tab of the user's real Firefox browser."""
        return _call_native("click", tabId=tab_id, selector=selector)

    def _live_fill(self, tab_id: str, selector: str, value: str, **_: Any) -> dict[str, Any]:
        """Fill a form field in a tab of the user's real Firefox browser."""
        return _call_native("fill", tabId=tab_id, selector=selector, value=value)

    def _live_type(self, tab_id: str, selector: str, text: str, **_: Any) -> dict[str, Any]:
        """Type text into an element in a tab of the user's real Firefox browser."""
        return _call_native("type", tabId=tab_id, selector=selector, text=text)

    def _live_press(self, tab_id: str, key: str, **_: Any) -> dict[str, Any]:
        """Press a keyboard key in a tab of the user's real Firefox browser."""
        return _call_native("press", tabId=tab_id, key=key)



    def _plan_next_action(self, obs: dict, goal: str, steps: list, allowed_effects: list) -> dict:
        """
        Heuristic-based planner for autonomous browser tasks.

        Returns an action plan dict with:
        - action: skill name to execute
        - params: parameters for the skill
        - reasoning: explanation of why this action
        - expected_effect: effect classification
        """
        import re

        url = obs.get("url", "").lower()
        title = obs.get("title", "").lower()
        interactive = obs.get("interactive", [])
        goal_lower = goal.lower()

        # If no browser session, open one
        if not url or url in ("about:blank", "about:newtab"):
            return {
                "action": "browser.open",
                "params": {"url": "https://www.google.com"},
                "reasoning": "No active browser session, opening default page",
                "expected_effect": "READ_ONLY"
            }

        # Check if we need to navigate to a URL mentioned in the goal
        # This should be done early, before trying to click things
        url_matches = re.findall(r'https?://[^\s]+', goal)
        for target_url in url_matches:
            if target_url.lower() not in url:
                return {
                    "action": "browser.navigate",
                    "params": {"url": target_url},
                    "reasoning": f"Navigating to target URL from goal: {target_url}",
                    "expected_effect": "READ_ONLY"
                }

        # Also check for bare domain names in the goal
        bare_matches = re.findall(r'\b([a-zA-Z0-9.-]+\.(?:com|org|net|io|dev|app|co|ai))\b', goal)
        for domain in bare_matches:
            if domain.lower() not in url:
                return {
                    "action": "browser.navigate",
                    "params": {"url": f"https://{domain}"},
                    "reasoning": f"Navigating to target domain from goal: {domain}",
                    "expected_effect": "READ_ONLY"
                }

        # Goal: search for something (check before click/find to prioritize explicit search)
        if "search" in goal_lower:
            # Check if we just did a search and have results - then navigate to top result
            if steps and len(steps) > 0:
                last_step = steps[-1]
                if (last_step.get("action") == "browser.search" and
                    last_step.get("success") and
                    last_step.get("result", {}).get("recommended")):
                    # Navigate to the first recommended result
                    recommended = last_step["result"]["recommended"]
                    if recommended:
                        top_result = recommended[0]
                        return {
                            "action": "browser.navigate",
                            "params": {"url": top_result["url"]},
                            "reasoning": f"Navigating to top search result: {top_result['title'][:50]}",
                            "expected_effect": "READ_ONLY"
                        }

            # Extract search query from goal
            search_query = goal
            for prefix in ["search for ", "search "]:
                if prefix in goal_lower:
                    search_query = goal_lower.split(prefix, 1)[1]
                    break
            return {
                "action": "browser.search",
                "params": {"query": search_query, "task": goal},
                "reasoning": f"Searching for: {search_query}",
                "expected_effect": "READ_ONLY"
            }

        # Goal: find/click a specific link/text
        if "click" in goal_lower or "find" in goal_lower or "learn more" in goal_lower:
            # Look for matching elements
            for el in interactive:
                name = el.get("name", "").lower()
                role = el.get("role", "").lower()
                # Check for "learn more", "more", "continue", "next", etc.
                if any(keyword in name for keyword in ["learn more", "more", "continue", "next", "read more", "view more"]):
                    return {
                        "action": "browser.click",
                        "params": {"selector": el.get("selector", "")},
                        "reasoning": f"Found target link: {el.get('name', '')}",
                        "expected_effect": "REVERSIBLE"
                    }

        # Goal: login
        if "login" in goal_lower or "sign in" in goal_lower:
            # Look for login form elements
            for el in interactive:
                if el.get("type") in ("password", "email", "username") or "login" in el.get("name", "").lower() or "sign in" in el.get("name", "").lower():
                    return {
                        "action": "browser.click",
                        "params": {"selector": el.get("selector", "")},
                        "reasoning": "Found login form element",
                        "expected_effect": "REVERSIBLE"
                    }

        # Default: if we have interactive elements, click the first meaningful one
        for el in interactive:
            name = el.get("name", "").strip()
            if name and len(name) > 2 and el.get("role") in ("link", "button", "menuitem"):
                return {
                    "action": "browser.click",
                    "params": {"selector": el.get("selector", "")},
                    "reasoning": f"Exploring: clicking {name}",
                    "expected_effect": "REVERSIBLE"
                }

        # No action found - might be complete
        return {
            "action": "COMPLETE",
            "reasoning": "No further actions identified, goal may be complete",
        }

    def detect_loop(self, action_history: list) -> bool:
        """Detect if the same action is being repeated with the same result."""
        if len(action_history) < 4:
            return False
        # Check last 4 actions for pattern
        recent = action_history[-4:]
        # Simple loop: same action + same target + same result repeated
        if len(set((a["action"], a["target"][:50], a["result"]) for a in recent)) == 1:
            return True
        # Alternating loop: A-B-A-B pattern
        if len(recent) >= 4:
            if (recent[0]["action"] == recent[2]["action"] and
                recent[1]["action"] == recent[3]["action"] and
                recent[0]["target"][:50] == recent[2]["target"][:50] and
                recent[1]["target"][:50] == recent[3]["target"][:50]):
                return True
        return False

    # Autonomous Task Handler

    def _autonomous_task(
        self,
        goal: str = "",
        max_steps: int = 50,
        allowed_effects: list = None,
        stop_conditions: list = None,
        timeout_seconds: int = 300,
        **_: Any,
    ) -> dict[str, Any]:
        """
        Execute an autonomous browser task from a high-level goal.

        Implements the observe->reason->act->verify loop:
        1. Observe browser state (snapshot, tabs, URL)
        2. Reason about next action based on goal
        3. Act (execute browser skill)
        4. Verify result
        5. Record evidence
        6. Checkpoint
        7. Loop detection / escalation / completion
        """
        import time
        import hashlib

        start_time = time.time()
        allowed_effects = allowed_effects or ["READ_ONLY", "REVERSIBLE"]
        stop_conditions = stop_conditions or []
        task_id = hashlib.md5(f"{goal}{time.time()}".encode()).hexdigest()[:8]
        steps = []
        action_history = []  # For loop detection
        consecutive_failures = 0

        # Helper: get compact observation
        def get_observation():
            snap = self._snapshot()
            if snap.get("error"):
                return {"error": snap.get("error"), "url": "", "title": "", "interactive": []}
            # Compute hash for loop detection
            obs_text = f"{snap.get('url','')}|{snap.get('title','')}|{len(snap.get('interactive',[]))}"
            obs_hash = hashlib.sha256(obs_text.encode()).hexdigest()[:16]
            return {
                "url": snap.get("url", ""),
                "title": snap.get("title", ""),
                "interactive_count": snap.get("interactive_count", 0),
                "interactive": snap.get("interactive", [])[:20],  # Limit for token budget
                "obs_hash": obs_hash,
            }

        # Helper: classify action effect
        def classify_effect(action, params):
            if action in ("browser.open", "browser.navigate", "browser.snapshot", "browser.status", "browser.list_tabs", "browser.eval_js"):
                return "READ_ONLY"
            if action in ("browser.click", "browser.type", "browser.fill", "browser.press", "browser.wait"):
                return "REVERSIBLE"
            if action in ("browser.upload_file", "browser.download", "browser.screenshot", "browser.analyze_screenshot"):
                return "EXTERNAL_SIDE_EFFECT"
            if action in ("browser.login", "browser.use_profile"):
                return "CONSEQUENTIAL"
            return "REVERSIBLE"

        # Helper: check if goal is achieved (simple heuristic)
        def check_goal_complete(obs, goal, steps=None):
            goal_lower = goal.lower()
            if not obs or obs.get("error"):
                return False
            url = obs.get("url", "").lower()
            current = urlparse(url)
            current_host = (current.hostname or "").casefold()

            def host_matches(domain: str) -> bool:
                domain = domain.casefold().lstrip(".")
                return current_host == domain or current_host.endswith(f".{domain}")

            # Compute flags early for use in all URL checks
            has_click_find = ("find" in goal_lower or "click" in goal_lower)
            has_go_navigate = ("go to" in goal_lower or "navigate" in goal_lower)
            pure_navigation = not (has_click_find and has_go_navigate)

            # Check for explicit completion indicators
            if "stop" in goal_lower or "done" in goal_lower or "complete" in goal_lower:
                page_text = str(obs.get("interactive", [])).lower()
                if any(w in page_text for w in ["done", "complete", "finished", "success", "submitted"]):
                    return True

            # URL-based conditions (specific known sites) - only if pure navigation
            if pure_navigation:
                if "github" in goal_lower and current_host in {"github.com", "www.github.com"}:
                    return True
                if "login" in goal_lower and ("dashboard" in url or "home" in url or "account" in url):
                    return True

            # Generic: if goal mentions a specific URL/path and we're there
            import re
            url_matches = re.findall(r'https?://[^\s]+', goal)
            for target_url in url_matches:
                target = urlparse(target_url.rstrip(".,;"))
                target_host = (target.hostname or "").casefold()
                target_path = target.path.rstrip("/")
                current_path = current.path.rstrip("/")
                if (
                    pure_navigation
                    and target_host
                    and host_matches(target_host)
                    and (not target_path or current_path == target_path or current_path.startswith(f"{target_path}/"))
                ):
                    return True

            # Generic: if goal mentions a domain and we're on that domain
            bare_matches = re.findall(r'\b([a-zA-Z0-9.-]+\.(?:com|org|net|io|dev|app|co|ai))\b', goal)
            for domain in bare_matches:
                if host_matches(domain) and pure_navigation:
                    return True

            # Check for "click X" or "find X" patterns - if we've clicked a matching element
            if steps and len(steps) > 0:
                last_step = steps[-1]
                if last_step.get("success") and last_step.get("action") in ("browser.click", "browser.navigate"):
                    # If goal says "find X" or "click X" and we successfully clicked/navigated
                    if has_click_find:
                        if not has_go_navigate:
                            # Simple click/find goal - done after click
                            return True
                        else:
                            # Compound goal: "go to X and click Y"
                            # Check if we've done both: navigated to target URL AND clicked something
                            # Look for a navigate step to the target URL followed by a click
                            target_urls = re.findall(r'https?://[^\s]+', goal)
                            for target_url in target_urls:
                                # Check if we have a navigate to target_url followed by a click
                                for i in range(len(steps) - 1):
                                    if (steps[i].get("action") == "browser.navigate" and
                                        steps[i].get("success") and
                                        target_url.lower() in steps[i].get("params", {}).get("url", "").lower()):
                                        if (steps[i+1].get("action") == "browser.click" and
                                            steps[i+1].get("success")):
                                            return True
                            # Also check bare domains
                            bare_matches = re.findall(r'([a-zA-Z0-9.-]+\.(?:com|org|net|io|dev|app|co|ai))', goal)
                            for domain in bare_matches:
                                for i in range(len(steps) - 1):
                                    if (steps[i].get("action") == "browser.navigate" and
                                        steps[i].get("success") and
                                        domain.lower() in steps[i].get("params", {}).get("url", "").lower()):
                                        if (steps[i+1].get("action") == "browser.click" and
                                            steps[i+1].get("success")):
                                            return True

            # Check for "click X to go to Y" pattern

            # Check for "click X to go to Y" pattern
            if "click" in goal_lower and "page" in goal_lower:
                page_match = re.search(r'page\s+(\d+)', goal_lower)
                if page_match:
                    target_page = page_match.group(1)
                    if f"page/{target_page}" in url or f"page/{target_page}/" in url:
                        return True

            return False

        # Main autonomous loop
        step = 0
        while step < max_steps:
            step += 1
            elapsed = time.time() - start_time
            if elapsed > timeout_seconds:
                return {"ok": False, "task_id": task_id, "error": f"Timeout after {timeout_seconds}s", "steps": steps}

            # 1. OBSERVE
            obs = get_observation()
            if obs.get("error"):
                # If no browser session, auto-open one
                if "no open browser session" in obs["error"].lower():
                    open_result = self._call("browser.open", url="https://www.google.com")
                    if not open_result.success:
                        return {"ok": False, "task_id": task_id, "error": f"Failed to auto-open browser: {open_result.error}", "steps": steps}
                    steps.append({
                        "step": step,
                        "action": "browser.open",
                        "params": {"url": "https://www.google.com"},
                        "success": True,
                        "result": "Auto-opened browser for task",
                        "timestamp": time.time(),
                    })
                    # Re-observe
                    obs = get_observation()
                    if obs.get("error"):
                        return {"ok": False, "task_id": task_id, "error": f"Observation failed after auto-open: {obs['error']}", "steps": steps}
                else:
                    return {"ok": False, "task_id": task_id, "error": f"Observation failed: {obs['error']}", "steps": steps}

            # Check goal completion
            if check_goal_complete(obs, goal, steps):
                return {
                    "ok": True,
                    "task_id": task_id,
                    "goal": goal,
                    "completed": True,
                    "final_url": obs.get("url"),
                    "final_title": obs.get("title"),
                    "steps_taken": step - 1,
                    "steps": steps,
                    "duration_seconds": time.time() - start_time,
                }

            # Check stop conditions
            for cond in stop_conditions:
                if cond.lower() in str(obs).lower():
                    return {
                        "ok": True,
                        "task_id": task_id,
                        "goal": goal,
                        "completed": True,
                        "stop_condition_met": cond,
                        "final_url": obs.get("url"),
                        "steps": steps,
                    }

            # 2. REASON - Use the model to decide next action
            # Build a compact prompt for the planner
            # Build a compact prompt for the planner
            obs_summary = (
                f"URL: {obs.get('url', '')}\n"
                f"Title: {obs.get('title', '')}\n"
                f"Interactive elements: {obs.get('interactive_count', 0)}\n"
            )
            for el in obs.get("interactive", [])[:15]:
                obs_summary += f"  - {el.get('role', '')}: {el.get('name', '')} ({el.get('selector', '')})\n"


            # Build planner prompt
            planner_prompt = f"""You are an autonomous browser agent. Your goal: {goal}

Current page state:
{obs_summary}

Available actions: browser.open, browser.navigate, browser.click, browser.type, browser.fill, browser.press, browser.wait, browser.eval_js, browser.screenshot, browser.analyze_screenshot, browser.upload_file, browser.download, browser.list_tabs, browser.switch_tab, browser.new_tab, browser.close_tab, browser.checkpoint_create, browser.checkpoint_restore

Previous actions (last 5):
{json.dumps(steps[-5:], indent=2) if steps else "None"}

Allowed effects: {allowed_effects}

Decide the NEXT SINGLE ACTION. Return JSON with:
{{
  "action": "skill_name",
  "params": {{{...}}},
  "reasoning": "why this action",
  "expected_effect": "READ_ONLY|REVERSIBLE|EXTERNAL_SIDE_EFFECT|CONSEQUENTIAL"
}}

If the goal appears complete, return {{"action": "COMPLETE", "reasoning": "..."}}
If you need human help, return {{"action": "ESCALATE", "escalation_type": "...", "reason": "..."}}"""

            # Call the model to decide next action
            # For now, use a simple heuristic-based planner
            # TODO: Replace with actual model call via adapter
            action_plan = self._plan_next_action(obs, goal, steps, allowed_effects)

            if action_plan.get("action") == "COMPLETE":
                return {
                    "ok": True,
                    "task_id": task_id,
                    "goal": goal,
                    "completed": True,
                    "final_url": obs.get("url"),
                    "final_title": obs.get("title"),
                    "steps_taken": step - 1,
                    "steps": steps,
                    "reasoning": action_plan.get("reasoning"),
                    "duration_seconds": time.time() - start_time,
                }

            if action_plan.get("action") == "ESCALATE":
                # Call wait_for_human
                esc_result = self._wait_for_human(
                    reason=action_plan.get("reason", "Planner requested escalation"),
                    escalation_type=action_plan.get("escalation_type", "UNKNOWN_BLOCKER"),
                )
                # After escalation, continue loop
                continue

            # 3. ACT
            action = action_plan.get("action")
            params = action_plan.get("params", {})
            expected_effect = action_plan.get("expected_effect", "REVERSIBLE")

            # Check if effect is allowed
            if expected_effect not in allowed_effects:
                steps.append({
                    "step": step,
                    "action": action,
                    "params": params,
                    "success": False,
                    "error": f"Effect {expected_effect} not allowed. Allowed: {allowed_effects}",
                })
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    return {"ok": False, "task_id": task_id, "error": "Too many consecutive failures", "steps": steps}
                continue

            # Execute the action
            try:
                result = self._call(action, **params)
                success = result.success
                result_data = result.data if success else result.error
            except Exception as e:
                success = False
                result_data = str(e)

            # 4. VERIFY
            post_obs = get_observation()
            state_changed = post_obs.get("obs_hash") != obs.get("obs_hash")

            # Record step
            step_record = {
                "step": step,
                "action": action,
                "params": params,
                "expected_effect": expected_effect,
                "reasoning": action_plan.get("reasoning"),
                "success": success,
                "result": result_data,
                "state_changed": state_changed,
                "post_url": post_obs.get("url"),
                "post_title": post_obs.get("title"),
                "timestamp": time.time(),
            }
            steps.append(step_record)

            # Track for loop detection
            action_history.append({
                "action": action,
                "target": str(params)[:100],
                "result": "success" if success else "failure",
            })
            if len(action_history) > 10:
                action_history.pop(0)

            if self.detect_loop(action_history):
                return {
                    "ok": False,
                    "task_id": task_id,
                    "error": "Loop detected: same action repeated with same result",
                    "steps": steps,
                }

            if not success:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    return {"ok": False, "task_id": task_id, "error": "Too many consecutive failures", "steps": steps}
            else:
                consecutive_failures = 0

            # Checkpoint every 5 steps
            if step % 5 == 0:
                self._checkpoint_create()

        return {
            "ok": False,
            "task_id": task_id,
            "error": f"Max steps ({max_steps}) reached without completing goal",
            "steps": steps,
            "duration_seconds": time.time() - start_time,
        }

    def _wait_for_human(
        self,
        reason: str = "",
        escalation_type: str = "UNKNOWN_BLOCKER",
        screenshot_path: str = "",
        resume_token: str = "",
        **_: Any,
    ) -> dict[str, Any]:
        """
        Pause autonomous execution and wait for human intervention.

        Creates a structured escalation record that can be persisted and resumed.
        """
        import time
        import hashlib
        import uuid

        escalation_id = str(uuid.uuid4())[:8]
        timestamp = time.time()

        # Take screenshot if not provided
        if not screenshot_path:
            snap = self._screenshot(full_page=True)
            screenshot_path = snap.get("path", "") if snap.get("ok") else ""

        escalation = {
            "escalation_id": escalation_id,
            "task_id": getattr(self, "_current_task_id", "unknown"),
            "type": escalation_type,
            "reason": reason,
            "url": self._snapshot().get("url", ""),
            "screenshot_path": screenshot_path,
            "resume_token": resume_token or secrets.token_urlsafe(16),
            "created_at": timestamp,
            "status": "waiting",
        }

        # In a real implementation, this would:
        # 1. Persist escalation to storage
        # 2. Notify human (via CLI, webhook, etc.)
        # 3. Block until human signals resume
        # 4. Verify obstacle is resolved
        # 5. Resume task

        # For now, return the escalation record
        return {
            "ok": True,
            "escalation": escalation,
            "message": f"Escalation created: {escalation_type}. Waiting for human...",
        }
