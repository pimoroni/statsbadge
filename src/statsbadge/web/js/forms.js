import { el } from "./dom.js"

let controlSerial = 0

export function nextId(prefix) {
  controlSerial += 1
  return `${prefix}${controlSerial}`
}

export function settingRow(stored, setting, changed, options) {
  const id = nextId("setting")
  const label = el("label", { htmlFor: id, textContent: setting.label || setting.key })
  const current = stored[setting.key] !== undefined ? stored[setting.key] : setting.default

  let input
  if (setting.type === "bool") {
    input = el("input", { type: "checkbox", id, checked: !!current })
    input.onchange = () => { stored[setting.key] = input.checked; changed() }
  } else if (setting.type === "number") {
    input = el("input", { type: "number", id, min: setting.min, max: setting.max,
                          step: setting.step, placeholder: setting.placeholder,
                          value: current === null || current === undefined ? "" : current })
    input.oninput = () => {
      stored[setting.key] = input.value === "" ? null : Number(input.value)
      changed()
    }
  } else if (setting.type === "choice") {
    input = el("select", { id }, (setting.options || []).map(
      (option) => el("option", { value: option, textContent: option,
                                 selected: option === current })))
    input.onchange = () => { stored[setting.key] = input.value; changed() }
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
      changed()
    }
  }
  return setting.unit
    ? [label, input, el("small", { textContent: setting.unit })]
    : [label, input]
}
