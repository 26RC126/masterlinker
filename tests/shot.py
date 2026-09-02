"""Boot the panel against the mock Zello server and screenshot it."""

import asyncio
import os
import sys
import tempfile
import threading

from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tests.test_integration as T
from masterlinker.bridge import Bridge
from masterlinker.web import build_app


async def serve(ready, hold):
    tmp = tempfile.mkdtemp()
    key_path = os.path.join(tmp, "k.key")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM,
                                   serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()))

    fake = T.FakeZello()
    runner = web.AppRunner(fake.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    config = T.make_config(os.path.join(tmp, "c.json"),
                           f"http://127.0.0.1:{port}/ws", key_path)
    for node_id, name, channel in (("delta", "Delta", "Chan D"),
                                   ("echo", "Echo relay", "Chan E")):
        from masterlinker.config import new_node
        node = new_node(node_id, name, channel)
        node["username"] = f"bot-{node_id}"
        config.upsert_node(node)
    config.save()

    bridge = Bridge(config)
    await bridge.start()
    await asyncio.sleep(0.8)

    await bridge.link("alpha", "bravo", announce=False)
    await bridge.link("bravo", "charlie", announce=False)
    await bridge.link("delta", "echo", announce=False)
    bridge.log("info", "beacon sent on alpha — M0ABC at 18 words per minute")
    bridge.log("audio", "human-1 on alpha -> bravo, charlie")
    bridge.log("text", "human-2 on bravo: back at the car, going mobile")
    bridge.log("warn", "held back 6s from open-mic on charlie (dead key, 2.1 bytes/frame)")
    bridge.log("say", "bravo: time — The time is 21 00")
    bridge.log("error", "echo: channel Chan E: invalid password (configuration)")

    app = build_app(config, bridge)
    api_runner = web.AppRunner(app, access_log=None)
    await api_runner.setup()
    api_site = web.TCPSite(api_runner, "127.0.0.1", 8799)
    await api_site.start()

    # make one channel look busy so the on-air lamp is visible
    asyncio.create_task(fake.inject_stream(
        "Chan A", "M0ABC", [bytes([90 + i % 30]) * 55 for i in range(400)]))
    await asyncio.sleep(0.5)
    bridge.push_state()

    ready.set()
    await asyncio.get_running_loop().run_in_executor(None, hold.wait)
    await api_runner.cleanup()
    await bridge.stop()
    await runner.cleanup()


def main():
    ready, hold = threading.Event(), threading.Event()
    thread = threading.Thread(target=lambda: asyncio.run(serve(ready, hold)), daemon=True)
    thread.start()
    ready.wait(30)

    import json as _json
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:8799/api/session") as r:
        print("session ->", _json.dumps(_json.load(r)))

    from playwright.sync_api import sync_playwright
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shots")
    os.makedirs(out, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.on("console", lambda m: print("console:", m.type, m.text))
        page.on("pageerror", lambda e: print("pageerror:", e))
        page.goto("http://127.0.0.1:8799/")
        page.wait_for_timeout(2500)
        page.screenshot(path=os.path.join(out, "panel.png"))

        phone = browser.new_page(viewport={"width": 390, "height": 780})
        phone.goto("http://127.0.0.1:8799/")
        phone.wait_for_timeout(2000)
        phone.screenshot(path=os.path.join(out, "panel-mobile.png"), full_page=True)

        page.click("#add-node")
        page.wait_for_timeout(600)
        page.screenshot(path=os.path.join(out, "panel-sheet.png"))
        browser.close()
    hold.set()
    print("shots written")


if __name__ == "__main__":
    main()
