"""Phone routing, actual runtime boundaries, transport and fresh-process evidence."""

import asyncio
import json
import os
import struct
import subprocess
import sys
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.channels.context import AuthLevel, ChannelContext, authorize_skill, current_channel
from core.channels.router import IdentityRouter
from core.identity import create_identity
from runtime.orchestrator import IdentityRuntime, InteractionRequest
from runtime.persistence import JSONFileBackend
from runtime.phone.asterisk import AudioSocket, PhoneGateway
from runtime.phone.audio import EnergyVAD
from runtime.phone.switchboard import Switchboard, VoiceCall


def test_phone_model_endpoint_stays_local():
    from cli.phone import local_model_url

    assert local_model_url({}) == "http://127.0.0.1:11434/v1"
    assert local_model_url({"model_base_url": "http://127.0.0.1:11435/v1"}).endswith("11435/v1")
    for url in ("https://example.com/v1", "http://127.0.0.1@example.com/v1", "http://user:secret@localhost/v1"):
        with pytest.raises(ValueError):
            local_model_url({"model_base_url": url})


def test_phone_adapter_modes_and_invalid_configuration():
    from adapters.openai_adapter import OllamaAdapter, OpenAIAdapter
    from cli.phone import phone_adapter

    assert type(phone_adapter({"model": "local"})) is OpenAIAdapter
    legacy = phone_adapter({"model": "local", "tool_mode": "legacy"})
    assert isinstance(legacy, OllamaAdapter)
    assert legacy.prefer_legacy_tools
    with pytest.raises(ValueError, match="tool_mode"):
        phone_adapter({"model": "local", "tool_mode": "unvalidated"})


@pytest.fixture
def router(tmp_path):
    router = IdentityRouter(tmp_path / "routing.db", {"aster": "a", "gabriel": "g"})
    router.bind("1001", "arsene", "aster")
    return router


def test_resolve_default_explicit_and_unknown(router):
    a = router.resolve("1001", "voice", scope="call")
    assert (a.user_id, a.identity_id) == ("arsene", "a")
    g = router.resolve("1001", "voice", "gabriel", scope="call")
    assert g.identity_id == "g" and g.session_id != a.session_id
    assert router.resolve("1001", "voice", scope="call") == a
    with pytest.raises(PermissionError):
        router.resolve("unknown", "voice")
    with pytest.raises(ValueError):
        router.resolve("1001", "voice", "missing")
    with pytest.raises(ValueError):
        router.bind("1001", "someone-else")


def test_pin_hash_lockout_and_restart(router):
    router.set_pin("arsene", "123456")
    assert router.verify_pin("1001", "123456")
    assert b"123456" not in open(router.path, "rb").read()
    for _ in range(5):
        assert not router.verify_pin("1001", "999999")
    assert not router.verify_pin("1001", "123456")
    fresh = IdentityRouter(router.path, router.identities)
    assert not fresh.verify_pin("1001", "123456")
    assert os.stat(router.path).st_mode & 0o777 == 0o600


def test_fresh_process_routing_and_pin(router):
    router.set_pin("arsene", "654321")
    router.select_sms("1001", "gabriel")
    sid = router.resolve("1001", "sms").session_id
    code = """
import json,sys
from core.channels.router import IdentityRouter
r=IdentityRouter(sys.argv[1], {'aster':'a','gabriel':'g'})
x=r.resolve('1001','sms')
print(json.dumps([x.identity_id,x.session_id,r.verify_pin('1001','654321')]))
"""
    output = subprocess.check_output([sys.executable, "-c", code, router.path], text=True)
    assert json.loads(output) == ["g", sid, True]


class Recorder:
    def __init__(self):
        self.requests = []

    def process(self, request):
        self.requests.append(request)
        return SimpleNamespace(output=request.identity_id, policy_passed=True)

    def end_session(self, sid):
        pass


def test_sms_commands_and_one_shot(router):
    runtime = Recorder()
    board = Switchboard(router, runtime)
    assert board.sms("1001", "hello") == "a"
    assert "gabriel" in board.sms("1001", "/id gabriel")
    assert board.sms("1001", "hello") == "g"
    old = runtime.requests[-1].session_id
    assert board.sms("1001", "@aster hello") == "a"
    assert board.sms("1001", "hello") == "g"
    assert router.caller("1001")["preferred"] == "aster"
    board.sms("1001", "/new")
    board.sms("1001", "hello")
    assert runtime.requests[-1].session_id != old
    assert "gabriel" in board.sms("1001", "/status")
    for command in ("/help", "/id", "/identities"):
        assert "aster" in board.sms("1001", command)


def test_voice_switching_dtmf_and_pin(router):
    runtime = Recorder()
    call = VoiceCall(Switchboard(router, runtime), "1001", "call")
    call.text("hello")
    a = runtime.requests[-1].session_id
    assert "gabriel" in call.dtmf("2")
    call.text("hello again")
    assert runtime.requests[-1].session_id != a
    call.text("Talk to Aster.")
    call.text("back")
    assert runtime.requests[-1].session_id == a
    assert "aster" in call.dtmf("0")
    assert "gabriel" in call.text("Who can I talk to?")
    router.set_pin("arsene", "123456")
    call.dtmf("*")
    for digit in "123456":
        call.dtmf(digit)
    assert call.auth == AuthLevel.RECOGNIZED
    call.dtmf("#")
    assert call.auth == AuthLevel.PIN
    call.close()
    assert call.auth == AuthLevel.RECOGNIZED


def test_policy_fail_closed():
    context = ChannelContext("voice", "sip", "phone_call", "1001", "call")
    for level in AuthLevel:
        token = current_channel.set(replace(context, auth_level=level))
        try:
            assert authorize_skill("email.send")[0] == (level == AuthLevel.PIN)
            assert authorize_skill("web.search")[0] == (level >= AuthLevel.RECOGNIZED)
            for tool in ("financial.transfer", "command_exec.run", "executive.submit", "unknown.tool"):
                assert not authorize_skill(tool)[0]
        finally:
            current_channel.reset(token)
    assert authorize_skill("command_exec.run")[0]


def test_real_runtime_context_and_memory_isolation(tmp_path):
    class Adapter:
        model = "test"

        def generate(self, context, user_input, identity, **kwargs):
            assert "Current transient interface: phone_call" in context
            assert "secret-caller" not in context
            return "Hello from the same identity."

    runtime = IdentityRuntime(storage=JSONFileBackend(root_dir=str(tmp_path)), adapter=Adapter())
    try:
        runtime.register(create_identity("Aster", identity_id="a"))
        response = runtime.process(
            InteractionRequest(
                identity_id="a",
                user_id="u",
                session_id="s",
                user_input="Hello",
                channel_context=ChannelContext("voice", "sip", "phone_call", "secret-caller", "secret-call"),
            )
        )
        assert response.output == "Hello from the same identity."
        assert "phone_call" in response.context_used.render()
        assert current_channel.get() is None
        # Context fields must not be promoted into canonical semantic stores.
        for path in tmp_path.rglob("*.json"):
            if "fact" in path.name or "profile" in path.name:
                assert "secret-caller" not in path.read_text()
                assert "phone_call" not in path.read_text()
        runtime.capability_registry.install("a", "calc")
        token = current_channel.set(ChannelContext("voice", "sip", "phone_call", "", "", AuthLevel.ANONYMOUS))
        try:
            skill = runtime.capability_registry.all_skills("a")[0]
            result = runtime.capability_registry.call("a", skill.name)
            assert not result.success
            assert runtime.capability_registry.tool_catalog("a")[0] == []
        finally:
            current_channel.reset(token)
    finally:
        runtime.shutdown()


@pytest.mark.asyncio
async def test_audiosocket_barge_in_stops_paced_frames():
    class Writer:
        def __init__(self):
            self.frames = []

        def write(self, data):
            self.frames.append(data)

        async def drain(self):
            pass

    writer = Writer()
    transport = AudioSocket(None, writer)
    task = asyncio.create_task(transport.play(b"\x01\x00" * 8000))
    await asyncio.sleep(0.045)
    await transport.clear()
    count = len(writer.frames)
    await task
    assert len(writer.frames) == count < 10


@pytest.mark.asyncio
async def test_gateway_rejects_unregistered_media(router):
    gateway = PhoneGateway(Switchboard(router, Recorder()), None, None, EnergyVAD())
    server = await asyncio.start_server(gateway.audio, "127.0.0.1", 0)
    async with server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        writer.write(struct.pack("!BH", 1, 16) + uuid.uuid4().bytes)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 2) == b""
        assert gateway.active == 0
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_vad_barge_in_preserves_session(router):
    class Transport:
        def __init__(self):
            self.incoming = asyncio.Queue()
            self.clears = 0
            self.started = asyncio.Event()

        async def receive(self):
            return await self.incoming.get()

        async def play(self, pcm):
            self.started.set()
            await asyncio.sleep(10)

        async def clear(self):
            self.clears += 1

    class TTS:
        async def synthesize(self, text):
            return b"\0" * 320

    class STT:
        async def transcribe(self, pcm):
            return "hello"

    runtime = Recorder()
    board = Switchboard(router, runtime)
    call = VoiceCall(board, "1001", "c")
    gateway = PhoneGateway(board, STT(), TTS(), EnergyVAD())
    transport = Transport()
    task = asyncio.create_task(gateway.conversation(call, transport))
    await asyncio.wait_for(transport.started.wait(), 1)
    baseline = transport.clears
    await transport.incoming.put((0x10, struct.pack("<h", 2000) * 800))
    for _ in range(30):
        await transport.incoming.put((0x10, b"\0" * 320))
    for _ in range(100):
        if runtime.requests:
            break
        await asyncio.sleep(0.01)
    assert transport.clears > baseline
    assert runtime.requests[0].channel_context.call_id == "c"
    await transport.incoming.put((0, b""))
    await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_provider_tts_failure_terminates_conversation(router):
    class Transport:
        async def receive(self):
            await asyncio.sleep(100)

        async def play(self, pcm):
            raise AssertionError("Must not play fabricated audio")

        async def clear(self):
            pass

    class BrokenTTS:
        async def synthesize(self, text):
            raise RuntimeError("Backend unavailable")

    board = Switchboard(router, Recorder())
    gateway = PhoneGateway(board, None, BrokenTTS(), EnergyVAD())
    with pytest.raises(RuntimeError, match="Backend unavailable"):
        await asyncio.wait_for(gateway.conversation(VoiceCall(board, "1001", "failure"), Transport()), 1)


@pytest.mark.asyncio
async def test_hangup_waits_for_runtime_before_cleanup():
    import threading

    from runtime.phone.asterisk import finish_thread

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def run():
        entered.set()
        release.wait(2)
        finished.set()

    task = asyncio.create_task(finish_thread(run))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_transient_response_cannot_mutate_permanent_traits(tmp_path):
    class Adapter:
        model = "test"

        def generate(self, **kwargs):
            return "I prefer phone calls. My communication style is voice only."

    runtime = IdentityRuntime(storage=JSONFileBackend(root_dir=str(tmp_path)), adapter=Adapter())
    try:
        identity = create_identity("Aster", identity_id="a")
        runtime.register(identity)
        runtime.process(
            InteractionRequest(
                identity_id="a",
                user_id="u",
                user_input="Hello",
                channel_context=ChannelContext("voice", "sip", "phone_call", "1001", "call"),
            )
        )
        persisted_facts = json.dumps(runtime._fact_stores["a"].to_dict_full())
        assert "voice only" not in persisted_facts
        assert "prefer phone calls" not in persisted_facts
    finally:
        runtime.shutdown()


def test_expired_pin_is_checked_at_capability_execution():
    context = ChannelContext("voice", "sip", "phone_call", "1001", "call", AuthLevel.PIN, auth_valid_until=0)
    token = current_channel.set(context)
    try:
        assert not authorize_skill("email.send")[0]
        assert authorize_skill("calc.evaluate")[0]
    finally:
        current_channel.reset(token)


def test_capability_policy_is_additive_to_identity_grants(tmp_path):
    from core.capabilities.base import Capability, Skill, object_schema
    from core.capabilities.registry import CapabilityRegistry, register
    from core.capabilities.result import CapabilityResult

    calls = []

    @register
    class EmailProbe(Capability):
        id = "phone_test_email"
        name = "Local test probe, never sends email"
        permissions = ["email:send"]

        def install(self, identity_id, storage):
            return None

        def uninstall(self, identity_id, storage):
            return None

        def prompts(self, identity_id):
            return []

        def skills(self):
            return [Skill(name="email.send", description="Test", permission="email:send", input_schema=object_schema())]

        def call(self, skill_name, **params):
            calls.append(skill_name)
            return CapabilityResult.from_data(self.id, skill_name, {"probe_executed": True})

    registry = CapabilityRegistry(JSONFileBackend(root_dir=str(tmp_path)))
    registry.install("a", EmailProbe.id)
    context = ChannelContext("voice", "sip", "phone_call", "1001", "call", AuthLevel.PIN)
    token = current_channel.set(context)
    try:
        assert not registry.call("a", "email.send").success
        registry.grant("a", EmailProbe.id, "email:send")
        assert registry.call("a", "email.send").success
        current_channel.set(replace(context, auth_level=AuthLevel.RECOGNIZED))
        assert not registry.call("a", "email.send").success
        assert calls == ["email.send"]
    finally:
        current_channel.reset(token)
        from core.capabilities.registry import _BUILTIN_CAPABILITIES

        _BUILTIN_CAPABILITIES.pop(EmailProbe.id, None)


@pytest.mark.asyncio
async def test_media_keepalive_sends_silence_while_model_is_busy():
    class Writer:
        def __init__(self):
            self.frames = []

        def write(self, data):
            self.frames.append(data)

        async def drain(self):
            pass

    writer = Writer()
    transport = AudioSocket(None, writer)
    task = asyncio.create_task(transport.keepalive())
    await asyncio.sleep(0.11)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.frames
    assert all(frame == b"\x10\x01\x40" + b"\0" * 320 for frame in writer.frames)


@pytest.mark.asyncio
async def test_fastagi_ticket_is_single_use_and_binds_registered_caller(router):
    class TTS:
        async def synthesize(self, text):
            return b"\0" * 320

    board = Switchboard(router, Recorder())
    gateway = PhoneGateway(board, None, TTS(), EnergyVAD())
    media = await asyncio.start_server(gateway.audio, "127.0.0.1", 0)
    gateway.audio_port = media.sockets[0].getsockname()[1]
    agi = await asyncio.start_server(gateway.agi, "127.0.0.1", 0)
    async with media, agi:
        ar, aw = await asyncio.open_connection("127.0.0.1", agi.sockets[0].getsockname()[1])
        aw.write(b"agi_callerid: 1001\n\n")
        await aw.drain()
        assert await asyncio.wait_for(ar.readline(), 1) == b"ANSWER\n"
        aw.write(b"200 result=0\n")
        await aw.drain()
        command = (await asyncio.wait_for(ar.readline(), 1)).decode()
        call_id = uuid.UUID(command.split()[2].split(",")[0])
        r1, w1 = await asyncio.open_connection("127.0.0.1", gateway.audio_port)
        hello = b"\x01\x00\x10" + call_id.bytes
        w1.write(hello)
        await w1.drain()
        assert await asyncio.wait_for(r1.readexactly(3), 1) == b"\x10\x01\x40"
        assert gateway.active == 1
        r2, w2 = await asyncio.open_connection("127.0.0.1", gateway.audio_port)
        w2.write(hello)
        await w2.drain()
        assert await asyncio.wait_for(r2.read(), 1) == b""
        w1.write(b"\0\0\0")
        await w1.drain()
        await asyncio.wait_for(r1.read(), 1)
        aw.write(b"200 result=0\n")
        await aw.drain()
        for writer in (aw, w1, w2):
            writer.close()
            await writer.wait_closed()
        assert gateway.active == 0
