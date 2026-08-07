"""What a source worked out, kept between runs.

Settings are what a source is *told* - a place, a unit, an API key, all of them the user's
answers and all editable in the UI. This is the other half: what a source *found out*, and
would otherwise have to find out again on every launch. A resolved location, a refresh token,
a high-water mark.

Namespaced by source name and written here, so an extension never touches the config
directory: it asks for a value and sets one, and where that lands is the host's business.
One file per source rather than one file with a key each, so two of them writing at once
cannot cost a third its state.
"""

import json
import os
import threading

# The most keys a store will hold. A cache keyed by something a user types - a place
# name - grows by one on every typo, and no other store here is near a cap.
MAX_KEYS = 64


class Store:
    """A dict that persists. `Store()` keeps everything in memory and nothing on disk.

    Written whole on every set, which suits what it holds: a handful of small values, saved
    when something is learned rather than on a timer. Anything that will not serialise is
    refused before the store changes, so a store in memory always matches the one on disk.
    """

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
        """Store several, in one write."""
        with self._lock:
            merged = dict(self._data)
            merged.update(values)
            if len(merged) > MAX_KEYS:
                # The oldest first, since a dict keeps insertion order.
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
    """The store one source writes to, or an in-memory one where there is nowhere to write."""
    if not directory:
        return Store()
    return Store(os.path.join(directory, f"{_safe(name)}.json"))


def _safe(name):
    """A source name as a filename. Entry point names are tame, but this is a path."""
    kept = [c if c.isalnum() or c in "-_" else "_" for c in str(name)]
    return "".join(kept)[:64] or "source"


def _read(path):
    try:
        with open(path) as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def _write(path, payload):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(payload)
    os.replace(tmp, path)
