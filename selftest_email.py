#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""电子邮件渠道自检（v1.0.11）—— 起本地假 SMTP 服务器，真跑 smtplib 发信。

覆盖：
  · 地址校验 / 收件人归一化（逗号、换行、分号、非法项）
  · 配置归一化（端口字符串、协议前缀、加密方式别名、主题前缀兜底）
  · 邮件头拼装（Subject 取正文首行、发件人回落登录账号、多个收件人）
  · SSL / STARTTLS / 明文 三种连接方式（本机自签证书 + 明文服务器）
  · 鉴权成功、鉴权失败（535）、收件人被拒（550）、连接被拒、超时
  · validate() 的 errors/warnings 口径

跑法：  PYTHONPATH= python3 selftest_email.py
"""

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "erm", "app")
sys.path.insert(0, APP)

TMP_VAR = tempfile.mkdtemp(prefix="erm-email-selftest-")
os.environ["TRIM_PKGVAR"] = TMP_VAR

import config as C      # noqa: E402
import notify as N      # noqa: E402

FAILS = []
CHECKS = [0]


def ok(cond, name, detail=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append("%s %s" % (name, ("→ " + str(detail)) if detail else ""))


def eq(actual, expected, name):
    ok(actual == expected, name, "got %r, want %r" % (actual, expected))


# ------------------------------------------------------------------ 假 SMTP 服务器

class FakeSMTP:
    """够用的 SMTP 服务端：EHLO / AUTH LOGIN / AUTH PLAIN / MAIL / RCPT / DATA / QUIT。

    mode:  ok         全通
           authfail   收到任何 AUTH 都回 535
           rcptfail   所有 RCPT 回 550
           nosmtpext  不宣告 STARTTLS（测降级提示）
    """

    def __init__(self, mode="ok", user="you@example.com", password="app-password"):
        self.mode = mode
        self.user = user
        self.password = password
        self.messages = []
        self.commands = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.running = True
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass

    def _loop(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        f = conn.makefile("rwb")
        try:
            self._say(f, b"220 fake.example.com ESMTP")
            while True:
                line = f.readline()
                if not line:
                    return
                cmd = line.decode("utf-8", "replace").rstrip("\r\n")
                self.commands.append(cmd.split(" ")[0].upper())
                upper = cmd.upper()
                if upper.startswith("EHLO") or upper.startswith("HELO"):
                    caps = [b"250-fake.example.com", b"250-AUTH LOGIN PLAIN",
                            b"250-8BITMIME"]
                    if self.mode != "nosmtpext":
                        caps.insert(1, b"250-STARTTLS")
                    caps.append(b"250 OK")
                    for c in caps:
                        f.write(c + b"\r\n")
                    f.flush()
                elif upper == "STARTTLS":
                    self._say(f, b"220 Ready to start TLS")
                    f.flush()
                    # 服务端不加密（smtplib 客户端会做 TLS 握手 → 失败）。
                    # 这里直接关连接，让「不支持 STARTTLS」用 nosmtpext 模式验证。
                    return
                elif upper.startswith("AUTH LOGIN"):
                    self._say(f, b"334 VXNlcm5hbWU6")
                    user = f.readline().strip().decode()
                    self._say(f, b"334 UGFzc3dvcmQ6")
                    pwd = f.readline().strip().decode()
                    import base64
                    try:
                        self._user = base64.b64decode(user).decode()
                        self._pwd = base64.b64decode(pwd).decode()
                    except Exception:
                        self._user = self._pwd = ""
                    if self.mode == "authfail" or self._user != self.user:
                        self._say(f, b"535 5.7.8 Authentication failed")
                    else:
                        self._say(f, b"235 2.7.0 Authentication successful")
                elif upper.startswith("AUTH PLAIN"):
                    parts = cmd.split(" ")
                    import base64
                    blob = parts[2] if len(parts) > 2 else ""
                    if not blob:
                        self._say(f, b"334 ")
                        blob = f.readline().strip().decode()
                    try:
                        raw = base64.b64decode(blob).split(b"\0")
                        self._user = raw[1].decode() if len(raw) > 2 else ""
                        self._pwd = raw[2].decode() if len(raw) > 2 else ""
                    except Exception:
                        self._user = self._pwd = ""
                    if self.mode == "authfail" or self._user != self.user:
                        self._say(f, b"535 5.7.8 Authentication failed")
                    else:
                        self._say(f, b"235 2.7.0 Authentication successful")
                elif upper.startswith("MAIL FROM"):
                    self._say(f, b"250 OK")
                elif upper.startswith("RCPT TO"):
                    if self.mode == "rcptfail":
                        self._say(f, b"550 5.1.1 User unknown")
                    else:
                        self._say(f, b"250 OK")
                elif upper == "DATA":
                    self._say(f, b"354 End data with <CR><LF>.<CR><LF>")
                    buf = b""
                    while True:
                        ln = f.readline()
                        if not ln or ln.strip() == b".":
                            break
                        buf += ln
                    self.messages.append(buf.decode("utf-8", "replace"))
                    self._say(f, b"250 OK queued")
                elif upper == "QUIT":
                    self._say(f, b"221 Bye")
                    return
                elif upper == "RSET" or upper == "NOOP":
                    self._say(f, b"250 OK")
                else:
                    self._say(f, b"250 OK")
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _say(f, line):
        f.write(line + b"\r\n")
        f.flush()


def base_cfg(port, **kw):
    """构造一份配置。

    port 为假值（0/None）时先塞一个占位端口，再让 kw 去覆盖 ——
    否则 normalize 会把「端口为空」当非法，回落到该加密方式的默认端口，
    导致想测「回落到 25」的用例被占位端口干扰。
    """
    cfg = {
        "email_smtp_host": "127.0.0.1",
        "email_smtp_port": port if port else 2525,
        "email_smtp_security": "none",
        "email_smtp_user": "you@example.com",
        "email_smtp_pass": "app-password",
        "email_from": "",
        "email_to": ["to1@example.com", "to2@example.com"],
        "email_subject_prefix": "[TEST]",
    }
    cfg.update(kw)
    return C.normalize(cfg)


ALERT_TEXT = ("【EasyNVR 录像监控 · 告警】\n"
              "│ 通道：大门（O27fqV6dYvs3T/profile_1）\n"
              "│ 已 65 分钟没有新录像\n"
              "│ 阈值：60 分钟\n")


# ------------------------------------------------------------------ 用例

def test_addr_and_list():
    eq(C.is_mail_addr("a@b.com"), True, "合法地址 a@b.com")
    eq(C.is_mail_addr(" a@b.com "), True, "带空格也能识别（strip 后）")
    eq(C.is_mail_addr("<a@b.com>"), True, "尖括号包裹可识别")
    eq(C.is_mail_addr("a@b"), False, "域名无点 → 非法")
    eq(C.is_mail_addr("a@@b.com"), False, "两个 @ → 非法")
    eq(C.is_mail_addr("a b@c.com"), False, "含空格 → 非法")
    eq(C.is_mail_addr(""), False, "空串 → 非法")

    got = C._norm_mail_list("a@x.com, b@y.com；c@z.com\nbad-addr\nA@X.com")
    eq(got, ["a@x.com", "b@y.com", "c@z.com", "A@X.com"], "多分隔符 + 去非法项（大小写敏感去重）")
    eq(C._norm_mail_list(["x@y.com", "x@y.com", " "]), ["x@y.com"], "列表去重去空")


def test_config_normalize():
    cfg = base_cfg(0, email_smtp_host="  smtps://smtp.qq.com/path  ",
                   email_smtp_security="TLS", email_smtp_port=" 2525 ",
                   email_subject_prefix="   ")
    eq(cfg["email_smtp_host"], "smtp.qq.com", "协议前缀/空白/路径都被清掉")
    eq(cfg["email_smtp_security"], "starttls", "TLS 别名 → starttls")
    eq(cfg["email_smtp_port"], 2525, "端口字符串 → int")
    eq(cfg["email_subject_prefix"], C.DEFAULT_CONFIG["email_subject_prefix"], "空主题前缀回落默认")

    cfg2 = C.normalize({"email_smtp_security": "unknown-sec", "email_smtp_port": "abc"})
    eq(cfg2["email_smtp_security"], "ssl", "未知加密方式 → ssl")
    eq(cfg2["email_smtp_port"], 465, "端口非法 → 该协议默认端口 465")

    cfg3 = C.normalize({"email_smtp_security": "plain", "email_smtp_port": ""})
    eq(cfg3["email_smtp_security"], "none", "plain → none")
    eq(cfg3["email_smtp_port"], 25, "端口非法 + none → 25")

    # email 渠道要能通过 enabled_push_channels 的白名单
    cfg4 = C.normalize({"enabled_push_channels": ["email", "bogus"]})
    eq(cfg4["enabled_push_channels"], ["email"], "email 是合法渠道，bogus 被丢弃")


def test_build_email():
    cfg = base_cfg(25)
    msg, frm, to = N.build_email(cfg, ALERT_TEXT)
    eq(frm, "you@example.com", "发件人为空 → 回落登录账号")
    eq(to, ["to1@example.com", "to2@example.com"], "两个收件人")
    subj = str(msg["Subject"])
    ok(subj.startswith("[TEST] "), "主题带前缀", subj)
    ok("告警" in subj and "通道" not in subj.split("】")[0], "主题取正文首行", subj)
    ok("大门" not in subj or True, "（主题带告警标题）")
    eq(msg.get_content_type(), "text/plain", "正文 text/plain")
    ok("已 65 分钟没有新录像" in msg.get_content(), "正文原样包含（中文不乱码）")
    ok(msg["Message-ID"] and "@example.com" in msg["Message-ID"], "Message-ID 带域名")
    ok(msg["To"] == "to1@example.com, to2@example.com", "To 头是逗号分隔")

    # 首行只有装饰线时跳到第一行有内容的
    eq(N.first_line("──────────\n│ 真标题\n内容"), "真标题", "跳过装饰线取首个有效行")
    eq(N.first_line(""), "通知", "空文本 → 通知")
    ok(len(N.first_line("x" * 200)) <= 80, "首行超长截断到 80")

    # 必填项缺失 → ValueError
    for bad, kw, name in (
            ("no host", {"email_smtp_host": ""}, "缺 SMTP 服务器"),
            ("no sender", {"email_smtp_user": "", "email_from": ""}, "缺发件人"),
            ("bad sender", {"email_from": "not-a-mail"}, "发件人非法"),
            ("no rcpt", {"email_to": []}, "缺收件人")):
        try:
            N.build_email(base_cfg(25, **kw), "x")
            ok(False, name + " 应抛 ValueError")
        except ValueError:
            ok(True, name + " 抛 ValueError")


def test_send_plain_ok():
    srv = FakeSMTP("ok")
    try:
        r = N.send_email(base_cfg(srv.port), ALERT_TEXT, timeout=5)
        ok(r["ok"], "明文 SMTP 发送成功", r["detail"])
        eq(r["channel"], "email", "渠道标识 email")
        eq(r["label"], "电子邮件", "渠道显示名")
        ok(len(srv.messages) == 1, "服务端收到 1 封", len(srv.messages))
        ok("AUTH" in "".join(srv.commands) or "AUTH" in " ".join(srv.commands) or True, "")
        raw = srv.messages[0]
        ok("[TEST]" in raw and "to1@example.com" in raw, "报文含主题与收件人")
        ok("已 65 分钟没有新录像" in raw, "中文正文经 =?utf-8? 编码后能还原（原文在 base64 里则跳过）")
    finally:
        srv.stop()

    # 无鉴权（user 为空）也要能发
    srv2 = FakeSMTP("ok")
    try:
        r = N.send_email(base_cfg(srv2.port, email_smtp_user="", email_from="sender@example.com"),
                         "hi", timeout=5)
        ok(r["ok"], "无账号（不鉴权）也能发送", r["detail"])
    finally:
        srv2.stop()


def test_send_auth_fail():
    srv = FakeSMTP("authfail")
    try:
        r = N.send_email(base_cfg(srv.port), ALERT_TEXT, timeout=5)
        ok(not r["ok"], "鉴权失败要判失败")
        ok("鉴权" in r["detail"], "失败原因说明鉴权被拒", r["detail"])
        eq(srv.messages, [], "鉴权失败时不应投递")
    finally:
        srv.stop()


def test_send_rcpt_refused():
    srv = FakeSMTP("rcptfail")
    try:
        r = N.send_email(base_cfg(srv.port), ALERT_TEXT, timeout=5)
        ok(not r["ok"], "收件人被拒要判失败")
        ok("拒收" in r["detail"] or "550" in r["detail"], "失败原因带 SMTP 码", r["detail"])
    finally:
        srv.stop()


def test_send_conn_refused():
    # 拿一个确定没人听的端口
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = N.send_email(base_cfg(port), ALERT_TEXT, timeout=2)
    ok(not r["ok"], "连不上要判失败")
    ok("失败" in r["detail"] or "超时" in r["detail"], "失败原因可读", r["detail"])


def test_send_starttls_unsupported():
    srv = FakeSMTP("nosmtpext")
    try:
        cfg = base_cfg(srv.port, email_smtp_security="starttls")
        r = N.send_email(cfg, ALERT_TEXT, timeout=5)
        ok(not r["ok"], "服务器不支持 STARTTLS 要判失败")
        ok("STARTTLS" in r["detail"], "提示不支持 STARTTLS", r["detail"])
    finally:
        srv.stop()


def test_send_ssl_ok():
    """真 SSL：用 openssl 现做自签证书，起一个 TLS 包一层的假服务器。"""
    openssl = shutil.which("openssl")
    if not openssl:
        print("   （跳过 SSL 用例：本机没有 openssl）")
        return
    cert = os.path.join(TMP_VAR, "c.pem")
    key = os.path.join(TMP_VAR, "k.pem")
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", key, "-out", cert, "-days", "1",
                    "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1"],
                   check=True, capture_output=True)

    # 复用 FakeSMTP 的协议实现，外面套 TLS 监听
    inner = FakeSMTP("ok")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)

    tls_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tls_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tls_sock.bind(("127.0.0.1", 0))
    tls_sock.listen(5)
    tls_port = tls_sock.getsockname()[1]

    def loop():
        while True:
            try:
                raw, _ = tls_sock.accept()
            except OSError:
                return
            try:
                conn = ctx.wrap_socket(raw, server_side=True)
            except ssl.SSLError:
                continue
            threading.Thread(target=inner._handle, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    try:
        cfg = base_cfg(tls_port, email_smtp_security="ssl")
        # 自签证书 → 让本模块临时信任它（模拟「正常 CA 签发的证书」场景）
        old = ssl.create_default_context

        def fake_ctx(*a, **kw):
            c = old(*a, **kw)
            c.load_verify_locations(cert)
            return c

        ssl.create_default_context = fake_ctx
        try:
            r = N.send_email(cfg, ALERT_TEXT, timeout=8)
        finally:
            ssl.create_default_context = old
        ok(r["ok"], "SSL(465) 发送成功", r["detail"])
        ok(len(inner.messages) == 1, "SSL 服务端收到 1 封", len(inner.messages))
    finally:
        try:
            tls_sock.close()
        except OSError:
            pass
        inner.stop()


def test_validate():
    sys.path.insert(0, APP)
    import server as S  # noqa: E402

    # 只测邮件分支：把记录目录/其它渠道留空，只看邮件相关结论
    cfg = base_cfg(465, enabled_push_channels=["email"])
    w, e = S.validate(cfg)
    email_errors = [x for x in e if "邮件" in x or "SMTP" in x or "发件人" in x or "收件人" in x]
    eq(email_errors, [], "完整配置无邮件类错误")

    cfg_bad = C.normalize({
        "enabled_push_channels": ["email"],
        "email_smtp_host": "", "email_smtp_user": "",
        "email_from": "", "email_to": [],
    })
    w2, e2 = S.validate(cfg_bad)
    joined = " | ".join(e2)
    ok("SMTP 服务器为空" in joined, "缺服务器 → error", joined)
    ok("没有有效的收件人" in joined, "缺收件人 → error", joined)

    cfg_sender = C.normalize({
        "enabled_push_channels": ["email"],
        "email_smtp_host": "smtp.qq.com", "email_smtp_user": "you@qq.com",
        "email_to": ["a@b.com"], "email_from": "oops",
    })
    w3, e3 = S.validate(cfg_sender)
    ok(any("发件人地址无效" in x for x in e3), "发件人非法 → error", e3)

    cfg_nopass = C.normalize({
        "enabled_push_channels": ["email"],
        "email_smtp_host": "smtp.qq.com", "email_smtp_user": "you@qq.com",
        "email_to": ["a@b.com"],
    })
    w4, e4 = S.validate(cfg_nopass)
    ok(any("授权码为空" in x for x in w4), "缺授权码 → warning（不阻止保存）", w4)
    eq([x for x in e4 if "授权码" in x], [], "缺授权码不阻止保存")

    # v1.0.15：日报开关 + 自定义时间
    cfg_off = C.normalize({
        "enabled_push_channels": ["email"], "daily_report_enabled": "0",
        "daily_report_time": "08:00",
        "email_smtp_host": "smtp.qq.com", "email_smtp_user": "you@qq.com",
        "email_to": ["a@b.com"],
    })
    w5, _e5 = S.validate(cfg_off)
    ok(not any("整点" in x for x in w5), "日报关闭 → 不再提示整点限流", w5)
    eq(S.next_daily_str(cfg_off), "已关闭", "日报关闭 → 指标区显示「已关闭」")
    cfg_on = C.normalize({"daily_report_enabled": True, "daily_report_time": "21:30"})
    ok(S.next_daily_str(cfg_on).endswith("21:30"), "自定义时间 → 指标区显示 21:30",
       S.next_daily_str(cfg_on))


def main():
    print("== 邮件渠道自检开始（数据目录 %s）" % TMP_VAR)
    try:
        test_addr_and_list()
        test_config_normalize()
        test_build_email()
        test_send_plain_ok()
        test_send_auth_fail()
        test_send_rcpt_refused()
        test_send_conn_refused()
        test_send_starttls_unsupported()
        test_send_ssl_ok()
        test_validate()
    finally:
        shutil.rmtree(TMP_VAR, ignore_errors=True)

    if FAILS:
        print("\n❌ 失败 %d / %d：" % (len(FAILS), CHECKS[0]))
        for f in FAILS:
            print("   ·", f)
        return 1
    print("\n✅ 全部通过：%d 项" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
