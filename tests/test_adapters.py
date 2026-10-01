"""
Tests for all LLM adapters.

Uses mocked HTTP responses so tests are fast and need no real API keys.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

# Install mock modules so adapter lazy-imports resolve during testing
_openai_mock = MagicMock()
_anthropic_mock = MagicMock()
sys.modules["openai"] = _openai_mock
sys.modules["anthropic"] = _anthropic_mock

from adapters.base import BaseAdapter  # noqa: E402
from adapters.openai_adapter import OpenAIAdapter, AnthropicAdapter, OllamaAdapter, list_ollama_models, ollama_model_capabilities, resolve_ollama_model  # noqa: E402
from adapters.openrouter_adapter import OpenRouterAdapter  # noqa: E402
from adapters import get_adapter  # noqa: E402


# ---------------------------------------------------------------------------
# Mock identity for adapter.generate()
# ---------------------------------------------------------------------------

class _MockIdentity:
    id = "test-identity"
    name = "TestBot"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_openai_client():
    """Mock the openai.OpenAI client so no real API call is made."""
    _openai_mock.OpenAI = MagicMock()
    client = MagicMock()
    _openai_mock.OpenAI.return_value = client

    choice = MagicMock()
    choice.message.content = "Hello from the mock!"

    completion = MagicMock()
    completion.choices = [choice]
    client.chat.completions.create.return_value = completion
    yield _openai_mock.OpenAI


@pytest.fixture
def mock_anthropic_client():
    """Mock the anthropic.Anthropic client."""
    _anthropic_mock.Anthropic = MagicMock()
    client = MagicMock()
    _anthropic_mock.Anthropic.return_value = client

    content_block = MagicMock()
    content_block.text = "Hello from Claude mock!"

    message = MagicMock()
    message.content = [content_block]
    client.messages.create.return_value = message
    yield _anthropic_mock.Anthropic


# ---------------------------------------------------------------------------
# BaseAdapter tests
# ---------------------------------------------------------------------------

class TestBaseAdapter:
    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            BaseAdapter()  # type: ignore[abstract]

    def test_build_messages(self):
        class ConcreteAdapter(BaseAdapter):
            def generate(self, context, user_input, identity, **kwargs):
                return "test"

        adapter = ConcreteAdapter(model="test-model")
        msgs = adapter.build_messages("system context", "user text")
        assert len(msgs) == 2
        assert msgs[0].role == "system"
        assert msgs[0].content == "system context"
        assert msgs[1].role == "user"
        assert msgs[1].content == "user text"

    def test_health_check_default(self):
        class ConcreteAdapter(BaseAdapter):
            def generate(self, context, user_input, identity, **kwargs):
                return "test"

        adapter = ConcreteAdapter()
        assert adapter.health_check() is True

    def test_repr(self):
        class ConcreteAdapter(BaseAdapter):
            def generate(self, context, user_input, identity, **kwargs):
                return "test"

        adapter = ConcreteAdapter(model="gpt-4o")
        assert repr(adapter) == "ConcreteAdapter(model='gpt-4o')"


# ---------------------------------------------------------------------------
# OpenAIAdapter tests
# ---------------------------------------------------------------------------

class TestOpenAIAdapter:
    def test_sdk_retries_are_disabled_because_adapter_owns_retry_policy(
        self, mock_openai_client
    ):
        adapter = OpenAIAdapter(api_key="sk-test", timeout=7)
        adapter._get_client()
        kwargs = mock_openai_client.call_args.kwargs
        assert kwargs["max_retries"] == 0
        assert adapter.timeout == 7.0

    def test_timeout_uses_the_installed_sdk_transport_type(self):
        # This module intentionally replaces sys.modules['openai'] with a mock.
        # A fresh interpreter verifies the actual installed SDK and socket timeout.
        import subprocess
        subprocess.run([sys.executable, "-c", '''
import socket, threading, time
from openai import APITimeoutError, Timeout
from adapters.openai_adapter import OpenAIAdapter
server = socket.socket()
server.bind(('127.0.0.1', 0))
server.listen()
release = threading.Event()
received_at = []
def stalled():
    conn, _ = server.accept()
    with conn:
        conn.recv(65536)
        received_at.append(time.monotonic())
        release.wait(2)
thread = threading.Thread(target=stalled, daemon=True)
thread.start()
client = OpenAIAdapter(api_key='test-local', timeout=.1,
    base_url=f'http://127.0.0.1:{server.getsockname()[1]}/v1')._get_client()
try:
    assert isinstance(client.timeout, Timeout)
    assert client._client.timeout.connect == 5.0
    assert client._client.timeout.read == .1
    try:
        client.chat.completions.create(model='test', messages=[{'role':'user','content':'hello'}])
    except APITimeoutError:
        # Time the stalled socket, not SDK lazy imports under CPU contention.
        assert received_at, 'The request never reached the local server'
        assert time.monotonic() - received_at[0] < 1.5
    else:
        raise AssertionError('Stalled local server did not time out')
finally:
    release.set()
    thread.join(3)
    server.close()
    client.close()
'''], check=True, timeout=30)

    def test_timeout_defaults_from_environment(self, monkeypatch):
        monkeypatch.setenv("OPENAI_TIMEOUT", "19.25")

        adapter = OpenAIAdapter(api_key="sk-test")

        assert adapter.timeout == 19.25

    @pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
    def test_explicit_invalid_timeout_is_rejected(self, value):
        with pytest.raises(ValueError, match="OPENAI_TIMEOUT must be a positive number"):
            OpenAIAdapter(api_key="sk-test", timeout=value)

    def test_generate(self, mock_openai_client):
        adapter = OpenAIAdapter(api_key="sk-test")
        result = adapter.generate(
            context="You are a helpful assistant.",
            user_input="Hello!",
            identity=_MockIdentity(),
        )
        assert result == "Hello from the mock!"
        mock_openai_client.return_value.chat.completions.create.assert_called_once()

    def test_generate_with_custom_temperature(self, mock_openai_client):
        adapter = OpenAIAdapter(api_key="sk-test", temperature=0.5)
        adapter.generate(
            context="Be concise.",
            user_input="Hi",
            identity=_MockIdentity(),
        )
        call_kwargs = mock_openai_client.return_value.chat.completions.create.call_args[1]
        assert call_kwargs["temperature"] == 0.5

    def test_generate_with_custom_max_tokens(self, mock_openai_client):
        adapter = OpenAIAdapter(api_key="sk-test", max_tokens=500)
        adapter.generate(
            context="Be concise.",
            user_input="Hi",
            identity=_MockIdentity(),
        )
        call_kwargs = mock_openai_client.return_value.chat.completions.create.call_args[1]
        assert call_kwargs["max_tokens"] == 500

    def test_tool_round_limit_reserves_a_schema_free_synthesis_request(
        self, mock_openai_client
    ):
        client = mock_openai_client.return_value
        tool_call = MagicMock()
        tool_call.id = "call-1"
        tool_call.function.name = "datetime__now"
        tool_call.function.arguments = '{"tz_name": "UTC"}'
        client.chat.completions.create.side_effect = [
            MagicMock(
                choices=[MagicMock(message=MagicMock(content="", tool_calls=[tool_call]))]
            ),
            MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(content="It is noon UTC.", tool_calls=None)
                    )
                ]
            ),
        ]
        tools = [{
            "type": "function",
            "function": {
                "name": "datetime__now",
                "description": "Get the current time",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        executed = []

        adapter = OpenAIAdapter(api_key="sk-test", max_tool_rounds=1)
        result = adapter.generate(
            context="Use evidence.",
            user_input="What time is it?",
            identity=_MockIdentity(),
            tools=tools,
            tool_choice="auto",
            execute_tool=lambda name, args: executed.append((name, args))
            or '{"datetime":"12:00","timezone":"UTC"}',
        )

        assert result == "It is noon UTC."
        assert executed == [("datetime__now", {"tz_name": "UTC"})]
        calls = client.chat.completions.create.call_args_list
        assert calls[0].kwargs["tools"] == tools
        assert calls[0].kwargs["tool_choice"] == "auto"
        assert "tools" not in calls[1].kwargs
        assert "tool_choice" not in calls[1].kwargs

    def test_tool_round_limit_does_not_execute_an_unbounded_loop(
        self, mock_openai_client
    ):
        client = mock_openai_client.return_value
        first_call = MagicMock()
        first_call.id = "call-1"
        first_call.function.name = "datetime__now"
        first_call.function.arguments = "{}"
        unexpected_call = MagicMock()
        unexpected_call.id = "call-2"
        unexpected_call.function.name = "datetime__now"
        unexpected_call.function.arguments = "{}"
        client.chat.completions.create.side_effect = [
            MagicMock(
                choices=[MagicMock(message=MagicMock(content="", tool_calls=[first_call]))]
            ),
            MagicMock(
                choices=[
                    MagicMock(message=MagicMock(content="", tool_calls=[unexpected_call]))
                ]
            ),
        ]
        executed = []
        adapter = OpenAIAdapter(api_key="sk-test", max_tool_rounds=1)

        with pytest.raises(RuntimeError, match="tool-call limit reached"):
            adapter.generate(
                context="Use evidence.",
                user_input="Keep calling forever",
                identity=_MockIdentity(),
                tools=[{"type": "function", "function": {"name": "datetime__now"}}],
                execute_tool=lambda name, args: executed.append((name, args)) or "{}",
            )

        assert executed == [("datetime__now", {})]

    def test_health_check_success(self, mock_openai_client):
        adapter = OpenAIAdapter(api_key="sk-test")
        assert adapter.health_check() is True

    def test_health_check_failure(self):
        with patch("openai.OpenAI") as mock:
            client = MagicMock()
            mock.return_value = client
            client.models.list.side_effect = Exception("API error")
            adapter = OpenAIAdapter(api_key="sk-test")
            assert adapter.health_check() is False

    def test_lazy_client(self):
        adapter = OpenAIAdapter(api_key="sk-test")
        assert adapter._client is None
        # First call triggers _get_client
        with patch("openai.OpenAI") as mock:
            mock.return_value = MagicMock()
            adapter.generate(
                context="test", user_input="test", identity=_MockIdentity(),
            )
            assert adapter._client is not None

    def test_recovers_from_legacy_function_call_error(self, mock_openai_client):
        """Groq 400 tool_use_failed (legacy <function=...> syntax) should recover.

        The rejected generation is executed as a real tool call and the model
        is retried with the tool result in context.
        """
        client = mock_openai_client.return_value
        err_msg = (
            "Error code: 400 - {'error': {'message': 'Failed to call a function. "
            "See failed_generation.', 'code': 'tool_use_failed', 'failed_generation': "
            "\"<function=executive__start_task>{'goal': 'getting_to_know_each_other'}</function>\"}}"
        )
        client.chat.completions.create.side_effect = [
            RuntimeError(err_msg),
            MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(content="Task committed!", tool_calls=None)
                    )
                ]
            ),
        ]

        executed = []

        def execute_tool(func_name, args):
            executed.append((func_name, args))
            return '{"task_id": "abc", "status": "queued"}'

        adapter = OpenAIAdapter(api_key="sk-test")
        result = adapter.generate(
            context="You are a helpful assistant.",
            user_input="start a task",
            identity=_MockIdentity(),
            execute_tool=execute_tool,
        )
        assert result == "Task committed!"
        assert executed == [("executive__start_task", {"goal": "getting_to_know_each_other"})]

    def test_recovers_rejected_unknown_structured_tool_as_error_evidence(
        self, mock_openai_client
    ):
        client = mock_openai_client.return_value
        err_msg = (
            "Error code: 400 - {'error': {'code': 'tool_use_failed', "
            "'failed_generation': '{\"name\": \"web_search\", \"arguments\": "
            "{\"query\": \"efficient inference\"}}'}}"
        )
        client.chat.completions.create.side_effect = [
            RuntimeError(err_msg),
            MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(
                            content="That search tool is unavailable, so I cannot verify it.",
                            tool_calls=None,
                        )
                    )
                ]
            ),
        ]
        executed = []
        adapter = OpenAIAdapter(api_key="sk-test", max_tool_rounds=1)

        result = adapter.generate(
            context="Use only runtime evidence.",
            user_input="Research efficient inference",
            identity=_MockIdentity(),
            tools=[{"type": "function", "function": {"name": "github__search_repositories"}}],
            execute_tool=lambda name, args: executed.append((name, args))
            or '{"error":"Unknown tool: web_search"}',
        )

        assert "unavailable" in result
        assert executed == [("web_search", {"query": "efficient inference"})]
        second_call = client.chat.completions.create.call_args_list[1]
        assert "tools" not in second_call.kwargs
        assert any(
            "Unknown tool: web_search" in message["content"]
            for message in second_call.kwargs["messages"]
            if isinstance(message.get("content"), str)
        )

    def test_retries_unparsed_tool_mode_output_without_tools(self, mock_openai_client):
        client = mock_openai_client.return_value
        err_msg = (
            "Error code: 400 - {'error': {'code': 'output_parse_failed', "
            "'message': 'Parsing failed. See failed_generation', "
            "'failed_generation': 'I cannot verify that claim.'}}"
        )
        client.chat.completions.create.side_effect = [
            RuntimeError(err_msg),
            MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(
                            content="I cannot verify that claim without evidence.",
                            tool_calls=None,
                        )
                    )
                ]
            ),
        ]
        adapter = OpenAIAdapter(api_key="sk-test", max_tool_rounds=1)

        result = adapter.generate(
            context="Use only runtime evidence.",
            user_input="Can you confirm this claim?",
            identity=_MockIdentity(),
            tools=[{"type": "function", "function": {"name": "github__get_repository"}}],
            execute_tool=lambda name, args: "unused",
        )

        assert result == "I cannot verify that claim without evidence."
        second_call = client.chat.completions.create.call_args_list[1]
        assert "tools" not in second_call.kwargs
        assert "cannot verify" in second_call.kwargs["messages"][-1]["content"]

    def test_recovers_once_when_model_calls_tool_after_tools_are_disabled(
        self, mock_openai_client
    ):
        client = mock_openai_client.return_value
        parse_error = (
            "Error code: 400 - {'error': {'code': 'output_parse_failed', "
            "'message': 'Parsing failed. See failed_generation', "
            "'failed_generation': 'I need a tool.'}}"
        )
        disabled_tool_error = (
            "Error code: 400 - {'error': {'code': 'tool_use_failed', "
            "'message': 'Tool choice is none, but model called a tool', "
            "'failed_generation': '{\"name\": \"github_get_release\", "
            "\"arguments\": {\"owner\": \"lacebx\", \"repo\": \"IdentityOS\"}}'}}"
        )
        client.chat.completions.create.side_effect = [
            RuntimeError(parse_error),
            RuntimeError(disabled_tool_error),
            MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(
                            content="I cannot verify the release without runtime evidence.",
                            tool_calls=None,
                        )
                    )
                ]
            ),
        ]
        executed = []
        adapter = OpenAIAdapter(api_key="sk-test", max_tool_rounds=1)

        result = adapter.generate(
            context="Use only runtime evidence.",
            user_input="What is the latest release?",
            identity=_MockIdentity(),
            retries=1,
            tools=[{"type": "function", "function": {"name": "github__get_release"}}],
            execute_tool=lambda name, args: executed.append((name, args)),
        )

        assert result == "I cannot verify the release without runtime evidence."
        assert executed == []
        calls = client.chat.completions.create.call_args_list
        assert len(calls) == 3
        assert "tools" not in calls[1].kwargs
        assert "tools" not in calls[2].kwargs
        assert "Do not claim an action occurred" in calls[2].kwargs["messages"][0]["content"]

    def test_recovers_when_no_tools_were_offered_but_model_calls_one(
        self, mock_openai_client
    ):
        client = mock_openai_client.return_value
        disabled_tool_error = (
            "Error code: 400 - {'error': {'message': "
            "'Tool choice is none, but model called a tool', "
            "'type': 'invalid_request_error', 'code': 'tool_use_failed', "
            "'failed_generation': '{\"name\": \"repo_browser.print_tree\", "
            "\"arguments\": {\"path\": \"\", \"depth\": 2}\\n}'}}"
        )
        client.chat.completions.create.side_effect = [
            RuntimeError(disabled_tool_error),
            MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(
                            content="I cannot inspect a repository without runtime evidence.",
                            tool_calls=None,
                        )
                    )
                ]
            ),
        ]
        adapter = OpenAIAdapter(api_key="sk-test", max_tool_rounds=4)

        result = adapter.generate(
            context="Use only runtime evidence.",
            user_input="Inspect the repository.",
            identity=_MockIdentity(),
            retries=1,
        )

        assert result == "I cannot inspect a repository without runtime evidence."
        calls = client.chat.completions.create.call_args_list
        assert len(calls) == 2
        assert "tools" not in calls[0].kwargs
        assert "tools" not in calls[1].kwargs
        assert "Do not claim an action occurred" in calls[1].kwargs["messages"][0]["content"]

    def test_recovers_truncated_tool_call_when_tools_were_not_offered(
        self, mock_openai_client
    ):
        client = mock_openai_client.return_value
        truncated_disabled_tool_error = (
            "Error code: 400 - {'error': {'message': "
            "'Tool choice is none, but model called a tool', "
            "'type': 'invalid_request_error', 'code': 'tool_use_failed', "
            "'failed_generation': '{\"name\": \"browser.run\", "
            "\"arguments\": {\"id\":\"1\",\"params\":{\"command\":[\"bash\"}'}}"
        )
        client.chat.completions.create.side_effect = [
            RuntimeError(truncated_disabled_tool_error),
            MagicMock(
                choices=[MagicMock(message=MagicMock(
                    content="I cannot inspect that without runtime evidence.",
                    tool_calls=None,
                ))]
            ),
        ]
        adapter = OpenAIAdapter(api_key="sk-test", max_tool_rounds=1)

        result = adapter.generate(
            context="Use only runtime evidence.",
            user_input="Inspect the repository.",
            identity=_MockIdentity(),
            retries=1,
        )

        assert result == "I cannot inspect that without runtime evidence."
        calls = client.chat.completions.create.call_args_list
        assert len(calls) == 2
        assert "tools" not in calls[0].kwargs
        assert "tools" not in calls[1].kwargs
        assert "Do not claim an action occurred" in calls[1].kwargs["messages"][0]["content"]

    def test_repeated_truncated_disabled_tool_call_returns_non_execution_evidence(
        self, mock_openai_client
    ):
        client = mock_openai_client.return_value
        truncated_disabled_tool_error = (
            "Error code: 400 - {'error': {'message': "
            "'Tool choice is none, but model called a tool', "
            "'code': 'tool_use_failed', 'failed_generation': "
            "'{\"name\": \"curl\", \"arguments\": {\"url\":\"}'}}"
        )
        client.chat.completions.create.side_effect = [
            RuntimeError(truncated_disabled_tool_error),
            RuntimeError(truncated_disabled_tool_error),
        ]
        adapter = OpenAIAdapter(api_key="sk-test", max_tool_rounds=1)

        result = adapter.generate(
            context="Use only runtime evidence.",
            user_input="Inspect an external page.",
            identity=_MockIdentity(),
            retries=1,
        )

        assert result == (
            "The model could not produce a valid plain-text response. "
            "No tool was executed and no external claim was verified."
        )
        assert client.chat.completions.create.call_count == 2

    def test_malformed_tool_arguments_retry_without_executing(
        self, mock_openai_client
    ):
        client = mock_openai_client.return_value
        malformed_error = (
            "Error code: 400 - {'error': {'message': "
            "'Failed to parse tool call arguments as JSON', "
            "'type': 'invalid_request_error', 'code': 'tool_use_failed', "
            "'failed_generation': '{\"name\": \"github__search_repositories\", "
            "\"arguments\": {\"query\":\"efficient LLM inference\"}'}}"
        )
        client.chat.completions.create.side_effect = [
            RuntimeError(malformed_error),
            MagicMock(
                choices=[MagicMock(message=MagicMock(
                    content="I cannot verify that without runtime evidence.",
                    tool_calls=None,
                ))]
            ),
        ]
        executed = []
        adapter = OpenAIAdapter(api_key="sk-test", max_tool_rounds=1)

        result = adapter.generate(
            context="Use tools only through the runtime.",
            user_input="Research efficient LLM inference.",
            identity=_MockIdentity(),
            retries=1,
            tools=[{
                "type": "function",
                "function": {"name": "github__search_repositories"},
            }],
            execute_tool=lambda name, args: executed.append((name, args)),
        )

        assert result == "I cannot verify that without runtime evidence."
        assert executed == []
        calls = client.chat.completions.create.call_args_list
        assert len(calls) == 2
        assert "tools" not in calls[1].kwargs
        assert "cannot verify" in calls[1].kwargs["messages"][-1]["content"]

    def test_repeated_tool_response_returns_non_execution_evidence(
        self, mock_openai_client
    ):
        client = mock_openai_client.return_value
        disabled_tool_error = (
            "Error code: 400 - {'error': {'message': "
            "'Tool choice is none, but model called a tool', "
            "'code': 'tool_use_failed', 'failed_generation': "
            "'{\"name\": \"browser.run\", \"arguments\": {}}'}}"
        )
        malformed_retry_error = (
            "Error code: 400 - {'error': {'message': "
            "'Failed to parse tool call arguments as JSON', "
            "'code': 'tool_use_failed', 'failed_generation': "
            "'{\"name\": \"github__summarize_release\", "
            "\"arguments\": {\"owner\":\"lacebx\",\"repo\":\"\"}'}}"
        )
        client.chat.completions.create.side_effect = [
            RuntimeError(disabled_tool_error),
            RuntimeError(malformed_retry_error),
        ]
        adapter = OpenAIAdapter(api_key="sk-test", max_tool_rounds=4)

        result = adapter.generate(
            context="Use only runtime evidence.",
            user_input="Find the latest release.",
            identity=_MockIdentity(),
            retries=1,
        )

        assert result == (
            "The model could not produce a valid plain-text response. "
            "No tool was executed and no external claim was verified."
        )
        assert client.chat.completions.create.call_count == 2

    def test_returns_runtime_evidence_when_final_synthesis_is_rejected(
        self, mock_openai_client
    ):
        client = mock_openai_client.return_value
        tool_call = MagicMock()
        tool_call.id = "call-1"
        tool_call.function.name = "calc__evaluate"
        tool_call.function.arguments = '{"expression": "(2+2)*5"}'
        rejected_final = (
            "Error code: 400 - {'error': {'code': 'tool_use_failed', "
            "'failed_generation': '{\"name\": \"calc.evaluate\", \"arguments\": "
            "{\"expression\": \"(2+2)*5\"}}'}}"
        )
        client.chat.completions.create.side_effect = [
            MagicMock(
                choices=[MagicMock(message=MagicMock(content="", tool_calls=[tool_call]))]
            ),
            RuntimeError(rejected_final),
        ]
        executed = []
        adapter = OpenAIAdapter(api_key="sk-test", max_tool_rounds=1)

        result = adapter.generate(
            context="Use only runtime evidence.",
            user_input="Calculate (2+2)*5",
            identity=_MockIdentity(),
            tools=[{"type": "function", "function": {"name": "calc__evaluate"}}],
            execute_tool=lambda name, args: executed.append((name, args))
            or '{"result":20}',
        )

        assert "Model synthesis was unavailable" in result
        assert 'calc__evaluate: {"result":20}' in result
        assert executed == [("calc__evaluate", {"expression": "(2+2)*5"})]

    def test_parse_legacy_function_call_json_and_dict(self):
        from adapters.openai_adapter import _parse_legacy_function_call

        json_form = "<function=foo.bar>{\"x\": 1}</function>"
        assert _parse_legacy_function_call(json_form) == ("foo.bar", {"x": 1})

        dict_form = "<function=executive__start_task>{'goal': 'hi'}</function>"
        assert _parse_legacy_function_call(dict_form) == ("executive__start_task", {"goal": "hi"})

        assert _parse_legacy_function_call("no function here") is None

    def test_parse_ordered_dotted_text_tool_calls_with_nested_arguments(self):
        from adapters.openai_adapter import (
            _parse_all_known_text_tool_calls,
            _parse_known_text_tool_call,
        )

        tools = [
            {"type": "function", "function": {"name": "browser__open"}},
            {"type": "function", "function": {"name": "browser__fill"}},
        ]
        text = (
            '<action>browser.fill({"selector":"#q","value":"a {brace}"})</action>\n'
            '<action>browser.open({"url":"https://example.com",'
            '"meta":{"source":"test"}})</action>'
        )

        assert _parse_known_text_tool_call(text, tools) == (
            "browser__fill",
            {"selector": "#q", "value": "a {brace}"},
        )
        assert _parse_all_known_text_tool_calls(text, tools) == [
            ("browser__fill", {"selector": "#q", "value": "a {brace}"}),
            (
                "browser__open",
                {"url": "https://example.com", "meta": {"source": "test"}},
            ),
        ]

    def test_text_tool_parser_ignores_unoffered_calls(self):
        from adapters.openai_adapter import _parse_all_known_text_tool_calls

        tools = [{"type": "function", "function": {"name": "browser__open"}}]
        assert _parse_all_known_text_tool_calls(
            'command_exec.run({"command":"whoami"})',
            tools,
        ) == []

    def test_parse_failed_generation_tool_call(self):
        from adapters.openai_adapter import _parse_failed_generation_tool_call

        error = (
            "Error code: 400 - {'error': {'code': 'tool_use_failed', "
            "'failed_generation': '"
            '{"name": "calc.evaluate", "arguments": {\\n'
            '  "expression": "(2+2)*5"\\n}}\'}}'
        )
        assert _parse_failed_generation_tool_call(error) == (
            "calc.evaluate",
            {"expression": "(2+2)*5"},
        )

        direct = '{"name":"web_search","arguments":{"query":"IdentityOS"}}'
        assert _parse_failed_generation_tool_call(direct) == (
            "web_search",
            {"query": "IdentityOS"},
        )

    def test_shrinks_max_tokens_on_token_limit_rejection(self, mock_openai_client):
        """A 413/token-budget rejection should shrink max_tokens and retry.

        Mirrors Groq's free-tier behavior where a request slightly over the
        tokens-per-minute allowance is rejected with 413.  The adapter halves
        the completion budget and retries instead of failing the whole turn.
        """
        client = mock_openai_client.return_value
        err_msg = (
            "Error code: 413 - {'error': {'message': 'Request too large for model "
            "`openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, "
            "Requested 8040, please reduce your message size and try again.', "
            "'type': 'tokens', 'code': 'rate_limit_exceeded'}}"
        )
        client.chat.completions.create.side_effect = [
            RuntimeError(err_msg),
            MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(content="Shrunk and succeeded!", tool_calls=None)
                    )
                ]
            ),
        ]

        adapter = OpenAIAdapter(api_key="sk-test", max_tokens=1024)
        result = adapter.generate(
            context="x" * 200,
            user_input="hi",
            identity=_MockIdentity(),
        )
        assert result == "Shrunk and succeeded!"
        calls = client.chat.completions.create.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["max_tokens"] == 1024
        assert calls[1].kwargs["max_tokens"] == 512

    def test_does_not_shrink_below_floor(self, mock_openai_client):
        """max_tokens should not shrink below 256; persists token-limit errors."""
        client = mock_openai_client.return_value
        err_msg = (
            "Error code: 413 - Request too large for model "
            "`openai/gpt-oss-120b` (tokens per minute: Limit 8000)"
        )
        client.chat.completions.create.side_effect = RuntimeError(err_msg)

        adapter = OpenAIAdapter(api_key="sk-test", max_tokens=300)
        with pytest.raises(RuntimeError, match="413"):
            adapter.generate(
                context="x" * 100,
                user_input="hi",
                identity=_MockIdentity(),
            )
        # 300 -> 256 (floor), then raises
        assert client.chat.completions.create.call_count == 2
        assert client.chat.completions.create.call_args_list[1].kwargs["max_tokens"] == 256

    def test_is_token_limit_error_markers(self):
        from adapters.openai_adapter import _is_token_limit_error
        assert _is_token_limit_error("Error code: 413 - request too large")
        assert _is_token_limit_error("tokens per minute (TPM): Limit 8000")
        assert _is_token_limit_error("maximum context length exceeded")
        assert not _is_token_limit_error("rate limit 429, try again later")


# ---------------------------------------------------------------------------
# AnthropicAdapter tests
# ---------------------------------------------------------------------------

class TestAnthropicAdapter:
    def test_generate(self, mock_anthropic_client):
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        result = adapter.generate(
            context="You are Claude.",
            user_input="Hello!",
            identity=_MockIdentity(),
        )
        assert result == "Hello from Claude mock!"
        mock_anthropic_client.return_value.messages.create.assert_called_once()

    def test_health_check_default(self):
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        assert adapter.health_check() is True

    def test_lazy_client(self):
        adapter = AnthropicAdapter(api_key="sk-ant-test")
        assert adapter._client is None
        with patch("anthropic.Anthropic") as mock:
            mock.return_value = MagicMock()
            adapter.generate(
                context="test", user_input="test", identity=_MockIdentity(),
            )
            assert adapter._client is not None


# ---------------------------------------------------------------------------
# OllamaAdapter tests
# ---------------------------------------------------------------------------

class TestOllamaAdapter:
    def test_timeout_validation_matches_openai_adapter(self):
        with pytest.raises(ValueError, match="OPENAI_TIMEOUT must be a positive number"):
            OllamaAdapter(timeout=0)

    def test_generate(self, mock_openai_client):
        adapter = OllamaAdapter(model="llama3.2")
        result = adapter.generate(
            context="You are a local model.",
            user_input="Hello!",
            identity=_MockIdentity(),
        )
        assert result == "Hello from the mock!"
        mock_openai_client.return_value.chat.completions.create.assert_called_once()

    def test_generate_disables_thinking_by_default(self, mock_openai_client):
        adapter = OllamaAdapter(model="qwen3.5:4b")
        adapter.generate(
            context="You are a local model.",
            user_input="Hello!",
            identity=_MockIdentity(),
        )
        call_kwargs = mock_openai_client.return_value.chat.completions.create.call_args[1]
        assert call_kwargs["extra_body"] == {"think": False}

    def test_generate_can_enable_thinking(self, mock_openai_client):
        adapter = OllamaAdapter(model="qwen3.5:4b", think=True)
        adapter.generate(
            context="You are a local model.",
            user_input="Hello!",
            identity=_MockIdentity(),
        )
        call_kwargs = mock_openai_client.return_value.chat.completions.create.call_args[1]
        assert call_kwargs["extra_body"] == {"think": True}

    def test_default_base_url(self):
        adapter = OllamaAdapter()
        assert adapter.base_url == "http://localhost:11434/v1"

    def test_health_check_default(self, monkeypatch):
        monkeypatch.setattr(
            "adapters.openai_adapter.list_ollama_models",
            lambda **kwargs: ["llama3.2:latest"],
        )
        adapter = OllamaAdapter(model="llama3.2")
        assert adapter.health_check() is True

    def test_health_check_missing_model(self, monkeypatch):
        monkeypatch.setattr(
            "adapters.openai_adapter.list_ollama_models",
            lambda **kwargs: ["smollm2:360m-instruct-q4_0"],
        )
        adapter = OllamaAdapter(model="llama3.2")
        assert adapter.health_check() is False

    def test_legacy_tool_loop_without_native_api(self, mock_openai_client):
        client = mock_openai_client.return_value
        client.chat.completions.create.side_effect = [
            MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(
                            content='<function=calc__evaluate>{"expression": "837 * 492"}</function>',
                            tool_calls=None,
                        ),
                    )
                ]
            ),
            MagicMock(
                choices=[
                    MagicMock(message=MagicMock(content="411804", tool_calls=None)),
                ]
            ),
        ]

        executed = []

        def execute_tool(func_name, args):
            executed.append((func_name, args))
            return '{"result": 411804}'

        adapter = OllamaAdapter(model="smollm2:360m-instruct-q4_0")
        adapter._supports_native_tools = False
        result = adapter.generate(
            context="Use tools for math.",
            user_input="Calculate 837 * 492",
            identity=_MockIdentity(),
            tools=[{"type": "function", "function": {"name": "calc__evaluate"}}],
            execute_tool=execute_tool,
        )
        assert result == "411804"
        assert executed == [("calc__evaluate", {"expression": "837 * 492"})]
        first_call = client.chat.completions.create.call_args_list[0]
        assert "tools" not in first_call[1]
        assert first_call[1]["extra_body"] == {"think": False}

    def test_legacy_tool_loop_stops_at_configured_round_limit(
        self, mock_openai_client
    ):
        client = mock_openai_client.return_value
        call_text = '<function=calc__evaluate>{"expression": "2+2"}</function>'
        client.chat.completions.create.side_effect = [
            MagicMock(
                choices=[
                    MagicMock(message=MagicMock(content=call_text, tool_calls=None))
                ]
            ),
            MagicMock(
                choices=[
                    MagicMock(message=MagicMock(content=call_text, tool_calls=None))
                ]
            ),
        ]
        executed = []
        adapter = OllamaAdapter(
            model="smollm2:360m-instruct-q4_0",
            max_tool_rounds=1,
        )
        adapter._supports_native_tools = False

        with pytest.raises(RuntimeError, match="tool-call limit reached"):
            adapter.generate(
                context="Use tools for math.",
                user_input="Calculate 2+2",
                identity=_MockIdentity(),
                tools=[{"type": "function", "function": {"name": "calc__evaluate"}}],
                execute_tool=lambda name, args: executed.append((name, args)) or "4",
            )

        assert executed == [("calc__evaluate", {"expression": "2+2"})]

    def test_legacy_tool_loop_describes_relevant_safe_tools(self, mock_openai_client):
        client = mock_openai_client.return_value
        client.chat.completions.create.side_effect = [
            MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(
                            content='<function=datetime__now>{"tz_name": null}</function>',
                            tool_calls=None,
                        ),
                    )
                ]
            ),
            MagicMock(
                choices=[
                    MagicMock(message=MagicMock(content="12:00 UTC", tool_calls=None)),
                ]
            ),
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "datetime__now",
                    "description": "Get the current date and time in a timezone",
                    "parameters": {
                        "type": "object",
                        "properties": {"tz_name": {"type": ["string", "null"]}},
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "filesystem__read_file",
                    "description": "Read a local file",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]
        executed = []

        adapter = OllamaAdapter(model="phi4-mini:latest")
        adapter._supports_native_tools = False
        output = adapter.generate(
            context="Identity context",
            user_input="What is the current time in UTC?",
            identity=_MockIdentity(),
            tools=tools,
            execute_tool=lambda name, args: executed.append((name, args)) or '{"timezone":"UTC"}',
        )

        assert output == "12:00 UTC"
        assert executed == [("datetime__now", {"tz_name": None})]
        first_context = client.chat.completions.create.call_args_list[0][1]["messages"][0]["content"]
        assert "<function=TOOL_NAME>{JSON_ARGUMENTS}</function>" in first_context
        assert "datetime__now" in first_context
        assert "call template: <function=datetime__now>{}</function>" in first_context
        assert '"null"' in first_context
        assert "filesystem__read_file" not in first_context

    def test_native_capable_ollama_model_keeps_structured_tools(
        self, mock_openai_client
    ):
        mock_openai_client.return_value.chat.completions.create.return_value.choices[
            0
        ].message.tool_calls = None
        adapter = OllamaAdapter(model="qwen3:4b", prefer_legacy_tools=False)
        adapter._supports_native_tools = True
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "datetime__now",
                    "description": "Get current time",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        output = adapter.generate(
            context="Identity context",
            user_input="What time is it?",
            identity=_MockIdentity(),
            tools=tools,
            execute_tool=lambda name, args: "unused",
        )

        assert output == "Hello from the mock!"
        request = mock_openai_client.return_value.chat.completions.create.call_args
        assert request.kwargs["tools"] == tools
        assert request.kwargs["tool_choice"] == "auto"
        assert "Executable tools" not in request.kwargs["messages"][0]["content"]

    def test_native_ollama_text_call_is_executed_only_for_offered_tool(
        self, mock_openai_client
    ):
        client = mock_openai_client.return_value
        client.chat.completions.create.side_effect = [
            MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(
                            content='datetime__now({"tz_name": "UTC"})',
                            tool_calls=None,
                        )
                    )
                ]
            ),
            MagicMock(
                choices=[
                    MagicMock(
                        message=MagicMock(
                            content="The verified time is 12:00 UTC.",
                            tool_calls=None,
                        )
                    )
                ]
            ),
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "datetime__now",
                    "description": "Get current time",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        executed = []
        adapter = OllamaAdapter(model="phi4-mini:latest", prefer_legacy_tools=False)
        adapter._supports_native_tools = True

        output = adapter.generate(
            context="Identity context",
            user_input="What time is it?",
            identity=_MockIdentity(),
            tools=tools,
            execute_tool=lambda name, args: executed.append((name, args))
            or '{"datetime":"12:00","timezone":"UTC"}',
        )

        assert output == "The verified time is 12:00 UTC."
        assert executed == [("datetime__now", {"tz_name": "UTC"})]
        assert adapter._supports_native_tools is False
        second_request = client.chat.completions.create.call_args_list[1]
        assert "tools" not in second_request.kwargs
        assert "12:00" in second_request.kwargs["messages"][1]["content"]



class TestOllamaModelHelpers:
    def test_resolve_exact_match(self):
        models = ["llama3.2:latest", "smollm2:360m-instruct-q4_0"]
        assert resolve_ollama_model("smollm2:360m-instruct-q4_0", models) == "smollm2:360m-instruct-q4_0"

    def test_resolve_base_name(self):
        models = ["llama3.2:latest", "smollm2:360m-instruct-q4_0"]
        assert resolve_ollama_model("llama3.2", models) == "llama3.2:latest"

    def test_list_ollama_models_parses_tags(self, monkeypatch):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {"models": [{"name": "smollm2:360m-instruct-q4_0"}]}
                ).encode()

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
        assert list_ollama_models() == ["smollm2:360m-instruct-q4_0"]

    def test_ollama_model_capabilities_uses_resolved_model_name(self, monkeypatch):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "models": [
                            {
                                "name": "qwen3:4b",
                                "capabilities": ["completion", "tools", "thinking"],
                            }
                        ]
                    }
                ).encode()

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
        assert ollama_model_capabilities("qwen3") == {
            "completion",
            "tools",
            "thinking",
        }


# ---------------------------------------------------------------------------
# OpenRouterAdapter tests
# ---------------------------------------------------------------------------

class TestOpenRouterAdapter:
    def test_generate(self, mock_openai_client):
        adapter = OpenRouterAdapter(api_key="sk-or-test")
        result = adapter.generate(
            context="You are a multi-model gateway.",
            user_input="Hello!",
            identity=_MockIdentity(),
        )
        assert result == "Hello from the mock!"
        mock_openai_client.return_value.chat.completions.create.assert_called_once()

    def test_default_model(self):
        adapter = OpenRouterAdapter(api_key="sk-or-test")
        assert adapter.model == "openai/gpt-4o"

    def test_default_base_url(self):
        adapter = OpenRouterAdapter(api_key="sk-or-test")
        assert adapter.base_url == "https://openrouter.ai/api/v1"

    def test_site_headers(self, mock_openai_client):
        adapter = OpenRouterAdapter(
            api_key="sk-or-test",
            site_url="https://example.com",
            site_name="IdentityOS",
        )
        adapter.generate(
            context="test", user_input="test", identity=_MockIdentity(),
        )

    def test_health_check(self, mock_openai_client):
        adapter = OpenRouterAdapter(api_key="sk-or-test")
        assert adapter.health_check() is True


# ---------------------------------------------------------------------------
# get_adapter factory tests
# ---------------------------------------------------------------------------

class TestGetAdapter:
    def test_openai(self):
        adapter = get_adapter("openai", api_key="sk-test")
        assert isinstance(adapter, OpenAIAdapter)

    def test_anthropic(self):
        adapter = get_adapter("anthropic", api_key="sk-ant-test")
        assert isinstance(adapter, AnthropicAdapter)

    def test_ollama(self):
        adapter = get_adapter("ollama", model="llama3.2")
        assert isinstance(adapter, OllamaAdapter)

    def test_openrouter(self):
        adapter = get_adapter("openrouter", api_key="sk-or-test")
        assert isinstance(adapter, OpenRouterAdapter)

    def test_unknown_adapter(self):
        with pytest.raises(ValueError, match="Unknown adapter"):
            get_adapter("nonexistent")

    def test_with_model_override(self):
        adapter = get_adapter("openai", model="gpt-3.5-turbo", api_key="sk-test")
        assert adapter.model == "gpt-3.5-turbo"

    def test_case_insensitive(self):
        adapter = get_adapter("OpenAI", api_key="sk-test")
        assert isinstance(adapter, OpenAIAdapter)


# ---------------------------------------------------------------------------
# Runtime + adapter integration test
# ---------------------------------------------------------------------------

class TestAdapterInRuntime:
    def test_runtime_with_adapter(self, mock_openai_client):
        from core.evaluation import register_default_criteria
        from core.identity import create_identity
        from runtime.orchestrator import IdentityRuntime, InteractionRequest
        from runtime.persistence import JSONFileBackend
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            storage = JSONFileBackend(root_dir=str(Path(tmp) / ".store"))
            adapter = OpenAIAdapter(api_key="sk-test")
            rt = IdentityRuntime(storage=storage, adapter=adapter)
            register_default_criteria(rt.evaluation_engine)

            spec = create_identity(name="AdapterBot", identity_id="adapter-bot")
            rt.register(spec)
            sid = rt.start_session("adapter-bot")
            resp = rt.process(InteractionRequest(
                identity_id="adapter-bot",
                user_input="Hello from the runtime!",
                session_id=sid,
            ))
            assert resp.output == "Hello from the mock!"
            assert resp.policy_passed is True
