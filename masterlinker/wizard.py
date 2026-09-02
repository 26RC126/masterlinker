"""First-run setup.

Two passes through the channel questions, on purpose. The first pass asks for
everything including the issuer and private key. The second asks for only the
credentials and channel — which is the whole lesson: the key pair is reusable,
so every channel after the first costs you four answers, not six. By the end
you have two channels, which is the smallest number that can be crosslinked.
"""

from __future__ import annotations

import getpass
import os
import sys
from typing import Any

from . import NAME_VARIANTS, __version__
from .audio import detect_tts_backend
from .auth import make_user
from .config import Config, new_link, new_node
from .zello import TokenManager, TokenError

RULE = "─" * 62


def _say(text: str = "") -> None:
    print(text)


def ask(prompt: str, default: str = "", *, required: bool = False,
        secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    while True:
        raw = (getpass.getpass(f"{prompt}{suffix}: ") if secret
               else input(f"{prompt}{suffix}: ")).strip()
        if not raw and default:
            return default
        if raw or not required:
            return raw
        _say("  That one is needed.")


def ask_yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


def ask_choice(prompt: str, options: list[str], default_index: int = 0) -> str:
    _say(prompt)
    for index, option in enumerate(options, 1):
        marker = " (default)" if index - 1 == default_index else ""
        _say(f"  {index:>2}. {option}{marker}")
    while True:
        raw = input(f"Pick 1-{len(options)} [{default_index + 1}]: ").strip()
        if not raw:
            return options[default_index]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        _say("  Not one of those.")


def read_private_key(config_dir: str) -> str:
    """Accept a file path, or a pasted PEM block. Returns the path we saved it to."""
    _say("Your private key can be a file path, or you can paste the key itself.")
    raw = ask("Path to key file, or press enter to paste").strip()
    if raw:
        path = os.path.abspath(os.path.expanduser(raw))
        if not os.path.exists(path):
            _say(f"  Nothing at {path}.")
            return read_private_key(config_dir)
        return path

    _say("Paste the whole key, BEGIN and END lines included. It ends when you paste END.")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        lines.append(line)
        if "-----END" in line:
            break
    pem = "\n".join(lines).strip() + "\n"
    if "BEGIN" not in pem:
        _say("  That did not look like a PEM key. Try again.")
        return read_private_key(config_dir)
    path = os.path.join(config_dir, "zello.key")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(pem)
    os.chmod(path, 0o600)
    if os.name == "nt":
        _say(f"  Saved to {path}.")
        _say("  Note: Windows ignores Unix file permissions, so this is not")
        _say("  restricted to your account. Keep it somewhere only you can read.")
    else:
        _say(f"  Saved to {path} (readable only by you).")
    return path


def channel_questions(config: Config, index: int, tutorial: bool) -> dict[str, Any]:
    ordinal = "first" if index == 0 else "second"
    _say()
    _say(RULE)
    if tutorial:
        _say(f"Channel {index + 1} — and this is the tutorial bit.")
        _say("Notice what it does not ask for. The issuer and private key you")
        _say("entered above are reusable, so every channel from here on needs")
        _say("only an account and a channel name.")
    else:
        _say(f"Your {ordinal} channel.")
    _say(RULE)
    _say("The channel must not have a channel password. The Channel API cannot")
    _say("join one, and reports the channel offline rather than saying so. Control")
    _say("access by adding this account as a member instead.")
    _say()

    default_id = "alpha" if index == 0 else "bravo"
    node_id = ask("Short id, lowercase, no spaces", default_id, required=True)
    node_id = node_id.lower().replace(" ", "-")
    name = ask("Display name", node_id.title())
    channel = ask("Zello channel name, exactly as it appears in the app",
                  required=True)
    nickname = ask("What should the bridge call it out loud", name)
    username = ask("Zello username for the bridge account", required=True)
    password = ask("Zello password", secret=True)

    node = new_node(node_id, name, channel)
    node.update({
        "nickname": nickname,
        "username": username,
        "password": password,
    })
    return node


def run(config_path: str) -> int:
    config = Config(config_path)
    config_dir = os.path.dirname(os.path.abspath(config_path)) or "."
    os.makedirs(config_dir, exist_ok=True)

    _say()
    _say(RULE)
    _say(f"Setup — version {__version__}")
    _say(RULE)
    _say("This asks for everything once, then gets out of the way.")
    _say("Everything here can be changed later in the web panel.")
    _say()

    # -- the name game, settled -------------------------------------------
    _say("First, the important question.")
    name = ask_choice("What should this thing call itself?", NAME_VARIANTS, 0)
    config.data["app_name"] = name
    _say(f"  {name} it is.")

    # -- Zello keys, once --------------------------------------------------
    _say()
    _say(RULE)
    _say("Zello API keys")
    _say(RULE)
    _say("From developers.zello.com, sign in, then Keys, then Add Key.")
    _say("Copy the Issuer and the Private Key. One pair covers every channel.")
    _say()
    issuer = ask("Issuer", config.data["zello"].get("issuer", ""), required=True)
    key_path = read_private_key(config_dir)
    config.data["zello"]["issuer"] = issuer
    config.data["zello"]["private_key_path"] = key_path

    try:
        TokenManager.from_path(issuer, key_path).token()
        _say("  Key checks out — a token minted cleanly.")
    except (TokenError, OSError) as exc:
        _say(f"  Could not mint a token: {exc}")
        if not ask_yes("Carry on anyway and fix it later?", True):
            return 1

    # -- two channels ------------------------------------------------------
    first = channel_questions(config, 0, tutorial=False)
    config.upsert_node(first)

    second = channel_questions(config, 1, tutorial=True)
    config.upsert_node(second)

    if ask_yes(f"\nLink {first['id']} and {second['id']} now?", True):
        link = new_link(first["id"], second["id"])
        config.data["links"] = [link]

    # -- web panel ---------------------------------------------------------
    _say()
    _say(RULE)
    _say("Web panel")
    _say(RULE)
    if ask_yes("Require a username and password to open the panel?", True):
        config.data["web"]["require_auth"] = True
        username = ask("Panel username", "admin", required=True)
        while True:
            password = ask("Panel password", secret=True)
            if len(password) < 8:
                _say("  Make it at least 8 characters.")
                continue
            if password != ask("Repeat it", secret=True):
                _say("  Those did not match.")
                continue
            break
        config.data["users"] = [make_user(username, password)]
        config.data["web"]["multi_user"] = ask_yes(
            "Allow more than one panel account later?", False)
    else:
        config.data["web"]["require_auth"] = False
        _say("  The panel will be open to anyone who can reach it.")
        _say("  Keep it bound to 127.0.0.1 unless something else guards the door.")

    host = ask("Listen on", config.data["web"]["host"])
    port = ask("Port", str(config.data["web"]["port"]))
    config.data["web"]["host"] = host
    config.data["web"]["port"] = int(port) if port.isdigit() else 8787

    # -- environment check -------------------------------------------------
    _say()
    _say(RULE)
    _say("Checking what is installed")
    _say(RULE)
    backend = detect_tts_backend()
    if backend == "none":
        _say("  No speech engine. Announcements will go out as text instead.")
        _say("  To fix: sudo apt install espeak-ng")
    else:
        _say(f"  Speech engine: {backend}")

    try:
        from .audio import OpusEncoder
        OpusEncoder(16000, 24000, config.data["audio"].get("libopus_path", "")).close()
        _say("  libopus found — the bridge can speak and beacon.")
    except Exception as exc:
        _say(f"  {exc}")
        _say("  Relaying voice between channels still works without it;")
        _say("  only announcements and morse need an encoder.")

    config.data["setup_complete"] = True
    config.save()

    _say()
    _say(RULE)
    _say(f"Done. Config saved to {config.path}")
    _say(f"Start it with:  python -m masterlinker run --config {config.path}")
    _say(f"Then open:      http://{config.data['web']['host']}:{config.data['web']['port']}")
    _say(RULE)
    return 0


if __name__ == "__main__":
    sys.exit(run("masterlinker.json"))
