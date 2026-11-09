#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EasyNVR 录像监控 —— 统计聚合（纯标准库，**零额外状态文件**）。

为什么不做采样库：
  录像文件本身就是最完整的历史账本。每个 mp4 的 mtime = 那一段录像写完的时刻，
  按时间排开就是「这个通道什么时候在录」。因此统计直接从磁盘反推：
    · 升级当天就能看到过去几十天的图表，不用等采集积累；
    · 不新增 SQLite / JSONL，不占空间、不和 state.json 抢写、不怕写坏；
    · 代价是「目录不可读 / 通道整体消失」这类语义状态看不出来 ——
      那属于"当前状态"，归状态页与告警管，统计页不重复。

口径（写死在代码里，改需求先对齐）：
  · 断流阈值 SEGMENT_GAP = 300 秒，与状态页「在线时长」用的 ONLINE_GAP_SECONDS 一致；
  · 在线时长 = 把录像 mtime 切成连续段，每段算 (末 − 首) + 一个「片段时长」。
    片段时长取段内相邻间隔的中位数（clamp 10–3600 秒，无间隔回落 60 秒）：
    60 个一分钟切片拼成的一小时正好算成 3600 秒，单个孤立文件也不会算成 0；
  · 闲置时长 = 窗口时长 − 在线时长；时间轴按「小时桶」切，桶从窗口起点整点算起；
  · 数量 = 窗口内录像文件数，按天 / 小时分桶；
  · 跨通道汇总一律取**每通道平均**（不然 10 个通道的"每日在线"会变成 240 小时）。
"""

import statistics
import time
from datetime import datetime, timedelta
from pathlib import Path

import config as C
import monitor as M

SEGMENT_GAP = 300          # 秒；> 5 分钟没新文件 = 断流（与状态页在线时长同口径）
PIECE_DEFAULT = 60         # 秒；推断不出片段时长时的兜底
MIN_GAP_REPORT = 300       # 秒；断流区间列表的最小长度
GAP_LIMIT = 200            # 断流清单最多返回多少条（v1.2.1：30 → 200，筛完还有得看）
GAP_WINDOW_SEC = 48 * 3600  # 断流清单的固定可见范围：最近 48 小时
# 断流分档：(key, 上界秒, 文案)；上界 None = 无上限。区间左开右闭，
# 正好 10:00 归「≤10 分钟」、正好 30:00 归「10–30 分钟」，不落两档。
GAP_BUCKETS = (
    ("le10", 600, "≤10 分钟"),
    ("m10_30", 1800, "10–30 分钟"),
    ("m30_60", 3600, "30 分钟–1 小时"),
    ("gt60", None, "> 1 小时"),
)
GAP_BUCKET_LABELS = {k: lb for k, _, lb in GAP_BUCKETS}

# range → (往今天之前回溯几天, 展示名)
RANGES = {
    "today": (0, "今天"),
    "yesterday": (1, "昨天"),
    "7d": (6, "近 7 天"),
    "30d": (29, "近 30 天"),
}
DEFAULT_RANGE = "7d"


# ---------------------------------------------------------------- 时间窗口

def window_of(cfg, key=DEFAULT_RANGE, now=None):
    """把 range 关键字翻译成时间窗口 + 需要打开哪些日期目录。

    today      → [今天 00:00, 现在]
    yesterday  → [昨天 00:00, 今天 00:00)
    7d / 30d   → [N 天前 00:00, 现在]
    """
    key = key if key in RANGES else DEFAULT_RANGE
    tz = M.get_tz(cfg)
    now = now or M.now_dt(cfg)
    now = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    back, label = RANGES[key]
    if key == "today":
        start, end = today0, now
    elif key == "yesterday":
        start, end = today0 - timedelta(days=1), today0
    else:
        start, end = today0 - timedelta(days=back), now

    # 日期目录：从窗口首日到窗口末日（末日取 end 前 1 秒所在的自然日）
    first = start.replace(hour=0, minute=0, second=0, microsecond=0)
    last = (end - timedelta(seconds=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    days = []
    d = first
    while d <= last:
        days.append(d)
        d += timedelta(days=1)

    return {
        "key": key, "label": label,
        "start": int(start.timestamp()), "end": int(end.timestamp()),
        "date_dirs": [d.strftime("%Y%m%d") for d in days],
        "day_labels": [d.strftime("%Y-%m-%d") for d in days],
        "single_day": key in ("today", "yesterday"),
    }


def gap_window(cfg, now=None):
    """断流清单的固定窗口：最近 48 小时（**不随页面的「今天 / 近 7 天」切换**）。

    两个口径要分开看，别混：
      · KPI 的「最长断流 / 断流段数」按页面选的时间范围算 —— 那是"这段时期里有多糟"；
      · 下面这份清单固定 48 小时 —— 那是"眼下有什么要处理"，近 30 天视图里若攒了
        几百条，最该看的今天那几条会被翻到最下面找不到。

    窗口是**滚动**的（now − 48h → now），不是从某个整点起算：否则同一段断流会随
    时间流逝在清单里"跳进跳出"。日期目录取窗口首日到今日（跨 3 天）。
    """
    tz = M.get_tz(cfg)
    now_dt = now or M.now_dt(cfg)
    now_dt = now_dt.replace(tzinfo=tz) if now_dt.tzinfo is None else now_dt.astimezone(tz)
    end = int(now_dt.timestamp())
    start = end - GAP_WINDOW_SEC
    first = datetime.fromtimestamp(start, tz).replace(hour=0, minute=0, second=0,
                                                      microsecond=0)
    last = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    days, d = [], first
    while d <= last:
        days.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return {
        "start": start, "end": end,
        "date_dirs": days,
        "start_str": M.fmt_ts(cfg, start), "end_str": M.fmt_ts(cfg, end),
        "label": "最近 48 小时",
    }


# ---------------------------------------------------------------- 目录读取（带缓存）

_CACHE = {}
_CACHE_TTL = 120           # 秒；统计页 15 秒刷新一次，缓存两分钟足够
_CACHE_MAX = 6000          # 条目上限，超了整表清空（宁可重扫，不让内存无界增长）


def clear_cache():
    _CACHE.clear()


def _ext_sig(exts):
    return ",".join(sorted(exts))


def _cache_get(key, now_ts):
    hit = _CACHE.get(key)
    if hit and now_ts - hit[0] <= _CACHE_TTL:
        return hit[1]
    _CACHE.pop(key, None)
    return None


def _cache_put(key, val, now_ts):
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (now_ts, val)


def _dir_mtimes(d, exts, now_ts):
    """单层目录里所有录像文件的 mtime（升序），按目录缓存。"""
    key = ("d", str(d), _ext_sig(exts))
    val = _cache_get(key, now_ts)
    if val is None:
        val = sorted(M._flat_mtimes(d, exts))
        _cache_put(key, val, now_ts)
    return val


def channel_layout(base):
    """通道目录的形态：day（下一层就是 YYYYMMDD）/ profile（下一层 profile_N）/ flat。"""
    base = Path(base)
    subs = M._subdirs(base)
    if any(M.DATE_RE.match(s.name) for s in subs):
        return "day", [base]
    profs = sorted((s for s in subs if M.PROFILE_RE.match(s.name)), key=lambda p: p.name)
    if profs:
        return "profile", profs
    return "flat", [base]


def channel_mtimes(base, exts, date_dirs, now_ts):
    """通道在给定日期目录里的所有录像 mtime（升序）。

    通道目录不存在 → 返回 None（前端据此标「目录不存在」，而不是当成"没录像"）。
    """
    base = Path(base)
    if not base.is_dir():
        return None
    layout, parents = channel_layout(base)
    if layout == "flat":                 # 文件直接躺在通道目录里，没有日期分层
        return list(_dir_mtimes(base, exts, now_ts))
    want = set(date_dirs)
    out = []
    for parent in parents:
        for sub in M._subdirs(parent):
            if not M.DATE_RE.match(sub.name):
                continue
            if sub.name[:8] not in want:     # 20261029_event 取前 8 位也是 20261029
                continue
            out.extend(_dir_mtimes(sub, exts, now_ts))
    out.sort()
    return out


# ---------------------------------------------------------------- 区间运算

def segments(mts, gap=SEGMENT_GAP):
    """mtime 序列 → 连续录像段 [(start, end)]，end 已补上「片段时长」。

    片段时长 = 段内相邻间隔的中位数（clamp 10–3600，无间隔回落 60）。
    这样：60 个 1 分钟切片 = 3600 秒（正好一小时）；单个孤立文件 = 60 秒（不是 0）。
    """
    if not mts:
        return []
    groups, cur = [], [mts[0]]
    for m in mts[1:]:
        if m - cur[-1] <= gap:
            cur.append(m)
        else:
            groups.append(cur)
            cur = [m]
    groups.append(cur)

    diffs = []
    for g in groups:
        for i in range(1, len(g)):
            d = g[i] - g[i - 1]
            if 10 <= d <= 3600:
                diffs.append(d)
    piece = int(statistics.median(diffs)) if diffs else PIECE_DEFAULT
    piece = max(10, min(3600, piece))
    return [(g[0], g[-1] + piece) for g in groups]


def clip(intervals, start, end):
    """把区间裁到 [start, end)。"""
    out = []
    for s, e in intervals:
        s2, e2 = max(s, start), min(e, end)
        if e2 > s2:
            out.append((s2, e2))
    return out


def bucket_len(start, end, i):
    """窗口内第 i 个整点桶的实际长度（末尾不足 1 小时的桶只算到 now）。"""
    b0 = start + i * 3600
    return max(0, min(3600, end - b0))


def bucket_seconds(intervals, start, end):
    """区间集合 → {桶序号: 覆盖秒数}。桶 i = [start + i*3600, +3600)。"""
    out = {}
    for s, e in intervals:
        i = max(0, (s - start) // 3600)
        while True:
            b0 = start + i * 3600
            if b0 >= end:
                break
            b1 = min(b0 + 3600, end)
            ov = min(e, b1) - max(s, b0)
            if ov > 0:
                out[i] = out.get(i, 0) + ov
            if b1 >= e:
                break
            i += 1
    return out


def gaps_of(mts, start, end, min_gap=MIN_GAP_REPORT):
    """窗口内的断流区间 [(from_ts, to_ts)]：相邻两个录像之间 > min_gap。

    起点不算「窗口开头 → 第一个录像」那一段：通道还没装/还没启用的日子不该
    被记成断流（7 天视图里会凭空多出好几条 4 天的"断流"）。
    终点算「最后一个录像 → 窗口终点」：那才是真断流，包括"到现在还在断"。
    """
    if not mts:
        return []
    out = []
    prev = max(start, mts[0])
    for m in mts[1:]:
        if m > end:
            break
        if m - prev > min_gap:
            out.append((prev, m))
        prev = max(prev, m)
    if end - prev > min_gap:
        out.append((prev, end))
    return out


def gap_bucket_of(dur_sec):
    """断流时长 → 档位 key（见 GAP_BUCKETS）。"""
    for key, upper, _ in GAP_BUCKETS:
        if upper is None or dur_sec <= upper:
            return key
    return GAP_BUCKETS[-1][0]


def _last_date_dir(base):
    """通道目录里最新的录像日期（YYYYMMDD 字符串），没有日期分层返回空串。

    只列目录名，不读文件 —— 用途只有一个：判断"这个通道以前录过没有"。
    """
    layout, parents = channel_layout(base)
    last = ""
    for p in parents:
        for sub in M._subdirs(p):
            name = sub.name
            if M.DATE_RE.match(name) and name[:8] > last:
                last = name[:8]
    return last


def gaps_recent(cfg, base, exts, win, now_ts, min_gap=MIN_GAP_REPORT):
    """48 小时窗口内的断流区间，**含「整个窗口都没录像」这一种**。

    前半段直接复用 gaps_of 的口径，但 gap_of 刻意没管一个情况：窗口内一个文件都没有。
    gaps_of 拿不到 mts 就返回空，于是"前天下午录完就再没动过"的通道会从清单里消失
    —— 而这恰恰是最该看见的那一条。判据是通道里存在早于窗口起点的录像日期，
    就记一条 (窗口起点 → 现在)。从没录过（目录是空的）不算，那是"没装/没开录像"，
    归状态页的 missing，不该混进断流清单冒充故障。
    """
    mts = channel_mtimes(base, exts, win["date_dirs"], now_ts)
    if mts is None:                       # 通道目录不存在
        return []
    in_win = [m for m in mts if win["start"] <= m <= win["end"]]
    if in_win:
        return gaps_of(in_win, win["start"], win["end"], min_gap)
    first_day = datetime.fromtimestamp(win["start"], M.get_tz(cfg)).strftime("%Y%m%d")
    last_day = _last_date_dir(base)
    if last_day and last_day < first_day:
        return [(win["start"], win["end"])]
    return []


# ---------------------------------------------------------------- 汇总

def build(cfg, range_key=DEFAULT_RANGE, now=None):
    """统计页的完整数据（一次算完，前端只负责画）。"""
    win = window_of(cfg, range_key, now)
    start, end = win["start"], win["end"]
    span = max(1, end - start)
    now_ts = int(time.time())
    exts = set(cfg.get("video_exts") or C.DEFAULT_CONFIG["video_exts"])
    chans = M.resolve_channels(cfg)
    n_buckets = (span + 3599) // 3600

    result = {
        "ok": True,
        "range": win["key"],
        "range_label": win["label"],
        "single_day": win["single_day"],
        "start_str": M.fmt_ts(cfg, start),
        "end_str": M.fmt_ts(cfg, end),
        "span_sec": span,
        "bucket_count": n_buckets,
        "day_labels": win["day_labels"],
        "error": "",
        "channels": [],
    }
    if not (cfg.get("record_root") or "").strip():
        result["error"] = "未配置录像目录"
        return result
    if not chans:
        result["error"] = ("录像目录下没有识别到任何通道。EasyNVR 的录像应为 "
                           "<摄像头ID>/profile_N/<日期>/")
        return result

    # ---- 逐通道采集 ----
    rows = []
    for ch in chans:
        mts = channel_mtimes(ch["path"], exts, win["date_dirs"], now_ts)
        missing = mts is None
        in_win = [m for m in (mts or []) if start <= m <= end]
        segs = clip(segments(in_win), start, end)
        rows.append({
            "channel": ch["channel"],
            "label": ch["label"] or ch["channel"],
            "layout": ch.get("layout") or "",
            "missing": missing,
            "files": len(in_win),
            "segments": segs,
            "buckets": bucket_seconds(segs, start, end),
            "gaps": gaps_of(in_win, start, end),
            "online_sec": sum(e - s for s, e in segs),
            "_mts": in_win,                     # 内部用（算文件数柱），返回前 pop
        })

    n_ch = max(1, len(rows))
    total_online = sum(r["online_sec"] for r in rows)

    # ---- 时间轴（多日 → 按天；单日 → 按小时）----
    # bars[i] 的 online_sec 是「每通道平均」，否则 10 个通道的"每日在线"会变成 240 小时。
    bars = []
    if win["single_day"]:
        for i in range(n_buckets):
            sec = sum(r["buckets"].get(i, 0) for r in rows)
            bars.append({"label": "%02d" % i, "online_sec": sec // n_ch,
                         "bucket_sec": bucket_len(start, end, i), "files": 0})
    else:
        for d in range(len(win["day_labels"])):
            sec = sum(v for r in rows for i, v in r["buckets"].items() if i // 24 == d)
            # 今天只走到 now：分母不能按 86400 算，否则在线率会被压到 40%
            dsec = sum(bucket_len(start, end, i)
                       for i in range(d * 24, min((d + 1) * 24, n_buckets)))
            bars.append({"label": win["day_labels"][d][5:], "online_sec": sec // n_ch,
                         "bucket_sec": dsec, "files": 0})

    # 文件数按同一套桶口径归桶，保证两张图的横轴严格对齐
    for r in rows:
        for m in r["_mts"]:
            i = max(0, (m - start) // 3600)
            key = i if win["single_day"] else i // 24
            if 0 <= key < len(bars):
                bars[key]["files"] += 1
    for b in bars:
        b["online_pct"] = round(100.0 * b["online_sec"] / max(1, b["bucket_sec"]), 1)
        b["idle_sec"] = max(0, b["bucket_sec"] - b["online_sec"])

    # ---- 热力图：单日 = 通道 × 小时；多日 = 天 × 小时 ----
    heat = {"kind": "channel" if win["single_day"] else "day", "rows": [], "cells": []}
    if win["single_day"]:
        heat["cols"] = ["%02d" % i for i in range(n_buckets)]
        for r in rows:
            heat["rows"].append(r["label"])
            line = []
            for i in range(n_buckets):
                blen = bucket_len(start, end, i) or 1
                line.append(round(min(1.0, r["buckets"].get(i, 0) / blen), 3))
            heat["cells"].append(line)
    else:
        heat["cols"] = ["%02d" % i for i in range(24)]
        n_days = len(win["day_labels"])
        for d in range(n_days):
            heat["rows"].append(win["day_labels"][d][5:])
            line = []
            for h in range(24):
                i = d * 24 + h
                if i >= n_buckets:
                    line.append(None)                     # 今天还没走到的小时
                    continue
                blen = bucket_len(start, end, i) or 1
                sec = sum(r["buckets"].get(i, 0) for r in rows)
                line.append(round(min(1.0, sec / (blen * n_ch)), 3))
            heat["cells"].append(line)

    # ---- 通道汇总 ----
    for r in rows:
        r["online_pct"] = round(100.0 * r["online_sec"] / span, 1)
        r["idle_sec"] = max(0, span - r["online_sec"])
        r["longest_sec"] = max((e - s for s, e in r["segments"]), default=0)
        r.pop("buckets", None)
        r.pop("segments", None)
        r.pop("_mts", None)

    # ---- 断流区间清单：固定最近 48 小时（与上面 KPI 的口径分开）----
    gwin = gap_window(cfg, now=now)
    gap_items = []
    for ch in chans:
        for s, e in gaps_recent(cfg, ch["path"], exts, gwin, now_ts):
            dur = int(e - s)
            bk = gap_bucket_of(dur)
            gap_items.append({"channel": ch["channel"],
                              "label": ch["label"] or ch["channel"],
                              "from": int(s), "to": int(e),
                              "dur_sec": dur, "minutes": dur // 60,
                              "bucket": bk,
                              "bucket_label": GAP_BUCKET_LABELS.get(bk, ""),
                              "dur_str": M.fmt_duration(dur),
                              "from_str": M.fmt_ts(cfg, s), "to_str": M.fmt_ts(cfg, e)})
    gap_items.sort(key=lambda g: g["to"], reverse=True)
    gap_counts = {"all": len(gap_items)}
    for key, _, _ in GAP_BUCKETS:
        gap_counts[key] = 0
    for g in gap_items:
        gap_counts[g["bucket"]] = gap_counts.get(g["bucket"], 0) + 1

    # KPI 的「最长断流」仍按页面选的时间范围算 —— 那是「这段时期有多糟」，
    # 和上面这份「眼下 48 小时」的清单不是一回事，别拿一个当另一个。
    range_gaps = [g for r in rows for g in r["gaps"]]
    range_gap_total = len(range_gaps)
    longest_gap = max((int(e - s) // 60 for s, e in range_gaps), default=0)

    result["channels"] = [{
        "channel": r["channel"], "label": r["label"], "missing": r["missing"],
        "files": r["files"], "online_sec": int(r["online_sec"]),
        "idle_sec": int(r["idle_sec"]), "online_pct": r["online_pct"],
        "longest_sec": int(r["longest_sec"]),
        "online_str": M.fmt_duration(r["online_sec"]),
        "idle_str": M.fmt_duration(r["idle_sec"]),
        "longest_str": M.fmt_duration(r["longest_sec"]),
        "gap_count": len(r["gaps"]),
    } for r in rows]

    result["bars"] = bars
    result["heat"] = heat
    result["gaps"] = gap_items[:GAP_LIMIT]
    result["gap_total"] = gap_counts["all"]
    result["gap_truncated"] = gap_counts["all"] > GAP_LIMIT
    result["gap_window"] = {
        "start": int(gwin["start"]), "end": int(gwin["end"]),
        "start_str": M.fmt_ts(cfg, gwin["start"]), "end_str": M.fmt_ts(cfg, gwin["end"]),
        "label": gwin["label"],
    }
    result["gap_buckets"] = (
        [{"key": "all", "label": "全部", "count": gap_counts["all"]}] +
        [{"key": k, "label": lb, "count": gap_counts[k]} for k, _, lb in GAP_BUCKETS])
    result["summary"] = {
        "channels": len(rows),
        "files": sum(r["files"] for r in rows),
        "online_sec": int(total_online // n_ch),
        "idle_sec": int(max(0, span - total_online // n_ch)),
        "online_pct": round(100.0 * total_online / (span * n_ch), 1),
        "longest_online_sec": int(max((r["longest_sec"] for r in rows), default=0)),
        "longest_gap_min": int(longest_gap),
        "longest_gap_str": M.fmt_duration(longest_gap * 60) if longest_gap else "无",
        "gap_total": range_gap_total,
        "missing_channels": sum(1 for r in rows if r["missing"]),
        "online_str": M.fmt_duration(total_online // n_ch),
        "idle_str": M.fmt_duration(max(0, span - total_online // n_ch)),
        "longest_str": M.fmt_duration(max((r["longest_sec"] for r in rows), default=0)),
    }
    return result
