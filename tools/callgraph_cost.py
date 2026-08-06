#!/usr/bin/env python3
"""Price the drawing work each function does, from figures that were actually measured.

    (imported by tools/callgraph.py, not run on its own)

What separates this from an invented complexity score is the price table: the badge's
primitives were timed on the board and written down in DEVELOPMENT.md, so they go in the
config and get read from there. An arc is 1.82ms, a line of live text 1ms, `import machine`
40ms.

Two numbers come out of it, and they mean different things.

`cost_self` is the priced work in one body: `draw.dial`'s two arcs really are 3.6ms, and
that figure is comparable with a stopwatch. This is the one to trust.

`cost` is every priced call reachable from here, which is an **upper bound and not a time**.
A static reading has to count both arms of every branch, and five levels of that compounds:
the dial page comes out at 674ms against a measured 18.2ms. That gap is structural, not a
mis-set weight, and no amount of tuning the table closes it - so the measure is labelled as
a bound rather than dressed up as a prediction. Ranking by how much expensive drawing a
call tree can touch is still worth having; reading it as microseconds is not.

One call site does run one callee, though, so the alternatives at a dispatch site are taken
at their worst rather than summed. Without that, `pages.render` counted all fifteen page
renderers for a frame that draws one.

Every node also carries `cost_conf`, the share of its cost that came from a priced call
rather than a default, and the viewer desaturates anything under half: a figure that is
mostly guesswork must not be allowed to glow red.
"""

# What a call, an allocation and an unknown callee cost when nothing better is known. The
# badge runs MicroPython, where a call is nearer 10us than the 1us CPython manages.
DEFAULTS = {
    "base": 10.0,
    "call": 8.0,
    "alloc": 20.0,
    "unknown": 8.0,
    "statement": 2.0,
    "loop_factor": 8.0,
    "loop_cap": 64.0,
}


def price_graph(graph, targets, config):
    """Fill in cost_self, cost and cost_conf on every node, and return the calibration."""
    weights = {}
    prices = {}
    for target in targets:
        settings = dict(DEFAULTS)
        settings.update(config.get("cost", {}).get("default", {}))
        settings.update(config.get("cost", {}).get(target.name, {}))
        weights[target.index] = settings
        prices[target.index] = dict(config.get("price", {}).get(target.name, {}))

    external_price(graph, prices)
    own_cost(graph, weights)
    spread(graph)
    flag_allocations(graph)
    return calibrate(graph, targets, config)


def settings_for(weights, node):
    return weights.get(node["target"], next(iter(weights.values()), DEFAULTS))


def external_price(graph, prices):
    """Give every external node the price its target's table quotes for it, if any."""
    for node in graph.nodes:
        if node["kind"] != "external":
            continue
        found = None
        for table in prices.values():
            if node["name"] in table:
                found = float(table[node["name"]])
                break
        node["cost_self"] = found if found is not None else 0.0
        node["priced"] = found is not None
        node["cost"] = node["cost_self"]
        node["cost_conf"] = 1.0 if found is not None else 0.0


def own_cost(graph, weights):
    """What a body costs before anything it calls is counted.

    A call inside a loop is counted for as many turns as the loop is estimated to take,
    which is where the loop factor earns its place: the badge's recurring performance
    story is work done per item rather than once.
    """
    calls = {}
    for edge in graph.edges:
        if edge["type"] in ("call", "instantiate"):
            calls.setdefault(edge["from"], []).append(edge)

    for node in graph.nodes:
        if node["kind"] == "external":
            continue
        settings = settings_for(weights, node)
        own = settings["base"] + settings["statement"] * node.get("statements", 0)
        priced = 0.0
        guessed = own

        for _, loops, _what in node.get("alloc_sites", ()):
            own += settings["alloc"] * turns_at(loops, settings)
            guessed += settings["alloc"] * turns_at(loops, settings)

        for edge in calls.get(node["id"], ()):
            callee = graph.node(edge["to"])
            turns = turns_at(edge.get("loops", 0), settings)
            if callee["kind"] == "external":
                if callee.get("priced"):
                    own += callee["cost_self"] * turns
                    priced += callee["cost_self"] * turns
                else:
                    own += settings["unknown"] * turns
                    guessed += settings["unknown"] * turns
            else:
                own += settings["call"] * turns
                guessed += settings["call"] * turns

        node["cost_self"] = round(own, 1)
        node["priced_self"] = priced
        node["guessed_self"] = guessed


def turns_at(depth, settings):
    """How many times a site at that loop depth is taken to run."""
    if depth <= 0:
        return 1.0
    return min(settings["loop_cap"], settings["loop_factor"] ** depth)


def spread(graph):
    """Add each callee's cost to its callers, over the graph with its cycles collapsed.

    Condensed first so recursion terminates: everything in a cycle shares one figure and
    is flagged, since there is no honest way to say how many times round it goes.
    """
    calls = {}
    for edge in graph.edges:
        if edge["type"] in ("call", "instantiate"):
            calls.setdefault(edge["from"], []).append(edge)

    groups, group_of = condense(graph, calls)
    order = topological(groups, group_of, calls)

    group_cost = {}
    group_priced = {}
    for index in order:
        members = groups[index]
        total = sum(graph.node(node).get("cost_self", 0.0) for node in members)
        priced = sum(graph.node(node).get("priced_self", 0.0) for node in members)
        for node in members:
            for cost, part in site_costs(calls.get(node, ()), group_of,
                                         group_cost, group_priced, index):
                total += cost
                priced += part
        group_cost[index] = total
        group_priced[index] = priced

    for index, members in enumerate(groups):
        total = group_cost.get(index, 0.0)
        priced = group_priced.get(index, 0.0)
        for node in members:
            record = graph.node(node)
            if record["kind"] == "external":
                continue
            record["cost"] = round(total, 1)
            record["cost_conf"] = round(priced / total, 3) if total else 0.0
            if len(members) > 1:
                record["flags"].add("recursive")

    for node in graph.nodes:
        node.pop("priced_self", None)
        node.pop("guessed_self", None)


def site_costs(edges, group_of, group_cost, group_priced, here):
    """What each of one body's call sites adds, taking one callee per dispatch site.

    A dispatch site runs exactly one of its alternatives: `pages.render` reaches all
    twelve page renderers plus the three extensions through one `handler(...)`, and a
    frame draws one page. Summing them made the estimate fifteen times too big, and the
    same went for argparse's seven subcommands off one `args.func(args)`. So the
    alternatives at one dispatch site are taken at their worst rather than added up.
    """
    sites = {}
    for edge in edges:
        other = group_of.get(edge["to"])
        if other is None or other == here:
            continue
        cost = group_cost.get(other, 0.0)
        part = group_priced.get(other, 0.0)
        if edge["via"] in ("static", "handed"):
            yield (cost, part)
            continue
        key = (edge["via"], edge["line"])
        worst = sites.get(key)
        if worst is None or cost > worst[0]:
            sites[key] = (cost, part)
    for cost, part in sites.values():
        yield (cost, part)


def condense(graph, calls):
    """Tarjan over the call graph, iterative, returning groups and each node's group."""
    index_of = {}
    low = {}
    on_stack = set()
    stack = []
    groups = []

    for start in range(len(graph.nodes)):
        if start in index_of:
            continue
        counter = len(index_of)
        work = [(start, iter([edge["to"] for edge in calls.get(start, ())]))]
        index_of[start] = low[start] = counter
        stack.append(start)
        on_stack.add(start)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is None:
                work.pop()
                if low[node] == index_of[node]:
                    group = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        group.append(member)
                        if member == node:
                            break
                    groups.append(group)
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
            elif child not in index_of:
                counter = len(index_of)
                index_of[child] = low[child] = counter
                stack.append(child)
                on_stack.add(child)
                work.append((child, iter([e["to"] for e in calls.get(child, ())])))
            elif child in on_stack:
                low[node] = min(low[node], index_of[child])

    group_of = {}
    for index, group in enumerate(groups):
        for node in group:
            group_of[node] = index
    return groups, group_of


def topological(groups, group_of, calls):
    """Callees before callers, over the condensed graph."""
    after = {index: set() for index in range(len(groups))}
    for node, edges in calls.items():
        for edge in edges:
            left, right = group_of.get(node), group_of.get(edge["to"])
            if left is not None and right is not None and left != right:
                after[left].add(right)

    seen = set()
    order = []

    for start in range(len(groups)):
        if start in seen:
            continue
        work = [(start, iter(sorted(after[start])))]
        seen.add(start)
        while work:
            index, children = work[-1]
            child = next(children, None)
            if child is None:
                work.pop()
                order.append(index)
            elif child not in seen:
                seen.add(child)
                work.append((child, iter(sorted(after[child]))))
    return order


def flag_allocations(graph):
    """Name the functions that build something inside a loop.

    DEVELOPMENT.md is emphatic about this being the badge's one recurring cost: a pen
    assignment is 64 bytes and a shape 416, and the world map draws 288 polygons with 24
    pens for exactly that reason. A list of the construction sites inside loops is the
    most directly actionable thing the cost model produces.
    """
    for node in graph.nodes:
        inside = [site for site in node.get("alloc_sites", ()) if site[1] >= 1]
        if inside:
            node["flags"].add("allocates_in_loop")
            node["alloc_in_loop"] = len(inside)
        else:
            node["alloc_in_loop"] = 0


def calibrate(graph, targets, config):
    """Predicted against measured, for the pages whose frame times are written down."""
    lines = []
    for target in targets:
        wanted = config.get("calibration", {}).get(target.name, {})
        for spec, measured in sorted(wanted.items()):
            module_name, _, qual = spec.partition(":")
            key = f"{target.name}/{module_name}.{qual}"
            found = graph.by_key.get(key)
            if found is None:
                lines.append((spec, None, float(measured), None))
                continue
            predicted = graph.node(found)["cost"]
            share = predicted / float(measured) if measured else 0.0
            lines.append((spec, predicted, float(measured), share))
    return lines


def report_calibration(lines):
    """Print the bound against what was measured, and say which way a miss would matter.

    Over is expected and says nothing: both arms of every branch are counted. Under is a
    real finding, because a bound that a real frame exceeds means the price table is
    missing something the page actually does.
    """
    if not lines:
        return None
    print("priced work reachable per page, against measured frame times:")
    under = []
    for spec, predicted, measured, share in lines:
        if predicted is None:
            print(f"  {spec:<20} not in the graph, so nothing to compare")
            continue
        print(f"  {spec:<20} bound {predicted / 1000:7.1f}ms   "
              f"measured {measured / 1000:5.1f}ms   {share:5.1f}x")
        if share < 1:
            under.append(spec)
    print("  (over is expected: a static reading counts both arms of every branch)")
    if under:
        return ("the bound is below what was measured for {}, so the price table is "
                "missing work those pages do".format(", ".join(under)))
    return None
