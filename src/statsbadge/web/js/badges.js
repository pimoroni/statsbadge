import { api } from "./api.js"
import { el, toast } from "./dom.js"
import { nextId } from "./forms.js"

export function badgeName(badges, id) {
  return (badges[id] && badges[id].name) || id
}

function appLabel(state) {
  if (!state) return "Not installed from here"
  const changes = state.added.length + state.changed.length + state.removed.length
  if (!changes) return "Up to date"
  return `${changes} file${changes === 1 ? "" : "s"} behind`
}

export function createBadges({ badgeSelect, note, holder, stale, pairButton, pairingPanel, onSwitch,
                               onForgotten, onPaired, onUpdate }) {
  let badges = {}
  let whose = null
  let capabilities = null

  async function forgetBadge(id) {
    if (!window.confirm(`Forget ${badgeName(badges, id)}? Its layout goes with it.`)) return
    await api(`/api/badges/${id}`, { method: "DELETE" })
    await onForgotten()
    toast("Forgotten")
  }

  function renderWhose() {
    const ids = Object.keys(badges)
    badgeSelect.replaceChildren(
      ...ids.map((id) => el("option", { value: id, textContent: badgeName(badges, id) })),
      el("option", { value: "",
                     textContent: ids.length
                       ? "Default, for any other badge"
                       : "No badge paired yet" }))
    badgeSelect.value = whose || ""
    badgeSelect.onchange = () => onSwitch(badgeSelect.value).catch((error) => toast(error.message, true))

    const own = whose && badges[whose] && badges[whose].configured
    note.textContent = whose && !own
      ? "on the default layout, until you save"
      : (!whose && ids.length ? "defaults for a newly paired badge" : "")
  }

  function renderBadges() {
    const ids = Object.keys(badges)
    if (ids.length) {
      holder.replaceChildren(holder.querySelector("h2"), ...ids.map(badgeBox))
    } else {
      holder.replaceChildren(holder.querySelector("h2"), el("section", null, el("p", {
        textContent: "None paired. Use the USB installer, or pair over the network." })))
    }
    renderStale()
  }

  function badgeBox(id) {
    const named = badges[id].name && badges[id].name !== id ? badges[id].name : ""
    const nameId = nextId("badge")
    const name = el("input", { type: "text", id: nameId, value: named,
                               placeholder: "Give it a name" })
    const heading = el("h3", { textContent: named || "Unnamed badge" })

    let pending = null
    const store = (announce) => {
      window.clearTimeout(pending)
      pending = null
      rename(id, name.value)
        .then((shown) => {
          heading.textContent = shown
          if (announce) toast("Renamed")
        })
        .catch((error) => toast(error.message, true))
    }
    name.oninput = () => {
      window.clearTimeout(pending)
      pending = window.setTimeout(() => store(false), 400)
    }
    name.onchange = () => store(true)

    const forget = el("button", { type: "button", className: "small danger",
                                  textContent: "Forget" })
    forget.onclick = () => forgetBadge(id).catch((error) => toast(error.message, true))

    const box = el("section", { "aria-current": id === whose ? "true" : null },
                   heading,
                   el("label", { htmlFor: nameId, textContent: "Name" }),
                   name,
                   facts(id))

    const footer = el("footer", null, forget)
    if (id !== whose) {
      const configure = el("button", { type: "button", className: "small add",
                                       textContent: "Configure" })
      configure.onclick = () => onSwitch(id).catch((error) => toast(error.message, true))
      footer.append(configure)
    }
    box.append(footer)
    return box
  }

  function facts(id) {
    const record = badges[id]
    const rows = [
      ["UID", el("code", { textContent: id })],
      ["Layout", record.configured ? "Its own" : "The default"],
      ["Pages", `${record.pages}`],
      ["Theme", themeLabel(record.theme)],
      ["Refresh", `${record.interval_ms} ms`],
      ["App", appLabel(record.app)],
    ]
    return el("dl", null, ...rows.flatMap(([term, said]) =>
      [el("dt", { textContent: term }), el("dd", null, said)]))
  }

  function themeLabel(name) {
    const record = (capabilities.themes || []).find((entry) => entry.name === name)
    return (record && record.label) || name || "unset"
  }

  async function rename(id, wanted) {
    const result = await api(`/api/badges/${id}`, {
      method: "PUT",
      body: JSON.stringify({ name: wanted }),
    })
    badges[id].name = result.name
    renderWhose()
    return result.name === id ? "Unnamed badge" : result.name
  }

  let pairingPoll = null

  async function startPairing() {
    await api("/api/pair", { method: "POST" })
    await watchPairing(true)
  }

  async function stopPairing() {
    await api("/api/pair", { method: "DELETE" })
    await watchPairing()
    toast("Pairing closed")
  }

  async function answer(requestId, approve) {
    const result = await api(`/api/enrol/${requestId}/${approve ? "approve" : "deny"}`,
                             { method: "POST" })
    toast(approve ? "Badge paired" : "Denied")
    await onPaired(approve ? result.approved : null)
    watchPairing()
  }

  function pendingList(pending) {
    return el("ul", null, pending.map((request) => {
      const buttons = [["Approve", true, "primary small"], ["Deny", false, "small danger"]]
        .map(([label, approve, className]) => {
          const button = el("button", { type: "button", className, textContent: label })
          button.onclick = () => answer(request.request_id, approve)
            .catch((error) => toast(error.message, true))
          return button
        })
      return el("li", null,
                el("span", { textContent: request.name }),
                el("samp", { textContent: request.code }),
                el("code", { textContent: request.badge_id }),
                el("div", null, buttons))
    }))
  }

  async function watchPairing(announce) {
    if (pairingPoll) {
      clearInterval(pairingPoll)
      pairingPoll = null
    }

    const paint = (state, pending) => {
      if (!state.active) {
        pairingPanel.close()
        pairButton.textContent = "Pair a badge…"
        pairButton.onclick = () => startPairing().catch((error) => toast(error.message, true))
        return false
      }
      pairButton.textContent = "Stop pairing"
      pairButton.onclick = () => stopPairing().catch((error) => toast(error.message, true))
      pairingPanel.replaceChildren(...[
        el("p", { textContent: `On the badge: launch Stats, press B to set up, and pick ${(state.hosts || []).join(" / ")}:${state.port}` }),
        el("p", { textContent: `closes in ${state.expires_in}s` }),
        pending.length ? el("p", { textContent: "Approve the one whose code matches." }) : null,
        pending.length ? pendingList(pending) : null,
      ].filter(Boolean))
      if (!pairingPanel.open) pairingPanel.show()
      return true
    }

    let state = await api("/api/pair")
    let pending = (await api("/api/enrol")).pending
    if (!paint(state, pending)) return
    if (announce) toast(`Pairing open for ${state.expires_in}s`)

    pairingPoll = setInterval(async () => {
      try {
        state = await api("/api/pair")
        pending = (await api("/api/enrol")).pending
        if (!paint(state, pending)) {
          clearInterval(pairingPoll)
          pairingPoll = null
        }
      } catch (error) {
        clearInterval(pairingPoll)
        pairingPoll = null
      }
    }, 1000)
  }

  function renderStale() {
    const names = Object.keys(badges)
      .filter((id) => badges[id].app && badges[id].app.behind)
      .map((id) => badgeName(badges, id))
    stale.hidden = !names.length
    if (!names.length) return
    const one = names.length === 1
    const button = el("button", { type: "button", className: "small",
                                  textContent: "Update…" })
    button.onclick = onUpdate
    stale.replaceChildren(
      `${names.join(", ")} ${one ? "was" : "were"} last seen running an older app. Connect ${one ? "it" : "them"} by USB to update.`,
      button)
  }

  function renderBadgeSelect(currentBadges, currentWhose) {
    badges = currentBadges
    whose = currentWhose
    renderWhose()
  }

  function render(currentBadges, currentWhose, currentCapabilities) {
    badges = currentBadges
    whose = currentWhose
    capabilities = currentCapabilities
    renderBadges()
  }

  return { renderBadgeSelect, render, watchPairing }
}
