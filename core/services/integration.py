"""Bindings for runtime owners and read-only views. Polling never creates a runtime."""

from pathlib import Path
from core.services.store import ServiceStore


def database_path(storage):
    root = getattr(storage, "root", None)
    return Path(root) / ".identityos" / "services.sqlite3" if root else None


def existing_store(storage):
    path = database_path(storage)
    if not path or not path.is_file():
        return None
    store = ServiceStore.__new__(ServiceStore)
    store.path = str(path)
    return store


def runtime_for(storage, registry):
    if not existing_store(storage):
        return None
    from core.services.runtime import ServiceRuntime

    return ServiceRuntime(storage, registry, database_path(storage))


def work_cards(storage, identity):
    store = existing_store(storage)
    if not store:
        return []
    return [
        {
            "id": j["id"],
            "requester": j["requester"],
            "provider": j["provider"],
            "service": j["contract"]["service"],
            "skill": j["contract"]["skill"],
            "state": j["state"],
            "price": j["price"],
            "pricing_reason": j["reason"],
            "artifact": j["artifact"],
            "acceptance": "PASS" if j["state"] == "COMPLETED" else "not completed",
            "updated": j["updated"],
        }
        for j in store.jobs(identity)
    ]


def specification_cards(storage, identity):
    store = existing_store(storage)
    if not store or not store.rows("SELECT name FROM sqlite_master WHERE type='table' AND name='negotiations'"):
        return []
    return store.rows('SELECT id,provider,requester,state,revision,job,updated FROM negotiations WHERE requester=? OR provider=? ORDER BY updated DESC LIMIT 30', (identity, identity))
