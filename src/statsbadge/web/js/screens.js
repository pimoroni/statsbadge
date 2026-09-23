import { api } from "./api.js"
import { el } from "./dom.js"
import { readingOf } from "./format.js"
import { H, rgb, SCREENS, SERIES, W } from "./preview.js"

const GRAPH_POINTS = 48

export function createScreens({ holder, chip }) {
  let config = null
  let caps = null
  let previewWanted = 0
  let shown = null
  let frameNow = null
  let rings = {}

  async function seedHistory() {
    try {
      rings = await api(`/api/history?keys=${SERIES.join(",")}&points=${GRAPH_POINTS}`)
    } catch (error) { rings = {} }
  }

  function paintScreens() {
    if (!shown) return
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

  function push(frame, currentConfig, currentCaps) {
    config = currentConfig
    caps = currentCaps
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

  async function show(currentConfig, currentCaps) {
    config = currentConfig
    caps = currentCaps
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
    chip.style.background = rgb(answer.palette.accent_b || answer.palette.accent)
    if (!Object.keys(rings).length) await seedHistory()
    paintScreens()
  }

  return { show, push }
}
