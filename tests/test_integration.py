"""End-to-end test against a stand-in Zello server.

Runs the real ZelloNode, the real Bridge and the real web API. The only fake
part is the server on the other end of the WebSocket, which implements enough
of the Channel API to be honest: logon, channel status, streams, binary audio
packets, and text messages.
"""

import asyncio
import json
import os
import struct
import sys
import tempfile

from aiohttp import web, ClientSession
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from masterlinker.bridge import Bridge
from masterlinker.config import Config, new_link, new_node
from masterlinker.web import build_app


class FakeZello:
    """Minimal Channel API server. Broadcasts to every other socket on a channel."""

    def __init__(self):
        self.app = web.Application()
        self.app.router.add_get("/ws", self.handle)
        self.clients = []          # (ws, channel, username)
        self.next_stream = 5000
        self.next_image = 9000
        self.received_text = []
        self.received_audio = {}   # stream_id -> list[bytes]

    async def handle(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        entry = [ws, None, None]
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                await self._command(entry, json.loads(msg.data))
            elif msg.type == web.WSMsgType.BINARY:
                await self._binary(entry, msg.data)
        if entry in self.clients:
            self.clients.remove(entry)
        return ws

    async def _command(self, entry, msg):
        ws = entry[0]
        cmd = msg.get("command")
        seq = msg.get("seq")

        if cmd == "logon":
            entry[1] = (msg.get("channels") or [msg.get("channel")])[0]
            entry[2] = msg.get("username")
            self.clients.append(entry)
            await ws.send_json({"seq": seq, "success": True, "refresh_token": "r"})
            await ws.send_json({
                "command": "on_channel_status", "channel": entry[1],
                "status": "online", "users_online": 2,
                "images_supported": True, "texting_supported": True,
                "locations_supported": True,
            })

        elif cmd == "start_stream":
            self.next_stream += 1
            sid = self.next_stream
            entry.append(sid)
            self.received_audio[sid] = []
            await ws.send_json({"seq": seq, "success": True, "stream_id": sid})
            await self._broadcast(entry, {
                "command": "on_stream_start", "type": "audio", "codec": "opus",
                "codec_header": msg["codec_header"],
                "packet_duration": msg["packet_duration"],
                "stream_id": sid, "channel": entry[1],
                "from": entry[2] or "anon", "for": False,
            })

        elif cmd == "stop_stream":
            await ws.send_json({"seq": seq, "success": True})
            await self._broadcast(entry, {"command": "on_stream_stop",
                                          "stream_id": msg["stream_id"]})

        elif cmd == "send_text_message":
            self.received_text.append((entry[1], entry[2], msg["text"]))
            await ws.send_json({"seq": seq, "success": True})
            await self._broadcast(entry, {
                "command": "on_text_message", "channel": entry[1],
                "from": entry[2] or "anon", "for": False,
                "message_id": len(self.received_text), "text": msg["text"],
            })

        elif cmd == "send_image":
            self.next_image += 1
            await ws.send_json({"seq": seq, "success": True,
                                "image_id": self.next_image})
        else:
            await ws.send_json({"seq": seq, "error": "unknown command"})

    async def _binary(self, entry, data):
        kind = data[0]
        if kind != 0x01:
            return
        sid = struct.unpack(">I", data[1:5])[0]
        self.received_audio.setdefault(sid, []).append(data[9:])
        for other in self.clients:
            if other is entry or other[1] != entry[1]:
                continue
            await other[0].send_bytes(data)

    async def _broadcast(self, entry, payload):
        for other in self.clients:
            if other is entry or other[1] != entry[1]:
                continue
            await other[0].send_json(payload)

    async def inject_stream(self, channel, username, packets, codec_header="gD4BFA=="):
        """Pretend a human keyed up in `channel`."""
        self.next_stream += 1
        sid = self.next_stream
        for entry in self.clients:
            if entry[1] != channel:
                continue
            await entry[0].send_json({
                "command": "on_stream_start", "type": "audio", "codec": "opus",
                "codec_header": codec_header, "packet_duration": 20,
                "stream_id": sid, "channel": channel, "from": username, "for": False,
            })
        for index, packet in enumerate(packets):
            frame = struct.pack(">BII", 0x01, sid, index) + packet
            for entry in self.clients:
                if entry[1] == channel:
                    await entry[0].send_bytes(frame)
            await asyncio.sleep(0.02)
        for entry in self.clients:
            if entry[1] == channel:
                await entry[0].send_json({"command": "on_stream_stop", "stream_id": sid})
        return sid

    async def inject_text(self, channel, username, text):
        for entry in self.clients:
            if entry[1] == channel:
                await entry[0].send_json({
                    "command": "on_text_message", "channel": channel,
                    "from": username, "for": False, "message_id": 1, "text": text,
                })


def make_config(path, ws_url, key_path):
    config = Config(path)
    config.data["zello"].update({"ws_url": ws_url, "issuer": "TEST=",
                                 "private_key_path": key_path,
                                 "reconnect_min_s": 0.2})
    config.data["web"].update({"require_auth": False, "port": 0})
    for node_id, channel in (("alpha", "Chan A"), ("bravo", "Chan B"),
                             ("charlie", "Chan C")):
        node = new_node(node_id, node_id.title(), channel)
        node["username"] = f"bot-{node_id}"
        node["password"] = "x"
        node["nickname"] = node_id.title()
        # keep the tests quick and deterministic
        config.upsert_node(node)
    config.data["node_defaults"]["guards"]["deadkey_eval_ms"] = 100
    config.data["node_defaults"]["tts"]["style"] = "nickname"
    config.data["setup_complete"] = True
    config.save()
    return config


async def main():
    tmp = tempfile.mkdtemp()
    key_path = os.path.join(tmp, "zello.key")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))

    fake = FakeZello()
    runner = web.AppRunner(fake.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    ws_url = f"http://127.0.0.1:{port}/ws"

    config = make_config(os.path.join(tmp, "cfg.json"), ws_url, key_path)
    bridge = Bridge(config)
    await bridge.start()
    await asyncio.sleep(0.6)

    results = []

    def check(name, condition, detail=""):
        results.append((name, condition, detail))
        print(f"{'PASS' if condition else 'FAIL'}  {name}{'  ' + detail if detail else ''}")

    # -- legacy config migration ---------------------------------------
    import json as _json
    legacy_path = os.path.join(tmp, "legacy.json")
    with open(legacy_path, "w") as fh:
        _json.dump({"nodes": [{"id": "old", "name": "Old", "channel": "X",
                               "channel_password": "hunter2"}]}, fh)
    legacy = Config(legacy_path)
    on_disk = open(legacy_path).read()
    check("a stored channel password is stripped from an older config",
          "channel_password" not in legacy.data["nodes"][0]
          and "hunter2" not in on_disk
          and len(legacy.migration_notes) == 1)

    snap = bridge.snapshot()
    online = [n for n in snap["nodes"] if n["connected"] and n["status"] == "online"]
    check("three nodes connect and report the channel online", len(online) == 3,
          f"{len(online)}/3")

    # -- link alpha <-> bravo ------------------------------------------
    # announce=False keeps synthesised speech out of the audio assertions below;
    # the announcement path is exercised separately.
    await bridge.link("alpha", "bravo", announce=False)
    await asyncio.sleep(0.2)
    check("link is recorded and enabled",
          bool(config.find_link("alpha", "bravo") or {}).__bool__()
          and config.find_link("alpha", "bravo")["enabled"])

    # -- audio relay ----------------------------------------------------
    speech = [bytes([80 + i % 40]) * 50 for i in range(25)]
    marker = set(speech)
    before = dict(fake.received_audio)
    await fake.inject_stream("Chan A", "human-1", speech)
    await asyncio.sleep(0.8)
    new_streams = {k: v for k, v in fake.received_audio.items()
                   if k not in before and v}
    # only look at streams carrying our marker payloads, so a concurrent
    # announcement cannot flatter or spoil the result
    relay_streams = {k: v for k, v in new_streams.items()
                     if any(p in marker for p in v)}
    relayed = max((len(v) for v in relay_streams.values()), default=0)
    check("speech from Chan A is relayed into Chan B",
          relayed >= len(speech) - 2, f"{relayed}/{len(speech)} packets")
    check("exactly one copy arrives, on one stream", len(relay_streams) == 1,
          f"{len(relay_streams)} streams")

    payloads = [p for v in relay_streams.values() for p in v]
    check("Opus payloads cross unmodified (no transcode)",
          payloads == speech[:len(payloads)] and len(payloads) >= len(speech) - 2)

    # -- dead key -------------------------------------------------------
    dead = b"\x00\x02"
    before = set(fake.received_audio)
    await fake.inject_stream("Chan A", "open-mic", [dead] * 30)
    await asyncio.sleep(0.8)
    leaked = [k for k, v in fake.received_audio.items()
              if k not in before and any(p == dead for p in v)]
    check("a dead key is held back rather than relayed", not leaked,
          f"{len(leaked)} streams leaked")

    # -- text relay -----------------------------------------------------
    fake.received_text.clear()
    await fake.inject_text("Chan A", "human-1", "radio check")
    await asyncio.sleep(0.4)
    forwarded = [t for t in fake.received_text if "radio check" in t[2]]
    check("text crosses the link with attribution",
          any("[Alpha/human-1]" in t[2] for t in forwarded),
          forwarded[0][2] if forwarded else "nothing forwarded")

    # -- multi-hop reach ------------------------------------------------
    await bridge.link("bravo", "charlie", announce=False)
    await asyncio.sleep(0.2)
    reach = sorted(n.id for n in bridge.targets("alpha", "audio"))
    check("A-B plus B-C reaches C in one pass", reach == ["bravo", "charlie"],
          str(reach))
    check("nothing routes back to its source", "alpha" not in reach)

    # -- direction ------------------------------------------------------
    link = config.find_link("bravo", "charlie")
    link["mode"] = "b_to_a" if link["a"] == "charlie" else "a_to_b"
    reach_c = sorted(n.id for n in bridge.targets("charlie", "audio"))
    check("a one-way link does not carry audio backwards",
          "bravo" not in reach_c, str(reach_c))
    link["mode"] = "both"

    # -- unlink ---------------------------------------------------------
    await bridge.unlink("alpha", "bravo", announce=False)
    await asyncio.sleep(0.2)
    check("unlink stops the path",
          bridge.targets("alpha", "audio") == [])

    # -- politeness -----------------------------------------------------
    node = bridge.nodes["alpha"]
    queue = bridge.queues["alpha"]
    import time as _t
    node.last_human_activity = _t.monotonic()
    queue.clear()
    from masterlinker.speech import Announcement, EMERGENCY
    queue.submit(Announcement(kind="time", text="The time is 10 00"))
    held = queue.next_ready(quiet_for=5, busy=False)
    check("announcements wait while a channel is busy", held is None)

    queue.submit(Announcement(kind="time", text="newer"))
    check("only the newest announcement survives",
          queue.pending.text == "newer" and queue.dropped >= 1)

    queue.submit(Announcement(kind="alert", text="stand by", priority=EMERGENCY))
    urgent = queue.next_ready(quiet_for=5, busy=False)
    check("an emergency alert ignores the wait",
          urgent is not None and urgent.kind == "alert")

    released = queue.next_ready(quiet_for=9999, busy=False)
    check("the held announcement goes out once the channel is quiet",
          released is not None and released.text == "newer")

    # -- web API --------------------------------------------------------
    app = build_app(config, bridge)
    api_runner = web.AppRunner(app, access_log=None)
    await api_runner.setup()
    api_site = web.TCPSite(api_runner, "127.0.0.1", 0)
    await api_site.start()
    api_port = api_site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{api_port}"

    async with ClientSession() as session:
        async with session.get(f"{base}/api/state") as resp:
            state = await resp.json()
        check("web API returns live state", len(state["nodes"]) == 3)

        async with session.post(f"{base}/api/links",
                                json={"a": "alpha", "b": "charlie"}) as resp:
            check("web API creates a link", resp.status == 200)

        async with session.post(f"{base}/api/links/toggle",
                                json={"a": "alpha", "b": "charlie"}) as resp:
            await resp.json()
        check("web API toggles it back off",
              not config.find_link("alpha", "charlie")["enabled"])

        async with session.get(f"{base}/api/config") as resp:
            body = await resp.json()
        leaked = [n["password"] for n in body["config"]["nodes"]
                  if n.get("password") not in ("", "••••••")]
        check("the panel never sees stored passwords", not leaked, str(leaked))

        async with session.get(f"{base}/") as resp:
            html = await resp.text()
        check("the panel page is served", resp.status == 200 and "Patch" in html)

    await api_runner.cleanup()
    await bridge.stop()
    await runner.cleanup()

    print()
    failed = [name for name, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
