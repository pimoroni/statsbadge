#!/usr/bin/env python3
"""Walk the graph from a named starting point, so the calls can be stepped through.

    (imported by tools/callgraph.py, not run directly)

A timeline is a named scenario and not one recording of everything, which is what stops
the dispatch tables ruining it. `choose` pins a lookup to one key. A single walk would
fire all fifteen page renderers; this gives one per page, each a picture of a real
frame.

The events are the same five fields a recording from tools/callgraph_trace.py produces, so
the viewer plays either without knowing which it has. What differs is the meaning, and the
viewer says so: a walk is a possible order, not an observed one. Both arms of every branch
are taken, in source order, because choosing between them is not something the source can
answer.

A loop is one pass with a count on it rather than an unrolled one. `for i in range(16)`
emits its body once carrying `n: 16`, which keeps the event stream small and reads better
than sixteen copies.
"""

# Deep enough for the badge's frame loop, which is about eight from `main` to a primitive.
MAX_DEPTH = 40
MAX_EVENTS = 60_000

# How many turns a loop is taken to run when nothing says otherwise.
UNKNOWN_TRIPS = 8

ENTER, EXIT = 0, 1


def build(graph, targets, config):
    """Every scenario the config asks for, as a timeline the viewer can step."""
    timelines = []
    for scenario in config.get("scenario", ()):
        found = walk(graph, targets, scenario)
        if isinstance(found, str):
            graph.notes.append(found)
            continue
        timelines.append(found)
    return timelines


def walk(graph, targets, scenario):
    entry = find(graph, targets, scenario.get("entry", ""))
    if entry is None:
        return (f"scenario {scenario.get('name')!r} starts at "
                f"{scenario.get('entry')!r}, which is not in the graph")

    chosen = {}
    for spec, key in (scenario.get("choose") or {}).items():
        chosen[find(graph, targets, spec)] = key

    calls = {}
    for edge in graph.edges:
        if edge["type"] in ("call", "instantiate"):
            calls.setdefault(edge["from"], []).append(edge)
    for edges in calls.values():
        edges.sort(key=lambda edge: (edge["line"], edge["to"]))

    events = []
    unpinned = set()
    truncated = descend(graph, calls, entry, chosen, events, [], unpinned)

    notes = []
    for node in sorted(unpinned):
        notes.append(f"{graph.node(node)['qual']} was not pinned, so its first member was "
                     f"taken; add it to this scenario's `choose` to say which")

    return {
        "name": scenario.get("name") or graph.node(entry)["qual"],
        "kind": "static",
        "unit": "step",
        "subject": graph.node(entry)["qual"],
        "under": "a walk of the graph, not a recording",
        "overhead": "",
        "truncated": truncated,
        "dropped": 0,
        "marks": [],
        "notes": notes,
        "events": events,
    }


def find(graph, targets, spec):
    """`pages:render` as a node, in whichever target holds it."""
    module_name, _, qual = spec.partition(":")
    for target in targets:
        if module_name not in target.modules:
            continue
        key = f"{target.name}/{module_name}" + (f".{qual}" if qual else "")
        found = graph.by_key.get(key)
        if found is not None:
            return found
    return None


def descend(graph, calls, node, chosen, events, stack, unpinned):
    """Depth-first from one node, in source order, returning whether it was cut short."""
    if len(events) >= MAX_EVENTS:
        return True
    depth = len(stack)
    events.append([len(events), ENTER, node, stack[-1] if stack else -1, 1])

    if node in stack:
        # Already on the way in, so this is recursion: say so and do not go round again.
        graph.node(node)["flags"].add("recursive")
        events.append([len(events), EXIT, node, stack[-1] if stack else -1, 1])
        return False
    if depth >= MAX_DEPTH:
        events.append([len(events), EXIT, node, stack[-1] if stack else -1, 1])
        return True

    stack.append(node)
    truncated = False
    for edge, turns in sites(graph, calls.get(node, ()), chosen, unpinned):
        if len(events) >= MAX_EVENTS:
            truncated = True
            break
        before = len(events)
        if descend(graph, calls, edge["to"], chosen, events, stack, unpinned):
            truncated = True
        if turns > 1:
            # The whole subtree ran once; the count says how many times it would.
            for event in events[before:]:
                event[4] = turns
    stack.pop()

    events.append([len(events), EXIT, node, stack[-1] if stack else -1, 1])
    return truncated


def sites(graph, edges, chosen, unpinned):
    """One body's call sites in source order, with one callee taken per dispatch site."""
    groups = {}
    order = []
    for edge in edges:
        if edge["via"] in ("static", "handed"):
            order.append(("plain", edge["line"], edge["to"], edge))
            continue
        key = ("dispatch", edge["line"], edge["via"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(edge)

    out = []
    for item in order:
        if isinstance(item, tuple) and item[0] == "plain":
            out.append((item[3], turns_for(item[3])))
            continue
        alternatives = groups[item]
        out.append((pick(graph, alternatives, chosen, unpinned),
                    turns_for(alternatives[0])))
    return [(edge, turns) for edge, turns in out if edge is not None]


def pick(graph, alternatives, chosen, unpinned):
    """Which of a dispatch site's callees this scenario follows."""
    tables = {edge.get("table") for edge in alternatives if edge.get("table")}
    for table in tables:
        wanted = chosen.get(table)
        if wanted is None:
            continue
        for edge in alternatives:
            if edge.get("label") == wanted:
                return edge
    for edge in alternatives:
        for table, wanted in chosen.items():
            if edge.get("label") == wanted and table is not None:
                return edge
    # Nothing pinned it, so take the first in source order and report that it was a choice.
    for edge in alternatives:
        registered = [other for other in graph.edges
                      if other["to"] == edge["to"] and other["type"] == "register"]
        for other in registered:
            unpinned.add(other["from"])
    return alternatives[0]


def turns_for(edge):
    depth = edge.get("loops", 0)
    if depth <= 0:
        return 1
    return UNKNOWN_TRIPS ** min(depth, 2)
