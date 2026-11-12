"""Screen + sound -> H.264/AAC -> MPEG-TS -> RTP -> TV."""
import subprocess

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

Gst.init(None)


def default_monitor():
    """The 'what you hear' source of the current speakers."""
    try:
        sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
        return sink + ".monitor" if sink else None
    except Exception:
        return None


def bitrate_for(width):
    return 6000 if width >= 1920 else 4000 if width >= 1280 else 2000


class Streamer:
    def __init__(self, log, on_error, on_first_frame):
        self.log = log
        self.on_error = on_error
        self.on_first_frame = on_first_frame
        self.pipeline = None
        self.encoder = None

    def start(self, video_src, tv_ip, port, width, height, fps, audio):
        """video_src is a gst-launch fragment, e.g. 'pipewiresrc fd=.. path=..'."""
        self.stop()
        kbit = bitrate_for(width)
        desc = (
            f"mpegtsmux name=mux alignment=7 "
            f"! rtpmp2tpay ssrc=1 perfect-rtptime=false "
            f"! udpsink host={tv_ip} port={port} sync=false async=false "
            f"{video_src} "
            f"! queue leaky=downstream max-size-buffers=2 "
            f"! videorate ! videoscale ! videoconvert "
            f"! video/x-raw,format=I420,width={width},height={height},framerate={fps}/1,pixel-aspect-ratio=1/1 "
            f"! x264enc name=enc tune=zerolatency speed-preset=ultrafast bitrate={kbit} "
            f"key-int-max={fps} bframes=0 ref=1 cabac=false dct8x8=false threads=2 "
            f"vbv-buf-capacity=50 insert-vui=true "
            f"! video/x-h264,profile=constrained-baseline,stream-format=byte-stream "
            f"! h264parse config-interval=-1 name=vparse "
            f"! queue max-size-time=500000000 ! mux.sink_4113 "
        )
        monitor = default_monitor() if audio else None
        if monitor:
            desc += (
                f"pulsesrc device={monitor} do-timestamp=true provide-clock=false "
                f"! queue leaky=downstream max-size-time=200000000 "
                f"! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2 "
                f"! avenc_aac bitrate=128000 "
                f"! queue max-size-time=500000000 ! mux.sink_4352 "
            )
        self.log(f"Streaming {width}x{height}@{fps} {kbit} kbit/s to {tv_ip}:{port}"
                 + (" with sound" if monitor else " (no sound)"))
        try:
            self.pipeline = Gst.parse_launch(desc)
        except GLib.Error as e:
            self.on_error(f"Could not build the video pipeline: {e.message}")
            return
        self.encoder = self.pipeline.get_by_name("enc")
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

        self._first = True
        pad = self.pipeline.get_by_name("vparse").get_static_pad("src")
        pad.add_probe(Gst.PadProbeType.BUFFER, self._probe)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _probe(self, pad, info):
        if self._first:
            self._first = False
            GLib.idle_add(self.on_first_frame)
        return Gst.PadProbeReturn.REMOVE

    def _on_message(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            self.log(f"pipeline error: {err.message} ({dbg})")
            self.on_error(err.message)
        elif msg.type == Gst.MessageType.WARNING:
            w, _ = msg.parse_warning()
            self.log(f"pipeline warning: {w.message}")

    def force_keyframe(self):
        if self.encoder:
            ev = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
            self.encoder.get_static_pad("src").send_event(ev)

    def stop(self):
        if self.pipeline:
            self.pipeline.get_bus().remove_signal_watch()
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.encoder = None
