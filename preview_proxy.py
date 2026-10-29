#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地预览代理：不登录 fnOS 也能看应用页面。

把 http://127.0.0.1:18081/app/erm 映射为
  · /app/erm/api  → 转发到应用自己的 unix socket（真实数据）
  · 其它          → 直接读 /vol1/@appcenter/erm/www
仅供验证前端，不参与打包。
"""

import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WWW = "/vol1/@appcenter/erm/www"
SOCK = "/vol1/@appcenter/erm/erm.sock"
PREFIX = "/app/erm"

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml"}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _proxy(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(20)
        s.connect(SOCK)
        req = ("%s %s HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
               "Content-Length: %d\r\nConnection: close\r\n\r\n" % (method, self.path, len(body)))
        s.sendall(req.encode() + body)
        data = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
        s.close()
        head, _, payload = data.partition(b"\r\n\r\n")
        try:
            status = int(head.split()[1])
        except Exception:
            status = 502
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
        p = self.path.split("?")[0]
        if p.startswith(PREFIX):
            p = p[len(PREFIX):]
        if not p or p == "/":
            p = "/index.html"
        fp = os.path.join(WWW, p.lstrip("/"))
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
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if "/api" in self.path:
            self._proxy("GET")
        else:
            self._static()

    def do_POST(self):
        self._proxy("POST")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 18081), H).serve_forever()
