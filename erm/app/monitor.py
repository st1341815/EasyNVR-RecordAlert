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
from notify import push_all, all_ok as push_all_ok, failed_channels

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
    """昨天 00:00 ~ 今天 00:00 的时间戳（按配置时区）。保留给旧调用方/测试。"""
    now = now or now_dt(cfg)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=1)
    return int(start.timestamp()), int(today.timestamp())


def hm_to_min(value, default=0):
    """'08:30' → 510。容忍 '8：30' / '8:30'；非法值回落 default 分钟数。"""
    try:
        parts = str(value or "").replace("：", ":").strip().split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (TypeError, ValueError, IndexError):
        return default


def report_window_bounds(cfg, now=None):
    """日报的统计区间，返回半开区间 [start_ts, end_ts)。

    v1.0.16：区间可自定义（report_window_from / report_window_to），HH:MM **含端点**，
    所以内部 end 多加 1 分钟 —— 默认 00:00–23:59 的结果是
    `[昨天 00:00, 今天 00:00)`，与 v1.0.15 的「昨天整天」完全一致（不丢 23:59 的文件）。

    · to > from  → 同一个自然日内（08:00–18:00）
    · to <= from → 跨天（08:00–次日 08:00），from == to 视为整天 24 小时

    「最近一个完整区间」：把 to 吸附到今天，若还没到就退回昨天 —— 日报在 09:00 发、
    区间 08:00–次日 08:00 时，统计的是昨天 08:00 到今天 08:00。
    """
    now = now or now_dt(cfg)
    f_min = hm_to_min(cfg.get("report_window_from"), 0)
    t_min = hm_to_min(cfg.get("report_window_to"), 23 * 60 + 59)
    span = (t_min - f_min) % 1440 or 1440          # from == to → 整整 24 小时
    end = now.replace(hour=t_min // 60, minute=t_min % 60, second=0, microsecond=0)
    if end > now:
        end -= timedelta(days=1)
    start = end - timedelta(minutes=span)          # 先算起点，再给终点补「含端点」的 1 分钟
    return int(start.timestamp()), int((end + timedelta(minutes=1)).timestamp())


def _in_window(cur_min, from_min, to_min):
    """分钟数是否落在 [from, to] 内。from == to = 全天；from > to = 跨夜。"""
    if from_min == to_min:
        return True
    if from_min < to_min:
        return from_min <= cur_min <= to_min
    return cur_min >= from_min or cur_min <= to_min


def alert_push_allowed(cfg, now=None):
    """告警推送计划：推送日 + 推送时段。false = 只检测、不推送（不消耗静默窗口）。"""
    now = now or now_dt(cfg)
    days = cfg.get("alert_push_weekdays") or list(C.DEFAULT_CONFIG["alert_push_weekdays"])
    if now.isoweekday() not in days:
        return False
    return _in_window(now.hour * 60 + now.minute,
                      hm_to_min(cfg.get("alert_window_from"), 0),
                      hm_to_min(cfg.get("alert_window_to"), 23 * 60 + 59))


def weekdays_text(days):
    days = sorted(set(int(d) for d in (days or [])))
    if days == list(C.WEEKDAY_ALL):
        return "每天"
    if days == list(C.WEEKDAY_WORK):
        return "工作日"
    if days == list(C.WEEKDAY_WEEKEND):
        return "周末"
    if not days:
        return "—"
    return "、".join(C.WEEKDAY_NAMES.get(d, str(d)) for d in days)


def window_text(from_s, to_s):
    f, t = hm_to_min(from_s, 0), hm_to_min(to_s, 23 * 60 + 59)
    # 00:00–23:59 与「起止相同」都等价于全天，文案统一说「全天」
    if f == t or (f == 0 and t == 23 * 60 + 59):
        return "全天"
    text = "%s–%s" % (from_s, to_s)
    return text + "（跨夜）" if f > t else text


def push_plan_text(cfg):
    """状态页用：把「工作日 08:00–18:00」拼成一句话。"""
    return "%s %s" % (weekdays_text(cfg.get("alert_push_weekdays")),
                      window_text(cfg.get("alert_window_from"), cfg.get("alert_window_to")))


def report_plan_text(cfg):
    return "%s %s" % (weekdays_text(cfg.get("report_weekdays")),
                      window_text(cfg.get("report_window_from"), cfg.get("report_window_to")))


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


def _report_span_text(cfg, start_ts, end_ts):
    """区间文案：同一天写「2026-11-03 00:00 — 23:59」，跨天写全两个日期。

    end_ts 是内部半开上界（已 +1 分钟），显示时要减回去，不然会显示成 24:00。
    """
    tz = get_tz(cfg)
    sdt = datetime.fromtimestamp(start_ts, tz)
    edt = datetime.fromtimestamp(max(start_ts, end_ts - 60), tz)
    if sdt.date() == edt.date():
        return "%s %s — %s" % (sdt.strftime("%Y-%m-%d"), sdt.strftime("%H:%M"),
                               edt.strftime("%H:%M"))
    return "%s — %s" % (sdt.strftime("%Y-%m-%d %H:%M"), edt.strftime("%Y-%m-%d %H:%M"))


def build_daily_report(cfg, now=None):
    now = now or now_dt(cfg)
    start, end = report_window_bounds(cfg, now)
    ev = evaluate(cfg, now=now, window=(start, end))
    day = datetime.fromtimestamp(start, get_tz(cfg)).strftime("%Y-%m-%d")
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
        "统计区间：%s" % _report_span_text(cfg, start, end),
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
    """下一次日报时间；日报开关关掉时返回 None（调用方要判空）。

    v1.0.16：按 report_weekdays 跳过非推送日 —— 设了「只在工作日」时，
    周五 18:00 看到的下次日报是下周一 08:00，而不是周六 08:00。
    """
    if not cfg.get("daily_report_enabled", True):
        return None
    now = now or now_dt(cfg)
    hh, mm = [int(x) for x in cfg["daily_report_time"].split(":")]
    days = cfg.get("report_weekdays") or list(C.WEEKDAY_ALL)
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    for i in range(0, 8):                       # 最多往前找一周（今天 + 7 天）
        cand = target + timedelta(days=i)
        if cand > now and cand.isoweekday() in days:
            return cand
    return None                                 # 理论上到不了：norm_weekdays 兜底每天


def report_due(cfg, state, now=None):
    """到点即发：当前时间 >= 今天的目标时间，且今天还没发过。

    这样进程重启、NAS 睡眠错过 08:00 也能补发当天一次，而不是漏掉一整天；
    last_report_day 落在 state.json，重启不会重复发。

    v1.0.16：非 report_weekdays 的日子不发（周末不打扰），也不写 last_report_day；
    「补发昨日日报」按钮（manual）不受推送日限制。

    v1.0.15：日报开关（daily_report_enabled）关掉时直接不发；state 里的
    last_report_day / report_retry 原样保留 —— 重新打开开关后，当天到点补发，
    不会因为「关掉那天已经算发过」而漏一天。

    v1.0.14：上一次推送**失败**时不写 last_report_day，而是登记 report_retry
    （带 next_try），到点由这里放行补发 —— 见 run_daily 的注释。
    """
    if not cfg.get("daily_report_enabled", True):
        return False
    now = now or now_dt(cfg)
    if now.isoweekday() not in (cfg.get("report_weekdays") or list(C.WEEKDAY_ALL)):
        return False
    today = now.strftime("%Y-%m-%d")
    retry = state.get("report_retry") or {}
    if isinstance(retry, dict) and retry.get("day") == today:
        return float(now.timestamp()) >= float(retry.get("next_try") or 0)
    if state.get("last_report_day") == today:
        return False
    hh, mm = [int(x) for x in cfg["daily_report_time"].split(":")]
    return (now.hour, now.minute) >= (hh, mm)


# ---------------------------------------------------------------- 执行

def run_scan(cfg, state, force_alert=False):
    """一轮扫描：判定 → 告警（含静默）→ 恢复通知 → 落 state。

    v1.0.16：先过「告警推送计划」闸门。时段外（夜里/周末）**只刷新检测结果**，
    不动通道告警状态、不推送 —— 好处是夜间断流不会被静默规则「吃掉」：
    早上一进时段，因为 alerting 仍是 False、last_alert_time 没推进，立刻就会告警。
    手动「立即检测」（force_alert）不受限制，用户就在页面盯着。
    """
    now_ts = int(time.time())
    ev = evaluate(cfg, with_counts=False)
    state.setdefault("channels", {})
    events = []

    allowed = alert_push_allowed(cfg, now_dt(cfg))
    if not allowed and not force_alert:
        state["last_check"] = now_ts
        state["last_result"] = ev
        if not state.get("quiet_since"):
            state["quiet_since"] = now_ts
            logger.info("进入推送时段外（%s）：告警照常检测，但不推送；进入时段后立即补发",
                        push_plan_text(cfg))
        C.save_state(state)
        return {"result": ev, "events": [], "pushed": [], "quiet": True}
    if allowed and state.pop("quiet_since", None):
        logger.info("离开推送时段外，恢复正常推送（%s）", push_plan_text(cfg))

    if ev.get("error") and not ev["channels"]:
        # 全局错误（目录没配/不可读/没有通道）：也要告警，否则用户以为在监控
        key = "__global__"
        gst = state["channels"].setdefault(key, {})
        silence_s = cfg["alert_silence_minutes"] * 60
        due = force_alert or (not gst.get("alerting")) or (
            silence_s == 0) or (now_ts - int(gst.get("last_alert_time", 0)) >= silence_s)
        if due:
            msg = "【EasyNVR 录像监控异常】\n%s\n检测时间：%s" % (ev["error"], fmt_ts(cfg, now_ts))
            events.append({"scope": key, "kind": "alert", "message": msg,
                           "prev_alert_time": gst.get("last_alert_time")})
            gst["last_alert_time"] = now_ts
            gst["alerting"] = True
            state["channels"][key] = gst
        state["last_check"] = now_ts
        state["last_result"] = ev
        C.save_state(state)
        pushed = push_events(cfg, events)
        if rollback_failed_alerts(state, events, pushed):
            mark_alert_retry(state, events, pushed)
            C.save_state(state)
        else:
            scopes = [e.get("scope") for e in events if e.get("kind") == "alert"]
            if scopes and clear_alert_retry(state, scopes):
                C.save_state(state)
        return {"result": ev, "events": events, "pushed": pushed}

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
            elif st.get("next_alert_retry"):
                # 上次推送失败、还欠一条告警：到退避时间就重发
                due = now_ts >= int(st.get("next_alert_retry") or 0)
            else:
                due = (now_ts - int(st.get("last_alert_time", 0))) >= silence_s
            if due:
                msg = build_alert_message(cfg, ch)
                events.append({"scope": name, "kind": "alert", "message": msg,
                               "prev_alert_time": st.get("last_alert_time")})
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
    pushed = push_events(cfg, events)
    if events:
        if rollback_failed_alerts(state, events, pushed):
            mark_alert_retry(state, events, pushed)   # 按退避节奏安排下一次重试
            C.save_state(state)
        else:
            scopes = [e.get("scope") for e in events if e.get("kind") == "alert"]
            if scopes and clear_alert_retry(state, scopes):
                C.save_state(state)
    return {"result": ev, "events": events, "pushed": pushed}


def pending_alert_retries(state):
    """尚未送达的告警，各自的「最早重试时间」列表（给主循环算唤醒间隔）。"""
    out = []
    for st in (state.get("channels") or {}).values():
        if not isinstance(st, dict):
            continue
        t = st.get("next_alert_retry")
        if t:
            try:
                out.append(float(t))
            except (TypeError, ValueError):
                continue
    return out


def rollback_failed_alerts(state, events, pushed):
    """告警一条都没送出去时，把 last_alert_time 回滚到推送前。

    背景（v1.0.14）：原先无论推送成败都推进 last_alert_time，飞书被限流的那次告警
    就被当成"已告警"，**整个静默期（默认 120 分钟）都不会再发** —— 限流一分钟，
    丢两小时。现在失败就回滚，下一轮扫描自然会重发。
    `alerting` 保持 True（异常事实仍然成立），只是让"该不该发"重新判定。
    """
    if not events or not pushed or push_all_ok(pushed):
        return False
    if not failed_channels(pushed):
        return False                    # 没启用渠道：没有"推送失败"可言
    changed = False
    for e in events:
        if e.get("kind") != "alert":
            continue
        st = state.get("channels", {}).get(e.get("scope"))
        if not isinstance(st, dict):
            continue
        prev = e.get("prev_alert_time")
        if prev is None:
            st.pop("last_alert_time", None)
        else:
            st["last_alert_time"] = prev
        st["rolled_back"] = True
        changed = True
    if changed:
        logger.warning("告警推送失败（%s），已回滚告警时间戳，下一轮扫描重发",
                       "、".join(C.PUSH_LABEL.get(c, str(c)) for c in failed_channels(pushed)))
    return changed


# 告警补发：连续推送失败时逐步拉长间隔（30s → 60s → 120s → 300s，封顶 5 分钟）。
# 为什么不能一直按「扫描间隔」硬重试：NAS 挂了整晚、飞书整晚抽风时，
# 5 分钟一次的失败日志会把 monitor.log 刷满（5MB×3 轮转也就几天）。
ALERT_RETRY_BACKOFF = (30, 60, 120, 300)
ALERT_RETRY_MAX = 300


def _retry_delay(fails):
    """第 fails 次失败后，多久再试。"""
    idx = max(0, int(fails) - 1)
    if idx < len(ALERT_RETRY_BACKOFF):
        return ALERT_RETRY_BACKOFF[idx]
    return ALERT_RETRY_MAX


def mark_alert_retry(state, events, pushed):
    """告警推送失败 → 登记 alert_retry（下次最早何时再试）。

    与 rollback_failed_alerts 配套：回滚让「due 判定」重新放行，这里控制**节奏**，
    等价于给静默窗口加一层"退避" —— 白天正常（扫描 10 分钟一次）时下一轮就重发，
    整晚失败时也不会把日志刷爆。
    """
    failed = failed_channels(pushed or [])
    if not events or not failed:
        return False
    scopes = [e.get("scope") for e in events if e.get("kind") == "alert"]
    if not scopes:
        return False
    now_ts = int(time.time())
    delay = 0
    for scope in scopes:
        st = state.get("channels", {}).get(scope)
        if not isinstance(st, dict):
            continue
        fails = int(st.get("alert_fails") or 0) + 1
        delay = _retry_delay(fails)
        st["alert_fails"] = fails
        st["next_alert_retry"] = now_ts + delay
    logger.warning("告警未能送达（%s），%d 秒后重试",
                   "、".join(C.PUSH_LABEL.get(c, str(c)) for c in failed), delay)
    return True


def clear_alert_retry(state, scopes):
    """推送成功 → 清掉补发状态。

    注意 `or` 的短路：`a or b or c` 在 a 为真时就不会执行 b/c，于是
    alert_fails / next_alert_retry 会一直留着，下一轮被误判成"还欠一条告警"而重发
    （自检抓到：ROUND3 成功送达后 ROUND4 仍重复告警）。所以先取值再 pop，别短路。
    """
    changed = False
    for scope in scopes:
        st = state.get("channels", {}).get(scope)
        if not isinstance(st, dict):
            continue
        for key in ("alert_fails", "next_alert_retry", "rolled_back"):
            if st.pop(key, None) is not None:
                changed = True
    return changed


def push_events(cfg, events):
    """把一组事件合并成一条消息推送（多通道同时异常时不要刷屏）。"""
    if not events:
        return []
    text = "\n\n".join(e["message"] for e in events)
    return push_all(cfg, text)


DAILY_RETRY_MAX = 3        # 失败后最多补发 3 次（首次之外）
DAILY_RETRY_DELAY = 300    # 秒；补发间隔 5 分钟，够跨过整点推送高峰


def _failure_summary(results):
    return "；".join("%s：%s" % (r.get("label") or r.get("channel"), r.get("detail"))
                    for r in (results or []) if not r.get("ok")) or "未知原因"


def run_daily(cfg, state, now=None, manual=False):
    """发日报。

    v1.0.14 的两个修正（2026-11-01/02 09:00 连续被飞书限流暴露）：
      1. **不再谎报「已发送」**：只有全部启用渠道都成功才写 last_report_day；
      2. **失败自动补发**：登记 report_retry（5 分钟后、最多 3 次），由 report_due
         放行；仍然失败才写 last_report_day 并记 ERROR，避免永久性配置错误
         （webhook 写错）每 15 秒重刷一遍日志。
    手动「立即发送」（manual=True）不登记补发：用户就在页面盯着，失败他自己会再点。
    """
    now = now or now_dt(cfg)
    today = now.strftime("%Y-%m-%d")
    text = build_daily_report(cfg, now)
    results = push_all(cfg, text)

    def _done(ok):
        C.save_state(state)
        return {"message": text, "pushed": results, "ok": ok, "day": today}

    if push_all_ok(results):
        state["last_report_day"] = today
        state["last_report_at"] = int(now.timestamp())
        state.pop("report_retry", None)
        logger.info("日报已发送：%s", today)
        return _done(True)

    failed = failed_channels(results)
    prev = state.get("report_retry") or {}
    retries_done = int(prev.get("retries") or 0) if (
        isinstance(prev, dict) and prev.get("day") == today) else 0

    if manual or not failed:
        # 手动触发，或压根没启用渠道：不补发。
        # 「今天已发」只在**没启用渠道**时才落 —— 手动发送失败不能把当天的自动日报吞掉。
        if not failed:
            state["last_report_day"] = today
        state.pop("report_retry", None)
        logger.error("日报推送失败（%s，%s），本次不补发", today, _failure_summary(results))
        return _done(False)

    if retries_done >= DAILY_RETRY_MAX:
        state["last_report_day"] = today
        state.pop("report_retry", None)
        logger.error("日报推送失败，已补发 %d 次仍失败，今天不再重试（%s）：%s",
                     DAILY_RETRY_MAX, today, _failure_summary(results))
        return _done(False)

    state["report_retry"] = {
        "day": today,
        "retries": retries_done + 1,
        "next_try": int(now.timestamp()) + DAILY_RETRY_DELAY,
        "failed": failed,
    }
    logger.warning("日报推送失败（失败渠道：%s），%d 秒后第 %d/%d 次补发",
                   "、".join(C.PUSH_LABEL.get(c, str(c)) for c in failed),
                   DAILY_RETRY_DELAY, retries_done + 1, DAILY_RETRY_MAX)
    return _done(False)


# ---------------------------------------------------------------- 主循环（供 server.py 起线程）

def monitor_loop(get_cfg, get_state, stop_event, on_tick=None, lock=None):
    """常驻循环：扫描 + 到点日报。

    get_cfg() 每轮重新取配置 —— 前端保存配置后**立即生效**，不用重启应用。
    lock：与「前端点『立即检测』」共用的互斥锁。state 必须是同一个对象，
    否则两条扫描路径各持一份 state，会重复发恢复通知（实测踩过）。
    v1.0.14：有告警补发待办时，唤醒粒度收紧到 ≤5 秒（退避到点就发，不等一个扫描周期）。
    """
    setup_logging()
    logger.info("ERM 监控启动")
    next_tick = 0.0
    while not stop_event.is_set():
        cfg = get_cfg()
        state = get_state()
        wait = 15
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
        # 告警补发到点后最多再等 5 秒就发，而不是干等一个扫描周期
        try:
            waits = [t - time.time() for t in pending_alert_retries(state)]
            if waits:
                wait = min(wait, max(1.0, min(waits)), 5.0)
        except Exception:
            pass
        # 每 15 秒醒一次：日报到点精度 15 秒内，且 stop 能及时响应
        stop_event.wait(max(1.0, wait))
    logger.info("ERM 监控已停止")
