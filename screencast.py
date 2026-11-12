"""Ask GNOME (xdg-desktop-portal) for permission to capture the screen.

The first time, GNOME shows a "Share your screen" dialog. We store the
restore token so later casts don't ask again.
"""
import os

from gi.repository import Gio, GLib

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJ_PATH = "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.ScreenCast"
TOKEN_FILE = os.path.expanduser("~/.config/tv-cast/restore-token")


class ScreenCast:
    def __init__(self, log):
        self.log = log
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")
        self.proxy = Gio.DBusProxy.new_sync(self.bus, Gio.DBusProxyFlags.NONE, None,
                                            BUS_NAME, OBJ_PATH, IFACE, None)
        self.session = None
        self.n = 0
        self.fd = None

    def _token(self):
        self.n += 1
        return f"tvcast{os.getpid()}_{self.n}"

    def _call(self, method, params, on_response):
        """Call a portal method that answers through a Request object's Response signal."""
        token = self._token()
        req_path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"
        sub = None

        def handler(conn, sender, path, iface, signal, args):
            conn.signal_unsubscribe(sub)
            code, results = args.unpack()
            on_response(code, results)

        sub = self.bus.signal_subscribe(BUS_NAME, "org.freedesktop.portal.Request", "Response",
                                        req_path, None, Gio.DBusSignalFlags.NO_MATCH_RULE, handler)
        params = params(token)
        try:
            self.proxy.call_sync(method, params, Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error as e:
            self.bus.signal_unsubscribe(sub)
            raise RuntimeError(f"{method}: {e.message}")

    def start(self, on_ready, on_error):
        """on_ready(fd, node_id) or on_error(message), both on the main thread."""
        def fail(msg):
            self.close()
            on_error(msg)

        def created(code, results):
            if code != 0:
                return fail("Screen sharing was not allowed.")
            self.session = results["session_handle"]
            cursor_modes = self.proxy.get_cached_property("AvailableCursorModes")
            version = self.proxy.get_cached_property("version")
            opts = {
                "handle_token": None,
                "types": GLib.Variant("u", 1),            # monitors
                "multiple": GLib.Variant("b", False),
                "cursor_mode": GLib.Variant("u", 2 if cursor_modes and cursor_modes.unpack() & 2 else 1),
            }
            if version and version.unpack() >= 4:
                opts["persist_mode"] = GLib.Variant("u", 2)   # remember until revoked
                token = _load_token()
                if token:
                    opts["restore_token"] = GLib.Variant("s", token)

            def params(tok):
                o = dict(opts)
                o["handle_token"] = GLib.Variant("s", tok)
                return GLib.Variant("(oa{sv})", (self.session, o))
            try:
                self._call("SelectSources", params, selected)
            except RuntimeError as e:
                fail(str(e))

        def selected(code, results):
            if code != 0:
                return fail("No screen was chosen.")
            try:
                self._call("Start", lambda tok: GLib.Variant(
                    "(osa{sv})", (self.session, "", {"handle_token": GLib.Variant("s", tok)})), started)
            except RuntimeError as e:
                fail(str(e))

        def started(code, results):
            if code != 0:
                return fail("Screen sharing was cancelled.")
            if results.get("restore_token"):
                _save_token(results["restore_token"])
            streams = results.get("streams") or []
            if not streams:
                return fail("GNOME gave no screen to capture.")
            node_id = streams[0][0]
            try:
                ret, fds = self.proxy.call_with_unix_fd_list_sync(
                    "OpenPipeWireRemote", GLib.Variant("(oa{sv})", (self.session, {})),
                    Gio.DBusCallFlags.NONE, -1, None, None)
                self.fd = fds.get(ret.unpack()[0])
            except GLib.Error as e:
                return fail(f"Could not open the screen stream: {e.message}")
            self.log(f"Screen capture ready (PipeWire node {node_id})")
            on_ready(self.fd, node_id)

        try:
            self._call("CreateSession", lambda tok: GLib.Variant("(a{sv})", ({
                "handle_token": GLib.Variant("s", tok),
                "session_handle_token": GLib.Variant("s", tok + "s"),
            },)), created)
        except RuntimeError as e:
            fail(str(e))

    def close(self):
        if self.session:
            try:
                self.bus.call_sync(BUS_NAME, self.session, "org.freedesktop.portal.Session", "Close",
                                   None, None, Gio.DBusCallFlags.NONE, -1, None)
            except GLib.Error:
                pass
            self.session = None
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


def _load_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _save_token(token):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        f.write(token)
