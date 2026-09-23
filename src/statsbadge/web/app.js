import { $, all, el, pick, titleCase, toast } from "./js/dom.js"
import { api, configPath } from "./js/api.js"
import { nextId, settingRow } from "./js/forms.js"
import { readingOf } from "./js/format.js"
import { fieldLabel, numericRefs, poolFor, refSelect } from "./js/refs.js"
import { drawThumb, H, rgb, SCREENS, SERIES, THUMB_H, THUMB_W, W } from "./js/preview.js"

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

  const titleId = nextId("page")
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
                ...(shape.scaled ? settingRow(page, MAX_SETTING, markDirty) : []),
                ...settings.flatMap((setting) => settingRow(page, setting, markDirty)),
                el("footer", null, moveButtons(index), addSlot(page, shape)))
  } else {
    const refs = shape.one ? [page[shape.one]] : (page[shape.many] || [])
    const named = refs.filter(Boolean).map((ref) => fieldLabel(caps, ref))
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

function slotList(page, shape) {
  const rows = []

  if (shape.one) {
    rows.push(el("li", null,
                 el("span", { textContent: page.kind === "bars" ? "List" : "Gauge" }),
                 refSelect(caps, page[shape.one], poolFor(caps, shape.pool),
                           (value) => { page[shape.one] = value; markDirty() })))
  }

  const current = shape.many ? page[shape.many] || [] : []
  current.forEach((ref, slot) => {
    const drop = el("button", { type: "button", className: "small", textContent: "−",
                                title: "Remove this slot" })
    drop.onclick = () => { current.splice(slot, 1); markDirty(); renderPages() }
    const row = el("li", null,
                   el("span", { className: "grip", textContent: "⋮" }),
                   refSelect(caps, ref, poolFor(caps, shape.many_pool),
                             (value) => { current[slot] = value; markDirty() }),
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
    page[shape.many] = current.concat([poolFor(caps, shape.many_pool)[0]])
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
  const pool = numericRefs(caps)
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
    box.append(...settingRow(stored, setting, markDirty))
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
      block.append(...settingRow(stored, setting, markDirty, { reveal: true }))
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
  const refs = numericRefs(caps)
  const offered = [["off", "Off"], ["theme", "Follow the Backlight"]]
  if (refs.length || chosen === "reading") offered.push(["reading", "Follow a Reading"])
  mode.replaceChildren(...offered.map(([value, text]) =>
    el("option", { value, textContent: text, selected: value === chosen })))

  let following = typeof stored === "string" ? stored : refs[0]

  const row = $("caselightref")
  row.hidden = chosen !== "reading"
  row.replaceChildren(...refSelect(caps, following, refs, (ref) => {
    following = ref
    config.caselights = ref
    markDirty()
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
    drawThumb(ctx, palette, config.gauge_fill)
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
    paint(ctx, shown.palette, shown.palette.series,
          { frame: frameNow, history: rings, gaugeFill: config.gauge_fill, caps })
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
  const nameId = nextId("badge")
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
