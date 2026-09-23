export async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...options.headers }
  const response = await fetch(path, { ...options, headers })
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new Error((body && body.error) || response.statusText)
  return body
}

export function configPath(whose, path) {
  const base = path || "/api/config"
  return whose ? `${base}?badge=${encodeURIComponent(whose)}` : base
}
