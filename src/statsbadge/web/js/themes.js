import { api } from "./api.js"
import { el, titleCase } from "./dom.js"
import { drawThumb, THUMB_H, THUMB_W } from "./preview.js"

const THEME_TABS = [["dark", "Dark"], ["light", "Light"], ["tinted", "Tinted"]]

const tabOf = (record) => (!record ? "dark" : record.derived ? "tinted" : record.mode)

function cardLabel(record) {
  if (record.derived) return record.label
  const suffix = ` ${titleCase(record.mode)}`
  return record.label.endsWith(suffix) ? record.label.slice(0, -suffix.length) : record.label
}

export function createThemes({ picker, tintNodes, accents, second, changed, repaint }) {
  let config = null
  let caps = null
  let themeTab = null
  let palettes = {}
  let palettesWanted = 0

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
        changed()
        renderThemes()
        repaint()
      }
      return card
    }))
    picker.replaceChildren(tabs, cards)
    for (const node of tintNodes) node.hidden = themeTab !== "tinted"
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
    for (const card of picker.querySelectorAll(".cards button")) {
      const palette = palettes[card.dataset.theme]
      if (!palette) continue
      const ctx = card.querySelector("canvas").getContext("2d")
      ctx.setTransform(2, 0, 0, 2, 0, 0)
      drawThumb(ctx, palette, config.gauge_fill)
    }
  }

  function renderTint() {
    if (!second.options.length) {
      second.replaceChildren(...(caps.accent_b_rules || []).map((rule) =>
        el("option", { value: rule, textContent: titleCase(rule) })))
    }
    second.value = config.accent_b || "same"
    second.onchange = () => {
      config.accent_b = second.value
      changed()
      fetchPalettes()
      renderTint()
    }

    accents.replaceChildren(swatches())
    repaint()
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
        changed()
        fetchPalettes()
        renderTint()
      }
      return chip
    }))
  }

  function render(currentConfig, currentCaps) {
    config = currentConfig
    caps = currentCaps
    themeTab = null
    renderThemes()
    renderTint()
    fetchPalettes()
  }

  function refresh(currentConfig, currentCaps) {
    config = currentConfig
    caps = currentCaps
    renderThemes()
  }

  return { render, refresh, paintThumbs }
}
