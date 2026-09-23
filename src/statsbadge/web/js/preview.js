import { fmt, fractionOf, nameFor, readingOf, shortUnit } from "./format.js"

export const THUMB_W = 160
export const THUMB_H = 120
const THUMB_READING = 0.63

export const W = 320
export const H = 240
export const rgb = (parts) => `rgb(${parts.join(", ")})`
const face = (weight, size) => `${weight} ${size}px Lexend, system-ui, sans-serif`

const ICONS = {
  c: 0xe322, g: 0xe30d, m: 0xf7a3, d: 0xe1db, n: 0xeb2f, p: 0xea0b, f: 0xf168, y: 0xe31e,
  l: 0xe9e4, t: 0xf076, s: 0xe1b8, r: 0xe677, u: 0xf09b, o: 0xf090, b: 0xe1a5, e: 0xeb58,
  a: 0xeff2, h: 0xefd6,
}

const HEADER_H = 30
const FOOTER_H = 20
const BODY_TOP = HEADER_H
const BODY_H = H - HEADER_H - FOOTER_H
const PAD = 10
const SIZE_TITLE = 19
const SIZE_SMALL = 11
const SIZE_VALUE = 17
const SIZE_BIG = 26
const SIZE_HUGE = 44

const DIAL_GAP = 16
const DIAL_OUTER = 82
const DIAL_INNER = 62
const DIAL_C = [DIAL_GAP + DIAL_OUTER, BODY_TOP + Math.floor(BODY_H / 2) + 2]
const DIAL_FROM = 225
const DIAL_TO = 495
const READOUT_X = DIAL_C[0] + DIAL_OUTER + DIAL_GAP
const READOUT_W = W - READOUT_X - DIAL_GAP
const READOUT_H = 38

function readoutRows(count) {
  const room = BODY_TOP + BODY_H - 6 - count * READOUT_H
  const top = Math.max(BODY_TOP + 6, Math.min(DIAL_C[1] - DIAL_OUTER, room))
  return Array.from({ length: count }, (_, index) => top + index * READOUT_H)
}


const DIAL = { field: "cpu.pct", readouts: ["cpu.temp", "cpu.freq", "cpu.procs"] }
const BARS = "cpu.cores"
export const SERIES = ["net.down_bps", "net.up_bps"]
const TILE_REFS = ["disk.pct", "disk.read_bps", "disk.write_bps", "disk.used_mb"]

function chrome(ctx, palette, title, current, frame) {
  ctx.textBaseline = "top"
  ctx.textAlign = "left"

  ctx.fillStyle = rgb(palette.bg)
  ctx.fillRect(0, BODY_TOP, W, BODY_H)

  ctx.fillStyle = rgb(palette.panel)
  ctx.fillRect(0, 0, W, HEADER_H)
  ctx.fillRect(0, H - FOOTER_H, W, FOOTER_H)

  const chromePen = rgb(palette.accent_b || palette.accent)
  ctx.fillStyle = chromePen
  ctx.fillRect(0, HEADER_H - 2, W, 2)

  ctx.fillStyle = rgb(palette.ink)
  ctx.font = face(400, SIZE_TITLE)
  ctx.fillText(title, PAD, 4)

  ctx.textAlign = "right"
  ctx.fillStyle = rgb(palette.dim)
  ctx.font = face(400, SIZE_SMALL)
  ctx.fillText(hostName(frame), W - PAD, 10)
  ctx.textAlign = "left"

  pips(ctx, palette, chromePen, current)
}

const hostName = (frame) => readingOf(frame, "sys.host") || "workshop-pc"

const PIP_ROOM = W - PAD * 4
const PIP_MAX_W = 14
const PIP_GAP = 5
const PIP_DOT = 4
const PIP_TIGHT = 2
const PIP_H = 4

function pips(ctx, palette, chromePen, current, total = 8) {
  let gap = PIP_GAP
  let width = Math.min(PIP_MAX_W, Math.floor((PIP_ROOM - (total - 1) * gap) / total))
  if (width < PIP_DOT) {
    gap = PIP_TIGHT
    width = Math.max(PIP_DOT,
                     Math.min(PIP_MAX_W, Math.floor((PIP_ROOM - (total - 1) * gap) / total)))
  }
  const span = total * width + (total - 1) * gap
  const left = Math.floor((W - span) / 2)
  const top = H - FOOTER_H + Math.floor(FOOTER_H / 2) - 2
  const round = Math.min(2, Math.floor(width / 2))
  for (let i = 0; i < total; i += 1) {
    ctx.beginPath()
    ctx.roundRect(left + i * (width + gap), top, width, PIP_H, round)
    ctx.fillStyle = i === current ? chromePen : rgb(palette.grid)
    ctx.fill()
  }
}

function gauge(ctx, palette, [cx, cy], outer, inner, reading, gaugeFill) {
  const middle = (outer + inner) / 2
  const over = (outer - inner) * 0.15
  const at = (degrees) => ((degrees - 90) * Math.PI) / 180

  ctx.lineCap = "butt"
  ctx.lineWidth = outer - inner
  const sweep = DIAL_FROM + (DIAL_TO - DIAL_FROM) * reading

  ctx.beginPath()
  ctx.arc(cx, cy, middle, at(sweep), at(DIAL_TO))
  ctx.strokeStyle = rgb(palette.grid)
  ctx.stroke()

  if (gaugeFill === "ramp") {
    const steps = 96
    for (let i = 0; i < steps; i += 1) {
      ctx.beginPath()
      ctx.arc(cx, cy, middle, at(DIAL_FROM + ((sweep - DIAL_FROM) * i) / steps),
              at(DIAL_FROM + ((sweep - DIAL_FROM) * (i + 1)) / steps + 0.35))
      ctx.strokeStyle = rgb(rampAt(palette.ramp, (i / steps) * reading))
      ctx.stroke()
    }
  } else {
    ctx.beginPath()
    ctx.arc(cx, cy, middle, at(DIAL_FROM), at(sweep))
    ctx.strokeStyle = rgb(rampAt(palette.ramp, reading))
    ctx.stroke()
  }

  if (reading > 0.001) {
    ctx.beginPath()
    ctx.lineWidth = outer - inner + 2 * over
    ctx.arc(cx, cy, middle, at(sweep - 1.4), at(sweep + 1.4))
    ctx.strokeStyle = rgb(palette.ink)
    ctx.stroke()
    ctx.lineWidth = outer - inner
  }
}

export function drawThumb(ctx, palette, gaugeFill) {
  ctx.fillStyle = rgb(palette.bg)
  ctx.fillRect(0, 0, THUMB_W, THUMB_H)
  ctx.fillStyle = rgb(palette.accent)
  ctx.fillRect(0, THUMB_H - 8, THUMB_W, 8)
  gauge(ctx, palette, [46, 54], 30, 20, THUMB_READING, gaugeFill)

  const text = String(Math.round(THUMB_READING * 100))
  ctx.textBaseline = "alphabetic"
  ctx.font = face(400, 30)
  const textW = ctx.measureText(text).width
  ctx.fillStyle = rgb(palette.ink)
  ctx.fillText(text, 86, 65)
  ctx.font = face(400, 14)
  ctx.fillStyle = rgb(palette.dim)
  ctx.fillText("%", 86 + textW + 1, 65)
}

function drawDial(ctx, palette, _series, { frame, gaugeFill, caps }) {
  chrome(ctx, palette, "CPU", 0, frame)

  const value = readingOf(frame, DIAL.field)
  const reading = fractionOf(DIAL.field, value, frame, caps) ?? 0.635
  const [cx, cy] = DIAL_C
  gauge(ctx, palette, DIAL_C, DIAL_OUTER, DIAL_INNER, reading, gaugeFill)

  const text = fmt(value, "pct")
  const unit = shortUnit("pct", caps)
  const unitSize = Math.max(SIZE_SMALL, Math.trunc(SIZE_HUGE * 0.45))
  ctx.font = face(400, SIZE_HUGE)
  const readingW = ctx.measureText(text).width
  ctx.font = face(400, unitSize)
  const suffixW = ctx.measureText(unit).width
  const left = cx - (readingW + suffixW) / 2
  const top = cy - SIZE_HUGE * 0.62

  ctx.fillStyle = rgb(palette.ink)
  ctx.font = face(400, SIZE_HUGE)
  ctx.fillText(text, left, top)
  ctx.fillStyle = rgb(palette.dim)
  ctx.font = face(400, unitSize)
  ctx.fillText(unit, left + readingW, top + SIZE_HUGE - unitSize)

  const rows = readoutRows(DIAL.readouts.length)
  DIAL.readouts.forEach((ref, index) => {
    const field = ref.split(".").pop()
    const held = readingOf(frame, ref)
    const y = rows[index]
    ctx.fillStyle = rgb(palette.dim)
    ctx.font = face(400, SIZE_SMALL)
    ctx.fillText(nameFor(ref), READOUT_X, y)
    ctx.fillStyle = rgb(palette.ink)
    ctx.font = face(400, SIZE_VALUE)
    ctx.fillText(fmt(held, field) + shortUnit(field, caps), READOUT_X, y + 10)

    const part = fractionOf(ref, held, frame, caps)
    if (part === null) return
    const filled = Math.trunc(READOUT_W * part)
    ctx.fillStyle = rgb(palette.grid)
    ctx.fillRect(READOUT_X + filled, y + 28, READOUT_W - filled, 3)
    if (filled) {
      ctx.fillStyle = rgb(rampAt(palette.ramp, part))
      ctx.fillRect(READOUT_X, y + 28, filled, 3)
    }
  })
}

const CORES = [0.31, 0.882, 0.125, 0.741, 0.2, 0.955, 0.602, 0.05]

function drawBars(ctx, palette, _series, { frame, caps }) {
  chrome(ctx, palette, "CORES", 1, frame)
  const held = readingOf(frame, BARS)
  const values = (Array.isArray(held) ? held : CORES.map((v) => v * 100)).slice(0, 16)
  const count = values.length
  const top = BODY_TOP + 6
  const slot = Math.max(6, Math.floor((BODY_H - 12) / count))
  const height = Math.max(4, slot - 3)

  ctx.font = face(400, SIZE_SMALL)
  const readings = values.map((value) => fmt(value, "cores") + shortUnit("cores", caps))
  const labelW = Math.max(...values.map((_v, i) => ctx.measureText(String(i)).width))
  const valueW = Math.max(...readings.map((text) => ctx.measureText(text).width))
  const x = PAD + labelW + COLUMN_GAP
  const width = Math.max(20, W - x - COLUMN_GAP - valueW - PAD)

  values.forEach((value, index) => {
    const part = Math.max(0, Math.min(1, value / 100))
    const y = top + index * slot
    ctx.textAlign = "left"
    ctx.fillStyle = rgb(palette.dim)
    ctx.font = face(400, SIZE_SMALL)
    ctx.fillText(String(index), PAD, y - 1)

    const filled = part > 0 ? Math.max(1, Math.trunc(width * part)) : 0
    ctx.fillStyle = rgb(palette.grid)
    ctx.fillRect(x + filled, y, width - filled, height)
    if (filled) {
      ctx.fillStyle = rgb(rampAt(palette.ramp, part))
      ctx.fillRect(x, y, filled, height)
    }
    ctx.textAlign = "right"
    ctx.fillStyle = rgb(palette.ink)
    ctx.fillText(readings[index], W - PAD, y - 1)
    ctx.textAlign = "left"
  })
}

const COLUMN_GAP = 8

const DOWN = [0.12, 0.2, 0.55, 0.86, 0.7, 0.52, 0.62, 0.44, 0.2, 0.1, 0.08, 0.3, 0.66, 0.8,
              0.62, 0.5, 0.72, 0.9, 0.55, 0.2, 0.12, 0.1, 0.26, 0.42, 0.3, 0.18, 0.12]
const UP = [0.05, 0.08, 0.14, 0.2, 0.16, 0.12, 0.18, 0.14, 0.08, 0.05, 0.04, 0.1, 0.16, 0.2,
            0.14, 0.1, 0.16, 0.22, 0.12, 0.06, 0.05, 0.04, 0.09, 0.13, 0.1, 0.07, 0.05]
function drawGraph(ctx, palette, series, { frame, history, caps }) {
  chrome(ctx, palette, "NETWORK", 4, frame)

  const plots = SERIES.map((ref) => history[ref] || [])
  const live = plots.some((ring) => ring.length > 1)
  const peak = live ? Math.max(...plots.flat().map((v) => v ?? 0), 1) * 1.15 : 9.8 * 1024 ** 2
  const peakText = fmt(peak, "down_bps") + shortUnit("down_bps", caps)

  ctx.font = face(400, SIZE_SMALL)
  const left = PAD + Math.max(ctx.measureText(peakText).width, ctx.measureText("0").width) + 4
  const top = BODY_TOP + 8
  const width = W - left - PAD
  const height = BODY_H - 26
  const right = left + width
  const bottom = top + height

  ctx.fillStyle = rgb(palette.grid)
  for (let i = 0; i < 5; i += 1) {
    ctx.fillRect(left, top + (height * i) / 4, width, 1)
  }
  ctx.fillStyle = rgb(palette.dim)
  ctx.fillText(peakText, PAD, top - 4)
  ctx.fillText("0", PAD, top + height - 8)

  const plot = (points, index) => {
    ctx.globalAlpha = palette.series_alpha[index] / 255
    ctx.beginPath()
    ctx.moveTo(left, bottom)
    points.forEach((value, at) => {
      ctx.lineTo(left + ((right - left) * at) / (points.length - 1),
                 bottom - (bottom - top) * value)
    })
    ctx.lineTo(right, bottom)
    ctx.closePath()
    ctx.fillStyle = rgb(series[index])
    ctx.fill()
    ctx.globalAlpha = 1
  }
  const scaled = plots.map((ring) => ring.map((v) => Math.max(0, (v ?? 0) / peak)))
  plot(live ? scaled[0] : DOWN, 0)
  plot(live ? scaled[1] : UP, 1)

  SERIES.forEach((ref, index) => {
    const label = nameFor(ref)
    const x = left + index * 110
    const y = H - FOOTER_H - 14
    ctx.fillStyle = rgb(series[index])
    ctx.fillRect(x, y + 3, 10, 4)
    ctx.fillStyle = rgb(palette.dim)
    ctx.font = face(400, SIZE_SMALL)
    ctx.fillText(label, x + 14, y - 2)
  })
}

const TILES = [["FULL", "74.2%", 0.742, "l"], ["READ", "50.0MB/s", 0.5, "u"],
               ["WRITE", "8.0MB/s", 0.08, "o"], ["USED", "687.3GB", 0.62, "a"]]

function drawGrid(ctx, palette, _series, { frame, caps }) {
  chrome(ctx, palette, "DISK", 5, frame)
  const count = TILE_REFS.length
  const columns = count > 4 ? 3 : 2
  const rows = Math.ceil(count / columns)
  const cellW = Math.floor((W - PAD * 2 - (columns - 1) * 6) / columns)
  const cellH = Math.floor((BODY_H - 12 - (rows - 1) * 6) / rows)
  const size = rows < 3 ? SIZE_BIG : SIZE_VALUE

  TILE_REFS.forEach((ref, index) => {
    const field = ref.split(".").pop()
    const held = readingOf(frame, ref)
    const part = fractionOf(ref, held, frame, caps) ?? TILES[index][2]
    const x = PAD + (index % columns) * (cellW + 6)
    const y = BODY_TOP + 6 + Math.floor(index / columns) * (cellH + 6)

    ctx.beginPath()
    ctx.roundRect(x, y, cellW, cellH, 5)
    ctx.fillStyle = rgb(palette.panel)
    ctx.fill()
    if (part !== null) {
      ctx.fillStyle = rgb(rampAt(palette.ramp, part))
      ctx.fillRect(x, y + cellH - 3, Math.trunc(cellW * part), 3)
    }

    ctx.textAlign = "left"
    ctx.fillStyle = rgb(palette.dim)
    ctx.font = face(400, SIZE_SMALL)
    ctx.fillText(nameFor(ref), x + 7, y + 5)
    ctx.textAlign = "right"
    ctx.font = `${SIZE_VALUE}px "Badge Icons"`
    ctx.fillText(String.fromCodePoint(ICONS[TILES[index][3]]), x + cellW - 7, y + 4)
    ctx.textAlign = "left"
    ctx.fillStyle = rgb(palette.ink)
    ctx.font = face(400, size)
    ctx.fillText(held === null ? TILES[index][1] : fmt(held, field) + shortUnit(field, caps),
                 x + 7, y + Math.floor(cellH / 2) - Math.floor(size / 2) + 2)
  })
}

export const SCREENS = [drawDial, drawBars, drawGraph, drawGrid]

function rampAt(stops, at) {
  let [low, first] = stops[0]
  for (const [position, colour] of stops) {
    if (at <= position) {
      const span = position - low
      const part = span <= 0 ? 0 : (at - low) / span
      return first.map((from, index) => Math.round(from + (colour[index] - from) * part))
    }
    [low, first] = [position, colour]
  }
  return stops[stops.length - 1][1]
}
