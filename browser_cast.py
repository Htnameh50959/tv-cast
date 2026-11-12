#!/usr/bin/env python3
"""Command-line caster: python3 browser_cast.py [TV_IP]   (Ctrl+C to stop)"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gi.repository import GLib  # noqa: E402

import netcast  # noqa: E402
from screencast import ScreenCast  # noqa: E402


def log(*a):
    print(*a, flush=True)


def main():
    if len(sys.argv) > 1:
        tv = {"ip": sys.argv[1], "name": sys.argv[1]}
    else:
        tvs = netcast.find_tvs()
        tv = tvs[0] if tvs else netcast.last_tv()
        if not tv:
            sys.exit("No LG TV found on the network. Is it on?")
    log(f"TV: {tv['name']} ({tv['ip']})")

    loop = GLib.MainLoop()
    server = netcast.CastServer(lambda m: log(m), lambda ev, d: None)
    remote = netcast.LGRemote(tv["ip"], log, lambda: log("Press OK on the TV remote to allow."))
    sc = ScreenCast(log)

    def ready(fd, node):
        try:
            server.start(fd, node)
        except RuntimeError as e:
            log(e)
            loop.quit()
            return
        url = server.url_for(tv["ip"])
        log(f"Serving {url}")

        def open_tv():
            try:
                remote.open_url(url)
                netcast.remember_tv(tv)
                log("Opened on the TV.")
            except Exception as e:
                log(f"Could not open the TV browser ({e}). Open {url} on the TV.")
        threading.Thread(target=open_tv, daemon=True).start()

    sc.start(ready, lambda m: (log(m), loop.quit()))
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        sc.close()
        threading.Thread(target=remote.close_browser, daemon=True).start()


if __name__ == "__main__":
    main()
