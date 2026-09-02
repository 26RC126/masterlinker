"""Configuration store for Masterlinker.

Everything is a plain dict backed by JSON on disk, so the web UI, the CLI wizard
and the running bridge all read and write the same shape. Defaults are applied
by deep-merge on load, which means adding a new setting in a later version never
breaks an existing config file.
"""

from __future__ import annotations

import copy
import json
import os
import secrets
import tempfile
import threading
from typing import Any

from . import DEFAULT_NAME

CONFIG_VERSION = 1

# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

# Per-node feature defaults. A node inherits these; anything set on the node
# itself wins. Kept separate so the wizard can show "leave blank for default".
NODE_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "listen_only": False,
    "text_only": False,  # accessibility: never transmit audio into this channel

    # What the node is called out loud. Falls back to name, then channel.
    "nickname": "",

    "tts": {
        "announce_link": True,
        "announce_unlink": True,
        # "nickname"  -> "Linked to Alpha"
        # "channel"   -> "Linked to My Channel Name"
        # "brief"     -> "Linked" / "Unlinked"
        # "silent"    -> nothing spoken (still logged + optionally texted)
        "style": "nickname",
        "voice": None,          # backend-specific voice id, None = backend default
        "rate_wpm": 165,
        "gain_db": 0.0,
        "also_text": False,     # mirror the spoken announcement as a text message
    },

    # "Shush" — hold automated speech while humans are using the channel.
    "politeness": {
        "enabled": True,
        "hold_after_human_s": 210,   # 3.5 minutes
        "coalesce": True,            # only the newest pending item survives
        "max_hold_s": 1800,          # after this, give up on a stale item
        "text_fallback": False,      # if held too long, send it as text instead
        "stale_drop_kinds": ["time", "beacon"],  # a late clock read is worse than none
    },

    "announcer": {
        "enabled": False,
        "prefix": "",               # e.g. "Masterlinker time check."
        "items": [
            # {"kind": "time", "every_minutes": 60, "enabled": True}
            # {"kind": "date", "at": "08:00", "enabled": True}
            # {"kind": "weather", "every_minutes": 180, "enabled": True}
            # {"kind": "custom", "every_minutes": 30, "text": "...", "enabled": True}
        ],
        "clock": "24h",             # "24h" or "12h"
        "timezone": "",             # blank = system tz
        "location": {"lat": None, "lon": None, "label": ""},
        "units": "metric",
    },

    "morse": {
        "enabled": False,
        "text": "",                 # callsign, name, whatever you like
        "every_minutes": 10,
        "wpm": 18,
        "tone_hz": 700,
        "gain_db": -6.0,
        "polite": True,             # obey the shush rules like any other announcement
    },

    # Chat -> voice, for people who type instead of talking.
    "chat_tts": {
        "enabled": False,
        "gate_s": 210,              # wait this long after the last human transmission
        "prefix_sender": True,      # "Alex says: ..."
        "max_chars": 400,
        "trigger": "all",           # "all" or "prefix"
        "trigger_prefix": "!say",
    },

    # Voice -> chat, using Zello's own transcription feature.
    # Availability depends on the Zello network; harmless if unsupported.
    "transcription_relay": {
        "enabled": False,
        "min_confidence": 0.55,
        "to_own_channel": False,    # post the transcript back into the source channel
    },

    "guards": {
        # Anti stuck-mic: cap a single relayed transmission.
        "tot_stream_s": 180,
        "tot_action": "close",      # "close" = stop relaying, "mute" = also cooldown the user
        "tot_cooldown_s": 60,

        # Dead key / open mic: judge the first slice of a stream by how much
        # data Opus is actually producing. Silence compresses to almost nothing.
        "deadkey_enabled": True,
        "deadkey_eval_ms": 800,
        "deadkey_max_avg_bytes": 12,
        "deadkey_late_open": True,  # if they start speaking later, open the relay then

        # Woodpecker / kerchunking: repeated micro-transmissions.
        "kerchunk_enabled": True,
        "kerchunk_max_s": 1.5,
        "kerchunk_count": 4,
        "kerchunk_window_s": 30,
        "kerchunk_cooldown_s": 120,

        # Auto-unlink a link that has carried nothing for this long.
        "link_idle_timeout_min": 180,
        "link_idle_timeout_enabled": True,
    },

    "forwarding": {
        "audio": True,
        "text": True,
        "image": True,
        "location": False,
        "attribute_sender": True,   # prefix forwarded text with the sender + source
        "text_rate_per_min": 20,
        "image_max_bytes": 2_000_000,
        "image_rate_per_min": 6,
    },
}

DEFAULTS: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "app_name": DEFAULT_NAME,
    "setup_complete": False,

    "web": {
        "host": "127.0.0.1",
        "port": 8787,
        "require_auth": True,
        "multi_user": False,
        "session_hours": 12,
        "secret": "",               # generated on first save
    },

    "zello": {
        "ws_url": "wss://zello.io/ws",
        "issuer": "",
        "private_key_path": "zello.key",
        "token_ttl_s": 3600,
        "reconnect_min_s": 2,
        "reconnect_max_s": 60,
        "client_version": "masterlinker",
        "platform_name": "Masterlinker Gateway",
        "request_transcriptions": False,
    },

    "audio": {
        "tts_backend": "auto",      # auto | espeak-ng | piper | say | none
        "piper_model": "",
        "sample_rate": 16000,       # what we announce in codec_header
        "frame_ms": 20,
        "opus_bitrate": 24000,
        "libopus_path": "",         # blank = search the usual names
    },

    "bridge": {
        "max_hops": 3,
        "dedupe_window_s": 8,
        "ptt_release_grace_ms": 120,
    },

    "users": [],                    # [{"username","salt","hash","role"}]
    "nodes": [],
    "links": [],
    "presets": [],                  # [{"name","links":[...]}] one-click topologies
    "node_defaults": copy.deepcopy(NODE_DEFAULTS),
}


def _deep_merge(base: Any, override: Any) -> Any:
    """Override wins, but keys missing from override keep the default."""
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = _deep_merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(override) if override is not None else copy.deepcopy(base)


class Config:
    """Thread-safe JSON config with atomic writes."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.migration_notes: list[str] = []
        self.load()

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                self.data = _deep_merge(copy.deepcopy(DEFAULTS), raw)
            else:
                self.data = copy.deepcopy(DEFAULTS)
            if not self.data["web"].get("secret"):
                self.data["web"]["secret"] = secrets.token_urlsafe(32)
            self.migration_notes = self._migrate()
            if self.migration_notes and os.path.exists(self.path):
                self.save()

    def _migrate(self) -> list[str]:
        """Drop settings that no longer do anything.

        `channel_password` was collected by early versions. The Channel API
        logon command has no field to put one in, so the value could never be
        sent anywhere — it was a credential sitting on disk with no
        destination. Anything found is removed on load and the file rewritten.
        """
        notes: list[str] = []
        for node in self.data.get("nodes", []):
            if "channel_password" in node:
                had_value = bool(node.pop("channel_password"))
                if had_value:
                    notes.append(
                        f"removed a stored channel password from node "
                        f"'{node.get('id', '?')}' — the Zello Channel API has no "
                        f"field for one, so it was never sent anywhere"
                    )
        return notes

    def save(self) -> None:
        with self._lock:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ml-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
                os.replace(tmp, self.path)
                try:
                    os.chmod(self.path, 0o600)   # it holds account passwords
                except OSError:
                    pass
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

    # -- convenience ------------------------------------------------------

    @property
    def app_name(self) -> str:
        return self.data.get("app_name") or DEFAULT_NAME

    def node(self, node_id: str) -> dict[str, Any] | None:
        return next((n for n in self.data["nodes"] if n["id"] == node_id), None)

    def resolved_node(self, node_id: str) -> dict[str, Any] | None:
        """Node config with node_defaults merged underneath it."""
        node = self.node(node_id)
        if node is None:
            return None
        return _deep_merge(copy.deepcopy(self.data["node_defaults"]), node)

    def resolved_nodes(self) -> list[dict[str, Any]]:
        return [self.resolved_node(n["id"]) for n in self.data["nodes"]]  # type: ignore[misc]

    def spoken_name(self, node_id: str) -> str:
        node = self.node(node_id) or {}
        return node.get("nickname") or node.get("name") or node.get("channel") or node_id

    def upsert_node(self, node: dict[str, Any]) -> None:
        with self._lock:
            existing = self.node(node["id"])
            if existing:
                existing.update(node)
            else:
                self.data["nodes"].append(node)

    def remove_node(self, node_id: str) -> None:
        with self._lock:
            self.data["nodes"] = [n for n in self.data["nodes"] if n["id"] != node_id]
            self.data["links"] = [
                l for l in self.data["links"]
                if l["a"] != node_id and l["b"] != node_id
            ]

    def find_link(self, a: str, b: str) -> dict[str, Any] | None:
        for link in self.data["links"]:
            if (link["a"], link["b"]) == (a, b) or (link["a"], link["b"]) == (b, a):
                return link
        return None


def new_link(a: str, b: str, mode: str = "both") -> dict[str, Any]:
    return {
        "id": f"{a}~{b}",
        "a": a,
        "b": b,
        "mode": mode,              # "both" | "a_to_b" | "b_to_a"
        "enabled": True,
        "media": {"audio": True, "text": True, "image": True, "location": False},
        "created_at": None,
        "note": "",
    }


def new_node(node_id: str, name: str, channel: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "name": name,
        "platform": "zello",
        "channel": channel,
        "username": "",
        "password": "",
        "ws_url": "",               # blank = inherit zello.ws_url
        "nickname": "",
    }
