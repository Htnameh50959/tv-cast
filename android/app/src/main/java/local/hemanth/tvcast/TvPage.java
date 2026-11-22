package local.hemanth.tvcast;

/** The web page served to the TV's browser. It shows the phone's screen as an MJPEG stream. */
public final class TvPage {
    private TvPage() {
    }

    public static String html(String device) {
        return PAGE.replace("__DEVICE__", device);
    }

    private static final String PAGE = """
            <!doctype html>
            <html>
            <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <title>TV Cast</title>
            <style>
              html,body{margin:0;width:100%;height:100%;background:#000;overflow:hidden;cursor:none}
              img{position:absolute;left:0;top:0;width:100%;height:100%;object-fit:contain}
              #m{position:absolute;left:0;right:0;top:46%;text-align:center;color:#ccc;font:28px sans-serif}
            </style>
            </head>
            <body>
            <img id="v" alt="">
            <div id="m">Connecting to __DEVICE__&hellip;</div>
            <script>
              var v=document.getElementById('v'),m=document.getElementById('m');
              function beacon(w){try{var x=new XMLHttpRequest();x.open('GET','/hello?'+w+'&t='+Date.now());x.send();}catch(e){}}
              function full(){var d=document.documentElement,f=d.requestFullscreen||d.webkitRequestFullscreen||d.webkitRequestFullScreen;
                if(f&&!(document.fullscreenElement||document.webkitFullscreenElement)){try{f.call(d);}catch(e){}}}
              document.addEventListener('click',full);
              document.addEventListener('keydown',full);
              v.onload=function(){m.style.opacity=0;beacon('playing');};
              v.onerror=function(){m.style.opacity=1;m.textContent='Reconnecting…';setTimeout(load,1200);};
              function load(){beacon('mjpeg');v.src='/mjpeg?'+Date.now();}
              load();
            </script>
            </body>
            </html>
            """;
}
