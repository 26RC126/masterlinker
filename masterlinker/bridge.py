"""The router — the part that actually crosslinks things.

Audio design note: both ends are Zello, both ends speak Opus, so there is no
transcode. An inbound stream's codec_header and packet_duration are re-declared
verbatim on the outbound stream and the payloads are copied across untouched.
That means no libopus on the relay path, no generation loss, and latency of
roughly one WebSocket hop. libopus is only needed for audio we synthesise.

Topology note: a link is an edge, and reach is computed by breadth-first search
across enabled edges up to `max_hops`. So A-B plus B-C sends A's audio directly
to both B and C in a single pass. Nothing is ever relayed twice, nothing loops
back, and turning off one edge cannot orphan half a net.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from typing import Any, Callable, Iterable

import aiohttp

from . import audio as A
from .config import Config, new_link
from .guards import ActivityLimiter, RateLimiter, StreamJudge, Verdict
from .speech import (EMERGENCY, NORMAL, Announcement, AnnouncementQueue, Schedule,
                     Speaker, date_phrase, time_phrase, weather_phrase)
from .zello import TokenManager, ZelloNode, StreamInfo


class Relay:
    """One outbound stream carrying one inbound stream into one node."""

    __slots__ = ("node", "stream_id", "packet_id")

    def __init__(self, node: ZelloNode, stream_id: int):
        self.node = node
        self.stream_id = stream_id
        self.packet_id = 0


class Bridge:
    def __init__(self, config: Config):
        self.config = config
        self.nodes: dict[str, ZelloNode] = {}
        self.queues: dict[str, AnnouncementQueue] = {}
        self.limiter = ActivityLimiter()
        self.rates = RateLimiter()
        self.schedule = Schedule()
        self.speaker = Speaker(config.data["audio"], self.log)

        self._session: aiohttp.ClientSession | None = None
        self._tokens: TokenManager | None = None
        self._relays: dict[tuple[str, int], list[Relay]] = {}
        self._judges: dict[tuple[str, int], StreamJudge] = {}
        self._recent_text: deque[tuple[float, str]] = deque(maxlen=200)
        self._tasks: list[asyncio.Task] = []
        self._running = False

        self.events: deque[dict[str, Any]] = deque(maxlen=500)
        self.subscribers: set[asyncio.Queue] = set()
        self.link_activity: dict[str, float] = {}

    # -- logging / events -------------------------------------------------

    def log(self, level: str, message: str, extra: dict[str, Any] | None = None) -> None:
        event = {"at": time.time(), "level": level, "message": message, **(extra or {})}
        self.events.append(event)
        for queue in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait({"type": "log", "event": event})

    def push_state(self) -> None:
        snap = self.snapshot()
        for queue in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait({"type": "state", "state": snap})

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession()
        self._reload_tokens()
        await self.rebuild_nodes()
        self._tasks = [
            asyncio.create_task(self._announcer_loop(), name="announcer"),
            asyncio.create_task(self._housekeeping_loop(), name="housekeeping"),
        ]
        if not self.speaker.available():
            self.log("warn", f"{self.speaker.encoder_error} — relaying voice between "
                             f"channels still works, but announcements will go out as text")
        if A.detect_tts_backend() == "none":
            self.log("warn", "no speech engine installed; announcements will go out as "
                             "text. On Debian, Ubuntu or Raspberry Pi OS: "
                             "sudo apt install espeak-ng")
        for note in self.config.migration_notes:
            self.log("warn", note)
        self.log("info", f"{self.config.app_name} started")

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        await asyncio.gather(*(n.stop() for n in self.nodes.values()),
                             return_exceptions=True)
        self.nodes.clear()
        if self._session:
            await self._session.close()
            self._session = None

    def _reload_tokens(self) -> None:
        zcfg = self.config.data["zello"]
        path = zcfg.get("private_key_path", "")
        try:
            self._tokens = TokenManager.from_path(
                zcfg.get("issuer", ""), path, int(zcfg.get("token_ttl_s", 3600))
            )
        except FileNotFoundError:
            self._tokens = TokenManager(zcfg.get("issuer", ""), "",
                                        int(zcfg.get("token_ttl_s", 3600)))
            self.log("error", f"private key file not found: {path}")

    async def rebuild_nodes(self) -> None:
        """Bring the running set of connections in line with the config."""
        assert self._session is not None
        wanted = {n["id"]: n for n in self.config.data["nodes"] if n.get("enabled", True)}

        for node_id in list(self.nodes):
            if node_id not in wanted:
                await self.nodes.pop(node_id).stop()
                self.queues.pop(node_id, None)
                self.log("info", f"node {node_id} removed")

        for node_id, raw in wanted.items():
            resolved = self.config.resolved_node(node_id) or raw
            existing = self.nodes.get(node_id)
            if existing:
                changed = any(existing.cfg.get(k) != resolved.get(k)
                              for k in ("channel", "username", "password",
                                        "ws_url", "listen_only"))
                existing.cfg = resolved
                self.queues[node_id].cfg = resolved.get("politeness", {})
                if not changed:
                    continue
                await existing.stop()
            node = ZelloNode(node_id, resolved, self.config.data["zello"],
                             self._tokens, self._session, self.log)
            node.on_stream_start = self._handle_stream_start
            node.on_stream_data = self._handle_stream_data
            node.on_stream_stop = self._handle_stream_stop
            node.on_text = self._handle_text
            node.on_image = self._handle_image
            node.on_transcription = self._handle_transcription
            node.on_state_change = self.push_state
            self.nodes[node_id] = node
            self.queues[node_id] = AnnouncementQueue(resolved.get("politeness", {}))
            node.start()
        self.push_state()

    # -- topology ---------------------------------------------------------

    def _edges_from(self, node_id: str) -> list[str]:
        out = []
        for link in self.config.data["links"]:
            if not link.get("enabled", True):
                continue
            if link["a"] == node_id and link.get("mode", "both") in ("both", "a_to_b"):
                out.append(link["b"])
            elif link["b"] == node_id and link.get("mode", "both") in ("both", "b_to_a"):
                out.append(link["a"])
        return out

    def targets(self, source_id: str, media: str = "audio") -> list[ZelloNode]:
        """Every node reachable from source, respecting direction, hops and media flags."""
        max_hops = int(self.config.data["bridge"].get("max_hops", 3))
        seen = {source_id}
        frontier = [(source_id, 0)]
        found: list[str] = []
        while frontier:
            current, depth = frontier.pop(0)
            if depth >= max_hops:
                continue
            for nxt in self._edges_from(current):
                if nxt in seen:
                    continue
                link = self.config.find_link(current, nxt)
                if link and not link.get("media", {}).get(media, True):
                    continue
                seen.add(nxt)
                found.append(nxt)
                frontier.append((nxt, depth + 1))

        result = []
        for node_id in found:
            node = self.nodes.get(node_id)
            if node is None or not node.connected or node.channel_status != "online":
                continue
            cfg = node.cfg
            if cfg.get("listen_only"):
                continue
            if media == "audio" and cfg.get("text_only"):
                continue
            if not cfg.get("forwarding", {}).get(media, True):
                continue
            result.append(node)
        return result

    def _mark_link_activity(self, a: str, b: str) -> None:
        link = self.config.find_link(a, b)
        if link:
            self.link_activity[link["id"]] = time.monotonic()

    # -- audio relay ------------------------------------------------------

    async def _handle_stream_start(self, node: ZelloNode, info: StreamInfo) -> None:
        key = (node.id, info.stream_id)
        guards = node.cfg.get("guards", {})

        if self.limiter.is_muted(node.id, info.sender):
            self.log("warn", f"{info.sender} is on cooldown on {node.id}; not relaying",
                     {"node": node.id})
            return

        self._judges[key] = StreamJudge(guards)
        self._relays[key] = []

        if not guards.get("deadkey_enabled", True):
            await self._open_relays(node, info)

    async def _open_relays(self, node: ZelloNode, info: StreamInfo) -> None:
        key = (node.id, info.stream_id)
        if self._relays.get(key):
            return
        targets = self.targets(node.id, "audio")
        if not targets:
            return
        header = info.codec_header or self.speaker.codec_header
        duration = info.packet_duration or 20
        relays: list[Relay] = []
        for target in targets:
            try:
                stream_id = await target.start_stream(header, duration)
                relays.append(Relay(target, stream_id))
                self._mark_link_activity(node.id, target.id)
            except Exception as exc:
                self.log("warn", f"could not open {node.id} -> {target.id}: {exc}",
                         {"node": target.id})
        self._relays[key] = relays
        if relays:
            names = ", ".join(r.node.id for r in relays)
            self.log("audio", f"{info.sender or 'someone'} on {node.id} -> {names}",
                     {"node": node.id, "sender": info.sender})
            self.push_state()

    async def _handle_stream_data(self, node: ZelloNode, stream_id: int, packet: bytes) -> None:
        key = (node.id, stream_id)
        judge = self._judges.get(key)
        if judge is None:
            return

        verdict = judge.feed(packet)

        if verdict == Verdict.TIMEOUT:
            if judge.opened:
                await self._close_relays(key)
                info = node.inbound.get(stream_id)
                self.log("warn",
                         f"{(info.sender if info else 'someone')} on {node.id} "
                         f"{judge.reason}; relay stopped", {"node": node.id})
                guards = node.cfg.get("guards", {})
                if guards.get("tot_action") == "mute" and info:
                    self.limiter.mute(node.id, info.sender,
                                      float(guards.get("tot_cooldown_s", 60)))
            judge.opened = False
            self._judges.pop(key, None)
            return

        if verdict == Verdict.DEAD:
            return

        if verdict == Verdict.PENDING:
            return

        if not judge.opened:
            info = node.inbound.get(stream_id)
            if info is None:
                return
            await self._open_relays(node, info)
            judge.opened = True
            for buffered in judge.take_buffer():
                await self._fan_out(key, buffered)

        await self._fan_out(key, packet)

    async def _fan_out(self, key: tuple[str, int], packet: bytes) -> None:
        for relay in list(self._relays.get(key, [])):
            try:
                await relay.node.send_audio(relay.stream_id, packet, relay.packet_id)
                relay.packet_id += 1
            except Exception as exc:
                self.log("warn", f"relay to {relay.node.id} failed: {exc}",
                         {"node": relay.node.id})
                with contextlib.suppress(ValueError):
                    self._relays[key].remove(relay)

    async def _handle_stream_stop(self, node: ZelloNode, info: StreamInfo) -> None:
        key = (node.id, info.stream_id)
        judge = self._judges.pop(key, None)
        await self._close_relays(key)

        if judge and judge.verdict == Verdict.DEAD and judge.duration > 1.0:
            self.log("warn",
                     f"held back {judge.duration:.0f}s from {info.sender or 'someone'} "
                     f"on {node.id} ({judge.reason})", {"node": node.id})

        reason = self.limiter.record(node.id, info.sender,
                                     info.started_at and (time.monotonic() - info.started_at) or 0,
                                     node.cfg.get("guards", {}))
        if reason:
            self.log("warn", f"{node.id}: {reason}", {"node": node.id})
        self.push_state()

    async def _close_relays(self, key: tuple[str, int]) -> None:
        grace = int(self.config.data["bridge"].get("ptt_release_grace_ms", 120)) / 1000
        relays = self._relays.pop(key, [])
        if grace and relays:
            await asyncio.sleep(grace)
        for relay in relays:
            with contextlib.suppress(Exception):
                await relay.node.stop_stream(relay.stream_id)

    # -- text / images ----------------------------------------------------

    async def _handle_text(self, node: ZelloNode, sender: str, text: str) -> None:
        fingerprint = f"{sender}|{text}"
        now = time.monotonic()
        window = float(self.config.data["bridge"].get("dedupe_window_s", 8))
        while self._recent_text and now - self._recent_text[0][0] > window:
            self._recent_text.popleft()
        if any(fp == fingerprint for _, fp in self._recent_text):
            return
        self._recent_text.append((now, fingerprint))

        self.log("text", f"{sender} on {node.id}: {text[:200]}",
                 {"node": node.id, "sender": sender})

        forwarding = node.cfg.get("forwarding", {})
        for target in self.targets(node.id, "text"):
            if not self.rates.allow(f"text:{target.id}",
                                    int(forwarding.get("text_rate_per_min", 20))):
                continue
            body = text
            if forwarding.get("attribute_sender", True):
                label = self.config.spoken_name(node.id)
                body = f"[{label}/{sender}] {text}"
            with contextlib.suppress(Exception):
                await target.send_text(body[:30000])
            self._mark_link_activity(node.id, target.id)

        await self._maybe_speak_chat(node, sender, text)

    async def _maybe_speak_chat(self, node: ZelloNode, sender: str, text: str) -> None:
        """Accessibility: someone types, the channel hears it."""
        cfg = node.cfg.get("chat_tts", {})
        if not cfg.get("enabled"):
            return
        body = text.strip()
        if cfg.get("trigger") == "prefix":
            prefix = cfg.get("trigger_prefix", "!say")
            if not body.lower().startswith(prefix.lower()):
                return
            body = body[len(prefix):].strip()
        if not body:
            return
        body = body[:int(cfg.get("max_chars", 400))]
        if cfg.get("prefix_sender", True):
            body = f"{sender} says: {body}"

        item = Announcement(kind="chat", text=body)
        # chat speech uses its own gate rather than the announcer's
        queue = self.queues.get(node.id)
        if queue is None:
            return
        original = dict(queue.cfg)
        queue.cfg = {**original, "hold_after_human_s": float(cfg.get("gate_s", 210))}
        queue.submit(item)
        queue.cfg = original

    async def _handle_image(self, node: ZelloNode, sender: str, data: bytes,
                            meta: dict[str, Any]) -> None:
        forwarding = node.cfg.get("forwarding", {})
        limit = int(forwarding.get("image_max_bytes", 2_000_000))
        if len(data) > limit:
            self.log("warn", f"image from {sender} on {node.id} is {len(data)//1024}KB; "
                             f"over the {limit//1024}KB limit", {"node": node.id})
            return
        self.log("image", f"{sender} sent an image on {node.id}",
                 {"node": node.id, "sender": sender})
        for target in self.targets(node.id, "image"):
            if not target.features.get("images", True):
                continue
            if not self.rates.allow(f"image:{target.id}",
                                    int(forwarding.get("image_rate_per_min", 6))):
                continue
            try:
                await target.send_image(data)
                self._mark_link_activity(node.id, target.id)
            except Exception as exc:
                self.log("warn", f"image {node.id} -> {target.id} failed: {exc}",
                         {"node": target.id})

    async def _handle_transcription(self, node: ZelloNode, msg: dict[str, Any]) -> None:
        cfg = node.cfg.get("transcription_relay", {})
        if not cfg.get("enabled"):
            return
        if float(msg.get("confidence", 0)) < float(cfg.get("min_confidence", 0.55)):
            return
        text = (msg.get("text") or "").strip()
        if not text:
            return
        sender = msg.get("sender", "") or "someone"
        label = self.config.spoken_name(node.id)
        body = f"[{label}/{sender}] {text}"
        recipients: Iterable[ZelloNode] = self.targets(node.id, "text")
        if cfg.get("to_own_channel"):
            recipients = list(recipients) + [node]
        for target in recipients:
            with contextlib.suppress(Exception):
                await target.send_text(body[:30000])
        self.log("text", f"transcript from {node.id}: {text[:160]}", {"node": node.id})

    # -- linking ----------------------------------------------------------

    async def link(self, a: str, b: str, mode: str = "both",
                   announce: bool = True) -> dict[str, Any]:
        if a == b:
            raise ValueError("a node cannot link to itself")
        for node_id in (a, b):
            if self.config.node(node_id) is None:
                raise ValueError(f"unknown node: {node_id}")
        link = self.config.find_link(a, b)
        if link:
            link["enabled"] = True
            link["mode"] = mode
        else:
            link = new_link(a, b, mode)
            link["created_at"] = time.time()
            self.config.data["links"].append(link)
        self.config.save()
        self.link_activity[link["id"]] = time.monotonic()
        self.log("link", f"linked {a} <-> {b} ({mode})")
        if announce:
            await self._announce_link_change(a, b, linked=True)
        self.push_state()
        return link

    async def unlink(self, a: str, b: str, announce: bool = True,
                     remove: bool = False) -> None:
        link = self.config.find_link(a, b)
        if not link:
            return
        if remove:
            self.config.data["links"] = [
                l for l in self.config.data["links"] if l["id"] != link["id"]
            ]
        else:
            link["enabled"] = False
        self.config.save()
        self.log("link", f"unlinked {a} <-> {b}")
        if announce:
            await self._announce_link_change(a, b, linked=False)
        self.push_state()

    async def _announce_link_change(self, a: str, b: str, linked: bool) -> None:
        for me, other in ((a, b), (b, a)):
            node = self.nodes.get(me)
            if node is None:
                continue
            tts = node.cfg.get("tts", {})
            if linked and not tts.get("announce_link", True):
                continue
            if not linked and not tts.get("announce_unlink", True):
                continue
            style = tts.get("style", "nickname")
            if style == "silent":
                continue
            verb = "Linked" if linked else "Unlinked"
            if style == "brief":
                text = verb
            elif style == "channel":
                other_node = self.config.node(other) or {}
                text = f"{verb} {'to' if linked else 'from'} {other_node.get('channel', other)}"
            else:
                text = f"{verb} {'to' if linked else 'from'} {self.config.spoken_name(other)}"
            item = Announcement(kind="link" if linked else "unlink", text=text,
                                also_text=bool(tts.get("also_text")))
            self.queues[me].submit(item)

    async def emergency(self, text: str, node_ids: list[str] | None = None) -> None:
        """Bypasses the shush rules by design. Use it like you would a real alert tone."""
        for node_id in (node_ids or list(self.nodes)):
            queue = self.queues.get(node_id)
            if queue is None:
                continue
            queue.submit(Announcement(kind="alert", text=text,
                                      priority=EMERGENCY, also_text=True))
        self.log("alert", f"emergency: {text}")
        self.push_state()

    async def say(self, node_id: str, text: str, priority: int = NORMAL) -> None:
        queue = self.queues.get(node_id)
        if queue is None:
            raise ValueError(f"unknown node: {node_id}")
        queue.submit(Announcement(kind="custom", text=text, priority=priority))

    def apply_preset(self, name: str) -> int:
        preset = next((p for p in self.config.data["presets"] if p["name"] == name), None)
        if preset is None:
            raise ValueError(f"unknown preset: {name}")
        for link in self.config.data["links"]:
            link["enabled"] = False
        count = 0
        for spec in preset.get("links", []):
            link = self.config.find_link(spec["a"], spec["b"])
            if link is None:
                link = new_link(spec["a"], spec["b"], spec.get("mode", "both"))
                self.config.data["links"].append(link)
            link["enabled"] = True
            link["mode"] = spec.get("mode", "both")
            count += 1
        self.config.save()
        self.log("link", f"applied preset '{name}' ({count} links)")
        self.push_state()
        return count

    # -- background loops -------------------------------------------------

    async def _announcer_loop(self) -> None:
        while self._running:
            try:
                await self._tick_schedules()
                await self._drain_queues()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log("error", f"announcer: {exc}")
            await asyncio.sleep(1.0)

    async def _tick_schedules(self) -> None:
        for node_id, node in self.nodes.items():
            cfg = node.cfg
            announcer = cfg.get("announcer", {})
            queue = self.queues.get(node_id)
            if queue is None:
                continue
            prefix = (announcer.get("prefix") or "").strip()

            if announcer.get("enabled"):
                for index, item in enumerate(announcer.get("items", [])):
                    if not item.get("enabled", True):
                        continue
                    key = f"{node_id}:ann:{index}"
                    fired = False
                    if item.get("at"):
                        fired = self.schedule.due_at_clock(key, item["at"],
                                                           announcer.get("timezone", ""))
                    elif item.get("every_minutes"):
                        fired = self.schedule.due(key, float(item["every_minutes"]))
                    if not fired:
                        continue
                    await self._submit_scheduled(node_id, queue, item, announcer, prefix)

            morse = cfg.get("morse", {})
            if morse.get("enabled") and morse.get("text"):
                if self.schedule.due(f"{node_id}:morse",
                                     float(morse.get("every_minutes", 10))):
                    item = Announcement(kind="beacon", morse=morse["text"])
                    if morse.get("polite", True):
                        queue.submit(item)
                    else:
                        item.priority = EMERGENCY
                        queue.submit(item)

    async def _submit_scheduled(self, node_id: str, queue: AnnouncementQueue,
                                item: dict[str, Any], announcer: dict[str, Any],
                                prefix: str) -> None:
        kind = item.get("kind", "custom")

        def decorate(body: str) -> str:
            return f"{prefix} {body}".strip() if prefix else body

        if kind == "time":
            ann = Announcement(kind="time",
                               builder=lambda: decorate(time_phrase(announcer)))
        elif kind == "date":
            ann = Announcement(kind="date",
                               builder=lambda: decorate(date_phrase(announcer)))
        elif kind == "weather":
            try:
                text = await weather_phrase(self._session, announcer)
            except Exception as exc:
                self.log("warn", f"weather lookup failed for {node_id}: {exc}",
                         {"node": node_id})
                return
            if not text:
                self.log("warn", f"{node_id}: set a latitude and longitude to announce weather",
                         {"node": node_id})
                return
            ann = Announcement(kind="weather", text=decorate(text))
        else:
            if not item.get("text"):
                return
            ann = Announcement(kind="custom", text=decorate(item["text"]))
        ann.also_text = bool(item.get("also_text"))
        queue.submit(ann)

    async def _drain_queues(self) -> None:
        for node_id, queue in self.queues.items():
            node = self.nodes.get(node_id)
            if node is None or not node.connected or node.channel_status != "online":
                continue
            busy = bool(node.inbound) or node.outbound_stream_id is not None
            quiet_for = (time.monotonic() - node.last_human_activity
                         if node.last_human_activity else None)
            item = queue.next_ready(quiet_for=quiet_for, busy=busy)
            if item is None:
                continue
            asyncio.create_task(self._deliver(node, item))

    async def _deliver(self, node: ZelloNode, item: Announcement) -> None:
        text = item.render()
        tts = node.cfg.get("tts", {})
        wants_text = item.also_text or tts.get("also_text") or not item.speak

        if wants_text and text and node.features.get("texting", True):
            with contextlib.suppress(Exception):
                await node.send_text(text)

        if not item.speak or node.cfg.get("text_only") or node.cfg.get("listen_only"):
            return

        pcm = await self.speaker.pcm_for(item, node.cfg)
        if pcm is None:
            if self.speaker.tts_error:
                self.log("warn", f"{node.id}: {self.speaker.tts_error} "
                                 f"(sent as text instead)", {"node": node.id})
                if text and not wants_text:
                    with contextlib.suppress(Exception):
                        await node.send_text(text)
            return

        try:
            packets = await self.speaker.encode(pcm)
        except A.OpusUnavailable as exc:
            self.log("warn", f"{node.id}: {exc} (sent as text instead)", {"node": node.id})
            if text and not wants_text:
                with contextlib.suppress(Exception):
                    await node.send_text(text)
            return

        try:
            stream_id = await node.start_stream(self.speaker.codec_header,
                                                self.speaker.frame_ms)
        except Exception as exc:
            self.log("warn", f"{node.id}: could not key up: {exc}", {"node": node.id})
            return

        frame_gap = self.speaker.frame_ms / 1000
        try:
            for index, packet in enumerate(packets):
                await node.send_audio(stream_id, packet, index)
                await asyncio.sleep(frame_gap)
        except Exception as exc:
            self.log("warn", f"{node.id}: transmission failed: {exc}", {"node": node.id})
        finally:
            with contextlib.suppress(Exception):
                await node.stop_stream(stream_id)
        self.log("say", f"{node.id}: {item.kind} — {text or item.morse}", {"node": node.id})

    async def _housekeeping_loop(self) -> None:
        while self._running:
            try:
                await self._expire_idle_links()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log("error", f"housekeeping: {exc}")
            await asyncio.sleep(30)

    async def _expire_idle_links(self) -> None:
        now = time.monotonic()
        for link in list(self.config.data["links"]):
            if not link.get("enabled", True):
                continue
            node = self.nodes.get(link["a"])
            guards = (node.cfg.get("guards", {}) if node
                      else self.config.data["node_defaults"]["guards"])
            if not guards.get("link_idle_timeout_enabled", True):
                continue
            limit = float(guards.get("link_idle_timeout_min", 180)) * 60
            if limit <= 0:
                continue
            last = self.link_activity.get(link["id"])
            if last is None:
                self.link_activity[link["id"]] = now
                continue
            if now - last > limit:
                self.log("link", f"{link['a']} <-> {link['b']} idle for "
                                 f"{limit/60:.0f} minutes; unlinking")
                await self.unlink(link["a"], link["b"])

    # -- snapshot ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "app_name": self.config.app_name,
            "nodes": [
                {**node.snapshot(),
                 "queue": self.queues[node.id].snapshot() if node.id in self.queues else {}}
                for node in self.nodes.values()
            ],
            "configured_nodes": [
                {"id": n["id"], "name": n.get("name", n["id"]),
                 "channel": n.get("channel", ""),
                 "nickname": n.get("nickname", ""),
                 "enabled": n.get("enabled", True)}
                for n in self.config.data["nodes"]
            ],
            "links": [
                {**link,
                 "idle_s": round(time.monotonic() - self.link_activity[link["id"]], 1)
                 if link["id"] in self.link_activity else None}
                for link in self.config.data["links"]
            ],
            "presets": [p["name"] for p in self.config.data["presets"]],
            "cooldowns": self.limiter.snapshot(),
            "audio": {
                "tts_backend": A.detect_tts_backend(),
                "opus": not bool(self.speaker.encoder_error),
                "opus_error": self.speaker.encoder_error,
            },
        }
