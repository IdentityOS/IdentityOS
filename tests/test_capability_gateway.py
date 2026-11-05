"""Capability contracts, authorization, and workspace-boundary regression tests."""

from __future__ import annotations

import sys

from core.capabilities.proxy import CapabilityProxy
from core.capabilities.registry import CapabilityRegistry
from runtime.persistence import JSONFileBackend


def _registry(tmp_path) -> CapabilityRegistry:
    return CapabilityRegistry(JSONFileBackend(root_dir=str(tmp_path / "store")))


def _grant(registry: CapabilityRegistry, identity_id: str, cap_id: str, scope: str) -> None:
    registry._storage.save(
        identity_id,
        "capability.permissions",
        {"grants": [{"capability": cap_id, "permission": scope}]},
    )


def test_typed_tool_catalog_excludes_unauthorized_process_execution(tmp_path):
    import core.capabilities.command_exec  # noqa: F401

    registry = _registry(tmp_path)
    registry.install("tester", "command_exec")

    definitions, mapping = registry.tool_catalog("tester")
    assert "command_exec__run" not in mapping
    assert definitions == []

    _grant(registry, "tester", "command_exec", "process:execute")
    definitions, mapping = registry.tool_catalog("tester")
    assert mapping["command_exec__run"] == "command_exec.run"
    schema = definitions[0]["function"]["parameters"]
    assert schema["required"] == ["command"]
    assert schema["additionalProperties"] is False


def test_gateway_enforces_permission_and_parameter_contract(tmp_path):
    import core.capabilities.command_exec  # noqa: F401

    registry = _registry(tmp_path)
    registry.install("tester", "command_exec")

    denied = registry.call("tester", "command_exec.run", command="true")
    assert denied.success is False
    assert denied.error["type"] == "permission_denied"

    _grant(registry, "tester", "command_exec", "process:execute")
    invalid = registry.call("tester", "command_exec.run")
    assert invalid.success is False
    assert invalid.error["type"] == "invalid_parameters"
    assert "command" in invalid.error["message"]

    result = registry.call("tester", "command_exec.run", command="true", timeout=5)
    assert result.success is True
    assert result.data["exit_code"] == 0
    assert result.params == {"command": "true", "timeout": 5}


def test_optional_model_arguments_accept_null_and_apply_handler_default(tmp_path):
    registry = _registry(tmp_path)
    registry.install("tester", "datetime")

    definitions, mapping = registry.tool_catalog("tester")
    tool_name = next(name for name, skill in mapping.items() if skill == "datetime.now")
    definition = next(
        item for item in definitions if item["function"]["name"] == tool_name
    )
    assert definition["function"]["parameters"]["properties"]["tz_name"]["type"] == [
        "string",
        "null",
    ]

    result = registry.call("tester", "datetime.now", tz_name=None)
    assert result.success is True
    assert result.data["timezone"] == "UTC"
    assert result.params == {}


def test_tool_catalog_limits_schemas_by_query_relevance(tmp_path):
    registry = _registry(tmp_path)
    registry.install("tester", "github")

    definitions, mapping = registry.tool_catalog(
        "tester",
        query="Find the latest release of lacebx/IdentityOS on GitHub",
        limit=2,
    )

    assert len(definitions) == 2
    assert len(mapping) == 2
    assert "github__summarize_release" in mapping
    assert set(mapping) == {
        definition["function"]["name"] for definition in definitions
    }


def test_capability_prompts_follow_the_relevance_bounded_tool_catalog(tmp_path):
    registry = _registry(tmp_path)
    registry.install("tester", "github")
    registry.install("tester", "datetime")

    _, mapping = registry.tool_catalog(
        "tester", query="What time is it in UTC?", limit=1
    )
    prompts = registry.prompts_for_skills("tester", set(mapping.values()))

    assert set(mapping.values()) == {"datetime.now"}
    assert prompts == registry.get("tester", "datetime").prompts("tester")


def test_required_null_remains_an_invalid_parameter(tmp_path):
    import core.capabilities.command_exec  # noqa: F401

    registry = _registry(tmp_path)
    registry.install("tester", "command_exec")
    _grant(registry, "tester", "command_exec", "process:execute")

    definitions, mapping = registry.tool_catalog("tester")
    tool_name = next(
        name for name, skill in mapping.items() if skill == "command_exec.run"
    )
    definition = next(
        item for item in definitions if item["function"]["name"] == tool_name
    )
    assert definition["function"]["parameters"]["properties"]["command"]["type"] == "string"

    result = registry.call("tester", "command_exec.run", command=None)
    assert result.success is False
    assert result.error["type"] == "invalid_parameters"


def test_identity_proxy_uses_the_registry_gateway(tmp_path):
    import core.capabilities.command_exec  # noqa: F401

    registry = _registry(tmp_path)
    cap = registry.install("tester", "command_exec")
    proxy = CapabilityProxy(cap, registry=registry, identity_id="tester")

    denied = proxy.run(command="true")
    assert denied.success is False
    assert denied.error["type"] == "permission_denied"


def test_permission_grants_are_idempotent_persistent_and_revocable(tmp_path):
    import core.capabilities.command_exec  # noqa: F401

    store = JSONFileBackend(root_dir=str(tmp_path / "store"))
    registry = CapabilityRegistry(store)
    registry.install("tester", "command_exec")
    registry.grant("tester", "command_exec", "process:execute")
    registry.grant("tester", "command_exec", "process:execute")

    restarted = CapabilityRegistry(
        JSONFileBackend(root_dir=str(tmp_path / "store"))
    )
    grants = restarted.permissions("tester")
    assert len(grants) == 1
    assert grants[0]["capability"] == "command_exec"
    assert grants[0]["permission"] == "process:execute"
    assert restarted.call(
        "tester", "command_exec.run", command="true"
    ).success is True

    assert restarted.revoke(
        "tester", "command_exec", "process:execute"
    ) is True
    assert restarted.revoke(
        "tester", "command_exec", "process:execute"
    ) is False
    denied = restarted.call("tester", "command_exec.run", command="true")
    assert denied.success is False
    assert denied.error["type"] == "permission_denied"


def test_capability_permission_cli_arguments_are_explicit():
    from cli.main import build_parser

    args = build_parser().parse_args(
        [
            "cap",
            "grant",
            "command_exec",
            "--identity",
            "tester",
            "--permission",
            "process:execute",
        ]
    )
    assert args.cap_command == "grant"
    assert args.id == "command_exec"
    assert args.identity == "tester"
    assert args.permission == "process:execute"


def test_command_execution_does_not_expand_shell_syntax(tmp_path):
    import core.capabilities.command_exec  # noqa: F401

    registry = _registry(tmp_path)
    registry.install("tester", "command_exec")
    _grant(registry, "tester", "command_exec", "process:execute")
    marker = tmp_path / "shell-injection-marker"
    command = (
        f'{sys.executable} -c "print(\'safe\')" ; '
        f'{sys.executable} -c "open(\'{marker}\', \'w\').write(\'bad\')"'
    )

    result = registry.call("tester", "command_exec.run", command=command)
    assert result.success is True
    assert result.data["stdout"].strip() == "safe"
    assert marker.exists() is False


def test_file_mutation_requires_grant_and_stays_inside_workspace(tmp_path):
    registry = _registry(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry.install(
        "tester",
        "file_tools",
        config={"allowed_roots": [str(workspace)]},
    )

    target = workspace / "note.txt"
    denied = registry.call(
        "tester",
        "file_tools.write_file",
        path=str(target),
        content="verified",
    )
    assert denied.success is False
    assert target.exists() is False

    _grant(registry, "tester", "file_tools", "filesystem:write")
    written = registry.call(
        "tester",
        "file_tools.write_file",
        path=str(target),
        content="verified",
    )
    assert written.success is True
    assert target.read_text() == "verified"

    escaped = registry.call(
        "tester",
        "file_tools.write_file",
        path=str(tmp_path / "outside.txt"),
        content="blocked",
    )
    assert escaped.success is False
    assert escaped.error["type"] == "PermissionError"
    assert (tmp_path / "outside.txt").exists() is False
