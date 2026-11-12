"""Wi-Fi Direct link to the TV.

Two ways to connect:
  NMLink      - through NetworkManager. The laptop usually ends up "in charge"
                (group owner) of the link. No setup needed.
  HelperLink  - "TV in charge" mode. A small root helper tells wpa_supplicant
                to let the TV be group owner (go_intent=0) and gets an address
                from the TV. Needs a one-time setup (setup.sh).

Both report progress through on_state(state, detail) on the main thread:
  "waiting_tv"   - asked the TV, waiting for it to accept
  "linked"       - link is up (detail = our IP address)
  "failed"       - detail = human readable reason
  "ended"        - link went away
"""
import json
import os
import subprocess
import threading

import gi

gi.require_version("NM", "1.0")
from gi.repository import GLib, NM  # noqa: E402

from wfd import WFD_IES  # noqa: E402

HELPER = "/usr/local/sbin/tv-cast-helper"


def wfd_sink_type(peer):
    """Return True if this Wi-Fi Direct peer is a Miracast display (sink)."""
    ies = peer.get_wfd_ies()
    data = ies.get_data() if ies else b""
    # subelement 0 (device info): id(1) len(2) info(2) ...
    if not data or len(data) < 5 or data[0] != 0:
        return False
    info = (data[3] << 8) | data[4]
    return (info & 0x3) in (1, 3)   # primary sink or dual-role


def helper_installed():
    return os.path.exists(HELPER)


class Finder:
    """Keeps a Wi-Fi Direct search running and reports Miracast TVs."""

    def __init__(self, on_change, log):
        self.on_change = on_change
        self.log = log
        self.client = NM.Client.new(None)
        self.dev = next((d for d in self.client.get_devices() if isinstance(d, NM.DeviceWifiP2P)), None)
        self.timer = None
        if self.dev:
            self.dev.connect("peer-added", lambda *a: self.on_change())
            self.dev.connect("peer-removed", lambda *a: self.on_change())

    def tvs(self):
        if not self.dev:
            return []
        return [p for p in self.dev.get_peers() if wfd_sink_type(p)]

    def start(self):
        if not self.dev or self.timer:
            return
        self._find()
        self.timer = GLib.timeout_add_seconds(25, self._find)

    def _find(self):
        def done(dev, res):
            try:
                dev.start_find_finish(res)
            except GLib.Error as e:
                self.log(f"search: {e.message}")
        self.dev.start_find(None, None, done)
        return True

    def stop(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        if self.dev:
            self.dev.stop_find(None, lambda d, r: _quiet(d.stop_find_finish, r))


def _quiet(fn, *a):
    try:
        fn(*a)
    except GLib.Error:
        pass


class NMLink:
    def __init__(self, finder, on_state, log):
        self.client = finder.client
        self.dev = finder.dev
        self.on_state = on_state
        self.log = log
        self.handler = None
        self.active = False

    def connect(self, peer):
        con = NM.SimpleConnection.new()
        s_con = NM.SettingConnection.new()
        s_con.set_property("id", "TV Cast")
        s_con.set_property("uuid", NM.utils_uuid_generate())
        s_con.set_property("type", "wifi-p2p")
        s_con.set_property("autoconnect", False)
        s_p2p = NM.SettingWifiP2P.new()
        s_p2p.set_property("peer", peer.get_hw_address())
        s_p2p.set_property("wfd-ies", GLib.Bytes.new(WFD_IES))
        s4 = NM.SettingIP4Config.new()
        s4.set_property("method", "auto")
        s4.set_property("never-default", True)
        s6 = NM.SettingIP6Config.new()
        s6.set_property("method", "auto")
        s6.set_property("never-default", True)
        s6.set_property("may-fail", True)
        for s in (s_con, s_p2p, s4, s6):
            con.add_setting(s)

        opts = GLib.Variant("a{sv}", {"persist": GLib.Variant("s", "volatile"),
                                      "bind-activation": GLib.Variant("s", "dbus-client")})
        self.handler = self.dev.connect("state-changed", self._state_changed)
        self.active = True

        def done(client, res):
            try:
                client.add_and_activate_connection2_finish(res)
            except GLib.Error as e:
                self._fail(f"NetworkManager refused: {e.message}")
        self.client.add_and_activate_connection2(con, self.dev, peer.get_path(), opts, None, done)
        self.log(f"Asking {peer.get_name()} ({peer.get_hw_address()}) to connect (normal mode)")

    def _state_changed(self, dev, new, old, reason):
        if not self.active:
            return
        s = NM.DeviceState
        self.log(f"link: {NM.DeviceState(old).value_nick} -> {NM.DeviceState(new).value_nick}"
                 f" ({NM.DeviceStateReason(reason).value_nick})")
        if new in (s.PREPARE, s.CONFIG):
            self.on_state("waiting_tv", None)
        elif new == s.ACTIVATED:
            ip = None
            cfg = dev.get_ip4_config()
            if cfg and cfg.get_addresses():
                ip = cfg.get_addresses()[0].get_address()
            self.on_state("linked", ip)
        elif new == s.FAILED:
            nick = NM.DeviceStateReason(reason).value_nick
            if nick == "supplicant-timeout":
                why = "The TV did not accept the connection in time."
            else:
                why = f"The connection failed ({nick})."
            self._fail(why)
        elif new == s.DISCONNECTED and old == s.ACTIVATED:
            self.active = False
            self.on_state("ended", None)

    def _fail(self, why):
        if self.active:
            self.active = False
            self.on_state("failed", why)

    def disconnect(self):
        self.active = False
        if self.handler:
            self.dev.disconnect(self.handler)
            self.handler = None
        self.dev.disconnect_async(None, lambda d, r: _quiet(d.disconnect_finish, r))


class HelperLink:
    """'TV in charge' mode through the root helper (see setup.sh)."""

    def __init__(self, on_state, log):
        self.on_state = on_state
        self.log = log
        self.proc = None

    def connect(self, peer):
        mac = peer.get_hw_address()
        self.log(f"Asking {peer.get_name()} ({mac}) to connect (TV in charge mode)")
        try:
            self.proc = subprocess.Popen(["sudo", "-n", HELPER, "connect", mac],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True)
        except OSError as e:
            self.on_state("failed", f"Could not start the helper: {e}")
            return
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        failed = False
        for line in proc.stdout:
            line = line.strip()
            try:
                ev = json.loads(line)
            except ValueError:
                GLib.idle_add(self.log, "helper: " + line)
                if "a password is required" in line:
                    failed = True
                    GLib.idle_add(self.on_state, "failed", "TV in charge mode is not set up yet.")
                continue
            GLib.idle_add(self.log, "helper: " + line)
            kind = ev.get("event")
            if kind == "waiting_tv":
                GLib.idle_add(self.on_state, "waiting_tv", None)
            elif kind == "linked":
                GLib.idle_add(self.on_state, "linked", ev.get("ip"))
            elif kind == "failed":
                failed = True
                GLib.idle_add(self.on_state, "failed", ev.get("message", "failed"))
            elif kind == "ended":
                GLib.idle_add(self.on_state, "ended", None)
        proc.wait()
        if proc is self.proc and not failed and proc.returncode not in (0, None, -15):
            GLib.idle_add(self.on_state, "failed", f"Helper stopped (code {proc.returncode}).")

    def disconnect(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()     # helper cleans up when its input closes
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.terminate()
        self.proc = None
