"""
updater.py - update the program without rebuilding the .exe.

How it works
------------
The .exe contains a built-in copy of every module. An UPDATE PACKAGE is a small
.zip holding newer copies of the six updatable modules (see UPDATABLE). Installing
one unpacks it into  %APPDATA%\\CFBPoll\\code  and, from the next start, launcher.py
loads those files INSTEAD of the built-in ones.

Safety net
----------
  * A package is checked before install: right files, no folders, valid Python syntax,
    and (for online downloads) a matching SHA-256 checksum.
  * If an installed update fails to start, it is moved aside automatically and the
    built-in version runs instead. The reason is saved to update_error.log.
  * "Undo last update" and "Revert to built-in version" are always available.

What updates CAN'T do
---------------------
  * Change this file, launcher.py or app_config.py (the "core") - that needs a one-time
    rebuild with build_exe.bat. If you change the core, raise BASE_VERSION so older
    builds refuse updates that need the new core.
  * Add new third-party libraries (numpy and requests are already built in).

Security note: an update is code that runs on your computer. Only install packages
you made or trust, and only use an online address you control.
"""
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
import zipfile
from pathlib import Path

import requests

from app_config import APP_DIR

BASE_VERSION = "1.0.0"     # version of the built-in code + core. Raise when the core changes.

# Load order matters: each module only imports ones earlier in this list (or the core).
UPDATABLE = ["efficiency", "ratings", "backtest", "cfb_data", "cfb_stats", "cfb_poll_app"]

CODE_DIR = APP_DIR / "code"
BACKUP_DIR = APP_DIR / "code_backup"
STAGING_DIR = APP_DIR / "code_staging"
FAILED_DIR = APP_DIR / "code_failed"
ERROR_LOG = APP_DIR / "update_error.log"
MAX_ZIP_BYTES = 10 * 1024 * 1024

_running = BASE_VERSION
_active = False


class UpdateError(Exception):
    """A problem the user should see in plain words."""


# ---------------- Versions ----------------
def parse_version(v):
    parts = [int(p) for p in re.findall(r"\d+", str(v))] or [0]
    return tuple((parts + [0] * 4)[:4])


def _read_manifest(directory):
    try:
        return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def installed_version():
    """Version of the update package on disk (what you'll get after a restart)."""
    m = _read_manifest(CODE_DIR) if CODE_DIR.exists() else None
    return str(m["version"]) if m and m.get("version") else BASE_VERSION


def running_version():
    """Version of the code actually running right now."""
    return _running


def overlay_active():
    return _active


# ---------------- Startup: load installed update over the built-in code ----------------
def rollback(error_text):
    """Move the installed update aside and record why."""
    manifest = _read_manifest(CODE_DIR) or {}
    shutil.rmtree(FAILED_DIR, ignore_errors=True)
    try:
        CODE_DIR.rename(FAILED_DIR)
    except OSError:
        shutil.rmtree(CODE_DIR, ignore_errors=True)
    try:
        ERROR_LOG.write_text(
            f"Update {manifest.get('version', '?')} failed to start and was rolled back.\n\n{error_text}",
            encoding="utf-8",
        )
    except OSError:
        pass


def activate_overlay():
    """
    Call once at startup, BEFORE anything imports an updatable module.
    Returns None normally, or the error text if the update had to be rolled back.
    """
    global _running, _active
    if not CODE_DIR.exists():
        return None
    manifest = _read_manifest(CODE_DIR) or {}
    try:
        for name in UPDATABLE:
            path = CODE_DIR / f"{name}.py"
            if not path.exists():
                continue
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module        # registered first so later imports find it
            spec.loader.exec_module(module)
        _running = str(manifest.get("version", BASE_VERSION))
        _active = True
        return None
    except Exception:
        error = traceback.format_exc()
        for name in UPDATABLE:                # forget the half-loaded update; built-in code takes over
            sys.modules.pop(name, None)
        rollback(error)
        return error


def pop_startup_error():
    """Text of the last rollback (if any), cleared after reading."""
    try:
        text = ERROR_LOG.read_text(encoding="utf-8")
        ERROR_LOG.unlink()
        return text
    except OSError:
        return ""


# ---------------- Installing ----------------
def install_bytes(data, expected_sha256=None):
    if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256.strip().lower():
        raise UpdateError("The download doesn't match its checksum, so it was NOT installed.")
    if len(data) > MAX_ZIP_BYTES:
        raise UpdateError("That update file is unexpectedly large, so it was not installed.")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UpdateError("That file isn't a valid update package (.zip).")

    names = [i.filename for i in zf.infolist() if not i.is_dir()]
    for n in names:
        if "/" in n or "\\" in n or n.startswith("."):
            raise UpdateError(
                f"'{n}' is inside a folder in the zip. The files must sit at the top level of the zip.")
    expected = {f"{m}.py" for m in UPDATABLE} | {"manifest.json"}
    missing, extra = sorted(expected - set(names)), sorted(set(names) - expected)
    if missing:
        raise UpdateError("The update package is incomplete. Missing: " + ", ".join(missing))
    if extra:
        raise UpdateError("The package has files that can't be updated this way: " + ", ".join(extra)
                          + ". Core files need a new .exe build.")

    try:
        manifest = json.loads(zf.read("manifest.json"))
        version = str(manifest.get("version", "")).strip()
    except (ValueError, AttributeError):
        raise UpdateError("The package's manifest.json isn't valid.")
    if not version:
        raise UpdateError("The package's manifest.json has no version.")
    needs = manifest.get("requires_base", "0")
    if parse_version(BASE_VERSION) < parse_version(needs):
        raise UpdateError(
            f"This update needs a newer program core (v{needs}; yours is v{BASE_VERSION}). "
            "Rebuild the .exe once with build_exe.bat, then install the update.")
    for n in names:
        if n.endswith(".py"):
            try:
                compile(zf.read(n), n, "exec")
            except SyntaxError as e:
                raise UpdateError(f"{n} has a syntax error on line {e.lineno}: {e.msg}")

    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    STAGING_DIR.mkdir(parents=True)
    for n in names:
        (STAGING_DIR / n).write_bytes(zf.read(n))
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)
    if CODE_DIR.exists():
        CODE_DIR.rename(BACKUP_DIR)
    STAGING_DIR.rename(CODE_DIR)
    return manifest


def install_file(path):
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise UpdateError(f"Couldn't read that file: {e}")
    return install_bytes(data)


# ---------------- Online updates (optional) ----------------
def _https_only(url, what):
    if not str(url).lower().startswith("https://"):
        raise UpdateError(f"The {what} must start with https://")


def fetch_manifest(url):
    _https_only(url, "update address")
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        m = r.json()
    except (requests.RequestException, ValueError) as e:
        raise UpdateError(f"Couldn't read the update info: {e}")
    if not isinstance(m, dict) or not m.get("version") or not m.get("zip_url"):
        raise UpdateError("The update info file is missing 'version' or 'zip_url'.")
    return m


def check_online(url):
    """The manifest dict if a NEWER version is available, else None."""
    m = fetch_manifest(url)
    return m if parse_version(m["version"]) > parse_version(installed_version()) else None


def download_and_install(m):
    _https_only(m["zip_url"], "download address")
    sha = str(m.get("sha256", "")).strip().lower()
    if not sha:
        raise UpdateError("The update info has no checksum (sha256), so it won't be installed.")
    print(f"Downloading version {m['version']}...")
    try:
        r = requests.get(m["zip_url"], timeout=120)
        r.raise_for_status()
    except requests.RequestException as e:
        raise UpdateError(f"Download failed: {e}")
    manifest = install_bytes(r.content, expected_sha256=sha)
    print(f"Version {manifest['version']} installed.")
    return manifest


# ---------------- Undo / revert / restart ----------------
def undo_last_update():
    """Swap the installed update with the previous one (press again to redo). False if none."""
    if not BACKUP_DIR.exists():
        return False
    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    if CODE_DIR.exists():
        CODE_DIR.rename(STAGING_DIR)
    BACKUP_DIR.rename(CODE_DIR)
    if STAGING_DIR.exists():
        STAGING_DIR.rename(BACKUP_DIR)
    return True


def revert_to_builtin():
    """Stop using installed updates. The reverted version is kept so 'Undo' can bring it back."""
    if not CODE_DIR.exists():
        return
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)
    CODE_DIR.rename(BACKUP_DIR)


def relaunch():
    """Start a fresh copy of the program (the caller then closes the current one)."""
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"   # makes a one-file .exe unpack itself fresh
    cmd = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(sys.argv[0]).resolve())]
    subprocess.Popen(cmd, env=env, close_fds=True)
