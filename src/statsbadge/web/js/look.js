import { el, titleCase } from "./dom.js"
import { numericRefs, refSelect } from "./refs.js"

export function createLook({ controls, outputs, caseLights, caseLightRef, buttons, changed,
                             onGaugeFill }) {
  let config = null
  let capabilities = null

  function bindRange(id, key, format, scale) {
    const input = controls[id]
    const out = outputs[id]
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
      changed()
    }
  }

  function bindSelect(id, read, write) {
    const select = controls[id]
    select.value = read()
    select.onchange = () => { write(select.value); changed() }
  }

  function bindCheck(id, key) {
    const input = controls[id]
    input.checked = !!config[key]
    input.onchange = () => { config[key] = input.checked; changed() }
  }

  function renderCaseLights() {
    const stored = config.caselights
    const chosen = stored === true ? "theme" : stored ? "reading" : "off"
    const refs = numericRefs(capabilities)
    const offered = [["off", "Off"], ["theme", "Follow the Backlight"]]
    if (refs.length || chosen === "reading") offered.push(["reading", "Follow a Reading"])
    caseLights.replaceChildren(...offered.map(([value, text]) =>
      el("option", { value, textContent: text, selected: value === chosen })))

    let following = typeof stored === "string" ? stored : refs[0]

    caseLightRef.hidden = chosen !== "reading"
    caseLightRef.replaceChildren(...refSelect(capabilities, following, refs, (ref) => {
      following = ref
      config.caselights = ref
      changed()
    }))

    caseLights.onchange = () => {
      const value = caseLights.value
      config.caselights = value === "off" ? false : value === "theme" ? true : following
      caseLightRef.hidden = value !== "reading"
      changed()
    }
  }

  function renderButtons() {
    const groups = new Map()
    const offer = (heading, option) => {
      if (!groups.has(heading)) groups.set(heading, [])
      groups.get(heading).push(option)
    }
    for (const local of capabilities.local_actions || []) {
      offer("Badge", el("option", { value: local.action, textContent: titleCase(local.label) }))
    }
    for (const command of capabilities.commands || []) {
      const option = el("option", { value: command.name, textContent: titleCase(command.label) })
      offer(command.group, option)
    }

    const offered = [
      el("option", { value: "", textContent: "Nothing" }),
      ...[...groups].map(([label, options]) => el("optgroup", { label }, options)),
    ]
    for (const which of ["a", "b", "c"]) {
      const select = buttons[which]
      select.replaceChildren(...offered.map((option) => option.cloneNode(true)))
      select.value = (config.buttons && config.buttons[which]) || ""
      select.onchange = () => {
        config.buttons = config.buttons || {}
        config.buttons[which] = select.value || null
        changed()
      }
    }
  }

  function render(currentConfig, currentCapabilities) {
    config = currentConfig
    capabilities = currentCapabilities
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
      onGaugeFill()
    })

    bindCheck("animate", "animate")
    bindCheck("plotanim", "plot_animation")
    bindCheck("autobright", "auto_brightness")

    renderCaseLights()
    renderButtons()
  }

  function refresh(currentConfig, currentCapabilities) {
    config = currentConfig
    capabilities = currentCapabilities
    renderButtons()
    renderCaseLights()
  }

  return { render, refresh }
}
