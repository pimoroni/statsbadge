import { $, all, el, pick, toast } from "./js/dom.js"
import { api, configPath } from "./js/api.js"
import { createScreens } from "./js/screens.js"
import { badgeName, createBadges } from "./js/badges.js"
import { createExtensions } from "./js/extensions.js"
import { createGeneral } from "./js/general.js"
import { createInstaller } from "./js/installer.js"
import { createLook } from "./js/look.js"
import { createPages } from "./js/pages.js"
import { createThemes } from "./js/themes.js"

let config = null
let caps = null
let dirty = false
let edits = 0
let whose = null
let badges = {}

const screens = createScreens({ holder: $("screens"), chip: $("accentbchip") })
const themes = createThemes({
  picker: $("theme"),
  tintNodes: all("[data-tint]"),
  accents: pick("div.accents"),
  second: $("accentb"),
  changed: markDirty,
  repaint: () => screens.show(config, caps),
})
const look = createLook({
  controls: Object.fromEntries(["interval", "brightness", "points", "idle", "advance", "smooth",
                                "rows", "slide", "gaugefill", "animate", "plotanim", "autobright"]
    .map((id) => [id, $(id)])),
  outputs: Object.fromEntries(all("output[for]").map((out) => [out.getAttribute("for"), out])),
  caseLights: $("caselights"),
  caseLightRef: $("caselightref"),
  buttons: { a: $("btn-a"), b: $("btn-b"), c: $("btn-c") },
  changed: markDirty,
  onGaugeFill: () => {
    screens.show(config, caps)
    themes.paintThumbs()
  },
})
const pages = createPages({
  list: $("pages"),
  status: pick('section[aria-label="Pages"] p[role="status"]'),
  kindPicker: $("kind"),
  recipePicker: $("recipe"),
  quickAddButton: $("quickadd"),
  changed: markDirty,
})
const extensions = createExtensions({
  holder: $("extensions"),
  changed: markDirty,
  onChanged: () => refreshCaps(),
})
const badgeView = createBadges({
  picker: pick("header label select"),
  note: pick("header > small"),
  holder: $("badges"),
  stale: $("stale"),
  pairButton: $("pair"),
  pairingPanel: $("pairing"),
  onSwitch: (id) => switchTo(id),
  onForgotten: forgotten,
  onPaired: paired,
  onUpdate: () => usbInstaller.open(),
})
const general = createGeneral({ holder: $("general") })
const usbInstaller = createInstaller({
  panel: $("installer"),
  onFinished: async () => {
    badges = await api("/api/badges").catch(() => badges)
    badgeView.renderPicker(badges, whose)
    badgeView.render(badges, whose, caps)
  },
})

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

function renderLook() {
  themes.render(config, caps)
  look.render(config, caps)
}

const REMEMBERED = "statsbadge.whose"

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

async function switchTo(id) {
  if (dirty && !window.confirm("Discard the unsaved changes to this badge?")) {
    badgeView.renderPicker(badges, whose)
    return
  }
  whose = id || null
  remember(whose)
  config = await api(configPath(whose))
  dirty = false
  $("save").disabled = true
  badgeView.renderPicker(badges, whose)
  pages.render(config, caps)
  extensions.render(config, caps)
  renderLook()
  badgeView.render(badges, whose, caps)
}

async function forgotten() {
  badges = await api("/api/badges")
  dirty = false
  await switchTo(Object.keys(badges)[0] || "")
}

async function paired(approved) {
  badges = await api("/api/badges")
  if (approved && !dirty) {
    await switchTo(approved)
  } else {
    badgeView.renderPicker(badges, whose)
    badgeView.render(badges, whose, caps)
  }
}

function ownIds(pages, badgeId) {
  const tag = badgeId.slice(0, 4)
  return pages.map((page) => (String(page.id).endsWith(`-${tag}`)
    ? page : { ...page, id: `${page.id}-${tag}` }))
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

  screens.push(frame, config, caps)

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
  pages.offer(caps)
  pages.render(config, caps)
  extensions.render(config, caps)
  renderSources()
  look.refresh(config, caps)
  themes.refresh(config, caps)
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
    toast(`Saved. ${whose ? badgeName(badges, whose) : "Badges using the default layout"} will update shortly.`)
    refreshCapsSoon().catch(() => {})
    badges = await api("/api/badges").catch(() => badges)
    badgeView.renderPicker(badges, whose)
    pages.render(config, caps)
    badgeView.render(badges, whose, caps)
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
  pages.renderKinds(caps)
  pages.offer(caps)
  badgeView.renderPicker(badges, whose)
  pages.render(config, caps)
  extensions.render(config, caps)
  renderLook()
  renderSources()
  badgeView.render(badges, whose, caps)
  general.render().catch(() => {})
  renderLive()
  extensions.refreshCatalogue().then(extensions.refreshOutdated).catch(() => {})

  $("save").onclick = save
  $("usb").onclick = usbInstaller.open
  const form = pick("main form")
  form.onsubmit = (event) => {
    event.preventDefault()
    pages.add($("kind").value)
  }
  $("quickadd").onclick = () => pages.quickAdd($("recipe").value)
  badgeView.watchPairing().catch(() => {})

  setInterval(renderLive, 1000)
  window.onbeforeunload = () => (dirty ? "You have unsaved changes." : undefined)
}

boot()
