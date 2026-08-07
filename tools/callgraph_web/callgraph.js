// The call-graph viewer. Reads the JSON the generator inlined above onto a 2D canvas.
// Coordinates come precomputed, leaving no layout here and no dependencies. Colour is
// the selected hot measure, shape marks what a node is, size is fan-in.

// Every edge carries a `via` for the rule that found it. Anything but `static` is drawn
// dashed and named in the panel, because a graph that quietly invents edges is worse
// than one that draws none.

const DATA = JSON.parse(document.getElementById("graph").textContent);
const N = DATA.nodes;
const E = DATA.edges;
const COUNT = N.kind.length;

const $ = (id) => document.getElementById(id);

// -- what the settings mean -------------------------------------------------

// Every one of these ships on every node, so changing what "hot" means never means
// regenerating anything.
const MEASURES = [
  { key: "traced", label: "observed calls",
    hint: "counted in a real run - null where no run touched it, which is not zero" },
  { key: "fan_in", label: "uses", hint: "call and read sites" },
  { key: "fan_out", label: "fan-out", hint: "distinct things it reaches" },
  { key: "globals", label: "global state", hint: "3x written + read + half of deeper" },
  { key: "complexity", label: "complexity", hint: "branches plus one" },
  { key: "loop_depth", label: "loop depth", hint: "deepest nesting" },
  { key: "allocs", label: "allocations", hint: "sites that build something" },
  { key: "alloc_in_loop", label: "builds in a loop",
    hint: "construction sites inside a loop, the badge's recurring cost" },
  { key: "cost_self", label: "priced work here",
    hint: "microseconds of measured firmware calls in this body alone" },
  { key: "cost", label: "reachable work (bound)",
    hint: "every priced call reachable from here - an upper bound, not a time: "
        + "both arms of every branch are counted" },
  { key: "lines", label: "lines", hint: "how big it is" },
  // Ordinal, so it goes in raw. Ranking would fatten the crowded
  // middle levels into a band and squash the sparse ends into nothing, which is exactly
  // what the axis is for.
  { key: "flow", label: "depth in the machine", strata: true,
    hint: "0 is an entry point, the most is a firmware primitive; every edge descends" },
];

// A cost that came mostly from defaults, with few priced calls behind it, is not worth
// showing at full strength, whatever its rank.
const CONFIDENT = 0.5;

// Shape marks what a node is, because colour is spent on the measure. A constant and a
// piece of state share an outline, filled or not: the split is there to separate coupling
// from state.
const SHAPES = {
  module: "ring",
  class: "square",
  function: "circle",
  method: "circle",
  property: "circle",
  const: "diamond-open",
  state: "diamond",
  table: "hex",
  external: "square-open",
};

const KIND_ORDER = ["module", "class", "function", "method", "property",
                    "const", "state", "table", "external"];

const EDGE_ORDER = ["call", "instantiate", "read", "write", "import",
                    "inherit", "override", "register", "tag", "resume"];

// Which vias are a rule's inference, as against a call written in the source.
const INFERRED = new Set(["table", "vtable", "argparse", "entrypoint", "framework",
                          "dynamic", "hint", "handed", "trace"]);

// -- the third axis ---------------------------------------------------------

// Axonometric, not perspective. Size already encodes fan-in, so under perspective a near
// node would be bigger for two indistinguishable reasons. The sprite cache keys on a
// rounded radius, which per-node depth scaling would blow out to thousands of tiles.

// Tilt zero then lands *exactly* on the flat picture. At tilt 0 with azimuth 0 the two
// projection lines reduce to a plain affine transform, and an explicit fast path takes
// that case.
const TILT_MAX = 70;           // past this, occlusion doubles as the view nears horizontal
const TILT_DEFAULT = 52;
const AZIMUTH_DEFAULT = 25;    // off-axis, so the module columns do not hide each other
// How tall the axis stands, against the footprint it stands on. Generous by necessity:
// screen height mixes plan position with height, and a level worth less than a typical
// move across the layout leaves the plan swamping the depth.

// At 1400 a level was 78 units against displacements of 500 and more. Taken from the
// layout, for any codebase.
const RISE = (() => {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (let i = 0; i < COUNT; i++) {
    if (N.kind[i] === "external") continue;
    minX = Math.min(minX, N.x[i]); maxX = Math.max(maxX, N.x[i]);
    minY = Math.min(minY, N.y[i]); maxY = Math.max(maxY, N.y[i]);
  }
  const across = Math.max(maxX - minX, maxY - minY);
  return Math.max(1500, across * 1.5);
})();
const FOG_NEAR = 1.0;
const FOG_FAR = 0.3;

const RAMP_STEPS = 32;
const MIN_RADIUS = 3;
const MAX_RADIUS = 17;
// Below this, modules only. Low, because the axis stands tall and fitting the graph
// tilted lands around 13%: at 0.15, opening in 3D showed empty boxes. The nodes are
// specks at this zoom, and a constellation of them still reads.
const MODULE_ZOOM = 0.07;
const MODULE_LABEL_ZOOM = 1.1;  // below this, modules are named
const LABEL_ZOOM = 1.15;       // above this, every node label that fits

// -- state ------------------------------------------------------------------

let theme = null;
let sprites = new Map();
const camera = { x: 0, y: 0, z: 0, scale: 1, az: 0, tilt: 0 };

// Which measure gives height, and how much of it is showing. Kept apart from `filters`
// because the slab must never touch `shown`: that re-ranks every measure, and every
// colour in the picture would jump as you scrub.
const view = {
  axis: "flow",
  slabLow: 0,
  slabHigh: 1,
  height: new Float32Array(COUNT),   // 0..1 per node
  span: [0, 0],                      // the depth range of what is on screen
  ground: 0,                         // just under the deepest node showing
};
let measure = MEASURES[0].key;
let rank = new Float32Array(COUNT);   // 0..1 percentile of the coloured measure
let ranks = new Map();               // every measure's percentiles, for the second axis
const shown = new Uint8Array(COUNT);
let hover = -1;
let picked = -1;
let litEdge = -1;
let trail = [];
let matched = new Set();

// -- the timeline -----------------------------------------------------------

const SPEEDS = [1, 4, 16, 64, 256];
const TRAIL_LENGTH = 60;      // how many calls back the comet tail reaches

const play = {
  scene: -1,
  at: 0,
  running: false,
  speed: 16,
  since: 0,
  stack: [],
  recent: new Map(),        // node -> how many calls ago it returned
  follow: false,            // let the shown band track the innermost frame
};

const filters = {
  targets: new Set(DATA.targets.map((t) => t.id)),
  kinds: new Set(KIND_ORDER.filter((k) => k !== "external")),
  edges: new Set(EDGE_ORDER),
  modules: new Set(DATA.modules.map((m) => m.id)),
  minFanIn: 0,
  minRank: 0,
  // A second measure with its own floor, so "hot and complex" - the pair worth looking at
  // for somewhere to optimise - is a thing you can ask for outright.
  also: "complexity",
  alsoRank: 0,
  only: null,          // "unreferenced" | "unreset" | "inferred" | null
};

const board = $("board");
const ctx = board.getContext("2d");
let dpr = 1;

// -- adjacency --------------------------------------------------------------

const outOf = Array.from({ length: COUNT }, () => []);
const intoOf = Array.from({ length: COUNT }, () => []);
for (let i = 0; i < E.from.length; i++) {
  outOf[E.from[i]].push(i);
  intoOf[E.to[i]].push(i);
}

const moduleById = new Map(DATA.modules.map((m) => [m.id, m]));
const targetById = new Map(DATA.targets.map((t) => [t.id, t]));

// The one that runs on the badge, found by what it declares and not by its index.
const BADGE_TARGET = (DATA.targets.find((t) => t.lang === "micropython") || {}).id;

// The deepest level the flow axis reaches, for the panel to read a level against.
const FLOW_MAX = N.flow ? N.flow.reduce((most, at) => Math.max(most, at), 0) : 0;

function edgeType(i) { return DATA.edge_types[E.type[i]]; }
function edgeVia(i) { return DATA.via[E.via[i]]; }
function moduleName(i) {
  const module = moduleById.get(N.module[i]);
  return module ? module.name : "";
}

// -- palette ----------------------------------------------------------------

function pickTheme() {
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  theme = DATA.palettes[dark ? "dark" : "light"] || DATA.palettes.dark;
  sprites = new Map();
  paintLegend();
}

/** The ramp colour for a 0..1 position, from the steps the generator sampled in OKLCH. */
function rampAt(at) {
  const step = Math.max(0, Math.min(RAMP_STEPS - 1, Math.round(at * (RAMP_STEPS - 1))));
  return theme.ramp[step];
}

function paintLegend() {
  const stops = theme.ramp.map((hex, i) =>
    `${hex} ${((i / (RAMP_STEPS - 1)) * 100).toFixed(1)}%`).join(", ");
  $("ramp").style.background = `linear-gradient(90deg, ${stops})`;
}

// -- measures and ranking ---------------------------------------------------

function valueOf(i) {
  const raw = N[measure];
  if (!raw) return 0;
  const value = raw[i];
  return value === null ? 0 : value;
}

/** Whether this node has no answer for the measure, as opposed to a low one. */
function untraced(i) {
  return measure === "traced" && N.traced && N.traced[i] === null;
}

/** Percentile rank over what is currently on screen.
 *
 * Not min-max and not log. Fan-in here is heavily power-law, where one node with ten
 * times everything else's value renders the rest of the graph cold. Rank spreads the
 * visible set across the ramp, and the legend carries the real numbers so a colour can
 * still be read back to one.
 */
function rankBy(key, live) {
  const raw = N[key];
  const at = (i) => (raw ? raw[i] : 0);
  const sorted = live.slice().sort((a, b) => at(a) - at(b));
  const out = new Float32Array(COUNT);
  const span = Math.max(1, sorted.length - 1);
  for (let i = 0; i < sorted.length; i++) {
    // Equal values share a rank, or an arbitrary tiebreak shows as a false gradient.
    let end = i;
    while (end + 1 < sorted.length && at(sorted[end + 1]) === at(sorted[i])) end++;
    const share = ((i + end) / 2) / span;
    for (let k = i; k <= end; k++) out[sorted[k]] = share;
    i = end;
  }
  return { rank: out, sorted };
}

function rerank() {
  const live = [];
  for (let i = 0; i < COUNT; i++) if (shown[i]) live.push(i);

  ranks = new Map();
  for (const spec of MEASURES) {
    if (N[spec.key]) ranks.set(spec.key, rankBy(spec.key, live));
  }
  const chosen = ranks.get(measure);
  rank = chosen ? chosen.rank : new Float32Array(COUNT);
  reheight();

  const sorted = chosen ? chosen.sorted : [];
  const ticks = $("ticks");
  ticks.textContent = "";
  for (const where of [0, 0.25, 0.5, 0.75, 1]) {
    const value = sorted.length
      ? short(valueOf(sorted[Math.round(where * (sorted.length - 1))]))
      : "-";
    ticks.appendChild(element("span", null, value));
  }
}

function rankIn(key, i) {
  const found = ranks.get(key);
  return found ? found.rank[i] : 0;
}

function short(value) {
  if (value === null || value === undefined) return "-";
  if (value >= 10000) return `${Math.round(value / 1000)}k`;
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

// -- filtering --------------------------------------------------------------

function applyFilters() {
  for (let i = 0; i < COUNT; i++) {
    const kind = N.kind[i];
    let ok = filters.kinds.has(kind);
    if (ok && N.target[i] !== null) {
      ok = filters.targets.has(N.target[i]) && filters.modules.has(N.module[i]);
    }
    if (ok && N.fan_in[i] < filters.minFanIn) ok = false;
    if (ok && filters.only === "unreferenced") ok = N.flags[i].includes("unreached");
    if (ok && filters.only === "unreset") ok = N.flags[i].includes("unreset");
    if (ok && filters.only === "inferred") {
      ok = outOf[i].concat(intoOf[i]).some((e) => INFERRED.has(edgeVia(e)));
    }
    shown[i] = ok ? 1 : 0;
  }
  rerank();
  if (filters.minRank > 0 || filters.alsoRank > 0) {
    for (let i = 0; i < COUNT; i++) {
      if (!shown[i]) continue;
      if (rank[i] < filters.minRank) shown[i] = 0;
      else if (filters.alsoRank > 0
               && rankIn(filters.also, i) < filters.alsoRank) shown[i] = 0;
    }
  }
  draw();
}

// -- camera -----------------------------------------------------------------

function width() { return board.width / dpr; }
function height() { return board.height / dpr; }

function flat() { return camera.tilt === 0 && camera.az === 0; }

/** Recompute every node's 0..1 height from the chosen axis.
 *
 * An ordinal axis goes in raw so its levels stay evenly spaced; a power-law one goes in
 * as a percentile, because these span 0-26 to 1-20,163,210 and nothing linear survives
 * that. `strata` on the measure picks between them.
 */
function reheight() {
  const spec = MEASURES.find((m) => m.key === view.axis);
  view.height = new Float32Array(COUNT);
  if (!spec || !N[spec.key]) return;

  if (spec.strata) {
    const raw = N[spec.key];
    let most = 0;
    for (let i = 0; i < COUNT; i++) most = Math.max(most, raw[i] || 0);
    for (let i = 0; i < COUNT; i++) view.height[i] = most ? (raw[i] || 0) / most : 0;
  } else {
    const found = ranks.get(spec.key);
    if (found) view.height = found.rank;
  }
}

// The two screen axes and the depth axis, as unit vectors. At tilt 0 with azimuth 0 they
// reduce to (1,0,0) and (0,1,0). That exactness is what the fast path relies on.
function basis() {
  const a = camera.az * Math.PI / 180;
  const t = camera.tilt * Math.PI / 180;
  const ca = Math.cos(a), sa = Math.sin(a), ct = Math.cos(t), st = Math.sin(t);
  return {
    ux: ca, uy: sa, uz: 0,
    vx: -sa * ct, vy: ca * ct, vz: -st,
    wx: -sa * st, wy: ca * st, wz: ct,
  };
}

let axes = basis();

function project(x, y, z) {
  if (flat()) {
    return [(x - camera.x) * camera.scale + width() / 2,
            (y - camera.y) * camera.scale + height() / 2, 0];
  }
  const dx = x - camera.x, dy = y - camera.y, dz = z - camera.z;
  const b = axes;
  return [(dx * b.ux + dy * b.uy + dz * b.uz) * camera.scale + width() / 2,
          (dx * b.vx + dy * b.vy + dz * b.vz) * camera.scale + height() / 2,
          dx * b.wx + dy * b.wy + dz * b.wz];
}

function riseOf(i) { return -view.height[i] * RISE; }

function groundLevel() {
  let deepest = 0;
  for (let i = 0; i < COUNT; i++) {
    if (shown[i]) deepest = Math.max(deepest, view.height[i]);
  }
  return -(deepest * RISE + 90);
}

function at(i) { return project(N.x[i], N.y[i], riseOf(i)); }

/** Screen back to the ground plane, which is all panning and zoom-at-cursor need. */
function toWorld(sx, sy) {
  if (flat()) {
    return [(sx - width() / 2) / camera.scale + camera.x,
            (sy - height() / 2) / camera.scale + camera.y];
  }
  const b = axes;
  const px = (sx - width() / 2) / camera.scale;
  const py = (sy - height() / 2) / camera.scale;
  // Solve the 2x2 [ux uy; vx vy] against the z = camera.z plane.
  const det = b.ux * b.vy - b.uy * b.vx;
  if (!det) return [camera.x, camera.y];
  return [camera.x + (px * b.vy - py * b.uy) / det,
          camera.y + (py * b.ux - px * b.vx) / det];
}

/** How much a node at this depth fades, so distance reads without perspective. */
function fogAt(depth) {
  const [near, far] = view.span;
  if (far <= near) return FOG_NEAR;
  const along = Math.max(0, Math.min(1, (depth - near) / (far - near)));
  return FOG_NEAR + (FOG_FAR - FOG_NEAR) * along;
}

/** Whether a node is inside the shown band of the axis. Dims, never filters. */
function inSlab(i) {
  return view.height[i] >= view.slabLow - 1e-6
         && view.height[i] <= view.slabHigh + 1e-6;
}

/** Frame a set at the current angle, by projecting it and solving for the scale.
 *
 * Projected, not measured in world x/y: once the view is tilted the
 * bounding box on screen is not the footprint: the height contributes to it.
 */
function fit(only) {
  const want = [];
  for (let i = 0; i < COUNT; i++) {
    if (only ? !only.has(i) : !shown[i]) continue;
    want.push(i);
  }
  if (!want.length) return;

  let cx = 0, cy = 0, cz = 0;
  for (const i of want) { cx += N.x[i]; cy += N.y[i]; cz += riseOf(i); }
  camera.x = cx / want.length;
  camera.y = cy / want.length;
  camera.z = cz / want.length;

  // Measure at unit scale about the new pivot, then solve.
  const was = camera.scale;
  camera.scale = 1;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const i of want) {
    const [sx, sy] = at(i);
    minX = Math.min(minX, sx); maxX = Math.max(maxX, sx);
    minY = Math.min(minY, sy); maxY = Math.max(maxY, sy);
  }
  const pad = 80;
  const span = Math.max(1, maxX - minX), tall = Math.max(1, maxY - minY);
  camera.scale = Math.max(0.05, Math.min(3,
    Math.min((width() - pad * 2) / span, (height() - pad * 2) / tall)));
  if (!isFinite(camera.scale)) camera.scale = was;
  draw();
}

// -- sprites ----------------------------------------------------------------

function radiusOf(i) {
  if (N.kind[i] === "module") return 6;
  const size = Math.sqrt(Math.max(1, N.lines[i]));
  return Math.max(MIN_RADIUS, Math.min(MAX_RADIUS, 2.2 + size * 0.9));
}

function scaledRadius(i) {
  return Math.max(2, radiusOf(i) * Math.min(1.6, Math.max(0.55, camera.scale)));
}

/** One pre-rendered image per appearance, blitted rather than drawn as a path.
 *
 * A thousand `arc()` submissions a frame is several milliseconds; a thousand `drawImage`
 * calls is well under one. The 32 ramp steps are the quantisation already needed here, so
 * discretising the colour costs nothing.
 */
function spriteFor(kind, step, radius, state) {
  const key = `${kind}|${step}|${radius}|${state}`;
  const found = sprites.get(key);
  if (found) return found;

  const side = Math.ceil((radius + 4) * 2);
  const tile = document.createElement("canvas");
  tile.width = tile.height = side * 2;
  const pen = tile.getContext("2d");
  pen.scale(2, 2);
  pen.translate(side / 2, side / 2);

  const fill = kind === "external" ? theme.dim : rampAt(step / (RAMP_STEPS - 1));
  const shape = SHAPES[kind] || "circle";
  const open = shape.endsWith("-open") || shape === "ring";

  pen.beginPath();
  if (shape === "circle" || shape === "ring") {
    pen.arc(0, 0, radius, 0, Math.PI * 2);
  } else if (shape === "square" || shape === "square-open") {
    const half = radius * 0.86;
    pen.roundRect(-half, -half, half * 2, half * 2, radius * 0.32);
  } else if (shape === "diamond" || shape === "diamond-open") {
    const reach = radius * 1.12;
    pen.moveTo(0, -reach); pen.lineTo(reach, 0);
    pen.lineTo(0, reach); pen.lineTo(-reach, 0);
    pen.closePath();
  } else if (shape === "hex") {
    for (let corner = 0; corner < 6; corner++) {
      const angle = (Math.PI / 3) * corner - Math.PI / 2;
      const px = Math.cos(angle) * radius * 1.06;
      const py = Math.sin(angle) * radius * 1.06;
      if (corner === 0) pen.moveTo(px, py); else pen.lineTo(px, py);
    }
    pen.closePath();
  }

  if (open) {
    pen.fillStyle = theme.bg;
    pen.fill();
    pen.strokeStyle = fill;
    pen.lineWidth = Math.max(1.4, radius * 0.3);
    pen.stroke();
  } else {
    pen.fillStyle = fill;
    pen.fill();
  }

  if (state === "sel") {
    pen.strokeStyle = theme.accent;
    pen.lineWidth = 2.4;
    pen.stroke();
  } else if (state === "badge") {
    // The badge target takes a heavier stroke and no hue of its own, so it survives
    // being a four-pixel dot and leaves the ramp to mean one thing.
    pen.globalAlpha = 0.45;
    pen.strokeStyle = theme.ink;
    pen.lineWidth = 1.6;
    pen.stroke();
    pen.globalAlpha = 1;
  } else {
    pen.strokeStyle = theme.grid;
    pen.lineWidth = 1;
    pen.stroke();
  }

  const made = { image: tile, side };
  sprites.set(key, made);
  return made;
}

// -- drawing ----------------------------------------------------------------

let queued = false;
function draw() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => { queued = false; paint(); });
}

function paint() {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = theme.bg;
  ctx.fillRect(0, 0, width(), height());

  const wide = camera.scale < MODULE_ZOOM;
  view.ground = flat() ? 0 : groundLevel();
  const live = firing();
  // While a timeline is running the focus is what is on the stack, not what was clicked.
  const focus = live ? null : focusSet();

  paintModuleBoxes(wide, focus);
  if (!wide) {
    paintEdges(focus, live);
    paintNodes(focus, live);
  }
  if (camera.scale < MODULE_LABEL_ZOOM) paintModuleLabels(wide);
  if (!wide) paintLabels(focus);

  $("scale").textContent =
    `${COUNT} nodes · ${E.from.length} edges · ${Math.round(camera.scale * 100)}%`;
  let visible = 0;
  for (let i = 0; i < COUNT; i++) visible += shown[i];
  const timeline = scene();
  const parts = [];
  if (live && timeline) {
    parts.push(`${timeline.name} · ${play.stack.length} frames deep`);
  } else if (wide) {
    parts.push("zoom in for nodes");
  } else {
    parts.push(`${visible} shown${focus ? ` · ${focus.size} in focus` : ""}`);
  }
  if (!flat()) {
    const spec = MEASURES.find((m) => m.key === view.axis);
    parts.push(`height ${spec ? spec.label : view.axis}`);
    parts.push(`az ${Math.round(camera.az)}° tilt ${Math.round(camera.tilt)}°`);
    if (view.slabHigh < 1) parts.push(`band top ${Math.round(view.slabHigh * 100)}%`);
  }
  $("hud").textContent = parts.join("  ·  ");
}

/** The selection and its immediate neighbours, or null when nothing is picked. */
function focusSet() {
  if (picked < 0) return null;
  const set = new Set([picked]);
  for (const e of outOf[picked]) set.add(E.to[e]);
  for (const e of intoOf[picked]) set.add(E.from[e]);
  return set;
}

/** Module footprints, flat on the ground plane.
 *
 * Drawn first and kept flat, never extruded into prisms: the median module spans a
 * third of the levels, so thirty-nine translucent boxes would become the scene's main
 * occluder. Flat, they give the view a floor, which is the strongest depth reference an
 * axonometric projection has - the nodes float above their own territory.
 */
function paintModuleBoxes(wide, focus) {
  ctx.lineWidth = 1;
  for (const module of DATA.modules) {
    if (!filters.targets.has(module.target) || !filters.modules.has(module.id)) continue;
    const [x, y, w, h] = module.box;
    const corners = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
      .map(([cx, cy]) => project(cx, cy, flat() ? 0 : view.ground));
    const xs = corners.map((c) => c[0]), ys = corners.map((c) => c[1]);
    if (Math.max(...xs) < -40 || Math.max(...ys) < -40
        || Math.min(...xs) > width() + 40 || Math.min(...ys) > height() + 40) continue;

    ctx.fillStyle = theme.panel;
    ctx.strokeStyle = theme.grid;
    ctx.beginPath();
    if (flat()) {
      ctx.roundRect(corners[0][0], corners[0][1],
                    corners[1][0] - corners[0][0], corners[3][1] - corners[0][1], 6);
    } else {
      ctx.moveTo(corners[0][0], corners[0][1]);
      for (const [cx, cy] of corners.slice(1)) ctx.lineTo(cx, cy);
      ctx.closePath();
    }
    ctx.globalAlpha = wide ? 0.9 : (focus ? 0.2 : 0.35);
    ctx.fill();
    ctx.globalAlpha = focus && !wide ? 0.25 : 0.6;
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
}

function paintModuleLabels(wide) {
  ctx.font = "500 12px system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = wide ? "middle" : "bottom";
  const placed = [];
  for (const module of DATA.modules) {
    if (!filters.targets.has(module.target) || !filters.modules.has(module.id)) continue;
    // Centred when the box is all there is, above it once the nodes are showing.
    const floor = flat() ? 0 : view.ground;
    const [cx, cy] = wide
      ? project(module.box[0] + module.box[2] / 2,
                module.box[1] + module.box[3] / 2, floor)
      : project(module.box[0] + module.box[2] / 2, module.box[1], floor);
    if (cx < -80 || cy < -20 || cx > width() + 80 || cy > height() + 20) continue;
    const label = module.name.split(".").pop();
    const span = ctx.measureText(label).width;
    const box = [cx - span / 2 - 2, cy - 13, span + 4, 13];
    if (placed.some((other) => overlaps(box, other))) continue;
    placed.push(box);
    if (!wide) {
      ctx.fillStyle = theme.bg;
      ctx.fillRect(box[0], box[1], box[2], box[3]);
    }
    ctx.fillStyle = wide ? theme.ink : theme.dim;
    ctx.fillText(label, cx, wide ? cy : cy - 2);
  }
}

/** Every edge in one path per bucket, and each edge as two halves so direction reads.
 *
 * A caller half in the dim colour and a callee half in the brighter one gives direction
 * at any zoom, for two `stroke()` calls in total. Arrowheads on thousands of
 * one-pixel lines are invisible on their own and noise in aggregate.
 */
function paintEdges(focus, live) {
  const buckets = [
    { colour: theme.grid, alpha: focus ? 0.1 : 0.26, line: 1, dash: [] },
    { colour: theme.dim, alpha: focus ? 0.12 : 0.3, line: 1, dash: [] },
    { colour: theme.dim, alpha: focus ? 0.3 : 0.45, line: 1.1, dash: [4, 3] },
    { colour: theme.ramp[Math.round((RAMP_STEPS - 1) * 0.7)], alpha: 0.9, line: 1.7,
      dash: [] },
    { colour: theme.ramp[0], alpha: 0.9, line: 1.7, dash: [] },
    { colour: theme.accent, alpha: 1, line: 2.6, dash: [] },
  ];
  const TAIL = 0, HEAD = 1, GUESS = 2, INTO = 3, OUTOF = 4, LIT = 5;

  // Three depth bands per colour, so distance reads on the edges too without giving up
  // the batching that makes four thousand of them free: 18 stroke calls, not 4000.
  const BANDS = flat() ? 1 : 3;
  const lanes = buckets.map(() => Array.from({ length: BANDS }, () => []));
  const near = hover >= 0 ? hover : picked;
  const [spanNear, spanFar] = view.span;
  const spread = spanFar - spanNear;

  function bandOf(depth) {
    if (BANDS === 1 || spread <= 0) return 0;
    const along = (depth - spanNear) / spread;
    return Math.max(0, Math.min(BANDS - 1, Math.floor(along * BANDS)));
  }

  /** One edge as one segment, or as four when its ends are at very different depths.
   *
   * A long edge spanning a big depth range cannot be drawn with a single sort key without
   * being visibly on the wrong side of something. Splitting it lets each piece sort and
   * fade on its own.
   */
  function lay(lane, ax, ay, az, bx, by, bz) {
    const apart = Math.abs(bz - az);
    if (BANDS === 1 || spread <= 0 || apart < spread * 0.08) {
      lane[bandOf((az + bz) / 2)].push([ax, ay, bx, by]);
      return;
    }
    const pieces = 4;
    for (let step = 0; step < pieces; step++) {
      const t0 = step / pieces, t1 = (step + 1) / pieces;
      lane[bandOf(az + (bz - az) * (t0 + t1) / 2)].push([
        ax + (bx - ax) * t0, ay + (by - ay) * t0,
        ax + (bx - ax) * t1, ay + (by - ay) * t1,
      ]);
    }
  }

  // Every edge on the call stack, as a chain from the entry point to the innermost frame.
  const onStack = new Set();
  if (live) {
    for (let up = 1; up < play.stack.length; up++) {
      onStack.add(`${play.stack[up - 1].node}|${play.stack[up].node}`);
    }
  }

  for (let e = 0; e < E.from.length; e++) {
    const from = E.from[e], to = E.to[e];
    if (!shown[from] || !shown[to]) continue;
    if (!filters.edges.has(edgeType(e))) continue;

    // An edge only draws when both its ends are showing and both are in the band, which
    // is what makes the slab useful: the long inter-module edges are the main occluder
    // and they are also the ones it should hide.
    if (!flat() && (!inSlab(from) || !inSlab(to))) continue;

    const [ax, ay, az] = at(from);
    const [bx, by, bz] = at(to);
    if (offScreen(ax, ay, bx, by)) continue;
    const mx = (ax + bx) / 2, my = (ay + by) / 2, mz = (az + bz) / 2;

    if (e === litEdge || onStack.has(`${from}|${to}`)) {
      lanes[LIT][0].push([ax, ay, bx, by]);
    } else if (live) {
      lay(lanes[TAIL], ax, ay, az, mx, my, mz);
      lay(lanes[HEAD], mx, my, mz, bx, by, bz);
    } else if (near >= 0 && to === near) {
      lay(lanes[INTO], ax, ay, az, bx, by, bz);
    } else if (near >= 0 && from === near) {
      lay(lanes[OUTOF], ax, ay, az, bx, by, bz);
    } else if (INFERRED.has(edgeVia(e))) {
      lay(lanes[GUESS], ax, ay, az, bx, by, bz);
    } else {
      lay(lanes[TAIL], ax, ay, az, mx, my, mz);
      lay(lanes[HEAD], mx, my, mz, bx, by, bz);
    }
  }

  for (let bucket = 0; bucket < buckets.length; bucket++) {
    const spec = buckets[bucket];
    for (let band = 0; band < BANDS; band++) {
      const segments = lanes[bucket][band];
      if (!segments.length) continue;
      // The lit chain never fades: a call stack that dims into the background is not
      // showing you the call stack.
      const fade = bucket === LIT || BANDS === 1
        ? 1
        : FOG_NEAR + (FOG_FAR - FOG_NEAR) * (band / Math.max(1, BANDS - 1));
      ctx.globalAlpha = spec.alpha * fade;
      ctx.strokeStyle = spec.colour;
      ctx.lineWidth = spec.line;
      ctx.setLineDash(spec.dash);
      ctx.beginPath();
      for (const [ax, ay, bx, by] of segments) {
        ctx.moveTo(ax, ay);
        ctx.lineTo(bx, by);
      }
      ctx.stroke();
    }
  }
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;
}

function offScreen(ax, ay, bx, by) {
  return (Math.max(ax, bx) < 0 || Math.min(ax, bx) > width()
          || Math.max(ay, by) < 0 || Math.min(ay, by) > height());
}

function paintNodes(focus, live) {
  // Far first, so a near node covers the one behind it. This is the whole of the depth
  // handling: sprites are billboards, so a painter's pass is not an approximation of
  // anything for them.
  const order = [];
  for (let i = 0; i < COUNT; i++) if (shown[i]) order.push(i);
  if (!flat()) {
    const depth = new Float32Array(COUNT);
    let near = Infinity, far = -Infinity;
    for (const i of order) {
      depth[i] = at(i)[2];
      near = Math.min(near, depth[i]);
      far = Math.max(far, depth[i]);
    }
    view.span = [near, far];
    order.sort((a, b) => depth[b] - depth[a]);
  } else {
    view.span = [0, 0];
  }

  for (const i of order) {
    const [sx, sy, depth] = at(i);
    if (sx < -30 || sy < -30 || sx > width() + 30 || sy > height() + 30) continue;

    let radius = scaledRadius(i);
    const step = Math.round(rank[i] * (RAMP_STEPS - 1));
    let state = "rest";
    if (i === picked) state = "sel";
    else if (N.target[i] === BADGE_TARGET) state = "badge";

    ctx.globalAlpha = focus && !focus.has(i) ? 0.13 : 1;
    if (matched.size && !matched.has(i)) ctx.globalAlpha *= 0.35;
    if (measure.startsWith("cost") && N.cost_conf
        && N.cost_conf[i] < CONFIDENT) ctx.globalAlpha *= 0.45;
    if (untraced(i)) ctx.globalAlpha *= 0.4;
    if (!flat()) {
      ctx.globalAlpha *= fogAt(depth);
      if (!inSlab(i)) ctx.globalAlpha *= 0.08;
    }

    // Firing shows three ways at once: the node swells, the stack draws as a chain, the
    // frames are listed. Anything that returned recently keeps a fading tint, so the walk
    // draws a trail behind it.
    let trailAt = 0;
    if (live) {
      const onStack = live.live.has(i);
      trailAt = live.recent.get(i) || 0;
      if (onStack) {
        ctx.globalAlpha = 1;
        radius *= i === live.now ? 1.6 : 1.25;
        state = "sel";
      } else if (trailAt) {
        ctx.globalAlpha = 0.35 + trailAt * 0.5;
      } else {
        ctx.globalAlpha *= 0.12;
      }
    }

    const sprite = spriteFor(N.kind[i], step, Math.round(radius), state);
    ctx.drawImage(sprite.image, sx - sprite.side / 2, sy - sprite.side / 2,
                  sprite.side, sprite.side);

    if (!flat() && (i === picked || i === hover
                    || (focus && focus.has(i) && focus.size <= 24)
                    || (live && live.live.has(i)))) {
      const [gx, gy] = project(N.x[i], N.y[i], view.ground);
      ctx.globalAlpha = 0.45;
      ctx.strokeStyle = i === picked || (live && live.live.has(i))
        ? theme.accent : theme.dim;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(sx, sy);
      ctx.lineTo(gx, gy);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    if (live && i === live.now) {
      ctx.globalAlpha = 1;
      ctx.strokeStyle = theme.accent;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(sx, sy, radius + 6, 0, Math.PI * 2);
      ctx.stroke();
    }

    if (matched.has(i) || i === hover) {
      ctx.globalAlpha = 1;
      ctx.strokeStyle = theme.accent;
      ctx.lineWidth = 1.8;
      ctx.beginPath();
      ctx.arc(sx, sy, radius + 3.5, 0, Math.PI * 2);
      ctx.stroke();
    }
  }
  ctx.globalAlpha = 1;
}

/** Labels, culled by zoom and decluttered against what is already placed. */
function paintLabels(focus) {
  ctx.font = "400 11px ui-monospace, SFMono-Regular, Menlo, monospace";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";

  const always = new Set();
  if (picked >= 0) for (const i of focusSet() || []) always.add(i);
  if (hover >= 0) always.add(hover);
  for (const i of matched) always.add(i);
  for (const frame of play.stack) always.add(frame.node);

  const order = [];
  for (let i = 0; i < COUNT; i++) {
    if (!shown[i]) continue;
    if (!always.has(i) && camera.scale < LABEL_ZOOM && radiusOf(i) < 9) continue;
    order.push(i);
  }
  // Anything that must be labelled first, then biggest first, so the labels that survive
  // decluttering are the ones worth reading.
  const depthOf = new Float32Array(COUNT);
  if (!flat()) for (const i of order) depthOf[i] = at(i)[2];
  order.sort((a, b) => (always.has(b) ? 1 : 0) - (always.has(a) ? 1 : 0)
                       || (flat() ? 0 : depthOf[a] - depthOf[b])
                       || radiusOf(b) - radiusOf(a));
  // Zoom stops predicting density once the view is tilted, so the budget is a count.
  const budget = flat() ? order.length : Math.max(always.size + 20, 60);
  let drawn = 0;

  const placed = [];
  for (const i of order) {
    const [sx, sy] = at(i);
    if (sx < -60 || sy < -20 || sx > width() + 60 || sy > height() + 20) continue;
    const text = N.name[i];
    const gap = scaledRadius(i) + 4;
    const span = ctx.measureText(text).width;
    const box = [sx + gap, sy - 6, span, 12];
    if (!always.has(i) && placed.some((other) => overlaps(box, other))) continue;
    if (!always.has(i) && drawn >= budget) continue;
    placed.push(box);
    drawn++;

    ctx.globalAlpha = focus && !focus.has(i) ? 0.2 : 1;
    ctx.fillStyle = theme.bg;
    ctx.fillRect(box[0] - 1, box[1], span + 2, 12);
    ctx.fillStyle = always.has(i) ? theme.ink : theme.dim;
    ctx.fillText(text, box[0], sy);
  }
  ctx.globalAlpha = 1;
}

function overlaps(a, b) {
  return !(a[0] + a[2] < b[0] || b[0] + b[2] < a[0]
           || a[1] + a[3] < b[1] || b[1] + b[3] < a[1]);
}

// -- hit testing ------------------------------------------------------------

function nodeAt(sx, sy) {
  let best = -1, closest = Infinity, nearest = Infinity;
  for (let i = 0; i < COUNT; i++) {
    if (!shown[i]) continue;
    if (!flat() && !inSlab(i)) continue;
    const [nx, ny, depth] = at(i);
    const away = Math.hypot(nx - sx, ny - sy);
    if (away > Math.max(4, scaledRadius(i)) + 3) continue;
    // Nearest the camera wins, not nearest the cursor: whatever is drawn on top is what
    // the click should land on.
    if (flat()) {
      if (away < nearest) { nearest = away; best = i; }
    } else if (depth < closest - 0.5
               || (Math.abs(depth - closest) <= 0.5 && away < nearest)) {
      closest = Math.min(closest, depth);
      nearest = away;
      best = i;
    }
  }
  return best;
}

// -- the detail panel -------------------------------------------------------

function element(tag, className, text) {
  const made = document.createElement(tag);
  if (className) made.className = className;
  if (text !== undefined) made.textContent = text;
  return made;
}

function showDetail(i) {
  const panel = $("detail");
  panel.textContent = "";
  if (i < 0) {
    panel.appendChild(element("h2", null, "nothing picked"));
    panel.appendChild(element("p", "empty",
      "Click a node. Hover one to light what reaches it and what it reaches."));
    for (const note of DATA.notes || []) {
      panel.appendChild(element("p", "note", note));
    }
    panel.appendChild(element("h2", null, `hottest by ${labelFor(measure)}`));
    panel.appendChild(hottest());
    return;
  }

  if (trail.length) panel.appendChild(crumbs());
  panel.appendChild(element("div", "title", N.qual[i]));
  if (N.sig[i]) panel.appendChild(element("div", "sig", N.sig[i]));

  const badges = element("div", "badges");
  badges.appendChild(element("span", "tag kind", N.kind[i]));
  const target = targetById.get(N.target[i]);
  if (target) badges.appendChild(element("span", "tag", target.label));
  for (const flag of N.flags[i]) {
    const warn = ["unreset", "unreached", "recursive"].includes(flag);
    badges.appendChild(element("span", `tag${warn ? " warn" : ""}`, flag));
  }
  panel.appendChild(badges);

  if (N.kind[i] !== "external") {
    const module = moduleById.get(N.module[i]);
    const at = `${module ? module.path : ""}:${N.line[i]}`;
    const where = element("div", "where");
    where.appendChild(element("span", null, at));
    const copy = element("button", null, "copy");
    copy.onclick = () => navigator.clipboard && navigator.clipboard.writeText(at);
    where.appendChild(copy);
    panel.appendChild(where);
  }

  const body = DATA.bodies[String(i)];
  if (body && body.doc) panel.appendChild(element("p", "doc", body.doc));
  if (body && body.value) panel.appendChild(element("div", "sig", `= ${body.value}`));

  if (N.cost_conf[i] < CONFIDENT && N.cost[i] > 0) {
    panel.appendChild(element("p", "note",
      `Only ${Math.round(N.cost_conf[i] * 100)}% of this cost came from a priced call; `
      + "the rest is a default. Drawn faded when colouring by cost."));
  }

  if (N.flags[i].includes("unreached")) {
    panel.appendChild(element("p", "note",
      "Nothing in the graph refers to this. That is not proof of dead code: a call "
      + "through a parameter is not something static analysis can follow."));
  }

  panel.appendChild(element("h2", null, "measures"));
  panel.appendChild(measureTable(i));

  if (N.globals_read[i].length || N.globals_written[i].length) {
    panel.appendChild(element("h2", null, "module state it touches"));
    panel.appendChild(element("p", "note",
      "Named, not counted: which state a function is tied to carries more than how "
      + "much of it. Reading a constant is not coupling and is not listed."));
    for (const other of N.globals_written[i]) panel.appendChild(jump(other, "writes"));
    for (const other of N.globals_read[i]) panel.appendChild(jump(other, "reads"));
  }

  if (N.kind[i] === "state") {
    panel.appendChild(element("h2", null, "reset by"));
    if (N.reset_by[i].length) {
      for (const other of N.reset_by[i]) panel.appendChild(jump(other, ""));
    } else {
      panel.appendChild(element("p", "note",
        "Nothing clears this. With more than one writer that is a leak candidate."));
    }
  }

  panel.appendChild(element("h2", null, `reached by (${intoOf[i].length})`));
  panel.appendChild(edgeList(i, intoOf[i], true));
  panel.appendChild(element("h2", null, `reaches (${outOf[i].length})`));
  panel.appendChild(edgeList(i, outOf[i], false));
}

function labelFor(key) {
  const found = MEASURES.find((spec) => spec.key === key);
  return found ? found.label : key;
}

function measureTable(i) {
  const table = element("table");
  const rows = [
    ["uses", N.fan_in[i]],
    ["reaches", N.fan_out[i]],
    ["lines", N.lines[i]],
    ["statements", N.statements[i]],
    ["complexity", N.complexity[i]],
    ["loop depth", N.loop_depth[i]],
    ["allocations", N.allocs[i]],
    ["global state", N.globals[i]],
    ["observed calls", N.traced && N.traced[i] !== null
      ? short(N.traced[i]) : "no run touched it"],
    ["priced work here", `${short(N.cost_self[i])}us`],
    ["reachable work (bound)", `${short(Math.round(N.cost[i] / 100) / 10)}ms`],
    ["of that, priced", `${Math.round(N.cost_conf[i] * 100)}%`],
    ["written / read / deeper", `${N.gw[i]} / ${N.gr[i]} / ${N.gwt[i]}`],
    ["depth in the machine", `${N.flow[i]} of ${FLOW_MAX}`],
    [`${labelFor(measure)} rank`, `${Math.round(rank[i] * 100)}%`],
  ];
  if (["function", "method", "property", "class", "module"].includes(N.kind[i])) {
    rows.splice(rows.length - 1, 0,
      ["depth from an entry point", N.layer[i] < 0 ? "not reached" : N.layer[i]]);
  }
  for (const [name, value] of rows) {
    const row = element("tr");
    row.appendChild(element("th", null, name));
    row.appendChild(element("td", null, String(value)));
    table.appendChild(row);
  }
  return table;
}

/** Callers or callees as rows: hovering one lights exactly that edge, clicking walks to it. */
function edgeList(i, edges, incoming) {
  if (!edges.length) return element("p", "empty", "nothing");
  const box = element("div");
  const seen = new Set();
  for (const e of edges) {
    const other = incoming ? E.from[e] : E.to[e];
    const key = `${other}|${edgeType(e)}|${edgeVia(e)}`;
    if (seen.has(key)) continue;
    seen.add(key);

    const row = element("button", "link");
    row.appendChild(element("span", "name", N.qual[other] || N.name[other]));
    const via = edgeVia(e) === "static"
      ? edgeType(e)
      : `${edgeType(e)} · ${edgeVia(e)}`;
    row.appendChild(element("span",
      `via${INFERRED.has(edgeVia(e)) ? " guess" : ""}`, via));
    row.title = E.label[e] || `${moduleName(other)} · line ${E.line[e]}`;
    row.onmouseenter = () => { litEdge = e; draw(); };
    row.onmouseleave = () => { litEdge = -1; draw(); };
    row.onclick = () => { trail.push(i); select(other, true); };
    box.appendChild(row);
  }
  return box;
}

function jump(other, why) {
  const row = element("button", "link");
  row.appendChild(element("span", "name", N.qual[other]));
  if (why) row.appendChild(element("span", "via", why));
  row.onclick = () => { trail.push(picked); select(other, true); };
  return row;
}

function crumbs() {
  const box = element("div");
  box.id = "crumbs";
  const back = element("button", null, "← back");
  back.onclick = () => {
    const to = trail.pop();
    if (to !== undefined) select(to, false);
  };
  box.appendChild(back);
  for (const i of trail.slice(-3)) {
    const step = element("button", null, N.name[i]);
    step.onclick = () => { trail = trail.slice(0, trail.indexOf(i)); select(i, false); };
    box.appendChild(step);
  }
  return box;
}

function hottest() {
  const live = [];
  for (let i = 0; i < COUNT; i++) {
    if (shown[i] && N.kind[i] !== "external") live.push(i);
  }
  live.sort((a, b) => valueOf(b) - valueOf(a));
  const box = element("div");
  for (const i of live.slice(0, 20)) {
    const row = element("button", "link");
    row.appendChild(element("span", "name", `${moduleName(i)}.${N.name[i]}`));
    row.appendChild(element("span", "via", short(valueOf(i))));
    row.onclick = () => { trail = []; select(i, true); };
    box.appendChild(row);
  }
  return box;
}

function select(i, recentre) {
  picked = i;
  litEdge = -1;
  showDetail(i);
  if (recentre && i >= 0) {
    const set = focusSet();
    if (set && set.size > 2) fit(set);
    else { camera.x = N.x[i]; camera.y = N.y[i]; }
  }
  draw();
}

// -- the filter rail --------------------------------------------------------

function check(label, count, on, onChange) {
  const row = element("label", "check");
  const box = element("input");
  box.type = "checkbox";
  box.checked = on;
  box.onchange = () => { onChange(box.checked); applyFilters(); buildRail(); };
  row.appendChild(box);
  row.appendChild(element("span", null, label));
  if (count !== null) row.appendChild(element("span", "count", String(count)));
  return row;
}

function slider(label, value, most, step, onChange, unit) {
  const box = element("div", "slider");
  const caption = element("div");
  caption.appendChild(element("span", null, `${label} `));
  caption.appendChild(element("b", null, `${value}${unit || ""}`));
  box.appendChild(caption);
  const range = element("input");
  range.type = "range";
  range.min = 0;
  range.max = most;
  range.step = step;
  range.value = value;
  range.onchange = () => { onChange(Number(range.value)); applyFilters(); buildRail(); };
  box.appendChild(range);
  return box;
}

function buildRail() {
  const rail = $("rail");
  rail.textContent = "";

  rail.appendChild(element("h2", null, "target"));
  for (const target of DATA.targets) {
    rail.appendChild(check(target.label, countWhere((i) => N.target[i] === target.id),
      filters.targets.has(target.id), (on) => {
        if (on) filters.targets.add(target.id); else filters.targets.delete(target.id);
      }));
  }

  rail.appendChild(element("h2", null, "what"));
  for (const kind of KIND_ORDER) {
    const count = countWhere((i) => N.kind[i] === kind);
    if (!count) continue;
    rail.appendChild(check(kind, count, filters.kinds.has(kind), (on) => {
      if (on) filters.kinds.add(kind); else filters.kinds.delete(kind);
    }));
  }

  rail.appendChild(element("h2", null, "edges"));
  for (const kind of EDGE_ORDER) {
    let count = 0;
    for (let e = 0; e < E.from.length; e++) if (edgeType(e) === kind) count++;
    if (!count) continue;
    rail.appendChild(check(kind, count, filters.edges.has(kind), (on) => {
      if (on) filters.edges.add(kind); else filters.edges.delete(kind);
    }));
  }

  // The two lists worth acting on, and the one worth checking a rule against.
  rail.appendChild(element("h2", null, "only"));
  const only = [
    ["unreferenced", "nothing refers to it",
     "Not proof of dead code: a call through a parameter cannot be followed."],
    ["unreset", "state with no reset hook",
     "More than one writer, and nothing clears it."],
    ["inferred", "reached by a rule, not a call",
     "Everything here is drawn dashed. This is where the graph is inferring."],
  ];
  for (const [key, label, why] of only) {
    rail.appendChild(check(label, countOnly(key), filters.only === key, (on) => {
      filters.only = on ? key : null;
    }));
    if (filters.only === key) rail.appendChild(element("p", "note", why));
  }

  // Two percentile floors on two different measures. The pair worth reaching for is hot
  // and complex: something called constantly that is also knotty is where optimising pays,
  // and either measure on its own turns up plenty that is neither.
  rail.appendChild(element("h2", null, "thresholds"));
  rail.appendChild(slider(`${labelFor(measure)} at least`,
    Math.round(filters.minRank * 100), 95, 5,
    (value) => { filters.minRank = value / 100; }, "%"));

  const also = element("div", "slider");
  const pick = element("select");
  pick.id = "also";
  for (const spec of MEASURES) {
    if (!N[spec.key] || spec.key === measure) continue;
    const option = element("option", null, spec.label);
    option.value = spec.key;
    pick.appendChild(option);
  }
  if ([...pick.options].some((o) => o.value === filters.also)) pick.value = filters.also;
  else if (pick.options.length) filters.also = pick.value;
  pick.onchange = () => { filters.also = pick.value; applyFilters(); buildRail(); };
  const caption = element("div");
  caption.appendChild(element("span", null, "and also "));
  caption.appendChild(pick);
  also.appendChild(caption);
  rail.appendChild(also);
  rail.appendChild(slider("at least", Math.round(filters.alsoRank * 100), 95, 5,
    (value) => { filters.alsoRank = value / 100; }, "%"));

  if (filters.minRank > 0 && filters.alsoRank > 0) {
    let live = 0;
    for (let i = 0; i < COUNT; i++) live += shown[i];
    rail.appendChild(element("p", "note",
      `${live} are in the top ${100 - Math.round(filters.minRank * 100)}% by `
      + `${labelFor(measure)} and the top `
      + `${100 - Math.round(filters.alsoRank * 100)}% by ${labelFor(filters.also)}.`));
  }

  rail.appendChild(slider("uses at least", filters.minFanIn, 20, 1,
    (value) => { filters.minFanIn = value; }));

  rail.appendChild(element("h2", null, "modules"));
  const toggle = element("button", null, filters.modules.size ? "hide all" : "show all");
  toggle.onclick = () => {
    if (filters.modules.size) filters.modules.clear();
    else for (const module of DATA.modules) filters.modules.add(module.id);
    applyFilters();
    buildRail();
  };
  rail.appendChild(toggle);
  for (const module of DATA.modules) {
    if (!filters.targets.has(module.target)) continue;
    rail.appendChild(check(module.name, countWhere((i) => N.module[i] === module.id),
      filters.modules.has(module.id), (on) => {
        if (on) filters.modules.add(module.id); else filters.modules.delete(module.id);
      }));
  }
}

function countWhere(test) {
  let count = 0;
  for (let i = 0; i < COUNT; i++) if (test(i)) count++;
  return count;
}

function countOnly(key) {
  if (key === "inferred") {
    return countWhere((i) =>
      outOf[i].concat(intoOf[i]).some((e) => INFERRED.has(edgeVia(e))));
  }
  const flag = key === "unreferenced" ? "unreached" : "unreset";
  return countWhere((i) => N.flags[i].includes(flag));
}

// -- the timeline -----------------------------------------------------------

function scene() {
  return (DATA.traces || [])[play.scene] || null;
}

/** Replay to a point, keeping the call stack and a short memory of what just returned.
 *
 * Replayed from the start each time, never stepped, which is both simpler and fast
 * enough: even the longest recording here is tens of thousands of events, and scrubbing
 * has to land anywhere anyway.
 */
function seek(at) {
  const timeline = scene();
  if (!timeline) return;
  play.at = Math.max(0, Math.min(timeline.events.length - 1, at));

  const stack = [];
  const recent = new Map();
  for (let i = 0; i <= play.at; i++) {
    const [, kind, node] = timeline.events[i];
    if (kind === 0) {
      stack.push({ node, at: i, turns: timeline.events[i][4] || 1 });
      recent.delete(node);
    } else {
      for (let up = stack.length - 1; up >= 0; up--) {
        if (stack[up].node === node) { stack.length = up; break; }
      }
      recent.set(node, i);
    }
  }
  play.stack = stack;
  play.recent = new Map();
  for (const [node, when] of recent) {
    const age = play.at - when;
    if (age <= TRAIL_LENGTH) play.recent.set(node, 1 - age / TRAIL_LENGTH);
  }
  followDepth();
  showStack();
  showProgress();
  draw();
}

/** Ease the shown band toward the innermost frame, with the camera held still.
 *
 * Not a camera follow. At sixteen to two hundred and fifty-six calls a second that is
 * nauseating, discarding the mental map the fixed layout exists to give you. This moves
 * the working depth into view, leaving the world where it is.
 */
function followDepth() {
  if (!play.follow || flat() || !play.stack.length) return;
  const innermost = play.stack[play.stack.length - 1].node;
  const want = Math.min(1, view.height[innermost] + 0.12);
  view.slabHigh += (want - view.slabHigh) * 0.25;
  syncView();
}

function showStack() {
  const box = $("stack");
  const timeline = scene();
  if (!timeline || !play.stack.length) { box.classList.remove("on"); return; }
  box.classList.add("on");
  box.textContent = "";
  // Innermost last, like a debugger: this is the part you actually read.
  for (const frame of play.stack.slice(-14)) {
    const row = element("div", frame === play.stack[play.stack.length - 1] ? "now" : "deep");
    row.appendChild(element("span", null, N.qual[frame.node] || N.name[frame.node]));
    if (frame.turns > 1) row.appendChild(element("span", "turns", ` x${frame.turns}`));
    box.appendChild(row);
  }
}

function showProgress() {
  const timeline = scene();
  if (!timeline) { $("progress").textContent = ""; return; }
  const total = timeline.events.length;
  const event = timeline.events[play.at] || [];
  const when = timeline.unit === "us"
    ? `${(event[0] / 1000).toFixed(1)}ms`
    : `step ${event[0]}`;
  const readout = $("progress");
  readout.textContent = "";
  readout.appendChild(element("b", null, `${play.at + 1}`));
  readout.appendChild(element("span", null, ` / ${total} · ${when}`));
  $("scrub").value = String(play.at);
}

function pickScene(index) {
  play.scene = index;
  play.running = false;
  $("go").textContent = "▶";
  $("scene").value = String(index);
  const timeline = scene();
  const badge = $("kind");
  if (!timeline) {
    badge.textContent = "";
    $("scrub").max = "0";
    $("stack").classList.remove("on");
    draw();
    return;
  }
  $("scrub").max = String(Math.max(0, timeline.events.length - 1));
  badge.textContent = timeline.kind === "trace"
    ? "recorded, times relative"
    : "a possible order, not an observed one";
  badge.className = timeline.kind === "trace" ? "real" : "";
  badge.title = timeline.kind === "trace"
    ? `${timeline.subject} under ${timeline.under}. ${timeline.overhead}`
    : "Both arms of every branch are walked, in source order. Nothing was run.";
  seek(0);
}

function tick(now) {
  if (play.running) {
    const timeline = scene();
    if (!timeline) {
      play.running = false;
    } else {
      const step = Math.max(1, Math.round(play.speed * (now - play.since) / 1000));
      play.since = now;
      if (play.at + step >= timeline.events.length - 1) {
        seek(timeline.events.length - 1);
        play.running = false;
        $("go").textContent = "▶";
      } else {
        seek(play.at + step);
      }
    }
  }
  requestAnimationFrame(tick);
}

function buildTransport() {
  const scenes = $("scene");
  const timelines = DATA.traces || [];
  if (!timelines.length) {
    $("play").style.display = "none";
    return;
  }
  const none = element("option", null, "no timeline");
  none.value = "-1";
  scenes.appendChild(none);
  timelines.forEach((timeline, index) => {
    const option = element("option", null,
      `${timeline.name}${timeline.kind === "trace" ? " (recorded)" : ""}`);
    option.value = String(index);
    scenes.appendChild(option);
  });
  scenes.onchange = () => pickScene(Number(scenes.value));

  const speeds = $("speed");
  for (const rate of SPEEDS) {
    const option = element("option", null, `${rate}/s`);
    option.value = String(rate);
    speeds.appendChild(option);
  }
  speeds.value = String(play.speed);
  speeds.onchange = () => { play.speed = Number(speeds.value); };

  $("go").onclick = () => {
    if (play.scene < 0) return;
    play.running = !play.running;
    play.since = performance.now();
    $("go").textContent = play.running ? "⏸" : "▶";
  };
  $("follow").onchange = (event) => {
    play.follow = event.target.checked;
    if (!play.follow) { view.slabHigh = 1; syncView(); draw(); }
  };
  $("back").onclick = () => seek(play.at - 1);
  $("fwd").onclick = () => seek(play.at + 1);
  $("rewind").onclick = () => seek(0);
  $("scrub").oninput = (event) => { play.running = false; seek(Number(event.target.value)); };
  requestAnimationFrame(tick);
}

/** What is firing right now, and what just did. */
function firing() {
  if (play.scene < 0 || !play.stack.length) return null;
  const live = new Map();
  for (const frame of play.stack) live.set(frame.node, 1);
  return { live, recent: play.recent, now: play.stack[play.stack.length - 1].node };
}

// -- input ------------------------------------------------------------------

function resize() {
  dpr = Math.min(2, window.devicePixelRatio || 1);
  board.width = Math.round(board.clientWidth * dpr);
  board.height = Math.round(board.clientHeight * dpr);
  draw();
}

let dragging = false;
let orbiting = false;
let last = [0, 0];

board.addEventListener("mousedown", (event) => {
  dragging = true;
  orbiting = event.shiftKey || event.button === 1;
  last = [event.clientX, event.clientY];
  board.classList.add("dragging");
});

board.addEventListener("contextmenu", (event) => event.preventDefault());

/** Point the camera, clamped so it cannot go under the floor or past horizontal. */
function turn(az, tilt) {
  camera.az = ((az % 360) + 360) % 360;
  camera.tilt = Math.max(0, Math.min(TILT_MAX, tilt));
  axes = basis();
  syncView();
}

/** Ease to an angle, showing the mapping change instead of teleporting to it. */
function glideTo(az, tilt, then) {
  const fromAz = camera.az, fromTilt = camera.tilt;
  // The short way round the circle.
  let delta = ((az - fromAz + 540) % 360) - 180;
  const started = performance.now();
  const span = 420;
  function step(now) {
    const along = Math.min(1, (now - started) / span);
    const eased = along < 0.5 ? 2 * along * along : 1 - 2 * (1 - along) * (1 - along);
    turn(fromAz + delta * eased, fromTilt + (tilt - fromTilt) * eased);
    draw();
    if (along < 1) requestAnimationFrame(step);
    else if (then) then();
  }
  requestAnimationFrame(step);
}

window.addEventListener("mouseup", () => {
  dragging = false;
  orbiting = false;
  board.classList.remove("dragging");
});

board.addEventListener("mousemove", (event) => {
  const box = board.getBoundingClientRect();
  if (dragging) {
    const dx = event.clientX - last[0], dy = event.clientY - last[1];
    last = [event.clientX, event.clientY];
    if (orbiting) {
      turn(camera.az + dx * 0.4, camera.tilt - dy * 0.3);
    } else {
      // Panning solves against the ground plane, so drag-follows-cursor stays exact at
      // tilt 0 and stays sensible when tilted.
      const before = toWorld(box.width / 2, box.height / 2);
      const after = toWorld(box.width / 2 - dx, box.height / 2 - dy);
      camera.x += after[0] - before[0];
      camera.y += after[1] - before[1];
    }
    hideTip();
    draw();
    return;
  }
  const found = nodeAt(event.clientX - box.left, event.clientY - box.top);
  if (found !== hover) {
    hover = found;
    draw();
  }
  if (found >= 0) showTip(found, event.clientX - box.left, event.clientY - box.top);
  else hideTip();
});

board.addEventListener("mouseleave", () => { hover = -1; hideTip(); draw(); });

board.addEventListener("click", (event) => {
  const box = board.getBoundingClientRect();
  trail = [];
  select(nodeAt(event.clientX - box.left, event.clientY - box.top), false);
});

board.addEventListener("wheel", (event) => {
  event.preventDefault();
  const box = board.getBoundingClientRect();
  const sx = event.clientX - box.left, sy = event.clientY - box.top;
  const [wx, wy] = toWorld(sx, sy);
  camera.scale = Math.max(0.05, Math.min(6,
    camera.scale * Math.exp(-event.deltaY * 0.0015)));
  // Zoom about the cursor, so whatever is under it stays under it.
  const [ax, ay] = toWorld(sx, sy);
  camera.x += wx - ax;
  camera.y += wy - ay;
  hideTip();
  draw();
}, { passive: false });

function showTip(i, sx, sy) {
  const tip = $("tip");
  tip.textContent = "";
  tip.appendChild(element("b", null, N.qual[i]));
  tip.appendChild(element("i", null,
    `  ${N.kind[i]} · ${moduleName(i)} · ${labelFor(measure)} `
    + (untraced(i) ? "no run touched it"
       : `${short(valueOf(i))} (${Math.round(rank[i] * 100)}%)`)));
  tip.classList.add("on");
  tip.style.left = `${Math.min(sx + 14, board.clientWidth - tip.offsetWidth - 8)}px`;
  tip.style.top = `${Math.max(4, sy - tip.offsetHeight - 12)}px`;
}

function hideTip() { $("tip").classList.remove("on"); }

$("find").addEventListener("input", (event) => {
  const query = event.target.value.trim().toLowerCase();
  matched = new Set();
  if (query.length >= 2) {
    for (let i = 0; i < COUNT; i++) {
      if (shown[i] && N.qual[i].toLowerCase().includes(query)) matched.add(i);
    }
  }
  draw();
});

$("find").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && matched.size) {
    trail = [];
    select([...matched][0], true);
  } else if (event.key === "Escape") {
    event.target.value = "";
    matched = new Set();
    draw();
  }
});

$("fit").addEventListener("click", () => fit(null));

window.addEventListener("keydown", (event) => {
  if (event.target.tagName === "INPUT" || event.target.tagName === "SELECT") return;
  if (event.key === "f") fit(null);
  else if (event.key === "Escape") { trail = []; matched = new Set(); select(-1, false); }
  else if (event.key === "/") { event.preventDefault(); $("find").focus(); }
  else if (event.key === "1") glideTo(0, 0, () => fit(null));
  else if (event.key === "2") glideTo(AZIMUTH_DEFAULT, TILT_DEFAULT, () => fit(null));
  else if (event.key === "3") glideTo(AZIMUTH_DEFAULT, TILT_MAX, () => fit(null));
  else if (event.key === "0") { view.slabLow = 0; view.slabHigh = 1;
                                glideTo(0, 0, () => fit(null)); }
  else if (event.key === "c" && play.stack.length) {
    fit(new Set(play.stack.map((frame) => frame.node)));
  }
  else if (event.key === "[") { turn(camera.az - 15, camera.tilt); draw(); }
  else if (event.key === "]") { turn(camera.az + 15, camera.tilt); draw(); }
  else if (event.key === " ") { event.preventDefault(); $("go").click(); }
  else if (event.key === "ArrowLeft") { event.preventDefault(); seek(play.at - 1); }
  else if (event.key === "ArrowRight") { event.preventDefault(); seek(play.at + 1); }
  else if (event.key === "Home" && play.scene >= 0) seek(0);
});

window.addEventListener("resize", resize);
window.matchMedia("(prefers-color-scheme: dark)")
  .addEventListener("change", () => { pickTheme(); draw(); });

// -- start ------------------------------------------------------------------

function hasTrace() {
  return (DATA.traces || []).length > 0
    && N.traced && N.traced.some((value) => value !== null);
}

function syncView() {
  $("tilt").value = String(Math.round(camera.tilt));
  $("tiltat").textContent = `${Math.round(camera.tilt)}\u00b0`;
  $("flip").classList.toggle("on", camera.tilt > 0);
  $("flip").textContent = camera.tilt > 0 ? "2D" : "3D";
  const cut = Math.round(view.slabHigh * 100);
  $("slab").value = String(cut);
  $("slabat").textContent = cut >= 100 ? "all" : `${cut}%`;
}

function buildDepth() {
  const pick = $("axis");
  for (const spec of MEASURES) {
    if (!N[spec.key]) continue;
    const option = element("option", null, spec.label);
    option.value = spec.key;
    option.title = spec.hint;
    pick.appendChild(option);
  }
  pick.value = view.axis;
  pick.onchange = () => {
    view.axis = pick.value;
    reheight();
    // Raising the axis from flat is what makes it comprehensible: you watch the nodes
    // rise out of a picture you already understand.
    if (camera.tilt === 0) glideTo(AZIMUTH_DEFAULT, TILT_DEFAULT, () => fit(null));
    else draw();
  };

  $("tilt").oninput = (event) => {
    turn(camera.az || AZIMUTH_DEFAULT, Number(event.target.value));
    draw();
  };
  // A dim, never a filter: touching `shown` would re-rank every measure and make every
  // colour in the picture jump as the band is scrubbed.
  $("slab").oninput = (event) => {
    view.slabHigh = Number(event.target.value) / 100;
    syncView();
    draw();
  };
  $("flip").onclick = () => {
    if (camera.tilt > 0) glideTo(0, 0, () => fit(null));
    else glideTo(AZIMUTH_DEFAULT, TILT_DEFAULT, () => fit(null));
  };
  syncView();
}

function buildMeasures() {
  const select = $("hot");
  if (!hasTrace()) measure = "fan_in";
  else measure = "traced";
  for (const spec of MEASURES) {
    if (!N[spec.key]) continue;
    if (spec.key === "traced" && !hasTrace()) continue;
    const option = element("option", null, spec.label);
    option.value = spec.key;
    option.title = spec.hint;
    select.appendChild(option);
  }
  select.value = measure;
  select.onchange = () => {
    measure = select.value;
    rerank();
    sprites = new Map();
    showDetail(picked);
    draw();
  };
}

pickTheme();
buildMeasures();
buildDepth();
buildTransport();
resize();
applyFilters();
buildRail();
fit(null);
showDetail(-1);
document.title = `statsbadge call graph · ${DATA.repo.rev || "working tree"}`;
