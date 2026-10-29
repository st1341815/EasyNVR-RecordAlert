#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 UI 调试：把「已装好」的应用形态跑起来，并用 TCP 端口暴露给浏览器。

- server.py 用 TRIM_APPDEST / TRIM_PKGVAR 指到临时目录（不动系统）
- 18081 端口：静态文件直出 www/，/app/erm/api 转发到应用自己的 unix socket
- 录像目录用 e2e 造的那棵 EasyNVR 树，通道表里有真实数据可看

    PYTHONPATH= python3 run_local_ui.py [端口]
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18081
BASE = "/tmp/erm-ui"
APP = os.path.join(BASE, "app")
VAR = os.path.join(BASE, "var")
REC = os.path.join(BASE, "NVR", "easynvr_rec")
SOCK = os.path.join(APP, "erm.sock")
SRC_APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "erm", "app")

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml"}


def mk(path, when, n=1):
    os.makedirs(path, exist_ok=True)
    for i in range(n):
        fp = os.path.join(path, "%s-%05d.mp4" % (when.strftime("%Y%m%d%H%M%S"), i))
        open(fp, "wb").write(b"\0" * 16)
        ts = when.timestamp() + i
        os.utime(fp, (ts, ts))


def seed():
    now = datetime.now()
    d1 = now.strftime("%Y%m%d")
    d0 = (now - timedelta(days=1)).strftime("%Y%m%d")
    for cam, prof in (("O27fqV6dYvs3T", "profile_1"), ("Ou5Rv3hRv7gaU", "profile_1")):
        mk(os.path.join(REC, cam, prof, d1), now - timedelta(seconds=45), 6)
        mk(os.path.join(REC, cam, prof, d0), now - timedelta(days=1, hours=2), 40)
        ev = os.path.join(REC, cam, prof, d1 + "_event")
        os.makedirs(ev, exist_ok=True)
        for i in range(3):
            open(os.path.join(ev, "20261029%06d_%d.jpg" % (i, i)), "wb").write(b"\0" * 8)
    os.makedirs(os.path.join(REC, "De45adQ0Mn7U", "profile_1", d1), exist_ok=True)


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _api(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(SOCK)
        s.sendall(("%s %s HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                   "Content-Length: %d\r\nConnection: close\r\n\r\n" % (method, self.path, len(body))
                   ).encode() + body)
        data = b""
        while True:
            c = s.recv(65536)
            if not c:
                break
            data += c
        s.close()
        head, _, payload = data.partition(b"\r\n\r\n")
        status = int(head.split()[1]) if head else 502
        ctype = "application/json; charset=utf-8"
        for line in head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-type:"):
                ctype = line.split(b":", 1)[1].strip().decode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _static(self):
        p = (self.path.split("?")[0] or "/")
        if p.startswith("/app/erm"):
            p = p[len("/app/erm"):]
        if p in ("", "/"):
            p = "/index.html"
        fp = os.path.join(APP, "www", p.lstrip("/"))
        if not os.path.isfile(fp):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = open(fp, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(fp)[1].lower(),
                                                  "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._api("GET") if "/api" in self.path else self._static()

    def do_POST(self):
        self._api("POST")


def main():
    shutil.rmtree(BASE, ignore_errors=True)
    shutil.copytree(SRC_APP, APP)
    os.makedirs(VAR, exist_ok=True)
    seed()

    env = dict(os.environ, TRIM_APPDEST=APP, TRIM_PKGVAR=VAR, PYTHONPATH="",
               PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen([sys.executable, "-X", "utf8", os.path.join(APP, "server.py")],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if os.path.exists(SOCK):
            break
        time.sleep(0.25)

    # 配好录像目录（走应用自己的 API，和真机一致）
    import json
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    body = json.dumps({"action": "save_config", "config": {
        "record_root": REC, "alert_threshold_minutes": "60", "scan_interval": "60",
        "video_exts": ".mp4", "daily_report_time": "08:00"}}).encode()
    s.sendall(b"POST /app/erm/api HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
              + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
    s.recv(65536)
    s.close()
    # 触发一次扫描，让通道表有数据
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    body = json.dumps({"action": "scan"}).encode()
    s.sendall(b"POST /app/erm/api HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
              + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
    s.recv(65536)
    s.close()

    print("UI 就绪：http://127.0.0.1:%d/app/erm   （Ctrl-C 结束）" % PORT)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Proxy)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
