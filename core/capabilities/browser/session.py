"""Persistent Playwright browser sessions keyed by identity.

Playwright's sync API cannot run inside an asyncio event loop (common under
pytest-asyncio / FastAPI). All sync Playwright work is confined to a dedicated
daemon thread with its own event loop semantics.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import queue
import re
import threading
from contextvars import copy_context
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from .url_policy import allow_browser_request

T = TypeVar("T")


class BrowserUnavailable(RuntimeError):
    """Raised when Playwright / Chromium is not installed."""


@dataclass
class SessionState:
    session_key: str
    identity_id: str
    execution_scope: str = "sdk"
    headless: bool = True
    allow_private_network: bool = False
    user_data_dir: Optional[Path] = None
    playwright: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    started: bool = False
    last_url: str = ""
    history: list[str] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)
    # Profile support
    browser_type: str = "chromium"  # chromium, firefox, webkit
    user_profile_dir: Optional[Path] = None  # Path to existing browser profile
    # Multi-tab support
    pages: dict[str, Any] = field(default_factory=dict)  # tab_id -> page
    active_tab_id: str = "main"
    # Checkpoint support
    checkpoints: list[dict] = field(default_factory=list)  # serialized checkpoints
    max_checkpoints: int = 10  # max checkpoints to keep

    # ─── Serialization ────────────────────────────────────────────────────
    def serialize_cookies(self) -> list[dict]:
        """Serialize cookies from the browser context."""
        if not self.context:
            return []
        try:
            cookies = self.context.cookies()
            return [
                {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c["domain"],
                    "path": c["path"],
                    "expires": c.get("expires"),
                    "httpOnly": c.get("httpOnly", False),
                    "secure": c.get("secure", False),
                    "sameSite": c.get("sameSite", "Lax"),
                }
                for c in cookies
            ]
        except Exception:
            return []

    def serialize_local_storage(self) -> dict:
        """Serialize localStorage from the active page."""
        if not self.page:
            return {}
        try:
            return self.page.evaluate("() => { const data = {}; for (let i = 0; i < localStorage.length; i++) { const key = localStorage.key(i); data[key] = localStorage.getItem(key); } return data; }")
        except Exception:
            return {}

    def serialize_session_storage(self) -> dict:
        """Serialize sessionStorage from the active page."""
        if not self.page:
            return {}
        try:
            return self.page.evaluate("() => { const data = {}; for (let i = 0; i < sessionStorage.length; i++) { const key = sessionStorage.key(i); data[key] = sessionStorage.getItem(key); } return data; }")
        except Exception:
            return {}

    def serialize_tabs(self) -> list[dict]:
        """Serialize all tabs."""
        tabs = []
        for tab_id, page in self.pages.items():
            if page == self.page:
                continue
            try:
                tabs.append({
                    "tab_id": tab_id,
                    "url": page.url,
                    "title": page.title(),
                })
            except Exception:
                pass
        # Add active tab
        if self.page:
            tabs.insert(0, {
                "tab_id": self.active_tab_id,
                "url": self.page.url,
                "title": self.page.title(),
            })
        return tabs

    def serialize_state(self) -> dict:
        """Serialize complete session state for checkpointing."""
        return {
            "timestamp": datetime.now().isoformat(),
            "session_key": self.session_key,
            "identity_id": self.identity_id,
            "last_url": self.last_url,
            "history": self.history[-100:],  # keep last 100
            "cookies": self.serialize_cookies(),
            "local_storage": self.serialize_local_storage(),
            "session_storage": self.serialize_session_storage(),
            "tabs": self.serialize_tabs(),
            "active_tab_id": self.active_tab_id,
        }

    def restore_cookies(self, cookies: list[dict]) -> None:
        """Restore cookies to the browser context."""
        if not self.context or not cookies:
            return
        try:
            self.context.add_cookies(cookies)
        except Exception:
            pass

    def restore_local_storage(self, data: dict) -> None:
        """Restore localStorage to the active page."""
        if not self.page or not data:
            return
        try:
            for key, value in data.items():
                self.page.evaluate(f"localStorage.setItem({json.dumps(key)}, {json.dumps(value)})")
        except Exception:
            pass

    def restore_session_storage(self, data: dict) -> None:
        """Restore sessionStorage to the active page."""
        if not self.page or not data:
            return
        try:
            for key, value in data.items():
                self.page.evaluate(f"sessionStorage.setItem({json.dumps(key)}, {json.dumps(value)})")
        except Exception:
            pass

    def create_checkpoint(self) -> dict:
        """Create a checkpoint of current session state."""
        import uuid
        checkpoint = self.serialize_state()
        checkpoint["checkpoint_id"] = str(uuid.uuid4())
        checkpoint["created_at"] = datetime.now().isoformat()
        self.checkpoints.append(checkpoint)
        # Trim old checkpoints
        if len(self.checkpoints) > self.max_checkpoints:
            self.checkpoints = self.checkpoints[-self.max_checkpoints:]
        return checkpoint

    def restore_checkpoint(self, checkpoint: dict) -> bool:
        """Restore session from a checkpoint."""
        try:
            if "cookies" in checkpoint:
                self.restore_cookies(checkpoint["cookies"])
            if "local_storage" in checkpoint:
                self.restore_local_storage(checkpoint["local_storage"])
            if "session_storage" in checkpoint:
                self.restore_session_storage(checkpoint["session_storage"])
            if "last_url" in checkpoint:
                self.last_url = checkpoint["last_url"]
            if "history" in checkpoint:
                self.history = checkpoint["history"]
            return True
        except Exception:
            return False


_SESSIONS: dict[str, SessionState] = {}
_GLOBAL_LOCK = threading.RLock()
_PLAYWRIGHT: Any = None

# Dedicated worker so sync Playwright never touches a foreign asyncio loop.
_JOBS: queue.Queue = queue.Queue()
_WORKER_READY = threading.Event()


def _worker_main() -> None:
    _WORKER_READY.set()
    while True:
        item = _JOBS.get()
        if item is None:
            break
        fn, fut = item
        try:
            fut.set_result(fn())
        except BaseException as exc:  # noqa: BLE001 — propagate to caller
            fut.set_exception(exc)


_WORKER = threading.Thread(target=_worker_main, name="identityos-playwright", daemon=True)
_WORKER.start()
_WORKER_READY.wait(timeout=5)


def run_in_browser_thread(fn: Callable[[], T]) -> T:
    """Execute ``fn`` on the Playwright worker thread and return its result."""
    if threading.current_thread() is _WORKER:
        return fn()
    fut: Future = Future()
    try:
        context = copy_context()
        _JOBS.put((lambda: context.run(fn), fut))
    except Exception:
        # copy_context() may fail in greenlet/gevent environments
        _JOBS.put((fn, fut))
    return fut.result(timeout=120)


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as exc:
        raise BrowserUnavailable(
            "Playwright is not installed. Run: "
            'pip install "playwright>=1.40" && playwright install chromium'
        ) from exc
    return sync_playwright


def _shared_playwright() -> Any:
    """Return the worker thread's single Playwright driver instance.

    The synchronous Playwright API owns an asyncio loop internally and cannot
    be started a second time on the same thread while another driver is live.
    Browser contexts remain independent; only the process-level driver is
    shared.
    """
    global _PLAYWRIGHT
    if _PLAYWRIGHT is None:
        _PLAYWRIGHT = _import_playwright()().start()
    return _PLAYWRIGHT


def _stop_shared_playwright() -> None:
    global _PLAYWRIGHT
    driver, _PLAYWRIGHT = _PLAYWRIGHT, None
    if driver is not None:
        try:
            driver.stop()
        except Exception:
            pass


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    if cleaned and cleaned == value and value not in {".", ".."}:
        return cleaned
    digest = hashlib.sha256(value.encode()).hexdigest()[:16]
    return f"identity-{digest}"


def session_dir(
    storage_root: str | Path,
    identity_id: str,
    execution_scope: str = "sdk",
) -> Path:
    root = identity_browser_dir(storage_root, identity_id)
    if execution_scope and execution_scope != "sdk":
        scope_hash = hashlib.sha256(execution_scope.encode()).hexdigest()
        root = root / "scopes" / scope_hash
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def identity_browser_dir(storage_root: str | Path, identity_id: str) -> Path:
    """Return the bounded browser-state directory without creating it."""
    return Path(storage_root).resolve() / _safe_component(identity_id) / "browser"


def checkpoint_dir(
    storage_root: str | Path,
    identity_id: str,
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> Path:
    """Return the checkpoint directory for a session."""
    base = session_dir(storage_root, identity_id, execution_scope)
    if browser_type != "chromium":
        base = base / f"browser-{browser_type}"
    if user_profile_dir:
        profile_hash = hashlib.sha256(str(user_profile_dir).encode()).hexdigest()[:8]
        base = base / f"profile-{profile_hash}"
    checkpoint_path = base / "checkpoints"
    checkpoint_path.mkdir(parents=True, exist_ok=True)
    try:
        checkpoint_path.chmod(0o700)
    except OSError:
        pass
    return checkpoint_path


def _checkpoint_file(checkpoint_dir: Path, checkpoint_id: str) -> Path:
    """Return the checkpoint file path."""
    return checkpoint_dir / f"{checkpoint_id}.json"


def save_checkpoint_to_disk(
    identity_id: str,
    checkpoint: dict,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> str:
    """Save a checkpoint to disk and return the checkpoint ID."""
    import uuid
    checkpoint_id = checkpoint.get("checkpoint_id") or str(uuid.uuid4())
    checkpoint["checkpoint_id"] = checkpoint_id
    checkpoint["saved_at"] = datetime.now().isoformat()

    ckpt_dir = checkpoint_dir(storage_root, identity_id, execution_scope, browser_type, user_profile_dir)
    ckpt_file = _checkpoint_file(ckpt_dir, checkpoint_id)

    try:
        ckpt_file.write_text(json.dumps(checkpoint, default=str))
        # Keep only last 10 checkpoints on disk
        _prune_old_checkpoints(ckpt_dir, max_keep=10)
        return checkpoint_id
    except Exception as e:
        return ""


def _prune_old_checkpoints(checkpoint_dir: Path, max_keep: int = 10) -> None:
    """Remove old checkpoint files, keeping only the most recent."""
    try:
        files = sorted(checkpoint_dir.glob("*.json"), key=lambda f: f.stat().st_mtime)
        for f in files[:-max_keep]:
            f.unlink(missing_ok=True)
    except Exception:
        pass


def load_checkpoints_from_disk(
    identity_id: str,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> list[dict]:
    """Load all checkpoints from disk, newest first."""
    ckpt_dir = checkpoint_dir(storage_root, identity_id, execution_scope, browser_type, user_profile_dir)
    checkpoints = []
    try:
        for ckpt_file in sorted(ckpt_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                data = json.loads(ckpt_file.read_text())
                checkpoints.append(data)
            except Exception:
                continue
    except Exception:
        pass
    return checkpoints


def delete_checkpoint_from_disk(
    identity_id: str,
    checkpoint_id: str,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> bool:
    """Delete a specific checkpoint from disk."""
    ckpt_dir = checkpoint_dir(storage_root, identity_id, execution_scope, browser_type, user_profile_dir)
    ckpt_file = _checkpoint_file(ckpt_dir, checkpoint_id)
    try:
        ckpt_file.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _session_key(
    storage_root: str | Path,
    identity_id: str,
    execution_scope: str,
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> str:
    base = str(session_dir(storage_root, identity_id, execution_scope).resolve())
    if browser_type != "chromium":
        base = f"{base}-{browser_type}"
    if user_profile_dir:
        profile_hash = hashlib.sha256(str(user_profile_dir).encode()).hexdigest()[:8]
        base = f"{base}-profile-{profile_hash}"
    return base


def get_session(
    identity_id: str,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> Optional[SessionState]:
    key = _session_key(storage_root, identity_id, execution_scope, browser_type, user_profile_dir)
    with _GLOBAL_LOCK:
        return _SESSIONS.get(key)


def ensure_session(
    identity_id: str,
    *,
    storage_root: str | Path = ".identity_store",
    headless: bool = True,
    user_agent: Optional[str] = None,
    execution_scope: str = "sdk",
    allow_private_network: bool = False,
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> SessionState:
    """Start or reuse a browser session for this identity."""

    def _ensure() -> SessionState:
        with _GLOBAL_LOCK:
            key = _session_key(storage_root, identity_id, execution_scope, browser_type, user_profile_dir)
            existing = _SESSIONS.get(key)
            if existing and existing.started and existing.page is not None:
                if (
                    existing.headless == headless
                    and existing.allow_private_network == allow_private_network
                    and existing.browser_type == browser_type
                    and existing.user_profile_dir == user_profile_dir
                ):
                    return existing
                _SESSIONS.pop(key, None)
                _safe_close(existing)

            state = SessionState(
                session_key=key,
                identity_id=identity_id,
                execution_scope=execution_scope,
                headless=headless,
                allow_private_network=allow_private_network,
                user_data_dir=session_dir(storage_root, identity_id, execution_scope),
                browser_type=browser_type,
                user_profile_dir=user_profile_dir,
            )
            state.playwright = _shared_playwright()
            try:
                # Determine user data directory
                if state.user_profile_dir and state.user_profile_dir.exists():
                    # Use existing browser profile
                    profile_dir = state.user_profile_dir
                    state.user_profile_dir.mkdir(parents=True, exist_ok=True)
                else:
                    # Use internal profile directory
                    profile_dir = state.user_data_dir / "profile"
                    profile_dir.mkdir(parents=True, exist_ok=True)

                # Select browser type
                browser_launcher = getattr(state.playwright, state.browser_type)
                if browser_launcher is None:
                    raise BrowserUnavailable(f"Browser type '{state.browser_type}' not available")

                # Prepare launch kwargs — avoid Chromium-only flags on Firefox.
                launch_kwargs: dict[str, Any] = {
                    "user_data_dir": str(profile_dir),
                    "headless": headless,
                    "viewport": {"width": 1400, "height": 900},
                    "locale": "en-US",
                }
                if state.browser_type == "firefox":
                    # Real Firefox profiles already define UA; don't force Chrome UA.
                    if user_agent:
                        launch_kwargs["user_agent"] = user_agent
                    # Playwright Firefox accepts firefox_user_prefs
                    launch_kwargs["firefox_user_prefs"] = {
                        "dom.webdriver.enabled": False,
                        "useAutomationExtension": False,
                    }
                else:
                    launch_kwargs["user_agent"] = user_agent or (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                    )
                    launch_kwargs["args"] = ["--disable-blink-features=AutomationControlled"]

                # Launch persistent context
                state.context = browser_launcher.launch_persistent_context(**launch_kwargs)
                state.context.route(
                    "**/*",
                    lambda route: route.continue_()
                    if allow_browser_request(
                        route.request.url,
                        allow_private_network=allow_private_network,
                    )
                    else route.abort("blockedbyclient"),
                )
                state.page = state.context.pages[0] if state.context.pages else state.context.new_page()
                # Initialize multi-tab support
                state.pages = {state.active_tab_id: state.page}
                state.started = True

                # Load checkpoints from disk
                disk_checkpoints = load_checkpoints_from_disk(
                    identity_id,
                    storage_root=storage_root,
                    execution_scope=execution_scope,
                    browser_type=browser_type,
                    user_profile_dir=user_profile_dir,
                )
                if disk_checkpoints:
                    state.checkpoints = disk_checkpoints

                _SESSIONS[key] = state
                return state
            except Exception:
                _safe_close(state)
                raise

    return run_in_browser_thread(_ensure)


def _safe_close(state: SessionState) -> None:
    with state.lock:
        for attr in ("context", "browser"):
            obj = getattr(state, attr, None)
            if obj is None:
                continue
            try:
                if attr == "playwright":
                    obj.stop()
                else:
                    obj.close()
            except Exception:
                pass
            setattr(state, attr, None)
        state.playwright = None
        state.page = None
        state.started = False


def close_session(
    identity_id: str,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
) -> bool:
    def _close() -> bool:
        key = _session_key(storage_root, identity_id, execution_scope)
        with _GLOBAL_LOCK:
            state = _SESSIONS.pop(key, None)
        if state is None:
            return False
        _safe_close(state)
        return True

    return run_in_browser_thread(_close)


def close_identity_sessions(
    identity_id: str,
    *,
    storage_root: str | Path = ".identity_store",
) -> int:
    """Close every in-process session for an identity in one storage root."""
    root = Path(storage_root).resolve()

    def _close_all_matching() -> int:
        with _GLOBAL_LOCK:
            matches = [
                (key, state)
                for key, state in _SESSIONS.items()
                if state.identity_id == identity_id
                and state.user_data_dir is not None
                and root in state.user_data_dir.resolve().parents
            ]
            for key, _ in matches:
                _SESSIONS.pop(key, None)
        for _, state in matches:
            _safe_close(state)
        return len(matches)

    return run_in_browser_thread(_close_all_matching)


def close_all() -> None:
    with _GLOBAL_LOCK:
        states = list(_SESSIONS.values())
        _SESSIONS.clear()
    for state in states:
        run_in_browser_thread(lambda state=state: _safe_close(state))
    run_in_browser_thread(_stop_shared_playwright)


atexit.register(close_all)


# ─── Multi-tab Support ────────────────────────────────────────────────────

def new_tab(
    identity_id: str,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
    url: str = "about:blank",
) -> dict:
    """Create a new tab in the session."""
    def _new_tab() -> dict:
        state = get_session(identity_id, storage_root=storage_root, execution_scope=execution_scope, browser_type=browser_type, user_profile_dir=user_profile_dir)
        if not state or not state.started or not state.context:
            return {"success": False, "error": "No active session"}
        with state.lock:
            tab_id = f"tab-{len(state.pages) + 1}"
            page = state.context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            state.pages[tab_id] = page
            state.active_tab_id = tab_id
            state.page = page
            return {"success": True, "tab_id": tab_id, "url": url}
    return run_in_browser_thread(_new_tab)


def switch_tab(
    identity_id: str,
    tab_id: str,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> dict:
    """Switch to a different tab."""
    def _switch() -> dict:
        state = get_session(identity_id, storage_root=storage_root, execution_scope=execution_scope, browser_type=browser_type, user_profile_dir=user_profile_dir)
        if not state or not state.started:
            return {"success": False, "error": "No active session"}
        with state.lock:
            if tab_id not in state.pages:
                return {"success": False, "error": f"Tab {tab_id} not found"}
            state.active_tab_id = tab_id
            state.page = state.pages[tab_id]
            return {"success": True, "tab_id": tab_id}
    return run_in_browser_thread(_switch)


def close_tab(
    identity_id: str,
    tab_id: str,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> dict:
    """Close a specific tab."""
    def _close() -> dict:
        state = get_session(identity_id, storage_root=storage_root, execution_scope=execution_scope, browser_type=browser_type, user_profile_dir=user_profile_dir)
        if not state or not state.started:
            return {"success": False, "error": "No active session"}
        with state.lock:
            if tab_id not in state.pages:
                return {"success": False, "error": f"Tab {tab_id} not found"}
            if len(state.pages) <= 1:
                return {"success": False, "error": "Cannot close last tab"}
            page = state.pages.pop(tab_id)
            try:
                page.close()
            except Exception:
                pass
            # Switch to another tab if we closed the active one
            if state.active_tab_id == tab_id:
                state.active_tab_id = next(iter(state.pages))
                state.page = state.pages[state.active_tab_id]
            return {"success": True, "active_tab_id": state.active_tab_id}
    return run_in_browser_thread(_close)


def list_tabs(
    identity_id: str,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> list[dict]:
    """List all tabs in the session."""
    def _list() -> list[dict]:
        state = get_session(identity_id, storage_root=storage_root, execution_scope=execution_scope, browser_type=browser_type, user_profile_dir=user_profile_dir)
        if not state or not state.started:
            return []
        with state.lock:
            tabs = []
            for tab_id, page in state.pages.items():
                try:
                    tabs.append({
                        "tab_id": tab_id,
                        "url": page.url,
                        "title": page.title(),
                        "active": tab_id == state.active_tab_id,
                    })
                except Exception:
                    pass
            return tabs
    return run_in_browser_thread(_list)


# ─── Checkpoint Support ───────────────────────────────────────────────────

def create_checkpoint(
    identity_id: str,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> dict:
    """Create a checkpoint of the current session state."""
    def _create() -> dict:
        state = get_session(identity_id, storage_root=storage_root, execution_scope=execution_scope, browser_type=browser_type, user_profile_dir=user_profile_dir)
        if not state or not state.started:
            return {"success": False, "error": "No active session"}
        checkpoint = state.create_checkpoint()
        # Save to disk
        checkpoint_id = save_checkpoint_to_disk(
            identity_id, checkpoint,
            storage_root=storage_root,
            execution_scope=execution_scope,
            browser_type=browser_type,
            user_profile_dir=user_profile_dir,
        )
        checkpoint["checkpoint_id"] = checkpoint_id
        return {"success": True, "checkpoint": checkpoint}
    return run_in_browser_thread(_create)


def restore_checkpoint(
    identity_id: str,
    checkpoint_index: int = -1,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> dict:
    """Restore session from a checkpoint (default: latest)."""
    def _restore() -> dict:
        state = get_session(identity_id, storage_root=storage_root, execution_scope=execution_scope, browser_type=browser_type, user_profile_dir=user_profile_dir)
        if not state or not state.started:
            return {"success": False, "error": "No active session"}

        # Try in-memory checkpoints first, then disk
        checkpoint = None
        if state.checkpoints:
            checkpoint = state.checkpoints[checkpoint_index]
        else:
            # Load from disk
            disk_checkpoints = load_checkpoints_from_disk(
                identity_id,
                storage_root=storage_root,
                execution_scope=execution_scope,
                browser_type=browser_type,
                user_profile_dir=user_profile_dir,
            )
            if disk_checkpoints:
                checkpoint = disk_checkpoints[0 if checkpoint_index == -1 else checkpoint_index]

        if not checkpoint:
            return {"success": False, "error": "No checkpoints available"}

        success = state.restore_checkpoint(checkpoint)
        if not success:
            return {"success": False, "error": "Failed to restore checkpoint"}

        # Navigate to last_url if available
        last_url = checkpoint.get("last_url")
        if last_url:
            try:
                state.page.goto(last_url, wait_until="domcontentloaded", timeout=30000)
                state.last_url = state.page.url
                success = True
            except Exception as e:
                return {"success": False, "error": f"Checkpoint restored but navigation failed: {e}"}

        return {"success": success, "restored_url": last_url, "checkpoint_id": checkpoint.get("checkpoint_id")}
    return run_in_browser_thread(_restore)


def list_checkpoints(
    identity_id: str,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> list[dict]:
    """List available checkpoints for a session."""
    # First check in-memory
    state = get_session(identity_id, storage_root=storage_root, execution_scope=execution_scope, browser_type=browser_type, user_profile_dir=user_profile_dir)
    in_memory = []
    if state:
        in_memory = [
            {"index": i, "timestamp": cp.get("timestamp"), "url": cp.get("last_url"), "checkpoint_id": cp.get("checkpoint_id"), "source": "memory"}
            for i, cp in enumerate(state.checkpoints)
        ]

    # Then check disk
    disk_checkpoints = load_checkpoints_from_disk(
        identity_id,
        storage_root=storage_root,
        execution_scope=execution_scope,
        browser_type=browser_type,
        user_profile_dir=user_profile_dir,
    )
    disk = [
        {"index": i, "timestamp": cp.get("timestamp"), "url": cp.get("last_url"), "checkpoint_id": cp.get("checkpoint_id"), "source": "disk"}
        for i, cp in enumerate(disk_checkpoints)
    ]

    # Merge, preferring in-memory for same checkpoint_id
    seen = set()
    result = []
    for cp in in_memory + disk:
        cid = cp.get("checkpoint_id")
        if cid and cid not in seen:
            seen.add(cid)
            result.append(cp)

    return result


def export_session(
    identity_id: str,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> dict:
    """Export complete session state for backup/migration."""
    def _export() -> dict:
        state = get_session(identity_id, storage_root=storage_root, execution_scope=execution_scope, browser_type=browser_type, user_profile_dir=user_profile_dir)
        if not state or not state.started:
            return {"success": False, "error": "No active session"}
        return {"success": True, "session": state.serialize_state()}
    return run_in_browser_thread(_export)


def import_session(
    identity_id: str,
    session_data: dict,
    *,
    storage_root: str | Path = ".identity_store",
    execution_scope: str = "sdk",
    browser_type: str = "chromium",
    user_profile_dir: Optional[Path] = None,
) -> dict:
    """Import session state from backup."""
    def _import() -> dict:
        state = ensure_session(
            identity_id,
            storage_root=storage_root,
            execution_scope=execution_scope,
            browser_type=browser_type,
            user_profile_dir=user_profile_dir,
        )
        success = state.restore_checkpoint(session_data)
        return {"success": success}
    return run_in_browser_thread(_import)


def page_snapshot(page: Any, *, max_chars: int = 12000) -> dict[str, Any]:
    """Collect a person-usable page snapshot for agent reasoning."""
    title = ""
    url = ""
    try:
        title = page.title()
        url = page.url
    except Exception:
        pass

    text = ""
    try:
        text = page.inner_text("body")
    except Exception:
        try:
            text = page.content()
        except Exception:
            text = ""
    text = " ".join(text.split())
    if len(text) > max_chars:
        text = text[:max_chars] + "…"

    interactive: list[dict[str, Any]] = []
    try:
        elements = page.query_selector_all(
            "a[href], button, input, textarea, select, [role='button'], [role='link'], [contenteditable='true']"
        )
        for el in elements[:80]:
            try:
                tag = (el.evaluate("e => e.tagName") or "").lower()
                role = el.get_attribute("role") or tag
                name = (
                    el.get_attribute("aria-label")
                    or el.get_attribute("name")
                    or el.get_attribute("placeholder")
                    or el.get_attribute("title")
                    or (el.inner_text() or "")[:80]
                ).strip()
                itype = el.get_attribute("type") or ""
                href = el.get_attribute("href") or ""
                selector_hint = _selector_hint(el, tag)
                if not name and not href:
                    continue
                interactive.append(
                    {
                        "role": role,
                        "name": name[:120],
                        "type": itype,
                        "href": href[:300],
                        "selector": selector_hint,
                    }
                )
            except Exception:
                continue
    except Exception:
        pass

    return {
        "url": url,
        "title": title,
        "text": text,
        "interactive": interactive,
        "interactive_count": len(interactive),
    }


def _selector_hint(el: Any, tag: str) -> str:
    eid = el.get_attribute("id")
    if eid:
        return f"#{eid}"
    name = el.get_attribute("name")
    if name:
        return f'{tag}[name="{name}"]'
    aria = el.get_attribute("aria-label")
    if aria:
        return f'[aria-label="{aria[:60]}"]'
    placeholder = el.get_attribute("placeholder")
    if placeholder:
        return f'[placeholder="{placeholder[:60]}"]'
    return tag
