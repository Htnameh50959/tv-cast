"""TV Cast: show this laptop's screen on an LG webOS TV over the home Wi-Fi."""
import json
import os
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

import audio  # noqa: E402
import netcast  # noqa: E402
from screencast import ScreenCast  # noqa: E402

APP_ID = "local.hemanth.TVCast"
LOG_DIR = os.path.expanduser("~/.cache/tv-cast")

PENDING, ACTIVE, DONE, FAILED = range(4)

# label, width, height, fps, kbit/s (video) or JPEG quality (live), mode
QUALITIES = [
    ("Smooth video — exactly 2 s behind (recommended)", 1280, 720, 30, 6000, "video"),
    ("Live — no delay, but jerky video (~11 fps on this TV)", 1280, 720, 30, 90, "live"),
    ("Live — no delay, lighter pictures", 1280, 720, 30, 70, "live"),
    ("Smooth video 1080p — exactly 2 s behind, may stutter", 1920, 1080, 30, 10000, "video"),
]

OUTPUTS = [("Laptop speakers", "laptop"), ("TV speakers", "tv"), ("Bluetooth", "bluetooth")]
MATCH_MS = round(netcast.DELAY * 1000)          # sound delay that matches the TV picture
SETTINGS_FILE = os.path.expanduser("~/.config/tv-cast/settings.json")


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(**kv):
    st = load_settings()
    st.update(kv)
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(st, f)
    except OSError:
        pass


class Step:
    def __init__(self, title, subtitle):
        self.default = subtitle
        self.row = Adw.ActionRow(title=title, subtitle=subtitle, use_markup=False)
        self.row.set_subtitle_lines(4)
        self.icon = Gtk.Image(pixel_size=20)
        self.spinner = Gtk.Spinner()
        self.stack = Gtk.Stack(valign=Gtk.Align.CENTER)
        self.stack.add_named(self.icon, "icon")
        self.stack.add_named(self.spinner, "spin")
        self.row.add_prefix(self.stack)
        self.set(PENDING)

    def set(self, state, subtitle=None):
        self.state = state
        for c in ("dim-label", "success", "error"):
            self.icon.remove_css_class(c)
        if state == ACTIVE:
            self.spinner.start()
            self.stack.set_visible_child_name("spin")
        else:
            self.spinner.stop()
            self.stack.set_visible_child_name("icon")
            name, css = {PENDING: ("media-record-symbolic", "dim-label"),
                         DONE: ("emblem-ok-symbolic", "success"),
                         FAILED: ("dialog-error-symbolic", "error")}[state]
            self.icon.set_from_icon_name(name)
            self.icon.add_css_class(css)
        self.row.set_subtitle(subtitle if subtitle is not None else self.default)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="TV Cast", default_width=500, default_height=740)
        os.makedirs(LOG_DIR, exist_ok=True)
        self.logf = open(os.path.join(LOG_DIR, "last.log"), "w", buffering=1)

        self.phase = "idle"          # idle | starting | casting | stopping
        self.tv = None
        self.tvs = []
        self.scanning = False
        self.server = None
        self.remote = None
        self.left_timer = None
        self.route = None            # audio.SoundRoute while the sound is moved

        self.build_ui()
        try:
            audio.clean_up(self.log)
        except (OSError, audio.subprocess.SubprocessError) as e:
            self.log(f"Sound clean-up: {e}")
        self.screen = ScreenCast(self.log)
        self.scan()
        GLib.timeout_add_seconds(15, self._auto_scan)
        self.connect("close-request", self.on_close)

    # ------------------------------------------------------------ UI
    def build_ui(self):
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Search again")
        self.refresh_btn.connect("clicked", lambda *_: self.scan())
        header.pack_start(self.refresh_btn)
        toolbar.add_top_bar(header)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=560)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=24, margin_bottom=24, margin_start=16, margin_end=16)

        box.append(Gtk.Image(icon_name="video-display-symbolic", pixel_size=72))
        title = Gtk.Label(label="TV Cast")
        title.add_css_class("title-1")
        box.append(title)
        self.message = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                                 label="Turn the TV on. It shows up below when it's on the same Wi-Fi.")
        box.append(self.message)

        self.tv_group = Adw.PreferencesGroup(title="TVs on your Wi-Fi")
        self.searching_row = Adw.ActionRow(title="Searching…", subtitle="Make sure the TV is switched on")
        self.search_spin = Gtk.Spinner(spinning=True)
        self.searching_row.add_prefix(self.search_spin)
        self.tv_group.add(self.searching_row)
        self.tv_rows = []
        box.append(self.tv_group)

        opts = Adw.PreferencesGroup(title="Options")
        self.quality = Adw.ComboRow(title="Picture quality",
                                    model=Gtk.StringList.new([q[0] for q in QUALITIES]))
        opts.add(self.quality)
        self.opts_group = opts
        box.append(opts)

        # sound: where it plays, and how late (the delay can be changed while casting)
        st = load_settings()
        sound = Adw.PreferencesGroup(title="Sound", sensitive=audio.available(),
                                     description=None if audio.available() else "Needs pactl, pw-record and pw-play")
        self.outputs = OUTPUTS
        self.bluetooth_devices = audio.bluetooth_sinks()
        self.output = Adw.ComboRow(title="Sound plays on", model=Gtk.StringList.new([x[0] for x in self.outputs]))
        self.output.set_selected(self._output_index(st.get("output")))
        sound.add(self.output)
        self.bluetooth_output = Adw.ComboRow(title="Bluetooth device",
                                             model=Gtk.StringList.new(self._bluetooth_names()))
        self.bluetooth_output.set_selected(self._bluetooth_index(st.get("output")))
        self.bluetooth_output.set_sensitive(bool(self.bluetooth_devices))
        self.bluetooth_output.set_visible(self._output_kind() == "bluetooth")
        self.output.connect("notify::selected", self._output_changed)
        self.bluetooth_output.connect("notify::selected", self._bluetooth_changed)
        sound.add(self.bluetooth_output)
        self.delay_row = Adw.SpinRow.new_with_range(-15000, 15000, 50)
        self.delay_row.set_title("Sound delay (ms)")
        self.delay_row.set_subtitle(f"{MATCH_MS} = matches the TV picture · 0 = no delay · "
                                    "below 0: the TV picture waits longer, so the sound comes first")
        self.delay_row.set_value(st.get("delay_ms", MATCH_MS))
        self.delay_row.connect("notify::value", self._delay_changed)
        sound.add(self.delay_row)
        box.append(sound)

        self.steps = [
            Step("Screen sharing", "GNOME asks once which screen to share"),
            Step("Open on the TV", "The first time, press OK on the TV remote to allow TV Cast"),
            Step("Picture on TV", "Your screen appears on the TV"),
        ]
        self.progress = Adw.PreferencesGroup(title="Progress", visible=False)
        for s in self.steps:
            self.progress.add(s.row)
        box.append(self.progress)

        self.stop_btn = Gtk.Button(label="Stop casting", halign=Gtk.Align.CENTER, visible=False)
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.add_css_class("pill")
        self.stop_btn.connect("clicked", lambda *_: self.stop_cast("Casting stopped."))
        box.append(self.stop_btn)

        hint = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                         label="Tip: press OK on the TV remote once for full screen. "
                               "Pick where the sound plays under Options. The smooth-video picture is "
                               f"{netcast.DELAY:g} s behind the laptop.")
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        box.append(hint)

        self.textview = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                     wrap_mode=Gtk.WrapMode.CHAR)
        box.append(Gtk.Expander(label="Technical details",
                                child=Gtk.ScrolledWindow(min_content_height=200, child=self.textview)))

        clamp.set_child(box)
        scroll.set_child(clamp)
        toolbar.set_content(scroll)
        self.set_content(toolbar)

    def say(self, text):
        self.message.set_label(text)

    def log(self, text):
        def do():
            line = time.strftime("%H:%M:%S ") + text
            buf = self.textview.get_buffer()
            buf.insert(buf.get_end_iter(), line + "\n")
            self.logf.write(line + "\n")
            return False
        if threading.current_thread() is threading.main_thread():
            do()
        else:
            GLib.idle_add(do)

    # ------------------------------------------------------------ finding TVs
    def _auto_scan(self):
        if self.phase == "idle":
            self.scan()
        return True

    def scan(self):
        if self.scanning:
            return
        self._refresh_bluetooth_devices()
        self.scanning = True
        self.searching_row.set_title("Searching…")
        self.search_spin.set_spinning(True)

        def work():
            try:
                found = netcast.find_tvs()
            except Exception as e:
                self.log(f"search failed: {e}")
                found = []
            GLib.idle_add(self._scan_done, found)
        threading.Thread(target=work, daemon=True).start()

    def _scan_done(self, found):
        self.scanning = False
        last = netcast.last_tv()
        if last and not any(t["ip"] == last["ip"] for t in found):
            found.append(dict(last, missing=True))
        self.tvs = found
        self.refresh_tvs()
        return False

    def refresh_tvs(self):
        for r in self.tv_rows:
            self.tv_group.remove(r)
        self.tv_rows = []
        self.searching_row.set_visible(not self.tvs)
        if not self.tvs:
            self.search_spin.set_spinning(self.scanning)
            if not self.scanning:
                self.searching_row.set_title("No TV found yet")
        for tv in self.tvs:
            sub = tv["ip"] + ("  ·  not answering right now" if tv.get("missing") else "")
            row = Adw.ActionRow(title=tv["name"], subtitle=sub, use_markup=False)
            row.add_prefix(Gtk.Image(icon_name="video-display-symbolic"))
            btn = Gtk.Button(label="Cast", valign=Gtk.Align.CENTER)
            btn.add_css_class("suggested-action")
            btn.connect("clicked", lambda _b, t=tv: self.start_cast(t))
            btn.set_sensitive(self.phase == "idle")
            row.add_suffix(btn)
            self.tv_group.add(row)
            self.tv_rows.append(row)

    # ------------------------------------------------------------ casting flow
    def start_cast(self, tv):
        if self.phase != "idle":
            return
        self.phase = "starting"
        self.tv = tv
        self.fs_tried = False
        for s in self.steps:
            s.set(PENDING)
        self.progress.set_visible(True)
        self.stop_btn.set_visible(True)
        self.tv_group.set_sensitive(False)
        self.opts_group.set_sensitive(False)
        self.output.set_sensitive(False)
        self.bluetooth_output.set_sensitive(False)
        self.refresh_btn.set_sensitive(False)
        self.log(f"--- Casting to {tv['name']} ({tv['ip']})")
        self.steps[0].set(ACTIVE, "If GNOME asks, choose your screen and click Share.")
        self.say("Allow screen sharing…")
        self.screen.start(self.on_screen_ready, lambda m: self.fail(0, m))

    def on_screen_ready(self, fd, node):
        if self.phase != "starting":
            return
        _, w, h, fps, rate, mode = QUALITIES[self.quality.get_selected()]
        self.server = netcast.CastServer(self.log, lambda ev, d: GLib.idle_add(self.on_event, ev, d))
        self.server.mode = mode
        if mode == "live":
            self.server.jpeg_quality = rate
        else:
            self.server.bitrate = rate
        try:
            self._route_sound(mode)
            self.server.start(fd, node, w, h, fps)
        except (RuntimeError, OSError) as e:
            return self.fail(0, f"Could not start the video: {e}")
        self.steps[0].set(DONE, f"Allowed · {w}×{h} at {fps} fps")
        self.steps[1].set(ACTIVE, "Opening the TV's browser… Look at the TV: press OK if it asks.")
        self.say("Opening on the TV…")
        url = self.server.url_for(self.tv["ip"])
        self.remote = netcast.LGRemote(self.tv["ip"], self.log, lambda: GLib.idle_add(self._prompted))
        threading.Thread(target=self._open_on_tv, args=(url,), daemon=True).start()

    def _output_kind(self):
        selected = self.output.get_selected()
        return self.outputs[selected][1] if selected < len(self.outputs) else "laptop"

    def _output(self):
        """The selected destination, including a concrete Bluetooth sink."""
        output = self._output_kind()
        selected = self.bluetooth_output.get_selected()
        if output == "bluetooth" and selected < len(self.bluetooth_devices):
            return f"sink:{self.bluetooth_devices[selected][0]}"
        return output

    def _output_index(self, output):
        if isinstance(output, str) and output.startswith("sink:"):
            return next(i for i, choice in enumerate(self.outputs) if choice[1] == "bluetooth")
        return next((i for i, choice in enumerate(self.outputs) if choice[1] == output), 0)

    def _bluetooth_names(self):
        return [label for _name, label in self.bluetooth_devices] or ["No Bluetooth devices connected"]

    def _bluetooth_index(self, output):
        if not self.bluetooth_devices:
            return 0
        name = output.removeprefix("sink:") if isinstance(output, str) else ""
        return next((i for i, device in enumerate(self.bluetooth_devices) if device[0] == name), 0)

    def _output_changed(self, *_):
        self.bluetooth_output.set_visible(self._output_kind() == "bluetooth")
        save_settings(output=self._output())

    def _bluetooth_changed(self, *_):
        if self._output_kind() == "bluetooth":
            save_settings(output=self._output())

    def _refresh_bluetooth_devices(self):
        """Refresh the device sub-menu after a Bluetooth device changes state."""
        if not hasattr(self, "bluetooth_output"):
            return
        current = self._output()
        self.bluetooth_devices = audio.bluetooth_sinks()
        self.bluetooth_output.set_model(Gtk.StringList.new(self._bluetooth_names()))
        self.bluetooth_output.set_selected(self._bluetooth_index(current))
        self.bluetooth_output.set_sensitive(bool(self.bluetooth_devices) and self.phase == "idle")

    @staticmethod
    def _selected_sink(output):
        return output.removeprefix("sink:") if output.startswith("sink:") else None

    def _delay_ms(self):
        """How late the sound plays; below 0 it can't, so the TV picture is held back longer instead."""
        ms = int(self.delay_row.get_value())
        if self.server:
            self.server.picture_delay = netcast.DELAY + max(0, -ms) / 1000
        return ms

    def _route_sound(self, mode):
        ms = self._delay_ms()
        out, ms = (self._output(), ms) if audio.available() else ("laptop", 0)
        self.sound_mode = mode
        if out == "bluetooth":
            self.log("Sound: no Bluetooth device is connected — it plays on the laptop")
            out = "laptop"
        if out == "tv" and mode == "live":
            self.log("Sound: the TV can't play sound with the live picture — it plays on the laptop")
            out = "laptop"
        self.sound_out = out
        if out == "laptop" and ms <= 0:
            return                       # normal: sound plays on the laptop as it is
        self.route = audio.SoundRoute(self.log)
        try:
            self.route.start(None if out == "tv" else ms, self._selected_sink(out))
        except (OSError, audio.subprocess.SubprocessError) as e:
            self.log(f"Sound: {e} — it stays on the laptop")
            self.route = None
            self.sound_out = "laptop"
            return
        if out == "tv":
            self.server.audio_device = audio.MONITOR
            self.server.audio_delay_ms = ms

    def _delay_changed(self, *_):
        ms = self._delay_ms()
        save_settings(delay_ms=ms)
        if self.phase != "casting" or not self.server:
            return
        self.log(f"Picture: TV held {self.server.picture_delay:g} s behind the laptop")
        out = getattr(self, "sound_out", "laptop")
        if out == "tv":
            self.server.audio_delay_ms = ms          # the TV page picks it up and restarts the sound
            return
        try:
            if self.route and self.route.delay and (ms > 0 or self._selected_sink(out)):
                self.route.delay.set_ms(ms)
                self.log(f"Sound: delay {ms} ms" if ms > 0 else "Sound: no extra delay")
            elif self.route and ms <= 0 and not self._selected_sink(out):
                self.route.stop()
                self.route = None
            elif not self.route and (ms > 0 or self._selected_sink(out)):
                self.route = audio.SoundRoute(self.log)
                self.route.start(ms, self._selected_sink(out))
        except (OSError, audio.subprocess.SubprocessError) as e:
            self.log(f"Sound: {e}")

    def _sound_back_to_laptop(self, why):
        if getattr(self, "sound_out", "laptop") != "tv":
            return
        self.sound_out = "laptop"
        self.log(f"Sound: {why} — playing it on the laptop instead")
        if self.route:
            self.route.stop()
            self.route = None
        self._delay_changed()          # laptop speakers, with the chosen delay

    def _prompted(self):
        if self.phase == "starting":
            self.steps[1].set(ACTIVE, "The TV is asking: press OK on the TV remote.")
            self.say("Press OK on the TV remote.")
        return False

    def _open_on_tv(self, url):
        try:
            self.remote.open_url(url)
            netcast.remember_tv({"ip": self.tv["ip"], "name": self.tv["name"]})
            GLib.idle_add(self._opened, url)
        except Exception as e:
            msg = str(e)
            if "denied" in msg.lower():
                msg = "The TV said no. Click Cast again and press OK on the TV remote when it asks."
            elif "connect" in msg.lower() or "timed out" in msg.lower() or "refused" in msg.lower():
                msg = "Couldn't reach the TV. Is it switched on and on the same Wi-Fi?"
            GLib.idle_add(self._open_failed, msg, url)

    def _opened(self, url):
        if self.phase != "starting":
            return False
        self.steps[1].set(DONE, "The TV's browser is open")
        self.steps[2].set(ACTIVE, "Waiting for the picture…")
        self.say("Starting the picture…")
        GLib.timeout_add_seconds(25, self._picture_timeout)
        return False

    def _open_failed(self, msg, url):
        if self.phase != "starting":
            return False
        self.steps[1].set(FAILED, msg)
        self.steps[2].set(ACTIVE, f"You can also open {url} in the TV's web browser yourself.")
        self.say(msg)
        return False

    def _picture_timeout(self):
        if self.phase == "starting" and self.steps[2].state == ACTIVE:
            self.fail(2, "The TV opened the page but never showed the picture.")
        return False

    def on_event(self, ev, detail):
        if self.phase not in ("starting", "casting"):
            return False
        if ev in ("tv_playing", "tv_mjpeg"):
            if self.left_timer:
                GLib.source_remove(self.left_timer)
                self.left_timer = None
            self.phase = "casting"
            for s in self.steps[:2]:
                if s.state != DONE:
                    s.set(DONE)
            how = ("smooth video" if ev == "tv_playing" else
                   "live, no delay" if self.server and self.server.mode == "live" else
                   "picture mode (TV can't play live video)")
            self.steps[2].set(DONE, f"On the TV · {how}")
            self.say("Your screen is on the TV.")
            if not getattr(self, "fs_tried", False):
                self.fs_tried = True
                threading.Thread(target=self._make_fullscreen, daemon=True).start()
        elif ev == "tv_streaming" and self.left_timer:
            GLib.source_remove(self.left_timer)
            self.left_timer = None
        elif ev == "tv_left" and self.phase == "casting":
            if not self.left_timer:
                self.left_timer = GLib.timeout_add_seconds(8, self._tv_gone)
        elif ev in ("tv_no_audio", "tv_audio_blocked", "tv_audio_error", "tv_audio_append_error"):
            self._sound_back_to_laptop("the TV can't play the sound")
        elif ev == "error":
            self.fail(2, f"Video problem: {detail}")
        return False

    def _make_fullscreen(self):
        """Click on the TV page so the browser bar disappears (full screen)."""
        for attempt in range(2):
            time.sleep(1.0)
            server, remote = self.server, self.remote
            if not server or not remote or server.tv_fullscreen:
                break
            try:
                remote.click_center()
            except Exception as e:
                self.log(f"full screen click failed: {e}")
                return
            time.sleep(2.5)
            if server.tv_fullscreen:
                self.log("TV is full screen")
                return
        if self.server and not self.server.tv_fullscreen:
            self.log("TV did not go full screen — press OK on the TV remote once")

    def _tv_gone(self):
        self.left_timer = None
        self.stop_cast("The TV stopped showing the screen (browser closed or TV off).", close_tv=False)
        return False

    def fail(self, step, message):
        if self.phase in ("idle", "stopping"):
            return False
        self.steps[step].set(FAILED, message.split("\n")[0])
        self.stop_cast(message, failed=True)
        return False

    def stop_cast(self, message, failed=False, close_tv=True):
        if self.phase in ("idle", "stopping"):
            return
        self.phase = "stopping"
        if self.left_timer:
            GLib.source_remove(self.left_timer)
            self.left_timer = None
        if self.server:
            self.server.stop()
            self.server = None
        self.screen.close()
        if self.route:
            self.route.stop()
            self.route = None
        if self.remote and close_tv and not failed:
            threading.Thread(target=self.remote.close_browser, daemon=True).start()
        self.remote = None
        for s in self.steps:
            if s.state == ACTIVE:
                s.set(PENDING)
        self.stop_btn.set_visible(False)
        self.tv_group.set_sensitive(True)
        self.opts_group.set_sensitive(True)
        self.output.set_sensitive(True)
        self.bluetooth_output.set_sensitive(bool(self.bluetooth_devices))
        self.refresh_btn.set_sensitive(True)
        self.say(message)
        self.log("--- " + message.replace("\n", " "))
        self.phase = "idle"
        self.refresh_tvs()

    def on_close(self, *_):
        if self.phase != "idle":
            self.stop_cast("Closing")
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        (self.props.active_window or Window(self)).present()


def main():
    App().run(None)
