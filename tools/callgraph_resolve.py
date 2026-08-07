#!/usr/bin/env python3
"""Turn scanned modules into nodes and edges, following the indirect calls too.

    (imported by tools/callgraph.py, never run directly)

The direct edges are the easy half. The indirect half is where the value is: twelve page
renderers reached only through a dict, three extension renderers registered into it from
other packages, and seven CLI subcommands hung off argparse. Left unresolved those all
look like dead code, and `main()` becomes trivial. Each has a rule here, and every edge
carries a `via` for the rule that found it.
"""

import fnmatch
import pathlib
import sys
import tomllib

from callgraph_layout import strong_components
from callgraph_scan import ATTR, CALLED, NAME, OPAQUE, SELF

# Dunder and near-universal method names are never treated as a vtable, or one call to
# `.get()` would join everything to everything.
NEVER_VIRTUAL = frozenset({
    "get", "set", "run", "load", "save", "read", "write", "close", "open", "start",
    "stop", "update", "reset", "clear", "add", "remove", "keys", "values", "items",
    "append", "send", "copy", "name", "value", "step", "show", "draw", "render",
})


class Graph:
    """Nodes and edges, with the lookups the rules need while they are being built."""

    def __init__(self):
        self.nodes = []
        self.edges = []
        self.modules = []
        self.by_key = {}
        self.module_by_key = {}
        self.notes = []

    def add_module(self, key, **fields):
        if key in self.module_by_key:
            return self.module_by_key[key]
        index = len(self.modules)
        record = {"id": index, "key": key}
        record.update(fields)
        self.modules.append(record)
        self.module_by_key[key] = index
        return index

    def add_node(self, key, **fields):
        if key in self.by_key:
            return self.by_key[key]
        index = len(self.nodes)
        record = {
            "id": index, "key": key, "flags": set(),
            "globals_read": set(), "globals_written": set(),
        }
        record.update(fields)
        self.nodes.append(record)
        self.by_key[key] = index
        return index

    def add_edge(self, source, target, kind, via="static", line=-1, label=None,
                 loops=0, table=None):
        if source is None or target is None or source == target:
            return
        self.edges.append({
            "from": source, "to": target, "type": kind, "via": via, "line": line,
            "label": label, "loops": loops, "table": table,
        })

    def node(self, index):
        return self.nodes[index]


class Target:
    """One tree, scanned, plus how names resolve inside it."""

    def __init__(self, index, config, modules):
        self.index = index
        self.name = config["name"]
        self.label = config.get("label", config["name"])
        self.config = config
        self.modules = modules
        self.flat = bool(config.get("flat"))
        self.injected = set()
        self.library = set(config.get("library_modules", ()))
        self.external = set(config.get("external_modules", ()))
        self.bare_imports = 0
        self.bare_resolved = 0
        self.bare_missing = []


def injected_names(path):
    """The badge builtins, and which firmware module each comes from.

    Read out of the ruff config, where the list already exists so ruff does not flag
    these as undefined, grouped by the file that injects them.
    """
    text = pathlib.Path(path).read_text()
    providers = {}
    provider = "firmware"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            provider = stripped.lstrip("# ").strip() or provider
            continue
        if stripped.startswith('"') and stripped.rstrip(",").endswith('"'):
            providers[stripped.strip('",')] = provider
    declared = tomllib.loads(text).get("builtins") or []
    return {name: providers.get(name, "firmware") for name in declared}


class Resolver:
    def __init__(self, targets, hints=(), entry_point_globs=()):
        self.targets = targets
        self.hints = list(hints)
        self.entry_point_globs = list(entry_point_globs)
        self.graph = Graph()
        self.scope_node = {}         # (target, module, qual) -> node id
        self.module_node = {}        # (target, module) -> module record id
        self.binding_node = {}       # (target, module, name) -> node id
        self.external_node = {}
        self.classes = {}            # node id -> {"bases": [], "methods": {}}
        self.tables = {}             # node id -> {"members": [], "open": bool}
        self.module_owner = {}       # module node id -> the Target holding it
        self.declared = {}
        self.mutated = {}
        self.attribute_types = {}    # (class node, attribute) -> the class it holds

    # -- pass 1: register ---------------------------------------------------

    def register(self):
        for target in self.targets:
            for name in sorted(target.modules):
                module = target.modules[name]
                module_id = self.graph.add_module(
                    f"{target.name}/{name}",
                    name=name,
                    path=str(module.path),
                    target=target.index,
                    lines=module.lines,
                    doc=module.body.doc,
                    is_package=module.is_package,
                )
                self.module_node[(target.index, name)] = module_id
                self.module_owner[self.graph.add_node(
                    f"{target.name}/{name}",
                    kind="module", name=name.rsplit(".", 1)[-1], qual=name,
                    module=module_id, target=target.index,
                    file=str(module.path), line=1, endline=module.lines,
                    sig="", doc=module.body.doc,
                    statements=module.body.statements,
                    branches=module.body.branches,
                    loop_depth=module.body.loop_depth,
                    allocs=module.body.allocs,
                    alloc_sites=module.body.alloc_sites,
                )] = target
                for scope in module.scopes:
                    if scope.kind == "module":
                        continue
                    key = f"{target.name}/{name}.{scope.qual}"
                    node = self.graph.add_node(
                        key,
                        kind=scope.kind, name=scope.name, qual=scope.qual,
                        module=module_id, target=target.index,
                        file=str(module.path), line=scope.line,
                        endline=scope.endline, sig=scope.sig, doc=scope.doc,
                        statements=scope.statements, branches=scope.branches,
                        loop_depth=scope.loop_depth, allocs=scope.allocs,
                        alloc_sites=scope.alloc_sites,
                        parent=scope.parent,
                    )
                    self.graph.node(node)["flags"].update(scope.flags)
                    self.graph.node(node)["returns_module"] = scope.returns_module
                    self.graph.node(node)["attributes"] = sorted(scope.attributes)
                    self.scope_node[(target.index, name, scope.qual)] = node
                    if scope.kind == "class":
                        self.classes[node] = {
                            "bases": scope.bases, "methods": {},
                            "module": name, "target": target.index,
                        }
                for scope in module.scopes:
                    if scope.parent is None or scope.kind == "module":
                        continue
                    owner = self.scope_node.get((target.index, name, scope.parent))
                    if owner in self.classes:
                        self.classes[owner]["methods"][scope.name] = \
                            self.scope_node[(target.index, name, scope.qual)]
                for binding, record in sorted(module.bindings.items()):
                    if binding in module.defs:
                        continue
                    node = self.graph.add_node(
                        f"{target.name}/{name}.{binding}",
                        kind="const", name=binding, qual=binding,
                        module=module_id, target=target.index,
                        file=str(module.path), line=record["line"],
                        endline=record["line"], sig="", doc="",
                        statements=0, branches=0, loop_depth=0, allocs=0,
                        alloc_sites=[],
                        value=record["value"], assigns=record["assigns"],
                        shape=record["shape"], members=record["members"],
                        reads=record["reads"],
                    )
                    self.binding_node[(target.index, name, binding)] = node

    def infer_attributes(self):
        """The class held by each `self.x`, from the constructor call that set it.

        A separate pass, because it needs every class registered first. It stops a
        method reached through an instance - `self.auth.begin_pairing()` - looking like
        nothing calls it.
        """
        for target in self.targets:
            for name in sorted(target.modules):
                module = target.modules[name]
                for scope in module.scopes:
                    if scope.returns_construct is not None:
                        held, _ = self.resolve(scope.returns_construct, target, name,
                                               scope)
                        node = self.scope_node.get((target.index, name, scope.qual))
                        if held is not None and node is not None \
                                and self.graph.node(held)["kind"] == "class":
                            self.graph.node(node)["returns_class"] = held
                    if scope.parent is None or not scope.attribute_types:
                        continue
                    holder = self.scope_node.get((target.index, name, scope.parent))
                    if holder is None:
                        continue
                    for attribute, shape in scope.attribute_types:
                        found, _ = self.resolve(shape, target, name, scope)
                        if found is None:
                            continue
                        if self.graph.node(found)["kind"] == "class":
                            self.attribute_types[(holder, attribute)] = found

    # -- name lookup --------------------------------------------------------

    def scope_imports(self, scope):
        """What a function imported for itself, shadowing the imports at module level.

        There are fourteen of these and every one is deliberate - `from . import portable`
        inside `sources.discover`, `import setup` inside `pairing_ui` - so a resolver that
        only reads module scope misses the edges that matter most.
        """
        cached = getattr(scope, "import_map", None)
        if cached is not None:
            return cached
        found = {}
        for bound, where, is_from, line in scope.imports:
            found[bound] = {"module": where, "is_from": is_from,
                            "name": bound if is_from else None, "line": line}
        scope.import_map = found
        return found

    def find_module(self, target, spec, from_module, is_from):
        """The module a bare or dotted import spec names, or None if it is not ours."""
        if target.flat:
            head = spec.lstrip(".").split(".")[0]
            if head in target.modules:
                return head
            return None
        if spec.startswith("."):
            depth = len(spec) - len(spec.lstrip("."))
            rest = spec.lstrip(".")
            module = target.modules.get(from_module)
            package = from_module if module and module.is_package else \
                from_module.rpartition(".")[0]
            for _ in range(depth - 1):
                package = package.rpartition(".")[0]
            candidate = f"{package}.{rest}" if rest else package
        else:
            candidate = spec
        if candidate in target.modules:
            return candidate
        if is_from:
            # `from statsbadge.sources.base import Source` names a module and a member.
            head = candidate.rpartition(".")[0]
            if head in target.modules:
                return candidate
        return None

    def find_anywhere(self, spec):
        """An absolute dotted module in whichever target has it.

        An extension's host module does `from statsbadge.sources.base import Source`,
        which crosses from the extension tree into the app's. Only absolute specs are
        looked for across targets: a flat target's bare `import draw` means the sibling
        beside it and must not reach into another tree.
        """
        if not spec or spec.startswith(".") or "." not in spec:
            return (None, None)
        for target in self.targets:
            if target.flat:
                continue
            if spec in target.modules:
                return (target, spec)
            head = spec.rpartition(".")[0]
            if head in target.modules:
                return (target, spec)
        return (None, None)

    def reach_into(self, module_node, attribute):
        """An attribute of a module node, looked up in the target that module belongs to."""
        owner = self.module_owner.get(module_node)
        if owner is None:
            return None
        return self.lookup_in(owner, self.graph.node(module_node)["qual"], attribute)

    def lookup(self, target, module_name, name, scope=None):
        """A module-level name in one module: its def, its binding, or an import."""
        module = target.modules[module_name]
        record = self.scope_imports(scope).get(name) if scope is not None else None

        if record is None:
            found = self.scope_node.get((target.index, module_name, name))
            if found is not None:
                return ("node", found)
            found = self.binding_node.get((target.index, module_name, name))
            if found is not None:
                return ("node", found)
            record = module.imports.get(name)

        if record is not None:
            holder = target
            where = self.find_module(target, record["module"], module_name,
                                     record["is_from"])
            if where is None:
                holder, where = self.find_anywhere(record["module"])
            if where is None:
                return ("external", record["module"].lstrip(".").split(".")[0])
            if record["is_from"] and record["name"]:
                # Either a member of that module, or a submodule of that package.
                inner = self.lookup_in(holder, where, record["name"])
                if inner is not None:
                    return ("node", inner)
                deeper = f"{where}.{record['name']}"
                if deeper in holder.modules:
                    return ("module_node", self.module_of(holder, deeper))
                return (None, None)
            return ("module_node", self.module_of(holder, where))

        if name in target.injected:
            return ("injected", name)
        return (None, None)

    def lookup_in(self, target, module_name, name):
        if module_name not in target.modules:
            return None
        found = self.scope_node.get((target.index, module_name, name))
        if found is not None:
            return found
        return self.binding_node.get((target.index, module_name, name))

    def resolve(self, shape, target, module_name, scope):
        """A node id for what an expression shape names, or None.

        Returns `(node_id, via)`, separating a plain name from something found
        by following a function that hands back a module.
        """
        if shape is None or shape[0] == OPAQUE:
            return (None, None)

        if shape[0] == NAME:
            name = shape[1]
            held = self.local_types(target, module_name, scope).get(name)
            if held is not None:
                return (held, "static")
            if name in scope.params and name not in scope.global_names:
                return (None, None)
            kind, value = self.lookup(target, module_name, name, scope)
            if kind == "node":
                return (value, "static")
            if kind == "module_node":
                return (value, "static")
            if kind == "injected":
                return (self.external(value, target.injected.get(value)), "static")
            if kind == "external":
                return (self.external(value, "package"), "static")
            return (None, None)

        if shape[0] == SELF:
            owner = scope.parent
            if owner is None:
                return (None, None)
            node = self.scope_node.get((target.index, module_name, f"{owner}.{shape[1]}"))
            if node is not None:
                return (node, "static")
            holder = self.scope_node.get((target.index, module_name, owner))
            if holder is None:
                return (None, None)
            # Inherited, perhaps: `self.note_fault(...)` in a Source subclass is defined on
            # the base. Without walking up, every inherited method reads as unreferenced.
            found = self.attribute_on(holder, shape[1], target)
            if found is not None:
                return (found, "static")
            # Failing that, an attribute whose type __init__ gave away.
            held = self.attribute_types.get((holder, shape[1]))
            return (held, "static") if held is not None else (None, None)

        if shape[0] == CALLED:
            inner = shape[1]
            # `super().__init__(...)`: whatever the enclosing class inherits from. Every
            # Source subclass starts this way, so without it three constructors and
            # everything they set up look unreached.
            if inner == (NAME, "super") and scope.parent is not None:
                holder = self.scope_node.get((target.index, module_name, scope.parent))
                for edge in self.graph.edges:
                    if edge["from"] == holder and edge["type"] == "inherit":
                        return (edge["to"], "static")
                return (None, None)
            if inner[0] == NAME:
                kind, value = self.lookup(target, module_name, inner[1], scope)
                if kind == "node":
                    node = self.graph.node(value)
                    handed = node.get("returns_module")
                    if handed:
                        return (self.module_of(
                            self.targets[node["target"]], handed), "static")
                    built = node.get("returns_class")
                    if built is not None:
                        return (built, "static")
            return self.resolve(inner, target, module_name, scope)

        if shape[0] == ATTR:
            base, attribute = shape[1], shape[2]
            # Names that are not nodes at all: an injected builtin like `screen`, or a
            # third-party package. Both get an external node at attribute granularity,
            # so `shape.arc` and `gc.collect` can be priced individually.
            if base[0] == NAME:
                kind, value = self.lookup(target, module_name, base[1], scope)
                if kind == "injected":
                    provider = target.injected.get(base[1], "firmware")
                    return (self.external(f"{base[1]}.{attribute}", provider), "static")
                if kind == "external":
                    return (self.external(f"{value}.{attribute}", "package"), "static")

            found, _ = self.resolve(base, target, module_name, scope)
            if found is None:
                return (None, None)
            reached = self.attribute_of(found, attribute)
            return (reached, "static") if reached is not None else (None, None)

        return (None, None)

    def local_types(self, target, module_name, scope):
        """The class held by each local name, where a constructor call gave it away.

        Built on first use and cached on the scope. The sentinel exists because
        resolving one local's constructor can ask about another, and a class is looked
        up by name like anything else.
        """
        cached = getattr(scope, "type_map", None)
        if cached is not None:
            return cached
        scope.type_map = {}
        for name, shape, _ in scope.local_types:
            found, _ = self.resolve(shape, target, module_name, scope)
            if found is None:
                continue
            held = self.graph.node(found)
            if held["kind"] == "class":
                scope.type_map[name] = found
            elif held.get("returns_class") is not None:
                scope.type_map[name] = held["returns_class"]
        return scope.type_map

    def attribute_of(self, node_index, attribute):
        """An attribute of whatever a node is: a module's member or a class's method."""
        node = self.graph.node(node_index)
        if node["kind"] == "module":
            return self.reach_into(node_index, attribute)
        if node["kind"] == "class":
            owner = self.targets[node["target"]] if node["target"] is not None else None
            if owner is None:
                return None
            found = self.attribute_on(node_index, attribute, owner)
            if found is not None:
                return found
            # Not a method, so perhaps an attribute holding another object:
            # `service.badges.begin_pairing()` goes through two of these.
            return self.attribute_types.get((node_index, attribute))
        return None

    def module_of(self, target, module_name):
        return self.graph.by_key.get(f"{target.name}/{module_name}")

    def external(self, name, provider):
        key = f"external/{name}"
        if key in self.external_node:
            return self.external_node[key]
        node = self.graph.add_node(
            key, kind="external", name=name, qual=name,
            module=None, target=None, file="", line=0, endline=0,
            sig="", doc="", statements=0, branches=0, loop_depth=0, allocs=0,
            alloc_sites=[], provider=provider or "package",
        )
        self.external_node[key] = node
        return node

    # -- pass 2: what is a constant, what is state, what is a table ---------

    def classify(self):
        """Split every module-level binding three ways, before any edge is drawn.

        Bound once and never touched is a constant; reading one is not coupling.
        Anything declared `global`, assigned twice, or changed in place is state - which
        is where the interesting half is, because most of this app's caches are never
        `global`-declared at all, only mutated.
        """
        declared = {}
        mutated = {}
        for target in self.targets:
            for name in sorted(target.modules):
                module = target.modules[name]
                for scope in module.scopes:
                    for global_name in sorted(scope.global_names):
                        node = self.binding_node.get((target.index, name, global_name))
                        if node is not None:
                            declared.setdefault(node, set()).add(
                                self.scope_id(target, name, scope))
                    for base, method, line, _, literal, argument in scope.mutations:
                        node, _ = self.resolve(base, target, name, scope)
                        if node is None:
                            continue
                        mutated.setdefault(node, []).append(
                            (self.scope_id(target, name, scope), method, line,
                             literal, argument, target, name, scope))

        for node in self.graph.nodes:
            index = node["id"]
            if node["kind"] != "const":
                continue
            members = node.get("members") or []
            is_state = (index in declared or index in mutated
                        or node.get("assigns", 1) > 1)
            if members:
                node["kind"] = "table"
                self.tables[index] = {"members": members, "open": False}
            elif node.get("shape") == "empty" and index in mutated \
                    and self.is_registry(mutated[index]):
                node["kind"] = "table"
                self.tables[index] = {"members": [], "open": True}
                node["flags"].add("open")
            elif is_state:
                node["kind"] = "state"
            if index in mutated:
                node["flags"].add("mutated-in-place")
            if index in declared:
                node["flags"].add("rebound")

        self.declared = declared
        self.mutated = mutated

    def is_registry(self, writes):
        """Whether an empty container is filled with things to dispatch on, or is a cache.

        Both start as `{}` at module scope and are written to by key, so the shape does
        not separate them. What goes in does. `pages.EXTRA["quakemap"] = render`
        puts a function in, and `pages.ANIMATED.add("waterfall")` puts a name in from
        three different modules. `draw._labels[key] = <a sprite>` puts in a value only
        this module ever computes, which is a cache and not a registry.
        """
        labels = set()
        for _, _, _, literal, argument, target, module_name, scope in writes:
            if argument is not None and argument[0] in (NAME, ATTR, SELF):
                found, _ = self.resolve(argument, target, module_name, scope)
                if found is not None and self.graph.node(found)["kind"] in (
                        "function", "method", "property", "class"):
                    return True
            if literal is not None:
                labels.add(module_name)
        return len(labels) >= 2

    def scope_id(self, target, module_name, scope):
        if scope.kind == "module":
            return self.graph.by_key.get(f"{target.name}/{module_name}")
        return self.scope_node.get((target.index, module_name, scope.qual))

    # -- pass 3: the plain edges -------------------------------------------

    def link(self):
        for target in self.targets:
            for name in sorted(target.modules):
                module = target.modules[name]
                module_node = self.graph.by_key[f"{target.name}/{name}"]
                for alias, record in sorted(module.imports.items()):
                    self.count_bare(target, name, record)
                    kind, value = self.lookup(target, name, alias)
                    if kind == "module_node":
                        self.graph.add_edge(module_node, value, "import",
                                            line=record["line"])
                    elif kind == "external":
                        self.graph.add_edge(module_node,
                                            self.external(value, "package"),
                                            "import", line=record["line"])
                    elif kind == "node":
                        holder = self.graph.node(value)["module"]
                        if holder is not None and holder != module_node:
                            self.graph.add_edge(
                                module_node, self.graph.by_key[
                                    self.graph.modules[holder]["key"]],
                                "import", line=record["line"])

                for scope in module.scopes:
                    self.link_scope(target, name, module, scope)

    def count_bare(self, target, module_name, record):
        """Track bare imports that ought to have resolved, and which did not.

        A flat target's `import draw` is a sibling file, and the whole badge app hangs
        off that reading correctly - get it wrong and the graph is eleven disconnected
        modules, with no error raised anywhere. The standard library is not counted, since
        `import time` resolving to nothing is the right answer.
        """
        if record["is_from"] or "." in record["module"]:
            return
        name = record["module"]
        if name in sys.stdlib_module_names or name in target.injected:
            return
        if name in target.external:
            return
        target.bare_imports += 1
        if self.find_module(target, name, module_name, False) is not None:
            target.bare_resolved += 1
        else:
            target.bare_missing.append(f"{module_name}: import {name}")

    def link_scope(self, target, module_name, module, scope):
        here = self.scope_id(target, module_name, scope)
        if here is None:
            return

        for call in scope.calls:
            node, via = self.resolve(call["shape"], target, module_name, scope)
            if node is not None:
                kind = "instantiate" if self.graph.node(node)["kind"] == "class" \
                    else "call"
                self.graph.add_edge(here, node, kind, via=via or "static",
                                    line=call["line"], loops=call["loops"])
                if kind == "instantiate":
                    # Constructing a class runs its __init__, so draw that edge too or
                    # every constructor in the codebase reads as unreferenced.
                    setup = self.classes.get(node, {}).get("methods", {}).get("__init__")
                    self.graph.add_edge(here, setup, "call", via=via or "static",
                                        line=call["line"])
            # A function handed over as an argument is called by whatever took it, so
            # the edge is drawn from here to it: this is where a thread's target and a
            # sort key show up at all.
            for shape in list(call["args"]) + list(call["kwargs"].values()):
                if shape is None or shape[0] not in (NAME, ATTR, SELF):
                    continue
                passed, _ = self.resolve(shape, target, module_name, scope)
                if passed is None:
                    continue
                if self.graph.node(passed)["kind"] in (
                        "function", "method", "property", "class"):
                    self.graph.add_edge(here, passed, "call", via="handed",
                                        line=call["line"])

        for shape, line, _ in scope.loads:
            node, _ = self.resolve(shape, target, module_name, scope)
            if node is None:
                continue
            record = self.graph.node(node)
            if record["kind"] in ("const", "state", "table"):
                self.graph.add_edge(here, node, "read", line=line)
                if record["kind"] != "const":
                    self.graph.node(here)["globals_read"].add(node)
            elif record["kind"] == "module":
                self.graph.add_edge(here, node, "read", line=line)

        for shape, line, _ in scope.stores:
            node, _ = self.resolve(shape, target, module_name, scope)
            if node is None:
                continue
            if self.graph.node(node)["kind"] in ("const", "state", "table"):
                self.graph.add_edge(here, node, "write", line=line)
                self.graph.node(here)["globals_written"].add(node)

        # `_labels.clear()` changes module state as surely as an assignment does, and in
        # this app it is the commoner of the two: most of the caches are never rebound.
        for base, _, line, _, _, _ in scope.mutations:
            node, _ = self.resolve(base, target, module_name, scope)
            if node is None:
                continue
            if self.graph.node(node)["kind"] in ("const", "state", "table"):
                self.graph.add_edge(here, node, "write", line=line)
                self.graph.node(here)["globals_written"].add(node)

        for global_name in sorted(scope.global_names):
            node = self.binding_node.get((target.index, module_name, global_name))
            if node is not None:
                self.graph.add_edge(here, node, "write", line=scope.line)
                self.graph.node(here)["globals_written"].add(node)

        if scope.kind == "class":
            self.link_class(target, module_name, module, scope, here)

    def link_class(self, target, module_name, module, scope, here):
        for base in scope.bases:
            node, _ = self.resolve(base, target, module_name, module.body)
            if node is None:
                continue
            self.graph.add_edge(here, node, "inherit")
            inherited = self.classes.get(node, {}).get("methods", {})
            mine = self.classes.get(here, {}).get("methods", {})
            for method, owner in sorted(inherited.items()):
                if method in mine:
                    self.graph.add_edge(mine[method], owner, "override")
                    self.graph.node(mine[method])["flags"].add("override")

    # -- pass 4: the tables ------------------------------------------------

    def fill_tables(self):
        """Give every table its members, from the literal and from elsewhere.

        A closed table names its members where it is written. An open one is empty there
        and filled by whoever imports it, which is how each extension's renderer reaches
        the app - so the members have to be collected after every module is known.
        """
        for index, table in self.tables.items():
            node = self.graph.node(index)
            module_name = self.graph.modules[node["module"]]["name"]
            target = self.targets[node["target"]]
            body = target.modules[module_name].body
            for key, shape in table["members"]:
                member, _ = self.resolve(shape, target, module_name, body)
                if member is not None:
                    self.graph.add_edge(index, member, "register", via="table",
                                        line=node["line"], label=key)
            node["flags"].add("dispatch")

        for index, writes in self.mutated.items():
            if index not in self.tables:
                continue
            for writer, method, line, literal, argument, target, name, scope in writes:
                if argument is None:
                    continue
                member, _ = self.resolve(argument, target, name, scope)
                if member is not None:
                    self.graph.add_edge(index, member, "register", via="table",
                                        line=line, label=literal)
                elif literal is not None:
                    # `pages.ANIMATED.add("waterfall")` names a page kind, not a
                    # renderer: it declares a property of one rather than providing it.
                    self.graph.add_edge(writer, index, "tag", via="table",
                                        line=line, label=str(literal))
                _ = method

    def key_of(self, table, member):
        """The key a table registered a member under, for a scenario to name it by."""
        if table is None:
            return None
        for edge in self.graph.edges:
            if edge["from"] == table and edge["to"] == member \
                    and edge["type"] == "register":
                return edge["label"]
        return None

    def table_members(self, index):
        return [edge["to"] for edge in self.graph.edges
                if edge["from"] == index and edge["type"] == "register"]

    def dispatch(self):
        """Follow a table through the local name a lookup put it in.

        `handler = EXTRA.get(kind) or _KINDS.get(kind)` then `handler(...)` is the whole
        page-drawing path, and `for cls in candidates` over a list built a class at a
        time is every stats source. Both are one function's worth of dataflow, so that
        is as far as this looks.
        """
        for target in self.targets:
            for name in sorted(target.modules):
                module = target.modules[name]
                for scope in module.scopes:
                    self.dispatch_scope(target, name, scope)

    def dispatch_scope(self, target, module_name, scope):
        here = self.scope_id(target, module_name, scope)
        if here is None:
            return

        holds = {}

        came_from = {}

        def members_of(source):
            kind, payload = source
            if kind == "display":
                found = []
                for _, shape in payload:
                    node, _ = self.resolve(shape, target, module_name, scope)
                    if node is not None:
                        found.append(node)
                return found
            node, _ = self.resolve(payload, target, module_name, scope)
            if node is not None and node in self.tables:
                if kind == "member":
                    members = self.table_members(node)
                    for member in members:
                        came_from[member] = node
                    return members
                return [node]
            if payload[0] == NAME and payload[1] in holds:
                return list(holds[payload[1]])
            return []

        for local, _, operator, sources in scope.local_binds:
            found = []
            for source in sources:
                found.extend(members_of(source))
            if not found:
                continue
            if operator == "+=" and local in holds:
                holds[local].update(found)
            else:
                holds.setdefault(local, set()).update(found)

        for base, method, _, _, _, argument in scope.mutations:
            if base[0] != NAME or base[1] not in holds or argument is None:
                continue
            if method not in ("append", "add", "extend", "__setitem__"):
                continue
            node, _ = self.resolve(argument, target, module_name, scope)
            if node is not None:
                holds[base[1]].add(node)

        for local, shape, _ in scope.iterations:
            found = members_of(("member", shape))
            if not found and shape[0] == NAME and shape[1] in holds:
                found = list(holds[shape[1]])
            if found:
                holds.setdefault(local, set()).update(found)

        if not holds:
            return

        for call in scope.calls:
            shape = call["shape"]
            if shape[0] == NAME and shape[1] in holds:
                for member in sorted(holds[shape[1]]):
                    kind = "instantiate" if self.graph.node(member)["kind"] == "class" \
                        else "call"
                    self.graph.add_edge(here, member, kind, via="table",
                                        line=call["line"], loops=call["loops"],
                                        label=self.key_of(came_from.get(member), member),
                                        table=came_from.get(member))
                    self.graph.node(member)["flags"].add("dispatch-target")
            elif (shape[0] == ATTR and shape[1][0] == NAME
                  and shape[1][1] in holds):
                for member in sorted(holds[shape[1][1]]):
                    reached = self.attribute_on(member, shape[2], target)
                    if reached is not None:
                        self.graph.add_edge(here, reached, "call", via="table",
                                            line=call["line"])
                        self.graph.node(reached)["flags"].add("dispatch-target")

    def attribute_on(self, node_index, attribute, target):
        """An attribute of a class or module node, following bases where it is a class."""
        record = self.graph.node(node_index)
        if record["kind"] == "module":
            return self.lookup_in(target, record["qual"], attribute)
        if record["kind"] != "class":
            return None
        seen = set()
        stack = [node_index]
        while stack:
            current = stack.pop(0)
            if current in seen:
                continue
            seen.add(current)
            entry = self.classes.get(current)
            if entry is None:
                continue
            found = entry["methods"].get(attribute)
            if found is not None:
                return found
            for edge in self.graph.edges:
                if edge["from"] == current and edge["type"] == "inherit":
                    stack.append(edge["to"])
        return None

    # -- pass 5: the recognisers -------------------------------------------

    def argparse_rule(self):
        """`sub.set_defaults(func=cmd_serve)` now, `args.func(args)` later.

        Generalising this would mean modelling argparse, but the idiom is most of the
        Python CLIs there are, and without it every subcommand is an island and `main()`
        looks like it does nothing.
        """
        for target in self.targets:
            for name in sorted(target.modules):
                module = target.modules[name]
                registered = {}
                for scope in module.scopes:
                    for call in scope.calls:
                        shape = call["shape"]
                        if shape[0] != ATTR or shape[2] != "set_defaults":
                            continue
                        for keyword, value in sorted(call["kwargs"].items()):
                            node, _ = self.resolve(value, target, name, scope)
                            if node is None:
                                continue
                            if self.graph.node(node)["kind"] in (
                                    "function", "method", "class"):
                                registered.setdefault(keyword, set()).add(node)
                if not registered:
                    continue
                for scope in module.scopes:
                    here = self.scope_id(target, name, scope)
                    for call in scope.calls:
                        shape = call["shape"]
                        if shape[0] != ATTR or shape[2] not in registered:
                            continue
                        for node in sorted(registered[shape[2]]):
                            self.graph.add_edge(here, node, "call", via="argparse",
                                                line=call["line"], label=shape[2])
                            self.graph.node(node)["flags"].add("dispatch-target")

    def entry_points(self):
        """The classes an installed package advertises, from the pyproject beside it.

        Three lines of TOML per extension close the one dynamic edge on the host side
        that is cheap to close: `entry.load()` cannot be followed, but what it would
        load is written down.
        """
        for pattern in self.entry_point_globs:
            for path in sorted(pathlib.Path().glob(pattern)):
                try:
                    with path.open("rb") as handle:
                        data = tomllib.load(handle)
                except (OSError, tomllib.TOMLDecodeError) as exc:
                    self.graph.notes.append(f"{path}: {exc}")
                    continue
                groups = data.get("project", {}).get("entry-points", {})
                for group, entries in sorted(groups.items()):
                    for advertised, spec in sorted(entries.items()):
                        self.link_entry_point(group, advertised, spec, path)

    def link_entry_point(self, group, advertised, spec, path):
        module_name, _, attribute = spec.partition(":")
        for target in self.targets:
            if module_name not in target.modules:
                continue
            node = self.lookup_in(target, module_name, attribute)
            if node is None:
                continue
            holder = self.graph.by_key.get(f"{target.name}/{module_name}")
            self.graph.add_edge(holder, node, "register", via="entrypoint",
                                label=f"{group}:{advertised}")
            self.graph.node(node)["flags"].add("entrypoint")
            for consumer in self.consumers_of(group):
                self.graph.add_edge(consumer, node, "call", via="entrypoint",
                                    label=advertised)
            _ = path

    def consumers_of(self, group):
        """Whoever reads this entry-point group: the scope naming it as a string."""
        found = []
        for target in self.targets:
            for name in sorted(target.modules):
                path = target.modules[name].path
                try:
                    text = pathlib.Path(path).read_text()
                except OSError:
                    continue
                if f'"{group}"' not in text and f"'{group}'" not in text:
                    continue
                for scope in target.modules[name].scopes:
                    if scope.kind != "function":
                        continue
                    if any(call["shape"][-1] == "entry_points"
                           for call in scope.calls if call["shape"][0] == ATTR):
                        found.append(self.scope_id(target, name, scope))
                    elif any(call["shape"] == (NAME, "entry_points")
                             for call in scope.calls):
                        found.append(self.scope_id(target, name, scope))
        return [node for node in found if node is not None]

    def framework_entries(self):
        """Methods a framework calls, which would otherwise read as dead code."""
        for target in self.targets:
            for pattern in target.config.get("framework_entry", ()):
                module_spec, _, qual_spec = pattern.partition(":")
                for name in sorted(target.modules):
                    if not fnmatch.fnmatch(name, module_spec):
                        continue
                    for scope in target.modules[name].scopes:
                        if not fnmatch.fnmatch(scope.qual, qual_spec):
                            continue
                        node = self.scope_node.get((target.index, name, scope.qual))
                        if node is not None:
                            self.graph.node(node)["flags"].add("entrypoint")
                            self.graph.node(node)["flags"].add("framework")

    def vtable(self):
        """A call on a name we cannot resolve, where the method belongs to one hierarchy.

        Restricted to method names defined nowhere else, which stops `get` or
        `run` joining everything to everything. The `Source` vtable - `available`,
        `sample`, `configure`, `pages`, `note_fault` - passes that test.
        """
        owners = {}
        for index, entry in self.classes.items():
            for method, node in entry["methods"].items():
                if method.startswith("__") or method in NEVER_VIRTUAL:
                    continue
                owners.setdefault(method, []).append((index, node))

        virtual = {}
        for method, holders in owners.items():
            roots = {self.hierarchy_root(index) for index, _ in holders}
            if len(roots) == 1 and len(holders) > 1:
                virtual[method] = [node for _, node in holders]

        if not virtual:
            return
        for target in self.targets:
            for name in sorted(target.modules):
                module = target.modules[name]
                for scope in module.scopes:
                    here = self.scope_id(target, name, scope)
                    if here is None:
                        continue
                    for call in scope.calls:
                        shape = call["shape"]
                        if shape[0] != ATTR or shape[2] not in virtual:
                            continue
                        if self.resolve(shape, target, name, scope)[0] is not None:
                            continue
                        for node in virtual[shape[2]]:
                            self.graph.add_edge(here, node, "call", via="vtable",
                                                line=call["line"], label=shape[2])

    def hierarchy_root(self, index):
        seen = set()
        current = index
        while current not in seen:
            seen.add(current)
            bases = [edge["to"] for edge in self.graph.edges
                     if edge["from"] == current and edge["type"] == "inherit"
                     and self.graph.node(edge["to"])["kind"] == "class"]
            if not bases:
                return current
            current = bases[0]
        return current

    def apply_hints(self):
        """Edges no rule can find, each carrying the reason it was written down."""
        for hint in self.hints:
            why = hint.get("why")
            if not why:
                self.graph.notes.append(
                    f"hint {hint.get('from')} -> {hint.get('to')} has no `why`, skipped")
                continue
            source = self.find_by_spec(hint["from"])
            target = self.find_by_spec(hint["to"])
            if source is None or target is None:
                self.graph.notes.append(
                    f"hint names something that is not here: "
                    f"{hint['from']} -> {hint['to']}")
                continue
            already = [edge for edge in self.graph.edges
                       if edge["from"] == source and edge["to"] == target
                       and edge["via"] != "hint"]
            if already:
                self.graph.notes.append(
                    f"hint {hint['from']} -> {hint['to']} is now found by "
                    f"{already[0]['via']}, so the hint can go")
                continue
            self.graph.add_edge(source, target, hint.get("kind", "call"), via="hint",
                                label=why)

    def find_by_spec(self, spec):
        """`badge_app.net:Client.step` as a node, in whichever target holds it."""
        module_name, _, qual = spec.partition(":")
        for target in self.targets:
            if module_name not in target.modules:
                continue
            if not qual:
                return self.graph.by_key.get(f"{target.name}/{module_name}")
            found = self.scope_node.get((target.index, module_name, qual))
            if found is not None:
                return found
            found = self.binding_node.get((target.index, module_name, qual))
            if found is not None:
                return found
        return None

    # -- pass 6: derived constants ------------------------------------------

    def link_bindings(self):
        """A constant worked out from other constants gets an edge to each of them."""
        for target in self.targets:
            for name in sorted(target.modules):
                module = target.modules[name]
                body = module.body
                for binding, record in sorted(module.bindings.items()):
                    here = self.binding_node.get((target.index, name, binding))
                    if here is None:
                        continue
                    sources = []
                    for shape in record["reads"]:
                        node, _ = self.resolve(shape, target, name, body)
                        if node is None or node == here:
                            continue
                        if self.graph.node(node)["kind"] in ("const", "state", "table"):
                            sources.append(node)
                    for node in dict.fromkeys(sources):
                        self.graph.add_edge(here, node, "read", line=record["line"])
                    if sources:
                        self.graph.node(here)["flags"].add("derived")
                        self.graph.node(here)["derived_from"] = list(
                            dict.fromkeys(sources))

    # -- pass 7: the numbers -----------------------------------------------

    def finish(self):
        incoming = {}
        outgoing = {}
        writers = {}
        readers = {}
        for edge in self.graph.edges:
            if edge["type"] in ("call", "instantiate", "read", "register"):
                incoming.setdefault(edge["to"], set()).add(edge["from"])
            if edge["type"] in ("call", "instantiate"):
                outgoing.setdefault(edge["from"], set()).add(edge["to"])
            if edge["type"] == "write":
                writers.setdefault(edge["to"], set()).add(edge["from"])
            if edge["type"] == "read":
                readers.setdefault(edge["to"], set()).add(edge["from"])

        for node in self.graph.nodes:
            index = node["id"]
            node["fan_in"] = len(incoming.get(index, ()))
            node["fan_out"] = len(outgoing.get(index, ()))
            node["writers"] = len(writers.get(index, ()))
            node["readers"] = len(readers.get(index, ()))
            node["lines"] = max(1, node["endline"] - node["line"] + 1)

        self.flag_caches(writers)
        self.score_globals(outgoing)
        self.assign_layers(outgoing)
        # A separate adjacency: `outgoing` also feeds fan_out and the global-state
        # scores, so widening it would silently move every one of those numbers and with
        # them every colour in the viewer.
        self.assign_flow()

    def assign_flow(self):
        """How deep in the machine each node sits: 0 is an entry point, the most is a leaf.

        Not breadth-first depth. Measured on this codebase, that leaves 1492 of 4102 edges
        pointing back up the way, because it gives the *minimum* distance from an entry
        point and so a node with a shorter route from somewhere else sits above things that
        call it. A third of the edges climbing is a hairball, not a flow.

        What matters is that the level rises along every edge, and any layering with that
        property has no back edges at all. The earliest legal level for a node is the
        longest path down to it; the latest is as far down as the longest chain allows.
        Either works, and both are lopsided: earliest crowds everything against the top,
        latest leaves 478 of 1008 nodes on the floor. The midpoint of the two is balanced
        and still rises along every edge, since both bounds shift by at least one.
        """
        FLOWING = ("call", "instantiate", "read", "register", "tag")
        forward = {}
        for edge in self.graph.edges:
            if edge["type"] in FLOWING:
                forward.setdefault(edge["from"], set()).add(edge["to"])

        groups = strong_components(list(range(len(self.graph.nodes))), forward)
        group_of = {}
        for index, group in enumerate(groups):
            for member in group:
                group_of[member] = index

        down = {index: set() for index in range(len(groups))}
        up = {index: set() for index in range(len(groups))}
        for source, sinks in forward.items():
            for sink in sinks:
                if group_of[source] != group_of[sink]:
                    down[group_of[source]].add(group_of[sink])
                    up[group_of[sink]].add(group_of[source])

        earliest = self.longest_paths(up)
        to_leaf = self.longest_paths(down)
        deepest = max(max(earliest.values(), default=0),
                      max(to_leaf.values(), default=0))

        for node in self.graph.nodes:
            group = group_of[node["id"]]
            latest = deepest - to_leaf[group]
            node["flow"] = (earliest[group] + latest) // 2

        # A firmware primitive is the floor of the machine whatever the arithmetic gives,
        # and that is safe: an external has no outgoing edges, so nothing sits
        # below it to be climbed back up to.
        for node in self.graph.nodes:
            if node["kind"] == "external":
                node["flow"] = deepest

    def longest_paths(self, adjacent):
        """Steps to the furthest reachable group, iteratively so nothing recurses deeply."""
        depth = {}
        for start in adjacent:
            if start in depth:
                continue
            work = [(start, iter(sorted(adjacent[start])))]
            waiting = {start}
            while work:
                index, children = work[-1]
                child = next(children, None)
                if child is None:
                    work.pop()
                    waiting.discard(index)
                    beyond = [depth[other] for other in adjacent[index]
                              if other in depth]
                    depth[index] = 1 + max(beyond) if beyond else 0
                elif child not in depth and child not in waiting:
                    waiting.add(child)
                    work.append((child, iter(sorted(adjacent[child]))))
        return depth

    def flag_caches(self, writers):
        """Tell a deliberate cache from a hazard, and name the caches nothing resets.

        Painting `draw.py` scarlet for touching module state would be true and useless -
        its caches are the design. What is worth reporting is a piece of state with more
        than one writer and no reset hook, which on 8MB of PSRAM is a leak candidate.
        """
        resets = {}
        for node in self.graph.nodes:
            written = node["globals_written"]
            read = node["globals_read"]
            if not written:
                continue
            if written & read:
                node["flags"].add("memo")
            if len(written) >= 2 and node["statements"] <= 3 * len(written) + 2:
                node["flags"].add("reset_hook")
                for state in sorted(written):
                    resets.setdefault(state, []).append(node["id"])

        for node in self.graph.nodes:
            if node["kind"] != "state":
                continue
            node["reset_by"] = sorted(resets.get(node["id"], ()))
            count = len(writers.get(node["id"], ()))
            # Only a container that is added to can grow without bound, so a name that
            # two functions rebind is no leak candidate however often it changes.
            grows = "mutated-in-place" in node["flags"]
            if grows and not node["reset_by"] and count > 1:
                node["flags"].add("unreset")
            if count == 1 and node["readers"] == 1:
                node["flags"].add("singleton")
            if node.get("shape") in ("empty", "none") or node["flags"] & {"memo"}:
                node["flags"].add("cache")

    def score_globals(self, outgoing):
        """Reading a constant is free; writing a cache two levels down is the surprise."""
        order = self.post_order(outgoing)
        transitive = {}
        for index in order:
            node = self.graph.node(index)
            reached = set()
            for callee in outgoing.get(index, ()):
                reached |= self.graph.node(callee)["globals_written"]
                reached |= transitive.get(callee, set())
            transitive[index] = reached

        for node in self.graph.nodes:
            index = node["id"]
            written = node["globals_written"]
            read = node["globals_read"]
            deeper = transitive.get(index, set()) - written
            node["gw"] = len(written)
            node["gr"] = len(read)
            node["gwt"] = len(deeper)
            node["gk"] = len([edge for edge in () if edge])
            node["globals"] = round(3 * len(written) + len(read) + 0.5 * len(deeper), 2)
            node["globals_read"] = sorted(read)
            node["globals_written"] = sorted(written)
            node["globals_written_deep"] = sorted(deeper)

    def post_order(self, outgoing):
        """Callees before callers, with a cycle broken wherever it is first met."""
        order = []
        state = {}
        for start in range(len(self.graph.nodes)):
            if state.get(start):
                continue
            stack = [(start, iter(sorted(outgoing.get(start, ()))))]
            state[start] = 1
            while stack:
                index, children = stack[-1]
                found = next(children, None)
                if found is None:
                    stack.pop()
                    state[index] = 2
                    order.append(index)
                elif not state.get(found):
                    state[found] = 1
                    stack.append((found, iter(sorted(outgoing.get(found, ())))))
        return order

    def assign_layers(self, outgoing):
        """How far each node is from something that starts running unprompted."""
        roots = []
        for target in self.targets:
            for spec in target.config.get("entrypoints", ()):
                node = self.find_by_spec(spec)
                if node is not None:
                    roots.append(node)
        roots.extend(node["id"] for node in self.graph.nodes
                     if "entrypoint" in node["flags"] or node["kind"] == "module")

        depth = {}
        frontier = [(node, 0) for node in dict.fromkeys(roots)]
        for node, _ in frontier:
            depth[node] = 0
        while frontier:
            following = []
            for node, level in frontier:
                for callee in sorted(outgoing.get(node, ())):
                    if callee not in depth:
                        depth[callee] = level + 1
                        following.append((callee, level + 1))
            frontier = following

        for node in self.graph.nodes:
            node["layer"] = depth.get(node["id"], -1)
            if node["fan_in"] == 0 and not node["flags"] & {"entrypoint"} \
                    and node["kind"] in (
                    "function", "method", "property", "class"):
                module = self.graph.modules[node["module"]]["name"] \
                    if node["module"] is not None else ""
                target = self.targets[node["target"]] if node["target"] is not None \
                    else None
                library = target.library if target else set()
                if module.rsplit(".", 1)[-1] not in library:
                    node["flags"].add("unreached")

    def run(self):
        self.register()
        self.infer_attributes()
        self.forget_caches()
        self.classify()
        self.link()
        self.link_bindings()
        self.fill_tables()
        self.framework_entries()
        self.entry_points()
        self.argparse_rule()
        self.dispatch()
        self.vtable()
        self.apply_hints()
        self.dedupe()
        self.finish()
        return self.graph

    def forget_caches(self):
        """Drop the per-scope type maps the inference passes warmed while filling them in."""
        for target in self.targets:
            for module in target.modules.values():
                for scope in module.scopes:
                    scope.type_map = None

    def dedupe(self):
        """One edge per (source, target, kind, via, line). The first seen wins."""
        seen = {}
        for edge in self.graph.edges:
            key = (edge["from"], edge["to"], edge["type"], edge["via"], edge["line"])
            if key not in seen:
                seen[key] = edge
        self.graph.edges = list(seen.values())
