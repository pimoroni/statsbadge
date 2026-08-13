"""The tray artwork, drawn by tools/icon.py."""

import os

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def load(attention=False, template=False):
    from PIL import Image
    name = "tray-template" if template else "tray"
    if attention:
        name += "-attention"
    with Image.open(os.path.join(ASSETS, f"{name}.png")) as art:
        return art.convert("RGBA")
