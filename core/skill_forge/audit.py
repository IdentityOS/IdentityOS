"""Static policy checks applied before any forged source is loaded."""

from __future__ import annotations

import ast

_RISK_IMPORTS = {
    "os": "environment",
    "subprocess": "process:execute",
    "socket": "network",
    "httpx": "network",
    "requests": "network",
    "urllib": "network",
    "pathlib": "filesystem",
    "shutil": "filesystem",
}
_FORBIDDEN_IMPORTS = {"ctypes", "importlib", "marshal", "multiprocessing", "pickle"}
_FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
_SAFE_IMPORTS = {
    "__future__", "collections", "dataclasses", "datetime", "decimal", "enum", "fractions",
    "functools", "hashlib", "itertools", "json", "math", "re", "statistics",
    "string", "time", "typing", "uuid",
}
_CAPABILITY_API_MODULES = {
    "core.capabilities.base",
    "core.capabilities.registry",
    "core.capabilities.result",
}


def audit_source(
    source: str,
    allowed_permissions: set[str],
    allowed_dependencies: set[str] | None = None,
) -> list[str]:
    """Return policy violations found before any generated code executes."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error at line {exc.lineno}: {exc.msg}"]
    issues: list[str] = []
    dependencies = allowed_dependencies or set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                root = name.split(".", 1)[0]
                if root == "core" and name not in _CAPABILITY_API_MODULES:
                    issues.append(f"internal import '{name}' is outside the capability API")
                    continue
                if root in _FORBIDDEN_IMPORTS:
                    issues.append(f"forbidden import '{root}'")
                required = _RISK_IMPORTS.get(root)
                if required and required not in allowed_permissions:
                    issues.append(f"import '{root}' requires forge permission '{required}'")
                if (
                    root not in _SAFE_IMPORTS
                    and root not in _RISK_IMPORTS
                    and root not in dependencies
                    and name not in _CAPABILITY_API_MODULES
                ):
                    issues.append(f"import '{root}' is not an allowed dependency")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in _FORBIDDEN_CALLS:
                    issues.append(f"forbidden dynamic call '{node.func.id}'")
                if node.func.id == "open" and "filesystem" not in allowed_permissions:
                    issues.append("open() requires forge permission 'filesystem'")
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and node.func.attr in {"system", "popen", "spawnl", "spawnlp"}
                and "process:execute" not in allowed_permissions
            ):
                issues.append(f"os.{node.func.attr} requires forge permission 'process:execute'")
    return sorted(set(issues))
