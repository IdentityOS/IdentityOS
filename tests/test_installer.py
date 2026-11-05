"""Artifact-level tests for the isolated user-space installer."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


def _test_wheel(path: Path, *, with_command: bool = True) -> Path:
    files = {
        "cli/__init__.py": "",
        "cli/idos.py": "def main():\n    print('test idos')\n    return 0\n",
        "identityos_test-1.0.0.dist-info/METADATA": (
            "Metadata-Version: 2.1\nName: identityos-test\nVersion: 1.0.0\n"
        ),
        "identityos_test-1.0.0.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: IdentityOS test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    if with_command:
        files["identityos_test-1.0.0.dist-info/entry_points.txt"] = "[console_scripts]\nidos = cli.idos:main\n"
    record = "identityos_test-1.0.0.dist-info/RECORD"
    rows = [[name, "", ""] for name in files] + [[record, "", ""]]
    record_text = "\n".join(",".join(row) for row in rows) + "\n"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        for name, content in files.items():
            wheel.writestr(name, content)
        wheel.writestr(record, record_text)
    return path


def _run_installer(tmp_path: Path, wheel: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--artifact",
            str(wheel),
            "--python",
            sys.executable,
            "--install-root",
            str(tmp_path / "install"),
            "--bin-dir",
            str(tmp_path / "bin"),
            "--no-deps",
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_installer_rejects_multiple_install_sources(tmp_path: Path):
    artifact = tmp_path / "artifact.whl"
    artifact.write_bytes(b"not-used")
    result = subprocess.run(
        ["bash", str(INSTALLER), "--artifact", str(artifact), "--version", "1.0.0"],
        text=True,
        capture_output=True,
        check=False,
        env={"HOME": str(tmp_path), "PATH": __import__("os").environ["PATH"]},
    )
    assert result.returncode == 2
    assert "exactly one" in result.stderr


def test_installer_creates_isolated_versioned_environment_and_is_rerunnable(tmp_path: Path):
    wheel = _test_wheel(tmp_path / "identityos_test-1.0.0-py3-none-any.whl")
    first = _run_installer(tmp_path, wheel)
    assert first.returncode == 0, first.stderr
    command = tmp_path / "bin" / "idos"
    assert command.is_symlink()
    assert subprocess.run([str(command)], capture_output=True, text=True, check=True).stdout.strip() == "test idos"
    first_target = (tmp_path / "install" / "current").resolve()

    second = _run_installer(tmp_path, wheel)
    assert second.returncode == 0, second.stderr
    second_target = (tmp_path / "install" / "current").resolve()
    assert second_target != first_target
    assert first_target.exists()
    assert len(list((tmp_path / "install" / "releases").iterdir())) == 2


def test_failed_install_does_not_replace_active_release(tmp_path: Path):
    good = _test_wheel(tmp_path / "identityos_test-1.0.0-py3-none-any.whl")
    assert _run_installer(tmp_path, good).returncode == 0
    active = (tmp_path / "install" / "current").resolve()
    bad = _test_wheel(tmp_path / "identityos_test-1.0.1-py3-none-any.whl", with_command=False)

    failed = _run_installer(tmp_path, bad)
    assert failed.returncode == 1
    assert "does not provide" in failed.stderr
    assert (tmp_path / "install" / "current").resolve() == active
    assert active.exists()
