import { $, all, el, pick, titleCase, toast } from "./js/dom.js"
import { api, configPath } from "./js/api.js"

let config = null
let caps = null
let dirty = false
let edits = 0
let whose = null
let badges = {}

function markDirty() {
  dirty = true
  edits += 1
  $("save").disabled = false
}

const REMEMBERED_TAB = "statsbadge.tab"

function showSheet(wanted) {
  const tabs = all("header nav button")
  const sheets = all("main > section")
  tabs.forEach((tab, index) => {
    if (index === wanted) tab.setAttribute("aria-current", "page")
    else tab.removeAttribute("aria-current")
    sheets[index].hidden = index !== wanted
  })
  if (tabs[wanted] && tabs[wanted].textContent === "Help") renderHelp().catch(() => {})
  try {
    window.localStorage.setItem(REMEMBERED_TAB, wanted)
  } catch {}
}

function bindTabs() {
  const tabs = all("header nav button")
  tabs.forEach((tab, index) => { tab.onclick = () => showSheet(index) })
  let opening = 0
  try {
    opening = Number(window.localStorage.getItem(REMEMBERED_TAB))
  } catch {}
  showSheet(tabs[opening] ? opening : 0)
}

function availableRefs() {
  const refs = []
  const available = (caps && caps.available) || {}
  for (const group of Object.keys(available).sort()) {
    for (const field of available[group]) refs.push(`${group}.${field}`)
  }
  return refs
}

function preferredRefs() {
  const printable = availableRefs().filter(
    (ref) => !listFields().includes(ref.split(".")[1])
             && !itemFields().includes(ref.split(".")[1]))
  return [...new Set(numericRefs().concat(printable))]
}

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

function itemRefs() {
  return availableRefs().filter((ref) => itemFields().includes(ref.split(".")[1]))
}

function notifyRefs() {
  return [...new Set(itemRefs().concat(numericRefs()))]
}

function gaugeRefs() {
  const percent = caps.percent_fields || []
  const scaled = Object.keys(caps.full_scale || {})
  return numericRefs().filter((ref) => {
    const field = ref.split(".")[1]
    return percent.includes(field) || scaled.includes(field)
  })
}

function seriesRefs() {
  const kept = caps.graphed || []
  const withHistory = numericRefs().filter((ref) => kept.includes(ref))
  return withHistory.length ? withHistory : numericRefs()
}

function listRefs() {
  return availableRefs().filter((ref) => listFields().includes(ref.split(".")[1]))
}

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

const HOST_SOURCE = "This host"

function sourceLabel(group) {
  return (caps.group_source || {})[group] || HOST_SOURCE
}

function fieldLabel(ref) {
  const [group, field] = ref.split(".")
  const labels = (caps.field_labels || {})[group] || {}
  return labels[field] || titleCase(field)
}

function refSelect(value, refs, onChange) {
  const options = [...new Set(refs)]
  if (value && !options.includes(value)) options.unshift(value)

  const byGroup = new Map()
  for (const ref of options) {
    const group = ref.split(".")[0]
    if (!byGroup.has(group)) byGroup.set(group, [])
    byGroup.get(group).push(ref)
  }

  const byOwner = new Map()
  for (const group of byGroup.keys()) {
    const owner = sourceLabel(group)
    if (!byOwner.has(owner)) byOwner.set(owner, [])
    byOwner.get(owner).push(group)
  }
  const owners = [...byOwner.keys()].sort(
    (a, b) => (a === HOST_SOURCE ? -1 : 0) - (b === HOST_SOURCE ? -1 : 0))

  const chosen = String(value || options[0] || "").split(".")[0]
  const source = el("select", { "aria-label": "Source" }, owners.map(
    (owner) => el("optgroup", { label: owner }, byOwner.get(owner).map(
      (group) => el("option", { value: group, textContent: groupLabel(group),
                                selected: group === chosen })))))

  const select = el("select", { "aria-label": "Reading" })
  const fill = (group, ref) => {
    select.replaceChildren(...(byGroup.get(group) || []).map(
      (each) => el("option", { value: each, textContent: fieldLabel(each),
                               selected: each === ref })))
  }
  fill(chosen, value)

  source.onchange = () => {
    fill(source.value, null)
    onChange(select.value)
    markDirty()
  }
  select.onchange = () => { onChange(select.value); markDirty() }
  return [source, select]
}

const expanded = new Set()

function renderPages() {
  $("pages").replaceChildren(...config.pages.map(pageCard))
  refreshPruned()
}

function kindLabel(kind) {
  if (caps.kinds[kind]) return caps.kinds[kind].title
  const option = $("kind").querySelector(`option[value="${CSS.escape(kind)}"]`)
  return (option && option.textContent) || titleCase(kind)
}

function shapeFor(kind) {
  if (caps.kinds[kind]) return caps.kinds[kind]
  const declared = (caps.extension_pages || []).find((page) => page.kind === kind)
  const slots = (declared && declared.slots) || {}
  return { one: slots.one || null, many: slots.many || null,
           max: slots.max || 0, slots: slots.label || "Values" }
}

function renderKindPicker() {
  const groups = new Map()
  for (const [kind, shape] of Object.entries(caps.kinds)) {
    if (!groups.has(shape.group)) groups.set(shape.group, [])
    groups.get(shape.group).push(el("option", { value: kind, textContent: shape.title,
                                                title: shape.summary }))
  }
  $("kind").replaceChildren(...[...groups].map(
    ([label, options]) => el("optgroup", { label }, options)))
}

function singular(label) {
  if (label === "Series") return label
  if (label === "Axes") return "Axis"
  return label.endsWith("s") ? label.slice(0, -1) : label
}

const MAX_SETTING = { key: "max", label: "Full scale", type: "number", min: 0, step: "any",
                      placeholder: "automatic" }

function pageCard(page, index) {
  const shape = shapeFor(page.kind)
  const open = expanded.has(page.id)
  const settings = (caps.extension_page_settings || {})[page.kind] || []

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
  title.oninput = () => { page.title = title.value; showTitle(); markDirty() }

  const toggle = el("button", { type: "button", textContent: open ? "▾" : "▸",
                                title: open ? "Collapse" : "Configure",
                                "aria-expanded": String(open) })
  toggle.onclick = () => {
    if (open) expanded.delete(page.id); else expanded.add(page.id)
    renderPages()
  }

  const remove = el("button", { type: "button", className: "danger small",
                                textContent: "✕", title: "Remove this page" })
  remove.onclick = () => {
    if (config.pages.length <= 1) return toast("Keep at least one page", true)
    if (!window.confirm(`Remove ${page.title || page.kind}?`)) return undefined
    config.pages.splice(index, 1)
    markDirty()
    return renderPages()
  }

  const kind = el("h3", { title: "Drag to reorder" },
                  el("span", { className: "kind", textContent: kindLabel(page.kind) }),
                  titled)
  const item = el("li", null, el("header", null, kind, toggle, remove))

  if (open) {
    item.append(el("label", { htmlFor: titleId, textContent: "Title" }), title,
                slotList(page, shape),
                ...(shape.scaled ? settingRow(page, MAX_SETTING) : []),
                ...settings.flatMap((setting) => settingRow(page, setting)),
                el("footer", null, moveButtons(index), addSlot(page, shape)))
  } else {
    const refs = shape.one ? [page[shape.one]] : (page[shape.many] || [])
    const named = refs.filter(Boolean).map(fieldLabel)
    const extra = settings.map((setting) => page[setting.key]).filter(Boolean)
    if (shape.scaled && page.max) extra.push(`full scale ${page.max}`)
    item.append(el("p", { textContent: named.concat(extra).join(", ") || "nothing chosen" }))
  }

  reorderable(item, config.pages, index, { tag: "page", along: "x", handle: kind })
  return item
}

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
                   refSelect(ref, poolFor(shape.many_pool), (value) => { current[slot] = value }),
                   drop)
    reorderable(row, current, slot, { tag: "slot", along: "y" })
    rows.push(row)
  })

  return el("ol", null, rows)
}

function addSlot(page, shape) {
  const current = shape.many ? page[shape.many] || [] : []
  if (!shape.many || current.length >= shape.max) return null
  const add = el("button", { type: "button", className: "small add",
                             textContent: `Add ${singular(shape.slots).toLowerCase()}` })
  add.onclick = () => {
    page[shape.many] = current.concat([poolFor(shape.many_pool)[0]])
    markDirty()
    renderPages()
  }
  return add
}

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

function freshId(base, taken) {
  const stamp = Date.now().toString(36).slice(-4)
  let id = `${base}${stamp}`
  for (let n = 2; taken.has(id); n += 1) id = `${base}${stamp}${n}`
  taken.add(id)
  return id
}

function pageIds() {
  return new Set(config.pages.map((page) => page.id))
}

function newPage(kind) {
  const taken = pageIds()
  const offered = (caps.extension_pages || []).find((page) => page.kind === kind)
  if (offered) {
    return { ...offered, id: freshId(offered.id || kind, taken) }
  }
  const shape = shapeFor(kind)
  const pool = numericRefs()
  const page = { id: freshId(kind, taken), kind, title: kind }
  if (shape.one) {
    page[shape.one] = kind === "bars" ? "cpu.cores" : (pool[0] || "cpu.pct")
  }
  if (shape.many) {
    page[shape.many] = pool.slice(0, Math.min(2, shape.max))
  }
  return page
}

function offerExtensionPages() {
  const picker = $("kind")
  const offered = (caps.extension_pages || []).filter(
    (page) => ![...picker.options].some((option) => option.value === page.kind))
  if (!offered.length) return
  const group = picker.querySelector("optgroup[label=\"Extensions\"]")
    || picker.appendChild(el("optgroup", { label: "Extensions" }))
  group.append(...offered.map(
    (page) => el("option", { value: page.kind, textContent: page.title || page.kind })))
}

function offerRecipes() {
  const picker = $("recipe")
  const listed = caps.recipes || []
  const wanted = picker.value
  picker.replaceChildren(...listed.map((recipe) => el("option", {
    value: recipe.name, textContent: recipe.title, title: recipe.summary || null })))
  if (listed.some((recipe) => recipe.name === wanted)) picker.value = wanted
  picker.hidden = !listed.length
  $("quickadd").hidden = !listed.length
}

function quickAdd(name) {
  const recipe = (caps.recipes || []).find((entry) => entry.name === name)
  if (!recipe) return
  const taken = pageIds()
  const added = recipe.pages.map((page) => (
    { ...page, id: freshId(page.id || page.kind, taken) }))
  config.pages.unshift(...added)
  if (added.length === 1) expanded.add(added[0].id)
  else toast(`Added ${recipe.title}: ${added.length} pages.`)
  markDirty()
  renderPages()
}

let prunedWanted = 0

async function refreshPruned() {
  const mine = ++prunedWanted
  try {
    const shown = await api("/api/preview", { method: "POST", body: JSON.stringify(config) })
    if (mine !== prunedWanted) return
    const kept = new Set(shown.pages.map((page) => page.id))
    const dropped = config.pages.filter((page) => !kept.has(page.id)).map((page) => page.title)
    const node = pick('section[aria-label="Pages"] p[role="status"]')
    node.textContent = `Not shown on the badge, because this host reports no data for them: ${dropped.join(", ")}`
    node.hidden = !dropped.length
  } catch {}
}

function renderSettings() {
  const schema = caps.extension_settings || {}
  const installed = caps.extensions || []
  config.settings = config.settings || {}
  const intro = $("extensions").querySelector("p")
  $("extensions").replaceChildren(
    ...(intro ? [intro] : []),
    el("div", { className: "configured" },
       ...installed.map((extension) => extensionBox(extension, schema[extension.name] || []))),
    catalogueBox())
}

const openExtensions = new Set()

function catalogued(name) {
  return ((catalogue && catalogue.offered) || []).find((entry) => entry.name === name)
}

function displayName(name) {
  const listed = catalogued(name)
  return (listed && listed.title) || name
}

function wants(needs) {
  return needs ? el("span", { className: "wants", textContent: `needs ${needs}` }) : null
}

function givenName(name, version) {
  const given = `statsbadge-${name}`
  return version ? `${given} @ ${version}` : given
}

let catalogue = null
const installing = new Set()

async function refreshCatalogue() {
  try {
    catalogue = await api("/api/extensions")
  } catch (error) {
    catalogue = null
  }
  renderSettings()
}

let behind = {}
let behindWhy = null
let checking = false

async function refreshOutdated() {
  checking = true
  renderSettings()
  let found
  try {
    found = await api("/api/extensions/outdated")
  } catch (error) {
    behindWhy = error.message || "the server did not answer"
    checking = false
    renderSettings()
    return
  }
  behind = {}
  for (const entry of found.outdated || []) {
    behind[entry.name.replace(/^statsbadge-/, "")] = entry.latest
  }
  behindWhy = found.why || null
  checking = false
  renderSettings()
}

function catalogueBox() {
  const box = el("section", { className: "offer" },
                 el("h3", { textContent: "Extensions" }))
  if (!catalogue) {
    box.append(el("p", { textContent: "Could not read the list of extensions." }))
    return box
  }
  if (!catalogue.manageable) {
    box.append(el("p", { textContent:
      `Installing needs uv or pip, and this server can reach neither. It runs from ${catalogue.prefix}, and a tray started at login carries the PATH it was given then. Add them with uv pip install, or start it again from a terminal.` }))
  }
  box.append(el("ul", { className: "catalogue" }, ...catalogue.offered.map(offerRow)))
  box.append(updateCheck())
  box.append(freeformForm())
  return box
}

function updateCheck() {
  const said = checking ? "Checking for updates..."
    : behindWhy ? `Could not check for updates: ${behindWhy}`
    : Object.keys(behind).length ? "" : "Everything is up to date."
  return el("p", { className: behindWhy ? "bad" : null },
            el("small", { textContent: said }),
            el("button", { type: "button", textContent: "Check again",
                           disabled: checking, onclick: () => refreshOutdated() }))
}

function offerRow(entry) {
  const notes = [givenName(entry.name, entry.version)]
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
               wants(entry.needs),
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

async function changeExtension(verb, name) {
  installing.add(name)
  renderSettings()
  let done
  try {
    done = await api("/api/extensions", {
      method: "POST",
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
    if (!(done.stuck || []).length && !done.nothing) {
      toast({ add: `Installed ${name}`, remove: `Removed ${name}`,
              upgrade: `Updated ${name}`, disable: `Switched ${name} off`,
              enable: `Switched ${name} on` }[verb])
    }
    for (const note of done.unpinned || []) toast(note)
    for (const name of done.restart || []) {
      toast(`Restart statsbadge to run the new ${name}`)
    }
    for (const entry of done.stuck || []) {
      toast(`Unable to uninstall ${entry.name}. It is installed in statsbadge's own environment, so whatever put it there has to take it out.`, true)
    }
    for (const entry of done.shadowed || []) {
      toast(`${entry.name} is already installed in statsbadge's own environment. That copy is the one that runs.`)
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
  const listed = catalogued(extension.name)
  const head = el("h3", null,
                  el("span", { textContent: displayName(extension.name) }),
                  wants(listed && listed.needs))
  const box = el("section", null, el("header", null, head), state)
  if (!settings.length) return box

  const toggle = el("button", { type: "button", textContent: open ? "▾" : "▸",
                                title: open ? "Collapse" : "Configure",
                                "aria-expanded": String(open) })
  toggle.onclick = () => {
    if (open) openExtensions.delete(extension.name)
    else openExtensions.add(extension.name)
    renderSettings()
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

const SECRET_SHOWN = 6
const SECRET_MAX = 18

function masked(value) {
  const text = value === null || value === undefined ? "" : String(value)
  if (!text) return ""
  const shown = text.slice(0, SECRET_SHOWN)
  return shown + "x".repeat(Math.max(4, Math.min(SECRET_MAX, text.length - shown.length)))
}

const editingSecrets = new Set()

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
    renderSettings()
  }
  block.append(button)
  return block
}

let controlSerial = 0

function settingRow(stored, setting, options) {
  const id = `setting${++controlSerial}`
  const label = el("label", { htmlFor: id, textContent: setting.label || setting.key })
  const current = stored[setting.key] !== undefined ? stored[setting.key] : setting.default

  let input
  if (setting.type === "bool") {
    input = el("input", { type: "checkbox", id, checked: !!current })
    input.onchange = () => { stored[setting.key] = input.checked; markDirty() }
  } else if (setting.type === "number") {
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
      input.autocomplete = "off"
      input.spellcheck = false
      input.placeholder = (options && options.reveal) ? "paste the key here" : ""
    }
    input.oninput = () => {
      stored[setting.key] = input.value === "" ? null : input.value
      markDirty()
    }
  }
  return setting.unit
    ? [label, input, el("small", { textContent: setting.unit })]
    : [label, input]
}

function renderLook() {
  themeTab = null
  renderThemes()
  renderTint()
  fetchPalettes()

  bindRange("interval", "interval_ms", (value) => `${value} ms`)
  bindRange("brightness", "brightness", (value) => `${value}%`, 100)
  bindRange("points", "graph_points", (value) => `${value}`)
  bindRange("idle", "idle_advance_s", (value) => (value === "0" ? "off" : `${value}s idle`))
  bindRange("advance", "advance_every_s", (value) => `${value}s`)

  bindSelect("smooth", () => (config.smooth === false ? "straight" : "curved"),
             (value) => { config.smooth = value === "curved" })
  bindSelect("rows", () => config.rows || "zebra", (value) => { config.rows = value })
  const turn = () => (typeof config.slide === "string" ? config.slide
    : (config.slide ? "over" : "off"))
  bindSelect("slide", turn, (value) => { config.slide = value })
  bindSelect("gaugefill", () => config.gauge_fill || "solid", (value) => {
    config.gauge_fill = value
    preview()
    paintThumbs()
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
  const value = Math.round((config[key] || 0) * factor)
  input.min = Math.min(Number(input.min), value)
  input.max = Math.max(Number(input.max), value)
  input.value = value
  out.textContent = format(String(value))
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

function renderCaseLights() {
  const mode = $("caselights")
  const stored = config.caselights
  const chosen = stored === true ? "theme" : stored ? "reading" : "off"
  const refs = numericRefs()
  const offered = [["off", "Off"], ["theme", "Follow the Backlight"]]
  if (refs.length || chosen === "reading") offered.push(["reading", "Follow a Reading"])
  mode.replaceChildren(...offered.map(([value, text]) =>
    el("option", { value, textContent: text, selected: value === chosen })))

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

let previewWanted = 0

let themeTab = null

let palettes = {}
let palettesWanted = 0

const THEME_TABS = [["dark", "Dark"], ["light", "Light"], ["tinted", "Tinted"]]
const THUMB_W = 160
const THUMB_H = 120
const THUMB_READING = 0.63

const tabOf = (record) => (!record ? "dark" : record.derived ? "tinted" : record.mode)

function cardLabel(record) {
  if (record.derived) return record.label
  const suffix = ` ${titleCase(record.mode)}`
  return record.label.endsWith(suffix) ? record.label.slice(0, -suffix.length) : record.label
}

function renderThemes() {
  if (!themeTab) themeTab = tabOf((caps.themes || []).find((entry) => entry.name === config.theme))
  const tabs = el("div", { className: "tabs" }, THEME_TABS.map(([name, text]) => {
    const tab = el("button", { type: "button", textContent: text,
                               "aria-pressed": String(name === themeTab) })
    tab.onclick = () => { themeTab = name; renderThemes() }
    return tab
  }))
  const shown = (caps.themes || []).filter((record) => tabOf(record) === themeTab)
  if (themeTab === "tinted") shown.sort((a, b) => (a.mode === b.mode ? 0 : a.mode === "dark" ? -1 : 1))
  const cards = el("div", { className: "cards" }, shown.map((record) => {
    const card = el("button", { type: "button", "data-theme": record.name,
                                "aria-pressed": String(record.name === config.theme) },
                    el("canvas", { width: THUMB_W * 2, height: THUMB_H * 2 }),
                    el("span", { textContent: cardLabel(record) }))
    card.onclick = () => {
      config.theme = record.name
      markDirty()
      renderThemes()
      preview()
    }
    return card
  }))
  $("theme").replaceChildren(tabs, cards)
  for (const node of all("[data-tint]")) node.hidden = themeTab !== "tinted"
  paintThumbs()
}

async function fetchPalettes() {
  const query = new URLSearchParams({ accent: (config.tint || []).join(","),
                                      second: config.accent_b || "same" })
  const mine = ++palettesWanted
  let answer
  try {
    answer = await api(`/api/themes?${query}`)
  } catch (error) {
    return
  }
  if (mine !== palettesWanted) return
  palettes = answer.palettes
  paintThumbs()
}

function paintThumbs() {
  for (const card of all("#theme .cards button")) {
    const palette = palettes[card.dataset.theme]
    if (!palette) continue
    const ctx = card.querySelector("canvas").getContext("2d")
    ctx.setTransform(2, 0, 0, 2, 0, 0)
    drawThumb(ctx, palette)
  }
}

function renderTint() {
  const second = $("accentb")
  if (!second.options.length) {
    second.replaceChildren(...(caps.accent_b_rules || []).map((rule) =>
      el("option", { value: rule, textContent: titleCase(rule) })))
  }
  second.value = config.accent_b || "same"
  second.onchange = () => {
    config.accent_b = second.value
    markDirty()
    fetchPalettes()
    renderTint()
  }

  pick("div.accents").replaceChildren(swatches())
  preview()
}

function swatches() {
  const offered = Object.values(caps.accents || {}).flat()
  return el("div", { className: "swatches" }, offered.map((accent) => {
    const shown = `rgb(${accent.join(", ")})`
    const chip = el("button", { type: "button", title: shown,
                                "aria-pressed": String(String(config.tint) === String(accent)) })
    chip.style.background = shown
    chip.onclick = () => {
      config.tint = accent.slice()
      markDirty()
      fetchPalettes()
      renderTint()
    }
    return chip
  }))
}

const W = 320
const H = 240
const rgb = (parts) => `rgb(${parts.join(", ")})`
const face = (weight, size) => `${weight} ${size}px Lexend, system-ui, sans-serif`

const ICONS = {
  c: 0xe322, g: 0xe30d, m: 0xf7a3, d: 0xe1db, n: 0xeb2f, p: 0xea0b, f: 0xf168, y: 0xe31e,
  l: 0xe9e4, t: 0xf076, s: 0xe1b8, r: 0xe677, u: 0xf09b, o: 0xf090, b: 0xe1a5, e: 0xeb58,
  a: 0xeff2, h: 0xefd6,
}

const isPercent = (field) => caps.percent_fields.includes(field) || field.endsWith("_pct")

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

function shortUnit(field) {
  if (field.endsWith("_bps")) return "B/s"
  if (field === "cores" || field === "pct" || field.endsWith("_pct")) return "%"
  if (field.endsWith("_mb")) return "B"
  if (field === "uptime_s" || field === "secs_left") return ""
  return caps.units[field] || ""
}

function fractionOf(ref, value, frame) {
  if (value === null || value === undefined || typeof value === "string"
      || typeof value === "boolean") return null
  const field = ref.split(".").pop()
  let top
  if (isPercent(field)) top = 100
  else top = Number((frame?.peaks || {})[ref]) || caps.full_scale[field]
  if (!top) return null
  return Math.max(0, Math.min(1, value / top))
}

function readingOf(frame, ref) {
  const [group, field] = ref.split(".")
  let held = (frame || {})[group]
  if (Array.isArray(held)) held = held[0]
  return held === undefined || held === null ? null : held[field]
}

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

function readoutRows(count) {
  const room = BODY_TOP + BODY_H - 6 - count * READOUT_H
  const top = Math.max(BODY_TOP + 6, Math.min(DIAL_C[1] - DIAL_OUTER, room))
  return Array.from({ length: count }, (_, index) => top + index * READOUT_H)
}

const NAMES = {
  "cpu.pct": "LOAD", "cpu.temp": "TEMP", "cpu.freq": "CLOCK", "cpu.procs": "PROCS",
  "mem.pct": "USED", "mem.used_mb": "USED", "mem.total_mb": "TOTAL", "mem.swap_pct": "SWAP",
  "gpu.pct": "LOAD", "gpu.temp": "TEMP", "gpu.power": "POWER", "gpu.mem_pct": "VRAM",
  "net.up_bps": "UP", "net.down_bps": "DOWN", "net.up_total_mb": "SENT",
  "net.down_total_mb": "RECV",
  "disk.pct": "FULL", "disk.read_bps": "READ", "disk.write_bps": "WRITE",
  "disk.used_mb": "USED", "disk.total_mb": "TOTAL", "disk.temp": "SSD",
  "power.battery_pct": "BATTERY", "power.package_w": "PACKAGE", "power.temp": "BATT",
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

const DIAL = { field: "cpu.pct", readouts: ["cpu.temp", "cpu.freq", "cpu.procs"] }
const BARS = "cpu.cores"
const SERIES = ["net.down_bps", "net.up_bps"]
const TILE_REFS = ["disk.pct", "disk.read_bps", "disk.write_bps", "disk.used_mb"]

function chrome(ctx, palette, title, current) {
  ctx.textBaseline = "top"
  ctx.textAlign = "left"

  ctx.fillStyle = rgb(palette.bg)
  ctx.fillRect(0, BODY_TOP, W, BODY_H)

  ctx.fillStyle = rgb(palette.panel)
  ctx.fillRect(0, 0, W, HEADER_H)
  ctx.fillRect(0, H - FOOTER_H, W, FOOTER_H)

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

function gauge(ctx, palette, [cx, cy], outer, inner, reading) {
  const middle = (outer + inner) / 2
  const over = (outer - inner) * 0.15
  const at = (degrees) => ((degrees - 90) * Math.PI) / 180

  ctx.lineCap = "butt"
  ctx.lineWidth = outer - inner
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

  if (reading > 0.001) {
    ctx.beginPath()
    ctx.lineWidth = outer - inner + 2 * over
    ctx.arc(cx, cy, middle, at(sweep - 1.4), at(sweep + 1.4))
    ctx.strokeStyle = rgb(palette.ink)
    ctx.stroke()
    ctx.lineWidth = outer - inner
  }
}

function drawThumb(ctx, palette) {
  ctx.fillStyle = rgb(palette.bg)
  ctx.fillRect(0, 0, THUMB_W, THUMB_H)
  ctx.fillStyle = rgb(palette.accent)
  ctx.fillRect(0, THUMB_H - 8, THUMB_W, 8)
  gauge(ctx, palette, [46, 54], 30, 20, THUMB_READING)

  const text = String(Math.round(THUMB_READING * 100))
  ctx.textBaseline = "alphabetic"
  ctx.font = face(400, 30)
  const textW = ctx.measureText(text).width
  ctx.fillStyle = rgb(palette.ink)
  ctx.fillText(text, 86, 65)
  ctx.font = face(400, 14)
  ctx.fillStyle = rgb(palette.dim)
  ctx.fillText("%", 86 + textW + 1, 65)
}

function drawDial(ctx, palette, _series, frame) {
  chrome(ctx, palette, "CPU", 0)

  const value = readingOf(frame, DIAL.field)
  const reading = fractionOf(DIAL.field, value, frame) ?? 0.635
  const [cx, cy] = DIAL_C
  gauge(ctx, palette, DIAL_C, DIAL_OUTER, DIAL_INNER, reading)

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
function drawGraph(ctx, palette, series) {
  chrome(ctx, palette, "NETWORK", 4)

  const plots = SERIES.map((ref) => rings[ref] || [])
  const live = plots.some((ring) => ring.length > 1)
  const peak = live ? Math.max(...plots.flat().map((v) => v ?? 0), 1) * 1.15 : 9.8 * 1024 ** 2
  const peakText = fmt(peak, "down_bps") + shortUnit("down_bps")

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
    ctx.globalAlpha = palette.series_alpha[index] / 255
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

let shown = null
let frameNow = null
let rings = {}

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
      canvas.width = W * 2
      canvas.height = H * 2
      return canvas
    }))
  }
  SCREENS.forEach((paint, index) => {
    const ctx = holder.children[index].getContext("2d")
    ctx.setTransform(2, 0, 0, 2, 0, 0)
    paint(ctx, shown.palette, shown.palette.series, frameNow)
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

const REMEMBERED = "statsbadge.whose"

function badgeName(id) {
  return (badges[id] && badges[id].name) || id
}

function pickBadge() {
  let last = null
  try {
    last = window.localStorage.getItem(REMEMBERED)
  } catch {}
  if (last && badges[last]) return last
  return Object.keys(badges)[0] || null
}

function remember(id) {
  try {
    if (id) window.localStorage.setItem(REMEMBERED, id)
    else window.localStorage.removeItem(REMEMBERED)
  } catch {}
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

async function switchTo(id) {
  if (dirty && !window.confirm("Discard the unsaved changes to this badge?")) {
    renderWhose()
    return
  }
  whose = id || null
  remember(whose)
  config = await api(configPath(whose))
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
  dirty = false
  await switchTo(Object.keys(badges)[0] || "")
  toast("Forgotten")
}

function ownIds(pages, badgeId) {
  const tag = badgeId.slice(0, 4)
  return pages.map((page) => (String(page.id).endsWith(`-${tag}`)
    ? page : { ...page, id: `${page.id}-${tag}` }))
}

async function renderGeneral() {
  const node = $("general")
  const heading = node.querySelector("h2")
  let host
  try {
    host = await api("/api/settings")
  } catch (error) {
    node.replaceChildren(heading,
                         el("p", { className: "bad", textContent: error.message }))
    return
  }

  const place = el("input", { type: "text", id: "hostplace", value: host.place || "",
                              placeholder: "Sheffield, GB" })
  const degrees = (id, value, limit) =>
    el("input", { type: "number", id, step: "0.001", min: -limit, max: limit,
                  value: value === null || value === undefined ? "" : value })
  const latitude = degrees("hostlat", host.latitude, 90)
  const longitude = degrees("hostlon", host.longitude, 180)

  const save = el("button", { type: "button", className: "primary", textContent: "Save" })
  save.onclick = () => api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ place: place.value.trim(), latitude: latitude.value,
                           longitude: longitude.value }),
  }).then(() => {
    toast("Saved")
    return renderGeneral()
  }).catch((error) => toast(error.message, true))

  node.replaceChildren(
    heading,
    el("section", null,
       el("h3", { textContent: "Where this badge is" }),
       el("p", { textContent: "A town or city, and a country if the name is a common one: Sheffield, or Sheffield, US. Looked up once and kept, so every extension asking gets the same answer, and a page naming somewhere else overrides it." }),
       el("label", { htmlFor: "hostplace", textContent: "Location" }), place,
       el("label", { htmlFor: "hostlat", textContent: "Latitude" }), latitude,
       el("label", { htmlFor: "hostlon", textContent: "Longitude" }), longitude,
       el("menu", null, save),
       el("p", { textContent: "Coordinates win over the name, for a spot no name lands on. Clear all three to set nowhere." })))
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

function badgeBox(id) {
  const named = badges[id].name && badges[id].name !== id ? badges[id].name : ""
  const nameId = `badge${++controlSerial}`
  const name = el("input", { type: "text", id: nameId, value: named,
                             placeholder: "Give it a name" })
  const heading = el("h3", { textContent: named || "Unnamed badge" })

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

function appLabel(state) {
  if (!state) return "Not installed from here"
  const changes = state.added.length + state.changed.length + state.removed.length
  if (!changes) return "Up to date"
  return `${changes} file${changes === 1 ? "" : "s"} behind`
}

function themeLabel(name) {
  const record = (caps.themes || []).find((entry) => entry.name === name)
  return (record && record.label) || name || "unset"
}

async function rename(id, wanted) {
  const result = await api(`/api/badges/${id}`, {
    method: "PUT",
    body: JSON.stringify({ name: wanted }),
  })
  badges[id].name = result.name
  renderWhose()
  return result.name === id ? "Unnamed badge" : result.name
}

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
    panel.replaceChildren(...[
      el("p", { textContent: `On the badge: launch Stats, press B to set up, and pick ${(state.hosts || []).join(" / ")}:${state.port}` }),
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

let installPoll = null
let installer = null
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

function installerBox() {
  const status = el("p", { className: "found", textContent: "Looking for a badge…" })
  const ssid = el("input", { type: "text", id: "ssid", placeholder: "Network name" })
  const password = el("input", { type: "password", id: "wifipass" })
  const region = el("select", { id: "region" },
                    el("option", { value: "", textContent: "Leave as it is" }))
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
    asking.force_secrets = true
    if (installer.region.value) asking.region = installer.region.value
    if (installer.zone.value !== "") asking.timezone = Number(installer.zone.value)
  }
  installer.go.disabled = true
  await api("/api/install", { method: "POST",
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
  if (installer.region.options.length < 2) {
    installer.region.append(...(state.regions || []).map(
      (name) => el("option", { value: name, textContent: name })))
  }
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
    `${names.join(", ")} ${one ? "was" : "were"} last seen running an older app. Connect ${one ? "it" : "them"} by USB to update.`,
    button)
}

async function renderHelp() {
  const node = $("help")
  const intro = node.querySelector("p")
  let facts
  try {
    facts = await api("/api/help")
  } catch (error) {
    node.replaceChildren(intro, el("p", { className: "bad", textContent: error.message }))
    return
  }
  const reading = el("section", null,
                     el("h2", { textContent: "Reading now" }),
                     el("p", { textContent: (facts.sources || []).join(", ") || "nothing" }))
  node.replaceChildren(intro, ...helpFor(facts), reading)
}

function helpFor(facts) {
  if (facts.platform === "Darwin") return [macHelp(facts.powermetrics || {})]
  if (facts.platform === "Windows") return [windowsHelp(facts.lhm || {})]
  return [el("section", null,
             el("h2", { textContent: "Linux" }),
             el("p", { textContent: "Temperatures, fans and power come from the kernel through psutil, and need nothing set up. The tray needs GTK bindings from your distribution, and GNOME hosts none without the AppIndicator extension." }))]
}

function macHelp(state) {
  const box = el("section", null,
                 el("h2", { textContent: "macOS" }),
                 el("p", { textContent: "GPU load, VRAM, thermal pressure and, on Apple Silicon, temperatures need no setup." }))
  if (!state.there) {
    box.append(el("p", { textContent: "powermetrics is missing, so package power, CPU and GPU clocks and GPU power are unavailable." }))
    return box
  }
  if (state.permitted) {
    box.append(el("p", { className: "good",
                         textContent: "Package power, CPU and GPU clocks and GPU power are on, via powermetrics." }))
    return box
  }
  box.append(
    el("p", { textContent: "Package power, CPU and GPU clocks and GPU power need powermetrics, which runs as root. To let sudo run it, and only it, without a password, run:" }),
    el("pre", { textContent: `sudo visudo -f ${state.file}` }),
    el("p", { textContent: "and add:" }),
    el("pre", { textContent: state.sudoers }),
    el("p", { textContent: "Restart statsbadge to apply. statsbadge itself does not run as root." }))
  return box
}

function windowsHelp(state) {
  const box = el("section", null,
                 el("h2", { textContent: "Windows" }),
                 el("p", { textContent: "Temperatures, fan speeds and package power need a kernel driver on Windows. statsbadge reads them from LibreHardwareMonitor's web server." }))
  box.append(state.reading
    ? el("p", { className: "good", textContent: "Connected to LibreHardwareMonitor." })
    : el("p", { textContent: "Not connected. Run LibreHardwareMonitor and turn on Options > Remote Web Server > Run. Download it from the project's GitHub releases; the similarly named .com site is unofficial." }))

  const field = el("input", { type: "text", id: "lhmurl", value: state.url || "",
                              placeholder: state.default })
  const save = el("button", { type: "button", className: "primary", textContent: "Save" })
  save.onclick = () => api("/api/settings", {
    method: "POST",
    body: JSON.stringify({ lhm_url: field.value.trim() || state.default }),
  }).then(() => {
    toast("Saved")
    return renderHelp()
  }).catch((error) => toast(error.message, true))

  box.append(el("label", { htmlFor: "lhmurl", textContent: "Web server address" }), field,
             el("menu", null, save),
             el("p", { textContent: "Only needed if it is not on the default port." }))
  return box
}

const FRAME_SCALARS = ["v", "t", "seq", "slow_rev"]
const FRAME_META = FRAME_SCALARS.concat(["peaks"])

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

  const peaks = $("peaks")
  const measurements = frame.peaks && Object.keys(frame.peaks).length
  if (measurements) peaks.replaceChildren(peaks.querySelector("h3"), readingList(frame.peaks))
  peaks.hidden = !measurements
}

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

const SHOWN = 48

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
    if (value && typeof value === "object") shown.title = JSON.stringify(value)
    if (caps.percent_fields.includes(key) && typeof value === "number") {
      shown.style.setProperty("--at", `${Math.max(0, Math.min(100, value))}%`)
    }
    rows.push(el("dt", { textContent: key }), shown)
  }
  return el("dl", null, rows)
}

function renderSources() {
  $("sources").querySelector("ul").replaceChildren(...caps.sources.map((source) => {
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

function capsSignature() {
  const faults = (caps.sources || []).map((source) => [source.name, source.last_fault])
  return JSON.stringify([caps.available, caps.extension_settings, caps.graphed,
                         caps.group_source, caps.extension_pages, caps.recipes, faults,
                         caps.commands, caps.local_actions, caps.themes])
}

async function refreshCaps() {
  if (dirty) return false
  let fresh
  try {
    fresh = await api("/api/capabilities")
  } catch (error) { return false }
  const before = capsSignature()
  caps = fresh
  if (capsSignature() === before) return false
  offerExtensionPages()
  offerRecipes()
  renderPages()
  renderSettings()
  renderSources()
  renderButtons()
  renderCaseLights()
  renderThemes()
  return true
}

async function refreshCapsSoon(delays = [400, 1200, 3000, 6000]) {
  for (const delay of delays) {
    await new Promise((wake) => setTimeout(wake, delay))
    if (await refreshCaps()) return
  }
}

async function save() {
  try {
    if (whose && badges[whose] && !badges[whose].configured) {
      config.pages = ownIds(config.pages, whose)
    }
    const sent = edits
    const result = await api(configPath(whose), {
      method: "PUT",
      body: JSON.stringify(config),
    })
    config.rev = result.rev
    if (whose && badges[whose]) badges[whose].configured = true
    if (edits === sent) {
      dirty = false
      $("save").disabled = true
    }
    toast(`Saved. ${whose ? badgeName(whose) : "Badges using the default layout"} will update shortly.`)
    refreshCapsSoon().catch(() => {})
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
    config = await api(configPath(whose))
  } catch (error) {
    document.body.replaceChildren(el("p", {
      textContent: `Cannot reach the server: ${error.message}` }))
    return
  }
  if (caps.statsbadge_version !== "unknown") $("version").textContent = `v${caps.statsbadge_version}`
  renderKindPicker()
  offerExtensionPages()
  offerRecipes()
  renderWhose()
  renderPages()
  renderSettings()
  renderLook()
  renderSources()
  renderBadges()
  renderGeneral().catch(() => {})
  renderLive()
  refreshCatalogue().then(refreshOutdated).catch(() => {})

  $("save").onclick = save
  $("usb").onclick = openInstaller
  const form = pick("main form")
  form.onsubmit = (event) => {
    event.preventDefault()
    config.pages.unshift(newPage($("kind").value))
    expanded.add(config.pages[0].id)
    markDirty()
    renderPages()
  }
  $("quickadd").onclick = () => quickAdd($("recipe").value)
  watchPairing().catch(() => {})

  setInterval(renderLive, 1000)
  window.onbeforeunload = () => (dirty ? "You have unsaved changes." : undefined)
}

boot()
