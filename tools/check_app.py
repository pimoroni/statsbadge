#!/usr/bin/env python3
"""Check the badge app is whole and parses, before it is packed.

The badge compiles these from source when the app is launched, where a syntax error
is a crash dialog and nothing else. Imports nothing: every module here expects the
badge's builtins.

    python3 tools/check_app.py src/statsbadge/badge_app
"""

import ast
import pathlib
import sys

WANTED = (
    "__init__.py",
    "icon.png",
    "draw.py",
    "look.py",
    "net.py",
    "pages.py",
    "setup.py",
)

# Names the badge injects into builtins from `badgeware`, plus the app's own modules.
BADGE_GLOBALS = {
    "screen", "display", "badge", "image", "shape", "brush", "color", "font",
    "vec2", "rect", "mat3", "State", "text", "clamp", "rnd", "frnd",
    "file_exists", "is_dir", "rtc", "run", "launch", "reset", "fatal_error",
    "loop", "HIRES", "LORES", "VSYNC", "LEFT", "CENTER", "RIGHT", "TOP",
    "MIDDLE", "BOTTOM", "CLIP", "ELLIPSES",
    # badgeware.badge sets these on builtins, so dir(builtins) does not list them.
    "BUTTON_A", "BUTTON_B", "BUTTON_C", "BUTTON_UP", "BUTTON_DOWN", "BUTTON_HOME",
}


def main(app):
    if not app.is_dir():
        return f"no such directory: {app}"

    missing = [name for name in WANTED if not (app / name).exists()]
    if missing:
        return "missing from {}: {}".format(app, ", ".join(missing))

    for path in sorted(app.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as exc:
            return f"{path}:{exc.lineno}: {exc.msg}"
        fault = check_names(path, tree)
        if fault:
            return fault
        print(f"parsed {path}")

    extra = sorted(p.name for p in app.iterdir()
                   if p.name not in WANTED and p.name != "__pycache__")
    if extra:
        print("also packing: {}".format(", ".join(extra)))
    return None


def check_names(path, tree):
    """Catch a name that is neither defined here, imported, nor a badge builtin.

    Worth doing because these modules cannot be imported on the host to find out,
    and a NameError on the badge is a crash dialog after the app has launched.
    """
    import builtins

    defined = set(dir(builtins)) | BADGE_GLOBALS | {"__name__", "__file__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                defined.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
            for arg in getattr(node, "args", ast.arguments(
                    posonlyargs=[], args=[], kwonlyargs=[], defaults=[],
                    kw_defaults=[], vararg=None, kwarg=None)).args:
                defined.add(arg.arg)
            args = getattr(node, "args", None)
            if args:
                for arg in list(args.posonlyargs) + list(args.kwonlyargs):
                    defined.add(arg.arg)
                if args.vararg:
                    defined.add(args.vararg.arg)
                if args.kwarg:
                    defined.add(args.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store,)):
            defined.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.comprehension,)):
            for name in ast.walk(node.target):
                if isinstance(name, ast.Name):
                    defined.add(name.id)
        elif isinstance(node, ast.Global):
            defined.update(node.names)

    used = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.setdefault(node.id, node.lineno)
    unknown = sorted((name, line) for name, line in used.items()
                     if name not in defined)
    if unknown:
        name, line = unknown[0]
        return (f"{path}:{line}: name {name!r} is not defined, imported or a "
                "badge builtin")
    return None


if __name__ == "__main__":
    fault = main(pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else
                  "src/statsbadge/badge_app"))
    if fault:
        sys.exit(fault)
    print("app looks packable")
