#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EasyNVR 录像监控 —— 扫描 / 判定 / 推送核心（纯标准库，无第三方依赖）。

判定口径（对应文档评审 B4 / B5）：
  · 闲置时长 = now − 「当前最新录像文件的 mtime」，**不**拿历史 state 值当基准。
    用历史值会在 EasyNVR 清理/轮转旧文件后误报，目录被清空后漏报且状态卡死。
  · latest == 0 分两种：目录不存在/不可读（配置或授权问题）与 目录在但没录像（录制停了），
    两种都要告警 —— 原文档直接 continue，新装通道 / 目录名填错永远不会被告警。
  · 日报用「到点即发 + 当天只发一次（日期落 state）」，不用「恰好命中某一分钟」。
"""

import contextlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config as C
from notify import push_all

logger = logging.getLogger("erm")

STATUS_LABEL = {
    "ok": "正常",
    "silent": "无新录像",
    "no_record": "无录像文件",
    "missing": "目录不存在",
    "unreadable": "无读取权限",
}


# ---------------------------------------------------------------- 日志

def setup_logging(cfg=None):
    """日志落 $TRIM_PKGVAR/logs/monitor.log，5MB × 3 轮转（原文档是 nohup 永久追加）。"""
    if logger.handlers:
        return logger
    C.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = RotatingFileHandler(str(C.LOG_DIR / "monitor.log"),
                             maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.setLevel(logging.INFO)
    return logger


# ---------------------------------------------------------------- 时区 / 时间

def get_tz(cfg):
    name = (cfg or {}).get("timezone") or "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return datetime.now().astimezone().tzinfo


def now_dt(cfg):
    return datetime.now(get_tz(cfg))


def fmt_ts(cfg, ts):
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), get_tz(cfg)).strftime("%Y-%m-%d %H:%M:%S")


def yesterday_bounds(cfg, now=None):
    """昨天 00:00 ~ 今天 00:00 的时间戳（按配置时区）。"""
    now = now or now_dt(cfg)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=1)
    return int(start.timestamp()), int(today.timestamp())


# ---------------------------------------------------------------- 通道解析
#
# EasyNVR 的真实目录是三层：
#   <录像根目录>/<摄像头随机ID>/profile_<n>/<YYYYMMDD>/20261029000315-62000.mp4
# 一个摄像头可能有多个 profile（主码流 / 子码流…），是否都录制取决于 EasyNVR 配置。
# 因此「通道」有两种口径，用 channel_layout 选：
#   camera  = 一个摄像头一个通道（聚合它所有 profile；默认，子码流不录也不会误报）
#   profile = 每个 profile 一个通道（能发现"主码流挂了但子码流还在"）

PROFILE_RE = re.compile(r"^profile[_-]?(\d+)$", re.I)
# EasyNVR 除了 20261029 还有 20261029_event（事件抓拍，里面是 jpg）
DATE_RE = re.compile(r"^(\d{8})(?:[_-].*)?$")

# 日期目录最多往回看多少天：够覆盖最长的录像保留期，又不至于每次扫描翻遍整棵树
LOOKBACK_DIRS = 120

# 在线时长（连续录像）口径：
#   从最新录像文件往回，相邻文件 mtime 间隔都 <= ONLINE_GAP_SECONDS 视为连续在线；
#   EasyNVR 正常约 1~2 分钟一个分段，5 分钟足够包容偶发的写入延迟。
#   一路扫到最早录像（视频最多保留约 180 天，不会跨年）。
ONLINE_GAP_SECONDS = 300


def _subdirs(path):
    out = []
    try:
        with os.scandir(str(path)) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False) and not e.name.startswith("."):
                        out.append(Path(e.path))
                except OSError:
                    continue
    except (OSError, PermissionError):
        pass
    return out


def _label(cfg, key):
    """显示名：先按完整 key 找别名，再按摄像头 ID 找，都没有就用目录名本身。"""
    aliases = cfg.get("channel_aliases") or {}
    if key in aliases:
        return aliases[key]
    cam, _, prof = key.partition("/")
    if cam in aliases:
        return "%s（%s）" % (aliases[cam], prof) if prof else aliases[cam]
    return key


def _channel(cfg, root, rel, layout, profiles=None):
    return {"channel": rel, "label": _label(cfg, rel), "path": str(root / rel),
            "layout": layout, "profiles": list(profiles or [])}


def resolve_channels(cfg):
    """返回 [{"channel","label","path","layout","profiles"}]。

    通道一律按 EasyNVR 的真实布局自动发现：
      <录像根目录>/<摄像头随机ID>/profile_<n>/<YYYYMMDD>/<录像文件>
    口径由 channel_layout 决定：
      camera  = 一个摄像头一个通道（聚合它下面所有 profile_N）
      profile = 每个 profile_N 各算一个通道

    没有 profile_N 子目录的一级目录**不当通道**：EasyNVR 的缩略图 / 事件抓拍 /
    日志目录也可能按日期分层，误当成通道会报"无录像"假告警。
    """
    root_s = str(cfg.get("record_root") or "").strip()
    if not root_s:
        return []
    root = Path(root_s)
    layout = "profile" if (cfg.get("channel_layout") or "camera").lower() == "profile" else "camera"

    out = []
    for cam in _subdirs(root):
        if DATE_RE.match(cam.name):               # 直接躺在根下的日期目录，不是摄像头
            continue
        profs = sorted((c.name for c in _subdirs(cam) if PROFILE_RE.match(c.name)),
                       key=lambda n: int(PROFILE_RE.match(n).group(1)))
        if not profs:
            continue
        if layout == "profile":
            for pr in profs:
                out.append(_channel(cfg, root, "%s/%s" % (cam.name, pr), "profile", [pr]))
        else:
            out.append(_channel(cfg, root, cam.name, "camera", profs))
    out.sort(key=lambda c: c["channel"])
    return out


# ---------------------------------------------------------------- 目录扫描

def _scan_flat_dir(d, exts, window=None):
    """单层 scandir：EasyNVR 的日期目录里直接就是 mp4，不必递归。"""
    cnt = latest = inw = 0
    try:
        with os.scandir(str(d)) as it:
            for e in it:
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    if os.path.splitext(e.name)[1].lower() not in exts:
                        continue
                    m = int(e.stat(follow_symlinks=False).st_mtime)
                    cnt += 1
                    if m > latest:
                        latest = m
                    if window and window[0] <= m < window[1]:
                        inw += 1
                except OSError:
                    continue
    except (OSError, PermissionError):
        pass
    return cnt, latest, inw


def _scan_deep(d, exts, window=None):
    """递归扫描：日期目录里还分层（或完全没有日期目录）时兜底。"""
    cnt = latest = inw = 0
    stack = [str(d)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not e.name.startswith("."):
                                stack.append(e.path)
                            continue
                        if not e.is_file(follow_symlinks=False):
                            continue
                        if os.path.splitext(e.name)[1].lower() not in exts:
                            continue
                        m = int(e.stat(follow_symlinks=False).st_mtime)
                        cnt += 1
                        if m > latest:
                            latest = m
                        if window and window[0] <= m < window[1]:
                            inw += 1
                    except OSError:
                        continue
        except (OSError, PermissionError):
            continue
    return cnt, latest, inw


def scan_channel(path, exts, window=None):
    """扫描一个通道目录（可以是摄像头目录，也可以是 profile 目录）。

    有 <YYYYMMDD> 日期子目录时按天打开（最多看最近 LOOKBACK_DIRS 天），
    不再对整棵历史目录树做递归 stat —— 一天几千个 mp4，全量扫会把每轮监控拖成秒级。
    """
    path = Path(path)
    subs = _subdirs(path)
    dated = [d for d in subs if DATE_RE.match(d.name)]
    other = [d for d in subs if not DATE_RE.match(d.name)]

    latest = total = inw = 0
    if dated:
        dated.sort(key=lambda d: d.name, reverse=True)
        for d in dated[:LOOKBACK_DIRS]:
            c, l, w = _scan_flat_dir(d, exts, window)
            total += c
            if l > latest:
                latest = l
            inw += w
    for d in other:
        c, l, w = _scan_deep(d, exts, window)
        total += c
        if l > latest:
            latest = l
        inw += w
    if not dated and not other:              # 文件直接放在通道目录里
        c, latest, inw = _scan_flat_dir(path, exts, window)
        total = c
    return {"latest": latest, "total": total, "in_window": inw}


# ---------------------------------------------------------------- 在线时长

def _flat_mtimes(d, exts):
    """单层目录里所有录像文件的 mtime 列表。"""
    out = []
    try:
        with os.scandir(str(d)) as it:
            for e in it:
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    if os.path.splitext(e.name)[1].lower() not in exts:
                        continue
                    out.append(int(e.stat(follow_symlinks=False).st_mtime))
                except OSError:
                    continue
    except (OSError, PermissionError):
        pass
    return out


def _days_of_mtimes(path, exts):
    """把通道目录整理成「按日期从新到老的 mtime 分组」：list[list[int]]。

    profile 布局：path 是 profile 目录，下一层就是日期目录，每个日期一组；
    camera 布局：path 是摄像头目录，下一层是 profile_N，要把各 profile 里
    同一天的录像合并成一组（否则跨 profile 的同日录像会被拆成两段误判断流）。
    每个内层 list 已按 mtime 从新到老排好。
    """
    path = Path(path)
    subs = _subdirs(path)
    dated = sorted((d for d in subs if DATE_RE.match(d.name)),
                   key=lambda d: d.name, reverse=True)
    if dated:
        out = []
        for d in dated:
            mts = _flat_mtimes(d, exts)
            mts.sort(reverse=True)
            out.append(mts)
        return out
    # camera 布局：下一层是 profile_N，按日期把各 profile 合并
    days = {}
    for prof in subs:
        if not PROFILE_RE.match(prof.name):
            continue
        for d in _subdirs(prof):
            if not DATE_RE.match(d.name):
                continue
            days.setdefault(d.name, []).extend(_flat_mtimes(d, exts))
    out = []
    for day in sorted(days, reverse=True):
        mts = days[day]
        mts.sort(reverse=True)
        out.append(mts)
    return out


def online_seconds(path, exts, latest):
    """连续在线时长（秒）。

    从最新录像往回，跨日期目录逐个比对相邻文件 mtime，直到发现第一个
    「间隔 > ONLINE_GAP_SECONDS」的断点为止；一路扫到最早录像都没断点，
    就按最早录像起算 —— 视频最多保留约 180 天，不会跨年。
    """
    if not latest:
        return 0
    anchor = int(latest)
    cur = anchor
    for day in _days_of_mtimes(path, exts):
        for m in day:
            if m > cur:                         # 时钟回拨/乱序的「未来」文件，忽略
                continue
            if cur - m <= ONLINE_GAP_SECONDS:
                cur = m
            else:
                return max(0, anchor - cur)
    return max(0, anchor - cur)


def fmt_duration(seconds):
    """在线时长的展示格式（最长约 180 天，无年份档）。

      · <1 分钟          → 「不足 1 分钟」
      · <100 小时        → 「N 分钟 / H 小时 M 分」
      · 100 小时~100 天   → 「D 天 H 时」
      · ≥100 天          → 「M 月 D 天 H 时」（1 月按 30 天）
    """
    seconds = int(seconds)
    if seconds < 60:
        return "不足 1 分钟"
    m = seconds // 60
    if m < 60:
        return "%d 分钟" % m
    h = m // 60
    if h < 100:
        return ("%d 小时 %d 分" % (h, m % 60)) if m % 60 else "%d 小时" % h
    d = h // 24
    if d < 100:
        return ("%d 天 %d 时" % (d, h % 24)) if h % 24 else "%d 天" % d
    mo, rd, rh = d // 30, d % 30, h % 24
    parts = ["%d 月" % mo]
    if rd:
        parts.append("%d 天" % rd)
    if rh:
        parts.append("%d 时" % rh)
    return " ".join(parts)


def online_text(path, exts, latest):
    """通道正在录像时的在线时长文本；异常/无录像时调用方会显示 —。"""
    return fmt_duration(online_seconds(path, exts, latest))


def evaluate(cfg, now=None, with_counts=True, window=None):
    """逐个通道判定，页面与告警逻辑共用。"""
    now_dt_ = now or now_dt(cfg)
    now_ts = int(now_dt_.timestamp())
    exts = set(cfg.get("video_exts") or C.DEFAULT_CONFIG["video_exts"])
    threshold_s = cfg["alert_threshold_minutes"] * 60
    root = cfg.get("record_root") or ""

    result = {"root": root, "checked_at": now_ts, "channels": [], "error": "",
              "layout": (cfg.get("channel_layout") or "camera")}
    if not root:
        result["error"] = "未配置录像目录"
        return result
    root_p = Path(root)
    if not root_p.is_dir():
        result["error"] = "录像目录不存在：%s" % root
        return result
    if not os.access(root, os.R_OK | os.X_OK):
        result["error"] = ("录像目录无读取权限：%s（请到「应用中心 → 本应用 → 设置 → 授权目录」"
                           "添加该目录并设为可读，然后重启应用）" % root)
        return result

    chans = resolve_channels(cfg)
    if not chans:
        result["error"] = ("录像目录下没有识别到任何通道。EasyNVR 的录像应为 "
                           "<摄像头ID>/profile_N/<日期>/，请确认「录像目录」指的正是 "
                           "easynvr_rec 这一层（不是它的上级或某个摄像头目录）")
        return result

    result["layout"] = chans[0].get("layout") or "camera"
    for ch in chans:
        path = Path(ch["path"])
        item = {"channel": ch["channel"], "label": ch["label"], "path": ch["path"],
                "layout": ch.get("layout"), "profiles": ch.get("profiles") or [],
                "status": "ok", "latest": 0, "latest_str": "—", "idle_min": None,
                "online_str": "—", "files": 0, "files_window": 0}
        if not path.exists() or not path.is_dir():
            item["status"] = "missing"
        elif not os.access(str(path), os.R_OK | os.X_OK):
            item["status"] = "unreadable"
        else:
            sc = scan_channel(path, exts, window if with_counts else None)
            item["latest"] = sc["latest"]
            item["files"] = sc["total"]
            item["files_window"] = sc["in_window"]
            if sc["latest"] == 0:
                item["status"] = "no_record"
            else:
                item["idle_min"] = max(0, (now_ts - sc["latest"]) // 60)
                item["status"] = "silent" if (now_ts - sc["latest"]) >= threshold_s else "ok"
                item["latest_str"] = fmt_ts(cfg, sc["latest"])
                if item["status"] == "ok":      # 正在录像才算在线；断流/无录像显示 —
                    item["online_str"] = online_text(path, exts, sc["latest"]) or "—"
        item["label"] = ch["label"]
        item["tag"] = STATUS_LABEL.get(item["status"], item["status"])
        item["alert"] = item["status"] != "ok"
        result["channels"].append(item)
    result["alerting"] = sum(1 for c in result["channels"] if c["alert"])
    return result

# ---------------------------------------------------------------- 消息文案

def channel_title(ch):
    """告警里的通道名：有别名就「大门（O27fqV6dYvs3T）」，没别名就用目录名。"""
    label = ch.get("label") or ch["channel"]
    if label and label != ch["channel"]:
        return "%s（%s）" % (label, ch["channel"])
    return ch["channel"]


def _alert_reason(cfg, ch):
    st = ch["status"]
    if st == "missing":
        return "通道目录不存在，请检查 EasyNVR 是否还在录制该摄像头、「授权目录」是否覆盖它"
    if st == "unreadable":
        return "没有读取权限，请到「应用中心 → 本应用 → 设置 → 授权目录」授权该目录后重启应用"
    if st == "no_record":
        return "近 %d 天内没有任何录像文件（从未录制，或录像已被清空）" % LOOKBACK_DIRS
    if st == "silent":
        return "已 %d 分钟没有新增录像（阈值 %d 分钟）" % (
            ch["idle_min"] or 0, cfg["alert_threshold_minutes"])
    return "状态异常"


def build_alert_message(cfg, ch):
    extra = ""
    if ch.get("profiles"):
        extra = "profile：%s\n" % "、".join(ch["profiles"])
    return (
        "【EasyNVR 录像异常】\n"
        "通道：%s\n"
        "%s"
        "问题：%s\n"
        "最后录像：%s\n"
        "检测时间：%s\n"
        "请检查 EasyNVR 服务、摄像头与录像目录。"
        % (channel_title(ch), extra, _alert_reason(cfg, ch),
           ch["latest_str"], fmt_ts(cfg, time.time()))
    )


def build_recovery_message(cfg, ch):
    return (
        "【EasyNVR 录像恢复】\n"
        "通道：%s\n"
        "已恢复：检测到新的录像文件\n"
        "最新录像：%s\n"
        "检测时间：%s"
        % (channel_title(ch), ch["latest_str"], fmt_ts(cfg, time.time()))
    )


def build_daily_report(cfg, now=None):
    now = now or now_dt(cfg)
    start, end = yesterday_bounds(cfg, now)
    ev = evaluate(cfg, now=now, window=(start, end))
    day = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if ev.get("error") and not ev["channels"]:
        return "【EasyNVR 录像日报 %s】\n统计失败：%s" % (day, ev["error"])
    lines, total, bad = [], 0, []
    for ch in ev["channels"]:
        lines.append("  %s：%d 个文件" % (channel_title(ch), ch["files_window"]))
        total += ch["files_window"]
        if ch["alert"]:
            bad.append("%s %s" % (channel_title(ch), ch["tag"]))
    body = [
        "【EasyNVR 录像日报 %s】" % day,
        "统计区间：%s 00:00 — 24:00" % day,
        "",
        *lines,
        "",
        "合计：%d 个录像文件（%d 个通道）" % (total, len(ev["channels"])),
    ]
    if bad:
        body.append("异常通道：" + "、".join(bad))
    return "\n".join(body)


# ---------------------------------------------------------------- 调度判定

def next_report_dt(cfg, now=None):
    now = now or now_dt(cfg)
    hh, mm = [int(x) for x in cfg["daily_report_time"].split(":")]
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def report_due(cfg, state, now=None):
    """到点即发：当前时间 >= 今天的目标时间，且今天还没发过。

    这样进程重启、NAS 睡眠错过 08:00 也能补发当天一次，而不是漏掉一整天；
    last_report_day 落在 state.json，重启不会重复发。
    """
    now = now or now_dt(cfg)
    today = now.strftime("%Y-%m-%d")
    if state.get("last_report_day") == today:
        return False
    hh, mm = [int(x) for x in cfg["daily_report_time"].split(":")]
    return (now.hour, now.minute) >= (hh, mm)


# ---------------------------------------------------------------- 执行

def run_scan(cfg, state, force_alert=False):
    """一轮扫描：判定 → 告警（含静默）→ 恢复通知 → 落 state。"""
    now_ts = int(time.time())
    ev = evaluate(cfg, with_counts=False)
    state.setdefault("channels", {})
    events = []

    if ev.get("error") and not ev["channels"]:
        # 全局错误（目录没配/不可读/没有通道）：也要告警，否则用户以为在监控
        key = "__global__"
        gst = state["channels"].setdefault(key, {})
        silence_s = cfg["alert_silence_minutes"] * 60
        due = force_alert or (not gst.get("alerting")) or (
            silence_s == 0) or (now_ts - int(gst.get("last_alert_time", 0)) >= silence_s)
        if due:
            msg = "【EasyNVR 录像监控异常】\n%s\n检测时间：%s" % (ev["error"], fmt_ts(cfg, now_ts))
            events.append({"scope": key, "kind": "alert", "message": msg})
            gst["last_alert_time"] = now_ts
            gst["alerting"] = True
            state["channels"][key] = gst
        state["last_check"] = now_ts
        state["last_result"] = ev
        C.save_state(state)
        return {"result": ev, "events": events, "pushed": push_events(cfg, events)}

    state["channels"].pop("__global__", None)
    silence_s = cfg["alert_silence_minutes"] * 60

    for ch in ev["channels"]:
        name = ch["channel"]
        st = state["channels"].setdefault(name, {})
        prev_alerting = bool(st.get("alerting"))

        if ch["alert"]:
            if force_alert:
                due = True
            elif not prev_alerting:
                due = True                      # 刚进入异常：立即告警
            elif silence_s <= 0:
                due = False                     # 0 = 同一异常只告警一次，恢复后才重新告警
            else:
                due = (now_ts - int(st.get("last_alert_time", 0))) >= silence_s
            if due:
                msg = build_alert_message(cfg, ch)
                events.append({"scope": name, "kind": "alert", "message": msg})
                st["last_alert_time"] = now_ts
                logger.warning("告警 %s: %s", name, _alert_reason(cfg, ch))
            st["alerting"] = True
        else:
            if prev_alerting and cfg.get("notify_recovery", True):
                msg = build_recovery_message(cfg, ch)
                events.append({"scope": name, "kind": "recovery", "message": msg})
                logger.info("恢复 %s", name)
            st["alerting"] = False
        st["latest"] = ch["latest"]
        st["status"] = ch["status"]
        st["idle_min"] = ch["idle_min"]
        st["checked_at"] = now_ts
        state["channels"][name] = st

    state["last_check"] = now_ts
    state["last_result"] = ev
    C.save_state(state)                          # 一轮只落一次盘（原文档是每通道一次）
    return {"result": ev, "events": events, "pushed": push_events(cfg, events)}


def push_events(cfg, events):
    """把一组事件合并成一条消息推送（多通道同时异常时不要刷屏）。"""
    if not events:
        return []
    text = "\n\n".join(e["message"] for e in events)
    return push_all(cfg, text)


def run_daily(cfg, state, now=None):
    now = now or now_dt(cfg)
    text = build_daily_report(cfg, now)
    results = push_all(cfg, text)
    state["last_report_day"] = now.strftime("%Y-%m-%d")
    state["last_report_at"] = int(now.timestamp())
    C.save_state(state)
    logger.info("日报已发送：%s", state["last_report_day"])
    return {"message": text, "pushed": results}


# ---------------------------------------------------------------- 主循环（供 server.py 起线程）

def monitor_loop(get_cfg, get_state, stop_event, on_tick=None, lock=None):
    """常驻循环：扫描 + 到点日报。

    get_cfg() 每轮重新取配置 —— 前端保存配置后**立即生效**，不用重启应用。
    lock：与「前端点『立即检测』」共用的互斥锁。state 必须是同一个对象，
    否则两条扫描路径各持一份 state，会重复发恢复通知（实测踩过）。
    """
    setup_logging()
    logger.info("ERM 监控启动")
    next_tick = 0.0
    while not stop_event.is_set():
        cfg = get_cfg()
        state = get_state()
        try:
            if time.time() >= next_tick:
                with (lock or contextlib.nullcontext()):
                    res = run_scan(cfg, state)
                if on_tick:
                    on_tick(res)
                next_tick = time.time() + max(30, int(cfg["scan_interval"]))
        except Exception as e:
            logger.exception("扫描异常: %s", e)
            next_tick = time.time() + 60
        try:
            if report_due(cfg, state):
                with (lock or contextlib.nullcontext()):
                    run_daily(cfg, state)
        except Exception as e:
            logger.exception("日报异常: %s", e)
        # 每 15 秒醒一次：日报到点精度 15 秒内，且 stop 能及时响应
        stop_event.wait(15)
    logger.info("ERM 监控已停止")
