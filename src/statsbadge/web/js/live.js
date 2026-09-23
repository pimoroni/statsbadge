import { el } from "./dom.js"

const FRAME_SCALARS = ["v", "t", "seq", "slow_rev"]
const FRAME_META = FRAME_SCALARS.concat(["peaks"])

export function frameShape(frame) {
  return Object.keys(frame).filter((key) => !FRAME_META.includes(key)).join(",")
}

function hostGroups(capabilities) {
  const extensions = new Set((capabilities.extensions || []).map((extension) => extension.name))
  const groups = new Set()
  for (const source of capabilities.sources || []) {
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

function liveGroup(name, item, capabilities) {
  return el("section", null, el("h3", { textContent: name }), readingList(item, capabilities))
}

function readingList(item, capabilities) {
  const rows = []
  for (const key of Object.keys(item)) {
    const value = item[key]
    const shown = el("dd", { textContent: reading(value) })
    if (value && typeof value === "object") shown.title = JSON.stringify(value)
    if (capabilities.percent_fields.includes(key) && typeof value === "number") {
      shown.style.setProperty("--at", `${Math.max(0, Math.min(100, value))}%`)
    }
    rows.push(el("dt", { textContent: key }), shown)
  }
  return el("dl", null, rows)
}

export function createLive({ live, fromExtensions, peaks, sources }) {
  function render(frame, capabilities) {
    const measured = hostGroups(capabilities)
    const own = []
    const theirs = []
    for (const group of Object.keys(frame)) {
      if (FRAME_SCALARS.includes(group) || group === "peaks") continue
      const items = Array.isArray(frame[group]) ? frame[group] : [frame[group]]
      for (const [index, item] of items.entries()) {
        if (!item || !Object.keys(item).length) continue
        const box = liveGroup(items.length > 1 ? `${group} ${index}` : group, item, capabilities)
        ;(measured.has(group) ? own : theirs).push(box)
      }
    }
    fillGroups(live, own)
    fillGroups(fromExtensions, theirs)

    const measurements = frame.peaks && Object.keys(frame.peaks).length
    if (measurements) peaks.replaceChildren(peaks.querySelector("h3"), readingList(frame.peaks, capabilities))
    peaks.hidden = !measurements
  }

  function renderSources(capabilities) {
    sources.querySelector("ul").replaceChildren(...capabilities.sources.map((source) => {
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

  return { render, renderSources }
}
