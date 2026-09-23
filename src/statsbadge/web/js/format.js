const isPercent = (field, capabilities) => capabilities.percent_fields.includes(field) || field.endsWith("_pct")

function rate(bps) {
  if (bps >= 1024 ** 3) return `${(bps / 1024 ** 3).toFixed(1)}G`
  if (bps >= 1024 ** 2) return `${(bps / 1024 ** 2).toFixed(1)}M`
  if (bps >= 1024) return `${(bps / 1024).toFixed(0)}K`
  return `${bps.toFixed(0)}`
}

function size(mb) {
  if (mb >= 1024 ** 2) return `${(mb / 1024 ** 2).toFixed(1)}T`
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)}G`
  return `${mb.toFixed(0)}M`
}

function duration(seconds) {
  const whole = Math.trunc(seconds)
  if (whole >= 86400) return `${Math.floor(whole / 86400)}d${Math.floor((whole % 86400) / 3600)}h`
  if (whole >= 3600) return `${Math.floor(whole / 3600)}h${Math.floor((whole % 3600) / 60)}m`
  return `${Math.floor(whole / 60)}m`
}

export function fmt(value, field) {
  if (value === null || value === undefined) return "--"
  if (typeof value === "boolean") return value ? "yes" : "no"
  if (typeof value === "string") return value
  if (Array.isArray(value)) return String(value.length)
  if (field.endsWith("_bps")) return rate(value)
  if (field.endsWith("_mb")) return size(value)
  if (field === "uptime_s" || field === "secs_left") return duration(value)
  if (["freq", "clock", "rpm", "procs"].includes(field)) return value.toFixed(0)
  return value >= 100 ? value.toFixed(0) : value.toFixed(1)
}

export function shortUnit(field, capabilities) {
  if (field.endsWith("_bps")) return "B/s"
  if (field === "cores" || field === "pct" || field.endsWith("_pct")) return "%"
  if (field.endsWith("_mb")) return "B"
  if (field === "uptime_s" || field === "secs_left") return ""
  return capabilities.units[field] || ""
}

export function fractionOf(ref, value, frame, capabilities) {
  if (value === null || value === undefined || typeof value === "string"
      || typeof value === "boolean") return null
  const field = ref.split(".").pop()
  let top
  if (isPercent(field, capabilities)) top = 100
  else top = Number((frame?.peaks || {})[ref]) || capabilities.full_scale[field]
  if (!top) return null
  return Math.max(0, Math.min(1, value / top))
}

export function readingOf(frame, ref) {
  const [group, field] = ref.split(".")
  let held = (frame || {})[group]
  if (Array.isArray(held)) held = held[0]
  return held === undefined || held === null ? null : held[field]
}

export const NAMES = {
  "cpu.pct": "LOAD", "cpu.temp": "TEMP", "cpu.freq": "CLOCK", "cpu.procs": "PROCS",
  "mem.pct": "USED", "mem.used_mb": "USED", "mem.total_mb": "TOTAL", "mem.swap_pct": "SWAP",
  "gpu.pct": "LOAD", "gpu.temp": "TEMP", "gpu.power": "POWER", "gpu.mem_pct": "VRAM",
  "net.up_bps": "UP", "net.down_bps": "DOWN", "net.up_total_mb": "SENT",
  "net.down_total_mb": "RECV",
  "disk.pct": "FULL", "disk.read_bps": "READ", "disk.write_bps": "WRITE",
  "disk.used_mb": "USED", "disk.total_mb": "TOTAL", "disk.temp": "SSD",
  "power.battery_pct": "BATTERY", "power.package_w": "PACKAGE", "power.temp": "BATT",
  "sys.host": "HOST", "sys.os": "OS", "sys.cpu_name": "CPU", "sys.uptime_s": "UPTIME",
}
export const UNIT_SUFFIXES = ["_bps", "_mb", "_pct"]

export function nameFor(ref) {
  if (NAMES[ref]) return NAMES[ref]
  let field = ref.split(".").pop()
  for (const suffix of UNIT_SUFFIXES) {
    if (field.endsWith(suffix) && field.length > suffix.length) {
      field = field.slice(0, -suffix.length)
      break
    }
  }
  return field.replace(/_/g, " ").toUpperCase()
}
