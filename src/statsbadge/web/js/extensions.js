import { api } from "./api.js"
import { el, toast } from "./dom.js"
import { settingRow } from "./forms.js"

const SECRET_SHOWN = 6
const SECRET_MAX = 18

function wants(needs) {
  return needs ? el("span", { className: "wants", textContent: `needs ${needs}` }) : null
}

function givenName(name, version) {
  const given = `statsbadge-${name}`
  return version ? `${given} @ ${version}` : given
}

function masked(value) {
  const text = value === null || value === undefined ? "" : String(value)
  if (!text) return ""
  const shown = text.slice(0, SECRET_SHOWN)
  return shown + "x".repeat(Math.max(4, Math.min(SECRET_MAX, text.length - shown.length)))
}

export function createExtensions({ holder, changed, onChanged }) {
  let config = null
  let caps = null
  const openExtensions = new Set()
  const editingSecrets = new Set()
  const installing = new Set()
  let catalogue = null
  let behind = {}
  let behindWhy = null
  let checking = false

  function renderSettings() {
    const schema = caps.extension_settings || {}
    const installed = caps.extensions || []
    config.settings = config.settings || {}
    const intro = holder.querySelector("p")
    holder.replaceChildren(
      ...(intro ? [intro] : []),
      el("div", { className: "configured" },
         ...installed.map((extension) => extensionBox(extension, schema[extension.name] || []))),
      catalogueBox())
  }

  function catalogued(name) {
    return ((catalogue && catalogue.offered) || []).find((entry) => entry.name === name)
  }

  function displayName(name) {
    const listed = catalogued(name)
    return (listed && listed.title) || name
  }

  async function refreshCatalogue() {
    try {
      catalogue = await api("/api/extensions")
    } catch (error) {
      catalogue = null
    }
    renderSettings()
  }

  async function refreshOutdated() {
    checking = true
    renderSettings()
    let found
    try {
      found = await api("/api/extensions/outdated")
    } catch (error) {
      behindWhy = error.message || "the server did not answer"
      checking = false
      renderSettings()
      return
    }
    behind = {}
    for (const entry of found.outdated || []) {
      behind[entry.name.replace(/^statsbadge-/, "")] = entry.latest
    }
    behindWhy = found.why || null
    checking = false
    renderSettings()
  }

  function catalogueBox() {
    const box = el("section", { className: "offer" },
                   el("h3", { textContent: "Extensions" }))
    if (!catalogue) {
      box.append(el("p", { textContent: "Could not read the list of extensions." }))
      return box
    }
    if (!catalogue.manageable) {
      box.append(el("p", { textContent:
        `Installing needs uv or pip, and this server can reach neither. It runs from ${catalogue.prefix}, and a tray started at login carries the PATH it was given then. Add them with uv pip install, or start it again from a terminal.` }))
    }
    box.append(el("ul", { className: "catalogue" }, ...catalogue.offered.map(offerRow)))
    box.append(updateCheck())
    box.append(freeformForm())
    return box
  }

  function updateCheck() {
    const said = checking ? "Checking for updates..."
      : behindWhy ? `Could not check for updates: ${behindWhy}`
      : Object.keys(behind).length ? "" : "Everything is up to date."
    return el("p", { className: behindWhy ? "bad" : null },
              el("small", { textContent: said }),
              el("button", { type: "button", textContent: "Check again",
                             disabled: checking, onclick: () => refreshOutdated() }))
  }

  function offerRow(entry) {
    const notes = [givenName(entry.name, entry.version)]
    if (entry.page) notes.push("includes badge page")
    if (entry.installed && !entry.managed) notes.push("installed by the environment")
    if (entry.disabled) notes.push("switched off")
    if (entry.installed && entry.managed && !entry.asked) {
      notes.push("installed, but not on the list")
    }
    if (entry.asked && !entry.installed) notes.push("asked for, but not installed")
    if (behind[entry.name]) notes.push(`${behind[entry.name]} is out`)

    const summary = [entry.summary, notes.join(" · ")].filter(Boolean)
    return el("li", null,
              el("div", null,
                 el("strong", { textContent: entry.title || entry.name }),
                 wants(entry.needs),
                 ...summary.map((text) => el("small", { textContent: text }))),
              el("div", null,
                 ...(behind[entry.name] && entry.managed ? [updateButton(entry)] : []),
                 offerButton(entry)))
  }

  function updateButton(entry) {
    const busy = installing.has(entry.name)
    return el("button", {
      type: "button",
      className: "primary",
      textContent: busy ? "Working..." : `Update to ${behind[entry.name]}`,
      disabled: busy || !catalogue.manageable,
      onclick: () => changeExtension("upgrade", entry.name),
    })
  }

  function offerButton(entry) {
    const busy = installing.has(entry.name)
    if (entry.installed && !entry.managed) {
      const verb = entry.disabled ? "enable" : "disable"
      return el("button", {
        type: "button",
        textContent: busy ? "Working..." : (entry.disabled ? "Enable" : "Disable"),
        title: "Installed by the environment, so it can only be switched off here",
        disabled: busy,
        onclick: () => changeExtension(verb, entry.name),
      })
    }
    const verb = entry.installed ? "remove" : "add"
    return el("button", {
      type: "button",
      textContent: busy ? "Working..." : (verb === "add" ? "Install" : "Remove"),
      disabled: busy || !catalogue.manageable,
      onclick: () => changeExtension(verb, entry.name),
    })
  }

  function freeformForm() {
    const field = el("input", { type: "text", name: "requirement",
                                placeholder: "another extension, or any pip requirement" })
    const form = el("form", { onsubmit: (event) => {
      event.preventDefault()
      const asked = field.value.trim()
      if (!asked) return
      field.value = ""
      changeExtension("add", asked)
    } },
                    field,
                    el("button", { type: "submit", textContent: "Install",
                                   disabled: !catalogue.manageable }))
    form.setAttribute("aria-label", "Install an extension by name")
    return form
  }

  async function changeExtension(verb, name) {
    installing.add(name)
    renderSettings()
    let done
    try {
      done = await api("/api/extensions", {
        method: "POST",
        body: JSON.stringify({ [verb]: [name] }),
      })
    } catch (error) {
      toast(String(error.message || error), true)
      installing.delete(name)
      renderSettings()
      return
    }
    installing.delete(name)
    if (!done.ok) {
      toast(done.why || "could not do that", true)
    } else {
      if (!(done.stuck || []).length && !done.nothing) {
        toast({ add: `Installed ${name}`, remove: `Removed ${name}`,
                upgrade: `Updated ${name}`, disable: `Switched ${name} off`,
                enable: `Switched ${name} on` }[verb])
      }
      for (const note of done.unpinned || []) toast(note)
      for (const name of done.restart || []) {
        toast(`Restart statsbadge to run the new ${name}`)
      }
      for (const entry of done.stuck || []) {
        toast(`Unable to uninstall ${entry.name}. It is installed in statsbadge's own environment, so whatever put it there has to take it out.`, true)
      }
      for (const entry of done.shadowed || []) {
        toast(`${entry.name} is already installed in statsbadge's own environment. That copy is the one that runs.`)
      }
      if (verb !== "remove" && (done.needs_usb || []).includes(name)) {
        toast("Run statsbadge install to push its page to the badge")
      }
    }
    await refreshCatalogue()
    await refreshOutdated()
    await onChanged()
  }

  function extensionBox(extension, settings) {
    const state = el("p")
    if (extension.error) {
      state.className = "bad"
      state.textContent = extension.error
    } else if (extension.available === false) {
      state.textContent = "Installed, but not usable on this host."
    } else {
      const parts = [givenName(extension.name, extension.version)]
      if (extension.provides.length) parts.push(extension.provides.join(", "))
      if (extension.badge_module) parts.push("includes badge page")
      state.textContent = parts.join(" · ")
    }

    const open = openExtensions.has(extension.name)
    const listed = catalogued(extension.name)
    const head = el("h3", null,
                    el("span", { textContent: displayName(extension.name) }),
                    wants(listed && listed.needs))
    const box = el("section", null, el("header", null, head), state)
    if (!settings.length) return box

    const toggle = el("button", { type: "button", textContent: open ? "▾" : "▸",
                                  title: open ? "Collapse" : "Configure",
                                  "aria-expanded": String(open) })
    toggle.onclick = () => {
      if (open) openExtensions.delete(extension.name)
      else openExtensions.add(extension.name)
      renderSettings()
    }
    box.firstChild.append(toggle)
    if (!open) return box

    config.settings[extension.name] = config.settings[extension.name] || {}
    const stored = config.settings[extension.name]
    for (const setting of settings) {
      if (setting.secret) continue
      box.append(...settingRow(stored, setting, changed))
      if (setting.hint) box.append(el("p", { textContent: setting.hint }))
    }
    const secrets = settings.filter((setting) => setting.secret)
    if (secrets.length) box.append(secretsBlock(extension.name, stored, secrets))
    return box
  }

  function secretsBlock(name, stored, secrets) {
    const open = editingSecrets.has(name)
    const block = el("div", { className: "secrets" })

    if (open) {
      for (const setting of secrets) {
        block.append(...settingRow(stored, setting, changed, { reveal: true }))
        if (setting.hint) block.append(el("p", { textContent: setting.hint }))
      }
    } else {
      block.append(el("dl", null, secrets.flatMap((setting) => [
        el("dt", { textContent: setting.label || setting.key }),
        el("dd", { textContent: masked(stored[setting.key]) }),
      ])))
    }

    const button = el("button", { type: "button", className: "small",
                                  textContent: open ? "Hide secrets" : "Edit secrets" })
    button.onclick = () => {
      if (open) editingSecrets.delete(name); else editingSecrets.add(name)
      renderSettings()
    }
    block.append(button)
    return block
  }

  function render(currentConfig, currentCaps) {
    config = currentConfig
    caps = currentCaps
    renderSettings()
  }

  return { render, refreshCatalogue, refreshOutdated }
}
