"""Guards that decide whether a transmission deserves to cross the bridge.

The dead-key detector is the interesting one. We never decode audio — the
bridge is a pure Opus passthrough — so measuring loudness directly is off the
table. But Opus is variable bitrate, and near-silence compresses to almost
nothing: a few bytes per frame against fifty-odd for speech. Average payload
size over the first fraction of a second separates an open mic from a person
talking, cheaply and without touching a codec.

The cost is latency: we cannot know it is a dead key until we have heard a bit
of it, so the first `deadkey_eval_ms` are buffered rather than forwarded. At
the default 800 ms that is a noticeable but tolerable delay, and it is only
paid when the detector is on.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class Verdict:
    PENDING = "pending"   # still buffering, do not open the outbound stream yet
    PASS = "pass"         # forward it
    DEAD = "dead"         # open mic / dead key, hold
    TIMEOUT = "timeout"   # ran past the timeout timer


@dataclass
class StreamJudge:
    """Per-inbound-stream state machine."""
    cfg: dict[str, Any]
    started: float = field(default_factory=time.monotonic)
    packets: int = 0
    payload_bytes: int = 0
    window_packets: int = 0
    window_bytes: int = 0
    window_started: float = field(default_factory=time.monotonic)
    opened: bool = False
    verdict: str = Verdict.PENDING
    buffered: list[bytes] = field(default_factory=list)
    reason: str = ""

    def __post_init__(self):
        if not self.cfg.get("deadkey_enabled", True):
            self.verdict = Verdict.PASS

    @property
    def duration(self) -> float:
        return time.monotonic() - self.started

    def feed(self, packet: bytes) -> str:
        """Take one Opus packet. Returns the current verdict."""
        self.packets += 1
        self.payload_bytes += len(packet)
        self.window_packets += 1
        self.window_bytes += len(packet)

        tot = float(self.cfg.get("tot_stream_s", 180) or 0)
        if tot and self.duration > tot:
            self.verdict = Verdict.TIMEOUT
            self.reason = f"over the {int(tot)}s timeout timer"
            return self.verdict

        if self.verdict == Verdict.PASS:
            return self.verdict

        eval_s = float(self.cfg.get("deadkey_eval_ms", 800)) / 1000
        if time.monotonic() - self.window_started < eval_s:
            if self.verdict == Verdict.PENDING:
                self.buffered.append(packet)
            return self.verdict

        avg = self.window_bytes / max(1, self.window_packets)
        threshold = float(self.cfg.get("deadkey_max_avg_bytes", 12))
        self.window_packets = 0
        self.window_bytes = 0
        self.window_started = time.monotonic()

        if avg <= threshold:
            if self.verdict == Verdict.PENDING:
                self.verdict = Verdict.DEAD
                self.reason = f"dead key ({avg:.1f} bytes/frame)"
                self.buffered.clear()
            return self.verdict

        # There is real audio. Open now — late if this is a second window and
        # late_open is on, which covers "keyed up, paused, then spoke".
        if self.verdict == Verdict.DEAD and not self.cfg.get("deadkey_late_open", True):
            return self.verdict
        self.verdict = Verdict.PASS
        self.reason = ""
        return self.verdict

    def take_buffer(self) -> list[bytes]:
        buf = self.buffered
        self.buffered = []
        return buf


class ActivityLimiter:
    """Kerchunk / woodpecker suppression, per (node, user)."""

    def __init__(self):
        self._history: dict[tuple[str, str], deque[float]] = {}
        self._muted: dict[tuple[str, str], float] = {}

    def is_muted(self, node_id: str, user: str) -> bool:
        until = self._muted.get((node_id, user))
        if until is None:
            return False
        if time.monotonic() >= until:
            self._muted.pop((node_id, user), None)
            return False
        return True

    def mute(self, node_id: str, user: str, seconds: float) -> None:
        self._muted[(node_id, user)] = time.monotonic() + seconds

    def muted_for(self, node_id: str, user: str) -> float:
        until = self._muted.get((node_id, user))
        return max(0.0, until - time.monotonic()) if until else 0.0

    def record(self, node_id: str, user: str, duration: float,
               cfg: dict[str, Any]) -> str | None:
        """Log a finished transmission. Returns a reason string if it triggered a mute."""
        if not cfg.get("kerchunk_enabled", True) or not user:
            return None
        if duration > float(cfg.get("kerchunk_max_s", 1.5)):
            return None
        key = (node_id, user)
        window = float(cfg.get("kerchunk_window_s", 30))
        history = self._history.setdefault(key, deque(maxlen=32))
        now = time.monotonic()
        history.append(now)
        while history and now - history[0] > window:
            history.popleft()
        if len(history) >= int(cfg.get("kerchunk_count", 4)):
            cooldown = float(cfg.get("kerchunk_cooldown_s", 120))
            self.mute(node_id, user, cooldown)
            history.clear()
            return (f"{len(history) or cfg.get('kerchunk_count')} rapid short transmissions; "
                    f"holding {user} for {int(cooldown)}s")
        return None

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {"node": node, "user": user, "seconds_left": round(self.muted_for(node, user), 1)}
            for (node, user) in list(self._muted)
            if self.is_muted(node, user)
        ]


class RateLimiter:
    """Token bucket for text and images, so a flood in one channel cannot flood five."""

    def __init__(self):
        self._events: dict[str, deque[float]] = {}

    def allow(self, key: str, per_minute: int) -> bool:
        if per_minute <= 0:
            return True
        now = time.monotonic()
        events = self._events.setdefault(key, deque(maxlen=256))
        while events and now - events[0] > 60:
            events.popleft()
        if len(events) >= per_minute:
            return False
        events.append(now)
        return True
