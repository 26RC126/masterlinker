"""Zello Channel API client.

Protocol reference: github.com/zelloptt/zello-channel-api (API.md, AUTH.md).

Two facts that shaped this file:

1. Zello Friends and Family (consumer Zello) only allows ONE channel per
   connection — the multi-channel `channels` array is Zello Work only. So each
   Masterlinker node opens its own WebSocket. That happens to be exactly what a
   crosslink wants anyway: independent reconnect, independent state.

2. Password-protected consumer channels are refused by the Channel API. The
   server does say why, in `on_channel_status`: error "invalid password",
   error_type "configuration". Most clients drop those fields and leave the
   operator staring at "offline". We surface them verbatim.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import random
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PACKET_AUDIO = 0x01
PACKET_IMAGE = 0x02
IMAGE_FULL = 0x01
IMAGE_THUMBNAIL = 0x02


# --------------------------------------------------------------------------
# Auth tokens
# --------------------------------------------------------------------------

class TokenError(RuntimeError):
    pass


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class TokenManager:
    """Mints RS256 JWTs from the developer-console issuer + private key.

    Claims are exactly the two Zello uses: iss and exp. The key never leaves
    this process; the token is minted fresh and cached until shortly before it
    expires, so a long-running bridge reconnects cleanly at 3am without you.
    """

    def __init__(self, issuer: str, private_key_pem: str, ttl_s: int = 3600):
        self.issuer = (issuer or "").strip()
        self.ttl_s = max(300, ttl_s)
        self._pem = private_key_pem
        self._key: rsa.RSAPrivateKey | None = None
        self._cached: tuple[str, float] | None = None

    @classmethod
    def from_path(cls, issuer: str, path: str, ttl_s: int = 3600) -> "TokenManager":
        with open(path, "r", encoding="utf-8") as fh:
            return cls(issuer, fh.read(), ttl_s)

    def _load(self) -> rsa.RSAPrivateKey:
        if self._key is not None:
            return self._key
        if not self.issuer:
            raise TokenError("no issuer set — add one from developers.zello.com > Keys")
        pem = (self._pem or "").strip()
        if not pem:
            raise TokenError("private key is empty")
        if "BEGIN" not in pem:
            raise TokenError(
                "private key does not look like PEM. Copy the whole block from the "
                "Zello developer console, including the BEGIN and END lines."
            )
        try:
            key = serialization.load_pem_private_key(pem.encode(), password=None)
        except Exception as exc:
            raise TokenError(f"could not read private key: {exc}") from exc
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TokenError("Zello needs an RSA key (RS256)")
        self._key = key
        return key

    def token(self) -> str:
        now = time.time()
        if self._cached and self._cached[1] - 120 > now:
            return self._cached[0]
        key = self._load()
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"},
                                    separators=(",", ":")).encode())
        exp = int(now) + self.ttl_s
        payload = _b64url(json.dumps({"iss": self.issuer, "exp": exp},
                                     separators=(",", ":")).encode())
        signing_input = f"{header}.{payload}".encode()
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        token = f"{header}.{payload}.{_b64url(signature)}"
        self._cached = (token, exp)
        return token


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

@dataclass
class StreamInfo:
    stream_id: int
    sender: str
    codec_header: str
    packet_duration: int
    started_at: float = field(default_factory=time.monotonic)
    packets: int = 0
    payload_bytes: int = 0


class ZelloError(RuntimeError):
    pass


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Width/height straight from the JPEG SOF marker, so Pillow stays optional."""
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg
    return 0, 0


def make_thumbnail(data: bytes, max_side: int = 320) -> bytes:
    try:
        from PIL import Image  # optional
        import io as _io
        img = Image.open(_io.BytesIO(data))
        img.thumbnail((max_side, max_side))
        buf = _io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=72)
        return buf.getvalue()
    except Exception:
        return data  # Zello accepts it; just less efficient


# --------------------------------------------------------------------------
# Node
# --------------------------------------------------------------------------

class ZelloNode:
    """One channel connection. Owns its socket, its reconnect loop, its state."""

    def __init__(self, node_id: str, cfg: dict[str, Any], zello_cfg: dict[str, Any],
                 tokens: TokenManager, session: aiohttp.ClientSession,
                 log: Callable[[str, str, dict[str, Any]], None]):
        self.id = node_id
        self.cfg = cfg
        self.zcfg = zello_cfg
        self.tokens = tokens
        self._session = session
        self._log = log

        self.channel = cfg.get("channel", "")
        self.username = cfg.get("username", "")
        self.ws_url = cfg.get("ws_url") or zello_cfg.get("ws_url") or "wss://zello.io/ws"

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()

        # live state, read by the web UI
        self.connected = False
        self.channel_status = "offline"
        self.users_online = 0
        self.features = {"images": False, "texting": False, "locations": False}
        self.last_error = ""
        self.last_error_type = ""
        self.inbound: dict[int, StreamInfo] = {}
        self.outbound_stream_id: int | None = None
        self.last_activity: float = 0.0
        self.last_human_activity: float = 0.0
        self.last_heard: list[dict[str, Any]] = []

        # image reassembly: message_id -> partial
        self._image_meta: dict[int, dict[str, Any]] = {}

        # callbacks wired up by the router
        self.on_stream_start: Callable[[ZelloNode, StreamInfo], Awaitable[None]] | None = None
        self.on_stream_data: Callable[[ZelloNode, int, bytes], Awaitable[None]] | None = None
        self.on_stream_stop: Callable[[ZelloNode, StreamInfo], Awaitable[None]] | None = None
        self.on_text: Callable[[ZelloNode, str, str], Awaitable[None]] | None = None
        self.on_image: Callable[[ZelloNode, str, bytes, dict], Awaitable[None]] | None = None
        self.on_transcription: Callable[[ZelloNode, dict], Awaitable[None]] | None = None
        self.on_state_change: Callable[[], None] | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"node:{self.id}")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._task:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=5)
        self.connected = False
        self.channel_status = "offline"
        self._notify()

    def _notify(self) -> None:
        if self.on_state_change:
            try:
                self.on_state_change()
            except Exception:
                pass

    async def _run(self) -> None:
        backoff = float(self.zcfg.get("reconnect_min_s", 2))
        while not self._stop.is_set():
            try:
                await self._connect_once()
                backoff = float(self.zcfg.get("reconnect_min_s", 2))
            except asyncio.CancelledError:
                raise
            except TokenError as exc:
                self.last_error = str(exc)
                self.last_error_type = "auth"
                self._log("error", f"[{self.id}] {exc}", {"node": self.id})
                self._notify()
                await asyncio.sleep(30)
                continue
            except Exception as exc:
                self.last_error = str(exc)
                self._log("warn", f"[{self.id}] connection lost: {exc}", {"node": self.id})
            finally:
                self.connected = False
                self.channel_status = "offline"
                self.inbound.clear()
                self.outbound_stream_id = None
                self._notify()
            if self._stop.is_set():
                break
            wait = min(backoff, float(self.zcfg.get("reconnect_max_s", 60)))
            wait *= 0.75 + random.random() * 0.5   # jitter, so 10 nodes do not stampede
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, float(self.zcfg.get("reconnect_max_s", 60)))

    async def _connect_once(self) -> None:
        auth_token = self.tokens.token()
        self._log("info", f"[{self.id}] connecting to {self.channel}", {"node": self.id})
        async with self._session.ws_connect(
            self.ws_url, heartbeat=None, autoping=True, max_msg_size=16 * 1024 * 1024
        ) as ws:
            self._ws = ws
            logon: dict[str, Any] = {
                "command": "logon",
                "auth_token": auth_token,
                "channels": [self.channel],
                "channel": self.channel,   # older servers/clients use the singular form
                "version": self.zcfg.get("client_version", "masterlinker"),
                "platform_name": self.zcfg.get("platform_name", "Masterlinker Gateway"),
                "platform_type": "masterlinker",
            }
            if self.username:
                logon["username"] = self.username
                logon["password"] = self.cfg.get("password", "")
            if self.cfg.get("listen_only"):
                logon["listen_only"] = True
            if self.zcfg.get("request_transcriptions"):
                logon["features"] = {"transcriptions": True}

            # The reader has to be running before we ask anything, because the
            # logon reply comes back down the same socket we would be blocking on.
            reader = asyncio.create_task(self._read_loop(ws), name=f"read:{self.id}")
            try:
                reply = await self._command(logon, timeout=20)
                if not reply.get("success"):
                    raise ZelloError(reply.get("error", "logon refused"))

                self.connected = True
                self.last_error = ""
                self.last_error_type = ""
                self._log("info",
                          f"[{self.id}] logged in as {self.username or 'anonymous'}",
                          {"node": self.id})
                self._notify()
                await reader
            finally:
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reader
        self._ws = None

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._on_text_frame(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await self._on_binary_frame(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    # -- command plumbing -------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _command(self, payload: dict[str, Any], timeout: float = 10) -> dict[str, Any]:
        if self._ws is None or self._ws.closed:
            raise ZelloError("not connected")
        seq = self._next_seq()
        payload["seq"] = seq
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[seq] = future
        async with self._send_lock:
            await self._ws.send_str(json.dumps(payload))
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(seq, None)

    async def _send_binary(self, data: bytes) -> None:
        if self._ws is None or self._ws.closed:
            raise ZelloError("not connected")
        async with self._send_lock:
            await self._ws.send_bytes(data)

    # -- inbound ----------------------------------------------------------

    async def _on_text_frame(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return

        seq = msg.get("seq")
        if seq is not None and seq in self._pending and "command" not in msg:
            future = self._pending.get(seq)
            if future and not future.done():
                future.set_result(msg)
            return

        command = msg.get("command")
        if command == "on_channel_status":
            self.channel_status = msg.get("status", "offline")
            self.users_online = msg.get("users_online", 0)
            self.features = {
                "images": bool(msg.get("images_supported")),
                "texting": bool(msg.get("texting_supported")),
                "locations": bool(msg.get("locations_supported")),
            }
            self.last_error = msg.get("error", "") or ""
            self.last_error_type = msg.get("error_type", "") or ""
            if self.last_error:
                hint = ""
                if "password" in self.last_error.lower():
                    hint = (" — the Channel API cannot join password-protected consumer "
                            "channels. Remove the channel password, or use an invite-only "
                            "channel with this account added as a member.")
                self._log("error",
                          f"[{self.id}] channel {self.channel}: {self.last_error}"
                          f" ({self.last_error_type}){hint}", {"node": self.id})
            self._notify()

        elif command == "on_stream_start":
            info = StreamInfo(
                stream_id=int(msg["stream_id"]),
                sender=msg.get("from", "") or "",
                codec_header=msg.get("codec_header", ""),
                packet_duration=int(msg.get("packet_duration", 20) or 20),
            )
            self.inbound[info.stream_id] = info
            self.last_activity = time.monotonic()
            if info.sender and info.sender != self.username:
                self.last_human_activity = time.monotonic()
            self._remember(info.sender)
            self._notify()
            if self.on_stream_start:
                await self.on_stream_start(self, info)

        elif command == "on_stream_stop":
            info = self.inbound.pop(int(msg.get("stream_id", 0)), None)
            self.last_activity = time.monotonic()
            self._notify()
            if info and self.on_stream_stop:
                await self.on_stream_stop(self, info)

        elif command == "on_text_message":
            sender = msg.get("from", "") or ""
            text = msg.get("text", "") or ""
            self.last_activity = time.monotonic()
            if sender and sender != self.username:
                self.last_human_activity = time.monotonic()
                if self.on_text:
                    await self.on_text(self, sender, text)

        elif command == "on_image":
            mid = int(msg.get("message_id", 0))
            self._image_meta[mid] = {
                "from": msg.get("from", ""),
                "width": msg.get("width", 0),
                "height": msg.get("height", 0),
                "type": msg.get("type") or msg.get("ct") or "jpeg",
                "source": msg.get("source", "library"),
                "at": time.monotonic(),
            }
            # keep the table from growing if a full-image packet never arrives
            cutoff = time.monotonic() - 120
            for key in [k for k, v in self._image_meta.items() if v["at"] < cutoff]:
                self._image_meta.pop(key, None)

        elif command == "on_transcription":
            if self.on_transcription:
                await self.on_transcription(self, msg)

        elif command == "on_error":
            self.last_error = msg.get("error", "")
            self._log("error", f"[{self.id}] server error: {self.last_error}",
                      {"node": self.id})
            self._notify()

    async def _on_binary_frame(self, data: bytes) -> None:
        if len(data) < 9:
            return
        kind = data[0]
        if kind == PACKET_AUDIO:
            stream_id = struct.unpack(">I", data[1:5])[0]
            payload = data[9:]
            info = self.inbound.get(stream_id)
            if info is not None:
                info.packets += 1
                info.payload_bytes += len(payload)
            self.last_activity = time.monotonic()
            if self.on_stream_data:
                await self.on_stream_data(self, stream_id, payload)
        elif kind == PACKET_IMAGE:
            message_id, image_type = struct.unpack(">II", data[1:9])
            if image_type != IMAGE_FULL:
                return   # thumbnail; we forward the full one
            meta = self._image_meta.pop(message_id, None)
            if not meta or not self.on_image:
                return
            sender = meta.get("from", "")
            if sender and sender != self.username:
                self.last_human_activity = time.monotonic()
                await self.on_image(self, sender, data[9:], meta)

    def _remember(self, sender: str) -> None:
        if not sender:
            return
        self.last_heard = ([{"user": sender, "at": time.time()}] +
                           [h for h in self.last_heard if h["user"] != sender])[:8]

    # -- outbound ---------------------------------------------------------

    async def start_stream(self, codec_header: str, packet_duration: int) -> int:
        reply = await self._command({
            "command": "start_stream",
            "channel": self.channel,
            "type": "audio",
            "codec": "opus",
            "codec_header": codec_header,
            "packet_duration": packet_duration,
        }, timeout=15)
        if not reply.get("success"):
            raise ZelloError(reply.get("error", "start_stream refused"))
        self.outbound_stream_id = int(reply["stream_id"])
        self._notify()
        return self.outbound_stream_id

    async def send_audio(self, stream_id: int, packet: bytes, packet_id: int = 0) -> None:
        await self._send_binary(struct.pack(">BII", PACKET_AUDIO, stream_id, packet_id) + packet)

    async def stop_stream(self, stream_id: int) -> None:
        with contextlib.suppress(Exception):
            await self._command({
                "command": "stop_stream",
                "channel": self.channel,
                "stream_id": stream_id,
            }, timeout=10)
        if self.outbound_stream_id == stream_id:
            self.outbound_stream_id = None
        self._notify()

    async def send_text(self, text: str) -> None:
        reply = await self._command({
            "command": "send_text_message",
            "channel": self.channel,
            "text": text[:30000],
        }, timeout=15)
        if not reply.get("success"):
            raise ZelloError(reply.get("error", "send_text_message refused"))

    async def send_image(self, jpeg: bytes) -> None:
        thumb = make_thumbnail(jpeg)
        width, height = jpeg_dimensions(jpeg)
        reply = await self._command({
            "command": "send_image",
            "channel": self.channel,
            "type": "jpeg",
            "source": "library",
            "width": width or 640,
            "height": height or 480,
            "thumbnail_content_length": len(thumb),
            "content_length": len(jpeg),
        }, timeout=20)
        if not reply.get("success"):
            raise ZelloError(reply.get("error", "send_image refused"))
        image_id = int(reply["image_id"])
        await self._send_binary(
            struct.pack(">BII", PACKET_IMAGE, image_id, IMAGE_THUMBNAIL) + thumb)
        await self._send_binary(
            struct.pack(">BII", PACKET_IMAGE, image_id, IMAGE_FULL) + jpeg)

    # -- introspection ----------------------------------------------------

    @property
    def talking(self) -> list[str]:
        return [s.sender or "someone" for s in self.inbound.values()]

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "id": self.id,
            "name": self.cfg.get("name", self.id),
            "channel": self.channel,
            "connected": self.connected,
            "status": self.channel_status,
            "users_online": self.users_online,
            "features": self.features,
            "error": self.last_error,
            "error_type": self.last_error_type,
            "talking": self.talking,
            "transmitting": self.outbound_stream_id is not None,
            "quiet_for_s": round(now - self.last_human_activity, 1) if self.last_human_activity else None,
            "last_heard": self.last_heard,
            "listen_only": bool(self.cfg.get("listen_only")),
            "text_only": bool(self.cfg.get("text_only")),
        }
