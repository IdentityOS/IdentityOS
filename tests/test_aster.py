from __future__ import annotations

from pathlib import Path

from core.capabilities.email.backends import FileMailboxBackend, MailboxTransport
from core.capabilities.operations import OperationsCapability
from core.capabilities.registry import CapabilityRegistry
from core.identity import IdentityClass
from core.operations import (
    Candidate,
    ControlState,
    OperationsEngine,
    OperationsStore,
    SearchCandidateSource,
    StaticCandidateSource,
)
from core.operations.aster import (
    ASTER_SIGNATURE,
    build_aster_engine,
    create_aster_identity,
    default_need_rules,
    persist_aster_identity,
)
from core.operations.runtime_registry import get_engine_for
from runtime.persistence import InMemoryBackend, JSONFileBackend


def test_aster_can_prefer_remote_fallbacks_over_unhealthy_local_primary(monkeypatch):
    from adapters.chain import ChainAdapter
    from adapters.groq_adapter import GroqAdapter
    from adapters.openai_adapter import OllamaAdapter
    from cli.aster_cmds import _build_reply_adapter

    monkeypatch.setenv("IDENTITY_ADAPTER", "ollama")
    monkeypatch.setenv("IDENTITY_ADAPTER_CONFIG", '{"model":"phi4-mini:latest"}')
    monkeypatch.delenv("ASTER_ADAPTER", raising=False)
    monkeypatch.setenv("ASTER_PREFER_REMOTE", "true")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    adapter = _build_reply_adapter()

    assert isinstance(adapter, ChainAdapter)
    assert isinstance(adapter.adapters[0], GroqAdapter)
    assert isinstance(adapter.adapters[-1], OllamaAdapter)


def test_aster_cli_json_redacts_secret_fields_recursively():
    from cli.aster_cmds import _redact_json_value

    redacted = _redact_json_value(
        {"status": "ok", "password": "fixture", "nested": {"session_token": "fixture"}}
    )

    assert redacted == {
        "status": "ok",
        "password": "[REDACTED]",
        "nested": {"session_token": "[REDACTED]"},
    }


def test_aster_cli_print_never_emits_secret_field_values(capsys):
    from cli.aster_cmds import _print_json

    _print_json({"password": "sensitive-fixture", "status": "ok"})

    output = capsys.readouterr().out
    assert "sensitive-fixture" not in output
    assert '"password": "[REDACTED]"' in output


def test_aster_identity_has_persona_and_signature():
    identity = create_aster_identity()
    assert identity.id == "aster"
    assert identity.identity_class is IdentityClass.AGENT
    assert identity.role == "Resource & Collaboration Operator"
    assert ASTER_SIGNATURE in identity.system_prompt
    assert "transparent" in identity.persona.lower()
    assert identity.tagline


def test_persist_aster_identity_loads_from_fresh_runtime(tmp_path: Path):
    storage = JSONFileBackend(root_dir=str(tmp_path / "_store"))
    spec = persist_aster_identity(storage)

    from runtime.orchestrator import IdentityRuntime

    runtime = IdentityRuntime(storage=storage)
    loaded = runtime.load("aster")
    assert loaded is not None
    assert loaded.name == "Aster"
    assert loaded.role == "Resource & Collaboration Operator"


def test_build_aster_engine_uses_search_source(tmp_path: Path):
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "README.md").write_text("# A\n\nno funding here\n", encoding="utf-8")

    def fake_search(query: str):
        return [{"title": "Open Source Fund", "url": "https://example.org/fund", "snippet": "a public fund"}]

    storage = InMemoryBackend()
    backend = FileMailboxBackend(tmp_path / "mail", mailbox="aster")
    engine = build_aster_engine(
        storage,
        project_root=tmp_path / "project",
        transport=MailboxTransport(backend),
        search_fn=fake_search,
        candidate_sources=[
            StaticCandidateSource([
                Candidate(
                    target_name="Acme Fund",
                    organization="Acme",
                    contact_email="grants@acme.org",
                    category="funding",
                    relevant_work=["identity"],
                    fit_reason="identity work",
                    value_proposition="open runtime",
                    potential_ask="conversation",
                    confidence=0.6,
                )
            ])
        ],
    )
    report = engine.tick()

    status = engine.status()
    assert status["project"]["name"]
    assert status["needs"]["open"] >= 1
    assert any("Acme Fund" in o["target_name"] for o in status["opportunities"]["items"])
    # The web-search source produced additional, uncontacted opportunities.
    assert any(o["target_name"] == "Open Source Fund" for o in status["opportunities"]["items"])
    engine.store.set_controls(ControlState(outbound_mode="autonomous"))
    report = engine.tick()
    assert report.outreach_sent


def test_operations_capability_gates_engine(tmp_path: Path):
    storage = InMemoryBackend()
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "README.md").write_text("# X\n", encoding="utf-8")
    engine = build_aster_engine(storage, project_root=tmp_path / "project")
    from core.operations.runtime_registry import get_engine_for

    assert get_engine_for(storage, "aster") is engine

    registry = CapabilityRegistry(storage)
    registry.install("aster", "operations")

    skipped, reason = registry.can("aster", "operations.run_tick")
    assert skipped is False

    registry.grant("aster", "operations", "operations.run")
    allowed, _ = registry.can("aster", "operations.run_tick")
    assert allowed is True

    result = registry.call("aster", "operations.run_tick")
    assert result.success is True
    assert result.data["observed"] is True


def test_json_store_survives_process_restart(tmp_path: Path):
    root = tmp_path / "_store"
    identity_id = "aster"
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "README.md").write_text("# Y\n", encoding="utf-8")

    storage1 = JSONFileBackend(root_dir=str(root))
    engine1 = build_aster_engine(storage1, project_root=tmp_path / "project")
    engine1.tick()
    needs_before = storage1.load(identity_id, "operations.needs")
    assert needs_before and needs_before.get("items")

    # Fresh process, same on-disk store.
    storage2 = JSONFileBackend(root_dir=str(root))
    store2 = OperationsStore(storage2, identity_id)
    assert len(store2.list_needs()) == len(needs_before["items"])
    assert store2.controls().paused is False
