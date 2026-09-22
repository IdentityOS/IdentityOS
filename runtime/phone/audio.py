"""Local speech adapters. All transport PCM is mono signed little-endian 8 kHz."""

import array
import asyncio
import math
import sys
import tempfile
import wave
from pathlib import Path
from typing import Protocol


class SpeechToText(Protocol):
    async def transcribe(self, pcm: bytes) -> str: ...


class TextToSpeech(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


class VoiceActivityDetector(Protocol):
    def is_speech(self, pcm: bytes) -> bool: ...


class TelephonyTransport(Protocol):
    async def receive(self) -> tuple[int, bytes]: ...
    async def play(self, pcm: bytes) -> None: ...
    async def clear(self) -> None: ...


async def command(*args, input=None, timeout=60):
    proc = await asyncio.create_subprocess_exec(
        *map(str, args),
        stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        # Tool stderr may contain speech; do not copy it into application logs.
        raise RuntimeError(f"Speech executable failed (exit {proc.returncode})")
    return stdout


def resample(pcm, source, target):
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return b""
    result = array.array(
        "h",
        (samples[min(int(i * source / target), len(samples) - 1)] for i in range(int(len(samples) * target / source))),
    )
    if sys.byteorder != "little":
        result.byteswap()
    return result.tobytes()


class EnergyVAD:
    """Simple local energy VAD. Calibrate threshold for microphone and echo."""

    def __init__(self, threshold=500):
        self.threshold = threshold

    def is_speech(self, pcm):
        samples = array.array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        return bool(samples) and math.sqrt(sum(s * s for s in samples) / len(samples)) >= self.threshold


class WhisperCpp:
    def __init__(self, executable, model):
        self.executable, self.model = executable, model

    async def transcribe(self, pcm):
        with tempfile.TemporaryDirectory(prefix="idos-stt-") as tmp:
            wav = Path(tmp) / "input.wav"
            with wave.open(str(wav), "wb") as f:
                f.setparams((1, 2, 16000, 0, "NONE", ""))
                f.writeframes(resample(pcm, 8000, 16000))
            output = Path(tmp) / "transcript"
            await command(self.executable, "-m", self.model, "-f", wav, "-l", "en", "-nt", "-otxt", "-of", output)
            return output.with_suffix(".txt").read_text().strip()


class Piper:
    def __init__(self, executable, model):
        self.executable, self.model = executable, model

    async def synthesize(self, text):
        with tempfile.TemporaryDirectory(prefix="idos-tts-") as tmp:
            wav = Path(tmp) / "speech.wav"
            await command(self.executable, "--model", self.model, "--output_file", wav, input=text.encode())
            with wave.open(str(wav), "rb") as f:
                if f.getnchannels() != 1 or f.getsampwidth() != 2:
                    raise ValueError("TTS must return mono PCM16")
                return resample(f.readframes(f.getnframes()), f.getframerate(), 8000)


class FasterWhisper:
    """Optional local Whisper backend; network downloads are forbidden at serve time."""

    def __init__(self, model, vocabulary=()):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model, device="cpu", compute_type="int8", local_files_only=True)
        self.lock = asyncio.Lock()
        self.initial_prompt = "Names: " + ", ".join(vocabulary) + "." if vocabulary else None

    async def transcribe(self, pcm):
        import numpy as np

        if not pcm:
            return ""

        def run():
            audio = np.frombuffer(resample(pcm, 8000, 16000), dtype="<i2").astype(np.float32) / 32768
            segments, _ = self.model.transcribe(
                audio, language="en", beam_size=1, condition_on_previous_text=False,
                initial_prompt=self.initial_prompt,
            )
            return " ".join(s.text.strip() for s in segments)

        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(run))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
