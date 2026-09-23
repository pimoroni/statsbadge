import { api } from "./api.js"
import { el, toast } from "./dom.js"

export function createGeneral({ holder }) {
  async function renderGeneral() {
    const heading = holder.querySelector("h2")
    let host
    try {
      host = await api("/api/settings")
    } catch (error) {
      holder.replaceChildren(heading,
                             el("p", { className: "bad", textContent: error.message }))
      return
    }

    const place = el("input", { type: "text", id: "hostplace", value: host.place || "",
                                placeholder: "Sheffield, GB" })
    const degrees = (id, value, limit) =>
      el("input", { type: "number", id, step: "0.001", min: -limit, max: limit,
                    value: value === null || value === undefined ? "" : value })
    const latitude = degrees("hostlat", host.latitude, 90)
    const longitude = degrees("hostlon", host.longitude, 180)

    const save = el("button", { type: "button", className: "primary", textContent: "Save" })
    save.onclick = () => api("/api/settings", {
      method: "POST",
      body: JSON.stringify({ place: place.value.trim(), latitude: latitude.value,
                             longitude: longitude.value }),
    }).then(() => {
      toast("Saved")
      return renderGeneral()
    }).catch((error) => toast(error.message, true))

    holder.replaceChildren(
      heading,
      el("section", null,
         el("h3", { textContent: "Where this badge is" }),
         el("p", { textContent: "A town or city, and a country if the name is a common one: Sheffield, or Sheffield, US. Looked up once and kept, so every extension asking gets the same answer, and a page naming somewhere else overrides it." }),
         el("label", { htmlFor: "hostplace", textContent: "Location" }), place,
         el("label", { htmlFor: "hostlat", textContent: "Latitude" }), latitude,
         el("label", { htmlFor: "hostlon", textContent: "Longitude" }), longitude,
         el("menu", null, save),
         el("p", { textContent: "Coordinates win over the name, for a spot no name lands on. Clear all three to set nowhere." })))
  }

  return { render: renderGeneral }
}
