"""test_interop_a2a.py — generic A2A client and the controlled in-process peer."""

from __future__ import annotations

import pytest

from core.interop import a2a as a2a_mod
from core.interop.http import HttpClient
from tests.interop_stubs import TransportFailure


def test_local_agent_card_discovery():
    agent = a2a_mod.A2ATestAgent(name="echo-agent")
    client = a2a_mod.A2AClient("http://local.invalid", http=HttpClient(timeout=1.0, transport=agent.transport()))
    card = client.card()
    assert card["name"] == "echo-agent"
    assert card["version"] == "1.0.0"


def test_local_agent_capabilities_inspection():
    agent = a2a_mod.A2ATestAgent()
    client = a2a_mod.A2AClient("http://local.invalid", http=HttpClient(timeout=1.0, transport=agent.transport()))
    caps = client.capabilities()
    assert len(caps) == 1
    assert caps[0]["name"] == "echo"
    assert "text/plain" in caps[0]["input_modes"]


def test_send_message_and_receive_to_completion():
    agent = a2a_mod.A2ATestAgent(name="worker", handler=lambda msg, sid: f"done({msg})")
    client = a2a_mod.A2AClient("http://local.invalid", http=HttpClient(timeout=1.0, transport=agent.transport()))
    task = client.send_message("please work")
    assert task.status == "completed"
    assert task.result == "done(please work)"
    fetched = client.receive(task.id)
    assert fetched.id == task.id
    assert fetched.completed


def test_exchange_returns_terminal_state():
    agent = a2a_mod.A2ATestAgent(handler=lambda msg, sid: f"echo: {msg}")
    client = a2a_mod.A2AClient("http://local.invalid", http=HttpClient(timeout=1.0, transport=agent.transport()))
    task = client.exchange("hello", poll_max=5)
    assert task.completed
    assert task.result == "echo: hello"


def test_empty_message_is_rejected():
    agent = a2a_mod.A2ATestAgent()
    client = a2a_mod.A2AClient("http://local.invalid", http=HttpClient(timeout=1.0, transport=agent.transport()))
    with pytest.raises(a2a_mod.A2AError):
        client.send_message("   ")


def test_receive_unknown_task_raises_not_found():
    agent = a2a_mod.A2ATestAgent()
    client = a2a_mod.A2AClient("http://local.invalid", http=HttpClient(timeout=1.0, transport=agent.transport()))
    client.card()
    with pytest.raises(a2a_mod.A2ANotFound):
        client.receive("missing-task")


def test_missing_card_is_not_found():
    def serve(request, timeout):
        return 404, "application/json", "{}", {}

    client = a2a_mod.A2AClient("http://lonely.invalid", http=HttpClient(timeout=1.0, transport=serve))
    with pytest.raises(a2a_mod.A2ANotFound):
        client.card()


def test_transport_errors_are_wrapped():
    def serve(request, timeout):
        raise TransportFailure(503, "gateway down")

    client = a2a_mod.A2AClient("http://slow.invalid", http=HttpClient(timeout=1.0, transport=serve))
    with pytest.raises(a2a_mod.A2AError):
        client.card()