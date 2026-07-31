// The config UI. Edits a config object, PUTs it, and the badge picks up the new
// revision on its next poll - so there is no "push to badge" step to get wrong.

const $ = (id) => document.getElementById(id);

let config = null;
let caps = null;
let dirty = false;

// Which field slots each page kind has, and how many.
const SHAPE = {
  dial: { one: "field", many: "readouts", max: 3, label: "Readouts" },
  bars: { one: "field", many: null, max: 0, label: "" },
  graph: { one: null, many: "fields", max: 2, label: "Series" },
  grid: { one: null, many: "fields", max: 6, label: "Values" },
  text: { one: null, many: "fields", max: 7, label: "Lines" },
};

// Theme swatches, mirroring stats/look.py so the UI shows what the badge will do.
const THEME_COLOURS = {
  dark: ["#12141c", "#38e8d1", "#7ed375", "#ec9f07", "#d71908"],
  light: ["#faf7f2", "#10919d", "#51924a", "#bc670c", "#8a0316"],
  frost: ["#f4f8fc", "#0064b9", "#008eb6", "#007d78", "#7d4b00", "#880001"],
  mono: ["#080808", "#ebebeb", "#6e6e6e", "#9a9a9a", "#cccccc", "#ffffff"],
  red: ["#1c1210", "#ff523e", "#a50000", "#ff523e", "#ffc7bc"],
  green: ["#10160f", "#02b900", "#006900", "#02b900", "#4bff39"],
  cyan: ["#0c161a", "#00a9d4", "#005f79", "#00a9d4", "#8de6ff"],
  amber: ["#0e0800", "#ffb000", "#8c5000", "#c07800", "#ffb000", "#fff0b4"],
  blueprint: ["#061022", "#5ab4ff", "#3c82dc", "#78d2ff", "#b4e4ff", "#ffffff"],
  vapor: ["#12081e", "#ff5ac8", "#5adcff", "#be82ff", "#e65ad2", "#ff50be"],
};

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error((body && body.error) || response.statusText);
  return body;
}

function toast(message, bad) {
  const node = document.createElement("div");
  node.className = "toast" + (bad ? " bad" : "");
  node.textContent = message;
  document.body.appendChild(node);
  setTimeout(() => node.remove(), 2600);
}

function markDirty() {
  dirty = true;
  $("save").disabled = false;
}

// -- field pickers ---------------------------------------------------------

function availableRefs() {
  const refs = [];
  const available = (caps && caps.available) || {};
  for (const group of Object.keys(available).sort()) {
    for (const field of available[group]) refs.push(`${group}.${field}`);
  }
  return refs;
}

/** Refs that make sense in a gauge: a number, not a name or a list. */
function numericRefs() {
  return availableRefs().filter((ref) => {
    const field = ref.split(".")[1];
    return !["name", "host", "os", "arch", "cpu_name", "iface", "cores", "load",
             "charging"].includes(field);
  });
}

function refSelect(value, refs, onChange) {
  const select = document.createElement("select");
  const options = refs.slice();
  if (value && !options.includes(value)) options.unshift(value);
  for (const ref of options) {
    const option = document.createElement("option");
    option.value = ref;
    option.textContent = ref;
    if (ref === value) option.selected = true;
    select.appendChild(option);
  }
  select.onchange = () => { onChange(select.value); markDirty(); };
  return select;
}

// -- pages -----------------------------------------------------------------

function renderPages() {
  const list = $("pages");
  list.innerHTML = "";
  config.pages.forEach((page, index) => list.appendChild(pageCard(page, index)));
  refreshPruned();
}

function pageCard(page, index) {
  const shape = SHAPE[page.kind] || SHAPE.text;
  const item = document.createElement("li");
  item.className = "page";
  item.draggable = true;

  const top = document.createElement("div");
  top.className = "top";

  const grip = document.createElement("span");
  grip.className = "grip";
  grip.textContent = "⠇";
  top.appendChild(grip);

  const kind = document.createElement("span");
  kind.className = "kind";
  kind.textContent = page.kind;
  top.appendChild(kind);

  const title = document.createElement("input");
  title.type = "text";
  title.className = "title";
  title.value = page.title || "";
  title.oninput = () => { page.title = title.value; markDirty(); };
  top.appendChild(title);

  const remove = document.createElement("button");
  remove.className = "small danger";
  remove.textContent = "✕";
  remove.title = "Remove this page";
  remove.onclick = () => {
    if (config.pages.length <= 1) return toast("Keep at least one page", true);
    config.pages.splice(index, 1);
    markDirty();
    renderPages();
  };
  top.appendChild(remove);
  item.appendChild(top);

  const fields = document.createElement("div");
  fields.className = "fields";

  if (shape.one) {
    const row = document.createElement("div");
    row.className = "fieldrow";
    const tag = document.createElement("span");
    tag.textContent = page.kind === "bars" ? "List" : "Gauge";
    row.appendChild(tag);
    const refs = page.kind === "bars"
      ? availableRefs().filter((r) => ["cpu.cores", "cpu.load"].includes(r))
      : numericRefs();
    row.appendChild(refSelect(page[shape.one], refs.length ? refs : availableRefs(),
                              (value) => { page[shape.one] = value; }));
    fields.appendChild(row);
  }

  if (shape.many) {
    const current = page[shape.many] || [];
    current.forEach((ref, slot) => {
      const row = document.createElement("div");
      row.className = "fieldrow";
      row.appendChild(refSelect(ref, numericRefs().concat(availableRefs()),
                                (value) => { current[slot] = value; }));
      const drop = document.createElement("button");
      drop.className = "small";
      drop.textContent = "−";
      drop.onclick = () => { current.splice(slot, 1); markDirty(); renderPages(); };
      row.appendChild(drop);
      fields.appendChild(row);
    });
    if (current.length < shape.max) {
      const add = document.createElement("button");
      add.className = "small";
      add.textContent = `+ ${shape.label.toLowerCase()}`;
      add.onclick = () => {
        const pool = numericRefs();
        page[shape.many] = current.concat([pool[0] || availableRefs()[0]]);
        markDirty();
        renderPages();
      };
      fields.appendChild(add);
    }
  }
  item.appendChild(fields);

  item.ondragstart = (event) => {
    item.classList.add("dragging");
    event.dataTransfer.setData("text/plain", String(index));
  };
  item.ondragend = () => item.classList.remove("dragging");
  item.ondragover = (event) => { event.preventDefault(); item.classList.add("over"); };
  item.ondragleave = () => item.classList.remove("over");
  item.ondrop = (event) => {
    event.preventDefault();
    item.classList.remove("over");
    const from = parseInt(event.dataTransfer.getData("text/plain"), 10);
    if (Number.isNaN(from) || from === index) return;
    const [moved] = config.pages.splice(from, 1);
    config.pages.splice(index, 0, moved);
    markDirty();
    renderPages();
  };
  return item;
}

function newPage(kind) {
  const shape = SHAPE[kind];
  const pool = numericRefs();
  const page = { id: `${kind}${Date.now().toString(36).slice(-4)}`, kind,
                 title: kind };
  if (shape.one) {
    page[shape.one] = kind === "bars" ? "cpu.cores" : (pool[0] || "cpu.pct");
  }
  if (shape.many) {
    page[shape.many] = pool.slice(0, Math.min(2, shape.max));
  }
  return page;
}

/** Tell the user when a page they configured will not appear on the badge. */
async function refreshPruned() {
  try {
    const preview = await api("/api/preview");
    const kept = new Set(preview.pages.map((p) => p.id));
    const dropped = config.pages.filter((p) => !kept.has(p.id)).map((p) => p.title);
    const node = $("pruned");
    if (dropped.length) {
      node.textContent = `Not shown on the badge, because this host reports no data `
        + `for them: ${dropped.join(", ")}`;
      node.classList.remove("hidden");
    } else {
      node.classList.add("hidden");
    }
  } catch (error) { /* preview is advisory */ }
}

// -- look and buttons ------------------------------------------------------

function renderLook() {
  const theme = $("theme");
  theme.innerHTML = "";
  for (const name of caps.themes) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    if (name === config.theme) option.selected = true;
    theme.appendChild(option);
  }
  theme.onchange = () => { config.theme = theme.value; markDirty(); swatches(); };
  swatches();

  bindRange("interval", "interval_ms", (v) => `${v} ms`);
  bindRange("brightness", "brightness", (v) => `${v}%`, 100);
  bindRange("points", "graph_points", (v) => `${v}`);

  const caselights = $("caselights");
  caselights.checked = !!config.caselights;
  caselights.onchange = () => { config.caselights = caselights.checked; markDirty(); };

  for (const which of ["a", "b", "c"]) {
    const select = $(`btn-${which}`);
    select.innerHTML = "";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "(nothing)";
    select.appendChild(none);
    for (const name of caps.commands) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name.replace(/_/g, " ");
      select.appendChild(option);
    }
    select.value = (config.buttons && config.buttons[which]) || "";
    select.onchange = () => {
      config.buttons = config.buttons || {};
      config.buttons[which] = select.value || null;
      markDirty();
    };
  }
}

function bindRange(id, key, format, scale) {
  const input = $(id);
  const out = $(`${id}out`);
  const factor = scale || 1;
  input.value = Math.round((config[key] || 0) * factor);
  out.textContent = format(input.value);
  input.oninput = () => {
    config[key] = factor === 1 ? parseInt(input.value, 10)
                               : parseInt(input.value, 10) / factor;
    out.textContent = format(input.value);
    markDirty();
  };
}

function swatches() {
  const node = $("swatches");
  node.innerHTML = "";
  for (const colour of THEME_COLOURS[config.theme] || []) {
    const chip = document.createElement("i");
    chip.style.background = colour;
    node.appendChild(chip);
  }
}

// -- badges ----------------------------------------------------------------

async function renderBadges() {
  const listing = await api("/api/badges");
  const node = $("badges");
  node.innerHTML = "";
  const ids = Object.keys(listing);
  if (!ids.length) {
    node.innerHTML = `<div class="hint">None paired. Use the USB installer, or `
      + `pair over the network.</div>`;
    return;
  }
  for (const id of ids) {
    const record = listing[id];
    const row = document.createElement("div");
    row.className = "badge-row";
    const name = document.createElement("span");
    name.className = "name";
    name.innerHTML = `${record.name || id}<br><code>${id}</code>`;
    row.appendChild(name);
    const forget = document.createElement("button");
    forget.className = "small danger";
    forget.textContent = "Forget";
    forget.onclick = async () => {
      await api(`/api/badges/${id}`, { method: "DELETE" });
      toast("Forgotten");
      renderBadges();
    };
    row.appendChild(forget);
    node.appendChild(row);
  }
}

// Pairing is off until asked for. Badges then ask to be let in and show a code; approving
// one here is what pairs it.
let pairingPoll = null;

async function startPairing() {
  await api("/api/pair", { method: "POST" });
  await watchPairing(true);
}

async function stopPairing() {
  await api("/api/pair", { method: "DELETE" });
  await watchPairing();
  toast("Pairing closed");
}

async function answer(requestId, approve) {
  await api(`/api/enrol/${requestId}/${approve ? "approve" : "deny"}`, { method: "POST" });
  toast(approve ? "Badge paired" : "Denied");
  renderBadges();
  watchPairing();
}

function paintPending(node, pending) {
  if (!pending.length) return;
  const list = document.createElement("div");
  list.className = "pending";
  for (const request of pending) {
    const row = document.createElement("div");
    row.className = "ask";
    const left = document.createElement("div");
    left.innerHTML = `<div class="askname">${request.name}</div>`
      + `<code>${request.badge_id}</code>`;
    const code = document.createElement("div");
    code.className = "askcode";
    code.textContent = request.code;
    const buttons = document.createElement("div");
    buttons.className = "askbuttons";
    const yes = document.createElement("button");
    yes.className = "primary small";
    yes.textContent = "Approve";
    yes.onclick = () => answer(request.request_id, true).catch((e) => toast(e.message, true));
    const no = document.createElement("button");
    no.className = "small danger";
    no.textContent = "Deny";
    no.onclick = () => answer(request.request_id, false).catch((e) => toast(e.message, true));
    buttons.append(yes, no);
    row.append(left, code, buttons);
    list.appendChild(row);
  }
  const hint = document.createElement("div");
  hint.className = "where";
  hint.textContent = "Approve the one whose code matches the badge.";
  node.append(hint, list);
}

async function watchPairing(announce) {
  if (pairingPoll) {
    clearInterval(pairingPoll);
    pairingPoll = null;
  }
  const node = $("pairing");
  const button = $("pair");

  const paint = (state, pending) => {
    if (!state.active) {
      node.classList.add("hidden");
      button.textContent = "Pair a badge\u2026";
      button.onclick = () => startPairing().catch((e) => toast(e.message, true));
      return false;
    }
    button.textContent = "Stop pairing";
    button.onclick = () => stopPairing().catch((e) => toast(e.message, true));
    node.classList.remove("hidden");
    node.innerHTML = `<div class="where">On the badge: launch Stats, press B to set up,`
      + ` and pick ${(state.hosts || []).join(" / ")}:${state.port}</div>`
      + `<div class="where countdown">closes in ${state.expires_in}s</div>`;
    paintPending(node, pending || []);
    return true;
  };

  let state = await api("/api/pair");
  let pending = (await api("/api/enrol")).pending;
  if (!paint(state, pending)) return;
  if (announce) toast(`Pairing open for ${state.expires_in}s`);

  pairingPoll = setInterval(async () => {
    try {
      state = await api("/api/pair");
      pending = (await api("/api/enrol")).pending;
      if (!paint(state, pending)) {
        clearInterval(pairingPoll);
        pairingPoll = null;
      }
    } catch (error) {
      clearInterval(pairingPoll);
      pairingPoll = null;
    }
  }, 1000);
}

// -- live ------------------------------------------------------------------

const PERCENT = ["pct", "swap_pct", "mem_pct", "fan_pct", "battery_pct"];

async function renderLive() {
  let frame;
  try {
    frame = await api("/api/stats");
  } catch (error) { return; }
  const node = $("live");
  node.innerHTML = "";
  for (const group of Object.keys(frame)) {
    if (["v", "t", "seq", "layout_rev"].includes(group)) continue;
    const items = Array.isArray(frame[group]) ? frame[group] : [frame[group]];
    for (const [i, item] of items.entries()) {
      if (!item || !Object.keys(item).length) continue;
      const box = document.createElement("div");
      box.className = "group";
      const heading = document.createElement("h3");
      heading.textContent = items.length > 1 ? `${group} ${i}` : group;
      box.appendChild(heading);
      for (const key of Object.keys(item)) {
        const value = item[key];
        const row = document.createElement("div");
        row.className = "kv";
        const label = document.createElement("span");
        label.textContent = key;
        const shown = document.createElement("span");
        if (value === null || value === undefined) {
          shown.className = "none";
          shown.textContent = "unknown";
        } else if (Array.isArray(value)) {
          shown.textContent = value.map((v) => Math.round(v)).join(" ");
        } else {
          shown.textContent = typeof value === "number"
            ? (Number.isInteger(value) ? value : value.toFixed(1)) : String(value);
        }
        row.append(label, shown);
        box.appendChild(row);
        if (PERCENT.includes(key) && typeof value === "number") {
          const bar = document.createElement("div");
          bar.className = "bar";
          const fill = document.createElement("i");
          fill.style.width = `${Math.max(0, Math.min(100, value))}%`;
          bar.appendChild(fill);
          box.appendChild(bar);
        }
      }
      node.appendChild(box);
    }
  }
}

function renderSources() {
  const node = $("sources");
  node.innerHTML = "";
  for (const source of caps.sources) {
    const row = document.createElement("div");
    row.className = source.faults ? "faulty" : "";
    row.textContent = source.faults
      ? `${source.name}: ${source.last_fault}`
      : `${source.name} → ${source.provides.join(", ")}`;
    node.appendChild(row);
  }
  const groups = Object.keys(caps.available || {}).join(", ");
  const summary = document.createElement("div");
  summary.style.marginTop = "6px";
  summary.textContent = `reporting: ${groups || "nothing yet"}`;
  node.appendChild(summary);
}

// -- boot ------------------------------------------------------------------

async function save() {
  try {
    const result = await api("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
    config.rev = result.rev;
    dirty = false;
    $("save").disabled = true;
    toast(`Saved. The badge will pick up revision ${result.rev}.`);
    refreshPruned();
  } catch (error) {
    toast(error.message, true);
  }
}

async function boot() {
  try {
    [config, caps] = await Promise.all([api("/api/config"), api("/api/capabilities")]);
  } catch (error) {
    document.body.innerHTML = `<p style="padding:20px">Cannot reach the server: `
      + `${error.message}</p>`;
    return;
  }
  const sys = (await api("/api/stats")).sys || {};
  $("hostline").textContent =
    `${sys.host || "host"} · ${sys.os || ""} · ${sys.cpu_name || ""}`;

  renderPages();
  renderLook();
  renderSources();
  renderBadges();
  renderLive();

  $("save").onclick = save;
  $("add").onclick = () => {
    config.pages.push(newPage($("addkind").value));
    markDirty();
    renderPages();
  };
  // Picks up a window opened by `statsbadge pair` or a previous page load.
  watchPairing().catch(() => {});

  setInterval(renderLive, 1000);
  window.onbeforeunload = () => (dirty ? "You have unsaved changes." : undefined);
}

boot();
