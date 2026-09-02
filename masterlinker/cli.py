"""Command line front end."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys

from . import __version__
from .bridge import Bridge
from .config import Config
from .web import build_app

DEFAULT_CONFIG = os.environ.get("MASTERLINKER_CONFIG", "masterlinker.json")


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

async def _serve(config: Config) -> None:
    from aiohttp import web

    bridge = Bridge(config)
    await bridge.start()
    app = build_app(config, bridge)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    host = config.data["web"]["host"]
    port = int(config.data["web"]["port"])
    site = web.TCPSite(runner, host, port)
    await site.start()

    print(f"{config.app_name} {__version__} — panel on http://{host}:{port}")
    if not config.data["web"].get("require_auth", True):
        print("  Panel authentication is off.")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("  Reachable beyond this machine. Put it behind TLS if that is not deliberate.")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals_available = False
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            signals_available = True
        except NotImplementedError:
            pass   # Windows: the proactor loop has no signal handlers

    try:
        if signals_available:
            await stop.wait()
        else:
            # Without a handler, Windows only delivers KeyboardInterrupt when
            # the loop wakes up. Waiting on an Event that nothing will ever set
            # would swallow Ctrl+C entirely, so tick instead.
            while not stop.is_set():
                await asyncio.sleep(0.4)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nShutting down…")
        await bridge.stop()
        await runner.cleanup()


def cmd_run(args) -> int:
    config = Config(args.config)
    if not config.data.get("setup_complete") and not config.data["nodes"]:
        print(f"No setup found at {config.path}.")
        print(f"Run:  python -m masterlinker setup --config {args.config}")
        return 1
    if args.host:
        config.data["web"]["host"] = args.host
    if args.port:
        config.data["web"]["port"] = args.port
    if args.name:
        config.data["app_name"] = args.name
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    try:
        asyncio.run(_serve(config))
    except KeyboardInterrupt:
        pass
    return 0


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def cmd_doctor(args) -> int:
    from .audio import OpusEncoder, detect_tts_backend
    from .zello import TokenManager, TokenError

    config = Config(args.config)
    ok = True
    print(f"Config: {config.path}")
    print(f"Name:   {config.app_name}")
    print()

    backend = detect_tts_backend()
    if backend == "none":
        print("Speech engine   not found — announcements will fall back to text")
        print("                fix: sudo apt install espeak-ng")
        ok = False
    else:
        print(f"Speech engine   {backend}")

    try:
        OpusEncoder(int(config.data["audio"]["sample_rate"]),
                    int(config.data["audio"]["opus_bitrate"]),
                    config.data["audio"].get("libopus_path", "")).close()
        print("Opus encoder    ready")
    except Exception as exc:
        print(f"Opus encoder    {exc}")
        print("                relaying voice still works; announcements do not")
        ok = False

    zcfg = config.data["zello"]
    key_path = zcfg.get("private_key_path", "")
    if not os.path.exists(key_path):
        print(f"Private key     missing at {key_path}")
        ok = False
    else:
        try:
            TokenManager.from_path(zcfg["issuer"], key_path).token()
            print("Auth token      minted successfully")
        except (TokenError, OSError) as exc:
            print(f"Auth token      {exc}")
            ok = False

    print()
    for note in config.migration_notes:
        print(f"Config            {note}")

    for node in config.data["nodes"]:
        flags = []
        if not node.get("username"):
            flags.append("no username, so it will connect anonymously and listen only")
        state = "; ".join(flags) if flags else "looks fine"
        print(f"  {node['id']:<12} {node.get('channel', '?'):<28} {state}")
        if flags:
            ok = False

    print()
    print("All good." if ok else "Some things need attention above.")
    return 0 if ok else 2


# --------------------------------------------------------------------------
# remote control
# --------------------------------------------------------------------------

async def _call(config: Config, method: str, path: str, body: dict | None = None):
    import aiohttp

    base = f"http://{config.data['web']['host']}:{config.data['web']['port']}"
    async with aiohttp.ClientSession() as session:
        if config.data["web"].get("require_auth", True):
            username = os.environ.get("MASTERLINKER_USER", "")
            password = os.environ.get("MASTERLINKER_PASSWORD", "")
            if not username:
                raise SystemExit(
                    "Set MASTERLINKER_USER and MASTERLINKER_PASSWORD to use this command."
                )
            async with session.post(f"{base}/api/login",
                                    json={"username": username, "password": password}) as resp:
                if resp.status != 200:
                    raise SystemExit("Sign in failed.")
        async with session.request(method, base + path, json=body) as resp:
            payload = await resp.json()
            if resp.status >= 400:
                raise SystemExit(payload.get("error", f"Request failed ({resp.status})"))
            return payload


def cmd_link(args) -> int:
    config = Config(args.config)
    asyncio.run(_call(config, "POST", "/api/links",
                      {"a": args.a, "b": args.b, "mode": args.mode}))
    print(f"Linked {args.a} and {args.b}.")
    return 0


def cmd_unlink(args) -> int:
    config = Config(args.config)
    asyncio.run(_call(config, "DELETE", f"/api/links/{args.a}/{args.b}", {}))
    print(f"Unlinked {args.a} and {args.b}.")
    return 0


def cmd_status(args) -> int:
    config = Config(args.config)
    state = asyncio.run(_call(config, "GET", "/api/state"))
    if args.json:
        print(json.dumps(state, indent=2))
        return 0
    for node in state["nodes"]:
        lamp = ("fault" if node["error"] else
                "on-air" if node["talking"] or node["transmitting"] else
                "ready" if node["connected"] and node["status"] == "online" else "down")
        detail = node["error"] or (", ".join(node["talking"]) or node["channel"])
        print(f"  {node['id']:<12} {lamp:<7} {node['users_online']:>3} in channel  {detail}")
    print()
    for link in state["links"]:
        mark = "linked  " if link["enabled"] else "unlinked"
        print(f"  {mark} {link['a']} <-> {link['b']} ({link['mode']})")
    return 0


def cmd_say(args) -> int:
    config = Config(args.config)
    path = "/api/emergency" if args.emergency else "/api/say"
    body = ({"text": args.text, "nodes": [args.node] if args.node else None}
            if args.emergency else {"node": args.node, "text": args.text})
    asyncio.run(_call(config, "POST", path, body))
    print("Queued.")
    return 0


# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="masterlinker",
        description="Crosslink Zello channels, with a web control panel.",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"config file (default: {DEFAULT_CONFIG})")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="run first-time setup")
    p.set_defaults(func=lambda a: __import__(
        "masterlinker.wizard", fromlist=["run"]).run(a.config))

    p = sub.add_parser("run", help="start the bridge and the web panel")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--name", help="override the program name for this run")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("doctor", help="check keys, codec and speech engine")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", help="show live state of a running bridge")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("link", help="link two channels")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--mode", default="both", choices=["both", "a_to_b", "b_to_a"])
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", help="unlink two channels")
    p.add_argument("a")
    p.add_argument("b")
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser("say", help="speak something into a channel")
    p.add_argument("node")
    p.add_argument("text")
    p.add_argument("--emergency", action="store_true",
                   help="skip the wait-for-a-gap rule")
    p.set_defaults(func=cmd_say)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
