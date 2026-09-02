/* Masterlinker control panel.
   One WebSocket carries state and log lines; everything else is a small POST. */

const $ = (id) => document.getElementById(id);

let state = { nodes: [], configured_nodes: [], links: [], presets: [] };
let session = {};
let socket = null;
let editing = null;
let announced = new Set();

/* ---------- boot ---------- */

async function boot() {
  session = await (await fetch("/api/session")).json();
  document.title = session.app_name || "Masterlinker";
  $("wordmark").textContent = session.app_name || "Masterlinker";
  $("signin-title").textContent = session.app_name || "Masterlinker";

  if (session.require_auth && !session.signed_in) {
    $("signin").hidden = false;
    return;
  }
  $("signin").hidden = true;
  $("app").hidden = false;
  $("signout").hidden = !session.require_auth;
  connect();
}

$("signin-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const response = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: form.get("username"),
      password: form.get("password"),
    }),
  });
  if (response.ok) {
    location.reload();
  } else {
    const body = await response.json().catch(() => ({}));
    $("signin-error").textContent = body.error || "Sign in failed.";
  }
});

$("signout").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  location.reload();
});

/* ---------- live feed ---------- */

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${scheme}://${location.host}/api/events`);
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "state") {
      state = message.state;
      render();
    } else if (message.type === "log") {
      appendLog(message.event);
    }
  };
  socket.onclose = () => setTimeout(connect, 3000);
}

/* ---------- rendering ---------- */

function nodeById(id) {
  return state.nodes.find((n) => n.id === id);
}

function label(id) {
  const configured = state.configured_nodes.find((n) => n.id === id);
  return configured ? configured.name || configured.id : id;
}

function lampState(node) {
  if (!node) return "off";
  if (node.error) return "fault";
  if (node.talking.length || node.transmitting) return "talking";
  if (node.connected && node.status === "online") return "online";
  return "off";
}

function render() {
  renderEngine();
  renderStrips();
  renderMatrix();
  renderLinkList();
  renderPresets();
  announceTalkers();
}

function renderEngine() {
  const audio = state.audio || {};
  const pill = $("engine-pill");
  const codec = audio.opus ? "Codec ready" : "No libopus";
  const speech =
    audio.tts_backend === "none"
      ? "no speech engine"
      : `speech via ${audio.tts_backend}`;
  pill.textContent = `${codec}, ${speech}`;
  pill.dataset.fault = !audio.opus || audio.tts_backend === "none" ? "1" : "0";
  pill.title = audio.opus_error || "Announcements need both; relaying voice needs neither.";
}

function renderStrips() {
  const list = $("strips");
  list.textContent = "";
  const configured = state.configured_nodes || [];
  $("nodes-empty").hidden = configured.length > 0;

  for (const entry of configured) {
    const node = nodeById(entry.id);
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.className = "strip";
    button.type = "button";
    button.addEventListener("click", () => openNodeSheet(entry.id));

    const lamp = document.createElement("span");
    lamp.className = "lamp";
    lamp.dataset.state = lampState(node);

    const body = document.createElement("span");
    const name = document.createElement("span");
    name.className = "strip-name";
    name.textContent = entry.name || entry.id;
    const sub = document.createElement("span");
    sub.className = "strip-sub";
    if (!node) {
      sub.textContent = "not connected";
    } else if (node.error) {
      sub.textContent = node.error;
      sub.dataset.fault = "1";
    } else if (node.talking.length) {
      sub.textContent = `${node.talking.join(", ")} talking`;
    } else if (node.transmitting) {
      sub.textContent = "transmitting";
    } else if (node.connected) {
      sub.textContent = entry.channel;
    } else {
      sub.textContent = "connecting…";
    }
    body.append(name, sub);

    const count = document.createElement("span");
    count.className = "strip-count";
    count.textContent = node && node.connected ? `${node.users_online}` : "—";
    count.title = "People in the channel";

    button.append(lamp, body, count);
    item.append(button);
    list.append(item);
  }
}

function linkBetween(a, b) {
  return (state.links || []).find(
    (l) => (l.a === a && l.b === b) || (l.a === b && l.b === a)
  );
}

function renderMatrix() {
  const table = $("matrix");
  table.textContent = "";
  const ids = (state.configured_nodes || []).map((n) => n.id);
  if (!ids.length) return;

  const head = table.createTHead().insertRow();
  head.append(document.createElement("th"));
  for (const id of ids) {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = id;
    head.append(cell);
  }

  const body = table.createTBody();
  for (const rowId of ids) {
    const row = body.insertRow();
    const header = document.createElement("th");
    header.scope = "row";
    header.textContent = rowId;
    row.append(header);

    for (const colId of ids) {
      const cell = row.insertCell();
      const hole = document.createElement("button");
      hole.type = "button";
      hole.className = "hole";
      if (rowId === colId) {
        hole.dataset.self = "1";
        hole.disabled = true;
        hole.setAttribute("aria-label", `${rowId}, itself`);
      } else {
        const link = linkBetween(rowId, colId);
        const on = Boolean(link && link.enabled);
        hole.setAttribute("aria-pressed", String(on));
        hole.setAttribute(
          "aria-label",
          `${on ? "Unlink" : "Link"} ${label(rowId)} and ${label(colId)}`
        );
        const source = nodeById(rowId);
        if (on && source && source.talking.length) hole.dataset.flowing = "1";
        hole.addEventListener("click", () => toggleLink(rowId, colId));
      }
      cell.append(hole);
    }
  }
}

function renderLinkList() {
  const list = $("link-list");
  list.textContent = "";
  const ids = (state.configured_nodes || []).map((n) => n.id);
  const pairs = [];
  for (let i = 0; i < ids.length; i += 1) {
    for (let j = i + 1; j < ids.length; j += 1) {
      const [a, b] = [ids[i], ids[j]];
      const link = linkBetween(a, b);
      pairs.push({ a, b, on: Boolean(link && link.enabled) });
    }
  }
  // Patched pairs first: on a phone the thing you came to switch off is the
  // thing you want at the top, not buried in n-squared rows.
  pairs.sort((x, y) => Number(y.on) - Number(x.on));

  for (const pair of pairs) {
    const row = document.createElement("li");
    row.className = "link-row";
    const label = document.createElement("span");
    label.className = "link-pair";
    label.textContent = `${pair.a} ↔ ${pair.b}`;
    const button = document.createElement("button");
    button.className = pair.on ? "btn btn-primary btn-small" : "btn btn-small";
    button.textContent = pair.on ? "Unlink" : "Link";
    button.addEventListener("click", () => toggleLink(pair.a, pair.b));
    row.append(label, button);
    list.append(row);
  }
}

function renderPresets() {
  const select = $("preset-select");
  const current = select.value;
  select.textContent = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "Presets";
  select.append(blank);
  for (const name of state.presets || []) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    select.append(option);
  }
  select.value = current;
}

/* ---------- announcements for screen readers ---------- */

function announceTalkers() {
  const talking = (state.nodes || [])
    .filter((n) => n.talking.length)
    .map((n) => `${n.talking.join(" and ")} on ${label(n.id)}`);
  const key = talking.join("|");
  if (announced.has(key)) return;
  announced = new Set([key]);
  $("live-region").textContent = talking.length ? `${talking.join("; ")} talking` : "";
}

/* ---------- log ---------- */

function appendLog(event) {
  const log = $("log");
  const item = document.createElement("li");
  item.dataset.level = event.level;
  const time = document.createElement("time");
  const stamp = new Date(event.at * 1000);
  time.dateTime = stamp.toISOString();
  time.textContent = stamp.toLocaleTimeString([], { hour12: false });
  const text = document.createElement("span");
  text.className = "log-text";
  text.textContent = event.message;
  item.append(time, text);
  log.append(item);
  while (log.children.length > 300) log.firstChild.remove();
  if ($("autoscroll").checked) log.scrollTop = log.scrollHeight;
}

/* ---------- actions ---------- */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error || `Request failed (${response.status})`);
  }
  return response.json();
}

async function toggleLink(a, b) {
  try {
    await api("/api/links/toggle", {
      method: "POST",
      body: JSON.stringify({ a, b }),
    });
  } catch (error) {
    appendLog({ at: Date.now() / 1000, level: "error", message: error.message });
  }
}

$("preset-select").addEventListener("change", async (event) => {
  const name = event.target.value;
  if (!name) return;
  await api(`/api/presets/${encodeURIComponent(name)}/apply`, { method: "POST" });
});

$("save-preset").addEventListener("click", async () => {
  const name = prompt("Name this layout");
  if (!name) return;
  await api("/api/presets", { method: "POST", body: JSON.stringify({ name }) });
});

/* ---------- node sheet ---------- */

function openNodeSheet(nodeId) {
  editing = nodeId || null;
  const sheet = $("node-sheet");
  const form = $("node-form");
  form.reset();
  $("node-error").textContent = "";
  $("node-sheet-title").textContent = nodeId ? `Edit ${nodeId}` : "Add a channel";
  $("delete-node").hidden = !nodeId;
  form.elements.id.readOnly = Boolean(nodeId);

  if (nodeId) {
    const configured = state.configured_nodes.find((n) => n.id === nodeId) || {};
    form.elements.id.value = configured.id || "";
    form.elements.name.value = configured.name || "";
    form.elements.channel.value = configured.channel || "";
    form.elements.nickname.value = configured.nickname || "";
    const node = nodeById(nodeId);
    if (node) {
      form.elements.listen_only.checked = node.listen_only;
      form.elements.text_only.checked = node.text_only;
    }
  }
  sheet.showModal();
}

$("add-node").addEventListener("click", () => openNodeSheet(null));

$("node-form").addEventListener("submit", async (event) => {
  if (event.submitter && event.submitter.value === "cancel") return;
  event.preventDefault();
  const form = new FormData(event.target);
  const payload = {
    id: form.get("id"),
    name: form.get("name"),
    channel: form.get("channel"),
    nickname: form.get("nickname"),
    listen_only: form.get("listen_only") === "on",
    text_only: form.get("text_only") === "on",
  };
  if (form.get("username")) payload.username = form.get("username");
  if (form.get("password")) payload.password = form.get("password");

  try {
    if (editing) {
      await api(`/api/nodes/${encodeURIComponent(editing)}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
    } else {
      await api("/api/nodes", { method: "POST", body: JSON.stringify(payload) });
    }
    $("node-sheet").close();
  } catch (error) {
    $("node-error").textContent = error.message;
  }
});

$("delete-node").addEventListener("click", async () => {
  if (!editing) return;
  if (!confirm(`Remove ${editing} and every link to it?`)) return;
  await api(`/api/nodes/${encodeURIComponent(editing)}`, { method: "DELETE" });
  $("node-sheet").close();
});

/* ---------- alerts ---------- */

$("open-alert").addEventListener("click", () => $("alert-sheet").showModal());

$("alert-form").addEventListener("submit", async (event) => {
  if (event.submitter && event.submitter.value !== "send") return;
  event.preventDefault();
  const text = new FormData(event.target).get("text");
  try {
    await api("/api/emergency", { method: "POST", body: JSON.stringify({ text }) });
    $("alert-sheet").close();
  } catch (error) {
    appendLog({ at: Date.now() / 1000, level: "error", message: error.message });
  }
});

boot();
