#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 mock 推送服务：模拟钉钉/企微/飞书机器人，便于在没有真实机器人时验收推送链路。

- 收到的每个请求体追加到 /vol1/1000/erm-test/webhook.log
- /vol1/1000/erm-test/mode 内容决定响应：ok | dingtalk_err | http500 | feishu_ok | slow
"""

import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = "/tmp/erm-mock"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            mode = open(BASE + "/mode").read().strip() or "ok"
        except Exception:
            mode = "ok"
        with open(BASE + "/webhook.log", "a", encoding="utf-8") as f:
            f.write("--- %s mode=%s path=%s\n" % (time.strftime("%F %T"), mode, self.path))
            f.write(raw.decode("utf-8", "replace") + "\n")
        if mode == "slow":
            time.sleep(3)
        if mode == "http500":
            body, code = "boom", 500
        elif mode == "dingtalk_err":
            body, code = json.dumps({"errcode": 310000, "errmsg": "keywords not in content"}), 200
        elif mode == "feishu_ok":
            body, code = json.dumps({"code": 0, "msg": "success"}), 200
        else:
            body, code = json.dumps({"errcode": 0, "errmsg": "ok"}), 200
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 18080), H).serve_forever()
