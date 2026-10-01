"""Operator-run loopback SIP/RTP client for real PBX acceptance checks."""

import argparse
import asyncio
import audioop
import json
import re
import socket
import struct
import sys
import uuid
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.phone.audio import FasterWhisper, Piper


def speech_window(pcm):
    """Do not treat Whisper's silence hallucinations as returned speech."""
    active = [offset for offset in range(0, len(pcm), 320) if audioop.rms(pcm[offset : offset + 320], 2) > 100]
    return pcm[max(0, active[0] - 1600) : active[-1] + 1920] if active else b""


async def main():
    parser = argparse.ArgumentParser(
        description="Loopback SIP acceptance probe (Python 3.11/3.12). "
        "Requires a test PBX endpoint 1001 on 127.0.0.1:15060; "
        "never point this unauthenticated harness at a LAN/public listener."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--dtmf", action="store_true")
    parser.add_argument("--output", default="/tmp/identityos-phone-probe.wav")
    parser.add_argument("--wait", type=int, default=60)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    loop = asyncio.get_running_loop()
    sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sip.bind(("127.0.0.1", 0))
    sip.setblocking(False)
    rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp.bind(("127.0.0.1", 0))
    rtp.setblocking(False)
    port = sip.getsockname()[1]
    rp = rtp.getsockname()[1]
    cid = str(uuid.uuid4())
    tag = uuid.uuid4().hex[:10]
    dest = ("127.0.0.1", 15060)
    remote_tag = ""
    contact = "sip:700@127.0.0.1:15060"

    async def send(method, cseq, body="", branch=None):
        msg = (
            f"{method} {contact} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bK{branch or uuid.uuid4().hex}\r\n"
            f"From: <sip:1001@127.0.0.1>;tag={tag}\r\nTo: <sip:700@127.0.0.1>{remote_tag}\r\n"
            f"Call-ID: {cid}\r\nCSeq: {cseq} {method}\r\n"
            f"Contact: <sip:1001@127.0.0.1:{port}>\r\nMax-Forwards: 70\r\n"
            f"Content-Type: application/sdp\r\nContent-Length: {len(body.encode())}\r\n\r\n{body}"
        )
        await loop.sock_sendto(sip, msg.encode(), dest)

    body = (
        "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=IdentityOS local acceptance\r\n"
        f"c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio {rp} RTP/AVP 0 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-16\r\na=sendrecv\r\n"
    )
    await send("INVITE", 1, body)
    while True:
        packet, _ = await asyncio.wait_for(loop.sock_recvfrom(sip, 65535), 10)
        response = packet.decode()
        print(response.splitlines()[0], flush=True)
        if response.startswith("SIP/2.0 200"):
            remote_tag = ";tag=" + re.search(r"^To:.*;tag=([^;\s]+)", response, re.M | re.I)[1]
            match = re.search(r"^Contact:\s*<([^>]+)>", response, re.M | re.I)
            if match:
                contact = match[1]
            target = ("127.0.0.1", int(re.search(r"m=audio (\d+)", response)[1]))
            break
        if re.match("SIP/2.0 [456]", response):
            raise RuntimeError(response)
    await send("ACK", 1)
    received = bytearray()
    seq = 0
    ts = 0
    ssrc = 73123
    outgoing = asyncio.Queue()
    running = True

    async def tx():
        nonlocal seq, ts
        while running:
            pcm = outgoing.get_nowait() if not outgoing.empty() else b"\0" * 320
            payload = audioop.lin2ulaw(pcm, 2)
            pkt = struct.pack("!BBHII", 0x80, 0, seq % 65536, ts, ssrc) + payload
            await loop.sock_sendto(rtp, pkt, target)
            seq += 1
            ts += 160
            await asyncio.sleep(0.02)

    async def rx():
        while running:
            p, _ = await loop.sock_recvfrom(rtp, 4096)
            if len(p) > 12 and p[1] & 127 == 0:
                received.extend(audioop.ulaw2lin(p[12:], 2))

    async def dtmf(digit):
        nonlocal seq
        event = "0123456789*#".index(digit)
        stamp = ts
        for i in range(1, 9):
            pkt = struct.pack("!BBHII", 0x80, 101 | (0x80 if i == 1 else 0), seq % 65536, stamp, ssrc) + struct.pack(
                "!BBH", event, 10 | (0x80 if i >= 6 else 0), min(i, 6) * 160
            )
            await loop.sock_sendto(rtp, pkt, target)
            seq += 1
            await asyncio.sleep(0.02)

    tasks = [asyncio.create_task(tx()), asyncio.create_task(rx())]
    tts = Piper(config["tts"]["executable"], config["tts"]["model"])
    stt = await asyncio.to_thread(FasterWhisper, config["stt"]["model"])
    try:
        await asyncio.sleep(14)
        print("GREETING:", await stt.transcribe(bytes(received)), flush=True)
        received.clear()
        if args.dtmf:
            await dtmf("2")
            await asyncio.sleep(5)
            print("DTMF REPLY:", await stt.transcribe(bytes(received)), flush=True)
            received.clear()
        speech = await tts.synthesize("Hello. What is your name?")
        for pos in range(0, len(speech), 320):
            await outgoing.put(speech[pos : pos + 320].ljust(320, b"\0"))
        await asyncio.sleep(args.wait)
        audible = speech_window(bytes(received))
        reply = await stt.transcribe(audible) if audible else ""
        print("RUNTIME REPLY:", reply, flush=True)
        with wave.open(args.output, "wb") as f:
            f.setparams((1, 2, 8000, 0, "NONE", ""))
            f.writeframes(received)
        if not reply.strip():
            raise RuntimeError("No audible runtime response observed")
        expected = list(config["identities"])[1 if args.dtmf else 0]
        if expected.casefold() not in reply.casefold():
            raise RuntimeError("Returned speech did not identify the selected identity")
    finally:
        await send("BYE", 2)
        running = False
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        sip.close()
        rtp.close()


if __name__ == "__main__":
    asyncio.run(main())
