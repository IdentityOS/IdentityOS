"""Deterministic regression tests for the developer-first lifecycle CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli import idos
from core.identity import create_identity
from identityos.paths import get_paths
from runtime.orchestrator import IdentityRuntime
from runtime.persistence import JSONFileBackend


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "identityos-home"
    monkeypatch.setenv("IDENTITYOS_HOME", str(home))
    monkeypatch.setenv("IDENTITYOS_INSTALL_ROOT", str(tmp_path / "managed-install"))
    monkeypatch.setenv("IDENTITYOS_BIN_DIR", str(tmp_path / "bin"))
    for name in (
        "GROQ_API_KEY",
        "SAMBANOVA_API_KEY",
        "CEREBRAS_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OLLAMA_API_KEY",
        "IDENTITY_ADAPTER",
    ):
        monkeypatch.delenv(name, raising=False)
    return home


def test_init_is_idempotent_and_uses_stable_paths(isolated_home: Path, capsys: pytest.CaptureFixture[str]):
    assert idos.main(["init", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["created"] is True
    assert Path(first["identity_store"]).is_dir()

    assert idos.main(["init", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["created"] is False
    assert second["identity_store"] == first["identity_store"]
    assert json.loads(get_paths().config_file.read_text())["dashboard"]["host"] == "127.0.0.1"


def test_start_rejects_public_bind_without_explicit_override(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
):
    assert idos.main(["init"]) == 0
    capsys.readouterr()
    assert idos.main(["start", "--host", "0.0.0.0"]) == 2
    assert "local-only" in capsys.readouterr().err


def test_status_cleans_stale_pid_file(isolated_home: Path, capsys: pytest.CaptureFixture[str]):
    paths = get_paths().ensure()
    paths.pid_file.write_text(json.dumps({"pid": 999_999_999, "url": "http://127.0.0.1:8000/playground"}))

    assert idos.main(["status", "--json"]) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["running"] is False
    assert not paths.pid_file.exists()


def test_doctor_json_is_machine_readable_and_redacts_credentials(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("GROQ_API_KEY", "super-secret-do-not-print")
    assert idos.main(["init"]) == 0
    capsys.readouterr()

    assert idos.main(["doctor", "--json"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["ok"] is True
    assert any(check["name"] == "inference_provider" for check in report["checks"])
    assert "super-secret-do-not-print" not in output


def test_doctor_reports_invalid_configuration(isolated_home: Path, capsys: pytest.CaptureFixture[str]):
    paths = get_paths().ensure()
    paths.config_file.write_text("not-json", encoding="utf-8")

    assert idos.main(["doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    configuration = next(check for check in report["checks"] if check["name"] == "configuration")
    assert configuration["status"] == "fail"


def test_update_without_exact_source_is_truthful(isolated_home: Path, capsys: pytest.CaptureFixture[str]):
    assert idos.main(["update"]) == 2
    captured = capsys.readouterr()
    assert "No update was performed" in captured.err


def test_uninstall_preserves_identity_data_by_default(
    isolated_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    paths = get_paths().ensure()
    identity = paths.identity_store / "keeper" / "latest_snapshot.json"
    identity.parent.mkdir(parents=True)
    identity.write_text("{}", encoding="utf-8")

    install_root = Path.home()  # overwritten below; keeps type checkers happy
    install_root = Path(__import__("os").environ["IDENTITYOS_INSTALL_ROOT"])
    install_root.mkdir(parents=True)
    (install_root / ".identityos-install").write_text("managed\n")
    (install_root / "old-code").write_text("code")
    bin_dir = Path(__import__("os").environ["IDENTITYOS_BIN_DIR"])
    bin_dir.mkdir()
    (bin_dir / "idos").symlink_to(install_root / "old-code")

    assert idos.main(["uninstall", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data_preserved"] is True
    assert identity.exists()
    assert not install_root.exists()
    assert not (bin_dir / "idos").exists()


def test_purge_requires_explicit_confirmation(isolated_home: Path, capsys: pytest.CaptureFixture[str]):
    assert idos.main(["uninstall", "--purge-data"]) == 2
    assert "requires --yes" in capsys.readouterr().err


def test_cli_export_and_import_use_transactional_portability(
    isolated_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    source_paths = get_paths().ensure()
    runtime = IdentityRuntime(storage=JSONFileBackend(str(source_paths.identity_store)))
    runtime.register(create_identity(name="CLI Portable", identity_id="cli-portable"))
    runtime.shutdown()
    archive = tmp_path / "cli-portable.identityos.json"

    assert idos.main([
        "export", "--identity", "cli-portable", "--output", str(archive), "--json",
    ]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["identity_id"] == "cli-portable"
    assert archive.is_file()

    monkeypatch.setenv("IDENTITYOS_HOME", str(tmp_path / "second-installation"))
    assert idos.main(["import", str(archive), "--json"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["verified_after_restart"] is True
    destination = JSONFileBackend(str(get_paths().identity_store))
    assert destination.list_identities() == ["cli-portable"]
