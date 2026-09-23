export const $ = (id) => document.getElementById(id)
export const pick = (selector) => document.querySelector(selector)
export const all = (selector) => [...document.querySelectorAll(selector)]

export function el(tag, props, ...children) {
  const node = document.createElement(tag)
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined) continue
    if (key.includes("-")) node.setAttribute(key, value)
    else node[key] = value
  }
  node.append(...children.flat().filter((child) => child !== null && child !== undefined))
  return node
}

const MINOR = new Set(["a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on",
                       "or", "the", "to", "with"])

export function titleCase(text) {
  return String(text).replace(/[-_]+/g, " ").trim().split(/\s+/)
    .map((word, index) => (index && MINOR.has(word.toLowerCase())
      ? word.toLowerCase()
      : word.charAt(0).toUpperCase() + word.slice(1)))
    .join(" ")
}

export function toast(message, bad) {
  const node = el("div", { className: bad ? "toast bad" : "toast", textContent: message })
  document.body.appendChild(node)
  setTimeout(() => node.remove(), 2600)
}
