// Run the badge app's own tests against the real firmware, under the WASM port.
//
//   node tools/wasm/run.mjs                       # all of tests/badge/wasm
//   node tools/wasm/run.mjs test_pages            # one module
//   BADGEWARE_RUNTIME=path/to/micropython.mjs node tools/wasm/run.mjs
//
// Everything under badge_app/ needs `badge`, `screen`, `image` and `tween`, which only the
// firmware has, so on a host it could only ever be read as text. Here it is imported and
// called: the runtime is a batteries-included badgeware-wasm build, which carries the
// badgeware package and the fonts inside the wasm, so the only staging is the app itself.
//
// DEVELOPMENT.md has the download for the runtime.

import { execFileSync, spawn } from "node:child_process"
import { readFileSync, readdirSync, statSync } from "node:fs"
import { Socket } from "node:net"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..")
const RUNTIME = process.env.BADGEWARE_RUNTIME
  || join(ROOT, "build", "badgeware-runtime", "micropython.mjs")

// Where an installed app lives on the badge, so look.APP_DIR is right and the font search
// finds lexend at the first candidate instead of the fallbacks.
const APP_DIR = "/system/apps/stats"
const TEST_DIR = "/tests"
// What this port has nothing behind: socket, wifi, secrets. `select` is not here - the
// runtime carries one, and a shim on sys.path wins over it.
const SHIM_DIR = "/shims"

if (!fileExists(RUNTIME)) {
  console.error(
    `no badgeware runtime at ${RUNTIME}\n\n` +
    "Fetch one:\n" +
    "  gh release download v3.0.1 --repo pimoroni/badgeware-wasm \\\n" +
    "    --pattern 'badgeware-tufty2350-batteries-jspi.zip'\n" +
    "  unzip -q badgeware-tufty2350-batteries-jspi.zip -d build/badgeware-runtime\n\n" +
    "or point BADGEWARE_RUNTIME at a build of your own.")
  process.exit(2)
}

// A jspi runtime suspends through WebAssembly.Suspending; without it emscripten stops
// deep inside the generated module with nothing useful to say.
const backend = /__MICROPYTHON_ASYNC__ = "([a-z-]+)"/.exec(readFileSync(RUNTIME, "utf8"))
if (backend?.[1] === "jspi" && typeof WebAssembly.Suspending !== "function") {
  console.error(`this node (${process.version}) has no JSPI, which this runtime needs. ` +
                "Use node 25 or newer, or an asyncify build.")
  process.exit(2)
}

const { loadMicroPython } = await import(pathToFileURL(RUNTIME).href)

// Headless: the display driver hands its framebuffer to the host on update() and there is
// nowhere for it to go. Without these the first flip calls an undefined global.
globalThis.blitrgba = () => {}
globalThis.backlight = () => {}
globalThis.sim_buttons = () => 0

// The other side of tools/wasm/shims/socket.py. Node holds the connections and fills
// their buffers on its own event loop; the shim asks after them. Bytes cross as base64.
const connections = new Map()
let nextHandle = 1

globalThis.sb_connect = (host, port) => {
  const handle = nextHandle++
  const held = { chunks: [], connected: false, ended: false, failed: false }
  held.sock = new Socket()
  held.sock.on("connect", () => { held.connected = true })
  held.sock.on("data", (chunk) => held.chunks.push(chunk))
  held.sock.on("error", () => { held.failed = true })
  held.sock.on("close", () => { held.ended = true })
  held.sock.connect(port, host)
  connections.set(handle, held)
  return handle
}

// 1 connected, 2 readable, 4 ended, 8 failed. Same numbers as the shim's constants.
globalThis.sb_state = (handle) => {
  const held = connections.get(handle)
  if (!held) return 8
  return (held.connected ? 1 : 0) | (held.chunks.length ? 2 : 0)
       | (held.ended ? 4 : 0) | (held.failed ? 8 : 0)
}

globalThis.sb_send = (handle, base64) => {
  connections.get(handle)?.sock.write(Buffer.from(base64, "base64"))
  return 1
}

globalThis.sb_recv = (handle) => {
  const held = connections.get(handle)
  if (!held?.chunks.length) return ""
  const chunk = Buffer.concat(held.chunks)
  held.chunks = []
  return chunk.toString("base64")
}

globalThis.sb_close = (handle) => {
  const held = connections.get(handle)
  if (held) {
    held.sock.destroy()
    connections.delete(handle)
  }
  return 1
}

const mp = await loadMicroPython({
  stdout: (line) => process.stdout.write(line + "\n"),
  stderr: (line) => process.stderr.write(line + "\n"),
  linebuffer: true,
  heapsize: 128 * 1024 * 1024,
})

stage(join(ROOT, "src", "statsbadge", "badge_app"), APP_DIR, (name) =>
  name !== "mpy" && name !== "__pycache__")
// From the installed extensions, not from extensions/ in the checkout. Printed, or a page
// edited there and not reinstalled is silently the old one.
mkdirp(`${APP_DIR}/ext`)
const staged = extensionModules()
for (const module of staged) {
  copy(module, `${APP_DIR}/ext/${module.split("/").pop()}`)
}
if (staged.length) {
  console.log(`ext: ${staged.length} file(s) from ${dirname(dirname(staged[0]))}`)
}
stage(join(ROOT, "tests", "badge", "wasm"), TEST_DIR, (name) => name !== "__pycache__")
stage(join(ROOT, "tools", "wasm", "shims"), SHIM_DIR, (name) => name !== "__pycache__")

let hostChild = null
const host = await serveHost()
if (host) {
  console.log(`host: statsbadge on 127.0.0.1:${host.port}, badge ${host.badge_id}`)
}

const wanted = process.argv[2]
const modules = mp.FS.readdir(TEST_DIR)
  .filter((name) => name.startsWith("test_") && name.endsWith(".py"))
  .map((name) => name.slice(0, -3))
  .filter((name) => !wanted || name === wanted)
if (!modules.length) {
  console.error(wanted ? `no test module called ${wanted}` : "no test modules found")
  process.exit(2)
}

// await, or a Python exception becomes an unhandled rejection and this reports success.
try {
  await mp.runPython(`
import sys
sys.path.insert(0, "${TEST_DIR}")
sys.path.insert(0, "${APP_DIR}")
sys.path.insert(0, "${APP_DIR}/ext")
sys.path.append("${SHIM_DIR}")

import badgeware                     # badge, screen, image, tween, the buttons
import os
os.chdir("${APP_DIR}")

# Where the real server is, for the tests that talk to one. None if it would not start.
import hostinfo
hostinfo.HOST = ${JSON.stringify(host)}

# What badge_app/app.py does at import, which the tests cannot do for themselves: it
# imports net, and this port has no socket module. Without the mode the screen is LORES
# and half the size look.py lays out for.
badge.mode(HIRES | VSYNC)
screen.antialias = image.X4
badge.default_clear = None

import unittest
ran = failed = skipped = 0
for name in ${JSON.stringify(modules)}:
    print("--", name)
    # MicroPython's unittest.main returns the result; it takes no exit= and raises nothing.
    result = unittest.main(module=name)
    ran += result.testsRun
    failed += result.failuresNum + result.errorsNum
    skipped += result.skippedNum
print("wasm: %d test(s), %d failed, %d skipped" % (ran, failed, skipped))
raise SystemExit(1 if failed else 0)
`)
} catch (error) {
  // SystemExit(0) still arrives here as a thrown PythonError.
  const message = String(error?.message ?? error)
  if (!/SystemExit: 0\b/.test(message)) {
    console.error(message)
    done(1)
  }
}
done(0)

/** The served host holds the event loop open, so leaving is deliberate. */
function done(code) {
  hostChild?.kill()
  process.exit(code)
}

/** A real statsbadge on a loopback port, with one badge paired to it. */
async function serveHost() {
  const child = spawn("uv", ["run", "--no-sync", "python", "tools/wasm/host.py"],
                      { cwd: ROOT, stdio: ["ignore", "pipe", "pipe"] })
  hostChild = child
  const said = await new Promise((done) => {
    let seen = ""
    const giveUp = setTimeout(() => done(null), 20000)
    child.stdout.on("data", (chunk) => {
      seen += chunk
      if (seen.includes("\n")) {
        clearTimeout(giveUp)
        done(seen.split("\n")[0])
      }
    })
    child.on("error", () => { clearTimeout(giveUp); done(null) })
    child.on("exit", () => { clearTimeout(giveUp); done(null) })
  })
  if (!said) {
    console.error("no statsbadge to serve: the tests that need a host will skip")
    child.kill()
    return null
  }
  process.on("exit", () => child.kill())
  return JSON.parse(said)
}

function fileExists(path) {
  try {
    statSync(path)
    return true
  } catch {
    return false
  }
}

/** Copy a host directory into MEMFS, one level of subdirectories deep. */
function stage(from, to, keep) {
  mkdirp(to)
  for (const name of readdirSync(from)) {
    if (keep && !keep(name)) continue
    const path = join(from, name)
    if (statSync(path).isDirectory()) {
      mkdirp(`${to}/${name}`)
      for (const inner of readdirSync(path)) copy(join(path, inner), `${to}/${name}/${inner}`)
      continue
    }
    copy(path, `${to}/${name}`)
  }
}

function copy(from, to) {
  mp.FS.writeFile(to, readFileSync(from))
}

function mkdirp(path) {
  let built = ""
  for (const part of path.split("/").filter(Boolean)) {
    built += `/${part}`
    try {
      mp.FS.mkdir(built)
    } catch {
      // already there
    }
  }
}

/** The badge modules and assets the installed extensions ship, as the installer sees them. */
function extensionModules() {
  const asked = "import json,sys;sys.path.insert(0,'src');"
    + "from statsbadge import extensions;"
    + "print(json.dumps([p for _n,p in extensions.badge_modules(extensions.load())]))"
  try {
    return JSON.parse(execFileSync("uv", ["run", "--no-sync", "python", "-c", asked],
                                   { cwd: ROOT, encoding: "utf8" }).trim())
  } catch {
    console.error("could not ask statsbadge for its extension modules; going without them")
    return []
  }
}
