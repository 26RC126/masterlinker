"""Web control panel: REST for actions, a WebSocket for live state.

Auth is on by default and single-user by default. Turn `web.multi_user` on and
the accounts list grows; leave `web.require_auth` on unless the panel is
already behind something else that authenticates.

The config the browser sees is redacted: no account passwords, no private key.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import os
import time
from collections import defaultdict
from typing import Any

from aiohttp import WSMsgType, web

from . import NAME_VARIANTS, auth
from .bridge import Bridge
from .config import Config, new_node

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
COOKIE = "masterlinker_session"

SECRET_KEYS = {"password", "hash", "salt", "secret", "private_key"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("••••••" if (k in SECRET_KEYS and v) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def client_ip(request: web.Request) -> str:
    """The caller's address, honouring a proxy only when told to trust one.

    Reading X-Forwarded-For unconditionally would let anyone spoof their way
    out of the login throttle by setting the header themselves.
    """
    config: Config = request.app["config"]
    if config.data["web"].get("behind_proxy"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote or "?"


def cookie_is_secure(config: Config) -> bool:
    web_cfg = config.data["web"]
    return bool(web_cfg.get("tls_cert")) or bool(web_cfg.get("behind_proxy"))


@web.middleware
async def security_middleware(request: web.Request, handler):
    """Security headers, plus an origin check on anything that changes state."""
    config: Config = request.app["config"]

    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("Origin")
        # Browsers always send Origin on these methods. Non-browser clients
        # (the CLI, curl) send none, and CSRF needs a browser, so a missing
        # Origin is safe to allow while a mismatched one is not.
        if origin:
            allowed = {f"{request.scheme}://{request.host}",
                       f"https://{request.host}", f"http://{request.host}"}
            allowed.update(config.data["web"].get("trusted_origins", []))
            if origin not in allowed:
                return web.json_response(
                    {"error": "Request blocked: it came from another site."},
                    status=403)

    response = await handler(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if cookie_is_secure(config):
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000")
    return response


@web.middleware
async def auth_middleware(request: web.Request, handler):
    config: Config = request.app["config"]
    public = request.path in ("/api/login", "/api/session", "/health") or \
        not request.path.startswith("/api")
    if not config.data["web"].get("require_auth", True) or public:
        return await handler(request)

    token = request.cookies.get(COOKIE, "")
    payload = auth.read_token(config.data["web"]["secret"], token) if token else None
    if payload is None:
        return web.json_response({"error": "not signed in"}, status=401)
    request["user"] = payload
    return await handler(request)


def build_app(config: Config, bridge: Bridge) -> web.Application:
    app = web.Application(middlewares=[security_middleware, auth_middleware])
    app["login_failures"] = defaultdict(list)
    app["config"] = config
    app["bridge"] = bridge

    # -- session ---------------------------------------------------------

    async def session_info(request: web.Request) -> web.Response:
        web_cfg = config.data["web"]
        token = request.cookies.get(COOKIE, "")
        payload = auth.read_token(web_cfg["secret"], token) if token else None
        return web.json_response({
            "app_name": config.app_name,
            "require_auth": web_cfg.get("require_auth", True),
            "multi_user": web_cfg.get("multi_user", False),
            "signed_in": payload is not None or not web_cfg.get("require_auth", True),
            "user": payload.get("u") if payload else None,
            "setup_complete": config.data.get("setup_complete", False),
        })

    async def login(request: web.Request) -> web.Response:
        web_cfg = config.data["web"]
        failures: dict[str, list[float]] = request.app["login_failures"]
        limit = int(web_cfg.get("login_max_attempts", 5))
        lockout = float(web_cfg.get("login_lockout_s", 300))
        now = time.monotonic()

        # Throttle by address and by username, so neither a single host
        # hammering the form nor a spread-out attempt on one account gets
        # unlimited guesses.
        body = await request.json()
        username = body.get("username", "")
        keys = [f"ip:{client_ip(request)}", f"user:{username}"]
        for key in keys:
            failures[key] = [t for t in failures[key] if now - t < lockout]
        blocked = next((k for k in keys if len(failures[k]) >= limit), None)
        if blocked:
            wait = int(lockout - (now - failures[blocked][0]))
            bridge.log("warn", f"sign-in blocked for {blocked} — too many attempts")
            return web.json_response(
                {"error": f"Too many attempts. Try again in {max(1, wait)} seconds."},
                status=429)

        user = auth.authenticate(config.data["users"], username,
                                 body.get("password", ""))
        if user is None:
            for key in keys:
                failures[key].append(now)
            bridge.log("warn", f"failed sign-in for '{username}' "
                               f"from {client_ip(request)}")
            return web.json_response({"error": "That username and password did not match."},
                                     status=401)

        for key in keys:
            failures.pop(key, None)
        hours = int(web_cfg.get("session_hours", 12))
        token = auth.issue_token(web_cfg["secret"], user["username"],
                                 user.get("role", "admin"), hours)
        response = web.json_response({"ok": True, "user": user["username"]})
        secure = cookie_is_secure(config)
        response.set_cookie(COOKIE, token, httponly=True,
                            samesite="Strict" if secure else "Lax",
                            secure=secure, max_age=hours * 3600, path="/")
        bridge.log("info", f"{user['username']} signed in from {client_ip(request)}")
        return response

    async def logout(_: web.Request) -> web.Response:
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE)
        return response

    # -- state -----------------------------------------------------------

    async def state(_: web.Request) -> web.Response:
        return web.json_response(bridge.snapshot())

    async def get_config(_: web.Request) -> web.Response:
        return web.json_response({
            "config": redact(copy.deepcopy(config.data)),
            "name_variants": NAME_VARIANTS,
        })

    async def get_log(_: web.Request) -> web.Response:
        return web.json_response({"events": list(bridge.events)[-200:]})

    # -- nodes -----------------------------------------------------------

    async def create_node(request: web.Request) -> web.Response:
        body = await request.json()
        node_id = (body.get("id") or "").strip().lower().replace(" ", "-")
        if not node_id:
            return web.json_response({"error": "Give the node an id."}, status=400)
        if config.node(node_id):
            return web.json_response({"error": f"There is already a node called {node_id}."},
                                     status=409)
        node = new_node(node_id, body.get("name") or node_id, body.get("channel", ""))
        for key in ("username", "password", "nickname",
                    "ws_url", "listen_only", "text_only"):
            if key in body:
                node[key] = body[key]
        config.upsert_node(node)
        config.save()
        await bridge.rebuild_nodes()
        return web.json_response({"ok": True, "node": redact(node)})

    async def update_node(request: web.Request) -> web.Response:
        node_id = request.match_info["node_id"]
        node = config.node(node_id)
        if node is None:
            return web.json_response({"error": "No node with that id."}, status=404)
        body = await request.json()
        for key, value in body.items():
            if key == "id":
                continue
            if key in SECRET_KEYS and value == "••••••":
                continue     # unchanged in the UI, do not overwrite with the mask
            node[key] = value
        config.save()
        await bridge.rebuild_nodes()
        return web.json_response({"ok": True, "node": redact(node)})

    async def delete_node(request: web.Request) -> web.Response:
        config.remove_node(request.match_info["node_id"])
        config.save()
        await bridge.rebuild_nodes()
        return web.json_response({"ok": True})

    # -- links -----------------------------------------------------------

    async def create_link(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            link = await bridge.link(body["a"], body["b"], body.get("mode", "both"),
                                     announce=body.get("announce", True))
        except (KeyError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"ok": True, "link": link})

    async def delete_link(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        await bridge.unlink(request.match_info["a"], request.match_info["b"],
                            announce=body.get("announce", True),
                            remove=body.get("remove", False))
        return web.json_response({"ok": True})

    async def toggle_link(request: web.Request) -> web.Response:
        body = await request.json()
        a, b = body["a"], body["b"]
        link = config.find_link(a, b)
        if link and link.get("enabled", True):
            await bridge.unlink(a, b, announce=body.get("announce", True))
        else:
            await bridge.link(a, b, body.get("mode", "both"),
                              announce=body.get("announce", True))
        return web.json_response({"ok": True})

    async def update_link(request: web.Request) -> web.Response:
        body = await request.json()
        link = config.find_link(body["a"], body["b"])
        if link is None:
            return web.json_response({"error": "No link between those nodes."}, status=404)
        for key in ("mode", "media", "note"):
            if key in body:
                link[key] = body[key]
        config.save()
        bridge.push_state()
        return web.json_response({"ok": True, "link": link})

    async def apply_preset(request: web.Request) -> web.Response:
        try:
            count = bridge.apply_preset(request.match_info["name"])
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        return web.json_response({"ok": True, "links": count})

    async def save_preset(request: web.Request) -> web.Response:
        body = await request.json()
        name = (body.get("name") or "").strip()
        if not name:
            return web.json_response({"error": "Name the preset."}, status=400)
        links = [{"a": l["a"], "b": l["b"], "mode": l.get("mode", "both")}
                 for l in config.data["links"] if l.get("enabled", True)]
        config.data["presets"] = [p for p in config.data["presets"] if p["name"] != name]
        config.data["presets"].append({"name": name, "links": links})
        config.save()
        bridge.push_state()
        return web.json_response({"ok": True, "links": len(links)})

    # -- speaking --------------------------------------------------------

    async def say(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            await bridge.say(body["node"], body.get("text", ""))
        except (KeyError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"ok": True})

    async def emergency(request: web.Request) -> web.Response:
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "Write the alert first."}, status=400)
        await bridge.emergency(text, body.get("nodes"))
        return web.json_response({"ok": True})

    # -- settings --------------------------------------------------------

    async def update_settings(request: web.Request) -> web.Response:
        body = await request.json()
        for section in ("web", "zello", "audio", "bridge", "node_defaults"):
            if section in body:
                for key, value in body[section].items():
                    if key in SECRET_KEYS and value == "••••••":
                        continue
                    config.data[section][key] = value
        if "app_name" in body and body["app_name"] in NAME_VARIANTS:
            config.data["app_name"] = body["app_name"]
        config.save()
        bridge.push_state()
        return web.json_response({"ok": True})

    # -- live feed -------------------------------------------------------

    async def events(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        bridge.subscribers.add(queue)
        pump: asyncio.Task | None = None
        try:
            # These sends can fail if the browser goes away mid-handshake, so
            # they belong inside the try — otherwise the queue is never
            # discarded and accumulates for the life of the process.
            await ws.send_json({"type": "state", "state": bridge.snapshot()})
            for event in list(bridge.events)[-60:]:
                await ws.send_json({"type": "log", "event": event})
            pump = asyncio.create_task(_pump(ws, queue))
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            bridge.subscribers.discard(queue)
            if pump is not None:
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump
        return ws

    async def _pump(ws: web.WebSocketResponse, queue: asyncio.Queue) -> None:
        while not ws.closed:
            message = await queue.get()
            with contextlib.suppress(Exception):
                await ws.send_json(message)

    async def index(_: web.Request) -> web.Response:
        return web.FileResponse(os.path.join(STATIC, "index.html"))

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "app": config.app_name})

    app.add_routes([
        web.get("/", index),
        web.get("/health", health),
        web.post("/api/login", login),
        web.post("/api/logout", logout),
        web.get("/api/session", session_info),
        web.get("/api/state", state),
        web.get("/api/config", get_config),
        web.get("/api/log", get_log),
        web.post("/api/settings", update_settings),
        web.post("/api/nodes", create_node),
        web.put("/api/nodes/{node_id}", update_node),
        web.delete("/api/nodes/{node_id}", delete_node),
        web.post("/api/links", create_link),
        web.put("/api/links", update_link),
        web.post("/api/links/toggle", toggle_link),
        web.delete("/api/links/{a}/{b}", delete_link),
        web.post("/api/presets", save_preset),
        web.post("/api/presets/{name}/apply", apply_preset),
        web.post("/api/say", say),
        web.post("/api/emergency", emergency),
        web.get("/api/events", events),
        web.static("/static", STATIC),
    ])
    return app
