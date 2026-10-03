"""test_capability_acquisition.py — generic skill → capability acquisition."""

from __future__ import annotations

from core.capabilities.registry import CapabilityRegistry
from core.operations.skill_acquisition import (
    SKILL_PREFIX_PROVIDERS,
    SkillAcquisitionResolver,
    build_interop_required_skills,
)
from runtime.persistence import InMemoryBackend


def _registry_and_resolver(tmp_path):
    storage = InMemoryBackend()
    registry = CapabilityRegistry(storage)
    resolver = SkillAcquisitionResolver(
        storage,
        "aster",
        registry=registry,
        secret_store_dir=str(tmp_path / "secrets"),
        state_dir=str(tmp_path / "cc-state"),
    )
    return registry, resolver


def test_prefix_table_maps_every_interop_skill():
    assert ("mcp.", "mcp") in SKILL_PREFIX_PROVIDERS
    assert ("a2a.", "a2a") in SKILL_PREFIX_PROVIDERS
    assert ("culture_commons.", "culture_commons") in SKILL_PREFIX_PROVIDERS
    assert ("cc.", "culture_commons") in SKILL_PREFIX_PROVIDERS


def test_required_skills_are_the_three_surfaces():
    skills = build_interop_required_skills()
    assert skills == ["mcp.discover", "a2a.discover", "culture_commons.observe"]


def test_acquire_installs_provider_capability(tmp_path):
    registry, resolver = _registry_and_resolver(tmp_path)
    ok, detail = resolver("mcp.discover")
    assert ok is True
    assert "mcp" in detail
    assert registry.get("aster", "mcp") is not None


def test_acquire_installs_culture_commons_with_config(tmp_path):
    registry, resolver = _registry_and_resolver(tmp_path)
    ok, detail = resolver("culture_commons.observe")
    assert ok is True
    cap = registry.get("aster", "culture_commons")
    assert cap is not None
    assert cap._config["state_dir"] == str(tmp_path / "cc-state")


def test_acquire_is_idempotent(tmp_path):
    registry, resolver = _registry_and_resolver(tmp_path)
    ok, _ = resolver("a2a.discover")
    assert ok is True
    ok, detail = resolver("a2a.discover")
    assert ok is True
    assert "already installed" in detail


def test_unknown_skill_is_honestly_unavailable(tmp_path):
    _, resolver = _registry_and_resolver(tmp_path)
    ok, detail = resolver("mind.reading")
    assert ok is False
    assert "no interop provider" in detail


def test_acquisition_without_registry_does_not_claim_success(tmp_path):
    storage = InMemoryBackend()
    resolver = SkillAcquisitionResolver(storage, "aster")
    ok, detail = resolver("mcp.discover")
    assert ok is False
    assert "no capability registry" in detail


def test_cc_alias_prefix(tmp_path):
    registry, resolver = _registry_and_resolver(tmp_path)
    ok, _ = resolver("cc.observe")
    assert ok is True
    assert registry.get("aster", "culture_commons") is not None


def test_acquired_capability_is_usable_through_registry(tmp_path):
    registry, resolver = _registry_and_resolver(tmp_path)
    resolver("mcp.call")
    cap = registry.get("aster", "mcp")
    skill_names = {s.name for s in cap.skills()}
    assert "mcp.discover" in skill_names and "mcp.call" in skill_names