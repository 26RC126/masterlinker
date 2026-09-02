"""Everything the bridge says out loud, and when it is allowed to say it.

The politeness rule ("shush") is the heart of this file. A bridge that talks
over people is a bridge people mute. So automated speech waits for a gap, and
if it waits so long that the message is stale, it either says the current
version or gives up rather than announcing a time that has passed.

Only one automated item is ever queued per channel. A newer one replaces the
older one instead of stacking, because two announcements back to back is worse
than one late announcement.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from . import audio as A

NORMAL = 0
EMERGENCY = 100


@dataclass
class Announcement:
    kind: str                       # time | date | weather | custom | link | unlink | chat | beacon | alert
    text: str = ""
    priority: int = NORMAL
    created: float = field(default_factory=time.monotonic)
    morse: str = ""                 # if set, sent as morse instead of speech
    builder: Optional[Callable[[], str]] = None   # re-render at send time (used by the clock)
    also_text: bool = False
    speak: bool = True

    @property
    def age(self) -> float:
        return time.monotonic() - self.created

    def render(self) -> str:
        if self.builder:
            with contextlib.suppress(Exception):
                return self.builder()
        return self.text


class AnnouncementQueue:
    """One pending normal item, plus a short emergency lane that jumps it."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.pending: Announcement | None = None
        self.emergency: list[Announcement] = []
        self.dropped = 0
        self.last_sent: float = 0.0

    def submit(self, item: Announcement) -> None:
        if item.priority >= EMERGENCY:
            self.emergency.append(item)
            return
        if self.pending is not None and self.cfg.get("coalesce", True):
            self.dropped += 1        # superseded, not lost to a bug
        self.pending = item

    def clear(self) -> None:
        self.pending = None
        self.emergency.clear()

    def next_ready(self, *, quiet_for: float | None, busy: bool) -> Announcement | None:
        """Pick something to send, or None if we should keep waiting."""
        if self.emergency:
            if busy:
                return None          # cannot interrupt a stream in progress; next gap
            return self.emergency.pop(0)

        item = self.pending
        if item is None or busy:
            return None

        if self.cfg.get("enabled", True):
            hold = float(self.cfg.get("hold_after_human_s", 210))
            if quiet_for is not None and quiet_for < hold:
                max_hold = float(self.cfg.get("max_hold_s", 1800))
                if item.age > max_hold:
                    self.pending = None
                    if item.kind in self.cfg.get("stale_drop_kinds", []):
                        self.dropped += 1
                        return None
                    if self.cfg.get("text_fallback"):
                        item.speak = False
                        item.also_text = True
                        return item
                    self.dropped += 1
                return None

        self.pending = None
        self.last_sent = time.monotonic()
        return item

    def snapshot(self) -> dict[str, Any]:
        return {
            "pending": self.pending.kind if self.pending else None,
            "pending_age_s": round(self.pending.age, 1) if self.pending else None,
            "emergency_waiting": len(self.emergency),
            "superseded": self.dropped,
        }


class Speaker:
    """Turns text or morse into Opus packets and pushes them into a node."""

    def __init__(self, audio_cfg: dict[str, Any], log: Callable[[str, str, dict], None]):
        self.cfg = audio_cfg
        self._log = log
        self.rate = int(audio_cfg.get("sample_rate", 16000))
        self.frame_ms = int(audio_cfg.get("frame_ms", 20))
        self.frame_samples = int(self.rate * self.frame_ms / 1000)
        self.encoder_error = ""
        self.tts_error = ""

    def _new_encoder(self) -> A.OpusEncoder:
        """A fresh encoder per utterance.

        Opus encoders carry prediction state, and libopus is not safe to call
        on one instance from two threads at once. Two channels announcing at
        the same moment would otherwise corrupt each other — which shows up as
        a hard abort inside the SILK resampler, not a Python traceback. They
        are cheap to create, so we simply do not share.
        """
        encoder = A.OpusEncoder(
            self.rate, int(self.cfg.get("opus_bitrate", 24000)),
            self.cfg.get("libopus_path", ""),
        )
        self.encoder_error = ""
        return encoder

    @property
    def codec_header(self) -> str:
        return A.codec_header(self.rate, 1, self.frame_ms)

    def available(self) -> bool:
        try:
            self._new_encoder().close()
        except A.OpusUnavailable as exc:
            self.encoder_error = str(exc)
            return False
        return True

    async def pcm_for(self, item: Announcement, node_cfg: dict[str, Any]) -> array.array | None:
        """Render an announcement to PCM off the event loop."""
        tts = node_cfg.get("tts", {})
        loop = asyncio.get_running_loop()
        if item.morse:
            morse_cfg = node_cfg.get("morse", {})
            return await loop.run_in_executor(
                None,
                lambda: A.apply_gain(
                    A.morse_pcm(item.morse, self.rate,
                                int(morse_cfg.get("wpm", 18)),
                                int(morse_cfg.get("tone_hz", 700))),
                    float(morse_cfg.get("gain_db", -6.0)),
                ),
            )
        text = item.render()
        if not text.strip():
            return None
        try:
            result = await loop.run_in_executor(
                None,
                lambda: A.synthesise(
                    text, self.rate,
                    backend=self.cfg.get("tts_backend", "auto"),
                    voice=tts.get("voice"),
                    wpm=int(tts.get("rate_wpm", 165)),
                    piper_model=self.cfg.get("piper_model", ""),
                ),
            )
        except A.TTSUnavailable as exc:
            self.tts_error = str(exc)
            return None
        self.tts_error = ""
        return A.apply_gain(result.pcm, float(tts.get("gain_db", 0.0)))

    async def encode(self, pcm: array.array) -> list[bytes]:
        loop = asyncio.get_running_loop()

        def work() -> list[bytes]:
            encoder = self._new_encoder()
            try:
                return encoder.encode(pcm, self.frame_samples)
            finally:
                encoder.close()

        return await loop.run_in_executor(None, work)


# --------------------------------------------------------------------------
# Scheduled announcements
# --------------------------------------------------------------------------

WEATHER_CODES = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "heavy rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


def _tz(name: str):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def time_phrase(cfg: dict[str, Any]) -> str:
    now = datetime.now(_tz(cfg.get("timezone", "")))
    if cfg.get("clock", "24h") == "12h":
        return f"The time is {now.strftime('%-I:%M %p').lower()}"
    return f"The time is {now.strftime('%H %M')}"


def date_phrase(cfg: dict[str, Any]) -> str:
    now = datetime.now(_tz(cfg.get("timezone", "")))
    return f"Today is {now.strftime('%A the %-d of %B %Y')}"


async def weather_phrase(session, cfg: dict[str, Any]) -> str:
    loc = cfg.get("location", {}) or {}
    lat, lon = loc.get("lat"), loc.get("lon")
    if lat is None or lon is None:
        return ""
    metric = cfg.get("units", "metric") == "metric"
    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,wind_speed_10m,weather_code",
        "wind_speed_unit": "mph" if not metric else "kmh",
        "temperature_unit": "celsius" if metric else "fahrenheit",
    }
    if cfg.get("timezone"):
        params["timezone"] = cfg["timezone"]
    async with session.get("https://api.open-meteo.com/v1/forecast",
                           params=params, timeout=20) as resp:
        data = await resp.json()
    cur = data.get("current", {})
    temp = cur.get("temperature_2m")
    feels = cur.get("apparent_temperature")
    wind = cur.get("wind_speed_10m")
    code = WEATHER_CODES.get(int(cur.get("weather_code", -1)), "")
    unit = "degrees" if metric else "degrees Fahrenheit"
    speed = "kilometres per hour" if metric else "miles per hour"
    label = loc.get("label") or ""
    where = f" at {label}" if label else ""
    bits = [f"Weather{where}:"]
    if code:
        bits.append(f"{code},")
    if temp is not None:
        bits.append(f"{round(temp)} {unit}")
    if feels is not None and temp is not None and abs(feels - temp) >= 2:
        bits.append(f"feeling like {round(feels)}")
    if wind is not None:
        bits.append(f", wind {round(wind)} {speed}")
    return " ".join(bits).replace(" ,", ",")


class Schedule:
    """Fires announcer items and morse beacons on their own cadence."""

    def __init__(self):
        self._next: dict[str, float] = {}

    def due(self, key: str, every_minutes: float, *, first_immediately: bool = False) -> bool:
        now = time.monotonic()
        if key not in self._next:
            self._next[key] = now if first_immediately else now + every_minutes * 60
            return first_immediately
        if now >= self._next[key]:
            self._next[key] = now + every_minutes * 60
            return True
        return False

    def due_at_clock(self, key: str, hhmm: str, tzname: str = "") -> bool:
        """Fire once when the wall clock passes HH:MM."""
        try:
            hour, minute = (int(x) for x in hhmm.split(":", 1))
        except Exception:
            return False
        now = datetime.now(_tz(tzname))
        stamp = now.strftime("%Y-%m-%d") + f" {hour:02d}:{minute:02d}"
        if self._next.get(key) == stamp:  # type: ignore[comparison-overlap]
            return False
        if (now.hour, now.minute) == (hour, minute):
            self._next[key] = stamp       # type: ignore[assignment]
            return True
        return False

    def reset(self, key: str) -> None:
        self._next.pop(key, None)
