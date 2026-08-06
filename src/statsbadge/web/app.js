// The config UI. Edits a config object, PUTs it, and the badge picks up the new revision on
// its next poll.
//
// Everything on the page belongs to one badge, chosen in the header: `whose` is its id, or
// null for the layout a badge draws before it has been given one of its own.

const $ = (id) => document.getElementById(id)
const pick = (selector) => document.querySelector(selector)
const all = (selector) => [...document.querySelectorAll(selector)]

/** An element, its properties, and whatever goes inside it. A key with a dash is set as an
 * attribute, since `aria-` and `data-` have no property of their own. */
function el(tag, props, ...children) {
  const node = document.createElement(tag)
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined) continue
    if (key.includes("-")) node.setAttribute(key, value)
    else node[key] = value
  }
  node.append(...children.flat().filter((child) => child !== null && child !== undefined))
  return node
}

let config = null
let caps = null
let dirty = false
let whose = null
let badges = {}

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
  // One slot list holding two sorts of thing: the renderer tells a message from a counter by
  // looking at the reading, so a feed, a mention and a follower count go in one page.
  notify: { one: null, many: "fields", max: 6, label: "Lines", manyPool: "notify" },
  // The badge's own vitals: no fields at all, since none of it comes from the host.
  badge: { one: null, many: null, max: 0, label: "" },
}

async function api(path, options) {
  const response = await fetch(path, options)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error((body && body.error) || response.statusText)
  return body
}

// Words that stay lowercase inside a title, the first one never being one of them.
const MINOR = new Set(["a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on",
                       "or", "the", "to", "with"])

/** Title case for a name that arrives as a word or a slug. What an extension declared is left
 * as it declared it: those are its own strings, and "kmh" is not "Kmh". */
function titleCase(text) {
  return String(text).replace(/[-_]+/g, " ").trim().split(/\s+/)
    .map((word, index) => (index && MINOR.has(word.toLowerCase())
      ? word.toLowerCase()
      : word.charAt(0).toUpperCase() + word.slice(1)))
    .join(" ")
}

function toast(message, bad) {
  const node = el("div", { className: bad ? "toast bad" : "toast", textContent: message })
  document.body.appendChild(node)
  setTimeout(() => node.remove(), 2600)
}

function markDirty() {
  dirty = true
  $("save").disabled = false
}

// -- tabs ------------------------------------------------------------------
//
// One sheet at a time, so a phone gets a page it can read and a wide screen gets boxes that
// flow across it. The nav and the sheets are in the same order; nothing else pairs them.

const REMEMBERED_TAB = "statsbadge.tab"

function showSheet(wanted) {
  const tabs = all("header nav button")
  const sheets = all("main > section")
  tabs.forEach((tab, index) => {
    if (index === wanted) tab.setAttribute("aria-current", "page")
    else tab.removeAttribute("aria-current")
    sheets[index].hidden = index !== wanted
  })
  try { window.localStorage.setItem(REMEMBERED_TAB, wanted) } catch (error) { /* private */ }
}

function bindTabs() {
  const tabs = all("header nav button")
  tabs.forEach((tab, index) => { tab.onclick = () => showSheet(index) })
  let opening = 0
  try { opening = Number(window.localStorage.getItem(REMEMBERED_TAB)) } catch (error) {
    /* private mode */
  }
  showSheet(tabs[opening] ? opening : 0)
}

// -- field pickers ---------------------------------------------------------

function availableRefs() {
  const refs = []
  const available = (caps && caps.available) || {}
  for (const group of Object.keys(available).sort()) {
    for (const field of available[group]) refs.push(`${group}.${field}`)
  }
  return refs
}

/** Numbers first, then everything else, each of them once. The numeric refs are a subset of
 * all of them, so without the Set every number appears twice. */
function preferredRefs() {
  // Lists are left out of even this: fmt has nothing to do with one but print it, so a grid
  // cell handed cpu.cores shows a row of Python. A message likewise.
  const printable = availableRefs().filter(
    (ref) => !listFields().includes(ref.split(".")[1])
             && !itemFields().includes(ref.split(".")[1]))
  return [...new Set(numericRefs().concat(printable))]
}

/** Refs that are a number at all: not a name, a flag, a list or a message. */
function numericRefs() {
  return availableRefs().filter((ref) => {
    const field = ref.split(".")[1]
    return !["name", "host", "os", "arch", "cpu_name", "iface", "charging"]
      .includes(field) && !listFields().includes(field)
      && !itemFields().includes(field)
  })
}

function listFields() {
  return caps.list_fields || ["cores", "load"]
}

function itemFields() {
  return caps.item_fields || []
}

/** Refs that are a message rather than a reading: a post, a mention, a headline. */
function itemRefs() {
  return availableRefs().filter((ref) => itemFields().includes(ref.split(".")[1]))
}

/** What a notifications page can hold: the messages first, then anything countable. */
function notifyRefs() {
  return [...new Set(itemRefs().concat(numericRefs()))]
}

/** Refs a gauge can place a needle on: a percentage, or something with a top end.
 *
 * Being a number is not enough. Uptime is a number and a ring drawn from it is empty whatever
 * the machine has been doing, because nothing says what a full one would be. */
function gaugeRefs() {
  const percent = caps.percent_fields || []
  const scaled = Object.keys(caps.full_scale || {})
  return numericRefs().filter((ref) => {
    const field = ref.split(".")[1]
    return percent.includes(field) || scaled.includes(field)
  })
}

/** Refs the host keeps a history ring for, which is what a graph needs to say anything.
 *
 * Without one the page plots the live value twice and draws a flat line, which looks like a
 * reading that never changes rather than one nobody is recording. */
function seriesRefs() {
  const kept = caps.graphed || []
  const withHistory = numericRefs().filter((ref) => kept.includes(ref))
  return withHistory.length ? withHistory : numericRefs()
}

/** Refs that are a list, for the kinds that draw one lane or bar per element. */
function listRefs() {
  return availableRefs().filter((ref) => listFields().includes(ref.split(".")[1]))
}

// Which pool each slot draws from. "gauge" needs a top end, "series" only needs to be a number
// since it scales itself from the data, "list" wants one value per element, "notify" takes a
// message or a number, and "any" prints whatever it is given.
const POOLS = {
  gauge: gaugeRefs,
  series: seriesRefs,
  list: listRefs,
  notify: notifyRefs,
  any: preferredRefs,
}

function groupLabel(group) {
  return (caps.group_labels || {})[group] || group
}

// What the sources this host measures itself with are listed under. Not "System", which is
// what the `sys` group is already called: that would head a list with its own last entry.
const HOST_SOURCE = "This host"

function sourceLabel(group) {
  return (caps.group_source || {})[group] || HOST_SOURCE
}

function fieldLabel(ref) {
  const [group, field] = ref.split(".")
  const labels = (caps.field_labels || {})[group] || {}
  return labels[field] || titleCase(field)
}

/** Two dropdowns: which source, then which of its readings.
 *
 * One list of every reading was navigable while a host measured itself and nothing else. An
 * extension contributes a group per thing it watches - a domain apiece, for an account with
 * six of them - so the source is picked first and the metric list is only ever that source's.
 * Returned as a fragment, so the row's flex layout still reaches the selects. */
function refSelect(value, refs, onChange) {
  // Deduplicated here as well as by the caller: a repeated option is impossible to tell apart
  // once it is on screen.
  const options = [...new Set(refs)]
  if (value && !options.includes(value)) options.unshift(value)

  const byGroup = new Map()
  for (const ref of options) {
    const group = ref.split(".")[0]
    if (!byGroup.has(group)) byGroup.set(group, [])
    byGroup.get(group).push(ref)
  }

  // Grouped under whoever provides them, since an extension watching six domains would
  // otherwise bury this host's own readings in a list of names with nothing saying what
  // they are.
  const byOwner = new Map()
  for (const group of byGroup.keys()) {
    const owner = sourceLabel(group)
    if (!byOwner.has(owner)) byOwner.set(owner, [])
    byOwner.get(owner).push(group)
  }
  // The host first whatever it is called, since most pages are made of it.
  const owners = [...byOwner.keys()].sort(
    (a, b) => (a === HOST_SOURCE ? -1 : 0) - (b === HOST_SOURCE ? -1 : 0))

  const chosen = String(value || options[0] || "").split(".")[0]
  const source = el("select", { "aria-label": "Source" }, owners.map(
    (owner) => el("optgroup", { label: owner }, byOwner.get(owner).map(
      (group) => el("option", { value: group, textContent: groupLabel(group),
                                selected: group === chosen })))))

  const select = el("select", { "aria-label": "Reading" })
  // Told which ref rather than reading it back off the select, so the first paint does not
  // depend on `value` already reflecting the option marked selected.
  const fill = (group, ref) => {
    select.replaceChildren(...(byGroup.get(group) || []).map(
      (each) => el("option", { value: each, textContent: fieldLabel(each),
                               selected: each === ref })))
  }
  fill(chosen, value)

  // Changing source picks that source's first reading: the one showing belongs to the source
  // being left, and leaving it there would read as a choice nobody made.
  source.onchange = () => {
    fill(source.value, null)
    onChange(select.value)
    markDirty()
  }
  select.onchange = () => { onChange(select.value); markDirty() }
  return [source, select]
}

// -- pages -----------------------------------------------------------------

// Which cards are open. Collapsed by default so the list reads as an overview and stays short
// enough to drag around.
const expanded = new Set()

function renderPages() {
  $("pages").replaceChildren(...config.pages.map(pageCard))
  refreshPruned()
}

/** The field slots a kind has. An extension's page declares its own, because only its renderer
 * knows whether it reads `fields` at all - the clock face draws from its groups and ignores
 * them, so offering seven pickers was offering seven controls that did nothing. */
function shapeFor(kind) {
  if (SHAPE[kind]) return SHAPE[kind]
  const declared = (caps.extension_pages || []).find((page) => page.kind === kind)
  const slots = (declared && declared.slots) || {}
  return { one: slots.one || null, many: slots.many || null,
           max: slots.max || 0, label: slots.label || "Values" }
}

/** A slot label in the singular, for the button that adds one. */
function singular(label) {
  if (label === "Series") return label
  if (label === "Axes") return "Axis"
  return label.endsWith("s") ? label.slice(0, -1) : label
}

function pageCard(page, index) {
  const shape = shapeFor(page.kind)
  const open = expanded.has(page.id)
  const settings = (caps.extension_page_settings || {})[page.kind] || []

  const titleId = `page${++controlSerial}`
  const title = el("input", { type: "text", id: titleId, value: page.title || "" })
  title.oninput = () => { page.title = title.value; markDirty() }

  const toggle = el("button", { type: "button", textContent: open ? "▾" : "▸",
                                title: open ? "Collapse" : "Configure",
                                "aria-expanded": String(open) })
  toggle.onclick = () => {
    if (open) expanded.delete(page.id); else expanded.add(page.id)
    renderPages()               // not markDirty: opening a card changes nothing
  }

  const remove = el("button", { type: "button", className: "danger small",
                                textContent: "✕", title: "Remove this page" })
  // Asked for, the button being next to the one that opens a card.
  remove.onclick = () => {
    if (config.pages.length <= 1) return toast("Keep at least one page", true)
    if (!window.confirm(`Remove ${page.title || page.kind}?`)) return undefined
    config.pages.splice(index, 1)
    markDirty()
    return renderPages()
  }

  // The title is the handle: a card holds a text field and two pickers, and dragging it
  // from anywhere meant dragging it out from under whichever one was being used.
  const kind = el("h3", { textContent: page.kind, title: "Drag to reorder" })
  const item = el("li", null,
                  el("header", null, kind, toggle, remove),
                  el("label", { htmlFor: titleId, textContent: "Title" }),
                  title)

  if (open) {
    item.append(slotList(page, shape),
                ...settings.flatMap((setting) => settingRow(page, setting)))
  } else {
    const refs = shape.one ? [page[shape.one]] : (page[shape.many] || [])
    const named = refs.filter(Boolean).map(fieldLabel)
    const extra = settings.map((setting) => page[setting.key]).filter(Boolean)
    item.append(el("p", { textContent: named.concat(extra).join(", ") || "nothing chosen" }))
  }

  reorderable(item, config.pages, index, { tag: "page", along: "x", handle: kind })
  return item
}

/** Drag one of `items` to another place in it.
 *
 * The tag names the list a drag came from. A page card is draggable and so are the rows
 * inside it, so without one a row dropped on its own card would reorder the pages; the
 * events are stopped on the way up for the same reason. `along` is the axis the list runs,
 * which decides both which half of an item counts as before it and which edge is marked,
 * and a `handle` is what has to be held for the drag to start at all.
 */
function reorderable(node, items, index, { tag, along, handle }) {
  node.draggable = !handle
  if (handle) {
    handle.onpointerdown = () => { node.draggable = true }
    handle.onpointerup = () => { node.draggable = false }
  }
  node.ondragstart = (event) => {
    event.stopPropagation()
    node.dataset.dragging = ""
    event.dataTransfer.setData("text/plain", `${tag}:${index}`)
  }
  node.ondragend = () => {
    node.draggable = !handle
    delete node.dataset.dragging
    delete node.dataset.over
  }
  node.ondragover = (event) => {
    event.preventDefault()
    event.stopPropagation()
    const box = node.getBoundingClientRect()
    node.dataset.over = (along === "x"
      ? event.clientX < box.left + box.width / 2
      : event.clientY < box.top + box.height / 2) ? "before" : "after"
  }
  // Only on the way out of the whole item: moving over a select inside it is a leave too.
  node.ondragleave = (event) => {
    if (!node.contains(event.relatedTarget)) delete node.dataset.over
  }
  node.ondrop = (event) => {
    event.preventDefault()
    event.stopPropagation()
    const after = node.dataset.over === "after"
    delete node.dataset.over
    const [from, at] = event.dataTransfer.getData("text/plain").split(":")
    const moved = parseInt(at, 10)
    if (from !== tag || Number.isNaN(moved)) return
    // Where it lands once it has been lifted out, which shifts everything after it down.
    let target = after ? index + 1 : index
    if (moved < target) target -= 1
    if (target === moved) return
    items.splice(target, 0, items.splice(moved, 1)[0])
    markDirty()
    renderPages()
  }
}

/** The readings a page is made of, one row per slot. */
function slotList(page, shape) {
  const rows = []
  const poolFor = (name) => {
    const refs = (POOLS[name] || POOLS.any)()
    return refs.length ? refs : availableRefs()
  }

  if (shape.one) {
    rows.push(el("li", null,
                 el("span", { textContent: page.kind === "bars" ? "List" : "Gauge" }),
                 refSelect(page[shape.one], poolFor(shape.pool),
                           (value) => { page[shape.one] = value })))
  }

  const current = shape.many ? page[shape.many] || [] : []
  current.forEach((ref, slot) => {
    const drop = el("button", { type: "button", className: "small", textContent: "−",
                                title: "Remove this slot" })
    drop.onclick = () => { current.splice(slot, 1); markDirty(); renderPages() }
    const row = el("li", null,
                   el("span", { className: "grip", textContent: "⠇" }),
                   refSelect(ref, poolFor(shape.manyPool), (value) => { current[slot] = value }),
                   drop)
    reorderable(row, current, slot, { tag: "slot", along: "y" })
    rows.push(row)
  })

  if (shape.many && current.length < shape.max) {
    const add = el("button", { type: "button", className: "small",
                               textContent: `Add ${singular(shape.label).toLowerCase()}` })
    add.onclick = () => {
      page[shape.many] = current.concat([poolFor(shape.manyPool)[0]])
      markDirty()
      renderPages()
    }
    rows.push(el("li", null, add))
  }
  return el("ol", null, rows)
}

function newPage(kind) {
  const suffix = Date.now().toString(36).slice(-4)
  const offered = (caps.extension_pages || []).find((page) => page.kind === kind)
  if (offered) {
    // An extension knows its own page: take the fields and title it shipped with, since only
    // its badge module knows what shape they go in.
    return { ...offered, id: `${offered.id || kind}${suffix}` }
  }
  const shape = SHAPE[kind]
  const pool = numericRefs()
  const page = { id: `${kind}${suffix}`, kind, title: kind }
  if (shape.one) {
    page[shape.one] = kind === "bars" ? "cpu.cores" : (pool[0] || "cpu.pct")
  }
  if (shape.many) {
    page[shape.many] = pool.slice(0, Math.min(2, shape.max))
  }
  return page
}

/** Add the installed extensions' pages to the kind picker, which lists the built-ins in
 * groups of its own. */
function offerExtensionPages() {
  const picker = pick("main form select")
  const offered = (caps.extension_pages || []).filter(
    (page) => ![...picker.options].some((option) => option.value === page.kind))
  if (!offered.length) return
  const group = picker.querySelector("optgroup[label=\"Extensions\"]")
    || picker.appendChild(el("optgroup", { label: "Extensions" }))
  group.append(...offered.map(
    (page) => el("option", { value: page.kind, textContent: page.title || page.kind })))
}

/** Tell the user when a page they configured will not appear on the badge. */
async function refreshPruned() {
  try {
    const shown = await api(configPath("/api/preview"))
    const kept = new Set(shown.pages.map((page) => page.id))
    const dropped = config.pages.filter((page) => !kept.has(page.id)).map((page) => page.title)
    const node = pick('p[role="status"]')
    node.textContent = "Not shown on the badge, because this host reports no data for "
      + `them: ${dropped.join(", ")}`
    node.hidden = !dropped.length
  } catch (error) { /* advisory */ }
}

// -- extension settings ----------------------------------------------------

/** A box per installed extension, titled with its name, holding whatever it can be told.
 *
 * Every discovered one, not only those with settings: an extension that asks to be told
 * nothing had nothing in the UI at all, and one that failed to import was invisible until a
 * page it was meant to draw did not appear. */
function renderSettings() {
  const schema = caps.extension_settings || {}
  const installed = caps.extensions || []
  if (!installed.length) {
    $("extensions").replaceChildren(el("section", null, el("p", { textContent:
      "None installed. pip install one, then run statsbadge install." })))
    return
  }
  config.settings = config.settings || {}
  $("extensions").replaceChildren(
    ...installed.map((extension) => extensionBox(extension, schema[extension.name] || [])))
}

function extensionBox(extension, settings) {
  const state = el("p")
  if (extension.error) {
    state.className = "bad"
    state.textContent = extension.error
  } else if (extension.available === false) {
    state.textContent = "Installed, but not usable on this host."
  } else {
    const parts = []
    if (extension.version) parts.push(extension.version)
    if (extension.provides.length) parts.push(extension.provides.join(", "))
    if (extension.badge_module) parts.push("draws its own page")
    state.textContent = parts.join(" · ")
  }

  const box = el("section", null, el("h3", { textContent: extension.name }), state)
  if (!settings.length) return box

  config.settings[extension.name] = config.settings[extension.name] || {}
  const stored = config.settings[extension.name]
  for (const setting of settings) {
    if (setting.secret) continue
    box.append(...settingRow(stored, setting))
    if (setting.hint) box.append(el("p", { textContent: setting.hint }))
  }
  const secrets = settings.filter((setting) => setting.secret)
  if (secrets.length) box.append(secretsBlock(extension.name, stored, secrets))
  return box
}

// How much of a secret is shown, and the most x's drawn after it. Enough to tell which key is
// in there without putting the key on the screen.
const SECRET_SHOWN = 6
const SECRET_MAX = 18

/** A stored secret as something safe to leave on screen. Empty for one that is not set, which
 * the sheet draws as "not set" - the two have to be told apart. */
function masked(value) {
  const text = value === null || value === undefined ? "" : String(value)
  if (!text) return ""
  const shown = text.slice(0, SECRET_SHOWN)
  return shown + "x".repeat(Math.max(4, Math.min(SECRET_MAX, text.length - shown.length)))
}

// Which extensions have their secrets open for editing. Module level, so a redraw - and a
// capabilities refresh after a save is one - does not close the box under the typing.
const editingSecrets = new Set()

/** The API keys, masked behind a button.
 *
 * Masked rather than hidden, because "not set" and "set to the wrong one" have to be told
 * apart, and the first few characters are what somebody checking would recognise. */
function secretsBlock(name, stored, secrets) {
  const open = editingSecrets.has(name)
  const block = el("div", { className: "secrets" })

  if (open) {
    for (const setting of secrets) {
      block.append(...settingRow(stored, setting, { reveal: true }))
      if (setting.hint) block.append(el("p", { textContent: setting.hint }))
    }
  } else {
    block.append(el("dl", null, secrets.flatMap((setting) => [
      el("dt", { textContent: setting.label || setting.key }),
      el("dd", { textContent: masked(stored[setting.key]) }),
    ])))
  }

  const button = el("button", { type: "button", className: "small",
                                textContent: open ? "Hide secrets" : "Edit secrets" })
  button.onclick = () => {
    if (open) editingSecrets.delete(name); else editingSecrets.add(name)
    renderSettings()               // not markDirty: opening a box changes nothing
  }
  block.append(button)
  return block
}

// Ids for the controls built here, so each has a label pointing at it. Only ever compared with
// the label beside it, so a counter that keeps climbing across redraws is enough.
let controlSerial = 0

/** A form row: the name, then the control it names. Two siblings and not one wrapping the
 * other, so both land in the tracks of whichever grid the row is added to. */
function settingRow(stored, setting, options) {
  const id = `setting${++controlSerial}`
  const label = el("label", { htmlFor: id, textContent: setting.label || setting.key })
  const current = stored[setting.key] !== undefined ? stored[setting.key] : setting.default

  let input
  if (setting.type === "bool") {
    input = el("input", { type: "checkbox", id, checked: !!current })
    input.onchange = () => { stored[setting.key] = input.checked; markDirty() }
  } else if (setting.type === "choice") {
    input = el("select", { id }, (setting.options || []).map(
      (option) => el("option", { value: option, textContent: option,
                                 selected: option === current })))
    input.onchange = () => { stored[setting.key] = input.value; markDirty() }
  } else {
    input = el("input", { type: "text", id,
                          value: current === null || current === undefined ? "" : current })
    if (setting.secret) {
      // Shown in full, since the whole of asking to edit these is to read one back and replace
      // it. Kept out of autofill and the spellchecker, neither of which has any business with
      // a token.
      input.autocomplete = "off"
      input.spellcheck = false
      input.placeholder = (options && options.reveal) ? "paste the key here" : ""
    }
    // Empty means unset, which is not the same as zero: a latitude of 0 is the equator.
    input.oninput = () => {
      stored[setting.key] = input.value === "" ? null : input.value
      markDirty()
    }
  }
  return [label, input]
}

// -- look and buttons ------------------------------------------------------

function renderLook() {
  const theme = $("theme")
  // Grouped by mode: which of them suit a lit room is the first thing anybody is choosing
  // between, and a flat list of twenty had the pairs scattered through it.
  theme.replaceChildren(...[["dark", "Dark"], ["light", "Light"]].map(([mode, heading]) =>
    el("optgroup", { label: heading }, caps.themes
      .filter((entry) => entry.mode === mode)
      .map((record) => el("option", { value: record.name, selected: record.name === config.theme,
                                      textContent: record.label || titleCase(record.name) })))))
  theme.onchange = () => { config.theme = theme.value; markDirty(); renderTint() }
  renderTint()

  bindRange("interval", "interval_ms", (value) => `${value} ms`)
  bindRange("brightness", "brightness", (value) => `${value}%`, 100)
  bindRange("points", "graph_points", (value) => `${value}`)
  // Zero is off, so the readout says so rather than showing a time nothing happens at.
  bindRange("idle", "idle_advance_s", (value) => (value === "0" ? "off" : `${value}s idle`))
  bindRange("advance", "advance_every_s", (value) => `${value}s`)

  // Stored as a flag, offered as the two things it looks like.
  bindSelect("smooth", () => (config.smooth === false ? "straight" : "curved"),
             (value) => { config.smooth = value === "curved" })
  bindSelect("rows", () => config.rows || "zebra", (value) => { config.rows = value })
  // Older configs stored a flag here, before there was a third way for a page to turn.
  const turn = () => (typeof config.slide === "string" ? config.slide
    : (config.slide ? "over" : "off"))
  bindSelect("slide", turn, (value) => { config.slide = value })
  bindSelect("gaugefill", () => config.gauge_fill || "solid", (value) => {
    config.gauge_fill = value
    preview()                          // the preview draws the gauge the way the badge will
  })

  bindCheck("animate", "animate")
  bindCheck("plotanim", "plot_animation")
  bindCheck("autobright", "auto_brightness")

  renderCaseLights()
  renderButtons()
}

function bindRange(id, key, format, scale) {
  const input = $(id)
  const out = pick(`output[for="${id}"]`)
  const factor = scale || 1
  input.value = Math.round((config[key] || 0) * factor)
  out.textContent = format(input.value)
  input.oninput = () => {
    config[key] = factor === 1
      ? parseInt(input.value, 10)
      : parseInt(input.value, 10) / factor
    out.textContent = format(input.value)
    markDirty()
  }
}

function bindSelect(id, read, write) {
  const select = $(id)
  select.value = read()
  select.onchange = () => { write(select.value); markDirty() }
}

function bindCheck(id, key) {
  const input = $(id)
  input.checked = !!config[key]
  input.onchange = () => { config[key] = input.checked; markDirty() }
}

/** Off, the theme's own level, or a reading for the lights to follow. The stored value is
 * false, true, or a field ref, so the option values carry it directly. */
function renderCaseLights() {
  const options = [["off", "Off"], ["theme", "Follow the Theme"]]
  for (const ref of numericRefs()) {
    options.push([ref, `${groupLabel(ref.split(".")[0])} - ${fieldLabel(ref)}`])
  }
  const current = config.caselights === true ? "theme" : config.caselights || "off"
  // A reading this host has stopped sending still has to be selectable, or opening the page
  // and saving it would quietly turn the lights off.
  if (!options.some(([value]) => value === current)) options.push([current, current])

  const caselights = $("caselights")
  caselights.replaceChildren(...options.map(([value, text]) =>
    el("option", { value, textContent: text, selected: value === current })))
  caselights.onchange = () => {
    const value = caselights.value
    config.caselights = value === "off" ? false : value === "theme" ? true : value
    markDirty()
  }
}

function renderButtons() {
  // The badge's own first, being the ones that need no host at all.
  const offered = [
    el("option", { value: "", textContent: "Nothing" }),
    ...(caps.local_actions || []).map((local) => el("option", {
      value: local.action, textContent: `${titleCase(local.label)} (on the badge)` })),
    ...caps.commands.map((name) => el("option", { value: name, textContent: titleCase(name) })),
  ]
  for (const which of ["a", "b", "c"]) {
    const select = $(`btn-${which}`)
    select.replaceChildren(...offered.map((option) => option.cloneNode(true)))
    select.value = (config.buttons && config.buttons[which]) || ""
    select.onchange = () => {
      config.buttons = config.buttons || {}
      config.buttons[which] = select.value || null
      markDirty()
    }
  }
}

// -- the theme preview ----------------------------------------------------
//
// The palette comes from the host, for every theme and not only the tinted ones: it is derived
// there for those, so what the preview shows and what reaches the badge cannot drift apart,
// and the browser needs no colour arithmetic of its own.

// Which preview request is the current one. Clicking along the swatches starts several, and
// they can come back in any order: without this the last reply wins rather than the last
// click, and the panel ends up showing a colour nobody chose.
let previewWanted = 0

/** Which family of accents the picker is showing. Follows the stored one when the panel opens,
 * so what is on screen is the row the chosen colour came from. */
let family = null

function familyOf(accent) {
  const stored = String(accent)
  for (const [name, list] of Object.entries(caps.accents || {})) {
    if (list.some((offer) => String(offer) === stored)) return name
  }
  return caps.accent_family || "normal"
}

function renderTint() {
  const tinted = !!(caps.tinted || {})[config.theme]
  // How the second accent is picked, which only a derived palette works out: a written-down
  // one either names its own or has none.
  for (const node of all("[data-tint]")) node.hidden = !tinted

  const second = $("accentb")
  second.value = config.accent_b || "same"
  second.onchange = () => { config.accent_b = second.value; markDirty(); renderTint() }

  // What the ramp does is the difference between the two pairs, so the hint says which.
  pick("p[data-tint]").textContent = (caps.bold || []).includes(config.theme)
    ? "The rest of the palette is worked out from this colour, and the ramp stays in its hue, "
      + "sweeping from a dark version through it to a pale one."
    : "The rest of the palette is worked out from this colour, and the ramp travels to red "
      + "unless the colour is already there."

  const accents = pick("div[data-tint]")
  accents.replaceChildren()
  if (tinted) {
    if (!family) family = familyOf(config.tint)
    accents.append(familyTabs(), swatches())
  }
  preview()
}

/** Which family of accents the picker is showing: four rows of twelve, one at a time. */
function familyTabs() {
  return el("div", { className: "tabs" }, Object.keys(caps.accents || {}).map((name) => {
    const tab = el("button", { type: "button", textContent: titleCase(name),
                               "aria-pressed": String(name === family) })
    tab.onclick = () => { family = name; renderTint() }   // a look, not yet a change
    return tab
  }))
}

/** A swatch is the colour that will be used, not a stand-in for it. */
function swatches() {
  return el("div", { className: "swatches" }, ((caps.accents || {})[family] || []).map(
    (accent) => {
      const shown = `rgb(${accent.join(", ")})`
      const chip = el("button", { type: "button", title: shown,
                                  "aria-pressed": String(String(config.tint) === String(accent)) })
      chip.style.background = shown
      chip.onclick = () => { config.tint = accent.slice(); markDirty(); renderTint() }
      return chip
    }))
}

// Where each of the preview's three bars sits on the ramp, in row order. The readings printed
// beside them are in the HTML.
const PREVIEW_BARS = [0.62, 0.46, 0.78]

const rgb = (parts) => `rgb(${parts.join(", ")})`

async function preview() {
  const query = new URLSearchParams({ theme: config.theme || "dark" })
  if ((caps.tinted || {})[config.theme]) {
    query.set("accent", (config.tint || []).join(","))
    query.set("second", config.accent_b || "same")
  }
  const mine = ++previewWanted
  let shown
  try {
    shown = await api(`/api/theme?${query}`)
  } catch (error) {
    return
  }
  if (mine !== previewWanted) return

  const palette = shown.palette
  const node = pick("main figure")
  const set = (name, parts) => node.style.setProperty(name, rgb(parts))
  set("--pv-bg", palette.bg)
  set("--pv-panel", palette.panel)
  set("--pv-ink", palette.ink)
  set("--pv-dim", palette.dim)
  // The header's rule and the current pip, which on the badge take the second accent.
  set("--pv-accent", palette.accent_b || palette.accent)
  set("--pv-grid", palette.grid)
  paintDial(node.querySelector("strong"), palette, config.gauge_fill)
  $("accentbchip").style.background = rgb(palette.accent_b || palette.accent)
  node.querySelectorAll("dd").forEach((bar, index) => {
    const at = PREVIEW_BARS[index]
    bar.style.setProperty("--at", `${at * 100}%`)
    bar.style.setProperty("--bar", rgb(rampAt(palette.ramp, at)))
  })
}

// The gauge the preview draws: a 270 degree sweep from the lower left, filled to the reading
// the panel shows, in the ramp's own colours.
const PV_SWEEP = 0.75                   // of a whole turn, the gap centred on the bottom
const PV_READING = 0.635                // the reading printed inside it

// How faint the part past the reading is when the whole ramp is shown, matching
// draw.TRACK_ALPHA on the badge. A gradient there is drawn over the page rather than
// composited, so the colours are mixed towards it here.
const PV_TRACK_ALPHA = 32 / 255

function paintDial(dial, palette, fill) {
  if (!dial) return
  const faint = (parts) => rgb(parts.map((part, index) =>
    Math.round(part * PV_TRACK_ALPHA + palette.bg[index] * (1 - PV_TRACK_ALPHA))))
  const bg = rgb(palette.bg)
  const filled = PV_SWEEP * PV_READING
  const at = (part) => `${(part * 100).toFixed(1)}%`
  const reached = rampAt(palette.ramp, PV_READING)

  const stops = []
  if (fill === "ramp") {
    // The whole ramp laid round the arc, as the conical gradient does it: a colour's place is
    // its place on the ramp, so the sweep ends at the reading's own colour and the rest of the
    // ramp shows faintly beyond it.
    for (const [position, parts] of palette.ramp) {
      if (position < PV_READING) stops.push(`${rgb(parts)} ${at(position * PV_SWEEP)}`)
    }
    stops.push(`${rgb(reached)} ${at(filled)}`)
    stops.push(`${rgb(palette.ink)} ${at(filled)} ${at(filled + 0.004)}`)
    stops.push(`${faint(reached)} ${at(filled + 0.004)}`)
    for (const [position, parts] of palette.ramp) {
      if (position > PV_READING) stops.push(`${faint(parts)} ${at(position * PV_SWEEP)}`)
    }
  } else {
    stops.push(`${rgb(reached)} ${at(filled)}`)
    stops.push(`${rgb(palette.ink)} ${at(filled)} ${at(filled + 0.004)}`)
    stops.push(`${rgb(palette.grid)} ${at(filled + 0.004)}`)
  }
  stops.push(`${rgb(palette.grid)} ${at(PV_SWEEP)}`, `${bg} ${at(PV_SWEEP)}`)
  // Built here and not in the sheet: a stop list cannot be handed to a gradient through a
  // custom property and then given positions of its own - the declaration parses as invalid
  // and the whole gauge disappears.
  dial.style.background = `radial-gradient(closest-side, ${bg} 74%, transparent 75%), `
    + `conic-gradient(from 225deg, ${stops.join(", ")})`
}

function rampAt(stops, at) {
  let [low, first] = stops[0]
  for (const [position, colour] of stops) {
    if (at <= position) {
      const span = position - low
      const part = span <= 0 ? 0 : (at - low) / span
      return first.map((from, index) => Math.round(from + (colour[index] - from) * part))
    }
    [low, first] = [position, colour]
  }
  return stops[stops.length - 1][1]
}

// -- which badge -----------------------------------------------------------
//
// One layout per badge, and a default for a badge that has not been given its own. The picker
// in the header says which of them the page is editing; `null` is the default.

const REMEMBERED = "statsbadge.whose"

function configPath(path) {
  const base = path || "/api/config"
  return whose ? `${base}?badge=${encodeURIComponent(whose)}` : base
}

function badgeName(id) {
  return (badges[id] && badges[id].name) || id
}

/** Which badge to open on: the one last edited if it is still paired, else the first. */
function pickBadge() {
  let last = null
  try { last = window.localStorage.getItem(REMEMBERED) } catch (error) { /* private mode */ }
  if (last && badges[last]) return last
  return Object.keys(badges)[0] || null
}

function remember(id) {
  try {
    if (id) window.localStorage.setItem(REMEMBERED, id)
    else window.localStorage.removeItem(REMEMBERED)
  } catch (error) { /* nothing to remember it in */ }
}

function renderWhose() {
  const ids = Object.keys(badges)
  const select = pick("header label select")
  select.replaceChildren(
    ...ids.map((id) => el("option", { value: id, textContent: badgeName(id) })),
    el("option", { value: "",
                   textContent: ids.length
                     ? "Default, for any other badge"
                     : "No badge paired yet" }))
  select.value = whose || ""
  select.onchange = () => switchTo(select.value).catch((error) => toast(error.message, true))

  const own = whose && badges[whose] && badges[whose].configured
  pick("header > small").textContent = whose && !own
    ? "on the default layout, until you save"
    : (!whose && ids.length ? "what a newly paired badge draws" : "")
  $("forget").disabled = !whose
  $("forget").onclick = () => forgetBadge(whose).catch((error) => toast(error.message, true))
}

/** Load another badge's layout into the page. */
async function switchTo(id) {
  if (dirty && !window.confirm("Discard the unsaved changes to this badge?")) {
    renderWhose()                       // put the picker back on the badge being edited
    return
  }
  whose = id || null
  remember(whose)
  config = await api(configPath())
  dirty = false
  $("save").disabled = true
  renderWhose()
  renderPages()
  renderSettings()
  renderLook()
  renderBadges()
}

async function forgetBadge(id) {
  if (!window.confirm(`Forget ${badgeName(id)}? Its layout goes with it.`)) return
  await api(`/api/badges/${id}`, { method: "DELETE" })
  badges = await api("/api/badges")
  dirty = false                         // the badge those edits belonged to is gone
  await switchTo(Object.keys(badges)[0] || "")
  toast("Forgotten")
}

/** Page ids made this badge's own.
 *
 * An extension keys what it does per page by page id - which city a clock page shows - so two
 * badges must not carry the same one. Done where a badge stops drawing the default and gets a
 * layout of its own, and derived from the badge id so switching back and forth is stable. */
function ownIds(pages, badgeId) {
  const tag = badgeId.slice(0, 4)
  return pages.map((page) => (String(page.id).endsWith(`-${tag}`)
    ? page : { ...page, id: `${page.id}-${tag}` }))
}

function renderBadges() {
  const list = $("badges").querySelector("menu")
  const ids = Object.keys(badges)
  if (!ids.length) {
    list.replaceChildren(el("li", null, el("p", {
      textContent: "None paired. Use the USB installer, or pair over the network." })))
    return
  }
  list.replaceChildren(...ids.map((id) => {
    // A button, since picking a badge is what a row is for and a keyboard has to reach it.
    const row = el("button", { type: "button", "aria-current": id === whose ? "true" : null },
                   el("span", { textContent: badges[id].name || id }),
                   el("small", { textContent: badges[id].configured ? "own layout" : "default" }),
                   el("code", { textContent: id }))
    row.onclick = () => switchTo(id).catch((error) => toast(error.message, true))
    return el("li", null, row)
  }))
}

// -- pairing ---------------------------------------------------------------
//
// Off until asked for. Badges then ask to be let in and show a code; approving one here is
// what pairs it.

let pairingPoll = null

async function startPairing() {
  await api("/api/pair", { method: "POST" })
  await watchPairing(true)
}

async function stopPairing() {
  await api("/api/pair", { method: "DELETE" })
  await watchPairing()
  toast("Pairing closed")
}

async function answer(requestId, approve) {
  const result = await api(`/api/enrol/${requestId}/${approve ? "approve" : "deny"}`,
                           { method: "POST" })
  toast(approve ? "Badge paired" : "Denied")
  badges = await api("/api/badges")
  // Straight to the badge just paired, since configuring it is what comes next - unless there
  // are edits for another one, which are not worth interrupting for.
  if (approve && result.approved && !dirty) {
    await switchTo(result.approved)
  } else {
    renderWhose()
    renderBadges()
  }
  watchPairing()
}

function pendingList(pending) {
  return el("ul", null, pending.map((request) => {
    const buttons = [["Approve", true, "primary small"], ["Deny", false, "small danger"]]
      .map(([label, approve, className]) => {
        const button = el("button", { type: "button", className, textContent: label })
        button.onclick = () => answer(request.request_id, approve)
          .catch((error) => toast(error.message, true))
        return button
      })
    return el("li", null,
              el("span", { textContent: request.name }),
              el("samp", { textContent: request.code }),
              el("code", { textContent: request.badge_id }),
              el("div", null, buttons))
  }))
}

async function watchPairing(announce) {
  if (pairingPoll) {
    clearInterval(pairingPoll)
    pairingPoll = null
  }
  const panel = pick("dialog")
  const button = $("pair")

  const paint = (state, pending) => {
    if (!state.active) {
      panel.close()
      button.textContent = "Pair a badge…"
      button.onclick = () => startPairing().catch((error) => toast(error.message, true))
      return false
    }
    button.textContent = "Stop pairing"
    button.onclick = () => stopPairing().catch((error) => toast(error.message, true))
    panel.replaceChildren(
      el("p", { textContent: "On the badge: launch Stats, press B to set up, and pick "
                             + `${(state.hosts || []).join(" / ")}:${state.port}` }),
      el("p", { textContent: `closes in ${state.expires_in}s` }),
      pending.length ? el("p", { textContent: "Approve the one whose code matches." }) : null,
      pending.length ? pendingList(pending) : null)
    if (!panel.open) panel.show()
    return true
  }

  let state = await api("/api/pair")
  let pending = (await api("/api/enrol")).pending
  if (!paint(state, pending)) return
  if (announce) toast(`Pairing open for ${state.expires_in}s`)

  pairingPoll = setInterval(async () => {
    try {
      state = await api("/api/pair")
      pending = (await api("/api/enrol")).pending
      if (!paint(state, pending)) {
        clearInterval(pairingPoll)
        pairingPoll = null
      }
    } catch (error) {
      clearInterval(pairingPoll)
      pairingPoll = null
    }
  }, 1000)
}

// -- live ------------------------------------------------------------------

const PERCENT = ["pct", "swap_pct", "mem_pct", "fan_pct", "battery_pct"]

// Everything on a frame that is not a group of readings. `peaks` is shown, being useful to
// see, but it is scale rather than a reading and comes and goes with what has been measured,
// so it is left out of the signature below.
const FRAME_SCALARS = ["v", "t", "seq", "layout_rev"]
const FRAME_META = FRAME_SCALARS.concat(["peaks"])

// Which groups the last frame carried. A source that finds out what it can report only once it
// is running - the Cloudflare one lists an account's domains after it is given a token - shows
// up here before the pickers know anything about it, and this is already being fetched every
// second, so it costs nothing to notice.
let liveGroups = ""

async function renderLive() {
  let frame
  try {
    frame = await api("/api/stats")
  } catch (error) { return }

  const shape = Object.keys(frame).filter((key) => !FRAME_META.includes(key)).join(",")
  if (shape !== liveGroups && !dirty) {
    liveGroups = shape
    refreshCaps().catch(() => {})
  }

  const node = $("live")
  if (node.closest("main > section").hidden) return

  const groups = []
  for (const group of Object.keys(frame)) {
    if (FRAME_SCALARS.includes(group)) continue
    const items = Array.isArray(frame[group]) ? frame[group] : [frame[group]]
    for (const [index, item] of items.entries()) {
      if (!item || !Object.keys(item).length) continue
      groups.push(liveGroup(items.length > 1 ? `${group} ${index}` : group, item))
    }
  }
  node.replaceChildren(node.querySelector("h2"), ...groups)
}

function liveGroup(name, item) {
  const rows = []
  for (const key of Object.keys(item)) {
    const value = item[key]
    const shown = el("dd")
    if (value === null || value === undefined) {
      shown.textContent = "unknown"
    } else if (Array.isArray(value)) {
      shown.textContent = value.map((each) => Math.round(each)).join(" ")
    } else if (typeof value === "number") {
      shown.textContent = Number.isInteger(value) ? value : value.toFixed(1)
    } else {
      shown.textContent = String(value)
    }
    if (PERCENT.includes(key) && typeof value === "number") {
      shown.style.setProperty("--at", `${Math.max(0, Math.min(100, value))}%`)
    }
    rows.push(el("dt", { textContent: key }), shown)
  }
  return el("section", null, el("h3", { textContent: name }), el("dl", null, rows))
}

function renderSources() {
  $("sources").querySelector("ul").replaceChildren(...caps.sources.map((source) => {
    // A fault goes underneath what a source provides rather than in place of it, and one it
    // has recovered from is a footnote: an upstream 503 an hour ago should not still be a
    // source's whole description.
    const row = el("li", { className: source.last_fault ? "faulty" : null,
                           textContent: `${source.name} → ${source.provides.join(", ") || "nothing"}` })
    if (source.last_fault) {
      row.append(el("small", { textContent: source.last_fault }))
    } else if (source.faults) {
      row.append(el("small", { textContent:
        ` recovered, ${source.faults} fault${source.faults === 1 ? "" : "s"} so far` }))
    }
    return row
  }))
}

// -- keeping up with the host ----------------------------------------------

/** What an extension offers, as one string, so a change in it can be noticed cheaply. */
function capsSignature() {
  // What each source is complaining about, and not how many times: a source failing every poll
  // counts one a second, and a signature that moved with it would redraw the whole page -
  // `/api/preview` and all - once a second for as long as it was broken.
  const faults = (caps.sources || []).map((source) => [source.name, source.last_fault])
  return JSON.stringify([caps.available, caps.extension_settings, caps.graphed,
                         caps.group_source, caps.extension_pages, faults])
}

/** Refetch capabilities and redraw if what the host offers has changed.
 *
 * An extension does not always know what it provides at startup: a token pasted in the browser
 * is what lets the Cloudflare one list an account's domains, and it lists them on a thread of
 * its own a moment after the save lands. */
async function refreshCaps() {
  // Never over unsaved work: these renderers rebuild their inputs from `config`, and a redraw
  // part way through typing an API key moves the caret out from under it.
  if (dirty) return false
  let fresh
  try {
    fresh = await api("/api/capabilities")
  } catch (error) { return false }
  const before = capsSignature()
  caps = fresh
  if (capsSignature() === before) return false
  offerExtensionPages()
  renderPages()
  renderSettings()
  renderSources()
  return true
}

/** Look again for a while, since what a save sets off does not finish inside the reply.
 *
 * The Cloudflare source is handed its token synchronously and then goes to the network, so the
 * domains land seconds later. Backing off rather than polling: this is watching for one thing
 * to arrive, not keeping a display current. */
async function refreshCapsSoon(delays = [400, 1200, 3000, 6000]) {
  for (const delay of delays) {
    await new Promise((wake) => setTimeout(wake, delay))
    if (await refreshCaps()) return
  }
}

// -- boot ------------------------------------------------------------------

async function save() {
  try {
    // A badge saving for the first time stops drawing the default and gets pages of its own.
    if (whose && badges[whose] && !badges[whose].configured) {
      config.pages = ownIds(config.pages, whose)
    }
    const result = await api(configPath(), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    })
    config.rev = result.rev
    if (whose && badges[whose]) badges[whose].configured = true
    dirty = false
    $("save").disabled = true
    toast(`Saved. ${whose ? badgeName(whose) : "A badge on the default"} `
          + `will pick up revision ${result.rev}.`)
    // Settings reach the sources on the save, and what a source does with them may be to go and
    // find out what it can offer. Not awaited: the save is done either way.
    refreshCapsSoon().catch(() => {})
    renderWhose()
    renderPages()
    renderBadges()
  } catch (error) {
    toast(error.message, true)
  }
}

async function boot() {
  bindTabs()
  try {
    [caps, badges] = await Promise.all([api("/api/capabilities"), api("/api/badges")])
    whose = pickBadge()
    config = await api(configPath())
  } catch (error) {
    document.body.replaceChildren(el("p", {
      textContent: `Cannot reach the server: ${error.message}` }))
    return
  }
  const sys = (await api("/api/stats")).sys || {}
  pick("#local p").textContent =
    `${sys.host || "host"} · ${sys.os || ""} · ${sys.cpu_name || ""}`

  offerExtensionPages()
  renderWhose()
  renderPages()
  renderSettings()
  renderLook()
  renderSources()
  renderBadges()
  renderLive()

  $("save").onclick = save
  const form = pick("main form")
  form.onsubmit = (event) => {
    event.preventDefault()
    // At the top: added at the bottom it lands off the end of a long list, and the first thing
    // anyone does with a new page is configure it.
    config.pages.unshift(newPage(form.querySelector("select").value))
    expanded.add(config.pages[0].id)
    markDirty()
    renderPages()
  }
  // Picks up a window opened by `statsbadge pair` or a previous page load.
  watchPairing().catch(() => {})

  setInterval(renderLive, 1000)
  window.onbeforeunload = () => (dirty ? "You have unsaved changes." : undefined)
}

boot()
