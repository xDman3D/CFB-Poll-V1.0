"""
launcher.py - what the .exe actually runs.

1. Loads any installed update over the built-in code (see updater.py).
2. Starts the app.
3. If an update breaks the app on startup, rolls it back and restarts on the built-in version.
4. Windowed programs hide error messages, so a crash is saved to crash.log and shown in a box.

This file is part of the "core": changing it needs a one-time rebuild with build_exe.bat.
"""
import sys
import traceback

import app_config
import updater


def _show_crash(text):
    try:
        (app_config.APP_DIR / "crash.log").write_text(text, encoding="utf-8")
    except OSError:
        pass
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(
            "CFB Poll crashed",
            f"{text[-1500:]}\n\n(Saved to {app_config.APP_DIR / 'crash.log'})",
        )
        root.destroy()
    except Exception:
        pass


def main():
    updater.activate_overlay()          # must happen before anything imports an updatable module
    import cfb_poll_app

    try:
        cfb_poll_app.main()
    except Exception:
        if updater.overlay_active():    # the update is the likely culprit: undo it and start clean
            updater.rollback(traceback.format_exc())
            updater.relaunch()
            return
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _show_crash(traceback.format_exc())
        sys.exit(1)
