"""Sendspin Player service loop."""

import xbmc
import xbmcaddon

import kodi_bridge


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
                try:
                    bridge.tick()
                except Exception:  # noqa: BLE001 - never let the service loop die
                    kodi_bridge.log.exception("tick failed")
            elif addon.getSettingBool("enabled"):
                retry_at -= 1
                if retry_at <= 0:
                    break
        if bridge is not None:
            bridge.stop()

