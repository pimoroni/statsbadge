import { api, configPath } from "./api.js"
import { badgeName, createBadges } from "./badges.js"
import { $, all, el, pick, toast } from "./dom.js"
import { createExtensions } from "./extensions.js"
import { createGeneral } from "./general.js"
import { createHelp } from "./help.js"
import { createInstaller } from "./installer.js"
import { createLive, frameShape } from "./live.js"
import { createLook } from "./look.js"
import { createPages } from "./pages.js"
import { createScreens } from "./screens.js"
import { createThemes } from "./themes.js"

let config = null
let capabilities = null
let dirty = false
let edits = 0
let whose = null
let badges = {}

const pageKindSelect = $("kind")

const screens = createScreens({ holder: $("screens"), chip: $("accentbchip") })
const themes = createThemes({
  holder: $("theme"),
  tintNodes: all("[data-tint]"),
  accents: pick("div.accents"),
  second: $("accentb"),
  changed: markDirty,
  repaint: () => screens.show(config, capabilities),
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
    screens.show(config, capabilities)
    themes.paintThumbs()
  },
})
const pages = createPages({
  list: $("pages"),
  status: pick('section[aria-label="Pages"] p[role="status"]'),
  pageKindSelect,
  recipeSelect: $("recipe"),
  quickAddButton: $("quickadd"),
  changed: markDirty,
})
const extensions = createExtensions({
  holder: $("extensions"),
  changed: markDirty,
  onChanged: () => refreshCapabilities(),
})
const badgeView = createBadges({
  badgeSelect: pick("header label select"),
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
    badgeView.renderBadgeSelect(badges, whose)
    badgeView.render(badges, whose, capabilities)
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
  themes.render(config, capabilities)
  look.render(config, capabilities)
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
    badgeView.renderBadgeSelect(badges, whose)
    return
  }
  whose = id || null
  remember(whose)
  config = await api(configPath(whose))
  dirty = false
  $("save").disabled = true
  badgeView.renderBadgeSelect(badges, whose)
  pages.render(config, capabilities)
  extensions.render(config, capabilities)
  renderLook()
  badgeView.render(badges, whose, capabilities)
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
    badgeView.renderBadgeSelect(badges, whose)
    badgeView.render(badges, whose, capabilities)
  }
}

function ownIds(layoutPages, badgeId) {
  const tag = badgeId.slice(0, 4)
  return layoutPages.map((page) => (String(page.id).endsWith(`-${tag}`)
    ? page : { ...page, id: `${page.id}-${tag}` }))
}

let liveGroups = ""

async function renderLive() {
  let frame
  try {
    frame = await api("/api/stats")
  } catch (error) { return }

  screens.push(frame, config, capabilities)

  const shape = frameShape(frame)
  if (shape !== liveGroups && !dirty) {
    liveGroups = shape
    refreshCapabilities().catch(() => {})
  }

  if (statsSheet.hidden) return

  live.render(frame, capabilities)
}

function capabilitiesSignature() {
  const faults = (capabilities.sources || []).map((source) => [source.name, source.last_fault])
  return JSON.stringify([capabilities.available, capabilities.extension_settings, capabilities.graphed,
                         capabilities.group_source, capabilities.extension_pages, capabilities.recipes, faults,
                         capabilities.commands, capabilities.local_actions, capabilities.themes])
}

async function refreshCapabilities() {
  if (dirty) return false
  let fresh
  try {
    fresh = await api("/api/capabilities")
  } catch (error) { return false }
  const before = capabilitiesSignature()
  capabilities = fresh
  if (capabilitiesSignature() === before) return false
  pages.offer(capabilities)
  pages.render(config, capabilities)
  extensions.render(config, capabilities)
  live.renderSources(capabilities)
  look.refresh(config, capabilities)
  themes.refresh(config, capabilities)
  return true
}

async function refreshCapabilitiesSoon(delays = [400, 1200, 3000, 6000]) {
  for (const delay of delays) {
    await new Promise((wake) => setTimeout(wake, delay))
    if (await refreshCapabilities()) return
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
    refreshCapabilitiesSoon().catch(() => {})
    badges = await api("/api/badges").catch(() => badges)
    badgeView.renderBadgeSelect(badges, whose)
    pages.render(config, capabilities)
    badgeView.render(badges, whose, capabilities)
  } catch (error) {
    toast(error.message, true)
  }
}

async function boot() {
  bindTabs()
  try {
    [capabilities, badges] = await Promise.all([api("/api/capabilities"), api("/api/badges")])
    whose = pickBadge()
    config = await api(configPath(whose))
  } catch (error) {
    document.body.replaceChildren(el("p", {
      textContent: `Cannot reach the server: ${error.message}` }))
    return
  }
  if (capabilities.statsbadge_version !== "unknown") $("version").textContent = `v${capabilities.statsbadge_version}`
  pages.renderKinds(capabilities)
  pages.offer(capabilities)
  badgeView.renderBadgeSelect(badges, whose)
  pages.render(config, capabilities)
  extensions.render(config, capabilities)
  renderLook()
  live.renderSources(capabilities)
  badgeView.render(badges, whose, capabilities)
  general.render().catch(() => {})
  renderLive()
  extensions.refreshCatalogue().then(extensions.refreshOutdated).catch(() => {})

  $("save").onclick = save
  $("usb").onclick = usbInstaller.open
  const form = pick("main form")
  form.onsubmit = (event) => {
    event.preventDefault()
    pages.add(pageKindSelect.value)
  }
  $("quickadd").onclick = () => pages.quickAdd($("recipe").value)
  badgeView.watchPairing().catch(() => {})

  setInterval(renderLive, 1000)
  window.onbeforeunload = () => (dirty ? "You have unsaved changes." : undefined)
}

boot()
