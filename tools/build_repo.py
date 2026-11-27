"""Build a Kodi add-on repository you can host on any web server.

    python3 tools/build_repo.py --url https://example.com/kodi/      # -> dist/repo/
    python3 tools/build_repo.py --publish                            # build, then rsync to `publish`

Settings come from the command line, else from repo.local.ini (gitignored, copy repo.example.ini).

Copy dist/repo/ to the web server so it is served at `url`. In Kodi:
  1. Settings > File manager > Add source > enter the url.
  2. Settings > Add-ons > Install from zip file > that source > repository.sendspin-<v>.zip
  3. Install from repository > Kodi-Sendspin repository > Services > Kodi-Sendspin
Kodi then updates the add-on by itself when a new version is published.
"""
import argparse
import configparser
import hashlib
import html
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from build_zip import ROOT, ADDON, addon_version, build_addon_zip

REPO_ID = "repository.sendspin"
REPO_VERSION = "1.0.0"  # bump when the repository add-on itself changes (url, name)
CONFIG = os.path.join(ROOT, "repo.local.ini")

REPO_ADDON_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="{id}" name="{name}" version="{version}" provider-name="{provider}">
    <extension point="xbmc.addon.repository" name="{name}">
        <dir>
            <info compressed="false">{url}addons.xml</info>
            <checksum>{url}addons.xml.md5</checksum>
            <datadir zip="true">{url}</datadir>
            <hashes>sha256</hashes>
        </dir>
    </extension>
    <extension point="xbmc.addon.metadata">
        <summary lang="en_GB">Updates for Kodi-Sendspin</summary>
        <description lang="en_GB">Installs and updates the Kodi-Sendspin add-on from {url}</description>
        <platform>all</platform>
        <assets>
            <icon>icon.png</icon>
        </assets>
    </extension>
</addon>
"""


def load_settings(args):
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG)
    sec = cfg["repo"] if cfg.has_section("repo") else {}
    url = args.url or sec.get("url", "")
    if not url:
        sys.exit("no url: pass --url or set it in %s (see repo.example.ini)" % CONFIG)
    if not url.startswith(("http://", "https://")):
        sys.exit("url must start with http:// or https://")
    return {
        "url": url.rstrip("/") + "/",
        "name": args.name or sec.get("name", "Kodi-Sendspin repository"),
        "provider": args.provider or sec.get("provider", "Kodi-Sendspin"),
        "publish": args.target or sec.get("publish", ""),
    }


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_indexes(out, title):
    """An index.html in every folder, listing everything in it (sub-folders end in "/").

    Kodi's file manager reads these as directory listings (link text must equal the href).
    It also caches a listing and then refuses to open any file missing from it, so a
    listing without addons.xml.md5 breaks the repository for the rest of the session.
    """
    for folder, dirs, files in os.walk(out):
        names = sorted(d + "/" for d in dirs) + sorted(f for f in files if f != "index.html")
        name = title if folder == out else os.path.relpath(folder, out)
        links = "\n".join('<a href="%s">%s</a><br>' % (html.escape(n), html.escape(n)) for n in names)
        write_text(os.path.join(folder, "index.html"),
                   '<!DOCTYPE html>\n<html><head><meta charset="utf-8"><title>%s</title></head>\n<body>\n'
                   '<h1>%s</h1>\n%s\n</body></html>\n' % (html.escape(name), html.escape(name), links))


def publish_addon(addon_dir, out):
    """Put <id>/<id>-<version>.zip (+ .sha256 and artwork) in ``out``. Returns (id, zip name)."""
    addon_id = os.path.basename(os.path.normpath(addon_dir))
    folder = os.path.join(out, addon_id)
    zip_path = build_addon_zip(addon_dir, folder)
    write_text(zip_path + ".sha256", sha256_file(zip_path) + "\n")
    # Kodi shows repository artwork from <datadir>/<id>/<asset path>
    for asset in ET.parse(os.path.join(addon_dir, "addon.xml")).getroot().iter("assets"):
        for el in asset:
            if el.text:
                src = os.path.join(addon_dir, el.text)
                dst = os.path.join(folder, el.text)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
    return addon_id, os.path.basename(zip_path)


def addons_xml(addon_dirs):
    parts = []
    for d in addon_dirs:
        with open(os.path.join(d, "addon.xml"), encoding="utf-8") as f:
            text = f.read()
        parts.append(text[text.index("<addon"):].rstrip())  # drop the <?xml ?> declaration
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n%s\n</addons>\n' % "\n".join(parts)


def build(settings, out):
    os.makedirs(out, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        # The repository add-on is generated, so the url never lives in the source tree.
        repo_dir = os.path.join(tmp, REPO_ID)
        os.makedirs(repo_dir)
        write_text(os.path.join(repo_dir, "addon.xml"), REPO_ADDON_XML.format(
            id=REPO_ID, version=REPO_VERSION, **{k: html.escape(v) for k, v in settings.items()}))
        shutil.copyfile(os.path.join(ADDON, "resources", "icon.png"), os.path.join(repo_dir, "icon.png"))

        published = [publish_addon(d, out) for d in (ADDON, repo_dir)]
        text = addons_xml([ADDON, repo_dir])
    write_text(os.path.join(out, "addons.xml"), text)
    write_text(os.path.join(out, "addons.xml.md5"), hashlib.md5(text.encode("utf-8")).hexdigest() + "\n")

    # Top-level copies so "Install from zip file" finds them without opening sub-folders.
    top = []
    for addon_id, zip_name in published:
        shutil.copyfile(os.path.join(out, addon_id, zip_name), os.path.join(out, zip_name))
        top.append(zip_name)
    write_indexes(out, "Kodi-Sendspin")
    return top


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="public URL the repository is served at")
    p.add_argument("--name", help="repository name shown in Kodi")
    p.add_argument("--provider", help="provider name of the repository add-on")
    p.add_argument("--out", default=os.path.join(ROOT, "dist", "repo"), help="output folder (default dist/repo)")
    p.add_argument("--clean", action="store_true", help="empty the output folder first (drops old versions)")
    p.add_argument("--publish", action="store_true", help="rsync the output folder to the `publish` target")
    p.add_argument("--target", help="rsync target, e.g. user@host:/var/www/kodi/")
    args = p.parse_args()
    settings = load_settings(args)
    if args.clean and os.path.isdir(args.out):
        shutil.rmtree(args.out)
    zips = build(settings, args.out)
    print("built %s for %s (service.sendspin %s)" % (args.out, settings["url"], addon_version(ADDON)))
    for z in zips:
        print("  ", z)
    if args.publish:
        if not settings["publish"]:
            sys.exit("no publish target: pass --target or set `publish` in %s" % CONFIG)
        cmd = ["rsync", "-rtv", args.out.rstrip("/") + "/", settings["publish"]]
        print(" ".join(cmd))
        subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
