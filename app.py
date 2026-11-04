"""Run the Marstek battery widget as a small desktop window."""

import multiprocessing
import os
import sys
from importlib import import_module
from pathlib import Path
from sys import platform

if __name__ == "__main__":
    multiprocessing.freeze_support()

# A windowed build has no console, and logging to a missing stream would crash.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from nicegui import ui
from nicegui import app

import_module("src.ui")
from src.ui import COMPACT_SIZE, DEFAULT_WINDOW_SIZE, MONITOR_HEIGHT, MONITOR_WIDTH, STORE

ICON_NAME = {"win32": "logo.ico", "darwin": "logo.icns"}.get(platform, "logo.png")


if __name__ in {"__main__", "__mp_main__"}:
    width = min(MONITOR_WIDTH, max(200, STORE.get_setting("window_width", DEFAULT_WINDOW_SIZE[0])))
    height = min(MONITOR_HEIGHT, max(200, STORE.get_setting("window_height", DEFAULT_WINDOW_SIZE[1])))
    if STORE.get_bool_setting("compact_mode", False):
        width, height = COMPACT_SIZE
    fullscreen = STORE.get_text_setting("window_mode", "windowed") == "fullscreen"
    app.native.window_args["min_size"] = (200, 60)
    app.native.window_args["on_top"] = (
        STORE.get_bool_setting("always_on_top", False) and not fullscreen)
    app.native.window_args["resizable"] = STORE.get_bool_setting("allow_resize", True)
    app.native.settings["DRAG_REGION_SELECTOR"] = ".page-content.compact .view-box"
    ui.run(host="127.0.0.1", native=True, window_size=(width, height), fullscreen=fullscreen, reload=False, title="Marstek Dashboard",
            favicon=Path(__file__).parent / "src" / "static" / ICON_NAME, reconnect_timeout=0)
