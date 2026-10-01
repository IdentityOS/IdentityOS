"""Loopback FastAGI registration + single-use AudioSocket tickets.

Asterisk owns SIP authentication. Its caller ID is routing data, never PIN proof.
No PSTN, public listeners, or provider credentials are required.
"""

import asyncio
import contextlib
import logging
import struct
import time
import uuid
from dataclasses import dataclass

from .switchboard import VoiceCall


async def finish_thread(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


log = logging.getLogger(__name__)


class AudioSocket:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.generation = 0
        self.last_send = time.monotonic()

    async def send_frame(self, frame):
        self.writer.write(struct.pack("!BH", 0x10, len(frame)) + frame)
        self.last_send = time.monotonic()
        await asyncio.wait_for(self.writer.drain(), 5)

    async def keepalive(self):
        # Asterisk can time out after two seconds of media inactivity. Keep
        # the call alive during local inference and clients' silence suppression.
        while True:
            await asyncio.sleep(0.02)
            if time.monotonic() - self.last_send >= 0.04:
                await self.send_frame(b"\0" * 320)

    async def receive(self):
        header = await asyncio.wait_for(self.reader.readexactly(3), 60)
        kind, size = struct.unpack("!BH", header)
        if size > 8192:
            raise ValueError("Oversized AudioSocket packet")
        payload = await asyncio.wait_for(self.reader.readexactly(size), 10)
        if kind == 0x10 and (not payload or size % 2):
            raise ValueError("Invalid PCM frame")
        return kind, payload

    async def play(self, pcm):
        generation = self.generation
        for offset in range(0, len(pcm), 320):
            if generation != self.generation:
                return
            frame = pcm[offset : offset + 320]
            await self.send_frame(frame)
            # Never queue a whole utterance into the PBX: at most 20 ms ahead.
            await asyncio.sleep(len(frame) / 16000)

    async def clear(self):
        self.generation += 1


@dataclass
class Ticket:
    caller: str
    expires: float


class PhoneGateway:
    def __init__(self, board, stt, tts, vad, *, audio_port=9092, max_calls=4):
        self.board, self.stt, self.tts, self.vad = board, stt, tts, vad
        self.audio_port, self.max_calls = audio_port, max_calls
        self.tickets = {}
        self.active = 0
        # Runtime contains mutable per-identity stores; serialize its calls.
        self.runtime_lock = asyncio.Lock()
        self.connections = set()

    async def agi(self, reader, writer):
        ticket_id = None
        try:
            env = {}
            for _ in range(64):
                line = await asyncio.wait_for(reader.readline(), 5)
                if not line or line in (b"\n", b"\r\n"):
                    break
                key, _, value = line.decode().partition(":")
                env[key] = value.strip()
            else:
                raise ValueError("Oversized AGI environment")
            caller = env.get("agi_callerid", "")
            self.board.router.caller(caller)
            now = time.monotonic()
            self.tickets = {k: v for k, v in self.tickets.items() if v.expires > now}
            if self.active + len(self.tickets) >= self.max_calls:
                raise PermissionError("Gateway capacity reached")
            ticket_id = uuid.uuid4()
            self.tickets[ticket_id] = Ticket(caller, now + 15)
            for cmd in ("ANSWER", f"EXEC AudioSocket {ticket_id},127.0.0.1:{self.audio_port}"):
                writer.write((cmd + "\n").encode())
                await writer.drain()
                response = await asyncio.wait_for(reader.readline(), 3610)
                if not response.startswith(b"200 result=0"):
                    raise RuntimeError("Asterisk rejected gateway command")
        except (ValueError, PermissionError, RuntimeError, OSError, asyncio.TimeoutError) as exc:
            log.warning("FastAGI call rejected or disconnected (%s)", type(exc).__name__)
        finally:
            if ticket_id:
                self.tickets.pop(ticket_id, None)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def audio(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        transport = AudioSocket(reader, writer)
        call = None
        media_tasks = []
        try:
            kind, payload = await asyncio.wait_for(transport.receive(), 10)
            if kind != 1 or len(payload) != 16:
                raise PermissionError("Missing call ticket")
            ticket_id = uuid.UUID(bytes=payload)
            ticket = self.tickets.pop(ticket_id, None)
            if ticket is None or ticket.expires < time.monotonic() or self.active >= self.max_calls:
                raise PermissionError("Invalid call ticket")
            call = VoiceCall(self.board, ticket.caller, str(ticket_id))
            self.active += 1
            media_tasks = [
                asyncio.create_task(self.conversation(call, transport)),
                asyncio.create_task(transport.keepalive()),
            ]
            done, _ = await asyncio.wait(media_tasks, timeout=3600, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise TimeoutError("Call duration exceeded")
            for completed in done:
                completed.result()
        except Exception as exc:
            # Provider exceptions are observable, but may contain private speech
            # or credentials. Log their class rather than the raw message/body.
            log.warning("Phone media session ended or failed (%s)", type(exc).__name__)
        finally:
            for media_task in media_tasks:
                media_task.cancel()
            await asyncio.gather(*media_tasks, return_exceptions=True)
            if call:
                async with self.runtime_lock:
                    call.close()
                self.active -= 1
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.connections.discard(task)

    async def conversation(self, call, transport):
        playback = None
        revision = 0
        playback_failure = asyncio.get_running_loop().create_future()
        queue = asyncio.Queue(maxsize=4)

        async def stop_playback():
            nonlocal playback
            await transport.clear()
            if playback:
                playback.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await playback
                playback = None

        async def speak(text):
            try:
                pcm = await self.tts.synthesize(text[:2400])
                await transport.play(pcm)
            except Exception as exc:
                if not playback_failure.done():
                    playback_failure.set_exception(exc)

        async def process():
            nonlocal playback
            while True:
                kind, data, turn_revision = await queue.get()
                if kind == "audio":
                    started = time.monotonic()
                    text = await self.stt.transcribe(data)
                    log.info(
                        "phone.stt completed chars=%d duration_ms=%.0f", len(text), (time.monotonic() - started) * 1000
                    )
                    if not text:
                        continue
                    async with self.runtime_lock:
                        started = time.monotonic()
                        response = await finish_thread(call.text, text)
                        log.info("phone.runtime completed duration_ms=%.0f", (time.monotonic() - started) * 1000)
                else:
                    response = data
                # A new utterance can suppress obsolete TTS without cancelling
                # runtime execution (cancellation would not undo side effects).
                if turn_revision == revision and response:
                    await stop_playback()
                    playback = asyncio.create_task(speak(response))

        async def listen():
            nonlocal revision, playback
            buffer = bytearray()
            silence = 0
            speaking = False
            while True:
                kind, data = await transport.receive()
                if kind == 0:
                    return
                if kind == 3:
                    if len(data) != 1 or chr(data[0]) not in "0123456789*#":
                        raise ValueError("Invalid DTMF")
                    revision += 1
                    await stop_playback()
                    buffer.clear()
                    speaking = False
                    response = await finish_thread(call.dtmf, data.decode())
                    if response:
                        playback = asyncio.create_task(speak(response))
                elif kind == 0x10:
                    voice = self.vad.is_speech(data)
                    if voice and not speaking:
                        revision += 1
                        await stop_playback()
                        speaking = True
                    if speaking:
                        buffer.extend(data)
                        silence = 0 if voice else silence + len(data)
                        if silence >= 9600 or len(buffer) >= 480000:
                            if len(buffer) - silence >= 1600:
                                log.info("phone.utterance queued pcm_bytes=%d", len(buffer))
                                queue.put_nowait(("audio", bytes(buffer), revision))
                            buffer.clear()
                            silence = 0
                            speaking = False
                else:
                    raise ValueError("Unsupported AudioSocket message")

        greeting = (
            f"IdentityOS. Connected to {call.alias}. Say switch identity or press zero for the directory."
            if call.alias
            else call.board.directory()
        )
        queue.put_nowait(("greeting", greeting, revision))
        worker = asyncio.create_task(process())
        listener = asyncio.create_task(listen())
        try:
            done, _ = await asyncio.wait((worker, listener, playback_failure), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            playback_failure.cancel()
            listener.cancel()
            # Let an executing runtime turn finish before session cleanup.
            # finish_thread shields actual thread work from cancellation.
            worker.cancel()
            await asyncio.gather(worker, listener, return_exceptions=True)
            await stop_playback()

    async def serve(self, agi_port=4573):
        agi = await asyncio.start_server(self.agi, "127.0.0.1", agi_port, limit=4096)
        try:
            audio = await asyncio.start_server(self.audio, "127.0.0.1", self.audio_port, limit=16384)
            async with agi, audio:
                await asyncio.gather(agi.serve_forever(), audio.serve_forever())
        finally:
            agi.close()
            for task in list(self.connections):
                task.cancel()
            await asyncio.gather(*self.connections, return_exceptions=True)
