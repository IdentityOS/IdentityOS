"""test_surfaces.py — Culture Commons surface integrates with the Operations loop."""

from __future__ import annotations

import json

import core.capabilities.culture_commons as cc_mod
from core.capabilities.registry import CapabilityRegistry
from core.operations.aster import build_aster_engine
from core.operations.models import ProvenancePhase
from core.operations.surfaces import CultureCommonsSurface, SURFACE_NAMESPACE
from core.secrets.store import SecretStore
from runtime.persistence import InMemoryBackend
from tests.interop_stubs import FakeMCPHttpClient
from tests.test_culture_commons_capability import FakeCommons, NAME


def _surface(tmp_path, monkeypatch, fake: FakeCommons):
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
    fake.present = [{"name": "AnotherAgent", "seat": "p1"}]
    fake.boards = {"ideas": [{"id": "t1", "subject": "hello world", "traces": []}]}
    storage = InMemoryBackend()
    registry = CapabilityRegistry(storage)
    registry.install(
        "aster", "culture_commons",
        config={"url": "https://culture.sbs/mcp", "state_dir": str(state_dir), "secret_store_dir": str(secret_dir), "name": NAME},
    )
    secret_store = SecretStore(secret_dir)
    engine = build_aster_engine(
        storage,
        project_root=tmp_path,
        capability_registry=registry,
        secret_store=secret_store,
        register=False,
    )
    return engine, registry, secret_store, state_dir


def test_surface_status_reports_installation_without_network(tmp_path, monkeypatch):
    engine, registry, store, state = _surface(tmp_path, monkeypatch, FakeCommons())
    surface = CultureCommonsSurface(engine)
    status = surface.status()
    assert status["name"] == "culture_commons"
    assert status["installed"] is True
    assert status["can_post"] is False


def test_observe_records_provenance_and_facts(tmp_path, monkeypatch):
    engine, registry, store, state = _surface(tmp_path, monkeypatch, FakeCommons())
    surface = CultureCommonsSurface(engine)
    result = surface.observe()
    assert result["observed"] is True

    entries = engine.store.list_provenance()
    observe_entries = [e for e in entries if e.action == "culture_commons.observe"]
    assert observe_entries, "no observe provenance recorded"
    assert observe_entries[0].refs.get("surface") == "culture_commons"
    assert observe_entries[0].evidence, "no evidence captured"

    snapshot = engine.store._storage.load("aster", SURFACE_NAMESPACE)
    assert snapshot["count"] == 1
    assert snapshot["facts"][0]["source"] == "culture.sbs"
    assert "room" in snapshot["summary"]

    rels = engine.store.list_relationships()
    assert any(getattr(r, "purpose", "").startswith("culture_commons") for r in rels)


def test_tick_with_surfaces_flag_is_opt_in(tmp_path, monkeypatch):
    engine, registry, store, state = _surface(tmp_path, monkeypatch, FakeCommons())
    engine._surfaces.append(CultureCommonsSurface(engine))

    report = engine.tick()
    assert report.observed is not None
    snapshot = engine.store._storage.load("aster", SURFACE_NAMESPACE)
    assert snapshot is None, "surface ran without the opt-in flag"
    cc_provenance = [e for e in engine.store.list_provenance() if e.action == "culture_commons.observe"]
    assert not cc_provenance, "surface wrote provenance without the opt-in flag"

    report = engine.tick(surfaces=True)
    assert report.observed is True
    snapshot = engine.store._storage.load("aster", SURFACE_NAMESPACE)
    assert snapshot["count"] == 1


def test_provenance_scrub_against_secret_store(tmp_path, monkeypatch):
    engine, registry, store, state = _surface(tmp_path, monkeypatch, FakeCommons())
    store.put("culture-commons/aster", "ultraSecretToken0101")
    engine._provenance(
        ProvenancePhase.OBSERVE,
        "leaked ultraSecretToken0101 in summary",
        action="test.scrub",
        result="and again ultraSecretToken0101",
    )
    entry = engine.store.list_provenance()[-1]
    blob = json.dumps(entry.to_dict(), default=str)
    assert "ultraSecretToken0101" not in blob


def test_observe_failure_is_recorded_not_fake(tmp_path, monkeypatch):
    from tests.interop_stubs import RPCServerError

    class BrokenCommons(FakeCommons):
        def call(self, method, params, headers):
            raise RPCServerError(-32000, "network down")

    engine, registry, store, state = _surface(tmp_path, monkeypatch, BrokenCommons())
    surface = CultureCommonsSurface(engine)
    result = surface.observe()
    assert result["observed"] is False
    entries = engine.store.list_provenance()
    assert entries[-1].action == "culture_commons.observe"
    assert "failed" in entries[-1].summary.lower()