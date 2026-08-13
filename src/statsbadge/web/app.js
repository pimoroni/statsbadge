// The config UI. Edits a config object, PUTs it, and the badge picks up the new revision on
// its next poll.
//
// Everything on the page belongs to one badge, chosen in the header: `whose` is its id, or
// null for the layout a badge draws before anything is saved for it.

const $ = (id) => document.getElementById(id)
const pick = (selector) => document.querySelector(selector)
const all = (selector) => [...document.querySelectorAll(selector)]

/** An element, its properties, and whatever goes inside it. A key with a dash is set as an
 * attribute, `aria-` and `data-` having no matching property. */
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
  // The badge's vitals, with no fields, all of it coming from the badge itself.
  badge: { one: null, many: null, max: 0, label: "" },
}

async function api(path, options) {
  const response = await fetch(path, options)
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error((body && body.error) || response.statusText)
  return body
}

// Words that stay lowercase inside a title. The first word is always capitalised.
const MINOR = new Set(["a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on",
                       "or", "the", "to", "with"])

/** Title case for a name that arrives as a word or a slug. What an extension declared is left
 * as it declared them, "kmh" not being "Kmh". */
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
// flow across it. The nav and the sheets are in the same order, which is what pairs them.

const REMEMBERED_TAB = "statsbadge.tab"

function showSheet(wanted) {
  const tabs = all("header nav button")
  const sheets = all("main > section")
  tabs.forEach((tab, index) => {
    if (index === wanted) tab.setAttribute("aria-current", "page")
    else tab.removeAttribute("aria-current")
    sheets[index].hidden = index !== wanted
  })
  try {
    window.localStorage.setItem(REMEMBERED_TAB, wanted)
  } catch (error) {
    // private mode
  }
}

function bindTabs() {
  const tabs = all("header nav button")
  tabs.forEach((tab, index) => { tab.onclick = () => showSheet(index) })
  let opening = 0
  try {
    opening = Number(window.localStorage.getItem(REMEMBERED_TAB))
  } catch (error) {
    // private mode
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
  // Lists are left out of even this. fmt prints one verbatim, so a grid cell handed
  // cpu.cores shows a row of Python. A message likewise.
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

/** Refs holding a message and not a reading: a post, a mention, a headline. */
function itemRefs() {
  return availableRefs().filter((ref) => itemFields().includes(ref.split(".")[1]))
}

/** What a notifications page can hold: the messages first, then anything countable. */
function notifyRefs() {
  return [...new Set(itemRefs().concat(numericRefs()))]
}

/** Refs a gauge can place a needle on: a percentage, or something with a top end.
 *
 * Being a number is not enough. Uptime is a number, and a ring drawn from it stays empty
 * whatever the machine has been doing, there being no full scale to fill against. */
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
 * reading that holds still, as against one nobody is recording. */
function seriesRefs() {
  const kept = caps.graphed || []
  const withHistory = numericRefs().filter((ref) => kept.includes(ref))
  return withHistory.length ? withHistory : numericRefs()
}

/** Refs that are a list, for the kinds that draw one lane or bar per element. */
function listRefs() {
  return availableRefs().filter((ref) => listFields().includes(ref.split(".")[1]))
}

// Which pool each slot draws from. "gauge" needs a top end, "series" only a number since it
// scales itself from the data, "list" one value per element, "notify" a message or a number,
// and "any" prints whatever it is given.
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

// What the sources this host measures itself with are listed under. Held apart from
// "System", the `sys` group's name, which would head a list with its last entry.
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
 * One list of every reading was navigable while a host only measured itself. An extension
 * contributes a group per thing it watches, a domain apiece for an account with six of
 * them, so the source is picked first and the metric list is only ever that source's.
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

  // Grouped under whoever provides them. An extension watching six domains would
  // otherwise bury this host's readings in a list of unexplained names.
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
  // Told which ref, and not reading it back off the select, so the first paint does not
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

/** The field slots a kind has. An extension's page declares them, since only its renderer
 * reads `fields` at all. The clock face draws from its groups and ignores them, so offering
 * seven pickers offered seven controls that did nothing. */
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

// Kinds whose renderer reads a page's full scale, held to the renderers by a test.
const SCALED = new Set(["dial", "dials", "rings", "radar", "trend", "bars", "graph",
                        "waterfall"])

// Without it a reading is scaled by the busiest the host has seen: wrong for a count.
const MAX_SETTING = { key: "max", label: "Full scale", type: "number", min: 0, step: "any",
                      placeholder: "automatic" }

function pageCard(page, index) {
  const shape = shapeFor(page.kind)
  const open = expanded.has(page.id)
  const settings = (caps.extension_page_settings || {})[page.kind] || []

  // A page starts out titled after its kind, and an extension's ships titled after itself,
  // so the name is only worth a second look once somebody has edited it.
  const titled = el("span", { className: "given" })
  const showTitle = () => {
    const given = (page.title || "").trim()
    titled.textContent = given.toLowerCase() === page.kind.toLowerCase()
      ? ""
      : titleCase(given)
  }
  showTitle()

  const titleId = `page${++controlSerial}`
  const title = el("input", { type: "text", id: titleId, value: page.title || "" })
  // The heading is right above the field, so it keeps up with the typing.
  title.oninput = () => { page.title = title.value; showTitle(); markDirty() }

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

  // The heading is the handle: a card holds a text field and two pickers, and dragging it
  // from anywhere meant dragging it out from under whichever one was being used.
  const kind = el("h3", { title: "Drag to reorder" },
                  el("span", { className: "kind", textContent: page.kind }), titled)
  const item = el("li", null, el("header", null, kind, toggle, remove))

  if (open) {
    item.append(el("label", { htmlFor: titleId, textContent: "Title" }), title,
                slotList(page, shape),
                ...(SCALED.has(page.kind) ? settingRow(page, MAX_SETTING) : []),
                ...settings.flatMap((setting) => settingRow(page, setting)),
                el("footer", null, moveButtons(index), addSlot(page, shape)))
  } else {
    const refs = shape.one ? [page[shape.one]] : (page[shape.many] || [])
    const named = refs.filter(Boolean).map(fieldLabel)
    const extra = settings.map((setting) => page[setting.key]).filter(Boolean)
    if (SCALED.has(page.kind) && page.max) extra.push(`full scale ${page.max}`)
    item.append(el("p", { textContent: named.concat(extra).join(", ") || "nothing chosen" }))
  }

  reorderable(item, config.pages, index, { tag: "page", along: "x", handle: kind })
  return item
}

/** Drag one of `items` to another place in it.
 *
 * The tag names the list a drag came from. A page card is draggable and so are the rows
 * inside it, and without one a row dropped on its card would reorder the pages. The events
 * are stopped on the way up for the same reason.
 *
 * `along` is the axis the list runs, setting which half of an item counts as before it and
 * which edge is marked. A `handle` has to be held for the drag to start.
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
  // Only on the way out of the item. Moving over a select inside it is a leave too.
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

function poolFor(name) {
  const refs = (POOLS[name] || POOLS.any)()
  return refs.length ? refs : availableRefs()
}

/** The readings a page is made of, one row per slot. */
function slotList(page, shape) {
  const rows = []

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
                   el("span", { className: "grip", textContent: "⋮" }),
                   refSelect(ref, poolFor(shape.manyPool), (value) => { current[slot] = value }),
                   drop)
    reorderable(row, current, slot, { tag: "slot", along: "y" })
    rows.push(row)
  })

  return el("ol", null, rows)
}

/** Another slot, while the kind has room for one. */
function addSlot(page, shape) {
  const current = shape.many ? page[shape.many] || [] : []
  if (!shape.many || current.length >= shape.max) return null
  const add = el("button", { type: "button", className: "small add",
                             textContent: `Add ${singular(shape.label).toLowerCase()}` })
  add.onclick = () => {
    page[shape.many] = current.concat([poolFor(shape.manyPool)[0]])
    markDirty()
    renderPages()
  }
  return add
}

/** The two ways to move a page that are not dragging it. The list reads across and then
 * down, so left and right cover it. */
function moveButtons(index) {
  return [["←", "Move left", index - 1, index === 0],
          ["→", "Move right", index + 1, index === config.pages.length - 1]]
    .map(([glyph, label, to, ends]) => {
      const button = el("button", { type: "button", className: "small", textContent: glyph,
                                    title: label, "aria-label": label, disabled: ends })
      button.onclick = () => {
        config.pages.splice(to, 0, config.pages.splice(index, 1)[0])
        markDirty()
        renderPages()
      }
      return button
    })
}

function newPage(kind) {
  const suffix = Date.now().toString(36).slice(-4)
  const offered = (caps.extension_pages || []).find((page) => page.kind === kind)
  if (offered) {
    // An extension declares its page: take the fields and title it shipped with, the
    // shape being its badge module's business.
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
 * groups it declares. */
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
    const node = pick('main p[role="status"]')
    node.textContent = "Not shown on the badge, because this host reports no data for "
      + `them: ${dropped.join(", ")}`
    node.hidden = !dropped.length
  } catch (error) {
    // advisory
  }
}

// -- extension settings ----------------------------------------------------

/** A box per installed extension, titled with its name, holding whatever it can be told.
 *
 * Every discovered one, and not only those with settings. An extension that asks to be
 * told nothing still gets a box, and one that failed to import is reported here instead of
 * showing up as a page that never draws. */
function renderSettings() {
  const schema = caps.extension_settings || {}
  const installed = caps.extensions || []
  config.settings = config.settings || {}
  // The lead-in written into the section, kept across a redraw of everything under it.
  const intro = $("extensions").querySelector("p")
  $("extensions").replaceChildren(
    ...(intro ? [intro] : []),
    el("div", { className: "configured" },
       ...installed.map((extension) => extensionBox(extension, schema[extension.name] || []))),
    catalogueBox())
}

// Which extension boxes are open. Collapsed to start with, as a page card is: the column
// is then a list of what is installed, and not a wall of every setting at once.
const openExtensions = new Set()

/** What the catalogue calls it, since a package name is not a heading. */
function displayName(name) {
  const listed = ((catalogue && catalogue.offered) || []).find((e) => e.name === name)
  return (listed && listed.title) || name
}

/** Under the title, not in it: what you would type, and which release is here. */
function givenName(name, version) {
  const given = `statsbadge-${name}`
  return version ? `${given} @ ${version}` : given
}

// What /api/extensions last said: the published list, each entry's state, and whether
// this install can rebuild itself.
let catalogue = null
// Names with a request in flight, so a second click cannot start a second rebuild.
const installing = new Set()

async function refreshCatalogue() {
  try {
    catalogue = await api("/api/extensions")
  } catch (error) {
    catalogue = null
  }
  renderSettings()
}

// What has a newer release, by short name. Kept apart from the catalogue: asking an index
// takes seconds, and the list draws without it.
let behind = {}

async function refreshOutdated() {
  let found
  try {
    found = await api("/api/extensions/outdated")
  } catch (error) {
    return
  }
  behind = {}
  for (const entry of found.outdated || []) {
    behind[entry.name.replace(/^statsbadge-/, "")] = entry.latest
  }
  renderSettings()
}

/** The published extensions, each with the one button that applies to it.
 *
 * A fixed list: PyPI cannot be asked which packages are statsbadge extensions. The box
 * below takes any requirement, so a third-party one goes in there. */
function catalogueBox() {
  const box = el("section", { className: "offer" },
                 el("h3", { textContent: "Extensions" }))
  if (!catalogue) {
    box.append(el("p", { textContent: "Could not read the list of extensions." }))
    return box
  }
  if (!catalogue.manageable) {
    box.append(el("p", { textContent:
      `Installing from here needs a uv tool install. This one runs from ${catalogue.prefix},`
      + " so add them with uv pip install instead." }))
  }
  box.append(el("ul", { className: "catalogue" }, ...catalogue.offered.map(offerRow)))
  box.append(freeformForm())
  return box
}

function offerRow(entry) {
  const notes = [givenName(entry.name, entry.version)]
  if (entry.needs) notes.push(`needs ${entry.needs}`)
  // The badge gets code over USB alone; /v1 carries readings and a layout.
  if (entry.page) notes.push("includes badge page")
  if (entry.installed && !entry.managed) notes.push("installed by the environment")
  if (entry.disabled) notes.push("switched off")
  if (entry.installed && entry.managed && !entry.asked) {
    notes.push("installed, but not on the list")
  }
  if (entry.asked && !entry.installed) notes.push("asked for, but not installed")
  if (behind[entry.name]) notes.push(`${behind[entry.name]} is out`)

  const summary = [entry.summary, notes.join(" · ")].filter(Boolean)
  return el("li", null,
            el("div", null,
               el("strong", { textContent: entry.title || entry.name }),
               ...summary.map((text) => el("small", { textContent: text }))),
            el("div", null,
               ...(behind[entry.name] && entry.managed ? [updateButton(entry)] : []),
               offerButton(entry)))
}

function updateButton(entry) {
  const busy = installing.has(entry.name)
  return el("button", {
    type: "button",
    className: "primary",
    textContent: busy ? "Working..." : `Update to ${behind[entry.name]}`,
    disabled: busy || !catalogue.manageable,
    onclick: () => changeExtension("upgrade", entry.name),
  })
}

function offerButton(entry) {
  const busy = installing.has(entry.name)
  // Whatever the environment installed cannot be uninstalled from here, so the most on
  // offer for one is to stop loading it.
  if (entry.installed && !entry.managed) {
    const verb = entry.disabled ? "enable" : "disable"
    return el("button", {
      type: "button",
      textContent: busy ? "Working..." : (entry.disabled ? "Enable" : "Disable"),
      title: "Installed by the environment, so it can only be switched off here",
      disabled: busy,
      onclick: () => changeExtension(verb, entry.name),
    })
  }
  // Keyed on what is here: an entry listed but absent is put back by installing it, the
  // repair `ext add` makes.
  const verb = entry.installed ? "remove" : "add"
  return el("button", {
    type: "button",
    textContent: busy ? "Working..." : (verb === "add" ? "Install" : "Remove"),
    disabled: busy || !catalogue.manageable,
    onclick: () => changeExtension(verb, entry.name),
  })
}

function freeformForm() {
  const field = el("input", { type: "text", name: "requirement",
                              placeholder: "another extension, or any pip requirement" })
  const form = el("form", { onsubmit: (event) => {
    event.preventDefault()
    const asked = field.value.trim()
    if (!asked) return
    field.value = ""
    changeExtension("add", asked)
  } },
                  field,
                  el("button", { type: "submit", textContent: "Install",
                                 disabled: !catalogue.manageable }))
  form.setAttribute("aria-label", "Install an extension by name")
  return form
}

/** Install or remove, then take up the result. A rebuild resolves the whole environment,
 * so this can sit there for a minute; the button says so and refuses a second click. */
async function changeExtension(verb, name) {
  installing.add(name)
  renderSettings()
  let done
  try {
    done = await api("/api/extensions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [verb]: [name] }),
    })
  } catch (error) {
    toast(String(error.message || error), true)
    installing.delete(name)
    renderSettings()
    return
  }
  installing.delete(name)
  if (!done.ok) {
    toast(done.why || "could not do that", true)
  } else {
    // Only where it took: a copy in statsbadge's own environment survives a build, and
    // nothing at all happened where there was nothing to do.
    if (!(done.stuck || []).length && !done.nothing) {
      toast({ add: `Installed ${name}`, remove: `Removed ${name}`,
              upgrade: `Updated ${name}`, disable: `Switched ${name} off`,
              enable: `Switched ${name} on` }[verb])
    }
    for (const note of done.unpinned || []) toast(note)
    // Already imported code stays imported until the process goes round again.
    for (const name of done.restart || []) {
      toast(`Restart statsbadge to run the new ${name}`)
    }
    // Installed into the environment itself, where a build beside the config cannot
    // reach it.
    for (const entry of done.stuck || []) {
      toast(`Unable to uninstall ${entry.name}. It is installed in statsbadge's own `
            + "environment, so whatever put it there has to take it out.", true)
    }
    for (const entry of done.shadowed || []) {
      toast(`${entry.name} is already installed in statsbadge's own environment. `
            + "That copy is the one that runs.")
    }
    if (verb !== "remove" && (done.needs_usb || []).includes(name)) {
      toast("Run statsbadge install to push its page to the badge")
    }
  }
  await refreshCatalogue()
  await refreshOutdated()
  await refreshCaps()
}

function extensionBox(extension, settings) {
  const state = el("p")
  if (extension.error) {
    state.className = "bad"
    state.textContent = extension.error
  } else if (extension.available === false) {
    state.textContent = "Installed, but not usable on this host."
  } else {
    const parts = [givenName(extension.name, extension.version)]
    if (extension.provides.length) parts.push(extension.provides.join(", "))
    if (extension.badge_module) parts.push("includes badge page")
    state.textContent = parts.join(" · ")
  }

  const open = openExtensions.has(extension.name)
  const head = el("h3", null,
                  el("span", { textContent: displayName(extension.name) }))
  const box = el("section", null, el("header", null, head), state)
  if (!settings.length) return box

  const toggle = el("button", { type: "button", textContent: open ? "▾" : "▸",
                                title: open ? "Collapse" : "Configure",
                                "aria-expanded": String(open) })
  toggle.onclick = () => {
    if (open) openExtensions.delete(extension.name)
    else openExtensions.add(extension.name)
    renderSettings()             // not markDirty: opening a box changes nothing
  }
  box.firstChild.append(toggle)
  if (!open) return box

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
 * Masked and not hidden, since "not set" and "set to the wrong one" have to be told
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
  } else if (setting.type === "number") {
    // The bounds are the browser's to enforce while it is being typed; the host clamps what
    // arrives, a typed value being able to leave the field out of range.
    input = el("input", { type: "number", id, min: setting.min, max: setting.max,
                          step: setting.step, placeholder: setting.placeholder,
                          value: current === null || current === undefined ? "" : current })
    input.oninput = () => {
      stored[setting.key] = input.value === "" ? null : Number(input.value)
      markDirty()
    }
  } else if (setting.type === "choice") {
    input = el("select", { id }, (setting.options || []).map(
      (option) => el("option", { value: option, textContent: option,
                                 selected: option === current })))
    input.onchange = () => { stored[setting.key] = input.value; markDirty() }
  } else {
    input = el("input", { type: "text", id,
                          value: current === null || current === undefined ? "" : current })
    if (setting.secret) {
      // Shown in full, editing these being about reading one back and replacing
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
  // What it is counted in, where saying so in the hint would be saying it twice.
  return setting.unit
    ? [label, input, el("small", { textContent: setting.unit })]
    : [label, input]
}

// -- look and buttons ------------------------------------------------------

function renderLook() {
  const theme = $("theme")
  // Grouped by mode: which of them suit a lit room is the first thing anybody is choosing
  // between, and a flat list of twenty-two had the pairs scattered through it.
  theme.replaceChildren(...[["dark", "Dark"], ["light", "Light"]].map(([mode, heading]) =>
    el("optgroup", { label: heading }, caps.themes
      .filter((entry) => entry.mode === mode)
      .map((record) => el("option", { value: record.name, selected: record.name === config.theme,
                                      textContent: record.label })))))
  theme.onchange = () => { config.theme = theme.value; markDirty(); renderTint() }
  renderTint()

  bindRange("interval", "interval_ms", (value) => `${value} ms`)
  bindRange("brightness", "brightness", (value) => `${value}%`, 100)
  bindRange("points", "graph_points", (value) => `${value}`)
  // Zero is off, and the readout says so instead of showing a time nothing happens at.
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

/** Off, the backlight's level, or a reading for the lights to follow. The stored value is
 * false, true, or a field ref.
 *
 * Which of the three comes first, and the reading is then picked the way a page picks one -
 * by source and then by metric - rather than from one list of every reading this host has. */
function renderCaseLights() {
  // The flag has always stored "theme", from when a palette named the level.
  const mode = $("caselights")
  const stored = config.caselights
  const chosen = stored === true ? "theme" : stored ? "reading" : "off"
  const refs = numericRefs()
  const offered = [["off", "Off"], ["theme", "Follow the Backlight"]]
  // Nothing numeric to follow means nothing to offer following. A stored ref still counts,
  // so a host that has stopped sending one does not silently drop it.
  if (refs.length || chosen === "reading") offered.push(["reading", "Follow a Reading"])
  mode.replaceChildren(...offered.map(([value, text]) =>
    el("option", { value, textContent: text, selected: value === chosen })))

  // Held on to while the mode moves off it and back, so leaving and returning lands on the
  // reading that was picked rather than on the first in the list.
  let following = typeof stored === "string" ? stored : refs[0]

  const row = $("caselightref")
  row.hidden = chosen !== "reading"
  row.replaceChildren(...refSelect(following, refs, (ref) => {
    following = ref
    config.caselights = ref
  }))

  mode.onchange = () => {
    const value = mode.value
    config.caselights = value === "off" ? false : value === "theme" ? true : following
    row.hidden = value !== "reading"
    markDirty()
  }
}

function renderButtons() {
  // What the badge answers itself, then what it asks this host to run, under the heading
  // the host asks for. A group exists once something is in it, so every heading has rows
  // under it.
  const groups = new Map()
  const offer = (heading, option) => {
    if (!groups.has(heading)) groups.set(heading, [])
    groups.get(heading).push(option)
  }
  for (const local of caps.local_actions || []) {
    offer("Badge", el("option", { value: local.action, textContent: titleCase(local.label) }))
  }
  for (const command of caps.commands || []) {
    const option = el("option", { value: command.name, textContent: titleCase(command.label) })
    offer(command.group, option)
  }

  const offered = [
    el("option", { value: "", textContent: "Nothing" }),
    ...[...groups].map(([label, options]) => el("optgroup", { label }, options)),
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
// The palette comes from the host, for every theme and not only the tinted ones. Deriving
// there keeps the preview and what reaches the badge from drifting apart, and the browser
// needs no colour arithmetic.

// Which preview request is the current one. Clicking along the swatches starts several, and
// they can come back in any order. Without this the last reply wins, not the last
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
  // Whether a theme takes an accent comes from the host, so no list is held here: a derived
  // theme is built from the one chosen, and a written palette is fixed as drawn.
  const record = (caps.themes || []).find((entry) => entry.name === config.theme)
  const derived = Boolean(record && record.derived)
  for (const node of all("[data-tint]")) node.hidden = !derived

  const second = $("accentb")
  if (!second.options.length) {
    second.replaceChildren(...(caps.accent_b_rules || []).map((rule) =>
      el("option", { value: rule, textContent: titleCase(rule) })))
  }
  second.value = config.accent_b || "same"
  second.onchange = () => { config.accent_b = second.value; markDirty(); renderTint() }

  const accents = pick("div[data-tint]")
  accents.replaceChildren()
  if (derived) {
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

// -- the preview -----------------------------------------------------------
//
// Four pages at the badge's own 320x240, drawn in the badge's own faces. The colours and the
// two graph series come from /api/theme: this file holds no palette and no rule for picking
// one, only where a page puts things.

const W = 320
const H = 240
const rgb = (parts) => `rgb(${parts.join(", ")})`
const face = (weight, size) => `${weight} ${size}px Lexend, system-ui, sans-serif`

// ci/badge-icons.txt, third column: the letter badge-side code draws, and what it means.
// GROUP_ICONS and FIELD_ICONS in badge_app/pages.py address them by the same letters.
const ICONS = {
  c: 0xe322, g: 0xe30d, m: 0xf7a3, d: 0xe1db, n: 0xeb2f, p: 0xea0b, f: 0xf168, y: 0xe31e,
  l: 0xe9e4, t: 0xf076, s: 0xe1b8, r: 0xe677, u: 0xf09b, o: 0xf090, b: 0xe1a5, e: 0xeb58,
  a: 0xeff2, h: 0xefd6,
}

// -- what a badge shows for a number ---------------------------------------
//
// draw.fmt, draw.short_unit and pages.fraction_of, in the browser.
//
// The badge cannot answer for them: pages.py imports draw, and draw expects the firmware's
// globals. A check compares these against the badge's for a table of readings.

const SCALE = {
  temp: 100, power: 250, package_w: 150, rpm: 6000,
  freq: 6000, clock: 3000,
  up_bps: 12.5e6, down_bps: 12.5e6, read_bps: 500e6, write_bps: 500e6,
}
// pages.PERCENT. Longer than the live view's list further down, which is a different job:
// `cores` is a list of percentages, and a gauge needs to know that where a printed reading
// does not.
const PERCENT_FIELDS = ["pct", "swap_pct", "mem_pct", "fan_pct", "battery_pct", "cores"]

const isPercent = (field) => PERCENT_FIELDS.includes(field) || field.endsWith("_pct")

function rate(bps) {
  if (bps >= 1024 ** 3) return `${(bps / 1024 ** 3).toFixed(1)}G`
  if (bps >= 1024 ** 2) return `${(bps / 1024 ** 2).toFixed(1)}M`
  if (bps >= 1024) return `${(bps / 1024).toFixed(0)}K`
  return `${bps.toFixed(0)}`
}

function size(mb) {
  if (mb >= 1024 ** 2) return `${(mb / 1024 ** 2).toFixed(1)}T`
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)}G`
  return `${mb.toFixed(0)}M`
}

function duration(seconds) {
  const whole = Math.trunc(seconds)
  if (whole >= 86400) return `${Math.floor(whole / 86400)}d${Math.floor((whole % 86400) / 3600)}h`
  if (whole >= 3600) return `${Math.floor(whole / 3600)}h${Math.floor((whole % 3600) / 60)}m`
  return `${Math.floor(whole / 60)}m`
}

/** A number as a badge would show it: short, and never wider than its box. */
function fmt(value, field) {
  if (value === null || value === undefined) return "--"
  if (typeof value === "boolean") return value ? "yes" : "no"
  if (typeof value === "string") return value
  if (Array.isArray(value)) return String(value.length)
  if (field.endsWith("_bps")) return rate(value)
  if (field.endsWith("_mb")) return size(value)
  if (field === "uptime_s" || field === "secs_left") return duration(value)
  if (["freq", "clock", "rpm", "procs"].includes(field)) return value.toFixed(0)
  return value >= 100 ? value.toFixed(0) : value.toFixed(1)
}

/** What follows the number. The prefix stays on the number, so 11.4 and MB/s make 11.4MB/s. */
function shortUnit(field) {
  if (field.endsWith("_bps")) return "B/s"
  if (field === "cores" || field === "pct" || field.endsWith("_pct")) return "%"
  if (field === "temp") return "\u00b0C"
  if (field === "power" || field === "package_w") return "W"
  if (field === "freq" || field === "clock") return "MHz"
  if (field.endsWith("_mb")) return "B"
  return ""
}

/** Where a value sits on 0-1, for a gauge. A rate is scaled by the busiest the host has
 * seen, which travels with the frame. A fixed full scale pegs a fast link and idles a
 * slow one. */
function fractionOf(ref, value, frame) {
  if (value === null || value === undefined || typeof value === "string"
      || typeof value === "boolean") return null
  const field = ref.split(".").pop()
  let top
  if (isPercent(field)) top = 100
  else top = Number((frame?.peaks || {})[ref]) || SCALE[field]
  if (!top) return null
  return Math.max(0, Math.min(1, value / top))
}

/** A reading out of the frame, by "group.field". The badge reads a list's first entry the
 * same way, a host with two GPUs having sent both. */
function readingOf(frame, ref) {
  const [group, field] = ref.split(".")
  let held = (frame || {})[group]
  if (Array.isArray(held)) held = held[0]
  return held === undefined || held === null ? null : held[field]
}

// look.py's geometry, so a preview lays a page out where the badge lays it out.
const HEADER_H = 30
const FOOTER_H = 20
const BODY_TOP = HEADER_H
const BODY_H = H - HEADER_H - FOOTER_H
const PAD = 10
const SIZE_TITLE = 19
const SIZE_SMALL = 11
const SIZE_VALUE = 17
const SIZE_BIG = 26
const SIZE_HUGE = 44

const DIAL_GAP = 16
const DIAL_OUTER = 82
const DIAL_INNER = 62
const DIAL_C = [DIAL_GAP + DIAL_OUTER, BODY_TOP + Math.floor(BODY_H / 2) + 2]
const DIAL_FROM = 225
const DIAL_TO = 495
const READOUT_X = DIAL_C[0] + DIAL_OUTER + DIAL_GAP
const READOUT_W = W - READOUT_X - DIAL_GAP
const READOUT_H = 38

// look.readout_rows: level with the top of the dial, lifted only where the rows would run
// past the band.
function readoutRows(count) {
  const room = BODY_TOP + BODY_H - 6 - count * READOUT_H
  const top = Math.max(BODY_TOP + 6, Math.min(DIAL_C[1] - DIAL_OUTER, room))
  return Array.from({ length: count }, (_, index) => top + index * READOUT_H)
}

// pages.NAMES and pages.name_for. The badge names a reading in the room it has: "DOWN"
// against the host's "Download".
const NAMES = {
  "cpu.pct": "LOAD", "cpu.temp": "TEMP", "cpu.freq": "CLOCK", "cpu.procs": "PROCS",
  "mem.pct": "USED", "mem.used_mb": "USED", "mem.total_mb": "TOTAL", "mem.swap_pct": "SWAP",
  "gpu.pct": "LOAD", "gpu.temp": "TEMP", "gpu.power": "POWER", "gpu.mem_pct": "VRAM",
  "net.up_bps": "UP", "net.down_bps": "DOWN", "net.up_total_mb": "SENT",
  "net.down_total_mb": "RECV",
  "disk.pct": "FULL", "disk.read_bps": "READ", "disk.write_bps": "WRITE",
  "disk.used_mb": "USED", "disk.total_mb": "TOTAL",
  "power.battery_pct": "BATTERY", "power.package_w": "PACKAGE",
  "sys.host": "HOST", "sys.os": "OS", "sys.cpu_name": "CPU", "sys.uptime_s": "UPTIME",
}
const UNIT_SUFFIXES = ["_bps", "_mb", "_pct"]

function nameFor(ref) {
  if (NAMES[ref]) return NAMES[ref]
  let field = ref.split(".").pop()
  for (const suffix of UNIT_SUFFIXES) {
    if (field.endsWith(suffix) && field.length > suffix.length) {
      field = field.slice(0, -suffix.length)
      break
    }
  }
  return field.replace(/_/g, " ").toUpperCase()
}

// The pages the preview draws, and the readings behind them. The same refs the default
// layout uses, so a preview shows a page somebody will actually have.
const DIAL = { field: "cpu.pct", readouts: ["cpu.temp", "cpu.freq", "cpu.procs"] }
const BARS = "cpu.cores"
const SERIES = ["net.down_bps", "net.up_bps"]
const TILE_REFS = ["disk.pct", "disk.read_bps", "disk.write_bps", "disk.used_mb"]

/** draw.background and draw.furniture: the body is the page colour and the two bands are
 * panels over it, with the rule along the bottom of the header. */
function chrome(ctx, palette, title, current) {
  ctx.textBaseline = "top"          // blit_label draws from the top, as screen.text does
  ctx.textAlign = "left"

  ctx.fillStyle = rgb(palette.bg)
  ctx.fillRect(0, BODY_TOP, W, BODY_H)

  ctx.fillStyle = rgb(palette.panel)
  ctx.fillRect(0, 0, W, HEADER_H)
  ctx.fillRect(0, H - FOOTER_H, W, FOOTER_H)

  // The chrome takes accent_b, leaving the accent for readings.
  const chromePen = rgb(palette.accent_b || palette.accent)
  ctx.fillStyle = chromePen
  ctx.fillRect(0, HEADER_H - 2, W, 2)

  ctx.fillStyle = rgb(palette.ink)
  ctx.font = face(400, SIZE_TITLE)
  ctx.fillText(title, PAD, 4)

  ctx.textAlign = "right"
  ctx.fillStyle = rgb(palette.dim)
  ctx.font = face(400, SIZE_SMALL)
  ctx.fillText(hostName(), W - PAD, 10)
  ctx.textAlign = "left"

  pips(ctx, palette, chromePen, current)
}

const hostName = () => readingOf(frameNow, "sys.host") || "workshop-pc"

// draw._pips: a dash per page, four high with rounded ends. Every pip is the same size and
// only the current one's colour differs.
const PIP_ROOM = W - PAD * 4
const PIP_MAX_W = 14
const PIP_GAP = 5
const PIP_DOT = 4
const PIP_TIGHT = 2
const PIP_H = 4

function pips(ctx, palette, chromePen, current, total = 8) {
  let gap = PIP_GAP
  let width = Math.min(PIP_MAX_W, Math.floor((PIP_ROOM - (total - 1) * gap) / total))
  if (width < PIP_DOT) {
    gap = PIP_TIGHT
    width = Math.max(PIP_DOT,
                     Math.min(PIP_MAX_W, Math.floor((PIP_ROOM - (total - 1) * gap) / total)))
  }
  const span = total * width + (total - 1) * gap
  const left = Math.floor((W - span) / 2)
  const top = H - FOOTER_H + Math.floor(FOOTER_H / 2) - 2
  const round = Math.min(2, Math.floor(width / 2))
  for (let i = 0; i < total; i += 1) {
    ctx.beginPath()
    ctx.roundRect(left + i * (width + gap), top, width, PIP_H, round)
    ctx.fillStyle = i === current ? chromePen : rgb(palette.grid)
    ctx.fill()
  }
}

function drawDial(ctx, palette, _series, frame) {
  chrome(ctx, palette, "CPU", 0)

  const value = readingOf(frame, DIAL.field)
  const reading = fractionOf(DIAL.field, value, frame) ?? 0.635
  const [cx, cy] = DIAL_C
  const middle = (DIAL_OUTER + DIAL_INNER) / 2
  // shape.arc angles start at the top and run clockwise; canvas starts at three o'clock.
  const at = (degrees) => ((degrees - 90) * Math.PI) / 180

  ctx.lineCap = "butt"
  ctx.lineWidth = DIAL_OUTER - DIAL_INNER
  const sweep = DIAL_FROM + (DIAL_TO - DIAL_FROM) * reading

  ctx.beginPath()
  ctx.arc(cx, cy, middle, at(sweep), at(DIAL_TO))
  ctx.strokeStyle = rgb(palette.grid)
  ctx.stroke()

  if (config.gauge_fill === "ramp") {
    const steps = 96
    for (let i = 0; i < steps; i += 1) {
      ctx.beginPath()
      ctx.arc(cx, cy, middle, at(DIAL_FROM + ((sweep - DIAL_FROM) * i) / steps),
              at(DIAL_FROM + ((sweep - DIAL_FROM) * (i + 1)) / steps + 0.35))
      ctx.strokeStyle = rgb(rampAt(palette.ramp, (i / steps) * reading))
      ctx.stroke()
    }
  } else {
    ctx.beginPath()
    ctx.arc(cx, cy, middle, at(DIAL_FROM), at(sweep))
    ctx.strokeStyle = rgb(rampAt(palette.ramp, reading))
    ctx.stroke()
  }

  // The tick draw.gauge puts over the join.
  if (reading > 0.001) {
    ctx.beginPath()
    ctx.lineWidth = DIAL_OUTER + 3 - (DIAL_INNER - 3)
    ctx.arc(cx, cy, (DIAL_OUTER + 3 + DIAL_INNER - 3) / 2, at(sweep - 1.4), at(sweep + 1.4))
    ctx.strokeStyle = rgb(palette.ink)
    ctx.stroke()
    ctx.lineWidth = DIAL_OUTER - DIAL_INNER
  }

  // The reading and its unit share a baseline inside the ring, centred as a pair.
  const text = fmt(value, "pct")
  const unit = shortUnit("pct")
  const unitSize = Math.max(SIZE_SMALL, Math.trunc(SIZE_HUGE * 0.45))
  ctx.font = face(400, SIZE_HUGE)
  const readingW = ctx.measureText(text).width
  ctx.font = face(400, unitSize)
  const suffixW = ctx.measureText(unit).width
  const left = cx - (readingW + suffixW) / 2
  const top = cy - SIZE_HUGE * 0.62

  ctx.fillStyle = rgb(palette.ink)
  ctx.font = face(400, SIZE_HUGE)
  ctx.fillText(text, left, top)
  ctx.fillStyle = rgb(palette.dim)
  ctx.font = face(400, unitSize)
  ctx.fillText(unit, left + readingW, top + SIZE_HUGE - unitSize)

  // draw.readout: a name, the reading under it, and a bar for the level.
  const rows = readoutRows(DIAL.readouts.length)
  DIAL.readouts.forEach((ref, index) => {
    const field = ref.split(".").pop()
    const held = readingOf(frame, ref)
    const y = rows[index]
    ctx.fillStyle = rgb(palette.dim)
    ctx.font = face(400, SIZE_SMALL)
    ctx.fillText(nameFor(ref), READOUT_X, y)
    ctx.fillStyle = rgb(palette.ink)
    ctx.font = face(400, SIZE_VALUE)
    ctx.fillText(fmt(held, field) + shortUnit(field), READOUT_X, y + 10)

    const part = fractionOf(ref, held, frame)
    if (part === null) return
    const filled = Math.trunc(READOUT_W * part)
    ctx.fillStyle = rgb(palette.grid)
    ctx.fillRect(READOUT_X + filled, y + 28, READOUT_W - filled, 3)
    if (filled) {
      ctx.fillStyle = rgb(rampAt(palette.ramp, part))
      ctx.fillRect(READOUT_X, y + 28, filled, 3)
    }
  })
}

const CORES = [0.31, 0.882, 0.125, 0.741, 0.2, 0.955, 0.602, 0.05]

function drawBars(ctx, palette, _series, frame) {
  chrome(ctx, palette, "CORES", 1)
  const held = readingOf(frame, BARS)
  const values = (Array.isArray(held) ? held : CORES.map((v) => v * 100)).slice(0, 16)
  const count = values.length
  const top = BODY_TOP + 6
  // Sized as draw.bars sizes it, whatever the core count.
  const slot = Math.max(6, Math.floor((BODY_H - 12) / count))
  const height = Math.max(4, slot - 3)

  ctx.font = face(400, SIZE_SMALL)
  const readings = values.map((value) => fmt(value, "cores") + shortUnit("cores"))
  const labelW = Math.max(...values.map((_v, i) => ctx.measureText(String(i)).width))
  const valueW = Math.max(...readings.map((text) => ctx.measureText(text).width))
  const x = PAD + labelW + COLUMN_GAP
  const width = Math.max(20, W - x - COLUMN_GAP - valueW - PAD)

  values.forEach((value, index) => {
    const part = Math.max(0, Math.min(1, value / 100))
    const y = top + index * slot
    ctx.textAlign = "left"
    ctx.fillStyle = rgb(palette.dim)
    ctx.font = face(400, SIZE_SMALL)
    ctx.fillText(String(index), PAD, y - 1)

    const filled = part > 0 ? Math.max(1, Math.trunc(width * part)) : 0
    ctx.fillStyle = rgb(palette.grid)
    ctx.fillRect(x + filled, y, width - filled, height)
    if (filled) {
      ctx.fillStyle = rgb(rampAt(palette.ramp, part))
      ctx.fillRect(x, y, filled, height)
    }
    ctx.textAlign = "right"
    ctx.fillStyle = rgb(palette.ink)
    ctx.fillText(readings[index], W - PAD, y - 1)
    ctx.textAlign = "left"
  })
}

const COLUMN_GAP = 8

const DOWN = [0.12, 0.2, 0.55, 0.86, 0.7, 0.52, 0.62, 0.44, 0.2, 0.1, 0.08, 0.3, 0.66, 0.8,
              0.62, 0.5, 0.72, 0.9, 0.55, 0.2, 0.12, 0.1, 0.26, 0.42, 0.3, 0.18, 0.12]
const UP = [0.05, 0.08, 0.14, 0.2, 0.16, 0.12, 0.18, 0.14, 0.08, 0.05, 0.04, 0.1, 0.16, 0.2,
            0.14, 0.1, 0.16, 0.22, 0.12, 0.06, 0.05, 0.04, 0.09, 0.13, 0.1, 0.07, 0.05]
// draw.SERIES_ALPHA: on a pale page a translucent area washes out, so both go solid.
const SERIES_ALPHA = [200, 150]
const PALE_SUM = 384

function drawGraph(ctx, palette, series) {
  chrome(ctx, palette, "NETWORK", 4)
  const pale = palette.bg[0] + palette.bg[1] + palette.bg[2] >= PALE_SUM

  // Both series share a scale, as the badge's graph does, so one cannot dwarf the other.
  const plots = SERIES.map((ref) => rings[ref] || [])
  const live = plots.some((ring) => ring.length > 1)
  const peak = live ? Math.max(...plots.flat().map((v) => v ?? 0), 1) * 1.15 : 9.8 * 1024 ** 2
  const peakText = fmt(peak, "down_bps") + shortUnit("down_bps")

  // The gutter takes its width from the scale in it, as draw.graph does.
  ctx.font = face(400, SIZE_SMALL)
  const left = PAD + Math.max(ctx.measureText(peakText).width, ctx.measureText("0").width) + 4
  const top = BODY_TOP + 8
  const width = W - left - PAD
  const height = BODY_H - 26
  const right = left + width
  const bottom = top + height

  ctx.fillStyle = rgb(palette.grid)
  for (let i = 0; i < 5; i += 1) {
    ctx.fillRect(left, top + (height * i) / 4, width, 1)
  }
  ctx.fillStyle = rgb(palette.dim)
  ctx.fillText(peakText, PAD, top - 4)
  ctx.fillText("0", PAD, top + height - 8)

  const plot = (points, index) => {
    ctx.globalAlpha = (index === 0 || pale ? SERIES_ALPHA[0] : SERIES_ALPHA[1]) / 255
    ctx.beginPath()
    ctx.moveTo(left, bottom)
    points.forEach((value, at) => {
      ctx.lineTo(left + ((right - left) * at) / (points.length - 1),
                 bottom - (bottom - top) * value)
    })
    ctx.lineTo(right, bottom)
    ctx.closePath()
    ctx.fillStyle = rgb(series[index])
    ctx.fill()
    ctx.globalAlpha = 1
  }
  const scaled = plots.map((ring) => ring.map((v) => Math.max(0, (v ?? 0) / peak)))
  plot(live ? scaled[0] : DOWN, 0)
  plot(live ? scaled[1] : UP, 1)

  SERIES.forEach((ref, index) => {
    const label = nameFor(ref)
    const x = left + index * 110
    const y = H - FOOTER_H - 14
    ctx.fillStyle = rgb(series[index])
    ctx.fillRect(x, y + 3, 10, 4)
    ctx.fillStyle = rgb(palette.dim)
    ctx.font = face(400, SIZE_SMALL)
    ctx.fillText(label, x + 14, y - 2)
  })
}

// FIELD_ICONS for disk: the arrows invert between a link and a disk, since storage is drawn
// against the disk and a write goes down into it.
const TILES = [["FULL", "74.2%", 0.742, "l"], ["READ", "50.0MB/s", 0.5, "u"],
               ["WRITE", "8.0MB/s", 0.08, "o"], ["USED", "687.3GB", 0.62, "a"]]

function drawGrid(ctx, palette, _series, frame) {
  chrome(ctx, palette, "DISK", 5)
  const count = TILE_REFS.length
  const columns = count > 4 ? 3 : 2
  const rows = Math.ceil(count / columns)
  const cellW = Math.floor((W - PAD * 2 - (columns - 1) * 6) / columns)
  const cellH = Math.floor((BODY_H - 12 - (rows - 1) * 6) / rows)
  const size = rows < 3 ? SIZE_BIG : SIZE_VALUE

  TILE_REFS.forEach((ref, index) => {
    const field = ref.split(".").pop()
    const held = readingOf(frame, ref)
    const part = fractionOf(ref, held, frame) ?? TILES[index][2]
    const x = PAD + (index % columns) * (cellW + 6)
    const y = BODY_TOP + 6 + Math.floor(index / columns) * (cellH + 6)

    ctx.beginPath()
    ctx.roundRect(x, y, cellW, cellH, 5)
    ctx.fillStyle = rgb(palette.panel)
    ctx.fill()
    if (part !== null) {
      ctx.fillStyle = rgb(rampAt(palette.ramp, part))
      ctx.fillRect(x, y + cellH - 3, Math.trunc(cellW * part), 3)
    }

    ctx.textAlign = "left"
    ctx.fillStyle = rgb(palette.dim)
    ctx.font = face(400, SIZE_SMALL)
    ctx.fillText(nameFor(ref), x + 7, y + 5)
    ctx.textAlign = "right"
    ctx.font = `${SIZE_VALUE}px "Badge Icons"`
    ctx.fillText(String.fromCodePoint(ICONS[TILES[index][3]]), x + cellW - 7, y + 4)
    ctx.textAlign = "left"
    ctx.fillStyle = rgb(palette.ink)
    ctx.font = face(400, size)
    ctx.fillText(held === null ? TILES[index][1] : fmt(held, field) + shortUnit(field),
                 x + 7, y + Math.floor(cellH / 2) - Math.floor(size / 2) + 2)
  })
}

const SCREENS = [drawDial, drawBars, drawGraph, drawGrid]

// What the previews draw from. The palette arrives on a theme change; the readings ride
// the frame this page already fetches every second.
let shown = null
let frameNow = null
let rings = {}

/** The rings the graph plots. Seeded from the host, or the graph stays flat for half a
 * minute, then appended from each frame. */
async function seedHistory() {
  try {
    rings = await api(`/api/history?keys=${SERIES.join(",")}&points=${GRAPH_POINTS}`)
  } catch (error) { rings = {} }
}

const GRAPH_POINTS = 48

function pushFrame(frame) {
  frameNow = frame
  for (const ref of SERIES) {
    const value = readingOf(frame, ref)
    if (value === null) continue
    const ring = rings[ref] || (rings[ref] = [])
    ring.push(value)
    if (ring.length > GRAPH_POINTS) ring.splice(0, ring.length - GRAPH_POINTS)
  }
  paintScreens()
}

function paintScreens() {
  if (!shown) return
  const holder = $("screens")
  if (holder.childElementCount !== SCREENS.length) {
    holder.replaceChildren(...SCREENS.map(() => {
      const canvas = el("canvas")
      canvas.width = W * 2                 // drawn at 2x, shown at 320 wide, so it stays sharp
      canvas.height = H * 2
      return canvas
    }))
  }
  SCREENS.forEach((paint, index) => {
    const ctx = holder.children[index].getContext("2d")
    ctx.setTransform(2, 0, 0, 2, 0, 0)
    paint(ctx, shown.palette, shown.series, frameNow)
  })
}

async function preview() {
  const query = new URLSearchParams({ theme: config.theme || "dark" })
  const record = (caps.themes || []).find((entry) => entry.name === config.theme)
  if (record && record.derived) {
    query.set("accent", (config.tint || []).join(","))
    query.set("second", config.accent_b || "same")
  }
  const mine = ++previewWanted
  let answer
  try {
    answer = await api(`/api/theme?${query}`)
  } catch (error) {
    return
  }
  if (mine !== previewWanted) return

  shown = answer
  $("accentbchip").style.background = rgb(answer.palette.accent_b || answer.palette.accent)
  if (!Object.keys(rings).length) await seedHistory()
  paintScreens()
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
// One layout per badge, and a default for a badge with nothing saved. The picker
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
  try {
    last = window.localStorage.getItem(REMEMBERED)
  } catch (error) {
    // private mode
  }
  if (last && badges[last]) return last
  return Object.keys(badges)[0] || null
}

function remember(id) {
  try {
    if (id) window.localStorage.setItem(REMEMBERED, id)
    else window.localStorage.removeItem(REMEMBERED)
  } catch (error) {
    // nowhere to remember it
  }
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
    : (!whose && ids.length ? "defaults for a newly paired badge" : "")
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
 * layout stored, and derived from the badge id so switching back and forth is stable. */
function ownIds(pages, badgeId) {
  const tag = badgeId.slice(0, 4)
  return pages.map((page) => (String(page.id).endsWith(`-${tag}`)
    ? page : { ...page, id: `${page.id}-${tag}` }))
}

function renderBadges() {
  const ids = Object.keys(badges)
  const node = $("badges")
  if (ids.length) {
    node.replaceChildren(node.querySelector("h2"), ...ids.map(badgeBox))
  } else {
    node.replaceChildren(node.querySelector("h2"), el("section", null, el("p", {
      textContent: "None paired. Use the USB installer, or pair over the network." })))
  }
  renderStale()
}

/** One box per paired badge: what to call it, what it is, and the two things that can be
 * done to it. The one the rest of the page is configuring is marked. */
function badgeBox(id) {
  // A badge nobody has named announces itself by its id, so there is no name to show and
  // the field is left empty, and not filled with the id under the id.
  const named = badges[id].name && badges[id].name !== id ? badges[id].name : ""
  const nameId = `badge${++controlSerial}`
  const name = el("input", { type: "text", id: nameId, value: named,
                             placeholder: "Give it a name" })
  const heading = el("h3", { textContent: named || "Unnamed badge" })

  // A moment after the typing stops as well as on the way out of the field, so a name that
  // is typed and then left alone is still saved. Only leaving it says so out loud.
  let pending = null
  const store = (announce) => {
    window.clearTimeout(pending)
    pending = null
    rename(id, name.value)
      .then((shown) => {
        heading.textContent = shown
        if (announce) toast("Renamed")
      })
      .catch((error) => toast(error.message, true))
  }
  name.oninput = () => {
    window.clearTimeout(pending)
    pending = window.setTimeout(() => store(false), 400)
  }
  name.onchange = () => store(true)

  const forget = el("button", { type: "button", className: "small danger",
                                textContent: "Forget" })
  forget.onclick = () => forgetBadge(id).catch((error) => toast(error.message, true))

  const box = el("section", { "aria-current": id === whose ? "true" : null },
                 heading,
                 el("label", { htmlFor: nameId, textContent: "Name" }),
                 name,
                 facts(id))

  const footer = el("footer", null, forget)
  if (id !== whose) {
    const configure = el("button", { type: "button", className: "small add",
                                     textContent: "Configure" })
    configure.onclick = () => switchTo(id).catch((error) => toast(error.message, true))
    footer.append(configure)
  }
  box.append(footer)
  return box
}

/** What a badge is drawing, without opening it: the layout it is on and the headline
 * settings off it. The id leads, being the thing to quote when a badge misbehaves. */
function facts(id) {
  const record = badges[id]
  const rows = [
    ["UID", el("code", { textContent: id })],
    ["Layout", record.configured ? "Its own" : "The default"],
    ["Pages", `${record.pages}`],
    ["Theme", themeLabel(record.theme)],
    ["Refresh", `${record.interval_ms} ms`],
    ["App", appLabel(record.app)],
  ]
  return el("dl", null, ...rows.flatMap(([term, said]) =>
    [el("dt", { textContent: term }), el("dd", null, said)]))
}

/** What the badge is running, from what it was last seen holding. Nothing recorded means
 * it was paired over the network and never installed to from here. */
function appLabel(state) {
  if (!state) return "Not installed from here"
  const changes = state.added.length + state.changed.length + state.removed.length
  if (!changes) return "Up to date"
  return `${changes} file${changes === 1 ? "" : "s"} behind`
}

/** A theme by the name the picker calls it. An unknown one is a theme this host no longer
 * ships, which the badge is still drawing, so its stored name shows instead. */
function themeLabel(name) {
  const record = (caps.themes || []).find((entry) => entry.name === name)
  return (record && record.label) || name || "unset"
}

/** Saved apart from the layout, since what a badge is called is nothing the badge draws,
 * so there is no revision to wait for. The list of badges is left alone, the field being
 * typed in being inside it. */
async function rename(id, wanted) {
  const result = await api(`/api/badges/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: wanted }),
  })
  badges[id].name = result.name
  renderWhose()
  return result.name === id ? "Unnamed badge" : result.name
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
  const panel = $("pairing")
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
    // Filtered: replaceChildren writes a null out as the word, where el() drops one.
    panel.replaceChildren(...[
      el("p", { textContent: "On the badge: launch Stats, press B to set up, and pick "
                             + `${(state.hosts || []).join(" / ")}:${state.port}` }),
      el("p", { textContent: `closes in ${state.expires_in}s` }),
      pending.length ? el("p", { textContent: "Approve the one whose code matches." }) : null,
      pending.length ? pendingList(pending) : null,
    ].filter(Boolean))
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

// -- over USB --------------------------------------------------------------
//
// The app and the badge's WiFi details only travel over USB: /v1 carries readings and a
// layout, never code. This drives the install `statsbadge install` does, against whichever
// badge is plugged in, which need not be the one the picker is on.

let installPoll = null
let installer = null
// Whether the last poll saw one running, which is what turns finishing into an event.
let installRan = false

function openInstaller() {
  const panel = $("installer")
  if (panel.open) {
    closeInstaller()
    return
  }
  panel.replaceChildren(...installerBox())
  panel.show()
  watchInstall()
}

function closeInstaller() {
  $("installer").close()
  installer = null
  if (installPoll) {
    clearInterval(installPoll)
    installPoll = null
  }
}

/** The panel: what is plugged in, the WiFi details if any are wanted, and the log.
 *
 * WiFi is off unless it is asked for. Sending a network replaces whatever the badge has,
 * and an update should not be a way to lose the one it is already on. */
function installerBox() {
  const status = el("p", { className: "found", textContent: "Looking for a badge…" })
  const ssid = el("input", { type: "text", id: "ssid", placeholder: "Network name" })
  const password = el("input", { type: "password", id: "wifipass" })
  const region = el("input", { type: "text", id: "region", placeholder: "us" })
  const zone = el("input", { type: "number", id: "zone", min: -12, max: 14, step: 1,
                             placeholder: "0" })
  const wifi = el("input", { type: "checkbox", id: "setwifi" })
  const fields = el("div", { className: "fields", hidden: true },
                    el("label", { htmlFor: "ssid", textContent: "Network" }), ssid,
                    el("label", { htmlFor: "wifipass", textContent: "Password" }), password,
                    el("label", { htmlFor: "region", textContent: "Region" }), region,
                    el("label", { htmlFor: "zone", textContent: "GMT offset" }), zone)
  wifi.onchange = () => { fields.hidden = !wifi.checked }

  const go = el("button", { type: "button", className: "primary", textContent: "Update" })
  go.onclick = () => startInstall().catch((error) => {
    toast(error.message, true)
    go.disabled = false
  })
  const cancel = el("button", { type: "button", textContent: "Close" })
  cancel.onclick = closeInstaller
  const log = el("pre", { hidden: true })

  installer = { status, wifi, ssid, password, region, zone, go, log }
  return [
    status,
    el("label", { htmlFor: "setwifi", className: "check" }, wifi, "Set the WiFi network"),
    fields,
    el("p", { className: "note",
              textContent: "The badge resets into USB storage while this runs." }),
    el("menu", null, go, cancel),
    log,
  ]
}

async function startInstall() {
  const asking = {}
  if (installer.wifi.checked) {
    const network = installer.ssid.value.trim()
    if (!network) {
      toast("Name the network first", true)
      return
    }
    asking.ssid = network
    asking.password = installer.password.value
    // Named here, so it replaces whatever the badge was set to.
    asking.force_secrets = true
    if (installer.region.value.trim()) asking.region = installer.region.value.trim()
    if (installer.zone.value !== "") asking.timezone = Number(installer.zone.value)
  }
  installer.go.disabled = true
  await api("/api/install", { method: "POST",
                              headers: { "Content-Type": "application/json" },
                              body: JSON.stringify(asking) })
  watchInstall()
}

function watchInstall() {
  if (installPoll) clearInterval(installPoll)
  const tick = () => api("/api/install").then(paintInstall).catch(closeInstaller)
  tick()
  installPoll = setInterval(tick, 1000)
}

function paintInstall(state) {
  if (!installer) return
  const port = (state.ports || [])[0]
  installer.status.textContent = state.running
    ? "Working…"
    : (port ? `Badge on ${port}` : "No badge connected. Plug one in by USB.")
  installer.go.disabled = state.running || !port
  const said = (state.log || []).join("\n")
  installer.log.hidden = !said
  installer.log.textContent = said
  installer.log.scrollTop = installer.log.scrollHeight
  if (installRan && !state.running) finishedInstall(state.result)
  installRan = state.running
}

async function finishedInstall(result) {
  if (!result) return
  if (result.error) toast(result.error, true)
  else if (result.cancelled) toast("Nothing was changed")
  else toast(installSummary(result))
  badges = await api("/api/badges").catch(() => badges)
  renderWhose()
  renderBadges()
}

function installSummary(result) {
  const copied = (result.copied || []).length
  const parts = [copied
    ? `${copied} file${copied === 1 ? "" : "s"} copied`
    : "already up to date"]
  if (result.wifi === "set") parts.push("WiFi set")
  if (result.credentials) parts.push("paired")
  return parts.join(", ")
}

/** One line when a badge is behind what this host would install.
 *
 * A guess, from what the badge was last seen holding: another machine can have installed
 * something else since, and the answer is only ever a reason to plug it in and look. */
function renderStale() {
  const names = Object.keys(badges)
    .filter((id) => badges[id].app && badges[id].app.behind)
    .map(badgeName)
  const node = $("stale")
  node.hidden = !names.length
  if (!names.length) return
  const one = names.length === 1
  const button = el("button", { type: "button", className: "small",
                                textContent: "Update…" })
  button.onclick = openInstaller
  node.replaceChildren(
    `${names.join(", ")} ${one ? "was" : "were"} last seen running an older app. `
    + `Connect ${one ? "it" : "them"} by USB to update.`,
    button)
}

// -- live ------------------------------------------------------------------

const PERCENT = ["pct", "swap_pct", "mem_pct", "fan_pct", "battery_pct"]

// Everything on a frame that is not a group of readings, which is collect.FRAME_SCALARS and
// held to it by a test. `peaks` is shown, being useful to see, but it is scale and not a
// reading and comes and goes with what has been measured, so it is left out of the signature
// below.
const FRAME_SCALARS = ["v", "t", "seq", "slow_rev"]
const FRAME_META = FRAME_SCALARS.concat(["peaks"])

// Which groups the last frame carried. A source that finds out what it can report only
// once it is running, the Cloudflare one listing an account's domains after it is given a
// token, shows up here first. This is already fetched every second, so noticing is free.
let liveGroups = ""

async function renderLive() {
  let frame
  try {
    frame = await api("/api/stats")
  } catch (error) { return }

  pushFrame(frame)

  const shape = Object.keys(frame).filter((key) => !FRAME_META.includes(key)).join(",")
  if (shape !== liveGroups && !dirty) {
    liveGroups = shape
    refreshCaps().catch(() => {})
  }

  if ($("live").closest("main > section").hidden) return

  const measured = hostGroups()
  const own = []
  const theirs = []
  for (const group of Object.keys(frame)) {
    if (FRAME_SCALARS.includes(group) || group === "peaks") continue
    const items = Array.isArray(frame[group]) ? frame[group] : [frame[group]]
    for (const [index, item] of items.entries()) {
      if (!item || !Object.keys(item).length) continue
      const box = liveGroup(items.length > 1 ? `${group} ${index}` : group, item)
      ;(measured.has(group) ? own : theirs).push(box)
    }
  }
  fillGroups($("live"), own)
  fillGroups($("from-extensions"), theirs)

  // The scale a plot is drawn against, and not a reading, so it sits with what this host
  // is, not with what it is doing.
  const peaks = $("peaks")
  const measurements = frame.peaks && Object.keys(frame.peaks).length
  if (measurements) peaks.replaceChildren(peaks.querySelector("h3"), readingList(frame.peaks))
  peaks.hidden = !measurements
}

/** The groups this host measures itself with. Anything else on a frame came from an
 * extension, including the ones an extension only finds out about once it is running and
 * so does not declare. */
function hostGroups() {
  const extensions = new Set((caps.extensions || []).map((extension) => extension.name))
  const groups = new Set()
  for (const source of caps.sources || []) {
    if (extensions.has(source.name)) continue
    for (const group of source.provides || []) groups.add(group)
  }
  return groups
}

function fillGroups(node, groups) {
  node.replaceChildren(node.querySelector("h2"), ...groups)
  node.hidden = !groups.length
}

// The most of a shaped reading that goes on a line. All of it is on
// the row's tooltip.
const SHOWN = 48

/** A reading as one line.
 *
 * Not everything a source reports is a number or a list of them: the ISS carries where it is
 * as an object and its ground track as a list of points, and the quakes source a list of
 * quakes. Those read as `[object Object]` and a row of `NaN`. */
function reading(value) {
  if (value === null || value === undefined) return "unknown"
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(1)
  }
  if (typeof value !== "object") return String(value)
  if (Array.isArray(value)) {
    if (value.every((each) => typeof each === "number")) {
      return value.map((each) => Math.round(each)).join(" ")
    }
    return `${value.length} item${value.length === 1 ? "" : "s"}`
  }
  const text = JSON.stringify(value)
  return text.length > SHOWN ? `${text.slice(0, SHOWN)}…` : text
}

function liveGroup(name, item) {
  return el("section", null, el("h3", { textContent: name }), readingList(item))
}

function readingList(item) {
  const rows = []
  for (const key of Object.keys(item)) {
    const value = item[key]
    const shown = el("dd", { textContent: reading(value) })
    // What did not fit, for anyone who wants to see what a source is actually sending.
    if (value && typeof value === "object") shown.title = JSON.stringify(value)
    if (PERCENT.includes(key) && typeof value === "number") {
      shown.style.setProperty("--at", `${Math.max(0, Math.min(100, value))}%`)
    }
    rows.push(el("dt", { textContent: key }), shown)
  }
  return el("dl", null, rows)
}

function renderSources() {
  $("sources").querySelector("ul").replaceChildren(...caps.sources.map((source) => {
    // A fault goes underneath what a source provides and not in place of it, and one it
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
  // Each source's current fault, and not how many it has had. One failing every poll
  // counts one a second, and a signature that moved with it would redraw every field,
  // `/api/preview` and all, once a second for as long as it was broken.
  const faults = (caps.sources || []).map((source) => [source.name, source.last_fault])
  return JSON.stringify([caps.available, caps.extension_settings, caps.graphed,
                         caps.group_source, caps.extension_pages, faults])
}

/** Refetch capabilities and redraw if what the host offers has changed.
 *
 * An extension may not have its groups at startup. A token pasted in the browser is what
 * lets the Cloudflare one list an account's domains, and it lists them on a thread a moment
 * after the save lands. */
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
 * domains land seconds later. Backing off and not polling, since this watches for one
 * thing to arrive and does not keep a display current. */
async function refreshCapsSoon(delays = [400, 1200, 3000, 6000]) {
  for (const delay of delays) {
    await new Promise((wake) => setTimeout(wake, delay))
    if (await refreshCaps()) return
  }
}

// -- boot ------------------------------------------------------------------

async function save() {
  try {
    // A badge saving for the first time stops drawing the default and gets its own pages.
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
    // Each badge's box shows the settings off its layout, which a save is what moves. Kept
    // to what is in hand if the fetch fails, the save itself having landed.
    badges = await api("/api/badges").catch(() => badges)
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
  offerExtensionPages()
  renderWhose()
  renderPages()
  renderSettings()
  renderLook()
  renderSources()
  renderBadges()
  renderLive()
  // After the first paint, since the list is a nicety and the page draws without it.
  refreshCatalogue().then(refreshOutdated).catch(() => {})

  $("save").onclick = save
  $("usb").onclick = openInstaller
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
