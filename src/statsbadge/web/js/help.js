import { api } from "./api.js"
import { el, toast } from "./dom.js"

export function createHelp({ holder }) {
  async function renderHelp() {
    const intro = holder.querySelector("p")
    let facts
    try {
      facts = await api("/api/help")
    } catch (error) {
      holder.replaceChildren(intro, el("p", { className: "bad", textContent: error.message }))
      return
    }
    const reading = el("section", null,
                       el("h2", { textContent: "Reading now" }),
                       el("p", { textContent: (facts.sources || []).join(", ") || "nothing" }))
    holder.replaceChildren(intro, ...helpFor(facts), reading)
  }

  function helpFor(facts) {
    if (facts.platform === "Darwin") return [macHelp(facts.powermetrics || {})]
    if (facts.platform === "Windows") return [windowsHelp(facts.lhm || {})]
    return [el("section", null,
               el("h2", { textContent: "Linux" }),
               el("p", { textContent: "Temperatures, fans and power come from the kernel through psutil, and need nothing set up. The tray needs GTK bindings from your distribution, and GNOME hosts none without the AppIndicator extension." }))]
  }

  function macHelp(state) {
    const box = el("section", null,
                   el("h2", { textContent: "macOS" }),
                   el("p", { textContent: "GPU load, VRAM, thermal pressure and, on Apple Silicon, temperatures need no setup." }))
    if (!state.there) {
      box.append(el("p", { textContent: "powermetrics is missing, so package power, CPU and GPU clocks and GPU power are unavailable." }))
      return box
    }
    if (state.permitted) {
      box.append(el("p", { className: "good",
                           textContent: "Package power, CPU and GPU clocks and GPU power are on, via powermetrics." }))
      return box
    }
    box.append(
      el("p", { textContent: "Package power, CPU and GPU clocks and GPU power need powermetrics, which runs as root. To let sudo run it, and only it, without a password, run:" }),
      el("pre", { textContent: `sudo visudo -f ${state.file}` }),
      el("p", { textContent: "and add:" }),
      el("pre", { textContent: state.sudoers }),
      el("p", { textContent: "Restart statsbadge to apply. statsbadge itself does not run as root." }))
    return box
  }

  function windowsHelp(state) {
    const box = el("section", null,
                   el("h2", { textContent: "Windows" }),
                   el("p", { textContent: "Temperatures, fan speeds and package power need a kernel driver on Windows. statsbadge reads them from LibreHardwareMonitor's web server." }))
    box.append(state.reading
      ? el("p", { className: "good", textContent: "Connected to LibreHardwareMonitor." })
      : el("p", { textContent: "Not connected. Run LibreHardwareMonitor and turn on Options > Remote Web Server > Run. Download it from the project's GitHub releases; the similarly named .com site is unofficial." }))

    const field = el("input", { type: "text", id: "lhmurl", value: state.url || "",
                                placeholder: state.default })
    const save = el("button", { type: "button", className: "primary", textContent: "Save" })
    save.onclick = () => api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ lhm_url: field.value.trim() || state.default }),
    }).then(() => {
      toast("Saved")
      return renderHelp()
    }).catch((error) => toast(error.message, true))

    box.append(el("label", { htmlFor: "lhmurl", textContent: "Web server address" }), field,
               el("menu", null, save),
               el("p", { textContent: "Only needed if it is not on the default port." }))
    return box
  }

  return { render: renderHelp }
}
