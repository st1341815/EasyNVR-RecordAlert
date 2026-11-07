#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把已安装的 erm 真实实例（静态目录 + unix socket）暴露到本地端口，给没有 fnOS 登录态的
浏览器做真机巡检。

- 静态：/vol1/@appcenter/erm/www
- API ：/app/erm/api/* → /vol1/@appcenter/erm/erm.sock 的 /api/*

socket 属主是 erm:erm(660)，所以要用 root 跑：
    sudo python3 tools/real_proxy.py [端口]
"""

import http.client
import os
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18083
WWW = sys.argv[2] if len(sys.argv) > 2 else "/vol1/@appcenter/erm/www"
SOCK = "/vol1/@appcenter/erm/erm.sock"

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
        ".ico": "image/x-icon", ".json": "application/json; charset=utf-8"}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _plain(self, status, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _proxy(self, method):
        raw = self.path
        q = raw.split("?", 1)[1] if "?" in raw else ""
        p = raw.split("?")[0][len("/app/erm/api"):] or "/"
        target = "/api" + p + (("?" + q) if q else "")
        body = None
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
        c = http.client.HTTPConnection("localhost")
        c.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.sock.settimeout(30)
        try:
            c.sock.connect(SOCK)
            hdrs = {"Content-Type": self.headers.get("Content-Type", "application/json")}
            c.request(method, target, body=body, headers=hdrs)
            r = c.getresponse()
            data = r.read()
            self.send_response(r.status)
            self.send_header("Content-Type", r.getheader("Content-Type") or "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._plain(502, ("proxy error: %s" % e).encode())
        finally:
            try:
                c.close()
            except Exception:
                pass

    def do_GET(self):
        if self.path.startswith("/app/erm/api"):
            return self._proxy("GET")
        p = self.path.split("?")[0]
        if p.startswith("/app/erm"):
            p = p[len("/app/erm"):] or "/"
        if p in ("", "/"):
            p = "/index.html"
        fp = os.path.join(WWW, p.lstrip("/"))
        if not os.path.isfile(fp):
            return self._plain(404, b"not found")
        with open(fp, "rb") as f:
            data = f.read()
        ctype = MIME.get(os.path.splitext(fp)[1].lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.startswith("/app/erm/api"):
            return self._proxy("POST")
        return self._plain(405, b"method not allowed")


if __name__ == "__main__":
    print("serving %s on http://127.0.0.1:%d/app/erm/ (socket %s)" % (WWW, PORT, SOCK))
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
