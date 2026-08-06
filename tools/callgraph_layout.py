#!/usr/bin/env python3
"""Give every node a place, the same place every time.

    (imported by tools/callgraph.py, not run on its own)

Laid out here rather than in the browser because a picture that settles differently on
every load never becomes a map of anything: the point is to be able to look twice and
recognise where you are, and to diff two revisions and see what moved. So the seed is
fixed, the iteration count is fixed, and the same graph gives the same coordinates.

Two levels, because the module is the unit the code is thought about in. Modules are
layered by what imports what, then each one's members are relaxed inside its own box.
"""

import math

MODULE_GAP = 90
COLUMN_GAP = 190
MEMBER_GAP = 38
MIN_BOX = 120
# Room for the largest node sprite, which is sized in screen pixels and so
# overhangs a world-space box at a wide zoom.
BOX_PAD = 44
TARGET_GAP = 320
ITERATIONS = 300


def place(graph):
    """Fill in x/y on every node and a box on every module."""
    members = {}
    for node in graph.nodes:
        if node["kind"] == "external" or node["module"] is None:
            continue
        if node["kind"] == "module":
            continue
        members.setdefault(node["module"], []).append(node["id"])

    intra, inter = split_edges(graph, members)

    offset_y = 0
    for target in sorted({module["target"] for module in graph.modules}):
        mine = [module for module in graph.modules if module["target"] == target]
        height = lay_out_target(graph, mine, members, intra, inter, offset_y)
        offset_y += height + TARGET_GAP

    # A module's own node sits at the centre of its box, so the import graph reads at the
    # zoomed-out level and the node does not land on the origin with all the others.
    for node in graph.nodes:
        if node["kind"] != "module" or node["module"] is None:
            continue
        box = graph.modules[node["module"]].get("box")
        if box:
            node["x"] = round(box[0] + box[2] / 2, 1)
            node["y"] = round(box[1] + box[3] / 2, 1)

    park_externals(graph)


def split_edges(graph, members):
    """Edges within one module, and the weight of the ties between modules."""
    home = {}
    for module, held in members.items():
        for node in held:
            home[node] = module

    intra = {}
    inter = {}
    for edge in graph.edges:
        source, sink = edge["from"], edge["to"]
        left, right = home.get(source), home.get(sink)
        if left is None or right is None:
            continue
        if left == right:
            intra.setdefault(left, []).append((source, sink))
        else:
            key = (left, right)
            inter[key] = inter.get(key, 0) + 1
    return intra, inter


# -- level one: the modules -------------------------------------------------


def lay_out_target(graph, modules, members, intra, inter, offset_y):
    """Lay out one target's modules in columns, and its members inside them."""
    ids = [module["id"] for module in modules]
    sizes = {}
    for module in modules:
        held = members.get(module["id"], [])
        side = max(MIN_BOX, math.ceil(math.sqrt(max(1, len(held)))) * MEMBER_GAP)
        sizes[module["id"]] = side

    layers = layer_modules(ids, inter)
    columns = order_layers(layers, inter)

    # A layer with a dozen modules in one column makes the whole picture far taller than
    # it is wide, and a graph that has to be fitted at 7% is a graph nobody can read. So a
    # column wraps once it passes a budget taken from the total area, which keeps the
    # block near square while leaving the left-to-right read of what imports what intact.
    area = sum(side * side for side in sizes.values())
    budget = max(MIN_BOX * 3, math.sqrt(area) * 1.6)

    x = 0.0
    tallest = 0.0
    for depth in sorted(columns):
        stacks = wrap(columns[depth], sizes, budget)
        for stack in stacks:
            total = sum(sizes[m] for m in stack) + MODULE_GAP * max(0, len(stack) - 1)
            y = -total / 2.0
            for module_id in stack:
                side = sizes[module_id]
                record = graph.modules[module_id]
                record["box"] = [round(x, 1), round(y + offset_y, 1), side, side]
                record["layer"] = depth
                lay_out_members(graph, members.get(module_id, []),
                                intra.get(module_id, []), x, y + offset_y, side)
                y += side + MODULE_GAP
            tallest = max(tallest, total)
            x += max(sizes[m] for m in stack) + MODULE_GAP
        x += COLUMN_GAP - MODULE_GAP
    return tallest


def wrap(row, sizes, budget):
    """One layer's modules split into as few stacks as keep each under the budget."""
    stacks = [[]]
    height = 0.0
    for module_id in row:
        side = sizes[module_id]
        if stacks[-1] and height + side + MODULE_GAP > budget:
            stacks.append([])
            height = 0.0
        stacks[-1].append(module_id)
        height += side + MODULE_GAP
    return [stack for stack in stacks if stack]


def layer_modules(ids, inter):
    """Each module's depth, by the longest import path that reaches it.

    Import cycles are collapsed first so a cycle cannot make the longest path infinite,
    and every member of one shares a depth.
    """
    forward = {module: set() for module in ids}
    for (left, right), _ in inter.items():
        if left in forward and right in forward:
            forward[left].add(right)

    groups = strong_components(ids, forward)
    group_of = {}
    for index, group in enumerate(groups):
        for module in group:
            group_of[module] = index

    between = {index: set() for index in range(len(groups))}
    for left, rights in forward.items():
        for right in rights:
            if group_of[left] != group_of[right]:
                between[group_of[left]].add(group_of[right])

    depth = {}

    def depth_of(index, seen):
        if index in depth:
            return depth[index]
        if index in seen:
            return 0
        seen = seen | {index}
        # A module that imports nothing sits at the left; one imported by nothing at the
        # right. Reading left to right then follows the direction of dependency.
        below = [depth_of(other, seen) for other in between[index]]
        depth[index] = 1 + max(below) if below else 0
        return depth[index]

    for index in range(len(groups)):
        depth_of(index, frozenset())

    deepest = max(depth.values(), default=0)
    return {module: deepest - depth[group_of[module]] for module in ids}


def strong_components(ids, forward):
    """Tarjan, iterative so a deep import chain cannot exhaust the stack."""
    index_of = {}
    low = {}
    on_stack = set()
    stack = []
    found = []
    counter = [0]

    for start in ids:
        if start in index_of:
            continue
        work = [(start, iter(sorted(forward.get(start, ()))))]
        index_of[start] = low[start] = counter[0]
        counter[0] += 1
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
                    found.append(group)
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
            elif child not in index_of:
                index_of[child] = low[child] = counter[0]
                counter[0] += 1
                stack.append(child)
                on_stack.add(child)
                work.append((child, iter(sorted(forward.get(child, ())))))
            elif child in on_stack:
                low[node] = min(low[node], index_of[child])
    return found


def order_layers(layers, inter):
    """Order each column so the ties between columns cross as little as possible."""
    columns = {}
    for module, depth in sorted(layers.items()):
        columns.setdefault(depth, []).append(module)

    weight = {}
    for (left, right), count in inter.items():
        weight[(left, right)] = count
        weight[(right, left)] = weight.get((right, left), 0) + count

    for _ in range(4):
        for depth in sorted(columns):
            neighbours = columns.get(depth - 1, [])
            if not neighbours:
                continue
            positions = {module: index for index, module in enumerate(neighbours)}

            def centre(module, positions=positions):
                pulls = [(positions[other], weight.get((module, other), 0))
                         for other in positions]
                total = sum(count for _, count in pulls)
                if not total:
                    return len(positions) / 2.0
                return sum(at * count for at, count in pulls) / total

            columns[depth] = sorted(columns[depth],
                                    key=lambda module: (centre(module), module))
    return columns


# -- level two: the members -------------------------------------------------


def lay_out_members(graph, held, edges, box_x, box_y, side):
    """Relax one module's own nodes inside its box.

    Seeded on a spiral in declaration order, so a module with no internal edges still
    comes out readable and in the order it is written.
    """
    if not held:
        return
    if len(held) == 1:
        graph.node(held[0])["x"] = round(box_x + side / 2, 1)
        graph.node(held[0])["y"] = round(box_y + side / 2, 1)
        return

    at = {}
    for index, node in enumerate(held):
        angle = index * 2.399963  # the golden angle, so a spiral never lines up
        radius = (side / 2 - BOX_PAD) * math.sqrt((index + 0.5) / len(held))
        at[node] = [radius * math.cos(angle), radius * math.sin(angle)]

    ideal = (side - 2 * BOX_PAD) / math.sqrt(len(held)) or 1.0
    # Sorted so the order edges were found in cannot change where anything lands.
    pairs = sorted((source, sink) for source, sink in edges
                   if source in at and sink in at)

    for step in range(ITERATIONS):
        cooling = (1.0 - step / ITERATIONS) * ideal * 0.35
        push = {node: [0.0, 0.0] for node in at}

        for one in range(len(held)):
            for two in range(one + 1, len(held)):
                first, second = held[one], held[two]
                dx = at[first][0] - at[second][0]
                dy = at[first][1] - at[second][1]
                distance = math.hypot(dx, dy) or 0.01
                force = (ideal * ideal) / distance
                push[first][0] += dx / distance * force
                push[first][1] += dy / distance * force
                push[second][0] -= dx / distance * force
                push[second][1] -= dy / distance * force

        for source, sink in pairs:
            dx = at[source][0] - at[sink][0]
            dy = at[source][1] - at[sink][1]
            distance = math.hypot(dx, dy) or 0.01
            force = (distance * distance) / ideal
            push[source][0] -= dx / distance * force
            push[source][1] -= dy / distance * force
            push[sink][0] += dx / distance * force
            push[sink][1] += dy / distance * force

        for node, (dx, dy) in push.items():
            distance = math.hypot(dx, dy) or 0.01
            step_x = dx / distance * min(distance, cooling)
            step_y = dy / distance * min(distance, cooling)
            at[node][0] += step_x
            at[node][1] += step_y
            # Kept inside its own box, so a module never overlaps its neighbour.
            limit = side / 2 - BOX_PAD
            reach = math.hypot(at[node][0], at[node][1])
            if reach > limit:
                at[node][0] *= limit / reach
                at[node][1] *= limit / reach

    centre_x = box_x + side / 2
    centre_y = box_y + side / 2
    for node, (dx, dy) in at.items():
        graph.node(node)["x"] = round(centre_x + dx, 1)
        graph.node(node)["y"] = round(centre_y + dy, 1)


def park_externals(graph):
    """Externals in a column of their own, grouped by what provides them.

    They are not part of the codebase, so giving them a place in it would be a lie; but
    seeing how much of a frame is spent in the firmware is the reason they are here.
    """
    edges = 0
    left = min((node["x"] for node in graph.nodes
                if node.get("x") is not None and node["kind"] != "external"), default=0)
    grouped = {}
    for node in graph.nodes:
        if node["kind"] != "external":
            continue
        grouped.setdefault(node.get("provider", "package"), []).append(node["id"])

    y = 0.0
    for provider in sorted(grouped):
        for index, node in enumerate(sorted(grouped[provider])):
            graph.node(node)["x"] = round(left - COLUMN_GAP * 2, 1)
            graph.node(node)["y"] = round(y + index * MEMBER_GAP * 0.7, 1)
            edges += 1
        y += len(grouped[provider]) * MEMBER_GAP * 0.7 + MODULE_GAP

    for node in graph.nodes:
        if node.get("x") is None:
            node["x"] = 0.0
            node["y"] = 0.0
