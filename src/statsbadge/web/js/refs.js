import { el, titleCase } from "./dom.js"

export function availableRefs(caps) {
  const refs = []
  const available = (caps && caps.available) || {}
  for (const group of Object.keys(available).sort()) {
    for (const field of available[group]) refs.push(`${group}.${field}`)
  }
  return refs
}

function preferredRefs(caps) {
  const printable = availableRefs(caps).filter(
    (ref) => !listFields(caps).includes(ref.split(".")[1])
             && !itemFields(caps).includes(ref.split(".")[1]))
  return [...new Set(numericRefs(caps).concat(printable))]
}

export function numericRefs(caps) {
  return availableRefs(caps).filter((ref) => {
    const field = ref.split(".")[1]
    return !["name", "host", "os", "arch", "cpu_name", "iface", "charging"]
      .includes(field) && !listFields(caps).includes(field)
      && !itemFields(caps).includes(field)
  })
}

function listFields(caps) {
  return caps.list_fields || ["cores", "load"]
}

function itemFields(caps) {
  return caps.item_fields || []
}

function itemRefs(caps) {
  return availableRefs(caps).filter((ref) => itemFields(caps).includes(ref.split(".")[1]))
}

function notifyRefs(caps) {
  return [...new Set(itemRefs(caps).concat(numericRefs(caps)))]
}

function gaugeRefs(caps) {
  const percent = caps.percent_fields || []
  const scaled = Object.keys(caps.full_scale || {})
  return numericRefs(caps).filter((ref) => {
    const field = ref.split(".")[1]
    return percent.includes(field) || scaled.includes(field)
  })
}

function seriesRefs(caps) {
  const kept = caps.graphed || []
  const withHistory = numericRefs(caps).filter((ref) => kept.includes(ref))
  return withHistory.length ? withHistory : numericRefs(caps)
}

function listRefs(caps) {
  return availableRefs(caps).filter((ref) => listFields(caps).includes(ref.split(".")[1]))
}

const POOLS = {
  gauge: gaugeRefs,
  series: seriesRefs,
  list: listRefs,
  notify: notifyRefs,
  any: preferredRefs,
}

function groupLabel(caps, group) {
  return (caps.group_labels || {})[group] || group
}

const HOST_SOURCE = "This host"

function sourceLabel(caps, group) {
  return (caps.group_source || {})[group] || HOST_SOURCE
}

export function fieldLabel(caps, ref) {
  const [group, field] = ref.split(".")
  const labels = (caps.field_labels || {})[group] || {}
  return labels[field] || titleCase(field)
}

export function refSelect(caps, value, refs, onChange) {
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
    const owner = sourceLabel(caps, group)
    if (!byOwner.has(owner)) byOwner.set(owner, [])
    byOwner.get(owner).push(group)
  }
  const owners = [...byOwner.keys()].sort(
    (a, b) => (a === HOST_SOURCE ? -1 : 0) - (b === HOST_SOURCE ? -1 : 0))

  const chosen = String(value || options[0] || "").split(".")[0]
  const source = el("select", { "aria-label": "Source" }, owners.map(
    (owner) => el("optgroup", { label: owner }, byOwner.get(owner).map(
      (group) => el("option", { value: group, textContent: groupLabel(caps, group),
                                selected: group === chosen })))))

  const select = el("select", { "aria-label": "Reading" })
  const fill = (group, ref) => {
    select.replaceChildren(...(byGroup.get(group) || []).map(
      (each) => el("option", { value: each, textContent: fieldLabel(caps, each),
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

export function poolFor(caps, name) {
  const refs = (POOLS[name] || POOLS.any)(caps)
  return refs.length ? refs : availableRefs(caps)
}
