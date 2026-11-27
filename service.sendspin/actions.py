"""Settings buttons: RunScript(.../actions.py,show_token|forget_pairings).

Run by path, so Kodi gives no add-on context: everything here names the add-on id
explicitly and nothing imports kodi_bridge (its module-level xbmcaddon.Addon() would fail).
"""
import os
import sys

import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon("service.sendspin")
sys.path.insert(0, os.path.join(ADDON.getAddonInfo("path"), "resources", "lib"))

from sendspin.identity import Identity  # noqa: E402


def identity():
    """Same folder the service uses (kodi_bridge.identity)."""
    profile = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
    os.makedirs(profile, exist_ok=True)
    return Identity(profile)


def show_token():
    ident = identity()
    path = os.path.join(ident.folder, "pairing_token.txt")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # a credential: owner only
    with os.fdopen(fd, "w", encoding="utf-8") as f:  # easier to copy from a file than off the TV
        f.write(ident.token + "\n")
    xbmcgui.Dialog().textviewer(ADDON.getLocalizedString(30211), ADDON.getLocalizedString(30212) % (ident.token, path))


def forget_pairings():
    if xbmcgui.Dialog().yesno(ADDON.getAddonInfo("name"), ADDON.getLocalizedString(30213)):
        identity().forget_all()
        xbmcgui.Dialog().notification(ADDON.getAddonInfo("name"), ADDON.getLocalizedString(30214),
                                      ADDON.getAddonInfo("icon"), 4000, False)


ACTIONS = {"show_token": show_token, "forget_pairings": forget_pairings}

if __name__ == "__main__":
    action = ACTIONS.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if action:
        action()
