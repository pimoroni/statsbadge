#!/usr/bin/env python3
"""Read the host and badge code into a graph, and write the viewer that draws it.

    python3 tools/callgraph.py --open

Nothing is executed. The badge modules cannot be imported on this host and the ones that
can would need a rasteriser to draw a frame, so the graph is what the source says and
every edge carries `via` naming the rule that found it - `static` for a plain call,
`table` for one through a dispatch dict, `hint` for one written down by hand. Real call
counts arrive separately, from tools/callgraph_trace.py, and merge into the same fields.

Reads tools/callgraph.toml for the targets. Pointed at a bare directory instead it assumes
CPython and package-relative imports, which is enough to draw any Python project.

The viewer opens flat. Tilting it stands the graph up, and height means depth in the
machine: entry points at the top, shape.arc and screen.blit on the floor. Any measure can
drive the axis, but the default is a level assigned so every edge descends - see
`assign_flow` in callgraph_resolve.py for why that is not the obvious breadth-first
answer. The payoff is the timeline, where a call chain that only ever goes down is a
picture of a stack that looks like a stack.

Drag pans and shift-drag orbits; 1 and 2 snap to plan and three-quarter views, 0 resets.
Tilt is clamped to 70 degrees, and the band slider shows one slice of the axis at a time.
A line drops to the floor from whatever is selected, hovered or on the call stack.

The projection is axonometric rather than perspective, so tilt zero is exactly the flat
picture rather than approximately - asserted, not eyeballed. That and the rest of the
drawing live in callgraph_web/callgraph.js, each decision commented where it is made.
"""

import argparse
import fnmatch
import json
import pathlib
import subprocess
import sys
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import callgraph_cost  # noqa: E402
import callgraph_layout  # noqa: E402
import callgraph_resolve  # noqa: E402
import callgraph_scan  # noqa: E402
import callgraph_walk  # noqa: E402
from callgraph_palette import palettes  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "callgraph.toml"
VIEWER = HERE / "callgraph_web"

# What the graph would have to be missing for the output to be misleading rather than
# merely incomplete. A flat target that resolves fewer than this many of its bare imports
# has almost certainly been pointed at the wrong directory.
MIN_BARE_RESOLVED = 0.9


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Extract a call graph and write the viewer for it.")
    parser.add_argument("tree", nargs="?",
                        help="a directory to graph instead of the configured targets")
    parser.add_argument("--config", type=pathlib.Path, default=DEFAULT_CONFIG,
                        help=f"targets to read (default {DEFAULT_CONFIG.name})")
    parser.add_argument("--out", type=pathlib.Path,
                        help="where to write, overriding the config")
    parser.add_argument("--trace", action="append", default=[], type=pathlib.Path,
                        help="a recording from callgraph_trace.py, repeatable")
    parser.add_argument("--open", action="store_true", dest="open_it",
                        help="open the viewer when it is written")
    parser.add_argument("--self-check", action="store_true",
                        help="assert the graph found what it is known to contain")
    args = parser.parse_args(argv)

    config = one_tree(args.tree) if args.tree else read_config(args.config)
    if isinstance(config, str):
        return config

    targets, faults = build_targets(config)
    for fault in faults:
        print(f"  {fault}", file=sys.stderr)
    if not any(target.modules for target in targets):
        return "nothing to graph: no Python found under any target root"

    resolver = callgraph_resolve.Resolver(
        targets,
        hints=config.get("hint", ()),
        entry_point_globs=config.get("entry_points", ()),
    )
    graph = resolver.run()

    fault = report(graph, targets)
    if fault:
        return fault

    traces = load_traces(args.trace, graph)
    traces = callgraph_walk.build(graph, targets, config) + traces

    lines = callgraph_cost.price_graph(graph, targets, config)
    fault = callgraph_cost.report_calibration(lines)
    if fault:
        print(f"  {fault}", file=sys.stderr)

    callgraph_layout.place(graph)
    for note in graph.notes:
        print(f"  note: {note}")

    out = args.out or pathlib.Path(config.get("out", "build/callgraph"))
    out.mkdir(parents=True, exist_ok=True)
    payload = assemble(graph, targets, config)
    payload["traces"] = traces
    write(out, payload, graph)

    if args.self_check:
        missing = self_check(graph)
        if missing:
            for line in missing:
                print(f"  {line}", file=sys.stderr)
            return f"{len(missing)} of the graph's known contents are missing"
        print("self-check: every known edge is present")

    if args.open_it:
        open_viewer(out / "index.html")
    return 0


# -- configuration ----------------------------------------------------------


def read_config(path):
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        return f"cannot read {path}: {exc.strerror}"
    except tomllib.TOMLDecodeError as exc:
        return f"{path}: {exc}"


def one_tree(tree):
    """The zero-config target: assume CPython, and take every entry point on offer."""
    return {
        "out": "build/callgraph",
        "target": [{
            "name": pathlib.Path(tree).name or "tree",
            "label": str(tree),
            "lang": "cpython",
            "roots": [str(tree)],
        }],
    }


def build_targets(config):
    targets = []
    faults = []
    for index, declared in enumerate(config.get("target", ())):
        modules = {}
        for root in roots_of(declared):
            paths = sources_under(root, declared.get("exclude", ()))
            found, trouble = callgraph_scan.scan_tree(
                paths, index, bool(declared.get("flat")), root)
            faults.extend(trouble)
            modules.update(found)
        target = callgraph_resolve.Target(index, declared, modules)
        if declared.get("builtins_from"):
            target.injected = callgraph_resolve.injected_names(
                declared["builtins_from"])
        targets.append(target)
    return targets, faults


def roots_of(declared):
    """Every directory a target's roots name, globs expanded.

    Each root names its own modules, which is what makes an extension's host module
    `statsbadge_clock` and not `extensions.statsbadge-clock.src.statsbadge_clock` - the
    name its own entry point advertises it under.
    """
    found = []
    for pattern in declared.get("roots", ()):
        if any(char in pattern for char in "*?["):
            found.extend(sorted(path for path in pathlib.Path().glob(pattern)
                                if path.is_dir()))
        else:
            path = pathlib.Path(pattern)
            if path.is_dir():
                found.append(path)
    return found


def sources_under(root, excludes):
    """Every .py under a root that no exclude pattern matches.

    fnmatch against the posix path rather than `PurePath.match`, whose `**` matches one
    segment only - which quietly let `badge_app/mpy` in, and that directory holds a
    byte-identical copy of the app.
    """
    paths = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        where = path.as_posix()
        if any(fnmatch.fnmatch(where, pattern) for pattern in excludes):
            continue
        paths.append(path)
    return paths


# -- reporting --------------------------------------------------------------


def report(graph, targets):
    """Say what resolved, and refuse to write a graph that is quietly missing its edges."""
    print(f"{len(graph.nodes)} nodes, {len(graph.edges)} edges, "
          f"{len(graph.modules)} modules")
    for target in targets:
        count = sum(1 for node in graph.nodes if node["target"] == target.index)
        line = f"  {target.name:<8} {count:>5} nodes"
        if target.bare_imports:
            share = target.bare_resolved / target.bare_imports
            line += (f", bare imports {target.bare_resolved}/"
                     f"{target.bare_imports} ({share:.0%})")
        print(line)
        for missing in target.bare_missing[:5]:
            print(f"      unresolved: {missing}")

    for target in targets:
        # Only a flat target can fail this way. There, a bare import names a sibling file
        # and every intra-target edge depends on it; in a package target a bare import is
        # a third-party dependency and not resolving is the right answer.
        if not target.flat or not target.bare_imports:
            continue
        share = target.bare_resolved / target.bare_imports
        if share < MIN_BARE_RESOLVED:
            return (f"target {target.name!r} resolved only {share:.0%} of its bare "
                    f"imports, so most of its edges would be missing with nothing to "
                    f"say so - check its roots and `flat`")
    return None


def self_check(graph):
    """The graph's known contents, from reading this codebase before writing the tool."""
    missing = []

    def node(key):
        return graph.by_key.get(key)

    def edges_from(key, kind=None, via=None):
        source = node(key)
        if source is None:
            return []
        return [edge for edge in graph.edges
                if edge["from"] == source
                and (kind is None or edge["type"] == kind)
                and (via is None or edge["via"] == via)]

    def want(condition, description):
        if not condition:
            missing.append(description)

    dispatched = edges_from("badge/pages.render", via="table")
    want(len(dispatched) >= 15,
         f"pages.render should dispatch to 12 renderers plus 3 extensions, got "
         f"{len(dispatched)}")

    want(len(edges_from("badge/pages.EXTRA", "register")) == 3,
         "all three extension renderers should register into pages.EXTRA")

    want(len(edges_from("host/statsbadge.__main__.main", via="argparse")) == 7,
         "main() should reach all seven cmd_* subcommands via argparse")

    source = node("host/statsbadge.sources.base.Source")
    subclasses = [edge for edge in graph.edges
                  if edge["to"] == source and edge["type"] == "inherit"]
    want(len(subclasses) == 10,
         f"Source should have 10 subclasses - 7 here and 3 from extensions - "
         f"got {len(subclasses)}")

    want(len(edges_from("host/statsbadge.sources.discover", via="table")) >= 9,
         "discover() should reach every candidate source through its own list")

    setup_calls = [edge for edge in edges_from("badge/__init__.main")
                   if graph.nodes[edge["to"]]["key"].startswith("badge/setup.")]
    want(setup_calls, "main() should reach setup.* through pairing_ui()'s returned module")

    dial = graph.nodes[node("badge/look.DIAL_C")] if node("badge/look.DIAL_C") else None
    want(dial and "derived" in dial["flags"],
         "look.DIAL_C should be flagged derived")
    readers = [graph.nodes[edge["from"]]["key"] for edge in graph.edges
               if node("badge/look.DIAL_C") is not None
               and edge["to"] == node("badge/look.DIAL_C") and edge["type"] == "read"]
    want(any("clockface" in key or "issmap" in key for key in readers),
         "an extension should read look.DIAL_C across a package boundary")

    clear = graph.nodes[node("badge/draw.clear_cache")] \
        if node("badge/draw.clear_cache") else None
    want(clear and "reset_hook" in clear["flags"],
         "draw.clear_cache should be flagged reset_hook")

    for name in ("_labels", "_readings", "_gradients"):
        found = node(f"badge/draw.{name}")
        want(found is not None and graph.nodes[found]["kind"] == "state",
             f"draw.{name} should be state, not a constant")

    want(not any("mpy" in module["path"].split("/") for module in graph.modules),
         "badge_app/mpy is a build artefact and should contribute nothing")

    # The flow axis is only worth having if the level rises along every edge; a layering
    # that lets a third of them climb back up is a hairball with extra steps.
    flowing = ("call", "instantiate", "read", "register", "tag")
    climbing = [edge for edge in graph.edges
                if edge["type"] in flowing
                and graph.nodes[edge["to"]]["flow"] < graph.nodes[edge["from"]]["flow"]]
    want(not climbing,
         f"{len(climbing)} flow edges point back up the way; the layering is not monotone")
    want(all(node.get("flow") is not None for node in graph.nodes),
         "every node needs a flow level, including the ones nothing reaches")

    # An external belongs to no target. Filling that in with zero put every firmware
    # builtin in the host target and inflated its count by 247.
    stray = [node["qual"] for node in graph.nodes
             if node["kind"] == "external" and node["target"] is not None]
    want(not stray, f"{len(stray)} external nodes carry a target they cannot belong to")

    return missing


# -- traces -----------------------------------------------------------------


def load_traces(paths, graph):
    """Merge each recording onto the graph, and hand back the timelines.

    Matched on (file, first line), which is exactly what the static pass already recorded
    per node, so there is no name-mangling to get wrong. An edge the run took that no rule
    found is kept and marked: those are the dispatches and the callbacks, and seeing one
    appear is the best reason to record anything at all.
    """
    where = {}
    for node in graph.nodes:
        if node["kind"] == "external" or not node["file"]:
            continue
        where[(str(pathlib.Path(node["file"]).resolve()), node["line"])] = node["id"]

    for node in graph.nodes:
        node["traced"] = None

    timelines = []
    for path in paths:
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            graph.notes.append(f"{path}: {exc}")
            continue
        timelines.append(merge_trace(payload, graph, where, path))
    return [line for line in timelines if line]


def merge_trace(payload, graph, where, path):
    landed = missed = 0
    for file, line, _name, count in payload.get("calls", ()):
        found = where.get((str(pathlib.Path(file).resolve()), line))
        if found is None:
            missed += 1
            continue
        landed += 1
        node = graph.node(found)
        node["traced"] = (node["traced"] or 0) + count

    fresh = 0
    for record in payload.get("edges", ()):
        left = where.get((str(pathlib.Path(record[0]).resolve()), record[1]))
        right = where.get((str(pathlib.Path(record[3]).resolve()), record[4]))
        if left is None or right is None:
            continue
        already = any(edge["from"] == left and edge["to"] == right
                      and edge["type"] in ("call", "instantiate")
                      for edge in graph.edges)
        if not already:
            graph.add_edge(left, right, "call", via="trace", label="seen at runtime")
            graph.node(right)["flags"].add("runtime_only")
            fresh += 1

    events = []
    for when, kind, file, line, _name in payload.get("events", ()):
        found = where.get((str(pathlib.Path(file).resolve()), line))
        if found is not None:
            events.append([when, kind, found])

    print(f"{path.name}: {landed} functions matched, {missed} outside the graph, "
          f"{fresh} edges no rule found, {len(events)} events")
    if fresh:
        print(f"  those {fresh} are drawn dashed and filterable as runtime-only - each is "
              f"a call a rule missed")

    return {
        "name": payload.get("name") or path.stem,
        "kind": "trace",
        "unit": payload.get("unit", "us"),
        "subject": payload.get("subject", ""),
        "under": payload.get("under", ""),
        "overhead": payload.get("overhead", ""),
        "dropped": payload.get("dropped_events", 0),
        "marks": payload.get("marks", []),
        "events": events,
    }


# -- output -----------------------------------------------------------------


def assemble(graph, targets, config):
    """The graph as the parallel arrays the viewer reads."""
    fields = ["name", "qual", "kind", "module", "target", "line", "endline", "sig",
              "fan_in", "fan_out", "complexity", "statements", "loop_depth", "allocs",
              "globals", "gw", "gr", "gwt", "layer", "lines", "x", "y",
              "cost", "cost_self", "cost_conf", "alloc_in_loop", "traced", "flow"]
    nodes = {field: [] for field in fields}
    nodes["flags"] = []
    nodes["globals_read"] = []
    nodes["globals_written"] = []
    nodes["reset_by"] = []

    for node in graph.nodes:
        for field in fields:
            value = node.get(field)
            if field == "complexity":
                value = 1 + node.get("branches", 0)
            if field in NULLABLE:
                nodes[field].append(value)
                continue
            nodes[field].append(default_for(field) if value is None else value)
        nodes["flags"].append(sorted(node["flags"]))
        nodes["globals_read"].append(list(node.get("globals_read") or ()))
        nodes["globals_written"].append(list(node.get("globals_written") or ()))
        nodes["reset_by"].append(list(node.get("reset_by") or ()))

    edges = {"from": [], "to": [], "type": [], "via": [], "line": [], "label": []}
    kinds, vias = [], []
    for edge in graph.edges:
        edges["from"].append(edge["from"])
        edges["to"].append(edge["to"])
        edges["type"].append(index_in(kinds, edge["type"]))
        edges["via"].append(index_in(vias, edge["via"]))
        edges["line"].append(edge["line"])
        edges["label"].append(edge["label"] or "")

    bodies = {}
    for node in graph.nodes:
        if node["doc"]:
            bodies[str(node["id"])] = {"doc": node["doc"],
                                       "value": node.get("value", "")}
        elif node.get("value"):
            bodies[str(node["id"])] = {"doc": "", "value": node["value"]}

    return {
        "version": 1,
        "repo": revision(),
        "config": {"targets": [target.name for target in targets],
                   "out": config.get("out")},
        "edge_types": kinds,
        "via": vias,
        "palettes": palettes(),
        "targets": [{"id": target.index, "name": target.name, "label": target.label,
                     "lang": target.config.get("lang", "cpython")}
                    for target in targets],
        "modules": [{"id": module["id"], "name": module["name"],
                     "path": module["path"], "target": module["target"],
                     "lines": module["lines"], "doc": module["doc"],
                     "box": module.get("box", [0, 0, 0, 0]),
                     "layer": module.get("layer", 0)}
                    for module in graph.modules],
        "nodes": nodes,
        "edges": edges,
        "bodies": bodies,
        "traces": [],
        "notes": graph.notes,
    }


# Fields where nothing is a real answer, and zero would be a wrong one. An external node
# belongs to no target and no module, and a node no run touched has no count - filling any
# of those with 0 put every firmware builtin in the host target and made the rail say the
# host held 705 nodes when it holds 458.
NULLABLE = frozenset({"target", "module", "traced"})


def default_for(field):
    return "" if field in ("name", "qual", "kind", "sig") else 0


def index_in(table, value):
    if value not in table:
        table.append(value)
    return table.index(value)


def revision():
    """The commit the graph was read at, so two runs at one revision agree exactly."""
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return {"rev": "", "dirty": False}
    return {"rev": rev, "dirty": bool(dirty)}


def write(out, payload, graph):
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    (out / "graph.json").write_text(json.dumps(payload, indent=1, sort_keys=True))

    template = (VIEWER / "callgraph.html").read_text()
    script = (VIEWER / "callgraph.js").read_text()
    # A JSON script block, so the only thing needing an escape is a literal </script.
    page = template.replace(
        "/*__GRAPH__*/", data.replace("</script", "<\\/script")).replace(
        "/*__VIEWER__*/", script)
    (out / "index.html").write_text(page)

    size = len(page) / 1024
    print(f"wrote {out}/graph.json and {out}/index.html ({size:.0f}KB, "
          f"{len(graph.nodes)} nodes)")


def open_viewer(path):
    opener = {"darwin": "open", "win32": "start"}.get(sys.platform, "xdg-open")
    try:
        subprocess.run([opener, str(path)], check=False)
    except OSError as exc:
        print(f"  could not open {path}: {exc.strerror}")


if __name__ == "__main__":
    sys.exit(main())
