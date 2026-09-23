"""Fetching from a web API, for a source's fetch thread."""

import json
import urllib.error
import urllib.request

from .. import version
from .base import SourceError

USER_AGENT = f"statsbadge/{version()} (+https://github.com/pimoroni/statsbadge)"


def fetch_json(url, headers=None, data=None, timeout=15, explain=None):
    """Return the JSON at `url`, POSTing `data` where it is given.

    An HTTP error raises SourceError worded by `explain(error, body)`, `body` being the
    error's JSON or None. The default names the status and whatever the body says.
    """
    request = urllib.request.Request(url, data=data, headers={
        "User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SourceError((explain or said)(exc, _body_of(exc))) from exc


def fetch_bytes(url, timeout=15):
    """Return what is at `url`, such as a picture."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def said(exc, body):
    """Return an HTTP error as its status and whatever message its body carried."""
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("message") or body.get("error") or body.get("detail") or "")
    return f"HTTP {exc.code}" + (f": {detail}" if detail else "")


def _body_of(exc):
    try:
        return json.loads(exc.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
