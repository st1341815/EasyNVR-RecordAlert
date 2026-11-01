#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端：真起 server.py 网关 + 假 SMTP，走 HTTP API 做「保存邮件配置 → 测试推送」。

跑法：  PYTHONPATH= python3 e2e_email.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "erm", "app")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="erm-e2e-")
os.environ["TRIM_PKGVAR"] = TMP

from selftest_email import FakeSMTP  # noqa: E402

FAILS = []
N = [0]


def ok(cond, name, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append("%s %s" % (name, ("→ " + str(detail)) if detail else ""))
    print("  %s %s" % ("✓" if cond else "✗", name))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    smtp = FakeSMTP("ok")
    sock_path = os.path.join(APP, "erm.sock")
    env = dict(os.environ)
    env["TRIM_PKGVAR"] = TMP
    env["TRIM_APPDEST"] = TMP                       # 让 soket 落在临时目录
    env["PYTHONPATH"] = ""
    proc = subprocess.Popen(
        [sys.executable, os.path.join(APP, "server.py")],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    for _ in range(50):
        if os.path.exists(sock_path):
            break
        time.sleep(0.1)
    ok(os.path.exists(sock_path), "网关 unix socket 已就绪（%s）" % sock_path)

    def call(action, extra=None):
        """走 unix socket 发一个 POST /app/erm/api。"""
        body = json.dumps(dict({"action": action}, **(extra or {}))).encode()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(30)
        s.connect(sock_path)
        req = (b"POST /app/erm/api HTTP/1.1\r\nHost: localhost\r\n"
               b"Content-Type: application/json\r\n"
               b"Content-Length: " + str(len(body)).encode() + b"\r\n"
               b"Connection: close\r\n\r\n" + body)
        s.sendall(req)
        buf = b""
        while True:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        s.close()
        head, _, payload = buf.partition(b"\r\n\r\n")
        return json.loads(payload.decode("utf-8") or "{}")

    try:
        try:
            st = call("status")
        except Exception as e:
            print("  连 socket 失败：%s" % e)
            proc.terminate()
            out = proc.communicate(timeout=5)[0].decode("utf-8", "replace")
            print(out[-2000:])
            return 1

        ok(True, "网关起得来并响应 status")
        cfg = {
            "record_root": "", "scan_interval": 300,
            "enabled_push_channels": ["email"],
            "email_smtp_host": "127.0.0.1",
            "email_smtp_port": smtp.port,
            "email_smtp_security": "none",
            "email_smtp_user": "you@example.com",
            "email_smtp_pass": "app-password",
            "email_from": "",
            "email_to": "to1@example.com\nto2@example.com",
            "email_subject_prefix": "[E2E]",
        }
        d = call("save_config", {"config": cfg})
        ok(d.get("saved"), "保存邮件配置成功", d.get("error"))
        ec = (d.get("config") or {})
        ok(ec.get("enabled_push_channels") == ["email"], "渠道列表含 email", ec.get("enabled_push_channels"))
        ok(ec.get("email_to") == ["to1@example.com", "to2@example.com"], "换行分隔的收件人被正确拆分", ec.get("email_to"))
        ok(ec.get("email_smtp_port") == smtp.port, "端口保存正确", ec.get("email_smtp_port"))

        d2 = call("test_push", {"channel": "email"})
        r = (d2.get("results") or [{}])[0]
        ok(r.get("ok"), "测试邮件发送成功", r.get("detail"))
        # 注意：网关启动时监控循环会先跑一轮（可能补发当天日报），所以这里 ≥1 而不是 ==1
        ok(len(smtp.messages) >= 1, "SMTP 服务端收到邮件（%d 封）" % len(smtp.messages), len(smtp.messages))
        raw = "\n".join(smtp.messages)
        ok("[E2E]" in raw, "主题前缀写进报文")
        ok("EasyNVR 录像监控" in raw or "5L2g" in raw, "正文为监控测试文案")

        d3 = call("doctor")
        chk = {c["item"]: c for c in (d3.get("checks") or [])}
        ok("SMTP 服务器" in chk, "自检里有 SMTP 服务器项", list(chk))
        ok("收件人" in chk, "自检里有收件人项", list(chk))

        # 非法渠道名要被拒（不能 500）
        d4 = call("test_push", {"channel": "bogus"})
        ok(d4.get("ok") is False and "未知渠道" in str(d4.get("error")), "未知渠道被拒且不崩", d4)
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except Exception:
            proc.kill()
        smtp.stop()
        shutil.rmtree(TMP, ignore_errors=True)

    if FAILS:
        print("\n❌ 失败 %d / %d" % (len(FAILS), N[0]))
        for f in FAILS:
            print("   ·", f)
        return 1
    print("\n✅ E2E_EMAIL_OK  %d 项" % N[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
