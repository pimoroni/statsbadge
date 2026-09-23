import { el, titleCase } from "./dom.js"

export function availableRefs(capabilities) {
  const refs = []
  const available = (capabilities && capabilities.available) || {}
  for (const group of Object.keys(available).sort()) {
    for (const field of available[group]) refs.push(`${group}.${field}`)
  }
  return refs
}

function preferredRefs(capabilities) {
  const printable = availableRefs(capabilities).filter(
    (ref) => !listFields(capabilities).includes(ref.split(".")[1])
             && !itemFields(capabilities).includes(ref.split(".")[1]))
  return [...new Set(numericRefs(capabilities).concat(printable))]
}

export function numericRefs(capabilities) {
  return availableRefs(capabilities).filter((ref) => {
    const field = ref.split(".")[1]
    return !["name", "host", "os", "arch", "cpu_name", "iface", "charging"]
      .includes(field) && !listFields(capabilities).includes(field)
      && !itemFields(capabilities).includes(field)
  })
}

function listFields(capabilities) {
  return capabilities.list_fields || ["cores", "load"]
}

function itemFields(capabilities) {
  return capabilities.item_fields || []
}

function itemRefs(capabilities) {
  return availableRefs(capabilities).filter((ref) => itemFields(capabilities).includes(ref.split(".")[1]))
}

function notifyRefs(capabilities) {
  return [...new Set(itemRefs(capabilities).concat(numericRefs(capabilities)))]
}

function gaugeRefs(capabilities) {
  const percent = capabilities.percent_fields || []
  const scaled = Object.keys(capabilities.full_scale || {})
  return numericRefs(capabilities).filter((ref) => {
    const field = ref.split(".")[1]
    return percent.includes(field) || scaled.includes(field)
  })
}

function seriesRefs(capabilities) {
  const kept = capabilities.graphed || []
  const withHistory = numericRefs(capabilities).filter((ref) => kept.includes(ref))
  return withHistory.length ? withHistory : numericRefs(capabilities)
}

function listRefs(capabilities) {
  return availableRefs(capabilities).filter((ref) => listFields(capabilities).includes(ref.split(".")[1]))
}

const POOLS = {
  gauge: gaugeRefs,
  series: seriesRefs,
  list: listRefs,
  notify: notifyRefs,
  any: preferredRefs,
}

function groupLabel(capabilities, group) {
  return (capabilities.group_labels || {})[group] || group
}

const HOST_SOURCE = "This host"

function sourceLabel(capabilities, group) {
  return (capabilities.group_source || {})[group] || HOST_SOURCE
}

export function fieldLabel(capabilities, ref) {
  const [group, field] = ref.split(".")
  const labels = (capabilities.field_labels || {})[group] || {}
  return labels[field] || titleCase(field)
}

export function refSelect(capabilities, value, refs, onChange) {
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
    const owner = sourceLabel(capabilities, group)
    if (!byOwner.has(owner)) byOwner.set(owner, [])
    byOwner.get(owner).push(group)
  }
  const owners = [...byOwner.keys()].sort(
    (a, b) => (a === HOST_SOURCE ? -1 : 0) - (b === HOST_SOURCE ? -1 : 0))

  const chosen = String(value || options[0] || "").split(".")[0]
  const source = el("select", { "aria-label": "Source" }, owners.map(
    (owner) => el("optgroup", { label: owner }, byOwner.get(owner).map(
      (group) => el("option", { value: group, textContent: groupLabel(capabilities, group),
                                selected: group === chosen })))))

  const select = el("select", { "aria-label": "Reading" })
  const fill = (group, ref) => {
    select.replaceChildren(...(byGroup.get(group) || []).map(
      (each) => el("option", { value: each, textContent: fieldLabel(capabilities, each),
                               selected: each === ref })))
  }
  fill(chosen, value)

  source.onchange = () => {
    fill(source.value, null)
    onChange(select.value)
  }
  select.onchange = () => onChange(select.value)
  return [source, select]
}

export function poolFor(capabilities, name) {
  const refs = (POOLS[name] || POOLS.any)(capabilities)
  return refs.length ? refs : availableRefs(capabilities)
}
