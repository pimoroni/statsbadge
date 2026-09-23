import { $, all, el, pick, toast } from "./js/dom.js"
import { api, configPath } from "./js/api.js"
import { createScreens } from "./js/screens.js"
import { badgeName, createBadges } from "./js/badges.js"
import { createExtensions } from "./js/extensions.js"
import { createGeneral } from "./js/general.js"
import { createHelp } from "./js/help.js"
import { createInstaller } from "./js/installer.js"
import { createLive, frameShape } from "./js/live.js"
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
const help = createHelp({ holder: $("help") })
const live = createLive({
  live: $("live"),
  fromExtensions: $("from-extensions"),
  peaks: $("peaks"),
  sources: $("sources"),
})
const statsSheet = $("live").closest("main > section")
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
  if (tabs[wanted] && tabs[wanted].textContent === "Help") help.render().catch(() => {})
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

let liveGroups = ""

async function renderLive() {
  let frame
  try {
    frame = await api("/api/stats")
  } catch (error) { return }

  screens.push(frame, config, caps)

  const shape = frameShape(frame)
  if (shape !== liveGroups && !dirty) {
    liveGroups = shape
    refreshCaps().catch(() => {})
  }

  if (statsSheet.hidden) return

  live.render(frame, caps)
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
  live.renderSources(caps)
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
  live.renderSources(caps)
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
