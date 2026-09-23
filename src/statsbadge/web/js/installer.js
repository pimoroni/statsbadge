import { api } from "./api.js"
import { el, toast } from "./dom.js"

function installSummary(result) {
  const copied = (result.copied || []).length
  const parts = [copied
    ? `${copied} file${copied === 1 ? "" : "s"} copied`
    : "already up to date"]
  if (result.wifi === "set") parts.push("WiFi set")
  if (result.credentials) parts.push("paired")
  return parts.join(", ")
}

export function createInstaller({ panel, onFinished }) {
  let installPoll = null
  let installer = null
  let installRan = false

  function openInstaller() {
    if (panel.open) {
      closeInstaller()
      return
    }
    panel.replaceChildren(...installerBox())
    panel.show()
    watchInstall()
  }

  function closeInstaller() {
    panel.close()
    installer = null
    if (installPoll) {
      clearInterval(installPoll)
      installPoll = null
    }
  }

  function installerBox() {
    const status = el("p", { className: "found", textContent: "Looking for a badge…" })
    const ssid = el("input", { type: "text", id: "ssid", placeholder: "Network name" })
    const password = el("input", { type: "password", id: "wifipass" })
    const region = el("select", { id: "region" },
                      el("option", { value: "", textContent: "Leave as it is" }))
    const zone = el("input", { type: "number", id: "zone", min: -12, max: 14, step: 1,
                               placeholder: "0" })
    const wifi = el("input", { type: "checkbox", id: "setwifi" })
    const fields = el("div", { className: "fields", hidden: true },
                      el("label", { htmlFor: "ssid", textContent: "Network" }), ssid,
                      el("label", { htmlFor: "wifipass", textContent: "Password" }), password,
                      el("label", { htmlFor: "region", textContent: "Region" }), region,
                      el("label", { htmlFor: "zone", textContent: "GMT offset" }), zone)
    wifi.onchange = () => { fields.hidden = !wifi.checked }

    const go = el("button", { type: "button", className: "primary", textContent: "Update" })
    go.onclick = () => startInstall().catch((error) => {
      toast(error.message, true)
      go.disabled = false
    })
    const cancel = el("button", { type: "button", textContent: "Close" })
    cancel.onclick = closeInstaller
    const log = el("pre", { hidden: true })

    installer = { status, wifi, ssid, password, region, zone, go, log }
    return [
      status,
      el("label", { htmlFor: "setwifi", className: "check" }, wifi, "Set the WiFi network"),
      fields,
      el("p", { className: "note",
                textContent: "The badge resets into USB storage while this runs." }),
      el("menu", null, go, cancel),
      log,
    ]
  }

  async function startInstall() {
    const asking = {}
    if (installer.wifi.checked) {
      const network = installer.ssid.value.trim()
      if (!network) {
        toast("Name the network first", true)
        return
      }
      asking.ssid = network
      asking.password = installer.password.value
      asking.force_secrets = true
      if (installer.region.value) asking.region = installer.region.value
      if (installer.zone.value !== "") asking.timezone = Number(installer.zone.value)
    }
    installer.go.disabled = true
    await api("/api/install", { method: "POST",
                                body: JSON.stringify(asking) })
    watchInstall()
  }

  function watchInstall() {
    if (installPoll) clearInterval(installPoll)
    const tick = () => api("/api/install").then(paintInstall).catch(closeInstaller)
    tick()
    installPoll = setInterval(tick, 1000)
  }

  function paintInstall(state) {
    if (!installer) return
    if (installer.region.options.length < 2) {
      installer.region.append(...(state.regions || []).map(
        (name) => el("option", { value: name, textContent: name })))
    }
    const port = (state.ports || [])[0]
    installer.status.textContent = state.running
      ? "Working…"
      : (port ? `Badge on ${port}` : "No badge connected. Plug one in by USB.")
    installer.go.disabled = state.running || !port
    const said = (state.log || []).join("\n")
    installer.log.hidden = !said
    installer.log.textContent = said
    installer.log.scrollTop = installer.log.scrollHeight
    if (installRan && !state.running) finishedInstall(state.result)
    installRan = state.running
  }

  async function finishedInstall(result) {
    if (!result) return
    if (result.error) toast(result.error, true)
    else if (result.cancelled) toast("Nothing was changed")
    else toast(installSummary(result))
    await onFinished()
  }

  return { open: openInstaller }
}
