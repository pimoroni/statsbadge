"""What a source worked out, kept between runs."""

import json
import os
import threading

# The most keys in a store. A cache keyed by something a user types grows by one on
# every typo.
MAX_KEYS = 64


class Store:
    """A dict that persists. `Store()` keeps everything in memory and nothing on disk."""

    def __init__(self, path=None):
        self.path = path
        self._lock = threading.Lock()
        self._data = _read(path) if path else {}

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value):
        """Store one value and write the file. Raises TypeError if it will not serialise."""
        self.update({key: value})

    def update(self, values):
        """Store several, in one write. A key set again moves to the end of the queue."""
        with self._lock:
            merged = dict(self._data)
            # Deleted before it is set, so a dict's insertion order is order of last use
            # and the key dropped at the cap is the one longest untouched.
            for key in values:
                merged.pop(key, None)
            merged.update(values)
            if len(merged) > MAX_KEYS:
                for key in list(merged)[:len(merged) - MAX_KEYS]:
                    del merged[key]
            payload = json.dumps(merged, indent=2, sort_keys=True)
            if self.path:
                _write(self.path, payload)
            self._data = merged

    def forget(self, key):
        with self._lock:
            if key not in self._data:
                return
            merged = dict(self._data)
            del merged[key]
            if self.path:
                _write(self.path, json.dumps(merged, indent=2, sort_keys=True))
            self._data = merged

    def all(self):
        with self._lock:
            return dict(self._data)

    def __repr__(self):
        return f"<store {self.path or 'in memory'}>"


def for_source(directory, name):
    """Return the store one source writes to, or an in-memory one with nowhere to write."""
    if not directory:
        return Store()
    return Store(os.path.join(directory, f"{_safe(name)}.json"))


def _safe(name):
    """Reduce a source name to a filename, this being a path."""
    kept = [c if c.isalnum() or c in "-_" else "_" for c in str(name)]
    return "".join(kept)[:64] or "source"


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def _write(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.replace(tmp, path)
