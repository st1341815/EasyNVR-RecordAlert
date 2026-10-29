#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送（钉钉 / 飞书 / 企业微信）—— 纯标准库 urllib，无 requests 依赖。

对应文档评审 B6：原文档 `requests.post(...)` 不看状态码、不解析响应体就记「已推送」，
钉钉/企微在 HTTP 200 下也可能返回 errcode≠0（关键词不匹配 310000、限流 130101），
于是"以为发了、其实没发"。这里统一：HTTP 状态 + 业务错误码 双重校验，
并且支持钉钉「加签」安全设置（timestamp + HMAC-SHA256 sign）。
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


def push_all(cfg, text, channels=None):
    """按配置推送到所有启用渠道，返回逐条结果（调用方据此写日志/展示）。"""
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
