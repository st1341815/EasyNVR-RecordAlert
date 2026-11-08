#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EasyNVR 源端服务巡检（v1.2.0 · P0）。

为什么单独做这件事
------------------
本工具判「有没有录像」，证据是磁盘上录像文件的 mtime。可是「没有新文件」至少有四种原因：

  1. 摄像头离线        —— 画面没了（业务异常，逐通道判定）
  2. 通道没启用录像    —— 本来就不该有文件（配置问题，逐通道判定）
  3. 画面静止未触发    —— motion 模式的常态（业务正常）
  4. **EasyNVR 自己挂了 / 授权过期** —— 上游瞎了，所有通道一起静默

前三种是「被监控对象的事」，第四种是「监控工具的上游没了」。第四种以前只能靠人发现：
2 路通道同时静止，看起来和「家里没人」一模一样，而且这时候恰恰最需要有人告诉你。

`GET /health` 是这个能力的入口，**不需要任何凭据**（连 app_id/app_key 都不用）：

    {"status":"ok","version":"v7.7.421","start_at":"2026-11-08T11:27:20+08:00",
     "git_hash":"4a4147d1-261010","company":"hi",
     "build_at":"2026-10-10 19:03:18","deadline":"2026-11-10 19:03:18"}

`deadline` 是授权到期时间。**过期之后 /health 依然返回 200、status 依然是 ok**，
录像和拉流却已经停了 —— 只盯 status 会漏掉最要命的那一次，所以 deadline 必须单独判。
"""

import contextlib
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime

import config as C
import monitor as M
from notify import failed_channels, push_all

logger = logging.getLogger("erm")

# 状态等级。顺序有意义：同一次探测只会落到其中一个，等级越高越严重。
LEVEL_OK = "ok"
LEVEL_EXPIRING = "expiring"      # 授权即将到期（仍在正常录像）
LEVEL_EXPIRED = "expired"        # 授权已过期
LEVEL_BAD_STATUS = "bad_status"  # 服务在，但 status 不是 ok
LEVEL_UNREACHABLE = "unreachable"  # 连不上

LEVEL_LABEL = {
    LEVEL_OK: "正常",
    LEVEL_EXPIRING: "授权临期",
    LEVEL_EXPIRED: "授权已过期",
    LEVEL_BAD_STATUS: "服务异常",
    LEVEL_UNREACHABLE: "服务不可达",
}
# 这些状态需要推送（ok 不需要；恢复通知另算）
LEVEL_ALERT = (LEVEL_EXPIRING, LEVEL_EXPIRED, LEVEL_BAD_STATUS, LEVEL_UNREACHABLE)

PROBE_TIMEOUT = 5.0
MAX_BODY = 64 * 1024
USER_AGENT = "erm-health/1.0"


# ---------------------------------------------------------------- 探测

def health_url(cfg):
    """拼 /health 地址。地址留空返回空串（调用方据此跳过）。"""
    base = str((cfg or {}).get("nvr_base_url") or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.lower().startswith(("http://", "https://")):
        base = "http://" + base
    return base + "/health"


def _short_err(exc):
    """把异常压成一行人话。urllib 的错误类型穷举不完，取最后一段 + 常见翻译。"""
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP %s %s" % (exc.code, exc.reason or "")
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        text = str(reason)
        low = text.lower()
        if "refused" in low:
            return "连接被拒绝（服务没起 / 端口不对）"
        if "timed out" in low or "timeout" in low:
            return "连接超时（网络不通 / 主机不可达）"
        if "name or service not known" in low or "nodename" in low:
            return "域名解析失败"
        return text
    text = str(exc) or exc.__class__.__name__
    return text.splitlines()[0][:200]


def probe(cfg, timeout=PROBE_TIMEOUT):
    """请求一次 /health。

    返回 {"ok": bool, "url", "data": dict, "error": str, "elapsed_ms": int}
    注意 ok 只代表「拿到了一份能解析的 /health 响应」，服务是否健康由 judge() 判。
    """
    url = health_url(cfg)
    if not url:
        return {"ok": False, "url": "", "error": "未填写 EasyNVR 地址", "elapsed_ms": 0}
    started = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BODY)
    except Exception as exc:                       # noqa: BLE001 —— 网络层什么都可能抛
        return {"ok": False, "url": url, "error": _short_err(exc),
                "elapsed_ms": int((time.time() - started) * 1000)}
    elapsed = int((time.time() - started) * 1000)
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        return {"ok": False, "url": url, "elapsed_ms": elapsed,
                "error": "/health 返回的不是 JSON（可能填成了网页地址）"}
    if not isinstance(data, dict):
        return {"ok": False, "url": url, "elapsed_ms": elapsed,
                "error": "/health 返回结构异常"}
    return {"ok": True, "url": url, "data": data, "error": "", "elapsed_ms": elapsed}


# ---------------------------------------------------------------- 判定

def parse_deadline(cfg, text):
    """把 deadline（本地时间字符串，无时区）转成时间戳。解析不了返回 0。

    EasyNVR 输出形如 '2026-11-10 19:03:18'，也可能带 T 或毫秒，这里都容错。
    """
    s = str(text or "").strip()
    if not s:
        return 0
    s = s.replace("T", " ").split(".")[0].split("+")[0].strip()
    tz = M.get_tz(cfg)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=tz).timestamp())
        except ValueError:
            continue
    return 0


def remain_text(days):
    """剩余天数的中文文案。不足一天单独说，别显示成「剩余 0 天」。"""
    if days is None:
        return ""
    if days >= 1:
        return "剩余 %d 天" % int(days)
    if days > 0:
        return "剩余不到 1 天"
    return "已过期 %d 天" % int(-days)


def judge(cfg, pr, now=None):
    """根据探测结果给出等级、剩余天数与一句话摘要。"""
    now = float(now if now is not None else time.time())
    warn_days = int(cfg.get("nvr_expire_warn_days") or 0)

    if not pr.get("ok"):
        return {"level": LEVEL_UNREACHABLE, "alert": True, "deadline": 0,
                "remain_days": None, "version": "",
                "reason": pr.get("error") or "探测失败", "now": now,
                "url": pr.get("url") or ""}

    data = pr.get("data") or {}
    version = str(data.get("version") or "")
    status = str(data.get("status") or "").strip().lower()
    dl_ts = parse_deadline(cfg, data.get("deadline"))
    remain_days = ((dl_ts - now) / 86400.0) if dl_ts else None

    base = {"deadline": dl_ts, "remain_days": remain_days, "version": version,
            "url": pr.get("url") or "", "now": now, "status_text": status}

    if status and status != "ok":
        return dict(base, level=LEVEL_BAD_STATUS, alert=True,
                    reason="服务自报状态 %s" % status)
    if dl_ts and now >= dl_ts:
        return dict(base, level=LEVEL_EXPIRED, alert=True,
                    reason="授权已于 %s 到期" % _fmt(cfg, dl_ts))
    if remain_days is not None and warn_days > 0 and remain_days <= warn_days:
        return dict(base, level=LEVEL_EXPIRING, alert=True,
                    reason="授权%s（%s 到期）" % (remain_text(remain_days), _fmt(cfg, dl_ts)))
    return dict(base, level=LEVEL_OK, alert=False, reason="服务正常")


def _fmt(cfg, ts):
    return M.fmt_ts(cfg, ts) if ts else "—"


# ---------------------------------------------------------------- 文案

def _header(cfg, level, reason):
    # LEVEL_LABEL 里已经含「授权」「服务」这类词，直接拼在 EasyNVR 后面即可 ——
    # 模板写成「服务%s」会和「服务不可达」叠成「服务服务不可达」。
    return "【EasyNVR %s】\n%s" % (LEVEL_LABEL.get(level, level), reason)


def build_alert(cfg, jd, pr):
    """告警正文。第一行是结论，第二段说清「这件事对本工具意味着什么」。"""
    level = jd["level"]
    lines = [_header(cfg, level, jd.get("reason") or "")]
    if jd.get("url"):
        lines.append("地址：%s" % jd["url"])
    if jd.get("version"):
        lines.append("版本：%s" % jd["version"])
    if level == LEVEL_UNREACHABLE:
        lines.append("影响：通道扫描仍在跑，但此时所有通道都会被判成「无新录像」——"
                     "看到的静默很可能是它挂了，不是摄像头的错。")
    elif level == LEVEL_EXPIRED:
        lines.append("影响：录像与拉流已经停止，磁盘上不会再有新文件。"
                     "续费/重新激活后才能恢复。")
    elif level == LEVEL_EXPIRING:
        lines.append("影响：到期后录像与拉流会停，本工具会看到所有通道同时静默。"
                     "建议提前续费，别等出事。")
    elif level == LEVEL_BAD_STATUS:
        lines.append("影响：服务进程在，但自报状态不正常，录像链路可能已经中断。")
    lines.append("检测时间：%s" % M.fmt_ts(cfg, jd.get("now") or time.time()))
    return "\n".join(lines)


def build_recovery(cfg, jd, pr):
    parts = ["【EasyNVR 服务已恢复】",
             "当前状态：%s" % LEVEL_LABEL.get(jd["level"], jd["level"])]
    if jd.get("version"):
        parts.append("版本：%s" % jd["version"])
    if jd.get("deadline"):
        parts.append("授权：%s（%s）" % (remain_text(jd.get("remain_days")),
                                       _fmt(cfg, jd["deadline"])))
    parts.append("检测时间：%s" % M.fmt_ts(cfg, jd.get("now") or time.time()))
    return "\n".join(parts)


# ---------------------------------------------------------------- 状态机 + 推送

def _today(cfg, now):
    return datetime.fromtimestamp(float(now), M.get_tz(cfg)).strftime("%Y-%m-%d")


def check(cfg, state, push=True, lock=None):
    """探测一次并更新 state；按需推送。

    去重规则（与通道告警同源，但有一条自己的补充）：
      · 等级变化 → 立即推；
      · 同等级持续 → 按「重复提醒间隔」重推，0 表示只提醒一次，等恢复后重置；
      · **授权临期（expiring）额外按天重推**：这类事情拖一天就少一天，
        「重复提醒间隔 0」不该让它在到期前彻底闭嘴。
      · 推送计划时段**不拦**：上游瞎了属于「监控本身失效」，和目录整体不可达同级，
        等到第二天早上的代价比被吵醒大。

    手动巡检（push=False）只探测、不推送 —— 人就在页面上看着，不该顺手发一条。
    """
    import contextlib

    if not cfg.get("nvr_health_enabled"):
        return {"ok": True, "skipped": "disabled", "probe": None, "judge": None}

    pr = probe(cfg)
    jd = judge(cfg, pr)
    now = int(time.time())

    with (lock or contextlib.nullcontext()):
        hs = state.get("nvr_health")
        if not isinstance(hs, dict):
            hs = {}
        prev_level = hs.get("level")
        was_alerting = bool(hs.get("alerting"))
        silence_s = int(cfg.get("alert_silence_minutes") or 0) * 60
        last_alert = int(hs.get("last_alert_time") or 0)
        last_day = str(hs.get("last_alert_day") or "")

        need_push, kind = False, ""
        if jd["alert"]:
            if not was_alerting or prev_level != jd["level"]:
                need_push, kind = True, "alert"
            elif jd["level"] == LEVEL_EXPIRING:
                # 临期不走「重复提醒间隔」：那个间隔是为了压住刷屏，而这件事
                # 一天提醒一次才合适 —— 拖一天就少一天，静默到过期是最坏结果。
                if last_day != _today(cfg, now):
                    need_push, kind = True, "alert"
            elif silence_s > 0 and (now - last_alert) >= silence_s:
                need_push, kind = True, "alert"
        elif was_alerting and cfg.get("notify_recovery", True):
            need_push, kind = True, "recovery"

        text = ""
        if need_push and kind == "alert":
            text = build_alert(cfg, jd, pr)
        elif need_push and kind == "recovery":
            text = build_recovery(cfg, jd, pr)

        pushed = []
        if need_push and push:
            # 与通道告警同一套语义（见 monitor.rollback_failed_alerts）：先记「已通知」，
            # 若配了渠道但一条都没送出去，再回滚时间戳让下一轮重试。
            # 「一个渠道都没配」不算失败 —— 那时候推进时间戳是对的，否则每轮都会
            # 重新判定「该推送」，日志被同一句异常刷屏。
            prev_alert, prev_day = hs.get("last_alert_time") or 0, hs.get("last_alert_day") or ""
            hs["last_alert_time"] = now
            hs["last_alert_day"] = _today(cfg, now)
            pushed = push_all(cfg, text)
            # 用 failed_channels 而不是「有没有 ok」：没配渠道时 push_all 会返回
            # channel="-" 的失败项，那不是可重试失败，否则每轮都判定「欠一条」重推。
            if failed_channels(pushed):
                hs["last_alert_time"], hs["last_alert_day"] = prev_alert, prev_day

        hs.update({
            "level": jd["level"],
            "label": LEVEL_LABEL.get(jd["level"], jd["level"]),
            "reason": jd.get("reason") or "",
            "version": jd.get("version") or "",
            "deadline": jd.get("deadline") or 0,
            "deadline_str": _fmt(cfg, jd.get("deadline") or 0),
            "remain_days": (None if jd.get("remain_days") is None
                            else round(float(jd["remain_days"]), 2)),
            "remain_text": remain_text(jd.get("remain_days")),
            "url": jd.get("url") or "",
            "elapsed_ms": pr.get("elapsed_ms") or 0,
            "checked_at": now,
        })
        # 手动巡检（push=False）只更新「看到的观察值」，一个字节都不动告警状态机：
        # 顺手把 alerting 置 True 的话，后台下一轮就以为「已经在告警」，
        # 只按静默间隔重推 —— 等于把人手动点出来的这次发现吞掉。
        if push:
            hs["alerting"] = bool(jd["alert"])
        if need_push and push and not pushed:
            hs["last_alert_error"] = "未启用任何推送渠道，仅记录"
        else:
            hs.pop("last_alert_error", None)
        state["nvr_health"] = hs
        C.save_state(state)

    if need_push and push:
        if kind == "alert":
            logger.warning("EasyNVR %s：%s", LEVEL_LABEL.get(jd["level"]), jd.get("reason"))
        else:
            logger.info("EasyNVR 服务已恢复")
    elif not jd["alert"]:
        logger.debug("EasyNVR /health 正常（%s）", jd.get("url"))
    elif prev_level != jd["level"]:
        # 只在状态变化时记一条：每 5 分钟刷同一句「还是不可达」，日志会变成噪音，
        # 真要看趋势应该看 state 里的 checked_at，不是翻日志。
        logger.warning("EasyNVR %s：%s（%s）",
                       LEVEL_LABEL.get(jd["level"]), jd.get("reason"),
                       "手动巡检，不推送" if not push else "按静默策略暂不重复推送")

    return {"ok": True, "probe": pr, "judge": jd, "event": kind,
            "text": text, "pushed": pushed}


def summary(state):
    """给前端的一小份摘要（不含大字段）。"""
    hs = (state or {}).get("nvr_health") or {}
    if not isinstance(hs, dict) or not hs:
        return {"level": "", "label": "未巡检", "alerting": False, "checked_at": 0}
    return {
        "level": hs.get("level") or "",
        "label": hs.get("label") or LEVEL_LABEL.get(hs.get("level"), "—"),
        "alerting": bool(hs.get("alerting")),
        "reason": hs.get("reason") or "",
        "version": hs.get("version") or "",
        "deadline_ts": hs.get("deadline") or 0,
        "deadline_str": hs.get("deadline_str") or "",
        "remain_text": hs.get("remain_text") or "",
        "url": hs.get("url") or "",
        "elapsed_ms": hs.get("elapsed_ms") or 0,
        "checked_at": hs.get("checked_at") or 0,
        "checked_str": hs.get("checked_str") or "",
    }
