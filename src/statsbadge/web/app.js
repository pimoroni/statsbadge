// The config UI. Edits a config object, PUTs it, and the badge picks up the new
// revision on its next poll - so there is no "push to badge" step to get wrong.
//
// Everything on the page belongs to one badge, chosen in the header: `whose` is its id, or
// null for the layout a badge draws before it has been given one of its own.

const $ = (id) => document.getElementById(id);

let config = null;
let caps = null;
let dirty = false;
let whose = null;
let badges = {};

// Which field slots each page kind has, and how many.
const SHAPE = {
  dial: { one: "field", many: "readouts", max: 3, label: "Readouts",
          pool: "gauge", manyPool: "any" },
  dials: { one: null, many: "fields", max: 4, label: "Gauges", manyPool: "gauge" },
  bars: { one: "field", many: null, max: 0, label: "", pool: "list" },
  graph: { one: null, many: "fields", max: 2, label: "Series", manyPool: "series" },
  grid: { one: null, many: "fields", max: 6, label: "Values", manyPool: "any" },
  text: { one: null, many: "fields", max: 7, label: "Lines", manyPool: "any" },
  rings: { one: null, many: "fields", max: 4, label: "Rings", manyPool: "gauge" },
  spark: { one: null, many: "fields", max: 6, label: "Rows", manyPool: "series" },
  radar: { one: null, many: "fields", max: 6, label: "Axes", manyPool: "gauge" },
  trend: { one: "field", many: null, max: 0, label: "", pool: "series" },
  waterfall: { one: "field", many: null, max: 0, label: "", pool: "list" },
  // The badge's own vitals: no fields at all, since none of it comes from the host.
  badge: { one: null, many: null, max: 0, label: "" },
};

// The palettes live on the host, and so does the arithmetic that derives a tinted one.

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error((body && body.error) || response.statusText);
  return body;
}

// Words that stay lowercase inside a title, the first one never being one of them. Dropdown
// options are labels rather than sentences, so they are cased like labels throughout.
const MINOR = new Set(["a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on",
                       "or", "the", "to", "with"]);

/** Title case for a name that arrives as a word or a slug. What an extension declared is left
 * as it declared it: those are its own strings, and "kmh" is not "Kmh". */
function title(text) {
  return String(text).replace(/[-_]+/g, " ").trim().split(/\s+/)
    .map((word, index) => (index && MINOR.has(word.toLowerCase())
      ? word.toLowerCase()
      : word.charAt(0).toUpperCase() + word.slice(1)))
    .join(" ");
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

/** Numbers first, then everything else, each of them once.
 *
 * A slot that can take any reading still wants the numbers at the top, since that is what
 * nearly every page is made of. Concatenating the two lists was the obvious way to say
 * that and the wrong one: the numeric refs are a subset of all of them, so every number
 * appeared twice, once qualified by its group and once again below it.
 */
function preferredRefs() {
  // Lists are left out of even this: fmt has nothing to do with one but print it, so a
  // grid cell handed cpu.cores shows a row of Python.
  const printable = availableRefs().filter(
    (ref) => !listFields().includes(ref.split(".")[1]));
  return [...new Set(numericRefs().concat(printable))];
}

/** Refs that are a number at all: not a name, a flag or a list. */
function numericRefs() {
  return availableRefs().filter((ref) => {
    const field = ref.split(".")[1];
    return !["name", "host", "os", "arch", "cpu_name", "iface", "charging"]
      .includes(field) && !listFields().includes(field);
  });
}

function listFields() {
  return caps.list_fields || ["cores", "load"];
}

/** Refs a gauge can place a needle on: a percentage, or something with a top end.
 *
 * Being a number is not enough. Uptime is a number and a ring drawn from it is empty
 * whatever the machine has been doing, because nothing says what a full one would be.
 */
function gaugeRefs() {
  const percent = caps.percent_fields || [];
  const scaled = Object.keys(caps.full_scale || {});
  return numericRefs().filter((ref) => {
    const field = ref.split(".")[1];
    return percent.includes(field) || scaled.includes(field);
  });
}

/** Refs the host keeps a history ring for, which is what a graph needs to say anything.
 *
 * Without one the page plots the live value twice and draws a flat line, which looks like
 * a reading that never changes rather than one nobody is recording.
 */
function seriesRefs() {
  const kept = caps.graphed || [];
  const withHistory = numericRefs().filter((ref) => kept.includes(ref));
  return withHistory.length ? withHistory : numericRefs();
}

/** Refs that are a list, for the kinds that draw one lane or bar per element. */
function listRefs() {
  const lists = availableRefs().filter(
    (ref) => listFields().includes(ref.split(".")[1]));
  // A waterfall wants a list per sample; the collector keeps rings for some of them.
  return lists;
}

// Which pool each slot draws from. "gauge" needs a top end, "series" only needs to be a
// number since it scales itself from the data, "list" wants one value per element, and
// "any" prints whatever it is given.
const POOLS = {
  gauge: gaugeRefs,
  series: seriesRefs,
  list: listRefs,
  any: preferredRefs,
};

/** What to call a group, and one of its fields, in a picker. */
function groupLabel(group) {
  return (caps.group_labels || {})[group] || group;
}

function fieldLabel(ref) {
  const [group, field] = ref.split(".");
  const labels = (caps.field_labels || {})[group] || {};
  // The host's own label where there is one - "Used %" is cased the way it wants - and the
  // field name titled where there is not.
  return labels[field] || title(field);
}

/** One dropdown, grouped by category, so a field can be found rather than hunted for.
 *
 * With the group named beside it: a collapsed select shows only the chosen option's own
 * text, so the optgroup that made the list navigable disappears exactly when it is needed,
 * leaving "Used %" with nothing to say what it is used of. Returns both, as a fragment, so
 * the row stays flat and its flex layout still applies to the select.
 */
function refSelect(value, refs, onChange) {
  const holder = document.createDocumentFragment();
  const groupName = document.createElement("span");
  groupName.className = "group";
  const select = document.createElement("select");
  // Deduplicated here as well as by the caller: one option per reading is the whole
  // point of a picker, and a repeat is impossible to tell apart once it is on screen.
  const options = [...new Set(refs)];
  if (value && !options.includes(value)) options.unshift(value);

  const byGroup = new Map();
  for (const ref of options) {
    const group = ref.split(".")[0];
    if (!byGroup.has(group)) byGroup.set(group, []);
    byGroup.get(group).push(ref);
  }
  for (const [group, groupRefs] of byGroup) {
    const holder = document.createElement("optgroup");
    holder.label = groupLabel(group);
    for (const ref of groupRefs) {
      const option = document.createElement("option");
      option.value = ref;
      option.textContent = fieldLabel(ref);
      if (ref === value) option.selected = true;
      holder.appendChild(option);
    }
    select.appendChild(holder);
  }
  // Told which ref rather than reading it back off the select, so the first paint does
  // not depend on `value` already reflecting the option marked selected.
  const showGroup = (ref) => {
    groupName.textContent = groupLabel(String(ref || "").split(".")[0]);
  };
  select.onchange = () => { showGroup(select.value); onChange(select.value); markDirty(); };
  showGroup(value || options[0]);
  holder.appendChild(groupName);
  holder.appendChild(select);
  return holder;
}

// -- pages -----------------------------------------------------------------

// Which cards are open. Collapsed by default so the list reads as an overview and stays
// short enough to drag around; opening one is how you get at its fields.
const expanded = new Set();

function renderPages() {
  const list = $("pages");
  list.innerHTML = "";
  config.pages.forEach((page, index) => list.appendChild(pageCard(page, index)));
  refreshPruned();
}

/** The field slots a kind has. An extension's page declares its own, because only its
 * renderer knows whether it reads `fields` at all - the clock face draws from its groups
 * and ignores them, so offering seven pickers was offering seven controls that did
 * nothing. */
function shapeFor(kind) {
  if (SHAPE[kind]) return SHAPE[kind];
  const declared = (caps.extension_pages || []).find((page) => page.kind === kind);
  const slots = (declared && declared.slots) || {};
  return { one: slots.one || null, many: slots.many || null,
           max: slots.max || 0, label: slots.label || "Values" };
}

/** A slot label in the singular. The labels name the whole set of slots a kind has - Rows,
 * Series, Axes - and the button under them adds one. */
function singular(label) {
  if (label === "Series") return label;
  if (label === "Axes") return "Axis";
  return label.endsWith("s") ? label.slice(0, -1) : label;
}

function pageCard(page, index) {
  const shape = shapeFor(page.kind);
  const item = document.createElement("li");
  item.className = "page";
  item.draggable = true;

  const top = document.createElement("div");
  top.className = "top";

  const grip = document.createElement("span");
  grip.className = "grip";
  grip.textContent = "⠇";
  top.appendChild(grip);

  const open = expanded.has(page.id);
  const toggle = document.createElement("button");
  toggle.className = "small twist";
  toggle.textContent = open ? "▾" : "▸";
  toggle.title = open ? "Collapse" : "Configure";
  toggle.onclick = () => {
    if (open) expanded.delete(page.id); else expanded.add(page.id);
    renderPages();               // not markDirty: opening a card changes nothing
  };
  top.appendChild(toggle);

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

  if (open && shape.one) {
    const row = document.createElement("div");
    row.className = "fieldrow";
    const tag = document.createElement("span");
    tag.textContent = page.kind === "bars" ? "List" : "Gauge";
    row.appendChild(tag);
    const pool = (POOLS[shape.pool] || POOLS.any)();
    row.appendChild(refSelect(page[shape.one], pool.length ? pool : availableRefs(),
                              (value) => { page[shape.one] = value; }));
    fields.appendChild(row);
  }

  if (open && shape.many) {
    const current = page[shape.many] || [];
    current.forEach((ref, slot) => {
      const row = document.createElement("div");
      row.className = "fieldrow";
      const pool = (POOLS[shape.manyPool] || POOLS.any)();
      row.appendChild(refSelect(ref, pool.length ? pool : availableRefs(),
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
      // What the slot is called, in the singular: the button adds one of them.
      add.textContent = `Add ${singular(shape.label).toLowerCase()}`;
      add.onclick = () => {
        const pool = (POOLS[shape.manyPool] || POOLS.any)();
        page[shape.many] = current.concat([pool[0] || availableRefs()[0]]);
        markDirty();
        renderPages();
      };
      fields.appendChild(add);
    }
  }
  // What this page in particular can be told, as against what the extension is told
  // once for every page: a place here, units there.
  for (const setting of open
       ? (caps.extension_page_settings || {})[page.kind] || [] : []) {
    const row = document.createElement("div");
    row.className = "fieldrow pagesetting";
    row.appendChild(settingRow(page, setting));
    fields.appendChild(row);
  }
  if (open) {
    item.appendChild(fields);
  } else {
    const summary = document.createElement("div");
    summary.className = "summary";
    const refs = shape.one ? [page[shape.one]] : (page[shape.many] || []);
    const named = refs.filter(Boolean).map((ref) => fieldLabel(ref));
    const extra = ((caps.extension_page_settings || {})[page.kind] || [])
      .map((setting) => page[setting.key])
      .filter(Boolean);
    summary.textContent = named.concat(extra).join(", ") || "nothing chosen";
    item.appendChild(summary);
  }

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
  const suffix = Date.now().toString(36).slice(-4);
  const offered = (caps.extension_pages || []).find((p) => p.kind === kind);
  if (offered) {
    // An extension knows its own page: take the fields and title it shipped with,
    // since only its badge module knows what shape they go in.
    return { ...offered, id: `${offered.id || kind}${suffix}` };
  }
  const shape = SHAPE[kind];
  const pool = numericRefs();
  const page = { id: `${kind}${suffix}`, kind, title: kind };
  if (shape.one) {
    page[shape.one] = kind === "bars" ? "cpu.cores" : (pool[0] || "cpu.pct");
  }
  if (shape.many) {
    page[shape.many] = pool.slice(0, Math.min(2, shape.max));
  }
  return page;
}

/** Add the installed extensions' pages to the kind picker, which only lists the built-ins. */
function offerExtensionPages() {
  const picker = $("addkind");
  for (const page of caps.extension_pages || []) {
    if ([...picker.options].some((option) => option.value === page.kind)) continue;
    const option = document.createElement("option");
    option.value = page.kind;
    option.textContent = page.title || page.kind;
    picker.appendChild(option);
  }
}

/** Tell the user when a page they configured will not appear on the badge. */
async function refreshPruned() {
  try {
    const preview = await api(configPath("/api/preview"));
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

// -- extension settings ----------------------------------------------------

/** A box per installed extension, titled with its name, holding whatever it can be told.
 *
 * Every discovered one, not only those with settings: an extension that asks to be told
 * nothing had nothing in the UI at all, and one that failed to import was invisible until a
 * page it was meant to draw did not appear. */
function renderSettings() {
  const node = $("settings");
  node.innerHTML = "";
  const schema = caps.extension_settings || {};
  const installed = caps.extensions || [];
  if (!installed.length) {
    node.innerHTML = '<section class="col"><p class="hint">None installed. '
      + '<code>pip install</code> one, then <code>statsbadge install</code>.</p></section>';
    return;
  }
  config.settings = config.settings || {};
  for (const extension of installed) {
    node.appendChild(extensionBox(extension, schema[extension.name] || []));
  }
}

function extensionBox(extension, settings) {
  const box = document.createElement("section");
  box.className = "col";
  const heading = document.createElement("h3");
  heading.textContent = extension.name;
  box.appendChild(heading);

  const state = document.createElement("p");
  state.className = "hint";
  if (extension.error) {
    state.className = "hint bad";
    state.textContent = extension.error;
  } else if (extension.available === false) {
    state.textContent = "Installed, but not usable on this host.";
  } else {
    const parts = [];
    if (extension.version) parts.push(extension.version);
    if (extension.provides.length) parts.push(extension.provides.join(", "));
    if (extension.badge_module) parts.push("draws its own page");
    state.textContent = parts.join(" · ");
  }
  box.appendChild(state);

  if (!settings.length) return box;
  config.settings[extension.name] = config.settings[extension.name] || {};
  const stored = config.settings[extension.name];
  for (const setting of settings) {
    box.appendChild(settingRow(stored, setting));
    if (setting.hint) {
      const hint = document.createElement("p");
      hint.className = "hint";
      hint.textContent = setting.hint;
      box.appendChild(hint);
    }
  }
  return box;
}

function settingRow(stored, setting) {
  const label = document.createElement("label");
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = setting.label || setting.key;
  label.appendChild(name);
  const current = stored[setting.key] !== undefined
    ? stored[setting.key] : setting.default;

  let input;
  if (setting.type === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!current;
    input.onchange = () => { stored[setting.key] = input.checked; markDirty(); };
  } else if (setting.type === "choice") {
    input = document.createElement("select");
    for (const option of setting.options || []) {
      const node = document.createElement("option");
      node.value = option;
      node.textContent = option;
      if (option === current) node.selected = true;
      input.appendChild(node);
    }
    input.onchange = () => { stored[setting.key] = input.value; markDirty(); };
  } else {
    input = document.createElement("input");
    input.type = "text";
    input.value = current === null || current === undefined ? "" : current;
    // Empty means unset, which is not the same as zero: a latitude of 0 is the equator.
    input.oninput = () => {
      stored[setting.key] = input.value === "" ? null : input.value;
      markDirty();
    };
  }
  label.appendChild(input);
  // A statement with a checkbox is one row of a form like any other: the name to the left, the
  // control on the same edge everything else sits on.
  if (setting.type === "bool") label.className = "check";
  return label;
}


// -- look and buttons ------------------------------------------------------

function renderLook() {
  const theme = $("theme");
  theme.innerHTML = "";
  // Grouped by mode: which of them suit a lit room is the first thing anybody is choosing
  // between, and a flat list of twenty had the pairs scattered through it.
  for (const [mode, heading] of [["dark", "Dark"], ["light", "Light"]]) {
    const group = document.createElement("optgroup");
    group.label = heading;
    for (const record of caps.themes.filter((entry) => entry.mode === mode)) {
      const option = document.createElement("option");
      option.value = record.name;
      option.textContent = record.label || title(record.name);
      if (record.name === config.theme) option.selected = true;
      group.appendChild(option);
    }
    if (group.children.length) theme.appendChild(group);
  }
  theme.onchange = () => { config.theme = theme.value; markDirty(); renderTint(); };
  renderTint();

  bindRange("interval", "interval_ms", (v) => `${v} ms`);
  bindRange("brightness", "brightness", (v) => `${v}%`, 100);
  bindRange("points", "graph_points", (v) => `${v}`);
  // Zero is off, so the readout says so rather than showing a time nothing happens at.
  bindRange("idle", "idle_advance_s", (v) => (v ? `${v}s idle` : "off"));
  bindRange("advance", "advance_every_s", (v) => `${v}s`);

  // Whether a plot is a curve through its samples or a polyline between them. One choice for
  // every graph, since it is a drawing choice and not a property of a page. Stored as a flag,
  // offered as the two things it looks like.
  const smooth = $("smooth");
  smooth.value = config.smooth === false ? "straight" : "curved";
  smooth.onchange = () => { config.smooth = smooth.value === "curved"; markDirty(); };

  // Whether a gauge eases to each new reading or steps to it. Off by default: a reading
  // arrives once a second, and on a field that swings between polls - a throughput, say -
  // the sweep reads as lag rather than as motion.
  const animate = $("animate");
  animate.checked = !!config.animate;
  animate.onchange = () => { config.animate = animate.checked; markDirty(); };

  // How a page turn moves: not at all, the next page over this one, or both together like a
  // card off a deck. Immediate by default - it is a fifth of a second before what you
  // pressed for can be read.
  const slide = $("slide");
  slide.value = typeof config.slide === "string" ? config.slide
    : (config.slide ? "over" : "off");
  slide.onchange = () => { config.slide = slide.value; markDirty(); };

  const plotanim = $("plotanim");
  plotanim.checked = !!config.plot_animation;
  plotanim.onchange = () => { config.plot_animation = plotanim.checked; markDirty(); };

  const rows = $("rows");
  rows.value = config.rows || "zebra";
  rows.onchange = () => { config.rows = rows.value; markDirty(); };

  // How the dial page's gauge fills: the ramp's colour for the reading, or the whole ramp
  // swept round the arc with what the reading has not reached left faint.
  const gaugefill = $("gaugefill");
  gaugefill.value = config.gauge_fill || "solid";
  gaugefill.onchange = () => {
    config.gauge_fill = gaugefill.value;
    markDirty();
    preview();                          // the preview draws the gauge the way the badge will
  };

  // The badge's own light sensor, taking the brightness above down to suit a dim room.
  const autobright = $("autobright");
  autobright.checked = !!config.auto_brightness;
  autobright.onchange = () => {
    config.auto_brightness = autobright.checked;
    markDirty();
  };

  // Off, the theme's own level, or a reading for the lights to follow. The stored value
  // is false, true, or a field ref, so the option values carry it directly.
  const caselights = $("caselights");
  caselights.innerHTML = "";
  const options = [["off", "Off"], ["theme", "Follow the Theme"]];
  for (const ref of numericRefs()) {
    options.push([ref, `${groupLabel(ref.split(".")[0])} - ${fieldLabel(ref)}`]);
  }
  const current = config.caselights === true ? "theme"
                : config.caselights ? config.caselights : "off";
  // A reading this host has stopped sending still has to be selectable, or opening the
  // page and saving it would quietly turn the lights off.
  if (!options.some(([value]) => value === current)) {
    options.push([current, current]);
  }
  for (const [value, text] of options) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = text;
    if (value === current) option.selected = true;
    caselights.appendChild(option);
  }
  caselights.onchange = () => {
    const value = caselights.value;
    config.caselights = value === "off" ? false : value === "theme" ? true : value;
    markDirty();
  };

  for (const which of ["a", "b", "c"]) {
    const select = $(`btn-${which}`);
    select.innerHTML = "";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "Nothing";
    select.appendChild(none);
    // The badge's own first, being the ones that need no host at all.
    for (const local of caps.local_actions || []) {
      const option = document.createElement("option");
      option.value = local.action;
      option.textContent = `${title(local.label)} (on the badge)`;
      select.appendChild(option);
    }
    for (const name of caps.commands) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = title(name);
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

// -- the theme preview ----------------------------------------------------
//
// The palette comes from the host, for every theme and not only the tinted ones: it is derived
// there for those, so what the preview shows and what reaches the badge cannot drift apart, and
// the browser needs no colour arithmetic of its own.

// Which preview request is the current one. Clicking along the swatches starts several, and
// they can come back in any order: without this the last *reply* wins rather than the last
// click, and the panel ends up showing a colour nobody chose.
let previewWanted = 0;

/** Which family of accents the picker is showing. Follows the stored one when the panel opens,
 * so what is on screen is the row the chosen colour came from. */
let family = null;

function familyOf(accent) {
  const stored = String(accent);
  for (const [name, list] of Object.entries(caps.accents || {})) {
    if (list.some((offer) => String(offer) === stored)) return name;
  }
  return caps.accent_family || "normal";
}

function renderTint() {
  const tinted = (caps.tinted || {})[config.theme];
  const accents = $("accents");
  accents.classList.toggle("hidden", !tinted);
  const hint = $("tinthint");
  hint.classList.toggle("hidden", !tinted);
  // How the second accent is picked, which only a derived palette works out: a written-down
  // one either names its own or has none.
  $("secondrow").classList.toggle("hidden", !tinted);
  const second = $("accentb");
  second.value = config.accent_b || "same";
  second.onchange = () => { config.accent_b = second.value; markDirty(); renderTint(); };
  // What the ramp does is the difference between the two pairs, so the hint says which.
  hint.textContent = (caps.bold || []).includes(config.theme)
    ? "The rest of the palette is worked out from this colour, and the ramp stays in its hue, "
      + "sweeping from a dark version through it to a pale one."
    : "The rest of the palette is worked out from this colour, and the ramp travels to red "
      + "unless the colour is already there.";
  accents.innerHTML = "";
  if (tinted) {
    if (!family) family = familyOf(config.tint);
    // Four rows of twelve, one row at a time: the family is how loud the accent is and the hue
    // is the choice. A swatch is the colour that will be used, not a stand-in for it.
    const tabs = document.createElement("div");
    tabs.className = "tabs";
    for (const name of Object.keys(caps.accents || {})) {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.textContent = title(name);
      if (name === family) tab.classList.add("on");
      tab.onclick = () => { family = name; renderTint(); };   // a look, not yet a change
      tabs.appendChild(tab);
    }
    accents.appendChild(tabs);

    const strip = document.createElement("div");
    strip.className = "swatches";
    for (const accent of (caps.accents || {})[family] || []) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.style.background = `rgb(${accent.join(", ")})`;
      chip.title = `rgb(${accent.join(", ")})`;
      if (String(config.tint) === String(accent)) chip.classList.add("on");
      chip.onclick = () => {
        config.tint = accent.slice();
        markDirty();
        renderTint();
      };
      strip.appendChild(chip);
    }
    accents.appendChild(strip);
  }
  preview();
}

async function preview() {
  const query = new URLSearchParams({ theme: config.theme || "dark" });
  if ((caps.tinted || {})[config.theme]) {
    query.set("accent", (config.tint || []).join(","));
    query.set("second", config.accent_b || "same");
  }
  const mine = ++previewWanted;
  let shown;
  try {
    shown = await api(`/api/theme?${query}`);
  } catch (error) {
    return;
  }
  if (mine !== previewWanted) return;
  const palette = shown.palette;
  const node = $("preview");
  const set = (name, rgb) => node.style.setProperty(name, `rgb(${rgb.join(", ")})`);
  set("--pv-bg", palette.bg);
  set("--pv-panel", palette.panel);
  set("--pv-ink", palette.ink);
  set("--pv-dim", palette.dim);
  // The header's rule and the current pip, which on the badge take the second accent.
  set("--pv-accent", palette.accent_b || palette.accent);
  set("--pv-grid", palette.grid);
  paintDial(node.querySelector(".pv-dial"), palette, config.gauge_fill);
  // The second accent as the rule resolved it, beside the rule.
  $("accentbchip").style.background = `rgb(${(palette.accent_b || palette.accent).join(", ")})`;
  // And the three bars, each at the ramp colour for its own reading.
  for (const [name, at] of [["--pv-r62", 0.62], ["--pv-r46", 0.46], ["--pv-r78", 0.78]]) {
    set(name, rampAt(palette.ramp, at));
  }
}

// The gauge the preview draws: a 270 degree sweep from the lower left, filled to the reading
// the panel shows, in the ramp's own colours - so what it says about a palette is what the
// badge will do with it.
const PV_SWEEP = 0.75;                  // of a whole turn, the gap centred on the bottom
const PV_READING = 0.635;               // the reading printed inside it

// How faint the part past the reading is when the whole ramp is shown, matching draw.TRACK_ALPHA
// on the badge. A gradient there is drawn over the page rather than composited, so the colours
// are mixed towards it here.
const PV_TRACK_ALPHA = 32 / 255;

function paintDial(dial, palette, fill) {
  if (!dial) return;
  const colour = (rgb) => `rgb(${rgb.join(", ")})`;
  const faint = (rgb) => colour(rgb.map((part, index) =>
    Math.round(part * PV_TRACK_ALPHA + palette.bg[index] * (1 - PV_TRACK_ALPHA))));
  const bg = colour(palette.bg);
  const filled = PV_SWEEP * PV_READING;
  const at = (part) => `${(part * 100).toFixed(1)}%`;
  const reached = rampAt(palette.ramp, PV_READING);

  const stops = [];
  if (fill === "ramp") {
    // The whole ramp laid round the arc, as the conical gradient does it: a colour's place is
    // its place on the ramp, so the sweep ends at the reading's own colour and the rest of the
    // ramp shows faintly beyond it.
    for (const [position, rgb] of palette.ramp) {
      if (position < PV_READING) stops.push(`${colour(rgb)} ${at(position * PV_SWEEP)}`);
    }
    stops.push(`${colour(reached)} ${at(filled)}`);
    stops.push(`${colour(palette.ink)} ${at(filled)} ${at(filled + 0.004)}`);
    stops.push(`${faint(reached)} ${at(filled + 0.004)}`);
    for (const [position, rgb] of palette.ramp) {
      if (position > PV_READING) stops.push(`${faint(rgb)} ${at(position * PV_SWEEP)}`);
    }
  } else {
    // One colour, the ramp's for this reading, and the unlit track beyond it.
    stops.push(`${colour(reached)} ${at(filled)}`);
    stops.push(`${colour(palette.ink)} ${at(filled)} ${at(filled + 0.004)}`);
    stops.push(`${colour(palette.grid)} ${at(filled + 0.004)}`);
  }
  stops.push(`${colour(palette.grid)} ${at(PV_SWEEP)}`, `${bg} ${at(PV_SWEEP)}`);
  dial.style.background = `radial-gradient(closest-side, ${bg} 74%, transparent 75%), `
    + `conic-gradient(from 225deg, ${stops.join(", ")})`;
}

function rampAt(stops, at) {
  let [low, first] = stops[0];
  for (const [position, colour] of stops) {
    if (at <= position) {
      const span = position - low;
      const part = span <= 0 ? 0 : (at - low) / span;
      return first.map((from, index) =>
        Math.round(from + (colour[index] - from) * part));
    }
    [low, first] = [position, colour];
  }
  return stops[stops.length - 1][1];
}

// -- which badge -----------------------------------------------------------
//
// One layout per badge, and a default for a badge that has not been given its own. The picker
// in the header says which of them the page is editing; `null` is the default.

const REMEMBERED = "statsbadge.whose";

function configPath(path) {
  const base = path || "/api/config";
  return whose ? `${base}?badge=${encodeURIComponent(whose)}` : base;
}

function badgeName(id) {
  return (badges[id] && badges[id].name) || id;
}

/** Which badge to open on: the one last edited if it is still paired, else the first. */
function pickBadge() {
  let last = null;
  try { last = window.localStorage.getItem(REMEMBERED); } catch (error) { /* private */ }
  if (last && badges[last]) return last;
  return Object.keys(badges)[0] || null;
}

function remember(id) {
  try {
    if (id) window.localStorage.setItem(REMEMBERED, id);
    else window.localStorage.removeItem(REMEMBERED);
  } catch (error) { /* nothing to remember it in */ }
}

function renderWhose() {
  const select = $("whose");
  const ids = Object.keys(badges);
  select.innerHTML = "";
  for (const id of ids) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = badgeName(id);
    select.appendChild(option);
  }
  const fallback = document.createElement("option");
  fallback.value = "";
  fallback.textContent = ids.length ? "Default, for any other badge" : "No badge paired yet";
  select.appendChild(fallback);
  select.value = whose || "";
  select.onchange = () => switchTo(select.value).catch((e) => toast(e.message, true));

  const own = whose && badges[whose] && badges[whose].configured;
  $("whosenote").textContent = whose && !own
    ? "on the default layout, until you save"
    : (!whose && ids.length ? "what a newly paired badge draws" : "");
  $("forget").disabled = !whose;
  $("forget").onclick = () => forgetBadge(whose).catch((e) => toast(e.message, true));
}

/** Load another badge's layout into the page. */
async function switchTo(id) {
  if (dirty && !window.confirm("Discard the unsaved changes to this badge?")) {
    renderWhose();                      // put the picker back on the badge being edited
    return;
  }
  whose = id || null;
  remember(whose);
  config = await api(configPath());
  dirty = false;
  $("save").disabled = true;
  renderWhose();
  renderPages();
  renderSettings();
  renderLook();
  renderBadges();
}

async function forgetBadge(id) {
  if (!window.confirm(`Forget ${badgeName(id)}? Its layout goes with it.`)) return;
  await api(`/api/badges/${id}`, { method: "DELETE" });
  badges = await api("/api/badges");
  dirty = false;                        // the badge those edits belonged to is gone
  await switchTo(Object.keys(badges)[0] || "");
  toast("Forgotten");
}

/** Page ids made this badge's own.
 *
 * An extension keys what it does per page by page id - which city a clock page shows - so two
 * badges must not carry the same one. Done where a badge stops drawing the default and gets a
 * layout of its own, and derived from the badge id so switching back and forth is stable. */
function ownIds(pages, badgeId) {
  const tag = badgeId.slice(0, 4);
  return pages.map((page) => (String(page.id).endsWith(`-${tag}`)
    ? page : { ...page, id: `${page.id}-${tag}` }));
}

function renderBadges() {
  const node = $("badges");
  node.innerHTML = "";
  const ids = Object.keys(badges);
  if (!ids.length) {
    node.innerHTML = `<div class="hint">None paired. Use the USB installer, or `
      + `pair over the network.</div>`;
    return;
  }
  for (const id of ids) {
    const row = document.createElement("div");
    row.className = "badge-row" + (id === whose ? " on" : "");
    const name = document.createElement("span");
    name.className = "name";
    name.innerHTML = `${badges[id].name || id}<br><code>${id}</code>`;
    row.appendChild(name);
    const state = document.createElement("span");
    state.className = "state";
    state.textContent = badges[id].configured ? "own layout" : "default";
    row.appendChild(state);
    // Clicking a row is the other way to get at a badge, the picker being in the header.
    row.onclick = () => switchTo(id).catch((error) => toast(error.message, true));
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
  const result = await api(`/api/enrol/${requestId}/${approve ? "approve" : "deny"}`,
                          { method: "POST" });
  toast(approve ? "Badge paired" : "Denied");
  badges = await api("/api/badges");
  // Straight to the badge just paired, since configuring it is what comes next - unless there
  // are edits for another one, which are not worth interrupting for.
  if (approve && result.approved && !dirty) {
    await switchTo(result.approved);
  } else {
    renderWhose();
    renderBadges();
  }
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
    // A badge saving for the first time stops drawing the default and gets pages of its own.
    if (whose && badges[whose] && !badges[whose].configured) {
      config.pages = ownIds(config.pages, whose);
    }
    const result = await api(configPath(), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
    config.rev = result.rev;
    if (whose && badges[whose]) badges[whose].configured = true;
    dirty = false;
    $("save").disabled = true;
    toast(`Saved. ${whose ? badgeName(whose) : "A badge on the default"} `
      + `will pick up revision ${result.rev}.`);
    renderWhose();
    renderPages();
    renderBadges();
  } catch (error) {
    toast(error.message, true);
  }
}

async function boot() {
  try {
    [caps, badges] = await Promise.all([api("/api/capabilities"), api("/api/badges")]);
    whose = pickBadge();
    config = await api(configPath());
  } catch (error) {
    document.body.innerHTML = `<p style="padding:20px">Cannot reach the server: `
      + `${error.message}</p>`;
    return;
  }
  const sys = (await api("/api/stats")).sys || {};
  $("hostline").textContent =
    `${sys.host || "host"} · ${sys.os || ""} · ${sys.cpu_name || ""}`;

  offerExtensionPages();
  renderWhose();
  renderPages();
  renderSettings();
  renderLook();
  renderSources();
  renderBadges();
  renderLive();

  $("save").onclick = save;
  $("add").onclick = () => {
    // At the top: added at the bottom it lands off the end of a long list, and the
    // first thing anyone does with a new page is configure it.
    config.pages.unshift(newPage($("addkind").value));
    expanded.add(config.pages[0].id);
    markDirty();
    renderPages();
  };
  // Picks up a window opened by `statsbadge pair` or a previous page load.
  watchPairing().catch(() => {});

  setInterval(renderLive, 1000);
  window.onbeforeunload = () => (dirty ? "You have unsaved changes." : undefined);
}

boot();
