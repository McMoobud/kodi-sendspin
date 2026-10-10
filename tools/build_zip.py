"""Build an installable Kodi zip: dist/service.sendspin-<version>.zip

    python3 tools/build_zip.py
Install in Kodi: Settings > Add-ons > Install from zip file.
"""
import os
import re
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON = os.path.join(ROOT, "service.sendspin")
version = re.search(r'<addon[^>]*version="([^"]+)"', open(os.path.join(ADDON, "addon.xml")).read()).group(1)
os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
out = os.path.join(ROOT, "dist", "service.sendspin-%s.zip" % version)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for base, dirs, files in os.walk(ADDON):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".pyc"):
                continue
            full = os.path.join(base, f)
            z.write(full, os.path.relpath(full, ROOT))
print(out, os.path.getsize(out), "bytes")
