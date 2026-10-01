"""Tests for grounded principal knowledge (Aster knows her principal).

All fetches are stubbed. Live gathering is proven separately against real
public sources during deployment, never in unit tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from core.operations.principal_knowledge import (
    _extract_repo_entries,
    _scrub_contacts,
    derive_github_user,
    fetch_principal_profile,
    load_profile,
    principal_context_lines,
    save_profile,
)
from runtime.persistence import InMemoryBackend, JSONFileBackend


def _fetcher(mapping):
    def fetch(url: str) -> str:
        if url not in mapping:
            raise RuntimeError(f"no stub for {url}")
        return mapping[url]

    return fetch


def _user_payload(**overrides):
    base = {"login": "lacebx", "name": None, "bio": "", "blog": "",
            "public_repos": 62}
    base.update(overrides)
    return json.dumps(base)


def _repos_payload():
    return json.dumps([
        {"id": 1, "name": "IdentityOS",
         "description": "Portable identity runtime",
         "language": "Python", "stargazers_count": 12},
        {"id": 2, "name": "CV",
         "description": "Developer CV in Markdown",
         "language": None, "stargazers_count": 0},
    ])


# ── derivation + scrubbing ──────────────────────────────────────────────


def test_derive_github_user():
    assert derive_github_user("https://github.com/lacebx/IdentityOS") == "lacebx"
    assert derive_github_user("not a url") == ""


def test_scrub_contacts():
    assert _scrub_contacts("mail Foo.Bar@Example.COM x") == "mail [contact redacted] x"
    assert _scrub_contacts("call (929) 473-1844") == "call [contact redacted]"
    assert _scrub_contacts("Phone (929)473-1844 Location") == "Phone [contact redacted] Location"
    assert _scrub_contacts("on 2026-09-26 shipped v2.4.1") == "on 2026-09-26 shipped v2.4.1"
    assert _scrub_contacts("see section 4.2 (2020)") == "see section 4.2 (2020)"
    assert _scrub_contacts("plain words here") == "plain words here"


# ── gathering ───────────────────────────────────────────────────────────


def test_gather_partial_results_never_raise():
    profile = fetch_principal_profile(_fetcher({}), github_user="lacebx")
    assert profile.github_user == "lacebx"
    assert profile.is_empty() is True
    assert fetch_principal_profile(_fetcher({}), github_user="").is_empty() is True


def test_gather_full_profile():
    mapping = {
        "https://api.github.com/users/lacebx": _user_payload(
            name="Arsène Manzi", bio="Builder of things",
            blog="https://example.org"),
        "https://api.github.com/users/lacebx/repos?sort=updated&per_page=100": _repos_payload(),
    }
    profile = fetch_principal_profile(
        _fetcher(mapping), github_user="lacebx",
        extractor=lambda url: ("Arsène MANZI Software Engineer working on open source. "
                               "Email a@b.cc Phone (111) 222-3333, building identity "
                               "systems and developer tools for collaboration.") if "github.io" in url else "")
    assert profile.github_name == "Arsène Manzi"
    assert [r["name"] for r in profile.top_repos] == ["IdentityOS", "CV"]
    assert "111" not in profile.portfolio_text and "a@b.cc" not in profile.portfolio_text
    assert "[contact redacted]" in profile.portfolio_text
    assert profile.fetched_at and profile.sources
    assert profile.is_fresh() is True
    assert profile.is_empty() is False


def test_repo_entries_tolerate_truncation():
    full = _repos_payload()
    entries = _extract_repo_entries(full[: len(full) // 2] + '{"id": 3, "name": "Broken')
    names = [e["name"] for e in entries]
    assert "IdentityOS" in names
    assert "Broken" not in names


def test_cached_profile_used_until_stale(tmp_path):
    store_dir = str(tmp_path / "store")
    storage = JSONFileBackend(root_dir=store_dir)
    mapping = {"https://api.github.com/users/lacebx": _user_payload(name="Arsène Manzi"),
               "https://api.github.com/users/lacebx/repos?sort=updated&per_page=100": _repos_payload()}
    fresh = fetch_principal_profile(_fetcher(mapping), github_user="lacebx")
    save_profile(storage, "aster", fresh)
    reloaded = load_profile(JSONFileBackend(root_dir=store_dir), "aster")
    assert reloaded.is_fresh() is True
    assert reloaded.github_name == "Arsène Manzi"
    old = load_profile(InMemoryBackend(), "nobody")
    assert old.is_fresh() is False and old.is_empty() is True
    stale = fetch_principal_profile(_fetcher(mapping), github_user="lacebx")
    stale.fetched_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    assert stale.is_fresh() is False


# ── context lines ───────────────────────────────────────────────────────


def test_context_lines_cite_only_verified():
    mapping = {"https://api.github.com/users/lacebx": _user_payload(),
               "https://api.github.com/users/lacebx/repos?sort=updated&per_page=100": _repos_payload()}
    profile = fetch_principal_profile(_fetcher(mapping), github_user="lacebx")
    lines = principal_context_lines(profile)
    assert any("lacebx" in line for line in lines)
    assert any("never invent" in line for line in lines)
    assert principal_context_lines(load_profile(InMemoryBackend(), "ghost")) == []


# ── reply integration: principal thread enriched, strangers isolated ─────


def _monitor_engine(tmp_path, storage=None, adapter=None):
    from core.capabilities.email.backends import FileMailboxBackend, MailboxTransport
    from core.operations import ControlState, OperationsEngine, OperatorConfig, PresenceStore

    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "README.md").write_text("# P\n\nContext.\n", encoding="utf-8")
    storage = storage or InMemoryBackend()
    config = OperatorConfig(
        identity_id="aster", project_root=str(root), project_name="IdentityOS",
        sender_name="Aster", purpose="outreach", need_rules=[], candidate_sources=[],
        required_skills=[],
    )
    engine = OperationsEngine(storage, config, transport=MailboxTransport(
        FileMailboxBackend(tmp_path / "mail", mailbox="aster")),
        adapter=adapter, presence=PresenceStore(storage, "aster"))
    engine.store.set_controls(ControlState(outbound_mode="autonomous"))
    return engine


class _EchoAdapter:
    model = "echo"

    def __init__(self):
        self.contexts = []
        self.inputs = []

    def generate(self, context, user_input, identity, **kwargs):
        self.contexts.append(context)
        self.inputs.append(user_input)
        return "Subject: Re: hi\nThanks for writing."


def test_principal_thread_gets_broad_context(tmp_path):
    from core.operations.principal import ensure_builder_relationship
    from core.operations.principal_knowledge import save_profile, PrincipalProfile

    storage = InMemoryBackend()
    adapter = _EchoAdapter()
    engine = _monitor_engine(tmp_path, storage=storage, adapter=adapter)
    profile = PrincipalProfile(github_user="lacebx", github_name="Arsène Manzi",
                               top_repos=[{"name": "IdentityOS", "description": "runtime",
                                           "language": "Python", "stars": "3"}],
                               fetched_at=datetime.now(timezone.utc).isoformat(),
                               sources=["test"])
    save_profile(storage, "aster", profile)
    rel = ensure_builder_relationship(engine.store)
    rel.email = "boss@example.org"
    rel.thread_ids.append("t-principal")
    engine.store.update_relationship(rel)
    engine.monitor.ingest(
        engine.store, sender_email="boss@example.org",
        body="Thanks Aster, keep up the good work on all of this.",
        subject="Thanks", thread_id="t-principal", external_id="e-principal",
        need_rules=[], project_facts=["IdentityOS persists state."],
        principal_domains=[])
    assert adapter.contexts, "model must be consulted for the principal thread"
    seen = " ".join(adapter.contexts) + " " + " ".join(adapter.inputs)
    assert "lacebx" in seen


def test_stranger_reply_gets_no_principal_facts(tmp_path):
    storage = InMemoryBackend()
    adapter = _EchoAdapter()
    engine = _monitor_engine(tmp_path, storage=storage, adapter=adapter)
    from core.operations.principal_knowledge import save_profile, PrincipalProfile

    save_profile(storage, "aster", PrincipalProfile(
        github_user="lacebx", portfolio_text="secret-adjacent bio",
        fetched_at=datetime.now(timezone.utc).isoformat(), sources=["test"]))
    engine.monitor.ingest(
        engine.store, sender_email="stranger@example.org",
        body="We fund open source identity work, interested?",
        subject="Grant", thread_id="t-s", external_id="e-s",
        need_rules=[], project_facts=["IdentityOS persists state."],
        principal_domains=[])
    assert adapter.contexts, "model must be consulted"
    seen = " ".join(adapter.contexts) + " " + " ".join(adapter.inputs)
    assert "secret-adjacent" not in seen
    assert "lacebx" not in seen
