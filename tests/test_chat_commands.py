"""Tests for the in-chat slash-command dispatcher (cli/chat_commands.py).

Covers model/adapter/temperature switching without killing the session,
status reporting, snapshot/history, session reset, capability inspection
and mid-session install.  Uses fake adapters and an in-memory runtime so
no network or LLM calls are made.

NOTE: imports deliberately target ``cli.chat_commands`` rather than
``cli.main`` — importing cli.main loads .env at module scope, which would
leak GROQ_API_KEY into os.environ and un-skip network-dependent test
modules elsewhere in the suite.
"""

from cli.chat_commands import (
    ChatCommandCompleter,
    ChatContext,
    _dispatch_chat_command,
    _set_adapter_model,
    _set_adapter_temperature,
)
from core.snapshot import SnapshotManager
from runtime.persistence import JSONFileBackend


class FakeAdapter:
    def __init__(self, model="default", name="FakeAdapter", base_url=""):
        self.model = model
        self.temperature = 0.7
        self.base_url = base_url


class FakeMemoryStore:
    def __init__(self):
        self._memories = []

    def by_identity(self, identity_id):
        return self._memories


class FakeGoalEngine:
    def by_scope(self, scope):
        return []


class FakeCapabilityRegistry:
    def list(self, identity_id):
        return []


class FakeRuntime:
    """Minimal stand-in for IdentityRuntime — no network, no LLM."""

    def __init__(self):
        self.adapter = FakeAdapter("groq-test")
        self.memory_store = FakeMemoryStore()
        self.goal_engine = FakeGoalEngine()
        self.capability_registry = FakeCapabilityRegistry()
        self._fact_stores = {}
        self.sessions = []

    def get_session_mode(self, session_id):
        return None

    def end_session(self, session_id):
        pass

    def start_session(self, identity_id):
        sid = f"session-{len(self.sessions)}"
        self.sessions.append(sid)
        return sid


def _make_ctx(runtime=None, tmp_path=None):
    import tempfile

    root = str(tmp_path) if tmp_path is not None else tempfile.mkdtemp(prefix="idos-chat-cmd-")
    storage = JSONFileBackend(root_dir=root)
    manager = SnapshotManager(storage, "gabe")
    manager.capture({"identity": {"id": "gabe", "name": "Gabriel"}}, label="initial")
    return ChatContext(runtime or FakeRuntime(), manager, storage, "gabe", "session-1")


class TestModelSwitching:
    def test_single_adapter_model_switch(self):
        a = FakeAdapter("old-model")
        _set_adapter_model(a, "openai/gpt-oss-120b")
        assert a.model == "openai/gpt-oss-120b"

    def test_chain_model_switch_updates_only_the_head(self):
        """A model slug is provider-scoped: writing it into every leaf turns
        cloud fallbacks into 404s (a local tag sent to Groq/Cerebras)."""
        from adapters import ChainAdapter
        head = FakeAdapter("m1", base_url="https://api.example.test")
        tail = FakeAdapter("openai/gpt-oss-120b", base_url="https://api.groq.test")
        chain = ChainAdapter([head, tail])
        _set_adapter_model(chain, "phi4-mini:latest")
        assert head.model == "phi4-mini:latest"
        assert tail.model == "openai/gpt-oss-120b", "fallback leaves must keep their own model"
        assert chain.model == "phi4-mini:latest"

    def test_temperature_switch(self):
        from adapters import ChainAdapter
        a = FakeAdapter("m1")
        b = FakeAdapter("m2")
        _set_adapter_temperature(ChainAdapter([a, b]), 1.1)
        assert a.temperature == 1.1
        assert b.temperature == 1.1

    def test_dispatch_model_without_arg_shows_current(self, capsys):
        ctx = _make_ctx()
        status = _dispatch_chat_command("/model", ctx)
        assert status == "handled"
        assert "groq-test" in capsys.readouterr().out

    def test_dispatch_model_with_arg_switches(self, capsys):
        ctx = _make_ctx()
        status = _dispatch_chat_command("/model openai/gpt-oss-120b", ctx)
        assert status == "handled"
        assert ctx.runtime.adapter.model == "openai/gpt-oss-120b"
        assert "Model set to openai/gpt-oss-120b" in capsys.readouterr().out

    def test_dispatch_model_provider_name_switches_provider(self, capsys):
        """`/model Nvidia` must call NVIDIA, not send model='Nvidia' to Groq."""
        from adapters import ChainAdapter
        groq = FakeAdapter("openai/gpt-oss-120b", base_url="https://api.groq.test")
        nvidia = FakeAdapter("nvidia/llama-3.1-nemotron-70b-instruct",
                             base_url="https://integrate.api.nvidia.test")
        ctx = _make_ctx()
        ctx.runtime.adapter = ChainAdapter([groq, nvidia])

        status = _dispatch_chat_command("/model Nvidia", ctx)

        assert status == "handled"
        leaves = list(ctx.runtime.adapter._adapters)
        assert leaves[0] is nvidia, "the named provider becomes the chain head"
        assert groq.model == "openai/gpt-oss-120b", "other providers' models untouched"
        assert ctx.runtime.adapter.model == "nvidia/llama-3.1-nemotron-70b-instruct"
        assert "Switched provider" in capsys.readouterr().out

    def test_dispatch_model_without_arg_lists_chain(self, capsys):
        from adapters import ChainAdapter
        ctx = _make_ctx()
        ctx.runtime.adapter = ChainAdapter([
            FakeAdapter("m1", base_url="https://a.test"),
            FakeAdapter("m2", base_url="https://b.test"),
        ])
        assert _dispatch_chat_command("/model", ctx) == "handled"
        assert "→" in capsys.readouterr().out

    def test_dispatch_temperature_valid_and_invalid(self, capsys):
        ctx = _make_ctx()
        assert _dispatch_chat_command("/temperature 1.5", ctx) == "handled"
        assert ctx.runtime.adapter.temperature == 1.5
        assert _dispatch_chat_command("/temperature 9", ctx) == "handled"
        assert ctx.runtime.adapter.temperature == 1.5  # unchanged


class TestCommandLifecycle:
    def test_exit_returns_exit(self):
        ctx = _make_ctx()
        assert _dispatch_chat_command("/exit", ctx) == "exit"
        assert _dispatch_chat_command("/quit", ctx) == "exit"

    def test_help_returns_handled(self, capsys):
        ctx = _make_ctx()
        assert _dispatch_chat_command("/help", ctx) == "handled"
        assert "Chat commands" in capsys.readouterr().out

    def test_bare_slash_shows_menu(self, capsys):
        ctx = _make_ctx()
        assert _dispatch_chat_command("/", ctx) == "handled"
        assert "Chat commands" in capsys.readouterr().out

    def test_unknown_command_handled_not_exit(self, capsys):
        ctx = _make_ctx()
        assert _dispatch_chat_command("/bogus", ctx) == "handled"
        assert "Unknown command" in capsys.readouterr().out

    def test_non_command_returns_none(self):
        ctx = _make_ctx()
        assert _dispatch_chat_command("just a normal message", ctx) is None

    def test_status_reports_adapter_and_session(self, capsys):
        ctx = _make_ctx()
        assert _dispatch_chat_command("/status", ctx) == "handled"
        out = capsys.readouterr().out
        assert "FakeAdapter(groq-test)" in out
        assert "session-1" in out

    def test_clear_resets_session(self, capsys):
        ctx = _make_ctx()
        old = ctx.session_id
        assert _dispatch_chat_command("/clear", ctx) == "handled"
        assert ctx.session_id != old
        assert ctx.turns == 0
        assert "Session reset" in capsys.readouterr().out


class TestSnapshotCommands:
    def test_snapshot_captures_state(self, capsys, tmp_path):
        ctx = _make_ctx(runtime=FakeRuntime(), tmp_path=tmp_path)
        assert _dispatch_chat_command("/snapshot", ctx) == "handled"
        assert "snapshot saved" in capsys.readouterr().out
        assert len(ctx.manager.history()) == 2  # initial + new

    def test_colon_snapshot_alias_still_works(self, capsys, tmp_path):
        ctx = _make_ctx(runtime=FakeRuntime(), tmp_path=tmp_path)
        assert _dispatch_chat_command(":snapshot", ctx) == "handled"
        assert "snapshot saved" in capsys.readouterr().out

    def test_history_lists_snapshots(self, capsys, tmp_path):
        ctx = _make_ctx(runtime=FakeRuntime(), tmp_path=tmp_path)
        assert _dispatch_chat_command("/history", ctx) == "handled"
        out = capsys.readouterr().out
        assert "Snapshot" in out
        assert "initial" in out

    def test_snapshot_handles_register_format_state(self, capsys, tmp_path):
        """Identities stored in register() format (no snapshot_id) must not crash.

        Regression: manager.latest() raises KeyError when latest_snapshot is
        ``{"modules": {...}}``; /snapshot must fall back and still checkpoint.
        """
        storage = JSONFileBackend(root_dir=str(tmp_path))
        manager = SnapshotManager(storage, "gabe")
        storage.save("gabe", "latest_snapshot", {"modules": {"identity": {"id": "gabe"}}})
        ctx = ChatContext(FakeRuntime(), manager, storage, "gabe", "session-1")
        assert _dispatch_chat_command("/snapshot", ctx) == "handled"
        assert "snapshot saved" in capsys.readouterr().out
        assert len(manager.history()) == 1


class TestRuntimeUnavailable:
    def test_only_help_and_exit_allowed(self, capsys, tmp_path):
        storage = JSONFileBackend(root_dir=str(tmp_path))
        manager = SnapshotManager(storage, "gabe")
        manager.capture({"identity": {"id": "gabe", "name": "Gabriel"}}, label="initial")
        ctx = ChatContext(None, manager, storage, "gabe", "session-1")
        assert _dispatch_chat_command("/status", ctx) == "handled"
        assert "Runtime unavailable" in capsys.readouterr().out
        assert _dispatch_chat_command("/exit", ctx) == "exit"


class FakeCapability:
    def __init__(self, cap_id, skills=()):
        self.id = cap_id
        self.version = "1.0.0"
        self.description = f"{cap_id} capability"

    def to_dict(self):
        return {"id": self.id, "version": self.version, "description": self.description}

    def skills(self):
        return []


class FakeInstallRegistry:
    """Records installs like the real CapabilityRegistry; starts with one cap."""

    def __init__(self):
        self.installed = {"datetime": FakeCapability("datetime")}
        self.install_calls = []

    def list(self, identity_id):
        return list(self.installed.values())

    def install(self, identity_id, cap_id, config=None):
        self.install_calls.append((identity_id, cap_id))
        cap = FakeCapability(cap_id)
        self.installed[cap_id] = cap
        return cap


class InstallableRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.capability_registry = FakeInstallRegistry()
        self.adapter_history = []

    def set_adapter(self, adapter):
        self.adapter = adapter
        self.adapter_history.append(adapter)


class TestCapabilityCommands:
    def test_caps_lists_installed(self, capsys, tmp_path):
        ctx = _make_ctx(runtime=InstallableRuntime(), tmp_path=tmp_path)
        assert _dispatch_chat_command("/caps", ctx) == "handled"
        out = capsys.readouterr().out
        assert "datetime" in out

    def test_caps_empty(self, capsys, tmp_path):
        rt = InstallableRuntime()
        rt.capability_registry.installed.clear()
        ctx = _make_ctx(runtime=rt, tmp_path=tmp_path)
        assert _dispatch_chat_command("/caps", ctx) == "handled"
        assert "No capabilities" in capsys.readouterr().out

    def test_cap_search_marketplace(self, capsys, tmp_path):
        ctx = _make_ctx(runtime=InstallableRuntime(), tmp_path=tmp_path)
        assert _dispatch_chat_command("/cap search weather", ctx) == "handled"
        assert "weather" in capsys.readouterr().out

    def test_cap_show_known_and_unknown(self, capsys, tmp_path):
        ctx = _make_ctx(runtime=InstallableRuntime(), tmp_path=tmp_path)
        assert _dispatch_chat_command("/cap show calc", ctx) == "handled"
        assert "calc" in capsys.readouterr().out
        assert _dispatch_chat_command("/cap show nope-does-not-exist", ctx) == "handled"
        assert "not found" in capsys.readouterr().out

    def test_bare_cap_lists_marketplace(self, capsys, tmp_path):
        """'/cap' with no subcommand must browse the marketplace."""
        ctx = _make_ctx(runtime=InstallableRuntime(), tmp_path=tmp_path)
        assert _dispatch_chat_command("/cap", ctx) == "handled"
        out = capsys.readouterr().out
        assert "marketplace" in out.lower()
        assert "calc" in out and "weather" in out

    def test_cap_show_without_id_lists_marketplace(self, capsys, tmp_path):
        """Users shouldn't need to know an id to browse — '/cap show' alone
        must list everything available instead of demanding an argument."""
        ctx = _make_ctx(runtime=InstallableRuntime(), tmp_path=tmp_path)
        assert _dispatch_chat_command("/cap show", ctx) == "handled"
        out = capsys.readouterr().out
        assert "calc" in out and "weather" in out
        assert "/cap show <id>" in out  # hint for the next step

    def test_cap_install_without_id_lists_not_installs(self, capsys, tmp_path):
        rt = InstallableRuntime()
        ctx = _make_ctx(runtime=rt, tmp_path=tmp_path)
        assert _dispatch_chat_command("/cap install", ctx) == "handled"
        out = capsys.readouterr().out
        assert "calc" in out
        assert rt.capability_registry.install_calls == []  # nothing installed

    def test_cap_install_uses_live_runtime(self, capsys, tmp_path):
        """Mid-session install must go through the RUNNING runtime's registry,
        so the identity can use the capability on the very next turn."""
        rt = InstallableRuntime()
        ctx = _make_ctx(runtime=rt, tmp_path=tmp_path)
        assert _dispatch_chat_command("/cap install calc", ctx) == "handled"
        out = capsys.readouterr().out
        assert "Installed calc" in out
        assert rt.capability_registry.install_calls == [("gabe", "calc")]
        assert "calc" in [c.id for c in rt.capability_registry.list("gabe")]

    def test_cap_install_unknown_is_reported_not_installed(self, capsys, tmp_path):
        rt = InstallableRuntime()
        ctx = _make_ctx(runtime=rt, tmp_path=tmp_path)
        assert _dispatch_chat_command("/cap install nope", ctx) == "handled"
        assert rt.capability_registry.install_calls == []
        assert "not found" in capsys.readouterr().out


class TestAdapterSwitchCommand:
    def test_adapter_shows_current(self, capsys, tmp_path):
        ctx = _make_ctx(runtime=InstallableRuntime(), tmp_path=tmp_path)
        assert _dispatch_chat_command("/adapter", ctx) == "handled"
        assert "FakeAdapter(groq-test)" in capsys.readouterr().out

    def test_orchestrator_set_adapter_swaps_and_emits_event(self, tmp_path):
        """The real runtime must expose a mid-session adapter swap, with an
        observable ADAPTER_SWITCHED event as evidence."""
        from runtime.event_bus import EventType
        from runtime.orchestrator import IdentityRuntime

        runtime = IdentityRuntime(storage=JSONFileBackend(root_dir=str(tmp_path)), adapter=FakeAdapter("old"))
        events = []
        runtime.event_bus.subscribe_all(events.append)
        new = FakeAdapter("new")
        runtime.set_adapter(new)
        assert runtime.adapter is new
        switched = [e for e in events if e.event_type == EventType.ADAPTER_SWITCHED]
        assert len(switched) == 1
        assert switched[0].payload["old_adapter"] == "FakeAdapter"
        assert switched[0].payload["model"] == "new"


# ── Interactive command menu ────────────────────────────────────────────


def _completions(text):
    """Run the completer against `text` and return (display, meta, text) tuples."""
    doc = type("Doc", (), {"text_before_cursor": text})()
    completer = ChatCommandCompleter()
    return [
        (c.display_text if hasattr(c, "display_text") else str(c.display),
         getattr(c, "display_meta_text", "") or "",
         c.text)
        for c in completer.get_completions(doc, None)
    ]


class TestChatCommandCompleter:
    def test_bare_slash_shows_full_menu(self):
        results = _completions("/")
        texts = [t for _, _, t in results]
        # Completion text is the bare name (the typed "/" is kept).
        assert "help" in texts and "caps" in texts and "exit" in texts
        from cli.chat_commands import COMMANDS
        for name in COMMANDS:
            assert name in texts

    def test_completion_composes_with_typed_slash(self):
        """Regression: inserting the completion must yield '/model', not
        the malformed '//status' seen before this fix."""
        for typed, expected in [("/", "/help"), ("/mo", "/model"), ("/sta", "/status")]:
            doc = type("Doc", (), {"text_before_cursor": typed})()
            first = next(ChatCommandCompleter().get_completions(doc, None))
            start = first.start_position
            composed = typed[: len(typed) + start] + first.text
            assert composed == expected, f"{typed!r} -> {composed!r}"

    def test_menu_entries_carry_descriptions(self):
        results = _completions("/")
        metas = [m for _, m, _ in results]
        assert any("capabilities" in m.lower() for m in metas)

    def test_prefix_filters(self):
        results = _completions("/mo")
        assert [t for _, _, t in results] == ["model"]

    def test_cap_subcommands_suggested_after_space(self):
        results = _completions("/cap ")
        texts = [t for _, _, t in results]
        assert "search " in texts and "show " in texts and "install " in texts

    def test_adapter_switch_suggested(self):
        results = _completions("/adapter s")
        assert [t for _, _, t in results] == ["switch"]

    def test_plain_text_gets_no_completions(self):
        assert _completions("hello wor") == []


class TestInputReaderFallback:
    def test_falls_back_to_input_without_tty(self, monkeypatch):
        from cli import chat_commands as cc

        monkeypatch.setattr(cc, "_is_interactive_tty", lambda: False)
        reader = cc.build_input_reader()

        import builtins
        calls = []

        def fake_input(prompt=""):
            calls.append(prompt)
            return "/help"

        monkeypatch.setattr(builtins, "input", fake_input)
        assert reader("you> ") == "/help"
        assert calls == ["you> "]

    def test_falls_back_when_prompt_toolkit_missing(self, monkeypatch):
        from cli import chat_commands as cc

        monkeypatch.setattr(cc, "_PROMPT_TOOLKIT_AVAILABLE", False)
        monkeypatch.setattr(cc, "_is_interactive_tty", lambda: True)
        reader = cc.build_input_reader()

        import builtins
        monkeypatch.setattr(builtins, "input", lambda prompt="": "ok")
        assert reader("you> ") == "ok"

    def test_uses_prompt_session_on_real_tty(self, monkeypatch):
        """On a TTY the reader must come from a PromptSession wired to the
        completer — that's what gives arrow-key navigation."""
        from cli import chat_commands as cc

        created = {}

        class FakeSession:
            def __init__(self, **kwargs):
                created.update(kwargs)

            def prompt(self, prompt, **kwargs):
                created.update(kwargs)
                return "/status"

        monkeypatch.setattr(cc, "_is_interactive_tty", lambda: True)
        monkeypatch.setattr(cc, "PromptSession", FakeSession)
        reader = cc.build_input_reader()
        assert isinstance(created.get("history"), cc.InMemoryHistory)

        result = reader("\x1b[92myou>\x1b[0m ")
        assert result == "/status"
        assert isinstance(created.get("completer"), cc.ChatCommandCompleter)
        assert created.get("complete_while_typing") is True

    def test_prompt_ansi_codes_are_parsed_not_printed(self, monkeypatch):
        """Regression: the colored prompt reached prompt_toolkit as a plain
        string, so users saw literal '^[[92m' garbage.  It must be wrapped
        in an ANSI formatted-text object."""
        from cli import chat_commands as cc

        captured = {}

        class FakeSession:
            def __init__(self, **kwargs):
                pass

            def prompt(self, prompt, **kwargs):
                captured["prompt"] = prompt
                return ""

        monkeypatch.setattr(cc, "_is_interactive_tty", lambda: True)
        monkeypatch.setattr(cc, "PromptSession", FakeSession)
        cc.build_input_reader()("\x1b[92myou>\x1b[0m ")
        prompt = captured["prompt"]
        assert type(prompt).__name__ == "ANSI"

    def test_help_menu_generated_from_command_table(self, capsys):
        """The printed /help must stay in sync with the completion menu —
        both derive from the same COMMANDS table."""
        from cli.chat_commands import COMMANDS, _help_lines

        out = "\n".join(_help_lines())
        for name in COMMANDS:
            assert f"/{name}" in out