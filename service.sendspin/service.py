"""Sendspin Player service entry point (runs at Kodi login)."""

import os
import sys

import xbmc
import xbmcaddon

sys.path.insert(0, os.path.join(xbmcaddon.Addon().getAddonInfo("path"), "resources", "lib"))

import kodi_bridge  # noqa: E402


class Monitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.settings_changed = False

    def onSettingsChanged(self):  # noqa: N802
        self.settings_changed = True


def run():
    monitor = Monitor()
    while not monitor.abortRequested():
        addon = xbmcaddon.Addon()
        kodi_bridge.ADDON = addon
        kodi_bridge.setup_logging(addon.getSettingBool("debug"))
        bridge = None
        if addon.getSettingBool("enabled"):
            url = kodi_bridge.resolve_server_url()
            if url:
                bridge = kodi_bridge.Bridge()
                bridge.start(url)
            else:
                kodi_bridge.notify("No Music Assistant found - set the server URL in settings", 8000)
        monitor.settings_changed = False
        retry_at = 60  # re-run discovery every minute if nothing was found
        while not monitor.abortRequested() and not monitor.settings_changed:
            if monitor.waitForAbort(1):
                break
            if bridge is not None:
                bridge.tick()
            elif addon.getSettingBool("enabled"):
                retry_at -= 1
                if retry_at <= 0:
                    break
        if bridge is not None:
            bridge.stop()


if __name__ == "__main__":
    run()
