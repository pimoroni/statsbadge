#!/usr/bin/env python3
"""Read a tree of Python into modules, scopes and unresolved references.

    (imported by tools/callgraph.py, not run directly)

Nothing here reads any other file. A reference is recorded as the shape of the
expression that made it, and resolved later once every module is known. That is what lets
the badge app be read at all: `import draw` there means the sibling file, and no per-file
pass can tell. Nothing is imported or executed either, these modules expecting the badge's
builtins, so the source is all there is to go on.
"""

import ast
import pathlib

# The shape of a referencing expression, resolved by callgraph_resolve.
#   ("name", "dial")                      a bare name
#   ("attr", <shape>, "dial")             an attribute of something
#   ("called", <shape>)                   whatever calling something returns
#   ("self", "render")                    an attribute of the enclosing instance
#   ("opaque",)                           an expression this does not model
NAME = "name"
ATTR = "attr"
CALLED = "called"
SELF = "self"
OPAQUE = "opaque"
OPAQUE_SHAPE = (OPAQUE,)

# Methods that change a container in place, so a module-level name they are called on is
# state rather than a constant however it was bound.
MUTATORS = frozenset({
    "append", "add", "update", "extend", "pop", "clear", "remove", "insert",
    "setdefault", "sort", "discard", "popitem", "reverse",
})

# Firmware calls that allocate, from DEVELOPMENT.md: a shape.circle is 416 bytes, a
# rectangle 192, a mat3 32. Counted to flag a construction inside a loop, which is the
# badge's one recurring performance fault.
ALLOCATORS = frozenset({
    "vec2", "mat3", "rect", "color", "image", "array", "bytearray", "memoryview",
})
ALLOCATING_ATTRS = frozenset({
    "circle", "rectangle", "rounded_rectangle", "squircle", "arc", "custom", "regular",
    "gradient", "with_alpha", "copy",
})

# `screen.pen = <colour>` allocates 64 bytes where assigning a transform does not, so a
# store to one of these is an allocation site in its own right.
ALLOCATING_STORES = frozenset({"pen"})


class Scope:
    """One function, method, class body or module body, and what it refers to."""

    def __init__(self, kind, name, qual, line, endline):
        self.kind = kind
        self.name = name
        self.qual = qual
        self.line = line
        self.endline = endline
        self.sig = ""
        self.doc = ""
        self.decorators = []
        self.bases = []
        self.attributes = set()
        self.attribute_types = []     # (attribute, the shape it was constructed from)
        self.parent = None
        self.flags = set()
        self.params = set()

        # Raw references, each (shape, line, loop_depth) unless noted.
        self.calls = []
        self.loads = []
        self.stores = []
        self.mutations = []          # (shape, method, line, loops, literal, arg_shape)
        self.instantiations = []
        self.global_names = set()
        self.imports = []            # (alias, module, is_from, line)
        self.returns_module = None
        self.returns_construct = None

        # Local dataflow, only as far as a dispatch table needs following.
        self.local_binds = []        # (name, line, op, [source, ...])
        self.local_types = []        # (name, the shape it was constructed from, line)
        self.iterations = []         # (target_name, iterated_shape, line)

        # Counted facts.
        self.statements = 0
        self.branches = 0
        self.loop_depth = 0
        self.allocs = 0
        self.alloc_sites = []        # (line, loop_depth, what)

    @property
    def complexity(self):
        return 1 + self.branches


class Module:
    """One source file: its scopes, its bindings and how it names other modules."""

    def __init__(self, name, path, target, flat):
        self.name = name
        self.path = path
        self.target = target
        self.flat = flat
        self.is_package = path.name == "__init__.py"
        self.lines = 0
        self.scopes = []
        self.bindings = {}           # name -> {"line", "assigns", "shape", "value"}
        self.imports = {}            # alias -> {"module", "is_from", "name", "line"}
        self.body = None             # the module-level Scope
        self.defs = {}               # top-level name -> Scope


def scan_tree(paths, target, flat, root):
    """Every path scanned into a Module, keyed by the module name it answers to."""
    modules = {}
    faults = []
    for path in paths:
        try:
            source = path.read_text()
        except OSError as exc:
            faults.append(f"{path}: {exc.strerror}")
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            faults.append(f"{path}:{exc.lineno}: {exc.msg}")
            continue
        module = scan_module(tree, path, target, flat, root)
        module.lines = source.count("\n") + 1
        modules[module.name] = module
    return modules, faults


def module_name_for(path, root, flat):
    """The name other modules in this target would import this file by.

    A flat target puts every file in one namespace, because the app inserts its own
    directory on sys.path and imports its siblings by bare name. A package target names
    a file by its dotted path from the root's parent, so `from . import auth` and
    `statsbadge.auth` describe the same module.
    """
    if flat:
        return path.stem
    root = pathlib.Path(root).resolve()
    # A root that is itself a package is named from outside it, so src/statsbadge/auth.py
    # is `statsbadge.auth`. A root that just holds packages is named from inside, so
    # extensions/*/src/statsbadge_clock is `statsbadge_clock` - the name its own entry
    # point advertises it under, and what the entry-point rule has to match.
    base = root.parent if (root / "__init__.py").exists() else root
    relative = path.resolve().relative_to(base)
    parts = list(relative.parts)
    if parts[-1] == "__init__.py":
        parts.pop()
    else:
        parts[-1] = pathlib.Path(parts[-1]).stem
    return ".".join(parts)


def scan_module(tree, path, target, flat, root):
    module = Module(module_name_for(path, root, flat), path, target, flat)
    body = Scope("module", "<module>", "<module>", 1, getattr(tree, "end_lineno", 1) or 1)
    body.doc = first_line(ast.get_docstring(tree))
    module.body = body
    module.scopes.append(body)

    walker = Walker(module)
    walker.scope_body(tree.body, body, top_level=True)
    return module


def first_line(doc):
    if not doc:
        return ""
    return doc.strip().split("\n", 1)[0].strip()


class Walker:
    """Walks one module, filling in its scopes as it goes."""

    def __init__(self, module):
        self.module = module

    # -- scopes -------------------------------------------------------------

    def scope_body(self, statements, scope, top_level=False, class_scope=None):
        """Record every statement in one scope, descending into nested definitions."""
        for statement in statements:
            self.statement(statement, scope, 0, top_level, class_scope)

    def statement(self, node, scope, loops, top_level=False, class_scope=None):
        scope.statements += 1
        scope.loop_depth = max(scope.loop_depth, loops)

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.define_function(node, scope, class_scope, top_level)
            return
        if isinstance(node, ast.ClassDef):
            self.define_class(node, scope, top_level)
            return

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            self.record_import(node, scope, top_level)
            return

        if isinstance(node, ast.Global):
            scope.global_names.update(node.names)
            return

        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            scope.branches += 1
            self.expression(getattr(node, "iter", None) or node.test, scope, loops)
            if isinstance(node, (ast.For, ast.AsyncFor)):
                if isinstance(node.target, ast.Name):
                    scope.iterations.append(
                        (node.target.id, describe(node.iter), node.lineno))
                self.store_target(node.target, scope, loops, top_level)
            for inner in node.body + node.orelse:
                self.statement(inner, scope, loops + 1, top_level, class_scope)
            return

        if isinstance(node, ast.If):
            scope.branches += 1
            self.expression(node.test, scope, loops)
            for inner in node.body + node.orelse:
                self.statement(inner, scope, loops, top_level, class_scope)
            return

        if isinstance(node, ast.Try):
            for handler in node.handlers:
                scope.branches += 1
                if handler.type is not None:
                    self.expression(handler.type, scope, loops)
            groups = [node.body, node.orelse, node.finalbody]
            groups.extend(handler.body for handler in node.handlers)
            for group in groups:
                for inner in group:
                    self.statement(inner, scope, loops, top_level, class_scope)
            return

        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                self.expression(item.context_expr, scope, loops)
                if item.optional_vars is not None:
                    self.store_target(item.optional_vars, scope, loops, top_level)
            for inner in node.body:
                self.statement(inner, scope, loops, top_level, class_scope)
            return

        if isinstance(node, ast.Assign):
            self.expression(node.value, scope, loops)
            for target in node.targets:
                self.store_target(target, scope, loops, top_level, node.value)
                self.note_local_bind(target, scope, "=", node.value)
            return

        if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            if node.value is not None:
                self.expression(node.value, scope, loops)
            self.store_target(node.target, scope, loops, top_level, node.value)
            self.note_local_bind(node.target, scope,
                                 "=" if isinstance(node, ast.AnnAssign) else "+=",
                                 node.value)
            return

        if isinstance(node, (ast.Return, ast.Expr, ast.Await, ast.Assert,
                             ast.Delete, ast.Raise)):
            for child in ast.iter_child_nodes(node):
                self.expression(child, scope, loops)
            return

        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                self.statement(child, scope, loops, top_level, class_scope)
            else:
                self.expression(child, scope, loops)

    def define_function(self, node, outer, class_scope, top_level):
        kind = "method" if class_scope is not None else "function"
        decorators = [describe(d) for d in node.decorator_list]
        names = [decorator_name(d) for d in node.decorator_list]
        if class_scope is not None and "property" in names:
            kind = "property"
        if class_scope is not None and "staticmethod" in names:
            kind = "function"

        qual = node.name if class_scope is None else f"{class_scope.name}.{node.name}"
        if not top_level and class_scope is None:
            qual = f"{outer.qual}.{node.name}"

        scope = Scope(kind, node.name, qual, node.lineno, node.end_lineno)
        scope.params = param_names(node.args)
        scope.sig = signature_of(node)
        scope.doc = first_line(ast.get_docstring(node))
        scope.decorators = decorators
        scope.parent = class_scope.qual if class_scope is not None else None
        if class_scope is None and not top_level:
            scope.flags.add("nested")
        if generator_in(node):
            scope.flags.add("generator")

        self.module.scopes.append(scope)
        if class_scope is None and top_level:
            self.module.defs[node.name] = scope
        if class_scope is not None:
            class_scope.attributes.add(node.name)

        for decorator in node.decorator_list:
            self.expression(decorator, outer, 0)
        for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d]:
            self.expression(default, outer, 0)

        self.scope_body(node.body, scope, top_level=False, class_scope=class_scope)
        self.note_returned_module(node, scope)

    def define_class(self, node, outer, top_level):
        qual = node.name if top_level else f"{outer.qual}.{node.name}"
        scope = Scope("class", node.name, qual, node.lineno, node.end_lineno)
        scope.doc = first_line(ast.get_docstring(node))
        scope.decorators = [describe(d) for d in node.decorator_list]
        scope.bases = [describe(base) for base in node.bases]

        self.module.scopes.append(scope)
        if top_level:
            self.module.defs[node.name] = scope

        for base in node.bases:
            self.expression(base, outer, 0)
        for decorator in node.decorator_list:
            self.expression(decorator, outer, 0)

        self.scope_body(node.body, scope, top_level=False, class_scope=scope)

    def note_returned_module(self, node, scope):
        """What a function hands back, where that is worth following.

        Two shapes, both of which are the only way something gets reached here.
        `import X; return X` is `badge_app.pairing_ui`, and the pairing flow hangs
        entirely off `pairing_ui().run(app)`. `return Service(...)` is `build_service`,
        and every method called on what it returns depends on knowing the class.
        """
        imported = {alias for alias, _, _, _ in scope.imports}
        returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
        if len(returns) != 1 or returns[0].value is None:
            return
        value = returns[0].value
        if isinstance(value, ast.Name) and value.id in imported:
            scope.returns_module = value.id
        elif isinstance(value, ast.Call):
            scope.returns_construct = describe(value.func)

    # -- references ---------------------------------------------------------

    def record_import(self, node, scope, top_level):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = (alias.asname or alias.name).split(".")[0]
                scope.imports.append((bound, alias.name, False, node.lineno))
                if top_level:
                    self.module.imports[bound] = {
                        "module": alias.name, "is_from": False,
                        "name": None, "line": node.lineno,
                    }
            return

        # `from . import auth` and `from .base import Source` both land here; the dots are
        # what say how far up the package to start looking.
        where = "." * node.level + (node.module or "")
        for alias in node.names:
            bound = alias.asname or alias.name
            scope.imports.append((bound, where, True, node.lineno))
            if top_level:
                self.module.imports[bound] = {
                    "module": where, "is_from": True,
                    "name": alias.name, "line": node.lineno,
                }

    def store_target(self, target, scope, loops, top_level, value=None):
        if isinstance(target, ast.Name):
            if top_level:
                self.bind(target.id, target.lineno, value)
            else:
                scope.stores.append(((NAME, target.id), target.lineno, loops))
            return

        if isinstance(target, ast.Attribute):
            base = describe(target.value)
            scope.stores.append(((ATTR, base, target.attr), target.lineno, loops))
            if isinstance(target.value, ast.Name) and target.value.id == "self":
                scope.attributes.add(target.attr)
                # `self.auth = auth.Store(...)` is what lets a later
                # `self.auth.begin_pairing()` resolve: without the attribute's type,
                # every method reached through an instance looks unreferenced.
                if isinstance(value, ast.Call):
                    scope.attribute_types.append((target.attr, describe(value.func)))
            if target.attr in ALLOCATING_STORES:
                scope.allocs += 1
                scope.alloc_sites.append((target.lineno, loops, f"{target.attr} ="))
            self.expression(target.value, scope, loops)
            return

        if isinstance(target, ast.Subscript):
            # `pages.EXTRA["quakemap"] = render` - the registration that joins an
            # extension's renderer to the app, and the reason a subscript store is
            # recorded as a mutation of what it is a subscript of.
            base = describe(target.value)
            scope.mutations.append((base, "__setitem__", target.lineno, loops,
                                    literal_of(target.slice),
                                    describe(value) if value is not None else None))
            self.expression(target.value, scope, loops)
            self.expression(target.slice, scope, loops)
            return

        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self.store_target(element, scope, loops, top_level, None)
            return

        if isinstance(target, ast.Starred):
            self.store_target(target.value, scope, loops, top_level, None)

    def note_local_bind(self, target, scope, op, value):
        """Where a local name's value came from, as far as a dispatch table needs.

        `handler = EXTRA.get(kind) or _KINDS.get(kind)` and `candidates = [Portable]`
        followed by `candidates.append(...)` are the two shapes that matter, and both
        end with the name being called or iterated.
        """
        if not isinstance(target, ast.Name) or value is None:
            return
        # `app = App(...)` then `app.tick()` is how the badge's whole frame loop is
        # reached, and how most methods are called anywhere: without the local's type
        # every one of them looks unreferenced.
        if isinstance(value, ast.Call):
            scope.local_types.append((target.id, describe(value.func), target.lineno))
        sources = value_sources(value)
        if sources:
            scope.local_binds.append((target.id, target.lineno, op, sources))

    def bind(self, name, line, value):
        record = self.module.bindings.get(name)
        if record is None:
            self.module.bindings[name] = {
                "line": line, "assigns": 1,
                "shape": value_shape(value), "value": literal_summary(value),
                "members": display_members(value),
                # What the value was worked out from. `look.DIAL_C` comes from DIAL_GAP,
                # DIAL_OUTER and BODY_TOP, and two extensions then read DIAL_C at module
                # scope - a chain that is nowhere in the source as anything but arithmetic.
                "reads": names_in(value),
            }
            return
        record["assigns"] += 1
        if record["shape"] == "empty" and value is not None:
            record["shape"] = value_shape(value)

    def expression(self, node, scope, loops):
        if node is None or not isinstance(node, ast.AST):
            return

        if isinstance(node, ast.Call):
            self.call(node, scope, loops)
            return

        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            scope.loads.append(((NAME, node.id), node.lineno, loops))
            return

        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            scope.loads.append((describe(node), node.lineno, loops))
            self.expression(node.value, scope, loops)
            return

        if isinstance(node, (ast.List, ast.Dict, ast.Set)):
            if any(ast.iter_child_nodes(node)):
                self.allocation(node, scope, loops, "container")
        elif isinstance(node, ast.Tuple) and node.elts:
            self.allocation(node, scope, loops, "tuple")
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                               ast.GeneratorExp)):
            scope.branches += len(
                [1 for gen in node.generators for _ in gen.ifs])
            self.allocation(node, scope, loops, "comprehension")
            for generator in node.generators:
                self.store_target(generator.target, scope, loops, False, None)
        elif isinstance(node, ast.JoinedStr):
            self.allocation(node, scope, loops, "f-string")
        elif isinstance(node, ast.BoolOp):
            scope.branches += max(0, len(node.values) - 1)
        elif isinstance(node, ast.IfExp):
            scope.branches += 1
        elif isinstance(node, ast.Lambda):
            self.expression(node.body, scope, loops)
            return

        for child in ast.iter_child_nodes(node):
            self.expression(child, scope, loops)

    def allocation(self, node, scope, loops, what):
        scope.allocs += 1
        scope.alloc_sites.append((node.lineno, loops, what))

    def call(self, node, scope, loops):
        shape = describe(node.func)
        # The arguments come along because a function handed to something is an edge too:
        # argparse's `set_defaults(func=cmd_serve)` is the whole CLI, and a thread's
        # `target=` is where a source's fetch loop actually starts.
        scope.calls.append({
            "shape": shape, "line": node.lineno, "loops": loops,
            "args": [describe(argument) for argument in node.args],
            "kwargs": {keyword.arg: describe(keyword.value)
                       for keyword in node.keywords if keyword.arg},
        })

        name = flat_name(node.func)
        attr = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if name in ALLOCATORS or attr in ALLOCATING_ATTRS:
            self.allocation(node, scope, loops, name or attr)
        if attr in MUTATORS:
            base = describe(node.func.value)
            first = node.args[0] if node.args else None
            scope.mutations.append((base, attr, node.lineno, loops,
                                    literal_of(first),
                                    describe(first) if first is not None else None))
        if name and name[:1].isupper():
            scope.instantiations.append((shape, node.lineno, loops))

        for child in ast.iter_child_nodes(node.func):
            self.expression(child, scope, loops)
        for argument in node.args:
            self.expression(argument, scope, loops)
        for keyword in node.keywords:
            self.expression(keyword.value, scope, loops)


# -- expression shapes ------------------------------------------------------


def describe(node):
    """The shape of an expression, in the tuple form callgraph_resolve reads."""
    if isinstance(node, ast.Name):
        return (NAME, node.id)
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            return (SELF, node.attr)
        return (ATTR, describe(node.value), node.attr)
    if isinstance(node, ast.Call):
        return (CALLED, describe(node.func))
    if isinstance(node, ast.Subscript):
        return describe(node.value)
    return OPAQUE_SHAPE


def flat_name(node):
    """A dotted name as one string, or None for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = flat_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def decorator_name(node):
    name = flat_name(node.func if isinstance(node, ast.Call) else node)
    return (name or "").rsplit(".", 1)[-1]


def signature_of(node):
    try:
        return f"({ast.unparse(node.args)})"
    except (AttributeError, ValueError):
        return "(...)"


def param_names(args):
    """Every name a call binds, so a load of one is not read as a module-level name."""
    names = {arg.arg for arg in args.posonlyargs + args.args + args.kwonlyargs}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def generator_in(node):
    """Whether this function body yields, not counting any nested one that does."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(child, (ast.Yield, ast.YieldFrom)):
            return True
        if generator_in(child):
            return True
    return False


def value_shape(node):
    """What a module-level binding was bound to, as far as classification cares.

    `empty` is the one that earns its place: a module-level `{}` or `set()` that another
    module fills is an open registry, which is how every extension reaches the app.
    """
    if node is None:
        return "unknown"
    if isinstance(node, (ast.Dict, ast.Set, ast.List, ast.Tuple)):
        return "empty" if not any(ast.iter_child_nodes(node)) else "display"
    if isinstance(node, ast.Call):
        name = flat_name(node.func)
        if name in ("set", "dict", "list") and not node.args:
            return "empty"
        return "call"
    if isinstance(node, ast.Constant):
        return "none" if node.value is None else "literal"
    if isinstance(node, (ast.Name, ast.Attribute)):
        return "alias"
    return "expression"


def display_members(node):
    """The names a display holds, for a table of callables to be recognised by.

    A dict of bare names is `pages._KINDS` and `commands.REGISTRY`; a list of them is
    the candidate classes in `sources.discover`. Anything with a non-name in it is not
    a dispatch table, so the whole thing is dropped rather than half-recorded.
    """
    if isinstance(node, ast.Dict):
        pairs = []
        for key, value in zip(node.keys, node.values, strict=False):
            shape = describe(value)
            if not names_something(shape) or value is None:
                return []
            pairs.append((literal_of(key), shape))
        return pairs
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        pairs = []
        for element in node.elts:
            shape = describe(element)
            if not names_something(shape):
                return []
            pairs.append((None, shape))
        return pairs
    return []


def names_something(shape):
    """Whether a shape refers to a definition rather than to a value.

    A dispatch table holds the functions themselves - `{"dial": _dial}` - where
    `{"dark": Theme(...)}` holds what calling one returned. Only the first is something
    to follow a call through.
    """
    return shape[0] in (NAME, ATTR, SELF)


def value_sources(node):
    """Where a value could have come from, as `("member", shape)`, `("alias", shape)`
    or `("display", members)`.

    Only the forms a dispatch lookup takes: a `.get()` or a subscript off a table, an
    `or` of two of those, a display of names, or a plain alias. Anything else gives
    nothing back, because a guess here would put an invented edge on the graph.
    """
    if isinstance(node, ast.BoolOp):
        found = []
        for value in node.values:
            found.extend(value_sources(value))
        return found
    if isinstance(node, ast.IfExp):
        return value_sources(node.body) + value_sources(node.orelse)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "pop"):
            return [("member", describe(node.func.value))]
        return []
    if isinstance(node, ast.Subscript):
        return [("member", describe(node.value))]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        members = display_members(node)
        return [("display", members)] if members else []
    if isinstance(node, (ast.Name, ast.Attribute)):
        return [("alias", describe(node))]
    return []


def names_in(node):
    """Every name and attribute a value expression loads, as shapes."""
    if node is None:
        return []
    found = []
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
            shape = describe(child)
            if shape != OPAQUE_SHAPE:
                found.append(shape)
        elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            found.append((NAME, child.id))
    # An attribute load walks its own base too, so the same name arrives twice.
    return list(dict.fromkeys(found))


def literal_of(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, bool)):
        return node.value
    return None


def literal_summary(node, limit=60):
    """A module-level value as text, for the panel to show, or "" if it is not literal."""
    if node is None:
        return ""
    try:
        text = ast.unparse(node)
    except (AttributeError, ValueError):
        return ""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
