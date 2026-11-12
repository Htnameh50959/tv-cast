"""Miracast (Wi-Fi Display) source side: the RTSP conversation with the TV.

The TV connects to us on TCP port 7236. Then:
  M1  we ask the TV which features it has        (OPTIONS)
  M2  the TV asks us the same                      (OPTIONS)
  M3  we ask for its video/audio formats + port    (GET_PARAMETER)
  M4  we tell it which format we'll send           (SET_PARAMETER)
  M5  we tell it to start                          (SET_PARAMETER wfd_trigger_method: SETUP)
  M6  the TV sets up the stream                    (SETUP)
  M7  the TV says "play"                           (PLAY)  -> we start sending video
Afterwards we send a keep-alive every 25 seconds.
"""
import random
import socket
import threading
import time

PORT = 7236

# WFD IEs we advertise: we're a "source", control port 7236 (0x1c44), 200 Mbit/s.
WFD_IES = bytes.fromhex("00 0006 0090 1c44 00c8")

# CEA resolution table bits we're willing to use, best first.
CEA_MODES = {
    7: (1920, 1080, 30),
    5: (1280, 720, 30),
    0: (640, 480, 60),
}
PREFERRED_CEA = [7, 5, 0]


def parse_params(body):
    params = {}
    for line in body.replace("\r", "").split("\n"):
        if ":" in line:
            k, _, v = line.partition(":")
            params[k.strip().lower()] = v.strip()
    return params


def choose_video(value):
    """Pick a format from the TV's wfd_video_formats.

    Returns (m4_descriptor, (w, h, fps)) or raises ValueError.
    Format: "<native> <pref> <codec>, <codec>..." with
    codec = "profile level cea vesa hh latency min_slice slice_enc frame_rate max_h max_v".
    """
    if not value or value == "none":
        raise ValueError("TV reported no video formats")
    parts = value.split(" ", 2)
    if len(parts) != 3:
        raise ValueError(f"bad wfd_video_formats: {value}")
    codecs = []
    for desc in parts[2].split(","):
        t = desc.strip().split()
        if len(t) < 9:
            continue
        codecs.append({
            "profile": int(t[0], 16), "level": int(t[1], 16), "cea": int(t[2], 16),
            "latency": int(t[5], 16), "min_slice": int(t[6], 16), "frc": int(t[8], 16) & 1,
        })
    # Constrained Baseline Profile is bit 0x01; every Miracast sink must support it.
    cbp = [c for c in codecs if c["profile"] & 0x01] or codecs
    if not cbp:
        raise ValueError(f"no usable codec in: {value}")
    codec = max(cbp, key=lambda c: c["level"])
    for bit in PREFERRED_CEA:
        if codec["cea"] & (1 << bit):
            break
    else:
        bit = 0  # 640x480p60 is mandatory for every sink
    level = 1 << (codec["level"].bit_length() - 1) if codec["level"] else 1
    desc = "00 00 01 %02X %08X %08X %08X %02X %04X %04X %02X none none" % (
        level, 1 << bit, 0, 0, codec["latency"], codec["min_slice"], 0, codec["frc"])
    return desc, CEA_MODES[bit]


def choose_audio(value):
    """AAC stereo 48 kHz if the TV supports it, else no audio."""
    for desc in (value or "").split(","):
        t = desc.strip().split()
        if len(t) >= 2 and t[0] == "AAC" and int(t[1], 16) & 0x1:
            return "AAC 00000001 00"
    return None


class Message:
    def __init__(self, first, headers, body):
        self.first = first
        self.headers = headers
        self.body = body
        p = first.split(" ")
        self.is_response = first.startswith("RTSP/")
        if self.is_response:
            self.status = int(p[1]) if len(p) > 1 and p[1].isdigit() else 0
            self.method = None
        else:
            self.method = p[0]
            self.uri = p[1] if len(p) > 1 else ""
        self.cseq = int(headers.get("cseq", "0") or 0)


class Session(threading.Thread):
    """One TV connection."""

    def __init__(self, owner, conn, addr):
        super().__init__(daemon=True)
        self.owner = owner
        self.conn = conn
        self.peer_ip = addr[0]
        self.local_ip = conn.getsockname()[0]
        self.cseq = 0
        self.pending = {}
        self.lock = threading.Lock()
        self.buf = b""
        self.alive = True
        self.m1_done = self.m2_done = self.m3_sent = False
        self.playing = False
        self.session_id = str(random.randint(10_000_000, 99_999_999))
        self.rtp_ports = (1028, 0)
        self.video = None
        self.audio = None

    # ---------------------------------------------------------- sending
    def _send(self, text):
        self.owner.log("→ " + text.replace("\r\n", "\n  ").rstrip())
        try:
            self.conn.sendall(text.encode())
        except OSError as e:
            self.close(f"send failed: {e}")

    def request(self, method, uri, name, body=None, headers=()):
        with self.lock:
            self.cseq += 1
            c = self.cseq
            self.pending[c] = name
        lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {c}", *headers]
        if body:
            lines += ["Content-Type: text/parameters", f"Content-Length: {len(body.encode())}"]
        self._send("\r\n".join(lines) + "\r\n\r\n" + (body or ""))

    def respond(self, req, status=200, reason="OK", headers=()):
        lines = [f"RTSP/1.0 {status} {reason}", f"CSeq: {req.cseq}",
                 "Date: " + time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()), *headers]
        self._send("\r\n".join(lines) + "\r\n\r\n")

    # ---------------------------------------------------------- receiving
    def _read(self):
        while self.alive:
            end = self.buf.find(b"\r\n\r\n")
            if end >= 0:
                head = self.buf[:end].decode(errors="replace").split("\r\n")
                headers = {}
                for h in head[1:]:
                    k, _, v = h.partition(":")
                    headers[k.strip().lower()] = v.strip()
                length = int(headers.get("content-length", "0") or 0)
                total = end + 4 + length
                if len(self.buf) >= total:
                    body = self.buf[end + 4:total].decode(errors="replace")
                    self.buf = self.buf[total:]
                    msg = Message(head[0], headers, body)
                    self.owner.log("← " + "\n  ".join(head) + ("\n  " + body.strip().replace("\r\n", "\n  ") if body.strip() else ""))
                    return msg
            try:
                data = self.conn.recv(65536)
            except OSError:
                data = b""
            if not data:
                return None
            self.buf += data
        return None

    # ---------------------------------------------------------- protocol
    def run(self):
        self.owner.emit("tv_connected", ip=self.peer_ip)
        self.request("OPTIONS", "*", "M1", headers=["Require: org.wfa.wfd1.0"])
        while self.alive:
            msg = self._read()
            if msg is None:
                self.close("TV closed the connection")
                break
            try:
                if msg.is_response:
                    self.on_response(self.pending.pop(msg.cseq, "?"), msg)
                else:
                    self.on_request(msg)
            except Exception as e:  # keep the session alive on odd messages
                self.owner.log(f"error handling message: {e!r}")

    def maybe_m3(self):
        if self.m1_done and self.m2_done and not self.m3_sent:
            self.m3_sent = True
            self.request("GET_PARAMETER", "rtsp://localhost/wfd1.0", "M3",
                         body="wfd_video_formats\r\nwfd_audio_codecs\r\nwfd_client_rtp_ports\r\n")

    def on_response(self, name, msg):
        if name == "M1":
            self.m1_done = True
            self.maybe_m3()
        elif name == "M3":
            if msg.status != 200:
                return self.close(f"TV refused the format query ({msg.status})")
            p = parse_params(msg.body)
            desc, self.video = choose_video(p.get("wfd_video_formats"))
            self.audio = choose_audio(p.get("wfd_audio_codecs"))
            ports = p.get("wfd_client_rtp_ports", "").split()
            if len(ports) >= 3 and ports[1].isdigit():
                self.rtp_ports = (int(ports[1]), int(ports[2]) if ports[2].isdigit() else 0)
            self.owner.emit("negotiated", video=self.video, audio=bool(self.audio))
            body = (f"wfd_video_formats: {desc}\r\n"
                    + (f"wfd_audio_codecs: {self.audio}\r\n" if self.audio else "")
                    + f"wfd_presentation_URL: rtsp://{self.local_ip}/wfd1.0/streamid=0 none\r\n"
                    + f"wfd_client_rtp_ports: RTP/AVP/UDP;unicast {self.rtp_ports[0]} {self.rtp_ports[1]} mode=play\r\n")
            self.request("SET_PARAMETER", "rtsp://localhost/wfd1.0", "M4", body=body)
        elif name == "M4":
            if msg.status != 200:
                return self.close(f"TV rejected the video format ({msg.status})")
            self.request("SET_PARAMETER", "rtsp://localhost/wfd1.0", "M5",
                         body="wfd_trigger_method: SETUP\r\n")

    def on_request(self, msg):
        m = msg.method
        if m == "OPTIONS":
            self.respond(msg, headers=["Public: org.wfa.wfd1.0, SETUP, TEARDOWN, PLAY, PAUSE, GET_PARAMETER, SET_PARAMETER"])
            self.m2_done = True
            self.maybe_m3()
        elif m == "GET_PARAMETER":
            self.respond(msg)
        elif m == "SET_PARAMETER":
            self.respond(msg)
            if "wfd_idr_request" in msg.body:
                self.owner.emit("idr")
        elif m == "SETUP":
            transport = msg.headers.get("transport", "RTP/AVP/UDP;unicast")
            for part in transport.split(";"):
                if part.startswith("client_port="):
                    first = part.split("=", 1)[1].split("-")[0]
                    if first.isdigit():
                        self.rtp_ports = (int(first), self.rtp_ports[1])
            self.respond(msg, headers=[
                f"Session: {self.session_id};timeout=30",
                f"Transport: {transport};server_port=19000-19001"])
        elif m == "PLAY":
            self.respond(msg, headers=[f"Session: {self.session_id}"])
            if not self.playing:
                self.playing = True
                self.owner.emit("play", ip=self.peer_ip, port=self.rtp_ports[0],
                                video=self.video or CEA_MODES[0], audio=bool(self.audio))
                threading.Thread(target=self._keepalive, daemon=True).start()
        elif m == "PAUSE":
            self.respond(msg, headers=[f"Session: {self.session_id}"])
        elif m == "TEARDOWN":
            self.respond(msg, headers=[f"Session: {self.session_id}"])
            self.close("TV ended the session")
        else:
            self.respond(msg, 501, "Not Implemented")

    def _keepalive(self):
        while self.alive:
            for _ in range(25):
                if not self.alive:
                    return
                time.sleep(1)
            self.request("GET_PARAMETER", "rtsp://localhost/wfd1.0", "keepalive",
                         headers=[f"Session: {self.session_id}"])

    def teardown(self):
        """Politely ask the TV to stop, then close."""
        if self.alive:
            self.request("SET_PARAMETER", "rtsp://localhost/wfd1.0", "teardown",
                         body="wfd_trigger_method: TEARDOWN\r\n")
            time.sleep(0.5)
        self.close("stopped")

    def close(self, reason):
        if not self.alive:
            return
        self.alive = False
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.conn.close()
        self.owner.emit("closed", reason=reason)


class WfdSource:
    """Listens on port 7236 and runs one Session per TV connection.

    emit(event, **details) is called from background threads with:
      tv_connected(ip) negotiated(video, audio) play(ip, port, video, audio)
      idr() closed(reason) error(message)
    """

    def __init__(self, emit, log):
        self.emit = emit
        self.log = log
        self.sock = None
        self.session = None

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("0.0.0.0", PORT))
        except OSError as e:
            self.emit("error", message=f"Port {PORT} is busy ({e.strerror}). Close Network Displays and try again.")
            return False
        self.sock.listen(2)
        threading.Thread(target=self._accept, daemon=True).start()
        return True

    def _accept(self):
        while self.sock:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.log(f"TV connected from {addr[0]}")
            if self.session and self.session.alive:
                self.session.close("replaced by a new connection")
            self.session = Session(self, conn, addr)
            self.session.start()

    def stop_session(self):
        if self.session:
            self.session.teardown()
            self.session = None

    def shutdown(self):
        self.stop_session()
        if self.sock:
            s, self.sock = self.sock, None
            s.close()
