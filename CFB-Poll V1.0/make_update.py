"""
make_update.py - builds an update package from the .py files in this folder.

    python make_update.py 1.0.1 "What changed in this version"
    python make_update.py 1.0.1 "What changed" https://github.com/YOU/REPO/releases/download/v1.0.1/

Creates, in a folder called  updates/ :
    update_1.0.1.zip   the package. Install it from the app's Updates tab.
    manifest.json      only needed for ONLINE updates. Upload it and the zip to your host.

The optional third argument is the web folder where you'll upload the zip, so the
manifest can point to it. Every package contains ALL updatable files (not just the
ones you changed), so installing it always gives a consistent set.
"""
import hashlib
import json
import sys
import zipfile
from pathlib import Path

from updater import BASE_VERSION, UPDATABLE

HERE = Path(__file__).resolve().parent


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    version, notes = sys.argv[1], sys.argv[2]
    prefix = sys.argv[3] if len(sys.argv) > 3 else ""
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    missing = [f"{m}.py" for m in UPDATABLE if not (HERE / f"{m}.py").exists()]
    if missing:
        raise SystemExit("Missing from this folder: " + ", ".join(missing))

    out = HERE / "updates"
    out.mkdir(exist_ok=True)
    zip_name = f"update_{version}.zip"
    inner = {"version": version, "notes": notes, "requires_base": BASE_VERSION}
    with zipfile.ZipFile(out / zip_name, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(inner, indent=2))
        for m in UPDATABLE:
            zf.write(HERE / f"{m}.py", f"{m}.py")

    sha = hashlib.sha256((out / zip_name).read_bytes()).hexdigest()
    outer = {
        "version": version,
        "notes": notes,
        "zip_url": prefix + zip_name,
        "sha256": sha,
        "requires_base": BASE_VERSION,
    }
    (out / "manifest.json").write_text(json.dumps(outer, indent=2), encoding="utf-8")

    print(f"Built {out / zip_name}")
    print(f"Checksum: {sha}")
    print("\nTo install by hand: Updates tab > 'Install update from file...' > pick the zip.")
    if prefix:
        print("To publish online: upload BOTH update_*.zip and manifest.json to " + prefix)
    else:
        print("For online updates, rerun with the web folder address as a third argument.")


if __name__ == "__main__":
    main()
