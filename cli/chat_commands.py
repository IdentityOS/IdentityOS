"""In-chat slash-command dispatcher.

Lets a user inspect and control an active chat session without killing it:
switch model/temperature/adapter mid-session, list or install capabilities,
checkpoint snapshots, and reset the session.  Commands are dispatched before
input reaches the runtime, so they add zero latency to normal messages.

When prompt_toolkit is available (and stdin/stdout are TTYs), typing "/"
immediately pops a live command menu: filter by typing, navigate with the
arrow keys, and press Enter to execute.  Without a terminal (piped input,
tests) it degrades to plain ``input()`` — bare "/" then prints the menu.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Iterable, Optional

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import ANSI as FTAnsi
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    _PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via fallback tests
    _PROMPT_TOOLKIT_AVAILABLE = False


class ChatContext:
    """Mutable per-session state shared by the REPL loop and chat commands."""

    def __init__(
        self,
        runtime: Any,
        manager: Any,
        storage: Any,
        identity_id: str,
        session_id: str,
        identity_name: str = "",
    ) -> None:
        self.runtime = runtime
        self.manager = manager
        self.storage = storage
        self.identity_id = identity_id
        self.identity_name = identity_name
        self.session_id = session_id
        self.turns = 0


# ── Adapter helpers ──────────────────────────────────────────────────────


def _leaf_adapters(adapter: Any) -> list[Any]:
    """Return the leaf adapters behind a ChainAdapter, or ``[adapter]``."""
    leaves = getattr(adapter, "_adapters", None)
    return list(leaves) if leaves else [adapter]


def _set_adapter_model(adapter: Any, model: str) -> None:
    """Set the model on the adapter actually being called, never on all of them.

    Setting one string on every leaf of a chain corrupts the fallbacks: a local
    Ollama tag becomes the model sent to Groq/Cerebras, which is a guaranteed
    404. Only the head (selected) adapter's model is meaningful.
    """
    leaves = _leaf_adapters(adapter)
    if leaves:
        leaves[0].model = model
    adapter.model = model


_PROVIDER_ALIASES: dict[str, tuple[str, ...]] = {
    "groq": ("groq",),
    "cerebras": ("cerebras",),
    "sambanova": ("sambanova",),
    "openrouter": ("openrouter",),
    "anthropic": ("anthropic", "claude"),
    "ollama": ("ollama",),
    "gemini": ("gemini",),
    "nvidia": ("nvidia", "nemotron"),
}


def _find_leaf_by_provider(adapter: Any, token: str) -> Optional[Any]:
    token = token.lower()
    keys = _PROVIDER_ALIASES.get(token, (token,))
    for leaf in _leaf_adapters(adapter):
        haystacks = (
            type(leaf).__name__.lower(),
            str(getattr(leaf, "base_url", "") or "").lower(),
        )
        if any(key in hay for key in keys for hay in haystacks):
            return leaf
    return None


def _promote_leaf(adapter: Any, leaf: Any) -> None:
    """Move *leaf* to the front of a chain so it is tried first."""
    leaves = _leaf_adapters(adapter)
    if leaf not in leaves:
        return
    leaves.remove(leaf)
    leaves.insert(0, leaf)
    if hasattr(adapter, "_adapters"):
        adapter._adapters = leaves
    adapter.model = getattr(leaf, "model", getattr(adapter, "model", ""))


def _set_adapter_temperature(adapter: Any, temperature: float) -> None:
    for leaf in _leaf_adapters(adapter):
        leaf.temperature = temperature
    adapter.temperature = temperature


def _describe_adapter(adapter: Any) -> str:
    if adapter is None:
        return "none"
    return f"{type(adapter).__name__}({getattr(adapter, 'model', '')})"


# ── Command table (single source of truth: dispatch + help + menu) ───────

# name -> (args hint, description)
COMMANDS: dict[str, tuple[str, str]] = {
    "help": ("", "Show this command menu"),
    "status": ("", "Session, identity, and adapter summary"),
    "model": ("[name]", "Show or switch the model"),
    "temperature": ("[0.0-2.0]", "Show or set sampling temperature"),
    "adapter": ("switch", "Pick a different adapter mid-session"),
    "caps": ("", "List capabilities held by this identity"),
    "cap": ("search|show|install <id>", "Search, inspect, or install capabilities"),
    "snapshot": ("", "Checkpoint identity state"),
    "history": ("", "List snapshots"),
    "clear": ("", "Reset the session (new session id)"),
    "exit": ("", "Leave chat (also /quit)"),
}

# Static argument suggestions per command (completed after the command word).
_ARG_SUGGESTIONS: dict[str, list[str]] = {
    "adapter": ["switch"],
    "cap": ["search ", "show ", "install "],
}

_COLON_ALIASES = {"snapshot": "snapshot", "history": "history", "help": "help"}


def _help_lines() -> list[str]:
    lines = ["  Chat commands"]
    for name, (args_hint, desc) in COMMANDS.items():
        suffix = f" {args_hint}" if args_hint else ""
        lines.append(f"    /{name}{suffix:<24} — {desc}")
    return lines


def _cmd_help(ctx: ChatContext, args: list[str]) -> str:
    print("\n".join(_help_lines()))
    return "handled"


def _require_runtime(ctx: ChatContext) -> bool:
    if ctx.runtime is None:
        print("  Runtime unavailable — only /help and /exit work in echo mode.")
        return False
    return True


def _cmd_status(ctx: ChatContext, args: list[str]) -> str:
    if not _require_runtime(ctx):
        return "handled"
    mode = None
    try:
        mode = ctx.runtime.get_session_mode(ctx.session_id)
    except Exception:
        pass
    print(f"  Identity:   {ctx.identity_name or ctx.identity_id} ({ctx.identity_id})")
    print(f"  Session:    {ctx.session_id}  ({ctx.turns} turns{', mode=' + str(mode.value) if mode else ''})")
    print(f"  Adapter:    {_describe_adapter(ctx.runtime.adapter)}")
    return "handled"


def _cmd_model(ctx: ChatContext, args: list[str]) -> str:
    if not _require_runtime(ctx):
        return "handled"
    adapter = ctx.runtime.adapter
    if adapter is None:
        print("  No adapter configured.")
        return "handled"
    if not args:
        print(f"  Current model: {_describe_adapter(adapter)}")
        leaves = _leaf_adapters(adapter)
        if len(leaves) > 1:
            print("  Chain (try order): "
                  + " → ".join(f"{type(a).__name__}({getattr(a, 'model', '')})" for a in leaves))
        return "handled"
    requested = args[0]

    # A provider name switches provider, not the model string: "/model nvidia"
    # must start calling NVIDIA, not send "model='nvidia'" to Groq.
    leaf = _find_leaf_by_provider(adapter, requested)
    if leaf is not None and "/" not in requested and ":" not in requested:
        _promote_leaf(adapter, leaf)
        print(f"  Switched provider to {type(leaf).__name__} "
              f"(model={getattr(leaf, 'model', '')}).")
        return "handled"

    # A model slug sets the model of the primary adapter only. Model names are
    # provider-scoped; smearing them across every adapter breaks all fallbacks.
    _set_adapter_model(adapter, requested)
    print(f"  Model set to {requested} on "
          f"{type(_leaf_adapters(adapter)[0]).__name__} "
          f"(primary adapter; fallbacks keep their own models).")
    return "handled"


def _cmd_temperature(ctx: ChatContext, args: list[str]) -> str:
    if not _require_runtime(ctx):
        return "handled"
    adapter = ctx.runtime.adapter
    if adapter is None:
        print("  No adapter configured.")
        return "handled"
    if not args:
        current = getattr(adapter, "temperature", 0.7)
        print(f"  Current temperature: {current}")
        return "handled"
    try:
        value = float(args[0])
    except ValueError:
        print(f"  Invalid temperature '{args[0]}' — expected a number between 0.0 and 2.0.")
        return "handled"
    if not 0.0 <= value <= 2.0:
        print(f"  Temperature {value} out of range — must be between 0.0 and 2.0.")
        return "handled"
    _set_adapter_temperature(adapter, value)
    print(f"  Temperature set to {value}")
    return "handled"


def _cmd_adapter(ctx: ChatContext, args: list[str]) -> str:
    if not _require_runtime(ctx):
        return "handled"
    if not args or args[0] != "switch":
        print(f"  Current adapter: {_describe_adapter(ctx.runtime.adapter)}")
        print("  Usage: /adapter switch")
        return "handled"
    from cli.main import _interactive_adapter_select

    print("  Scanning available adapters...")
    new_adapter = _interactive_adapter_select()
    if new_adapter is None:
        print("  No working adapter found — keeping current one.")
        return "handled"
    setter = getattr(ctx.runtime, "set_adapter", None)
    if callable(setter):
        setter(new_adapter)
    else:
        ctx.runtime.adapter = new_adapter
    print(f"  Adapter switched to {_describe_adapter(new_adapter)}")
    return "handled"


# ── Capability commands ──────────────────────────────────────────────────


def _cmd_caps(ctx: ChatContext, args: list[str]) -> str:
    if not _require_runtime(ctx):
        return "handled"
    caps = ctx.runtime.capability_registry.list(ctx.identity_id)
    if not caps:
        print(f"  No capabilities installed for {ctx.identity_id}.")
        print("  Try /cap search <query> and /cap install <id>.")
        return "handled"
    print(f"  Capabilities held by {ctx.identity_name or ctx.identity_id} ({len(caps)}):")
    for cap in caps:
        info = cap.to_dict() if hasattr(cap, "to_dict") else {}
        cap_id = info.get("id", getattr(cap, "id", "?"))
        version = info.get("version", getattr(cap, "version", ""))
        desc = info.get("description", getattr(cap, "description", ""))
        skills = info.get("skills") or [s.name if hasattr(s, "name") else str(s) for s in cap.skills()]
        print(f"    {cap_id:20s}  v{version}  {str(desc)[:55]}")
        for skill in skills:
            print(f"      - {skill}")
    return "handled"


def _list_marketplace(header: str) -> None:
    """Print all marketplace capabilities (id, version, description)."""
    from cli.registry_cmds import _load_cap_registry

    caps = _load_cap_registry().get("capabilities", [])
    print(f"  {header} ({len(caps)} available):")
    for c in caps:
        print(f"    {c.get('id', ''):20s}  v{c.get('version', '')}  {str(c.get('description', ''))[:55]}")
    print("  Use /cap show <id> for details, /cap install <id> to install.")


def _cmd_cap(ctx: ChatContext, args: list[str]) -> str:
    if not args:
        _list_marketplace("Capability marketplace")
        return "handled"

    sub, rest = args[0], args[1:]
    if sub == "search":
        from cli.registry_cmds import _load_cap_registry

        query = " ".join(rest).lower()
        results = []
        for c in _load_cap_registry().get("capabilities", []):
            text = f"{c.get('id', '')} {c.get('name', '')} {c.get('description', '')}"
            if query in text.lower():
                results.append(c)
        print(f"  {len(results)} matches for '{' '.join(rest)}'")
        for c in results:
            print(f"    {c['id']:20s}  {c.get('description', '')[:60]}")
        return "handled"

    if sub == "show":
        from cli.registry_cmds import _find_capability, _load_cap_manifest

        if not rest:
            _list_marketplace("Capabilities you can show")
            return "handled"
        entry = _find_capability(rest[0])
        if not entry:
            print(f"  Capability '{rest[0]}' not found in marketplace.")
            return "handled"
        d = {**entry, **_load_cap_manifest(entry)}
        print(f"    id:           {d.get('id', '')}")
        print(f"    name:         {d.get('name', '')}")
        print(f"    version:      v{d.get('version', '')}")
        print(f"    description:  {d.get('description', '')}")
        skills = d.get("skills", [])
        print(f"    skills ({len(skills)}):")
        for s in skills:
            print(f"      {s['name']:38s}  {s.get('description', '')[:50]}")
        return "handled"

    if sub == "install":
        from cli.registry_cmds import _find_capability

        if not rest:
            _list_marketplace("Capabilities you can install")
            return "handled"
        cap_id = rest[0]
        if _find_capability(cap_id) is None:
            print(f"  Capability '{cap_id}' not found in marketplace.")
            return "handled"
        try:
            cap = ctx.runtime.capability_registry.install(ctx.identity_id, cap_id)
        except ValueError as e:
            print(f"  Install failed: {e}")
            return "handled"
        n_skills = len(cap.skills())
        print(f"  Installed {cap_id} → {ctx.identity_id} ({n_skills} skills)")
        print("  Available immediately in this session; persisted across restarts.")
        return "handled"

    print(f"  Unknown subcommand '/cap {sub}'. Use search, show, or install.")
    return "handled"


# ── Snapshot commands ────────────────────────────────────────────────────


def _current_modules(ctx: ChatContext) -> dict:
    """Best-effort current state for snapshotting, tolerant of register() format."""
    try:
        snap = ctx.manager.latest()
        if snap is not None:
            return snap.modules
    except (KeyError, TypeError):
        pass
    raw = ctx.storage.load(ctx.identity_id, "latest_snapshot") or {}
    return raw.get("modules") or raw


def _cmd_snapshot(ctx: ChatContext, args: list[str]) -> str:
    modules = _current_modules(ctx)
    snap_id = ctx.manager.capture(modules, label=f"chat-turn-{ctx.turns}")
    print(f"  [snapshot saved: {snap_id[:8]}]")
    return "handled"


def _cmd_history(ctx: ChatContext, args: list[str]) -> str:
    snaps = ctx.manager.history()
    if not snaps:
        print("  No snapshots yet. Use /snapshot to checkpoint.")
        return "handled"
    for snap in snaps:
        print(f"  {snap.summary()}")
    return "handled"


def _cmd_clear(ctx: ChatContext, args: list[str]) -> str:
    if not _require_runtime(ctx):
        return "handled"
    try:
        ctx.runtime.end_session(ctx.session_id)
    except Exception:
        pass
    ctx.session_id = ctx.runtime.start_session(ctx.identity_id)
    ctx.turns = 0
    print(f"  Session reset ({ctx.session_id}). Conversation memory cleared for context.")
    return "handled"


def _cmd_exit(ctx: ChatContext, args: list[str]) -> str:
    return "exit"


_HANDLERS = {
    "help": _cmd_help,
    "status": _cmd_status,
    "model": _cmd_model,
    "temperature": _cmd_temperature,
    "adapter": _cmd_adapter,
    "caps": _cmd_caps,
    "cap": _cmd_cap,
    "snapshot": _cmd_snapshot,
    "history": _cmd_history,
    "clear": _cmd_clear,
    "exit": _cmd_exit,
    "quit": _cmd_exit,
}


def dispatch_chat_command(text: str, ctx: ChatContext) -> Optional[str]:
    """Dispatch a slash command.

    Returns:
        None        — input was not a command; caller should send it to the runtime
        "handled"   — command executed; continue the chat loop
        "exit"      — user asked to leave the chat
    """
    stripped = text.strip()
    if stripped.startswith("/"):
        name, _, rest = stripped[1:].partition(" ")
        name = name.strip()
        handler = _HANDLERS.get(name) if name else _cmd_help
    elif stripped.startswith(":"):
        alias = stripped[1:].split()[0] if stripped[1:].strip() else ""
        mapped = _COLON_ALIASES.get(alias)
        if mapped is None:
            return None
        handler = _HANDLERS[mapped]
        rest = ""
    else:
        return None

    if handler is None:
        print(f"  Unknown command '/{name.strip()}'. Type /help for chat commands.")
        return "handled"

    args = rest.split()
    return handler(ctx, args)


# Backwards-compatible internal name used by tests and the CLI.
_dispatch_chat_command = dispatch_chat_command


# ── Interactive command menu (arrow-key navigation) ──────────────────────


class ChatCommandCompleter(Completer):
    """Live completion for slash commands.

    Pops the full menu as soon as the user types "/", narrows as they type,
    and suggests static arguments (e.g. ``switch`` after ``/adapter``).
    Each entry shows its description in the menu so users can discover
    commands without ever opening /help.
    """

    def get_completions(self, document, complete_event) -> Iterable[Completion]:
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return

        name_part, space, arg_part = text[1:].partition(" ")

        if not space:
            # Completing the command word itself. Insert just the command
            # name (no slash) so it replaces the typed prefix rather than
            # appending after the leading "/".
            query = name_part.lower()
            for name, (args_hint, desc) in COMMANDS.items():
                if name.startswith(query):
                    display = f"/{name} {args_hint}".rstrip()
                    yield Completion(
                        name,
                        start_position=-len(name_part),
                        display=display,
                        display_meta=desc,
                    )
            return

        # Completing arguments after the command word.
        suggestions = _ARG_SUGGESTIONS.get(name_part.lower(), [])
        for suggestion in suggestions:
            if suggestion.startswith(arg_part):
                yield Completion(
                    suggestion,
                    start_position=-len(arg_part),
                    display=suggestion.rstrip(),
                    display_meta=f"/{name_part} argument",
                )


def _menu_key_bindings():
    """Key bindings for the command menu.

    Enter while the menu is open executes the highlighted command (applies
    the selected completion, then submits) — so users can type "/", arrow
    down to a command, and hit Enter.  With no menu open, Enter behaves
    normally.
    """
    kb = KeyBindings()

    @kb.add("enter", eager=True)
    def _enter(event):
        buf = event.app.current_buffer
        state = buf.complete_state
        if state is not None and state.current_completion is not None:
            buf.apply_completion(state.current_completion)
            buf.complete_state = None
        buf.validate_and_handle()

    return kb


def _is_interactive_tty() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def build_input_reader() -> Callable[[str], str]:
    """Return a prompt-to-line function.

    Uses a prompt_toolkit PromptSession with live command completion when
    running interactively; falls back to plain ``input()`` otherwise (piped
    input, tests, or missing prompt_toolkit).  The fallback keeps bare "/"
    useful via the dispatcher's printed menu.
    """
    if _PROMPT_TOOLKIT_AVAILABLE and _is_interactive_tty():
        session = PromptSession(history=InMemoryHistory())

        def read(prompt: str) -> str:
            # The CLI prompt contains raw ANSI color codes; prompt_toolkit
            # renders plain strings literally, so parse them explicitly.
            return session.prompt(
                FTAnsi(prompt),
                completer=ChatCommandCompleter(),
                complete_while_typing=True,
                key_bindings=_menu_key_bindings(),
            )

        return read

    def read_fallback(prompt: str) -> str:
        return input(prompt)

    return read_fallback
