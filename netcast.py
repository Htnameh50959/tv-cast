"""Cast the screen to an LG webOS TV over the normal Wi-Fi network.

  screen (GNOME portal / PipeWire)
    -> H.264 (Intel hardware encoder, x264 fallback) in fragmented MP4
    -> HTTP on this laptop
    -> the TV's web browser plays it with Media Source Extensions (low delay).
  If the TV browser can't play MP4 live, the page falls back to MJPEG.

The TV browser is opened remotely with LG's SSAP API (ws://TV:3000).
The first time, the TV shows a pop-up: press OK on the remote.
"""
import asyncio
import collections
import copy
import json
import os
import queue
import socket
import struct
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

from lg_handshake import REGISTRATION_PAYLOAD  # noqa: E402

Gst.init(None)

PORT = 8090
DELAY = 2.0     # seconds the TV picture is held behind the laptop screen (smooth video modes)
KEY_FILE = os.path.expanduser("~/.config/tv-cast/lg-client-key")
LAST_TV_FILE = os.path.expanduser("~/.config/tv-cast/last-tv.json")
BROWSER_ID = "com.webos.app.browser"


# ---------------------------------------------------------------- discovery
def find_tvs(timeout=3.0):
    """Return [{'ip':..., 'name':...}] of LG webOS TVs on the network (SSDP)."""
    msg = (b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\n'
           b'MX: 2\r\nST: urn:lge-com:service:webos-second-screen:1\r\n\r\n')
    msg_all = msg.replace(b"urn:lge-com:service:webos-second-screen:1", b"ssdp:all")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    found = {}
    try:
        for m in (msg, msg_all, msg):
            s.sendto(m, ("239.255.255.250", 1900))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            text = data.decode(errors="ignore")
            head = {l.split(":", 1)[0].strip().upper(): l.split(":", 1)[1].strip()
                    for l in text.split("\r\n") if ":" in l}
            if "webos" in (head.get("SERVER", "") + head.get("ST", "")).lower() and addr[0] not in found:
                found[addr[0]] = head.get("LOCATION")
    finally:
        s.close()
    tvs = []
    for ip, loc in found.items():
        name = "LG TV"
        try:
            xml = urllib.request.urlopen(loc, timeout=2).read().decode(errors="ignore")
            if "<friendlyName>" in xml:
                name = xml.split("<friendlyName>", 1)[1].split("<", 1)[0].strip()
        except Exception:
            pass
        tvs.append({"ip": ip, "name": name})
    return tvs


def last_tv():
    try:
        with open(LAST_TV_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def remember_tv(tv):
    os.makedirs(os.path.dirname(LAST_TV_FILE), exist_ok=True)
    with open(LAST_TV_FILE, "w") as f:
        json.dump(tv, f)


def local_ip_towards(ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, 9))
        return s.getsockname()[0]
    finally:
        s.close()


# ---------------------------------------------------------------- TV remote
class LGRemote:
    """Minimal webOS SSAP client. All methods are blocking; call from a thread."""

    def __init__(self, ip, log, on_prompt=None):
        self.ip = ip
        self.log = log
        self.on_prompt = on_prompt or (lambda: None)

    async def _session(self, requests):
        import websockets
        reg = {"type": "register", "id": "reg0", "payload": copy.deepcopy(REGISTRATION_PAYLOAD)}
        try:
            with open(KEY_FILE) as f:
                reg["payload"]["client-key"] = f.read().strip()
        except OSError:
            pass
        replies = []
        async with websockets.connect(f"ws://{self.ip}:3000", open_timeout=5, max_size=None) as ws:
            await ws.send(json.dumps(reg))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                if msg.get("type") == "response" and msg.get("payload", {}).get("pairingType") == "PROMPT":
                    self.on_prompt()
                elif msg.get("type") == "registered":
                    key = msg.get("payload", {}).get("client-key")
                    if key:
                        os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
                        with open(KEY_FILE, "w") as f:
                            f.write(key)
                    break
                elif msg.get("type") == "error":
                    raise RuntimeError(msg.get("error") or "TV refused")
            for i, (uri, payload) in enumerate(requests):
                await ws.send(json.dumps({"type": "request", "id": f"r{i}", "uri": uri, "payload": payload}))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                    if msg.get("id") == f"r{i}":
                        replies.append(msg)
                        break
        return replies

    async def _pointer(self, *msgs):
        import websockets
        reply = (await self._session([("ssap://com.webos.service.networkinput/getPointerInputSocket", {})]))[0]
        path = reply.get("payload", {}).get("socketPath")
        if not path:
            raise RuntimeError(f"no input socket: {reply}")
        async with websockets.connect(path, open_timeout=5) as ws:
            for m in msgs:
                await ws.send(m)
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.3)

    def click_center(self):
        """Move the TV pointer to the middle of the screen and click.
        The page treats the click as a real user action and goes full screen
        (a pressed OK button doesn't reach the page: the browser bar has focus)."""
        move = lambda dx, dy: f"type:move\ndx:{dx}\ndy:{dy}\ndown:0\n\n"
        asyncio.run(self._pointer(*([move(-200, -200)] * 10 + [move(80, 50)] * 8 + ["type:click\n\n"])))

    def call(self, *requests):
        return asyncio.run(self._session(list(requests)))

    def open_url(self, url):
        r = self.call(("ssap://system.launcher/open", {"target": url}))[0]
        if r.get("type") == "error":
            raise RuntimeError(r.get("error"))
        return r.get("payload", {})

    def close_browser(self, session_id=None):
        payload = {"id": BROWSER_ID}
        if session_id:
            payload["sessionId"] = session_id
        try:
            self.call(("ssap://system.launcher/close", payload))
        except Exception as e:
            self.log(f"close browser: {e}")


# ---------------------------------------------------------------- web page
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Laptop screen</title>
<style>html,body{margin:0;width:100%;height:100%;background:#000;overflow:hidden;cursor:none}
video,img{position:absolute;left:0;top:0;width:100%;height:100%;object-fit:contain}
#m{position:absolute;left:0;right:0;bottom:6%;text-align:center;color:#ccc;
font:28px sans-serif;transition:opacity 1s}</style></head><body>
<video id="v" muted autoplay playsinline></video>
<div id="m">Connecting to the laptop&hellip;</div>
<script>
var CODEC='__CODEC__',MODE='__MODE__',AUDIO=__AUDIO__;
var v=document.getElementById('v'),m=document.getElementById('m');
function note(t){m.innerHTML=t;m.style.opacity=1;}
function hide(){m.style.opacity=0;}
function beacon(w){try{var x=new XMLHttpRequest();x.open('GET','/hello?'+w+'&t='+Date.now());x.send();}catch(e){}}
function full(){var d=document.documentElement;
 var f=d.requestFullscreen||d.webkitRequestFullscreen||d.webkitRequestFullScreen;
 if(f&&!(document.fullscreenElement||document.webkitFullscreenElement)){try{f.call(d);}catch(e){}}}
document.addEventListener('keydown',full);document.addEventListener('click',full);
function fsrep(){beacon('screen&fs='+((document.fullscreenElement||document.webkitFullscreenElement)?1:0)+'&ww='+window.innerWidth+'x'+window.innerHeight);}
document.addEventListener('fullscreenchange',fsrep);document.addEventListener('webkitfullscreenchange',fsrep);window.addEventListener('resize',fsrep);
function retry(){setTimeout(function(){location.reload();},1500);}
function mjpeg(){
 v.style.display='none';var i=document.createElement('img');document.body.appendChild(i);
 i.onload=function(){hide();};i.onerror=retry;i.src='/mjpeg?'+Date.now();beacon('mjpeg');
 setTimeout(hide,3000);
}
function live(){
 // ask for the next picture only after the last one is on screen: nothing can queue up
 v.style.display='none';
 var a=[document.createElement('img'),document.createElement('img')],cur=0,started=false,id=Math.random().toString(36).slice(2),seq=0,shown=0,fails=0;
 a[0].style.visibility=a[1].style.visibility='hidden';document.body.appendChild(a[0]);document.body.appendChild(a[1]);
 function next(){var n=a[1-cur];n.src='/frame?c='+id+'&i='+(seq++);}
 a[0].onload=a[1].onload=function(){var n=a[1-cur];n.style.visibility='visible';a[cur].style.visibility='hidden';cur=1-cur;shown++;fails=0;
  if(!started){started=true;hide();beacon('mjpeg');}next();};
 a[0].onerror=a[1].onerror=function(){if(++fails>20){retry();return;}setTimeout(next,100);};
 setInterval(function(){beacon('stats&shown_fps='+(shown/3).toFixed(1));shown=0;},3000);
 next();
}
function mse(){
 var ms=new MediaSource(),sb=null,q=[],reading=true,asb=null,aq=[],aoffSet=false;
 v.src=URL.createObjectURL(ms);
 function feed(){
  if(!sb||sb.updating||!q.length)return;
  var n=0,i;for(i=0;i<q.length;i++)n+=q[i].length;
  var b=new Uint8Array(n),o=0;for(i=0;i<q.length;i++){b.set(q[i],o);o+=q[i].length;}
  q=[];try{sb.appendBuffer(b);}catch(e){beacon('append-error');retry();}
 }
 // sound: its own stream; shifted onto the video's timeline by the difference of their
 // capture clocks, so the browser then plays picture and sound in sync by itself
 function afeed(){
  if(!asb||asb.updating||!aq.length)return;
  if(!aoffSet){if(amoff===null||moff===null||ashift===null)return;asb.timestampOffset=amoff-moff+ashift;aoffSet=true;}
  var n=0,i;for(i=0;i<aq.length;i++)n+=aq[i].length;
  var b=new Uint8Array(n),o=0;for(i=0;i<aq.length;i++){b.set(aq[i],o);o+=aq[i].length;}
  aq=[];try{asb.appendBuffer(b);}catch(e){beacon('audio-append-error');}
 }
 ms.addEventListener('sourceopen',function(){
  try{sb=ms.addSourceBuffer('video/mp4; codecs="'+CODEC+'"');}catch(e){beacon('no-mse');mjpeg();return;}
  sb.addEventListener('updateend',feed);
  if(AUDIO){try{asb=ms.addSourceBuffer('audio/mp4; codecs="mp4a.40.2"');}catch(e){asb=null;beacon('no-audio');}}
  if(asb&&window.fetch&&window.ReadableStream){
   asb.addEventListener('updateend',afeed);setInterval(afeed,100);
   fetch('/audio?'+Date.now()).then(function(r){var rd=r.body.getReader();
    function pump(){rd.read().then(function(c){if(c.done){beacon('audio-ended');return;}aq.push(c.value);afeed();pump();},
     function(){beacon('audio-error');});}
    pump();},function(){beacon('audio-error');});
  }
  var x=new XMLHttpRequest();
  if(window.fetch&&window.ReadableStream){
   fetch('/live?'+Date.now()).then(function(r){var rd=r.body.getReader();
    function pump(){rd.read().then(function(c){if(c.done){retry();return;}q.push(c.value);feed();pump();},retry);}
    pump();},retry);
  }else{beacon('no-fetch');mjpeg();}
 });
 // Hold the picture exactly DELAY seconds behind the laptop screen.
 // The laptop tells us its clock and the wall-clock capture time of media time 0 (/clock),
 // so  delay = laptop_now - (moff + video.currentTime).
 var started=false,jumps=0,waits=0,DELAY=__DELAY__,coff=null,moff=null,amoff=null,ashift=null,best=1e9,keys=[];
 // This TV can't change playback speed, and a jump lands on the keyframe before the target.
 // So we jump only ONTO a keyframe, started SEEK seconds early (the time the TV needs for a
 // jump, learned and remembered), then play normally: the delay stays put with no more jumps.
 // with sound the TV moves in whole frames (~34 ms) and jumps take longer: own tolerance and memory
 // A trim (short pause) always moves the picture a whole frame (~33 ms) on this TV, so trim only
 // when that brings it closer to 2 s (more than half a frame too new). Otherwise a 10 ms trim
 // overshoots past TOL and triggers a jump (a visible ~0.3 s freeze) every few seconds.
 var SKEY=AUDIO?'tvcast-seek-sound':'tvcast-seek',TOL=AUDIO?0.04:0.03,TRIM=AUDIO?0.02:0.017;
 var SEEK=AUDIO?0.55:0.3;try{SEEK=parseFloat(localStorage.getItem(SKEY))||SEEK;}catch(e){}
 var planned=false,checkAt=0,justJumped=false,bad=0,tries=0,trimAt=0,trims=0;
 function sync(cb){var t0=performance.now(),x=new XMLHttpRequest();x.open('GET','/clock?'+Date.now());
  x.onload=function(){var t1=performance.now(),r;try{r=JSON.parse(x.responseText);}catch(e){return;}
   if(r.media!=null)moff=r.media;if(r.amedia!=null)amoff=r.amedia;
   if(r.ashift!=null){if(aoffSet&&Math.abs(r.ashift-ashift)>0.001){beacon('sound-delay-changed');location.reload();}ashift=r.ashift;}if(r.keys)keys=r.keys;
   if(r.delay!=null&&Math.abs(r.delay-DELAY)>0.001)setDelay(r.delay);
   var rtt=t1-t0;best+=1;   // keep the quickest round trip (most exact); slowly allow fresher ones
   if(coff===null||rtt<=best){best=rtt;coff=r.now*1000-(t0+t1)/2;}
   if(cb)cb();};
  x.send();}
 for(var k=0;k<10;k++)setTimeout(sync,k*120);setInterval(sync,2000);
 // The picture delay changed (a negative sound delay holds the picture longer): hold the
 // picture once for the extra time, or jump ahead onto a keyframe for a shorter delay.
 var holdUntil=0;
 function setDelay(d){var old=DELAY;DELAY=d;beacon('delay-changed&to='+d);
  if(!started)return;bad=0;tries=0;
  if(d>old){holdUntil=performance.now()+(d-old)*1000;checkAt=holdUntil+1500;v.pause();
   setTimeout(function(){v.play();},(d-old)*1000);}
  else plan();}
 function lnow(){return (performance.now()+coff)/1000;}
 function err(){return lnow()-moff-v.currentTime-DELAY;}   // >0: picture too old
 function plan(){
  if(planned)return;planned=true;
  sync(function(){
   var end=sb.buffered.end(sb.buffered.length-1),want=lnow()-DELAY-moff,K=null;
   for(var i=0;i<keys.length;i++)if(keys[i]>=want+SEEK+0.1&&keys[i]<end){K=keys[i];break;}
   if(K===null){planned=false;return;}
   setTimeout(function(){v.currentTime=K;jumps++;tries++;justJumped=true;checkAt=performance.now()+1500;planned=false;},
              Math.max(0,(K-SEEK-want)*1000-(lnow()-DELAY-moff-want)*1000));
  });}
 v.addEventListener('waiting',function(){waits++;});
 setInterval(function(){if(!sb||!sb.buffered.length||coff===null||moff===null)return;var e=sb.buffered.end(sb.buffered.length-1);
  var qq=v.getVideoPlaybackQuality?v.getVideoPlaybackQuality():null;
  var dec=qq?qq.totalVideoFrames:(v.webkitDecodedFrameCount||-1),drop=qq?qq.droppedVideoFrames:(v.webkitDroppedFrameCount||-1);
  beacon('stats&delay='+(started?(err()+DELAY).toFixed(3):'-')+'&newest='+(lnow()-moff-e).toFixed(3)
   +'&seek='+SEEK.toFixed(3)+'&rtt='+best.toFixed(0)+'&jumps='+jumps+'&trims='+trims+'&waits='+waits+'&dec='+dec+'&drop='+drop
   +(asb?'&sound='+(v.muted?'muted':'on')+'&abuf='+(asb.buffered.length?(asb.buffered.end(asb.buffered.length-1)-v.currentTime).toFixed(2):'-'):'')
   +'&w='+v.videoWidth+'&ww='+window.innerWidth+'x'+window.innerHeight);},3000);

 setInterval(function(){
  if(!sb||!sb.buffered.length||coff===null||moff===null)return;
  var end=sb.buffered.end(sb.buffered.length-1),start=sb.buffered.start(0);
  var want=lnow()-DELAY-moff;
  if(!started){if(want<start||want>end)return;
   if(asb&&(!asb.buffered.length||want<asb.buffered.start(0)||want>asb.buffered.end(asb.buffered.length-1)))return;
   v.currentTime=want;
   if(asb){v.muted=false;var pr=v.play();if(pr&&pr.catch)pr.catch(function(){v.muted=true;v.play();beacon('audio-blocked');});}
   else v.play();
   started=true;hide();beacon('playing');
   checkAt=performance.now()+1500;return;}
  if(performance.now()<holdUntil)return;
  if(v.paused)v.play();
  if(v.seeking||planned||performance.now()<checkAt)return;
  var e=err();
  if(justJumped){justJumped=false;
   // learn how long this TV takes to jump (only from clean jumps)
   if(Math.abs(e)<0.6&&tries<=6){SEEK=Math.max(0,Math.min(1.5,SEEK+e));try{localStorage.setItem(SKEY,SEEK);}catch(x){}}
   if(Math.abs(e)>TOL&&tries<6){plan();return;}
   tries=0;}
  // TV clock running a bit fast (picture getting too new): hold the current frame briefly
  if(e<-TRIM&&e>-0.1&&performance.now()>trimAt){trimAt=performance.now()+1000;trims++;v.pause();
   setTimeout(function(){v.play();},-e*1000);return;}
  // off by more than TOL for 3 s (e.g. Wi-Fi stall): one exact jump to fix it
  bad=Math.abs(e)>TOL?bad+1:0;
  if(bad>=30){bad=0;tries=0;plan();}
  if(!sb.updating&&v.currentTime-start>20){try{sb.remove(start,v.currentTime-10);}catch(x){}}
  if(asb&&!asb.updating&&asb.buffered.length&&v.currentTime-asb.buffered.start(0)>20){
   try{asb.remove(asb.buffered.start(0),v.currentTime-10);}catch(x){}}
 },100);
 v.addEventListener('error',function(){beacon('video-error');retry();});
}
if(MODE=='live')live();
else if(window.MediaSource&&MediaSource.isTypeSupported('video/mp4; codecs="'+CODEC+'"'))mse();
else{beacon('no-mse');mjpeg();}
</script></body></html>"""


# ---------------------------------------------------------------- MP4 helpers
def _boxes(b, start, end):
    i = start
    while i + 8 <= end:
        size, kind = struct.unpack(">I4s", b[i:i + 8])
        if size < 8:
            return
        yield kind, i + 8, i + size
        i += size


def strip_mehd(init):
    """Remove the fragment-duration box so the browser treats it as a live stream."""
    out = bytearray()
    for kind, s, e in _boxes(init, 0, len(init)):
        box = init[s - 8:e]
        if kind == b"moov":
            inner = bytearray()
            for k2, s2, e2 in _boxes(init, s, e):
                sub = init[s2 - 8:e2]
                if k2 == b"mvex":
                    mv = b"".join(init[s3 - 8:e3] for k3, s3, e3 in _boxes(init, s2, e2) if k3 != b"mehd")
                    sub = struct.pack(">I4s", len(mv) + 8, b"mvex") + mv
                inner += sub
            box = struct.pack(">I4s", len(inner) + 8, b"moov") + bytes(inner)
        out += box
    return bytes(out)


def codec_string(init):
    j = init.find(b"avcC")
    if j < 0:
        return "avc1.640028"
    return "avc1." + init[j + 5:j + 8].hex().upper()


def media_timescale(init):
    """Ticks per second of the video track (mdhd box)."""
    j = init.find(b"mdhd")
    if j < 0:
        return 0
    p = j + 8 + (16 if init[j + 4] == 1 else 8)
    return struct.unpack(">I", init[p:p + 4])[0]


def fragment_time(moof):
    """baseMediaDecodeTime of a fragment (tfdt box), in timescale ticks."""
    j = moof.find(b"tfdt")
    if j < 0:
        return None
    if moof[j + 4] == 1:
        return struct.unpack(">Q", moof[j + 8:j + 16])[0]
    return struct.unpack(">I", moof[j + 8:j + 12])[0]


def fragment_is_key(moof):
    """True if the first sample in this moof is a sync sample (keyframe)."""
    for k, s, e in _boxes(moof, 8, len(moof)):
        if k != b"traf":
            continue
        default_flags = None
        for k2, s2, e2 in _boxes(moof, s, e):
            if k2 == b"tfhd":
                flags = int.from_bytes(moof[s2 + 1:s2 + 4], "big")
                p = s2 + 8
                for bit, n in ((0x1, 8), (0x2, 4), (0x8, 4), (0x10, 4)):
                    if flags & bit:
                        p += n
                if flags & 0x20:
                    default_flags = int.from_bytes(moof[p:p + 4], "big")
            elif k2 == b"trun":
                flags = int.from_bytes(moof[s2 + 1:s2 + 4], "big")
                p = s2 + 8
                if flags & 0x1:
                    p += 4
                if flags & 0x4:
                    return not (int.from_bytes(moof[p:p + 4], "big") >> 16) & 1
                if flags & 0x400:
                    p += 4 if flags & 0x100 else 0
                    p += 4 if flags & 0x200 else 0
                    return not (int.from_bytes(moof[p:p + 4], "big") >> 16) & 1
        if default_flags is not None:
            return not (default_flags >> 16) & 1
    return True


# ---------------------------------------------------------------- streamer
class Client:
    def __init__(self):
        self.q = queue.Queue()
        self.need_key = True
        self.alive = True


class CastServer:
    """Screen -> fMP4/MJPEG over HTTP. Events go to on_event(name, detail) from any thread."""

    def __init__(self, log, on_event):
        self.log = log
        self.on_event = on_event
        self.pipe = None
        self.enc = None
        self.httpd = None
        self.init_seg = None
        self.codec = None
        self.buf = bytearray()
        self.pending_moof = None
        self.clients = []
        self.lock = threading.Lock()
        self.jpeg = None
        self.jpeg_n = 0
        self.jpeg_cond = threading.Condition()
        self.mjpeg_clients = 0
        self.bitrate = 12000
        self.hw = True
        self.mode = "video"        # "live" = JPEG pictures, no TV video buffer
        self.jpeg_quality = 70
        self.jpeg_sent = 0
        self.frame_clients = {}
        self.tv_fullscreen = False
        self.tv_gaps = []
        # wall-clock capture time of video media time 0 (TV uses it to hold an exact delay)
        self.media_offsets = collections.deque(maxlen=61)
        self.key_times = collections.deque(maxlen=20)   # media times (s) of recent keyframes
        # sound for the TV (None = no sound sent): a PulseAudio/PipeWire source to record
        self.audio_device = None
        self.audio_delay_ms = round(DELAY * 1000)   # TV sound: how long after the laptop makes it
        self.picture_delay = DELAY                  # seconds the TV picture is held back (more for a negative sound delay)
        self.apipe = None
        self.a_init = None
        self.a_buf = bytearray()
        self.a_moof = None
        self.a_clients = []
        self.audio_offsets = collections.deque(maxlen=61)
        self.timescale = 0

    # -- pipeline
    def _pipeline_desc(self, fd, node, width, height, fps, hw):
        if self.mode == "live":
            # every frame is its own picture: the TV shows it the moment it arrives.
            # (vapostproc can't take the portal's plain BGRx frames here, so scale on the CPU;
            #  extra frames above `fps` are dropped by a pad probe before any work is done.)
            # high quality: full colour detail (4:4:4) + sharp scaling, so text stays crisp
            sharp = self.jpeg_quality >= 85
            fmt, method = ("Y444", "lanczos") if sharp else ("I420", "bilinear")
            return (f"pipewiresrc name=cap fd={fd} path={node} do-timestamp=true keepalive-time=500 resend-last=true "
                    f"! queue name=lq leaky=downstream max-size-buffers=1 "
                    f"! videoconvertscale n-threads=4 method={method} "
                    f"! video/x-raw,format={fmt},width={width},height={height},pixel-aspect-ratio=1/1 "
                    f"! queue leaky=downstream max-size-buffers=1 ! jpegenc quality={self.jpeg_quality} "
                    f"! appsink name=jpg emit-signals=true sync=false max-buffers=1 drop=true ")
        src = (f"pipewiresrc name=cap fd={fd} path={node} do-timestamp=true keepalive-time=500 resend-last=true "
               f"! queue leaky=downstream max-size-buffers=2 ! tee name=t ")
        if hw:
            enc = (f"t. ! queue leaky=downstream max-size-buffers=2 ! vapostproc "
                   f"! video/x-raw(memory:VAMemory),format=NV12,width={width},height={height},pixel-aspect-ratio=1/1 "
                   f"! videorate ! video/x-raw(memory:VAMemory),framerate={fps}/1 "
                   f"! vah264lpenc name=enc rate-control=cbr bitrate={self.bitrate} key-int-max={fps} b-frames=0 target-usage=7 "
                   f"! video/x-h264,profile=constrained-baseline ")
        else:
            enc = (f"t. ! queue leaky=downstream max-size-buffers=2 ! videoconvert ! videoscale "
                   f"! video/x-raw,format=I420,width={width},height={height},pixel-aspect-ratio=1/1 "
                   f"! videorate ! video/x-raw,framerate={fps}/1 "
                   f"! x264enc name=enc tune=zerolatency speed-preset=veryfast bitrate={self.bitrate} "
                   f"key-int-max={fps} bframes=0 threads=4 ! video/x-h264,profile=constrained-baseline ")
        enc += ("! h264parse name=par ! video/x-h264,stream-format=avc,alignment=au "
                "! mp4mux fragment-duration=1 streamable=true "
                "! appsink name=mp4 emit-signals=true sync=false max-buffers=0 ")
        mj = ("t. ! valve name=jv drop=true ! queue leaky=downstream max-size-buffers=1 "
              "! videoconvert ! videoscale ! video/x-raw,width=1280,height=720 ! jpegenc quality=80 "
              "! appsink name=jpg emit-signals=true sync=false async=false max-buffers=1 drop=true ")
        return src + enc + mj

    def start(self, fd, node, width=1920, height=1080, fps=30):
        self.init_seg = None
        self.buf = bytearray()
        for hw in ((True, False) if self.hw and self.mode != "live" else (False,)):
            desc = self._pipeline_desc(fd, node, width, height, fps, hw)
            try:
                pipe = Gst.parse_launch(desc)
            except GLib.Error as e:
                self.log(f"{'hardware' if hw else 'software'} encoder unavailable: {e.message}")
                continue
            if self.mode == "live":
                self.codec = "mjpeg"
                gap, last = 1.0 / fps * 0.9, [0.0]

                def limit(pad, info):
                    now = time.monotonic()
                    if now - last[0] < gap:
                        return Gst.PadProbeReturn.DROP
                    last[0] = now
                    return Gst.PadProbeReturn.OK
                pipe.get_by_name("lq").get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, limit)
            else:
                pipe.get_by_name("mp4").connect("new-sample", self._on_mp4)
                self._stamp_capture(pipe)
            pipe.get_by_name("jpg").connect("new-sample", self._on_jpg)
            self.last_capture = time.monotonic()
            src = pipe.get_by_name("cap")
            if src is not None:
                def seen(pad, info):
                    self.last_capture = time.monotonic()
                    self.cap_frames = getattr(self, "cap_frames", 0) + 1
                    return Gst.PadProbeReturn.OK
                src.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, seen)
            bus = pipe.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self._on_bus)
            if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                pipe.set_state(Gst.State.NULL)
                self.log("encoder failed to start, trying software")
                continue
            self.pipe, self.enc, self.hw = pipe, pipe.get_by_name("enc"), hw
            if self.mode == "live":
                self.log(f"Live pictures {width}x{height} up to {fps} fps, JPEG quality {self.jpeg_quality} "
                         "")
            else:
                self.log(f"Encoding {width}x{height}@{fps} {self.bitrate} kbit/s "
                         f"({'Intel hardware' if hw else 'software'} H.264)")
            break
        if not self.pipe:
            raise RuntimeError("Could not start the video encoder")
        if self.audio_device and self.mode != "live":
            self._start_audio()
        self._start_http()
        threading.Thread(target=self._stats, daemon=True).start()

    def _stamp_capture(self, pipe, element="par", offsets=None):
        """Record when each encoded frame was captured, in laptop wall-clock time.

        mp4mux's media time is (frame PTS - first frame PTS), so
        capture_wall - media_time is one constant per stream; the TV page adds it to
        video.currentTime to know exactly how old the picture on screen is."""
        offsets = self.media_offsets if offsets is None else offsets
        offsets.clear()
        first = [None]

        def stamp(pad, info):
            buf = info.get_buffer()
            clock = pipe.get_clock()
            ev = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
            if buf.pts == Gst.CLOCK_TIME_NONE or clock is None or ev is None:
                return Gst.PadProbeReturn.OK
            if first[0] is None:
                first[0] = buf.pts
            rt = ev.parse_segment().to_running_time(Gst.Format.TIME, buf.pts)
            age = (clock.get_time() - pipe.get_base_time() - rt) / Gst.SECOND   # capture -> now
            offsets.append(time.time() - age - (buf.pts - first[0]) / Gst.SECOND)
            return Gst.PadProbeReturn.OK
        pipe.get_by_name(element).get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, stamp)

    def media_offset(self, offsets=None):
        o = sorted(self.media_offsets if offsets is None else offsets)
        return o[len(o) // 2] if o else None

    # -- sound for the TV: its own fMP4 stream; the page lines it up with the video
    def _start_audio(self):
        desc = (f"pulsesrc name=asrc device={self.audio_device} buffer-time=40000 latency-time=10000 "
                "! audio/x-raw,rate=48000,channels=2 ! queue ! audioconvert ! audioresample "
                "! voaacenc bitrate=160000 ! aacparse name=apar ! audio/mpeg,mpegversion=4,stream-format=raw "
                "! mp4mux fragment-duration=1 streamable=true "
                "! appsink name=amp4 emit-signals=true sync=false max-buffers=0 ")
        try:
            pipe = Gst.parse_launch(desc)
        except GLib.Error as e:
            self.log(f"sound for the TV unavailable: {e.message}")
            return
        pipe.get_by_name("amp4").connect("new-sample", self._on_amp4)
        self._stamp_capture(pipe, "apar", self.audio_offsets)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)
        if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipe.set_state(Gst.State.NULL)
            self.log("sound for the TV failed to start")
            return
        self.apipe = pipe
        self.log("Sending sound to the TV (AAC 160 kbit/s)")

    def audio_shift(self):
        """Seconds the TV sound plays after (+) or before (-) the matching picture.
        Sound can't come much earlier than the picture: it must have reached the TV already."""
        return max(self.audio_delay_ms / 1000 - self.picture_delay, -(self.picture_delay - 0.5))

    def _on_amp4(self, sink):
        buf = sink.emit("pull-sample").get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        self.a_buf += info.data
        buf.unmap(info)
        while len(self.a_buf) >= 8:
            size, kind = struct.unpack(">I4s", self.a_buf[:8])
            if size < 8 or len(self.a_buf) < size:
                break
            box = bytes(self.a_buf[:size])
            del self.a_buf[:size]
            if kind == b"ftyp":
                self._a_ftyp = box
            elif kind == b"moov":
                self.a_init = strip_mehd(getattr(self, "_a_ftyp", b"") + box)
            elif kind == b"moof":
                self.a_moof = box
            elif kind == b"mdat" and self.a_moof:
                frag, self.a_moof = self.a_moof + box, None
                with self.lock:
                    clients = list(self.a_clients)
                for c in clients:
                    if c.q.qsize() > 100:      # TV fell behind (>2 s): drop what's waiting
                        with c.q.mutex:
                            c.q.queue.clear()
                    c.q.put(frag)
        return Gst.FlowReturn.OK

    def _serve_audio(self, h, ip):
        h.close_connection = True
        h.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        t0 = time.time()
        while self.a_init is None and self.apipe and time.time() - t0 < 10:
            time.sleep(0.05)
        if self.a_init is None:
            h.send_error(503, "no sound")
            return
        h._head("audio/mp4")
        h.wfile.write(self.a_init)
        c = Client()
        with self.lock:
            self.a_clients.append(c)
        self.log(f"TV {ip} is receiving sound")
        try:
            while c.alive and self.apipe:
                try:
                    h.wfile.write(c.q.get(timeout=5))
                except queue.Empty:
                    continue
        finally:
            with self.lock:
                if c in self.a_clients:
                    self.a_clients.remove(c)

    def _stats(self):
        last, t = 0, time.time()
        cap_last = 0
        while self.pipe:
            time.sleep(5)
            cap = getattr(self, "cap_frames", 0)
            self.log(f"screen capture: {(cap - cap_last) / 5:.0f} frames/s from laptop screen"
                     + ("  <-- no new frames (screen still, or capture stalled)" if cap - cap_last < 15 else ""))
            cap_last = cap
            idle = time.monotonic() - getattr(self, "last_capture", time.monotonic())
            if idle > 3:
                self.log(f"WARNING: screen capture gave no frames for {idle:.0f} s (capture stalled)")
            if self.mode == "live":
                n, now = self.jpeg_sent, time.time()
                kb = len(self.jpeg or b"") // 1024
                with self.lock:
                    g, self.tv_gaps = self.tv_gaps, []
                tv = f", TV takes ~{1000 * sum(g) / len(g):.0f} ms to show each" if g else ""
                self.log(f"laptop: {(n - last) / (now - t):.1f} pictures/s sent to TV, ~{kb} KB each{tv}")
                last, t = n, now
                continue
            n, now = getattr(self, "nfrag", 0), time.time()
            with self.lock:
                qs = [c.q.qsize() for c in self.clients]
            self.log(f"laptop: {(n - last) / (now - t):.1f} fps sent, skips={getattr(self, 'nskip', 0)}, queue={qs}")
            last, t = n, now

    def _on_bus(self, bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            self.log(f"video error: {err.message}")
            self.on_event("error", err.message)

    def force_keyframe(self):
        if self.enc:
            ev = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
            self.enc.get_static_pad("src").send_event(ev)

    def _on_mp4(self, sink):
        buf = sink.emit("pull-sample").get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        self.buf += info.data
        buf.unmap(info)
        while len(self.buf) >= 8:
            size, kind = struct.unpack(">I4s", self.buf[:8])
            if size < 8 or len(self.buf) < size:
                break
            box = bytes(self.buf[:size])
            del self.buf[:size]
            self._on_box(kind, box)
        return Gst.FlowReturn.OK

    def _on_box(self, kind, box):
        if kind == b"ftyp":
            self._ftyp = box
        elif kind == b"moov":
            self.init_seg = strip_mehd(getattr(self, "_ftyp", b"") + box)
            self.codec = codec_string(self.init_seg)
            self.timescale = media_timescale(self.init_seg)
            self.log(f"Video format {self.codec}")
        elif kind == b"moof":
            self.pending_moof = box
        elif kind == b"mdat" and self.pending_moof:
            self.nfrag = getattr(self, "nfrag", 0) + 1
            frag, key = self.pending_moof + box, fragment_is_key(self.pending_moof)
            t = fragment_time(self.pending_moof)
            if key and t is not None and self.timescale:
                self.key_times.append(t / self.timescale)
            self.pending_moof = None
            with self.lock:
                clients = list(self.clients)
            for c in clients:
                if c.need_key:
                    if not key:
                        continue
                    c.need_key = False
                if c.q.qsize() > 20:      # TV fell behind (>0.6 s): skip ahead to next keyframe
                    with c.q.mutex:
                        c.q.queue.clear()
                    c.need_key = True
                    self.nskip = getattr(self, "nskip", 0) + 1
                    self.force_keyframe()
                    continue
                c.q.put(frag)

    def _on_jpg(self, sink):
        buf = sink.emit("pull-sample").get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            with self.jpeg_cond:
                self.jpeg = bytes(info.data)
                self.jpeg_n += 1
                self.jpeg_cond.notify_all()
            buf.unmap(info)
        return Gst.FlowReturn.OK

    def _set_mjpeg(self, delta):
        with self.lock:
            self.mjpeg_clients += delta
            on = self.mjpeg_clients > 0
        jv = self.pipe.get_by_name("jv") if self.pipe else None
        if jv:
            jv.set_property("drop", not on)

    # -- http
    def _start_http(self):
        server = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _head(self, ctype, length=None):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache, no-store")
                if length is not None:
                    self.send_header("Content-Length", str(length))
                else:
                    self.send_header("Connection", "close")
                self.end_headers()

            def do_GET(self):
                ip = self.client_address[0]
                path = self.path.split("?", 1)[0]
                q = self.path.partition("?")[2]
                try:
                    if path == "/live":
                        server._serve_live(self, ip)
                    elif path == "/audio":
                        server._serve_audio(self, ip)
                    elif path == "/frame":
                        server._serve_frame(self, ip, q)
                    elif path == "/mjpeg":
                        server._serve_mjpeg(self, ip)
                    elif path == "/clock":
                        off = server.media_offset()
                        body = json.dumps({"now": time.time(), "media": off, "keys": list(server.key_times),
                                           "amedia": server.media_offset(server.audio_offsets),
                                           "ashift": server.audio_shift(),
                                           "delay": server.picture_delay}).encode()
                        self._head("application/json", len(body))
                        self.wfile.write(body)
                    elif path == "/hello":
                        what = q.split("&")[0]
                        if what == "screen":
                            server.tv_fullscreen = "fs=1" in q
                        if what in ("stats", "screen"):
                            server.log("TV " + q.rsplit("&t=", 1)[0])
                            self._head("text/plain", 2)
                            self.wfile.write(b"ok")
                            return
                        server.log(f"TV says: {what}")
                        server.on_event("tv_" + what.replace("-", "_"), ip)
                        self._head("text/plain", 2)
                        self.wfile.write(b"ok")
                    else:
                        t0 = time.time()
                        while server.codec is None and time.time() - t0 < 10:
                            time.sleep(0.1)
                        body = (PAGE.replace("__CODEC__", server.codec or "avc1.640028").replace("__MODE__", server.mode)
                                .replace("__DELAY__", str(server.picture_delay))
                                .replace("__AUDIO__", "1" if server.apipe else "0")).encode()
                        self._head("text/html; charset=utf-8", len(body))
                        self.wfile.write(body)
                        server.on_event("tv_page", ip)
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass

        for attempt in range(30):
            try:
                self.httpd = ThreadingHTTPServer(("0.0.0.0", PORT), H)
                break
            except OSError:
                if attempt == 29:
                    raise
                time.sleep(0.2)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _serve_live(self, h, ip):
        h.close_connection = True
        h.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 512 * 1024)
        h.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        t0 = time.time()
        while self.init_seg is None and time.time() - t0 < 10:
            time.sleep(0.05)
        if self.init_seg is None:
            h.send_error(503, "video not ready")
            return
        h._head("video/mp4")
        h.wfile.write(self.init_seg)
        c = Client()
        with self.lock:
            # one viewer per TV: drop older connections from the same address
            self.clients.append(c)
        self.force_keyframe()
        self.log(f"TV {ip} is receiving video")
        self.on_event("tv_streaming", ip)
        try:
            while c.alive:
                try:
                    frag = c.q.get(timeout=5)
                except queue.Empty:
                    self.force_keyframe()
                    continue
                h.wfile.write(frag)
        finally:
            c.alive = False
            with self.lock:
                if c in self.clients:
                    self.clients.remove(c)
            self.log(f"TV {ip} stopped receiving video")
            self.on_event("tv_left", ip)

    def _serve_frame(self, h, ip, q):
        args = dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv)
        cid = args.get("c", ip)
        now = time.monotonic()
        with self.lock:
            last_n, sent_t = self.frame_clients.get(cid, (-1, None))
            if sent_t is not None:
                self.tv_gaps.append(now - sent_t)      # TV time to receive+draw the last picture
            if cid not in self.frame_clients:
                new = True
            else:
                new = False
        if new:
            self.log(f"TV {ip} is receiving live pictures")
            self.on_event("tv_streaming", ip)
        with self.jpeg_cond:
            self.jpeg_cond.wait_for(lambda: (self.jpeg and self.jpeg_n != last_n) or not self.pipe, timeout=3)
            data, n = self.jpeg, self.jpeg_n
        if not data:
            h.send_error(503, "no picture yet")
            return
        h.send_response(200)
        h.send_header("Content-Type", "image/jpeg")
        h.send_header("Cache-Control", "no-cache, no-store")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
        h.wfile.flush()
        self.jpeg_sent += 1
        with self.lock:
            self.frame_clients[cid] = (n, time.monotonic())

    def _serve_mjpeg(self, h, ip):
        h.close_connection = True
        h.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 128 * 1024)
        h.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        h._head("multipart/x-mixed-replace; boundary=frame")
        self._set_mjpeg(+1)
        self.log(f"TV {ip} is receiving {'live pictures' if self.mode == 'live' else 'pictures (MJPEG fallback)'}")
        self.on_event("tv_streaming", ip)
        last = -1
        try:
            while self.pipe:
                with self.jpeg_cond:
                    self.jpeg_cond.wait_for(lambda: self.jpeg_n != last, timeout=5)
                    data, last = self.jpeg, self.jpeg_n
                if data:
                    h.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                  % len(data) + data + b"\r\n")
                    self.jpeg_sent += 1
        finally:
            self._set_mjpeg(-1)
            self.on_event("tv_left", ip)

    def url_for(self, tv_ip):
        return f"http://{local_ip_towards(tv_ip)}:{PORT}/"

    def stop(self):
        with self.lock:
            for c in self.clients:
                c.alive = False
            self.clients = []
        if self.pipe:
            self.pipe.get_bus().remove_signal_watch()
            self.pipe.set_state(Gst.State.NULL)
            self.pipe = None
            self.enc = None
        if self.apipe:
            self.apipe.get_bus().remove_signal_watch()
            self.apipe.set_state(Gst.State.NULL)
            self.apipe = None
        with self.lock:
            for c in self.a_clients:
                c.alive = False
            self.a_clients = []
        with self.jpeg_cond:
            self.jpeg_cond.notify_all()
        if self.httpd:
            h, self.httpd = self.httpd, None
            threading.Thread(target=lambda: (h.shutdown(), h.server_close()), daemon=True).start()
