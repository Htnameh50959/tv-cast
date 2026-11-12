"""Where the laptop's sound goes while casting (same approach as PhoneLink's sound delay).

A silent virtual speaker ("TV Cast") becomes the default output and every app's sound
is moved into it. From there it is either
  - sent to the TV with the video (TV Cast records the virtual speaker's monitor), or
  - played into the real speakers DELAY seconds later by a DelayLine, so it matches
    the TV picture.
Stopping puts every app back on the speakers it came from.
"""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time

SINK = "tvcast_audio"
MONITOR = SINK + ".monitor"
PREFIXES = ("tvcast_", "phonelink_")      # virtual speakers / delay streams, never real speakers
F_SETPIPE_SZ = 1031


def _pactl(*args):
    return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5)


def available():
    return all(shutil.which(x) for x in ("pactl", "pw-record", "pw-play"))


def _sinks():
    try:
        return json.loads(_pactl("-f", "json", "list", "sinks").stdout or "[]")
    except ValueError:
        return []


def _sink_exists(name):
    return any(x.get("name") == name for x in _sinks())


def default_sink():
    return _pactl("get-default-sink").stdout.strip()


def real_sink():
    """The speakers/headphones themselves, never a virtual delay speaker in front of them."""
    d = default_sink()
    if not d.startswith(PREFIXES):
        return d
    if d.startswith("phonelink_"):
        # PhoneLink's delay is on: use the speakers its delay plays into
        try:
            inputs = json.loads(_pactl("-f", "json", "list", "sink-inputs").stdout or "[]")
        except ValueError:
            inputs = []
        ids = {str(x.get("index")): x.get("name") for x in _sinks()}
        for i in inputs:
            if (i.get("properties") or {}).get("node.name") == "phonelink_delay_out":
                name = ids.get(str(i.get("sink")))
                if name and not name.startswith(PREFIXES):
                    return name
    for x in _sinks():
        if not x.get("name", "").startswith(PREFIXES):
            return x["name"]
    return d


def sink_label(name):
    for x in _sinks():
        if x.get("name") == name:
            return (x.get("properties") or {}).get("device.description") or x.get("description") or name
    return "Speakers"


def move_streams(to, only_from=None):
    """Move apps' sound to `to` (our own delay output and PhoneLink's stay where they are)."""
    try:
        inputs = json.loads(_pactl("-f", "json", "list", "sink-inputs").stdout or "[]")
    except ValueError:
        return
    sink_id = {x.get("name"): str(x.get("index")) for x in _sinks()}
    for i in inputs:
        if (i.get("properties") or {}).get("node.name", "").startswith(PREFIXES):
            continue
        if only_from and str(i.get("sink")) != sink_id.get(only_from):
            continue
        _pactl("move-sink-input", str(i["index"]), to)


def _our_modules():
    mods = [line.split("\t") for line in _pactl("list", "short", "modules").stdout.splitlines()]
    return [m[0] for m in mods if len(m) > 2 and m[1] == "module-null-sink" and f"sink_name={SINK}" in m[2]]


def clean_up(log=print, back=None, why="left over from last time"):
    """Remove every TV Cast speaker and put the sound back on `back` (default: the real speakers)."""
    if not shutil.which("pactl"):
        return
    mods = _our_modules()
    if not mods:
        return
    if not back or back.startswith("tvcast_") or not _sink_exists(back):
        back = real_sink()
    if default_sink().startswith("tvcast_"):
        _pactl("set-default-sink", back)
    # every app still on a TV Cast speaker goes back (there may be several with the same name)
    try:
        inputs = json.loads(_pactl("-f", "json", "list", "sink-inputs").stdout or "[]")
    except ValueError:
        inputs = []
    ours = {str(x.get("index")) for x in _sinks() if x.get("name", "").startswith("tvcast_")}
    for i in inputs:
        if str(i.get("sink")) in ours and not (i.get("properties") or {}).get("node.name", "").startswith(PREFIXES):
            _pactl("move-sink-input", str(i["index"]), back)
    for m in mods:
        _pactl("unload-module", m)
    log(f"Sound: back on {sink_label(back)}, no delay ({why})")


def _watch(pid):
    """Run as a separate process: when TV Cast exits in any way (crash, killed), undo the sound routing."""
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            pass
        time.sleep(1)
    clean_up(why="TV Cast closed")


class DelayLine:
    """Records the virtual speaker and plays it into the real one `ms` later.

    (PhoneLink's DelayLine.) A FIFO of raw audio starts with `ms` of silence; one thread
    fills it from pw-record, another empties it into pw-play at the sound card's pace.
    The two sides can run on slightly different clocks, so the level is nudged back to
    target when it drifts.
    """

    RATE, CHANNELS, WIDTH = 48000, 2, 2           # s16 stereo
    FRAME = CHANNELS * WIDTH
    CHUNK = RATE // 50 * FRAME                    # 20 ms
    DRIFT = RATE * 25 // 1000 * FRAME             # 25 ms (just over one 20 ms chunk)
    OVERHEAD_MS = 65                              # recording + playback buffers (measured), taken off the FIFO

    def __init__(self, source_sink, real, ms, log):
        self.log = log
        self.lock = threading.Lock()
        self.ms = ms
        self.fifo = bytearray(self._bytes(ms))
        self.stopped = False
        fmt = ["--rate", str(self.RATE), "--channels", str(self.CHANNELS), "--format", "s16"]
        self.rec = subprocess.Popen(
            ["pw-record", "-P", "{ stream.capture.sink=true node.name=tvcast_delay_in }",
             "--target", source_sink, *fmt, "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.play = subprocess.Popen(
            ["pw-play", "-P", "{ node.name=tvcast_delay_out node.dont-reconnect=true }", "--latency", "20ms",
             "--target", real, *fmt, "-"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for pipe in (self.rec.stdout, self.play.stdin):
            try:  # small pipes: what sits in them is delay we don't control
                fcntl.fcntl(pipe.fileno(), F_SETPIPE_SZ, 4096)
            except OSError:
                pass
        threading.Thread(target=self._read, name="tvcast-delay-in", daemon=True).start()
        threading.Thread(target=self._write, name="tvcast-delay-out", daemon=True).start()

    def _bytes(self, ms):
        return self.RATE * max(0, ms - self.OVERHEAD_MS) // 1000 * self.FRAME

    def alive(self):
        return not self.stopped and self.rec.poll() is None and self.play.poll() is None

    def set_ms(self, ms):
        """Change the delay without a restart: pad with silence, or skip ahead."""
        with self.lock:
            diff = self._bytes(ms) - self._bytes(self.ms)
            if diff > 0:
                self.fifo[0:0] = bytes(diff)
            elif diff < 0:
                del self.fifo[:-diff]
            self.ms = ms

    def _read(self):
        while not self.stopped:
            data = self.rec.stdout.read1(self.CHUNK)
            if not data:
                break
            with self.lock:
                self.fifo += data
                over = len(self.fifo) - self._bytes(self.ms)
                if over > self.DRIFT:              # the speaker's clock is slower: catch up
                    del self.fifo[:over - over % self.FRAME]
        self.stopped = True

    def _write(self):
        while not self.stopped:
            with self.lock:
                short = self._bytes(self.ms) - len(self.fifo)
                if short > self.DRIFT:             # the speaker's clock is faster: hold back a little
                    self.fifo[0:0] = bytes(short - short % self.FRAME)
                n = min(self.CHUNK, len(self.fifo))
                n -= n % self.FRAME
                chunk = bytes(self.fifo[:n]) if n else bytes(self.CHUNK)
                del self.fifo[:n]
            try:
                self.play.stdin.write(chunk)
                self.play.stdin.flush()
            except (OSError, ValueError):
                break
        self.stopped = True

    def stop(self):
        self.stopped = True
        for p in (self.rec, self.play):
            p.terminate()
        for p in (self.rec, self.play):
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()


class SoundRoute:
    """Moves all sound into the TV Cast speaker; optionally delays it onto the real speakers."""

    def __init__(self, log):
        self.log = log
        self.module = None
        self.before = None       # default speaker before casting (may be PhoneLink's delay)
        self.real = None
        self.delay = None

    def start(self, delay_ms=None):
        """delay_ms=None: sound only goes to the TV (laptop silent). Otherwise laptop, delay_ms later."""
        clean_up(self.log)                       # never two TV Cast speakers
        self.before, self.real = default_sink(), real_sink()
        label = sink_label(self.real).replace('"', "'")
        r = _pactl("load-module", "module-null-sink", f"sink_name={SINK}",
                   f'sink_properties=device.description="{label} (TV Cast)"')
        if not r.stdout.strip().isdigit():
            raise OSError(f"couldn't make the TV Cast speaker: {r.stderr.strip()}")
        self.module = r.stdout.strip()
        for _ in range(30):
            if _sink_exists(SINK):
                break
            time.sleep(0.1)
        if delay_ms is not None:
            try:
                self.delay = DelayLine(SINK, self.real, delay_ms, self.log)
            except OSError:
                self.stop()
                raise
        _pactl("set-default-sink", SINK)
        move_streams(SINK)
        # if TV Cast dies mid-cast, this puts the sound back by itself
        self.watcher = subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "--watch", str(os.getpid())],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
        self.log(f"Sound: {'only on the TV' if delay_ms is None else f'{label}, {delay_ms} ms later'}")

    def stop(self):
        if getattr(self, "watcher", None):
            self.watcher.terminate()
            self.watcher = None
        if not self.module:
            clean_up(self.log, why="cast stopped")
            return
        back = self.before if self.before and _sink_exists(self.before) and self.before != SINK else self.real
        if default_sink() == SINK:            # nobody else changed the speakers meanwhile
            _pactl("set-default-sink", back)
            move_streams(back, only_from=SINK)
        else:                                  # the user picked other speakers: leave them there
            move_streams(default_sink(), only_from=SINK)
        if self.delay:
            self.delay.stop()
            self.delay = None
        _pactl("unload-module", self.module)
        self.module = None
        clean_up(self.log, back, why="cast stopped")    # anything else of ours left: gone too
        self.log(f"Sound: back on {sink_label(back)}, no delay")


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--watch":
    _watch(int(sys.argv[2]))
