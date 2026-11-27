"""Build an installable Kodi zip: dist/service.sendspin-<version>.zip

    python3 tools/build_zip.py
Install in Kodi: Settings > Add-ons > Install from zip file.
"""
import os
import re
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON = os.path.join(ROOT, "service.sendspin")


def addon_version(addon_dir):
    with open(os.path.join(addon_dir, "addon.xml"), encoding="utf-8") as f:
        return re.search(r'<addon[^>]*version="([^"]+)"', f.read()).group(1)


def build_addon_zip(addon_dir, out_dir):
    """Zip ``addon_dir`` as ``<out_dir>/<id>-<version>.zip`` (top folder = add-on id). Returns the path."""
    addon_id = os.path.basename(os.path.normpath(addon_dir))
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "%s-%s.zip" % (addon_id, addon_version(addon_dir)))
    parent = os.path.dirname(os.path.normpath(addon_dir))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(addon_dir):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for f in sorted(files):
                if f.endswith(".pyc"):
                    continue
                full = os.path.join(base, f)
                z.write(full, os.path.relpath(full, parent))
    return out


if __name__ == "__main__":
    path = build_addon_zip(ADDON, os.path.join(ROOT, "dist"))
    print(path, os.path.getsize(path), "bytes")
