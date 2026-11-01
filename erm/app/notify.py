#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送（钉钉 / 飞书 / 企业微信 / 电子邮件）—— 纯标准库，无第三方依赖。

对应文档评审 B6：原文档 `requests.post(...)` 不看状态码、不解析响应体就记「已推送」，
钉钉/企微在 HTTP 200 下也可能返回 errcode≠0（关键词不匹配 310000、限流 130101），
于是"以为发了、其实没发"。这里统一：HTTP 状态 + 业务错误码 双重校验，
并且支持钉钉「加签」安全设置（timestamp + HMAC-SHA256 sign）。

电子邮件走 SMTP 直发（smtplib，标准库）。为什么不用飞牛系统自带的「通知邮箱」：
它的凭据在 SAC 里加密存储、发送接口是 WebUI 内的 CGI（需管理员会话），
推送总线 socket 也只对系统模块开放（root 0600）——第三方应用拿不到；
开放平台（developer.fnnas.com）也没有邮件/通知 API。所以自己连 SMTP，
想用同一个邮箱就把授权码在这里再填一次。
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request

import config as C


def dingtalk_signed_url(webhook, secret):
    """官方加签：sign = base64(hmac_sha256(secret, f"{ts}\\n{secret}"))，URL 追加 timestamp & sign。"""
    webhook = (webhook or "").strip()
    secret = (secret or "").strip()
    if not webhook or not secret:
        return webhook
    ts = str(int(round(time.time() * 1000)))
    string_to_sign = "%s\n%s" % (ts, secret)
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                      hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
    sep = "&" if "?" in webhook else "?"
    return "%s%stimestamp=%s&sign=%s" % (webhook, sep, ts, sign)


def _payload(channel, text):
    if channel == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    if channel == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    if channel == "wechat":
        return {"msgtype": "text", "text": {"content": text}}
    return None


def _post_json(url, payload, timeout=10):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "erm-easynvr-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _check_body(channel, body):
    """返回 (ok, detail)。三个平台的错误码字段不一样，都认。"""
    try:
        j = json.loads(body)
    except Exception:
        return True, "已发送（响应非 JSON，未校验业务码）"
    if not isinstance(j, dict):
        return True, "已发送"
    for key in ("errcode", "code", "StatusCode", "errno"):
        if key in j:
            try:
                val = int(j.get(key))
            except (TypeError, ValueError):
                continue
            msg = j.get("errmsg") or j.get("msg") or j.get("StatusMessage") or ""
            if val == 0:
                return True, "ok"
            return False, "平台返回 %s=%s %s" % (key, val, msg)
    return True, "已发送（未识别的响应结构）"


def push_one(cfg, channel, text, timeout=10):
    if channel == "email":
        return send_email(cfg, text, timeout=max(timeout, 20))
    url = (cfg.get("%s_webhook" % channel) or "").strip()
    if channel == "dingtalk":
        send_url = dingtalk_signed_url(url, cfg.get("dingtalk_secret") or "")
    else:
        send_url = url
    if not send_url:
        return {"channel": channel, "label": C.PUSH_LABEL.get(channel, channel),
                "ok": False, "detail": "未配置 webhook"}
    payload = _payload(channel, text)
    if payload is None:
        return {"channel": channel, "ok": False, "detail": "未知渠道"}
    t0 = time.time()
    try:
        status, body = _post_json(send_url, payload, timeout=timeout)
    except Exception as e:
        return {"channel": channel, "label": C.PUSH_LABEL.get(channel, channel),
                "ok": False, "detail": "请求失败：%s" % e,
                "ms": int((time.time() - t0) * 1000)}
    ms = int((time.time() - t0) * 1000)
    if status // 100 != 2:
        return {"channel": channel, "label": C.PUSH_LABEL.get(channel, channel),
                "ok": False, "detail": "HTTP %s：%s" % (status, body[:200]), "ms": ms}
    ok, detail = _check_body(channel, body)
    return {"channel": channel, "label": C.PUSH_LABEL.get(channel, channel),
            "ok": ok, "detail": detail, "ms": ms}


# ---------------------------------------------------------------- 电子邮件

def _email_result(ok, detail, ms=None):
    r = {"channel": "email", "label": C.PUSH_LABEL.get("email", "电子邮件"), "ok": ok, "detail": detail}
    if ms is not None:
        r["ms"] = ms
    return r


def build_email(cfg, text):
    """把纯文本正文拼成 MIME 邮件。返回 (msg, from_addr, to_addrs) 或抛 ValueError。"""
    from email.message import EmailMessage
    from email.utils import formataddr, formatdate, make_msgid

    host = (cfg.get("email_smtp_host") or "").strip()
    user = (cfg.get("email_smtp_user") or "").strip()
    from_addr = (cfg.get("email_from") or "").strip() or user
    to_addrs = C._norm_mail_list(cfg.get("email_to"))

    if not host:
        raise ValueError("未配置 SMTP 服务器")
    if not C.is_mail_addr(from_addr):
        raise ValueError("发件人地址无效：%s" % (from_addr or "（空）"))
    if not to_addrs:
        raise ValueError("未配置收件人（或收件人地址无效）")

    subject = "%s %s" % (cfg.get("email_subject_prefix") or "", first_line(text))
    msg = EmailMessage()
    msg["Subject"] = subject.strip()
    msg["From"] = formataddr(("EasyNVR 录像监控", from_addr))
    msg["To"] = ", ".join(to_addrs)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(from_addr.split("@")[-1] or None))
    msg.set_content(text)
    return msg, from_addr, to_addrs


def first_line(text):
    """主题里塞正文首行（告警标题），去掉告警框字符，太长就截断。"""
    for line in str(text or "").splitlines():
        s = line.strip().strip("│┃|【】[] ").strip()
        if s and not set(s) <= set("─━=—-_ "):
            return s[:80]
    return "通知"


def send_email(cfg, text, timeout=20):
    """SMTP 直发。SSL(465) / STARTTLS(587) / 明文(25) 三种都支持。

    校验风格与 webhook 渠道一致：连接/鉴权/被拒收人/其它异常分别给出可读原因，
    绝不"以为发了、其实没发"。
    """
    import smtplib
    import socket
    import ssl

    t0 = time.time()
    try:
        msg, from_addr, to_addrs = build_email(cfg, text)
    except ValueError as e:
        return _email_result(False, str(e))

    host = cfg.get("email_smtp_host")
    port = C.as_int(cfg.get("email_smtp_port"), 465, 1, 65535)
    sec = cfg.get("email_smtp_security") or "ssl"
    user = (cfg.get("email_smtp_user") or "").strip()
    password = cfg.get("email_smtp_pass") or ""

    ctx = ssl.create_default_context()
    try:
        if sec == "ssl":
            srv = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        else:
            srv = smtplib.SMTP(host, port, timeout=timeout)
            srv.ehlo()
            if sec == "starttls":
                if not srv.has_extn("starttls"):
                    srv.quit()
                    return _email_result(False, "服务器不支持 STARTTLS（%s:%s），"
                                                "可把加密方式改为 SSL" % (host, port))
                srv.starttls(context=ctx)
                srv.ehlo()
    except smtplib.SMTPAuthenticationError:
        return _email_result(False, "SMTP 鉴权被拒（账号或授权码错误）")
    except (socket.timeout, TimeoutError):
        return _email_result(False, "连接超时（%s:%s，检查端口/网络/防火墙）" % (host, port))
    except ssl.SSLError as e:
        return _email_result(False, "TLS 握手失败：%s（可尝试改用 STARTTLS 或其它端口）" % e)
    except (smtplib.SMTPException, OSError) as e:
        return _email_result(False, "连接 %s:%s 失败：%s" % (host, port, e))

    try:
        if user:
            try:
                srv.login(user, password)
            except smtplib.SMTPAuthenticationError as e:
                return _email_result(False, "SMTP 鉴权被拒（用户名/授权码错误）：%s"
                                     % _smtp_msg(e))
            except smtplib.SMTPException as e:
                return _email_result(False, "SMTP 登录失败：%s" % _smtp_msg(e))
        refused = srv.send_message(msg, from_addr=from_addr, to_addrs=to_addrs)
        try:
            srv.quit()
        except Exception:
            pass
    except smtplib.SMTPRecipientsRefused as e:
        return _email_result(False, "收件人被全部拒收：%s" % _refused_detail(e.recipients))
    except smtplib.SMTPSenderRefused as e:
        return _email_result(False, "发件人被拒收（%s）：%s" % (from_addr, _smtp_msg(e)))
    except smtplib.SMTPDataError as e:
        return _email_result(False, "邮件正文被拒（可能被判为垃圾邮件）：%s" % _smtp_msg(e))
    except (smtplib.SMTPException, OSError) as e:
        return _email_result(False, "发送失败：%s" % e)

    ms = int((time.time() - t0) * 1000)
    if refused:
        detail = "部分收件人被拒：%s（其余 %d 个已送达）" % (
            _refused_detail(refused), len(to_addrs) - len(refused))
        return _email_result(False, detail, ms)
    return _email_result(True, "已发送 → %s" % ", ".join(to_addrs), ms)


def _smtp_msg(e):
    """smtplib 异常 → '550 xxx' 这类可读串。"""
    code = getattr(e, "smtp_code", "")
    err = getattr(e, "smtp_error", "")
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    txt = ("%s %s" % (code, err)).strip() or str(e)
    return txt[:200]


def _refused_detail(recipients):
    if not isinstance(recipients, dict):
        return str(recipients)[:200]
    parts = []
    for addr, (code, err) in list(recipients.items())[:5]:
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        parts.append("%s → %s %s" % (addr, code, str(err)[:80]))
    return "；".join(parts)


def push_all(cfg, text, channels=None):
    chans = channels if channels is not None else (cfg.get("enabled_push_channels") or [])
    if not chans:
        return [{"channel": "-", "label": "-", "ok": False, "detail": "未启用任何推送渠道"}]
    out = []
    for ch in chans:
        r = push_one(cfg, ch, text)
        out.append(r)
        try:
            import logging
            lg = logging.getLogger("erm")
            if r.get("ok"):
                lg.info("已推送 %s（%sms）", ch, r.get("ms"))
            else:
                lg.error("推送 %s 失败：%s", ch, r.get("detail"))
        except Exception:
            pass
    return out


def test_text(cfg):
    return ("【EasyNVR 录像监控 · 测试消息】\n"
            "如果你看到这条消息，说明推送渠道已打通。\n"
            "录像目录：%s\n"
            "告警阈值：%s 分钟\n"
            "发送时间：%s"
            % (cfg.get("record_root") or "（未配置）",
               cfg.get("alert_threshold_minutes"),
               time.strftime("%Y-%m-%d %H:%M:%S")))
