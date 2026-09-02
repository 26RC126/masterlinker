"""Audio generation for announcements, TTS and morse beacons.

Relaying Zello-to-Zello needs no codec at all — we pass the original Opus
packets straight through (see bridge.py). This module only exists for audio
*we* create: spoken announcements, chat-to-speech, and morse.

Everything degrades gracefully. No libopus, or no speech engine, and the
bridge keeps running: announcements fall back to text messages and say so in
the log rather than crashing the node.
"""

from __future__ import annotations

import array
import base64
import ctypes
import ctypes.util
import io
import math
import os
import shutil
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Opus codec header (the 4 bytes Zello wants, base64'd)
# --------------------------------------------------------------------------

def codec_header(sample_rate: int, frames_per_packet: int, frame_ms: int) -> str:
    """{sample_rate_hz(16LE), frames_per_packet(8), frame_size_ms(8)} -> base64."""
    raw = struct.pack("<HBB", sample_rate, frames_per_packet, frame_ms)
    return base64.b64encode(raw).decode()


def parse_codec_header(b64: str) -> tuple[int, int, int]:
    raw = base64.b64decode(b64)
    if len(raw) < 4:
        raise ValueError("codec header too short")
    rate, fpp, ms = struct.unpack("<HBB", raw[:4])
    return rate, fpp, ms


# --------------------------------------------------------------------------
# libopus, via ctypes so there is no build step on a Pi
# --------------------------------------------------------------------------

OPUS_APPLICATION_VOIP = 2048
OPUS_SET_BITRATE_REQUEST = 4002


class OpusUnavailable(RuntimeError):
    pass


_LIB_CANDIDATES = ["opus", "libopus.so.0", "libopus.so", "libopus.0.dylib", "opus.dll"]


def _load_libopus(explicit_path: str = "") -> ctypes.CDLL:
    tried = []
    paths = [explicit_path] if explicit_path else []
    found = ctypes.util.find_library("opus")
    if found:
        paths.append(found)
    paths += _LIB_CANDIDATES
    for path in paths:
        if not path:
            continue
        tried.append(path)
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    raise OpusUnavailable(
        "libopus not found (tried: %s). Install it — Debian/Ubuntu/Raspberry Pi OS: "
        "sudo apt install libopus0" % ", ".join(tried)
    )


class OpusEncoder:
    """Minimal mono Opus encoder producing raw packets (no Ogg container)."""

    def __init__(self, sample_rate: int = 16000, bitrate: int = 24000, lib_path: str = ""):
        self.sample_rate = sample_rate
        self._lib = _load_libopus(lib_path)
        self._lib.opus_encoder_create.restype = ctypes.c_void_p
        self._lib.opus_encoder_create.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)
        ]
        self._lib.opus_encode.restype = ctypes.c_int
        self._lib.opus_encode.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int,
        ]
        self._lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        self._lib.opus_encoder_ctl.restype = ctypes.c_int

        err = ctypes.c_int()
        self._enc = self._lib.opus_encoder_create(
            sample_rate, 1, OPUS_APPLICATION_VOIP, ctypes.byref(err)
        )
        if err.value != 0 or not self._enc:
            raise OpusUnavailable(f"opus_encoder_create failed ({err.value})")
        try:
            self._lib.opus_encoder_ctl(
                ctypes.c_void_p(self._enc), OPUS_SET_BITRATE_REQUEST, ctypes.c_int32(bitrate)
            )
        except Exception:
            pass  # bitrate is a nicety, not a requirement

    def encode(self, pcm: array.array, frame_samples: int) -> list[bytes]:
        """PCM (signed 16-bit mono) -> list of Opus packets, one per frame."""
        out: list[bytes] = []
        buf = (ctypes.c_ubyte * 4000)()
        total = len(pcm)
        pad = (-total) % frame_samples
        if pad:
            pcm = array.array("h", pcm.tolist() + [0] * pad)
        for offset in range(0, len(pcm), frame_samples):
            chunk = (ctypes.c_int16 * frame_samples)(*pcm[offset:offset + frame_samples])
            n = self._lib.opus_encode(
                ctypes.c_void_p(self._enc), chunk, frame_samples, buf, len(buf)
            )
            if n < 0:
                raise OpusUnavailable(f"opus_encode failed ({n})")
            out.append(bytes(buf[:n]))
        return out

    def close(self) -> None:
        if getattr(self, "_enc", None):
            self._lib.opus_encoder_destroy(ctypes.c_void_p(self._enc))
            self._enc = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# PCM helpers
# --------------------------------------------------------------------------

def read_wav_mono(data: bytes, target_rate: int) -> array.array:
    """Decode a 16-bit WAV to mono signed-16 PCM at target_rate."""
    with wave.open(io.BytesIO(data), "rb") as wf:
        channels, width, rate, frames = (
            wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
        )
        raw = wf.readframes(frames)
    if width != 2:
        raise ValueError(f"expected 16-bit WAV, got {width * 8}-bit")

    samples = array.array("h")
    samples.frombytes(raw)
    if sys_is_big_endian():
        samples.byteswap()

    if channels > 1:
        mono = array.array("h", [0] * (len(samples) // channels))
        for i in range(len(mono)):
            acc = sum(samples[i * channels:(i + 1) * channels])
            mono[i] = max(-32768, min(32767, acc // channels))
        samples = mono

    return resample(samples, rate, target_rate)


def sys_is_big_endian() -> bool:
    return struct.pack("=h", 1) != struct.pack("<h", 1)


def resample(pcm: array.array, src_rate: int, dst_rate: int) -> array.array:
    """Linear interpolation. Fine for speech and tones; we are not mastering an album."""
    if src_rate == dst_rate or not pcm:
        return pcm
    ratio = src_rate / dst_rate
    out_len = int(len(pcm) / ratio)
    out = array.array("h", [0] * out_len)
    for i in range(out_len):
        pos = i * ratio
        idx = int(pos)
        frac = pos - idx
        a = pcm[idx]
        b = pcm[idx + 1] if idx + 1 < len(pcm) else a
        out[i] = int(a + (b - a) * frac)
    return out


def apply_gain(pcm: array.array, gain_db: float) -> array.array:
    if abs(gain_db) < 0.01:
        return pcm
    factor = 10 ** (gain_db / 20)
    for i, v in enumerate(pcm):
        pcm[i] = max(-32768, min(32767, int(v * factor)))
    return pcm


def silence(ms: int, rate: int) -> array.array:
    return array.array("h", [0] * int(rate * ms / 1000))


def tone(ms: int, rate: int, hz: int, amplitude: float = 0.45,
         ramp_ms: int = 5) -> array.array:
    """Sine with short raised-cosine edges, so morse does not click."""
    n = int(rate * ms / 1000)
    ramp = min(int(rate * ramp_ms / 1000), n // 2)
    out = array.array("h", [0] * n)
    for i in range(n):
        env = 1.0
        if ramp:
            if i < ramp:
                env = 0.5 - 0.5 * math.cos(math.pi * i / ramp)
            elif i >= n - ramp:
                j = n - 1 - i
                env = 0.5 - 0.5 * math.cos(math.pi * j / ramp)
        out[i] = int(32767 * amplitude * env * math.sin(2 * math.pi * hz * i / rate))
    return out


# --------------------------------------------------------------------------
# Morse
# --------------------------------------------------------------------------

MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.", "-": "-....-",
    "=": "-...-", "+": ".-.-.", "@": ".--.-.", ":": "---...", "'": ".----.",
}


def morse_pcm(text: str, rate: int, wpm: int = 18, hz: int = 700) -> array.array:
    """PARIS timing: one dit = 1200 / wpm milliseconds."""
    dit = max(20, int(1200 / max(5, wpm)))
    out = array.array("h")
    out.extend(silence(150, rate))
    for word in text.upper().split():
        for ch in word:
            pattern = MORSE.get(ch)
            if not pattern:
                continue
            for symbol in pattern:
                out.extend(tone(dit * (3 if symbol == "-" else 1), rate, hz))
                out.extend(silence(dit, rate))          # intra-character
            out.extend(silence(dit * 2, rate))          # -> 3 dits between letters
        out.extend(silence(dit * 4, rate))              # -> 7 dits between words
    out.extend(silence(200, rate))
    return out


# --------------------------------------------------------------------------
# Text to speech
# --------------------------------------------------------------------------

@dataclass
class TTSResult:
    pcm: array.array
    backend: str


class TTSUnavailable(RuntimeError):
    pass


def detect_tts_backend() -> str:
    for exe, name in (("espeak-ng", "espeak-ng"), ("espeak", "espeak"),
                      ("piper", "piper"), ("say", "say")):
        if shutil.which(exe):
            return name
    return "none"


def synthesise(text: str, rate: int, backend: str = "auto", voice: str | None = None,
               wpm: int = 165, piper_model: str = "") -> TTSResult:
    """Render text to PCM at `rate`. Raises TTSUnavailable if no engine works."""
    text = (text or "").strip()
    if not text:
        raise TTSUnavailable("nothing to say")
    if backend in ("auto", ""):
        backend = detect_tts_backend()
    if backend == "none":
        raise TTSUnavailable(
            "no speech engine found. Debian/Ubuntu/Raspberry Pi OS: sudo apt install espeak-ng"
        )

    if backend in ("espeak-ng", "espeak"):
        return TTSResult(_espeak(text, rate, backend, voice, wpm), backend)
    if backend == "piper":
        return TTSResult(_piper(text, rate, piper_model), backend)
    if backend == "say":
        return TTSResult(_macos_say(text, rate, voice, wpm), backend)
    raise TTSUnavailable(f"unknown TTS backend: {backend}")


def _run_to_wav(cmd: list[str], stdin: bytes | None = None) -> bytes:
    proc = subprocess.run(cmd, input=stdin, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise TTSUnavailable(proc.stderr.decode(errors="replace")[:400] or "TTS command failed")
    return proc.stdout


def _espeak(text: str, rate: int, exe: str, voice: str | None, wpm: int) -> array.array:
    cmd = [exe, "-s", str(max(80, min(400, wpm))), "--stdout"]
    if voice:
        cmd += ["-v", voice]
    cmd += ["--", text]
    return read_wav_mono(_run_to_wav(cmd), rate)


def _piper(text: str, rate: int, model: str) -> array.array:
    if not model or not os.path.exists(model):
        raise TTSUnavailable("piper needs audio.piper_model set to a .onnx voice file")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    try:
        _run_to_wav(["piper", "--model", model, "--output_file", path], stdin=text.encode())
        with open(path, "rb") as fh:
            return read_wav_mono(fh.read(), rate)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _macos_say(text: str, rate: int, voice: str | None, wpm: int) -> array.array:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    try:
        cmd = ["say", "-r", str(wpm), "-o", path, "--data-format=LEI16@22050"]
        if voice:
            cmd += ["-v", voice]
        cmd += ["--", text]
        subprocess.run(cmd, capture_output=True, timeout=60, check=True)
        with open(path, "rb") as fh:
            return read_wav_mono(fh.read(), rate)
    finally:
        if os.path.exists(path):
            os.unlink(path)
