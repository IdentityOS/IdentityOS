"""
core/operations/observer.py

Project understanding for an operator identity.

The observer produces an evidence-backed :class:`ProjectState` from real,
inspectable artifacts (README, package metadata, docs, optional git history).
It never trusts a model's description of the project; every fact carries a
file path or command as evidence (source type, path, observation time, and the
commit that was current when the fact was observed).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from .models import ProjectFact, ProjectState, utcnow


class ProjectStateObserver:
    """Reads a project directory and summarizes it with verifiable evidence."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)
        self._root = self._resolve_root(self.project_root)

    # ── public API ────────────────────────────────────────────────────

    def observe(
        self,
        *,
        extra_facts: Optional[list[str]] = None,
        extra_evidence: Optional[list[str]] = None,
        evidence_provider: Optional[Callable[[], list[str]]] = None,
    ) -> ProjectState:
        commit_sha = self._git_head_sha()
        detail_facts: list[ProjectFact] = []
        evidence: list[str] = []

        name = self._read_package_name()
        if name:
            detail_facts.append(self._fact(
                f"Project name: {name}", "metadata", "pyproject.toml", commit_sha,
            ))
            evidence.append("pyproject.toml / package metadata")

        description = self._read_package_description()
        if description:
            detail_facts.append(self._fact(
                f"Project description: {description}", "metadata", "pyproject.toml", commit_sha,
            ))
            evidence.append("pyproject.toml / package metadata")

        readme_text = self._read_first("README.md", "README.rst", "readme.md")
        if readme_text:
            summary = self._summarize_text(readme_text)
            detail_facts.append(self._fact(
                f"README summary: {summary}", "readme", str(self._root / "README.md"), commit_sha,
            ))
            evidence.append("README.md")

        version = self._read_version()
        if version:
            detail_facts.append(self._fact(
                f"Declared version: {version}", "metadata", "pyproject.toml", commit_sha,
            ))
            evidence.append("pyproject.toml")

        docs = self._list_docs()
        if docs:
            detail_facts.append(self._fact(
                f"Documentation files: {', '.join(docs[:12])}", "docs", "docs/", commit_sha,
            ))
            evidence.append("docs/")

        capabilities = self._list_capabilities()
        if capabilities:
            detail_facts.append(self._fact(
                f"Declared capabilities: {', '.join(capabilities[:20])}", "registry",
                "registry/capabilities/index.json", commit_sha,
            ))
            evidence.append("registry/capabilities/index.json")

        tests = self._count_tests()
        if tests:
            detail_facts.append(self._fact(
                f"Test functions detected: {tests}", "tests", "tests/", commit_sha,
            ))
            evidence.append("tests/")

        git_head = self._git_last_commit()
        if git_head:
            detail_facts.append(self._fact(
                f"Latest commit: {git_head}", "git", "git log -1", commit_sha,
            ))
            evidence.append("git log -1")

        for statement in extra_facts or []:
            detail_facts.append(self._fact(statement, "external", "", commit_sha))
        for statement in extra_evidence or []:
            detail_facts.append(self._fact(statement, "external", "", commit_sha))
        if evidence_provider is not None:
            for statement in evidence_provider() or []:
                detail_facts.append(self._fact(statement, "external", "", commit_sha))
                evidence.append(statement)

        return ProjectState(
            project_id=name or self._root.name,
            name=name or self._root.name,
            summary=detail_facts[0].statement if detail_facts else "",
            facts=[f.statement for f in detail_facts],
            fact_details=detail_facts,
            evidence=evidence,
            observed_at=utcnow().isoformat(),
            metadata={"commit_sha": commit_sha, "resolved_root": str(self._root)},
        )

    # ── root resolution ───────────────────────────────────────────────

    def _resolve_root(self, given: Path) -> Path:
        """Return the real project root when ``given`` points at a wrapper dir.

        A common operator mistake is to point at a parent directory that
        *contains* the repo (e.g. ``~/Documents/Doug`` wrapping
        ``~/Documents/Doug/IdentityOS``).  We walk a few shallow levels and,
        only when unambiguous, resolve to the child that actually declares the
        project (``pyproject.toml``).  Ambiguous layouts keep the given root so
        we never guess the wrong repository.
        """
        if self._declares_project(given):
            return given

        candidates: list[tuple[Path, int]] = []
        for depth, pattern in ((1, "*/pyproject.toml"), (2, "*/*/pyproject.toml")):
            for toml in given.glob(pattern):
                root = toml.parent
                score = sum((
                    2 if (root / ".git").exists() else 0,
                    1 if (root / "README.md").is_file() else 0,
                    1 if (root / "tests").is_dir() else 0,
                ))
                candidates.append((root, score))
        if not candidates:
            return given
        candidates.sort(key=lambda item: item[1], reverse=True)
        best, best_score = candidates[0]
        if best_score > 0 and sum(1 for _, s in candidates if s == best_score) == 1:
            return best
        return given

    @staticmethod
    def _declares_project(root: Path) -> bool:
        return (root / "pyproject.toml").is_file() or (root / "setup.py").is_file() or (root / ".git").exists()

    # ── internals ─────────────────────────────────────────────────────

    def _fact(self, statement: str, source_type: str, source_path: str, commit_sha: str) -> ProjectFact:
        digest = hashlib.sha1(statement.encode("utf-8")).hexdigest()[:10]
        return ProjectFact(
            id=f"fact-{digest}",
            statement=statement,
            source_type=source_type,
            source_path=source_path,
            observed_at=utcnow().isoformat(),
            commit_sha=commit_sha,
        )

    def _read_first(self, *names: str) -> str:
        for name in names:
            path = self._root / name
            if path.is_file():
                try:
                    return path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
        return ""

    def _read_package_name(self) -> str:
        data = self._read_pyproject()
        project = data.get("project", {}) if isinstance(data, dict) else {}
        return str(project.get("name", "") or "")

    def _read_package_description(self) -> str:
        data = self._read_pyproject()
        project = data.get("project", {}) if isinstance(data, dict) else {}
        return str(project.get("description", "") or "")

    def _read_version(self) -> str:
        data = self._read_pyproject()
        project = data.get("project", {}) if isinstance(data, dict) else {}
        return str(project.get("version", "") or "")

    def _read_pyproject(self) -> dict[str, Any]:
        text = self._read_first("pyproject.toml")
        if not text:
            return {}
        # Prefer a real TOML parser when available; fall back to a tiny regex.
        try:
            import tomllib  # type: ignore

            return tomllib.loads(text)
        except Exception:
            pass
        name = re.search(r'name\s*=\s*"([^"]+)"', text)
        version = re.search(r'version\s*=\s*"([^"]+)"', text)
        description = re.search(r'description\s*=\s*"([^"]+)"', text)
        project: dict[str, Any] = {}
        if name:
            project["name"] = name.group(1)
        if version:
            project["version"] = version.group(1)
        if description:
            project["description"] = description.group(1)
        return {"project": project} if project else {}

    def _summarize_text(self, text: str, limit: int = 400) -> str:
        lines = [ln.strip() for ln in text.splitlines()]
        # Drop the title line, badge-only lines, and raw HTML tag lines.
        body = [
            ln for ln in lines
            if ln and not ln.startswith("#") and not ln.startswith("[!")
            and not ln.startswith("![") and not ln.startswith("<")
        ]
        return " ".join(body)[:limit]

    def _list_docs(self) -> list[str]:
        docs_dir = self._root / "docs"
        if not docs_dir.is_dir():
            return []
        return sorted(p.name for p in docs_dir.iterdir() if p.is_file())

    def _list_capabilities(self) -> list[str]:
        index = self._root / "registry" / "capabilities" / "index.json"
        if not index.is_file():
            return []
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = data if isinstance(data, list) else data.get("capabilities", [])
        names: list[str] = []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("id"):
                names.append(str(entry["id"]))
        return names

    def _count_tests(self) -> int:
        tests_dir = self._root / "tests"
        if not tests_dir.is_dir():
            return 0
        count = 0
        for path in tests_dir.glob("test_*.py"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            count += len(re.findall(r"^\s*def test_", text, flags=re.MULTILINE))
        return count

    def _git_head_sha(self) -> str:
        git_dir = self._root / ".git"
        if not git_dir.exists():
            return ""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    def _git_last_commit(self) -> str:
        git_dir = self._root / ".git"
        if not git_dir.exists():
            return ""
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--pretty=%h %s"],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""