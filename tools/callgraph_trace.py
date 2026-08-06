#!/usr/bin/env python3
"""Record what actually gets called, for the graph to colour by instead of guessing.

    python3 tools/callgraph_trace.py --out build/trace-tests.json -- tests/test_core.py
    python3 tools/callgraph_trace.py --out build/trace-probe.json -- statsbadge probe

`sys.setprofile`, not `sys.monitoring`: .python-version pins 3.11. It is the better
instrument here anyway, firing on call and return only rather than on every line.

`tests/test_core.py` is the run worth having. It exercises nearly every host path, runs as
a plain script with no pytest, and already installs the badge fakes on builtins before
importing draw, look, pages and worldmap - so one command gives real counts for the whole
host tree and for the badge's drawing layer, which is the part where cost matters. It needs
no shims of its own.

Counts for the whole run, but only the first `--window` seconds of ordered events: a run
this size produces millions, and forty megabytes will not inline into a viewer. What was
dropped is printed rather than passed over.

The times are relative. Under a profiler this size of run is two to four times slower than
it really is, and the viewer says so wherever it shows them.
"""

import argparse
import json
import os
import pathlib
import runpy
import sys
import threading
import time

# Enough of the beginning to see a launch path, which is the part that does not repeat.
DEFAULT_WINDOW = 2.0
DEFAULT_EVENTS = 200_000


class Recorder:
    """Counts for the whole run, and the ordered events at the start of it."""

    def __init__(self, roots, window, cap):
        self.roots = [str(pathlib.Path(root).resolve()) for root in roots]
        self.window = window
        self.cap = cap
        self.calls = {}           # (file, firstlineno, name) -> count
        self.edges = {}           # (caller, callee) -> count
        self.events = []
        self.started = time.perf_counter_ns()
        self.dropped = 0
        self.marks = []
        self.lock = threading.Lock()

    def wanted(self, code):
        """Only code under the configured roots, so both the cost and the file stay small."""
        name = code.co_filename
        if not name or name[0] == "<":
            return False
        for root in self.roots:
            if name.startswith(root):
                return True
        return False

    def key(self, code):
        return (code.co_filename, code.co_firstlineno, code.co_name)

    def profile(self, frame, event, _arg):
        if event not in ("call", "return"):
            return
        code = frame.f_code
        if not self.wanted(code):
            return
        here = self.key(code)

        if event == "call":
            self.calls[here] = self.calls.get(here, 0) + 1
            back = frame.f_back
            if back is not None and self.wanted(back.f_code):
                pair = (self.key(back.f_code), here)
                self.edges[pair] = self.edges.get(pair, 0) + 1

        since = (time.perf_counter_ns() - self.started) // 1000
        if since > self.window * 1_000_000:
            return
        if len(self.events) >= self.cap:
            self.dropped += 1
            return
        self.events.append((since, 0 if event == "call" else 1, here))

    def mark(self, name):
        """A label the subject can drop in, so a timeline can be read in phases."""
        with self.lock:
            self.marks.append(((time.perf_counter_ns() - self.started) // 1000, name))

    def payload(self, name, subject):
        return {
            "name": name,
            "subject": subject,
            "under": "sys.setprofile",
            "overhead": "roughly 2-4x on call-heavy code, so the times are relative",
            "unit": "us",
            "window_s": self.window,
            "dropped_events": self.dropped,
            "marks": self.marks,
            "calls": [[file, line, fname, count]
                      for (file, line, fname), count in sorted(self.calls.items())],
            "edges": [[a[0], a[1], a[2], b[0], b[1], b[2], count]
                      for (a, b), count in sorted(self.edges.items())],
            "events": [[when, kind, where[0], where[1], where[2]]
                       for when, kind, where in self.events],
        }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Record real call counts for tools/callgraph.py to merge.")
    parser.add_argument("--out", required=True, type=pathlib.Path,
                        help="where to write the recording")
    parser.add_argument("--name", help="what to call it in the viewer")
    parser.add_argument("--root", action="append", default=[],
                        help="only record code under here, repeatable (default src)")
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW,
                        help=f"seconds of ordered events to keep (default {DEFAULT_WINDOW})")
    parser.add_argument("--events", type=int, default=DEFAULT_EVENTS,
                        help=f"most events to keep (default {DEFAULT_EVENTS})")
    parser.add_argument("subject", nargs=argparse.REMAINDER,
                        help="-- then a .py path or a module name, and its arguments")
    args = parser.parse_args(argv)

    subject = [word for word in args.subject if word != "--"]
    if not subject:
        return "nothing to run: pass -- then a script or a module name"

    roots = args.root or ["src", "extensions", "tests"]
    roots = [root for root in roots if pathlib.Path(root).exists()]
    if not roots:
        return "none of the roots to record under exist"

    recorder = Recorder(roots, args.window, args.events)
    name = args.name or pathlib.Path(subject[0]).stem

    code = run(subject, recorder)
    payload = recorder.payload(name, " ".join(subject))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=1))

    print(f"{len(payload['calls'])} functions, {len(payload['edges'])} edges, "
          f"{len(payload['events'])} ordered events")
    if recorder.dropped:
        print(f"  dropped {recorder.dropped} events past the cap of {args.events}; "
              f"the counts are still for the whole run")
    print(f"wrote {args.out}")
    if code:
        print(f"  note: the subject exited {code}, so this run may be incomplete")
    return 0


def run(subject, recorder):
    """Run the subject in this process with the profiler already installed.

    In-process because these subjects do not cooperate with being told to profile
    themselves, and `tests/test_core.py` runs perfectly well this way - it spawns its own
    threads and a real server, and `threading.setprofile` covers those.
    """
    first = subject[0]
    argv = list(subject)
    here = os.getcwd()

    saved_argv = sys.argv[:]
    saved_path = sys.path[:]
    sys.path.insert(0, here)
    sys.path.insert(0, str(pathlib.Path(here) / "src"))

    # A subject can call this to label a phase; it is a no-op when nothing is recording.
    import builtins
    builtins.callgraph_mark = recorder.mark

    # The server is a ThreadingMixIn and the collector runs a thread of its own, so without
    # the threading hook the whole of both would go unrecorded.
    threading.setprofile(recorder.profile)
    sys.setprofile(recorder.profile)
    code = 0
    try:
        if first.endswith(".py"):
            sys.argv = argv
            runpy.run_path(first, run_name="__main__")
        else:
            sys.argv = argv
            runpy.run_module(first, run_name="__main__", alter_sys=True)
    except SystemExit as exit_code:
        code = exit_code.code or 0
    except BaseException as exc:                                  # noqa: BLE001
        # A subject that fails still leaves a usable recording of what ran before it did.
        code = f"{type(exc).__name__}: {exc}"
    finally:
        sys.setprofile(None)
        threading.setprofile(None)
        sys.argv = saved_argv
        sys.path = saved_path
        del builtins.callgraph_mark
    return code


if __name__ == "__main__":
    sys.exit(main())
