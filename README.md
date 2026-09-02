# Masterlinker

Crosslink Zello channels to each other, from a command line or a web panel.
Link A to B, then B to C, unlink any of it whenever you like.

Also answers to LinkMaster, MasterLink, Master-Linker, Masterlinking,
Mastercrosslinking, Master-Crosslinker and Masterlink. Setup asks which one you
want, and every screen and announcement uses it from then on.

![The control panel: a patch matrix, channel strips with on-air lamps, and a
live activity log](docs/panel.png)

---

## Contents

- [What it does](#what-it-does)
- [Before you start](#before-you-start)
- [Install](#install)
- [Setup](#setup)
- [Running it](#running-it)
- [The panel](#the-panel)
- [Features in detail](#features-in-detail)
- [Accessibility](#accessibility)
- [Command line](#command-line)
- [Configuration reference](#configuration-reference)
- [How it works inside](#how-it-works-inside)
- [Things that will bite you](#things-that-will-bite-you)
- [Testing](#testing)

---

## What it does

Each **node** is one Zello channel. A **link** is a patch between two nodes.
Audio, text and images that arrive on one side come out the other.

```
   Channel C  ←──── link ────→  Channel A  ←──── link ────→  Channel B
```

Every link is two-way, which is the default. Two links are enough for three
channels: B and C have no link of their own, but B is heard on C through A, and
C is heard on B the same way. A third link between them would add nothing.

Links can be made one-way if you want them to be (`both`, `a_to_b`, `b_to_a`),
can be switched off without being deleted, and can carry audio, text and images
independently.

---

## Before you start

You need three things from Zello.

**1. A dedicated Zello account for the bridge.** Not your personal one. This
account is the "user" that appears to be talking whenever audio crosses into a
channel, and it needs permission to talk and listen in every channel you bridge.

**2. An issuer and a private key.** Sign in at
[developers.zello.com](https://developers.zello.com/), go to **Keys**, then
**Add Key**. Copy the **Issuer** and the **Private Key** in full, including the
`-----BEGIN` and `-----END` lines.

The key pair is reusable. One pair covers every channel you will ever add,
which is why setup asks for it once and never again.

**3. Channels without channel passwords.** A password on the channel itself
stops this working entirely, and there is no setting here that gets round it.
See [Things that will bite you](#things-that-will-bite-you).

---

## Install

Python 3.11 or newer.

```bash
sudo apt install python3-venv libopus0 espeak-ng     # Debian, Ubuntu, Raspberry Pi OS
git clone https://github.com/26RC126/masterlinker.git && cd masterlinker
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

`libopus0` and `espeak-ng` are only needed for audio the bridge *creates*:
announcements, chat-to-speech, morse. Relaying voice between channels works
without either. If they are missing, announcements go out as text messages
instead and the panel says so rather than failing silently.

Optional: `pip install Pillow` gives proper thumbnails when forwarding images.
Without it the full image is sent as its own thumbnail, which works but wastes
bandwidth.

Check what you have:

```bash
./venv/bin/python -m masterlinker doctor
```

```
Speech engine   espeak-ng
Opus encoder    ready
Auth token      minted successfully

  alpha        My First Channel             looks fine
  bravo        My Second Channel            looks fine

All good.
```

---

## Setup

```bash
./venv/bin/python -m masterlinker setup
```

This writes two files into the working directory: `masterlinker.json`, which
holds your Zello account password, and `zello.key`, your RSA private key. Both
are created `0600` and both are in `.gitignore`. If you move them, keep them
out of version control — a leaked key has to be revoked at
developers.zello.com, not just deleted.

It asks, in order:

1. **Which name to use.** Ten variants, pick one.
2. **Issuer and private key.** Paste the key or give a path. If you paste it,
   it is saved to `zello.key` with `0600` permissions. The wizard immediately
   mints a token to prove the key works before you go any further.
3. **Your first channel.** Id, display name, Zello channel name, spoken
   nickname, account username and password.
4. **Your second channel — the tutorial.** Same questions minus the issuer and
   the key, because those are reusable. That is the whole lesson: every channel
   after the first costs four answers, not six. Two channels is also the
   smallest number you can crosslink, so you finish setup with something that
   works rather than something half-built.
5. **Whether to link them now.**
6. **The web panel account**, host and port.

Everything is editable afterwards in the panel or by hand in the JSON.

---

## Running it

```bash
./venv/bin/python -m masterlinker run
```

```
Masterlinker 0.1.0 — panel on http://127.0.0.1:8787
```

As a service:

```bash
sudo useradd --system --home /opt/masterlinker masterlinker
sudo cp -r . /opt/masterlinker && sudo chown -R masterlinker: /opt/masterlinker
sudo cp masterlinker.service /etc/systemd/system/
sudo systemctl enable --now masterlinker
journalctl -u masterlinker -f
```

The default host is `127.0.0.1`. If you change it, put the panel behind TLS —
sessions are cookies and the login posts a password. The program prints a
warning when it binds to anything else.

---

## The panel

The middle of the screen is a patch bay. Rows send, columns receive, and
clicking a hole patches or unpatches that pair. The diagonal is inert.

- A **green lamp** means connected and the channel is online.
- An **amber lamp** means someone is talking, or the bridge is transmitting.
  A patched hole turns amber too, so you can see the path audio is taking.
- A **red lamp** means the channel reported a problem, and the strip shows what
  the server actually said.

The number on the right of each strip is how many people are in that channel.

**Save layout** stores the current set of links as a named preset. Picking a
preset from the dropdown applies it, which is one click to go from "everything
patched for the Sunday net" to "just the two everyday channels".

On a phone the matrix becomes a list, with patched pairs sorted to the top.

---

## Features in detail

### Link and unlink

From the panel, the API, or the command line. Links persist across restarts.
Turning a link off keeps its settings; removing it discards them.

Reach is computed across the whole graph, not pair by pair. If A is linked to B
and B to C, audio from A goes to **both** B and C in one pass. It is never
relayed twice and never comes back to where it started. `bridge.max_hops`
(default 3) caps how far a signal travels.

### Spoken link and unlink announcements

Per channel, four styles:

| `tts.style` | What it says |
|---|---|
| `nickname` | "Unlinked from Alpha" — uses the node's spoken nickname |
| `channel` | "Unlinked from My First Channel" — uses the real channel name |
| `brief` | "Unlinked" |
| `silent` | Nothing spoken; still logged, and still texted if `also_text` is on |

Each side announces independently, so Channel A can be chatty and Channel B
terse. `tts.also_text` mirrors the announcement as a text message.

### Who is talking

Every channel strip shows live talk state from `on_stream_start` and
`on_stream_stop`, pushed to the browser over a WebSocket. `masterlinker status`
shows the same thing in a terminal.

### Text and image forwarding

Text crosses links in both directions and is attributed by default:
`[Alpha/human-1] radio check`. Turn `forwarding.attribute_sender` off for bare
relaying. Images are forwarded as JPEG with a size cap
(`forwarding.image_max_bytes`, default 2 MB) and a rate cap.

Duplicate suppression stops the same message bouncing around a mesh: identical
text from the same sender inside `bridge.dedupe_window_s` is only relayed once.

### Automatic announcements

Per channel, any mix of:

- **`time`** — "The time is 21 40". 12 or 24 hour.
- **`date`** — "Today is Wednesday the 2 of September 2026".
- **`weather`** — from Open-Meteo, which needs no API key. Set
  `announcer.location.lat` and `.lon`. Reads condition, temperature, apparent
  temperature when it differs by 2 degrees or more, and wind.
- **`custom`** — anything you write.

Each item fires either `every_minutes` or `at: "HH:MM"`.

### Morse beacon

Per channel. Real PARIS timing (one dit is 1200/wpm milliseconds), a sine tone
with raised-cosine edges so it does not click, configurable speed, pitch and
level. Send your callsign every ten minutes, or your name, or anything in the
character set.

By default the beacon is polite and waits its turn like everything else. Set
`morse.polite: false` if it must go out on schedule regardless.

### Shush — waiting for a gap

**On by default, 210 seconds.**

Automated speech does not talk over people. After any human transmission or
text message on a channel, the bridge holds its tongue for
`politeness.hold_after_human_s`, and it will not start while a stream is in
progress either way.

Only **one** automated item is ever pending per channel. A newer one replaces
the older one rather than stacking, because two announcements back to back is
worse than one late announcement.

If an item waits longer than `politeness.max_hold_s` (default 30 minutes) it is
either dropped or sent as text, depending on `politeness.text_fallback`. Kinds
listed in `stale_drop_kinds` (default `time` and `beacon`) are always dropped
when stale — a clock reading that is half an hour out is worse than silence.

### Emergency alerts

Skip the queue and ignore the wait. Spoken and texted into every connected
channel. They still will not cut into a transmission already in progress —
that is not something the protocol allows — but they go out in the first gap
ahead of everything else.

From the panel's **Emergency alert** button, or:

```bash
masterlinker say alpha "All stations, stand by" --emergency
```

### Timeout timers

Two of them, because "timeout" means two different things on a bridge and both
are worth having.

- **`guards.tot_stream_s`** — default **180 seconds**. Caps a single relayed
  transmission. This is the classic anti-stuck-mic timer. When it fires the
  relay closes; set `tot_action: "mute"` to also put that user on a cooldown.
- **`guards.link_idle_timeout_min`** — default **180 minutes**. Unlinks a link
  that has carried nothing for three hours, so a forgotten patch does not sit
  there indefinitely.

If you only wanted one, set the other to `0` to disable it.

### Dead key and woodpecker protection

**Dead key.** The bridge never decodes audio, so it cannot measure loudness
directly. It does not need to: Opus is variable bitrate, and near-silence
compresses to almost nothing. Measured on a real system, speech averages about
**49 bytes per frame** and digital silence about **2**. The default threshold
of 12 sits comfortably between them.

The first `deadkey_eval_ms` (default 800 ms) of a stream are buffered rather
than forwarded while the bridge decides. If it turns out to be speech, the
buffer is flushed and nothing is lost — you just pay 800 ms of latency. If it
is an open mic, nothing crosses at all and the other channels stay usable.
`deadkey_late_open` covers "keyed up, went quiet, then started talking".

Set `deadkey_enabled: false` for zero added latency at the cost of the check.

**Woodpecker.** `kerchunk_count` transmissions (default 4) shorter than
`kerchunk_max_s` (default 1.5 s) inside `kerchunk_window_s` (default 30 s) put
that specific user on a `kerchunk_cooldown_s` cooldown. Per user per channel —
one person kerchunking never silences anyone else.

### Chat to speech

**Off by default.** Someone types in the channel, the bridge says it aloud, so
people who cannot or would rather not talk are still heard.

`chat_tts.gate_s` (default 210 s) makes it wait for a gap, same as any other
speech. `trigger: "prefix"` restricts it to messages starting with `!say` if
you do not want every message read out. `prefix_sender` reads "Alex says:"
before the message so listeners know who it came from.

---

## Accessibility

Beyond chat-to-speech, four things that were worth adding.

**Transcription relay.** Zello can transcribe voice messages: set
`zello.request_transcriptions: true` and `transcription_relay.enabled` on a
node, and voice arriving there is posted as text into the linked channels.

*Why it matters:* it closes the loop. Chat-to-speech lets someone who cannot
speak be heard; transcription relay lets someone who cannot hear follow along.
Together they make a voice net usable by people who do not use voice.

*Practical consequences:* accuracy varies, and it depends on the network
supporting the feature. `min_confidence` (default 0.55) drops the worst
guesses. Transcripts arrive **after** the message finishes, or at the one
minute mark for long ones, so it is a record rather than a live caption. If the
network does not support it, nothing happens and nothing breaks.

**Text-only channels.** `text_only: true` on a node means the bridge never
transmits audio there — it still receives everything, and everything it would
have said arrives as text.

*Why:* someone monitoring on a phone in a meeting, in a noisy vehicle, or who
simply reads more comfortably than they listen, gets the whole net silently.

**Screen reader announcements.** The panel keeps an ARIA live region that says
"M0ABC on Alpha talking" as state changes. Lamps are never the only signal —
every one has text beside it. Focus is visible everywhere, the patch matrix is
made of real buttons with `aria-pressed`, and every hole is labelled "Link
Alpha and Bravo" rather than left as an unlabelled cell.

*Why:* a matrix of coloured dots is the single most exclusionary thing this
interface could have been.

**Reduced motion.** The on-air lamp is the only thing that moves, and
`prefers-reduced-motion` turns it into a solid colour. Colour is never the only
carrier of meaning.

---

## Command line

```
masterlinker setup                    first-time setup
masterlinker run                      start the bridge and the panel
masterlinker doctor                   check keys, codec and speech engine
masterlinker status [--json]          live state of a running bridge
masterlinker link A B [--mode both]   patch two channels
masterlinker unlink A B               unpatch them
masterlinker say NODE "text"          speak into a channel
masterlinker say NODE "text" --emergency
```

`--config PATH` works on all of them, or set `MASTERLINKER_CONFIG`.

The control commands talk to a running instance over its HTTP API. If the panel
requires a password, set `MASTERLINKER_USER` and `MASTERLINKER_PASSWORD`.

```bash
MASTERLINKER_USER=admin MASTERLINKER_PASSWORD=... masterlinker status
```

```
  alpha        on-air    2 in channel  M0ABC
  bravo        ready     2 in channel  My Second Channel
  charlie      down      0 in channel  invalid password

  linked   alpha <-> bravo (both)
  unlinked bravo <-> charlie (both)
```

Useful for cron: link the club channels at 19:55 on a Sunday, unlink at 22:00.

---

## Configuration reference

Everything lives in one JSON file. See `config.example.json` for a filled-in
version. New settings are merged in on load, so upgrading never breaks an
existing config.

Anything under `node_defaults` applies to every node; the same key set on a
node overrides it. That means you can set the shush window once globally and
make one talkative channel the exception.

**Setting names end in their unit, not a plural.** The `_s` on
`hold_after_human_s` is seconds, not the end of "humans". Across the file:
`_s` seconds, `_ms` milliseconds, `_min` minutes, `_hz` hertz, `_db` decibels,
`_wpm` words per minute, `_bytes` bytes. Where a name spells the unit out in
full it means the same thing — `session_hours`, `every_minutes`. Reading
`max_hold_s` as "maximum hold, in seconds" rather than "max holds" is the
convention throughout, and it is why no setting needs a comment to say what
number it wants.

| Section | Notable keys |
|---|---|
| `web` | `host`, `port`, `require_auth`, `multi_user`, `session_hours` |
| `zello` | `ws_url`, `issuer`, `private_key_path`, `token_ttl_s`, `request_transcriptions` |
| `audio` | `tts_backend` (`auto`/`espeak-ng`/`piper`/`say`/`none`), `piper_model`, `sample_rate`, `frame_ms`, `opus_bitrate` |
| `bridge` | `max_hops`, `dedupe_window_s`, `ptt_release_grace_ms` |
| `node_defaults.tts` | `announce_link`, `announce_unlink`, `style`, `voice`, `rate_wpm`, `also_text` |
| `node_defaults.politeness` | `enabled`, `hold_after_human_s`, `coalesce`, `max_hold_s`, `text_fallback` |
| `node_defaults.guards` | `tot_stream_s`, `deadkey_*`, `kerchunk_*`, `link_idle_timeout_min` |
| `node_defaults.forwarding` | `audio`, `text`, `image`, `attribute_sender`, rate and size caps |
| `node_defaults.chat_tts` | `enabled`, `gate_s`, `prefix_sender`, `trigger` |
| `node_defaults.announcer` | `items`, `clock`, `timezone`, `location`, `units` |
| `node_defaults.morse` | `text`, `every_minutes`, `wpm`, `tone_hz`, `polite` |

The file is written `0600` because it holds Zello account passwords. Panel
passwords are PBKDF2-HMAC-SHA256 with 200,000 rounds and a per-user salt.
The panel's own API never returns stored secrets — they come back as `••••••`,
and writing that value back leaves the stored one untouched.

---

## How it works inside

**One socket per channel.** The Channel API only allows multiple channels per
connection on Zello Work; consumer Zello gets one. So each node opens its own
WebSocket. That suits a crosslink anyway: independent reconnect with jittered
exponential backoff, independent state, one channel failing does not disturb
the others.

**No transcode.** Both ends of a Zello-to-Zello bridge speak Opus, so an
inbound stream's `codec_header` and `packet_duration` are re-declared verbatim
on the outbound stream and the payloads are copied across untouched. No
generation loss, no CPU cost, no libopus on the relay path, and latency of
about one WebSocket hop. libopus is only needed for audio the bridge
synthesises itself.

**Reach, not chaining.** Targets are found by breadth-first search across
enabled edges, respecting direction and the hop limit. A→B and B→C sends A's
audio directly into both. Nothing is relayed twice, and because the source is
excluded from the search, nothing can loop.

**Tokens.** RS256 JWTs with exactly the two claims Zello uses, `iss` and `exp`,
minted from your key and cached until shortly before expiry. A long-running
bridge reconnects cleanly at three in the morning without you.

**Announcements are one per channel.** A queue that holds a single pending item
plus a short emergency lane that jumps it. Time announcements re-render at send
time, so an item held for two minutes says the right time when it finally goes
out.

---

## Things that will bite you

**Password-protected channels do not work, and this is not fixable here.**

The Zello Channel API refuses to join a password-protected consumer channel.
The server does tell you why — it returns:

```json
{"command": "on_channel_status", "channel": "...", "status": "offline",
 "users_online": 0, "images_supported": false, "texting_supported": false,
 "locations_supported": false,
 "error": "invalid password", "error_type": "configuration"}
```

Most clients read only `status` and leave you staring at "offline" with no
reason given, which is indistinguishable from an empty channel, a bad key, a
missing membership or a typo in the channel name. Masterlinker reads `error`
and `error_type` and puts them in front of you, with the fix attached.

The fix is on the channel, not in this program: remove the channel password and
control access by adding the bridge account as a member instead.

There is deliberately no channel password setting. The `logon` command's
attributes are fixed — `auth_token`, `refresh_token`, `username`, `password`,
`channels`, `listen_only`, `version`, `platform_type`, `platform_name`,
`language`, `features` — and none of them carries a channel password. A setting
for one would be a credential written to disk with nowhere to send it, and it
would imply a capability that does not exist. Early versions did collect one;
if an older config still contains it, it is deleted from the file on first load
and the removal is noted in the log and by `doctor`.

**Use a dedicated account.** The bridge account appears as the speaker for
everything it relays. Using your personal account means your name is on every
transmission from every channel.

**Bridging changes how a channel feels.** Two channels patched together is
twice the traffic and twice the people who cannot see each other's PTT.
A periodic custom announcement saying the channel is bridged, and the
`ptt_release_grace_ms` gap on release, both help. So does leaving the shush
window at its default rather than turning it down.

**Development tokens expire.** The sample token from the developer console is
good for 30 days. Masterlinker mints its own from your issuer and private key,
so this does not apply — but if you paste a sample token somewhere expecting it
to keep working, it will not.

**The panel is not hardened for the open internet.** It is a control panel for
a thing on your own network. Keep it on localhost, or behind a reverse proxy
with TLS and your own access control.

---

## Testing

```bash
python3 tests/test_integration.py
```

Runs the real client, router and web API against a stand-in Zello server that
implements logon, channel status, streams, binary audio packets and text
messages. Twenty checks, including that Opus payloads cross byte-identical,
that exactly one copy arrives on one stream, that A→B→C reaches C in a single
pass without looping, that a one-way link does not carry audio backwards, that
a dead key is held back, and that the panel never receives stored passwords.

```bash
python3 tests/shot.py
```

Boots the panel against the same mock and screenshots it, desktop and phone.

---

## Scope

This talks to Zello only. The node config carries a `platform` field and the
router does not care what a node is underneath, so another platform is a new
client class rather than a rewrite — but nothing else is implemented today, and
this README does not pretend otherwise.

Zello is a trademark of Zello Inc. This is an independent project and is not
affiliated with or endorsed by them.

## Licence

See `LICENSE`.
