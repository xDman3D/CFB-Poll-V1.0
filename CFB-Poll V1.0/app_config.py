"""
app_config.py - where the app keeps its files, plus saved settings.

A packaged .exe unpacks itself into a temporary folder every time it runs, so the
database and settings can't live next to the code. They live in your user folder:

    Windows:  %APPDATA%\\CFBPoll\\        (cfb.db = all downloaded data, settings.json)

Set the CFBPOLL_HOME environment variable to use a different folder.
"""
import json
import os
import sys
import threading
from pathlib import Path


def _default_dir() -> Path:
    if os.environ.get("CFBPOLL_HOME"):
        return Path(os.environ["CFBPOLL_HOME"])
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "CFBPoll"
    return Path.home() / ".cfbpoll"


APP_DIR = _default_dir()
APP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = APP_DIR / "cfb.db"
SETTINGS_PATH = APP_DIR / "settings.json"

# Set by the UI's Cancel button; long-running loops check it between steps.
CANCEL = threading.Event()


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def apply_tuned_params() -> None:
    """Load any auto-tuned settings from settings.json into the rating engine."""
    import ratings

    for name, value in load_settings().get("params", {}).items():
        if hasattr(ratings, name):
            setattr(ratings, name, value)
