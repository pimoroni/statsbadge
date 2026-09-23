import { api } from "./api.js"
import { el, titleCase, toast } from "./dom.js"
import { nextId, settingRow } from "./forms.js"
import { fieldLabel, numericRefs, poolFor, refSelect } from "./refs.js"

const MAX_SETTING = { key: "max", label: "Full scale", type: "number", min: 0, step: "any",
                      placeholder: "automatic" }

function singular(label) {
  if (label === "Series") return label
  if (label === "Axes") return "Axis"
  return label.endsWith("s") ? label.slice(0, -1) : label
}

function freshId(base, taken) {
  const stamp = Date.now().toString(36).slice(-4)
  let id = `${base}${stamp}`
  for (let n = 2; taken.has(id); n += 1) id = `${base}${stamp}${n}`
  taken.add(id)
  return id
}

export function createPages({ list, status, kindPicker, recipePicker, quickAddButton, changed }) {
  let config = null
  let caps = null
  const expanded = new Set()
  let prunedWanted = 0

  function renderPages() {
    list.replaceChildren(...config.pages.map(pageCard))
    refreshPruned()
  }

  function kindLabel(kind) {
    if (caps.kinds[kind]) return caps.kinds[kind].title
    const option = kindPicker.querySelector(`option[value="${CSS.escape(kind)}"]`)
    return (option && option.textContent) || titleCase(kind)
  }

  function shapeFor(kind) {
    if (caps.kinds[kind]) return caps.kinds[kind]
    const declared = (caps.extension_pages || []).find((page) => page.kind === kind)
    const slots = (declared && declared.slots) || {}
    return { one: slots.one || null, many: slots.many || null,
             max: slots.max || 0, slots: slots.label || "Values" }
  }

  function renderKindPicker() {
    const groups = new Map()
    for (const [kind, shape] of Object.entries(caps.kinds)) {
      if (!groups.has(shape.group)) groups.set(shape.group, [])
      groups.get(shape.group).push(el("option", { value: kind, textContent: shape.title,
                                                  title: shape.summary }))
    }
    kindPicker.replaceChildren(...[...groups].map(
      ([label, options]) => el("optgroup", { label }, options)))
  }

  function pageCard(page, index) {
    const shape = shapeFor(page.kind)
    const open = expanded.has(page.id)
    const settings = (caps.extension_page_settings || {})[page.kind] || []

    const titled = el("span", { className: "given" })
    const showTitle = () => {
      const given = (page.title || "").trim()
      titled.textContent = given.toLowerCase() === page.kind.toLowerCase()
        ? ""
        : titleCase(given)
    }
    showTitle()

    const titleId = nextId("page")
    const title = el("input", { type: "text", id: titleId, value: page.title || "" })
    title.oninput = () => { page.title = title.value; showTitle(); changed() }

    const toggle = el("button", { type: "button", textContent: open ? "▾" : "▸",
                                  title: open ? "Collapse" : "Configure",
                                  "aria-expanded": String(open) })
    toggle.onclick = () => {
      if (open) expanded.delete(page.id); else expanded.add(page.id)
      renderPages()
    }

    const remove = el("button", { type: "button", className: "danger small",
                                  textContent: "✕", title: "Remove this page" })
    remove.onclick = () => {
      if (config.pages.length <= 1) return toast("Keep at least one page", true)
      if (!window.confirm(`Remove ${page.title || page.kind}?`)) return undefined
      config.pages.splice(index, 1)
      changed()
      return renderPages()
    }

    const kind = el("h3", { title: "Drag to reorder" },
                    el("span", { className: "kind", textContent: kindLabel(page.kind) }),
                    titled)
    const item = el("li", null, el("header", null, kind, toggle, remove))

    if (open) {
      item.append(el("label", { htmlFor: titleId, textContent: "Title" }), title,
                  slotList(page, shape),
                  ...(shape.scaled ? settingRow(page, MAX_SETTING, changed) : []),
                  ...settings.flatMap((setting) => settingRow(page, setting, changed)),
                  el("footer", null, moveButtons(index), addSlot(page, shape)))
    } else {
      const refs = shape.one ? [page[shape.one]] : (page[shape.many] || [])
      const named = refs.filter(Boolean).map((ref) => fieldLabel(caps, ref))
      const extra = settings.map((setting) => page[setting.key]).filter(Boolean)
      if (shape.scaled && page.max) extra.push(`full scale ${page.max}`)
      item.append(el("p", { textContent: named.concat(extra).join(", ") || "nothing chosen" }))
    }

    reorderable(item, config.pages, index, { tag: "page", along: "x", handle: kind })
    return item
  }

  function reorderable(node, items, index, { tag, along, handle }) {
    node.draggable = !handle
    if (handle) {
      handle.onpointerdown = () => { node.draggable = true }
      handle.onpointerup = () => { node.draggable = false }
    }
    node.ondragstart = (event) => {
      event.stopPropagation()
      node.dataset.dragging = ""
      event.dataTransfer.setData("text/plain", `${tag}:${index}`)
    }
    node.ondragend = () => {
      node.draggable = !handle
      delete node.dataset.dragging
      delete node.dataset.over
    }
    node.ondragover = (event) => {
      event.preventDefault()
      event.stopPropagation()
      const box = node.getBoundingClientRect()
      node.dataset.over = (along === "x"
        ? event.clientX < box.left + box.width / 2
        : event.clientY < box.top + box.height / 2) ? "before" : "after"
    }
    node.ondragleave = (event) => {
      if (!node.contains(event.relatedTarget)) delete node.dataset.over
    }
    node.ondrop = (event) => {
      event.preventDefault()
      event.stopPropagation()
      const after = node.dataset.over === "after"
      delete node.dataset.over
      const [from, at] = event.dataTransfer.getData("text/plain").split(":")
      const moved = parseInt(at, 10)
      if (from !== tag || Number.isNaN(moved)) return
      let target = after ? index + 1 : index
      if (moved < target) target -= 1
      if (target === moved) return
      items.splice(target, 0, items.splice(moved, 1)[0])
      changed()
      renderPages()
    }
  }

  function slotList(page, shape) {
    const rows = []

    if (shape.one) {
      rows.push(el("li", null,
                   el("span", { textContent: page.kind === "bars" ? "List" : "Gauge" }),
                   refSelect(caps, page[shape.one], poolFor(caps, shape.pool),
                             (value) => { page[shape.one] = value; changed() })))
    }

    const current = shape.many ? page[shape.many] || [] : []
    current.forEach((ref, slot) => {
      const drop = el("button", { type: "button", className: "small", textContent: "−",
                                  title: "Remove this slot" })
      drop.onclick = () => { current.splice(slot, 1); changed(); renderPages() }
      const row = el("li", null,
                     el("span", { className: "grip", textContent: "⋮" }),
                     refSelect(caps, ref, poolFor(caps, shape.many_pool),
                               (value) => { current[slot] = value; changed() }),
                     drop)
      reorderable(row, current, slot, { tag: "slot", along: "y" })
      rows.push(row)
    })

    return el("ol", null, rows)
  }

  function addSlot(page, shape) {
    const current = shape.many ? page[shape.many] || [] : []
    if (!shape.many || current.length >= shape.max) return null
    const add = el("button", { type: "button", className: "small add",
                               textContent: `Add ${singular(shape.slots).toLowerCase()}` })
    add.onclick = () => {
      page[shape.many] = current.concat([poolFor(caps, shape.many_pool)[0]])
      changed()
      renderPages()
    }
    return add
  }

  function moveButtons(index) {
    return [["←", "Move left", index - 1, index === 0],
            ["→", "Move right", index + 1, index === config.pages.length - 1]]
      .map(([glyph, label, to, ends]) => {
        const button = el("button", { type: "button", className: "small", textContent: glyph,
                                      title: label, "aria-label": label, disabled: ends })
        button.onclick = () => {
          config.pages.splice(to, 0, config.pages.splice(index, 1)[0])
          changed()
          renderPages()
        }
        return button
      })
  }

  function pageIds() {
    return new Set(config.pages.map((page) => page.id))
  }

  function newPage(kind) {
    const taken = pageIds()
    const offered = (caps.extension_pages || []).find((page) => page.kind === kind)
    if (offered) {
      return { ...offered, id: freshId(offered.id || kind, taken) }
    }
    const shape = shapeFor(kind)
    const pool = numericRefs(caps)
    const page = { id: freshId(kind, taken), kind, title: kind }
    if (shape.one) {
      page[shape.one] = kind === "bars" ? "cpu.cores" : (pool[0] || "cpu.pct")
    }
    if (shape.many) {
      page[shape.many] = pool.slice(0, Math.min(2, shape.max))
    }
    return page
  }

  function offerExtensionPages() {
    const picker = kindPicker
    const offered = (caps.extension_pages || []).filter(
      (page) => ![...picker.options].some((option) => option.value === page.kind))
    if (!offered.length) return
    const group = picker.querySelector("optgroup[label=\"Extensions\"]")
      || picker.appendChild(el("optgroup", { label: "Extensions" }))
    group.append(...offered.map(
      (page) => el("option", { value: page.kind, textContent: page.title || page.kind })))
  }

  function offerRecipes() {
    const picker = recipePicker
    const listed = caps.recipes || []
    const wanted = picker.value
    picker.replaceChildren(...listed.map((recipe) => el("option", {
      value: recipe.name, textContent: recipe.title, title: recipe.summary || null })))
    if (listed.some((recipe) => recipe.name === wanted)) picker.value = wanted
    picker.hidden = !listed.length
    quickAddButton.hidden = !listed.length
  }

  function quickAdd(name) {
    const recipe = (caps.recipes || []).find((entry) => entry.name === name)
    if (!recipe) return
    const taken = pageIds()
    const added = recipe.pages.map((page) => (
      { ...page, id: freshId(page.id || page.kind, taken) }))
    config.pages.unshift(...added)
    if (added.length === 1) expanded.add(added[0].id)
    else toast(`Added ${recipe.title}: ${added.length} pages.`)
    changed()
    renderPages()
  }

  async function refreshPruned() {
    const mine = ++prunedWanted
    try {
      const shown = await api("/api/preview", { method: "POST", body: JSON.stringify(config) })
      if (mine !== prunedWanted) return
      const kept = new Set(shown.pages.map((page) => page.id))
      const dropped = config.pages.filter((page) => !kept.has(page.id)).map((page) => page.title)
      const node = status
      node.textContent = `Not shown on the badge, because this host reports no data for them: ${dropped.join(", ")}`
      node.hidden = !dropped.length
    } catch {}
  }

  function add(kind) {
    config.pages.unshift(newPage(kind))
    expanded.add(config.pages[0].id)
    changed()
    renderPages()
  }

  function render(currentConfig, currentCaps) {
    config = currentConfig
    caps = currentCaps
    renderPages()
  }

  function offer(currentCaps) {
    caps = currentCaps
    offerExtensionPages()
    offerRecipes()
  }

  function renderKinds(currentCaps) {
    caps = currentCaps
    renderKindPicker()
  }

  return { render, offer, renderKinds, add, quickAdd }
}
