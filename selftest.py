#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""erm 本地自检（不依赖 pytest / 不联网，除本地 mock HTTP）。

覆盖文档评审里点出的每个逻辑坑：
  · 向导字符串 → 强类型（B1）
  · 通道只按 EasyNVR 真实布局 <摄像头ID>/profile_N/<日期> 自动发现（v1.0.8）
  · 逗号字符串 → 列表，不能拆成单字符（B2）
  · 日报「到点即发 + 当天只发一次 + 重启不重复」（B3）
  · 从未录过像 / 目录不存在 也要告警（B4）
  · 闲置判定基于「最新文件时间」，清理旧文件不误报（B5）
  · 推送看 HTTP 状态 + 业务错误码，钉钉加签（B6）
  · 状态原子写、配置 0600（A7）

跑法：  PYTHONPATH= python3 selftest.py
"""

import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "erm", "app")
sys.path.insert(0, APP)

# 隔离数据目录：必须在 import config 之前设置
TMP_VAR = tempfile.mkdtemp(prefix="erm-selftest-")
os.environ["TRIM_PKGVAR"] = TMP_VAR

import config as C      # noqa: E402
import monitor as M     # noqa: E402
import notify as N      # noqa: E402

FAILS = []
CHECKS = [0]


def ok(cond, name, detail=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append("%s %s" % (name, ("→ " + str(detail)) if detail else ""))


def eq(actual, expected, name):
    ok(actual == expected, name, "got %r, want %r" % (actual, expected))


CAM_OK = "AAAA0000000A"        # 刚录过（profile_1 有今天、profile_2 只有 3 小时前）
CAM_SILENT = "BBBB0000000B"    # 3 小时没录像
CAM_EMPTY = "CCCC0000000C"     # profile 目录在，但一个录像文件都没有


def mk_record_tree(root):
    """造一棵 EasyNVR 真实布局的目录：
    <root>/<摄像头ID>/profile_<n>/<YYYYMMDD>/<录像文件>

    A 刚录过（两个 profile）/ B 停了 3 小时 / C 空目录。
    返回 (root, {摄像头ID: [文件路径…]})。
    """
    now = time.time()
    d_now = datetime.fromtimestamp(now).strftime("%Y%m%d")
    d_old = datetime.fromtimestamp(now - 3 * 3600).strftime("%Y%m%d")
    files = {}
    for cam, prof, day, ts in (
            (CAM_OK, "profile_1", d_now, now - 60),
            (CAM_OK, "profile_2", d_old, now - 3 * 3600),
            (CAM_SILENT, "profile_1", d_old, now - 3 * 3600)):
        p = os.path.join(root, cam, prof, day)
        os.makedirs(p, exist_ok=True)
        fp = os.path.join(p, "20261029_%s.mp4" % prof)
        with open(fp, "wb") as f:
            f.write(b"\0" * 16)
        os.utime(fp, (ts, ts))
        files.setdefault(cam, []).append(fp)
    os.makedirs(os.path.join(root, CAM_EMPTY, "profile_1", d_now), exist_ok=True)
    return root, files


# ------------------------------------------------------------------ mock 推送服务

class MockHandler(BaseHTTPRequestHandler):
    mode = "ok"          # ok | dingtalk_err | http500 | feishu_ok | feishu_rl | feishu_rl_once
    bodies = []
    hits = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        MockHandler.bodies.append(json.loads(self.rfile.read(n).decode("utf-8")))
        MockHandler.hits += 1
        if MockHandler.mode == "http500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        if MockHandler.mode == "dingtalk_err":
            body = json.dumps({"errcode": 310000, "errmsg": "keywords not in content"}).encode()
        elif MockHandler.mode == "feishu_rl":
            body = json.dumps({"code": 11232, "msg": "frequency limited "
                               "psm[lark.oapi.app_platform_runtime]appID[1500]"}).encode()
        elif MockHandler.mode == "feishu_rl_once":
            # 第一次限流、之后成功：验证「限流 → 退避 → 重试成功」整条链路
            if MockHandler.hits == 1:
                body = json.dumps({"code": 11233, "msg": "chat rate limit"}).encode()
            else:
                body = json.dumps({"code": 0, "msg": "success"}).encode()
        elif MockHandler.mode == "feishu_ok":
            body = json.dumps({"code": 0, "msg": "success"}).encode()
        else:
            body = json.dumps({"errcode": 0, "errmsg": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def paused_retry():
    """自检期间把「推送退避等待」压成 0 秒。

    notify 的退避是真睡（生产上 20s + 60s，实测有效），但自检不能真等 80 秒：
    这里替换的是模块级 RETRY_DELAYS 常量（push_one 的默认参数即取自它）。
    重试**逻辑**照常走（次数、频控判定、结果标记都不变），只是不睡。
    """
    old = N.RETRY_DELAYS
    N.RETRY_DELAYS = ()
    try:
        yield
    finally:
        N.RETRY_DELAYS = old


def start_mock():
    srv = HTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/hook" % srv.server_port


# ------------------------------------------------------------------ 测试

def test_config_types():
    raw = {"alert_threshold_minutes": "45", "scan_interval": "abc",
           "alert_silence_minutes": "0",
           "enabled_push_channels": "dingtalk,wechat", "daily_report_time": "7:5",
           "notify_recovery": "停用", "video_exts": "mp4,.TS"}
    cfg = C.normalize(raw)
    eq(cfg["alert_threshold_minutes"], 45, "B1 阈值字符串→int")
    eq(cfg["scan_interval"], 300, "B1 非法 scan_interval → 默认 300")
    eq(cfg["alert_silence_minutes"], 0, "B1 静默 0 保留（0=只报一次）")
    eq(cfg["enabled_push_channels"], ["dingtalk", "wechat"], "B2 渠道列表")
    eq(cfg["daily_report_time"], "07:05", "日报时间规整 HH:MM")
    # v1.0.15：日报开关。旧配置没有这个键 → 必须默认开（升级后行为不变）
    eq(C.normalize({})["daily_report_enabled"], True, "日报开关默认开（旧配置兼容）")
    eq(C.normalize({"daily_report_enabled": "0"})["daily_report_enabled"], False, "开关 0 → 关")
    eq(C.normalize({"daily_report_enabled": "停用"})["daily_report_enabled"], False, "开关「停用」→ 关")
    eq(C.normalize({"daily_report_enabled": ""})["daily_report_enabled"], False, "开关空串 → 关")
    eq(cfg["notify_recovery"], False, "as_bool 识别「停用」")
    eq(cfg["video_exts"], [".mp4", ".ts"], "后缀补点并小写")
    # 关键回归：旧代码 "60" * 60 得到字符串
    ok(cfg["alert_threshold_minutes"] * 60 == 2700, "B1 阈值乘 60 是数字")
    eq(C.normalize({"enabled_push_channels": "bogus"})["enabled_push_channels"], [],
       "未知渠道被过滤")

    # v1.0.8：通道一律自动发现，设置项只剩 camera / profile
    eq(C.normalize({})["channel_layout"], "camera", "默认通道布局 = camera")
    eq(C.normalize({"channel_layout": "profile"})["channel_layout"], "profile", "profile 口径保留")
    eq(C.normalize({"channel_layout": "auto"})["channel_layout"], "camera", "旧值 auto → camera")
    eq(C.normalize({"channel_layout": "flat"})["channel_layout"], "camera", "旧值 flat → camera")
    eq(C.normalize({"channel_layout": "bogus"})["channel_layout"], "camera", "非法值 → camera")
    legacy = C.normalize({"record_root": "/x", "monitor_channels": "a,b",
                          "channel_include": "stream_*"})
    ok("monitor_channels" not in legacy, "废弃键 monitor_channels 被丢弃")
    ok("channel_include" not in legacy, "废弃键 channel_include 被丢弃")


def test_config_io():
    cfg = C.save_config({"record_root": "/tmp/x", "alert_threshold_minutes": "15"})
    ok(C.CONFIG_FILE.is_file(), "配置落 $TRIM_PKGVAR/config.json")
    mode = os.stat(C.CONFIG_FILE).st_mode & 0o777
    eq(mode, 0o600, "配置文件权限 0600（webhook 等同口令）")
    loaded = C.load_config()
    eq(loaded["alert_threshold_minutes"], 15, "重新读取仍是 int")
    eq(loaded["record_root"], "/tmp/x", "录像目录往返一致")
    # 原子写：不应残留临时文件
    leftovers = [f for f in os.listdir(TMP_VAR) if f.startswith(".config.json.")]
    eq(leftovers, [], "原子写无临时文件残留")


def test_channels_and_evaluate():
    root, files = mk_record_tree(os.path.join(TMP_VAR, "record"))
    cfg = C.normalize({"record_root": root, "alert_threshold_minutes": "60",
                       "video_exts": ".mp4"})
    chans = M.resolve_channels(cfg)
    eq([c["channel"] for c in chans], [CAM_OK, CAM_SILENT, CAM_EMPTY],
       "按 <摄像头ID>/profile_N 自动发现通道")
    eq([c["profiles"] for c in chans],
       [["profile_1", "profile_2"], ["profile_1"], ["profile_1"]], "列出各摄像头的 profile")

    ev = M.evaluate(cfg)
    eq(ev["layout"], "camera", "默认摄像头聚合口径")
    st = {c["channel"]: c for c in ev["channels"]}
    eq(st[CAM_OK]["status"], "ok", "刚录过的通道正常")
    eq(st[CAM_SILENT]["status"], "silent", "3 小时没录像 → silent")
    eq(st[CAM_EMPTY]["status"], "no_record", "B4 空 profile → no_record（会告警）")
    eq(st[CAM_OK]["files"], 2, "聚合统计该摄像头两个 profile 的文件")
    ok(st[CAM_SILENT]["idle_min"] >= 170, "闲置分钟按「最新文件时间」算", st[CAM_SILENT]["idle_min"])

    # B5：最新文件被删（EasyNVR 清理）→ 用历史 state 会误报，用 latest 不会
    f2 = files[CAM_SILENT][0]
    os.utime(f2, (time.time() - 30, time.time() - 30))
    ev = {c["channel"]: c for c in M.evaluate(cfg)["channels"]}
    eq(ev[CAM_SILENT]["status"], "ok", "B5 清掉旧文件后重新录制 → 立刻恢复正常（不误报）")

    # profile 口径：主/子码流各算一个通道
    cfg_p = C.normalize({"record_root": root, "video_exts": ".mp4",
                         "channel_layout": "profile"})
    eq([c["channel"] for c in M.resolve_channels(cfg_p)],
       ["%s/profile_1" % CAM_OK, "%s/profile_2" % CAM_OK,
        "%s/profile_1" % CAM_SILENT, "%s/profile_1" % CAM_EMPTY],
       "profile 口径逐个列出")

    # 老式子目录（stream_*）不再被当成通道：EasyNVR 里根本没有这种目录
    old = os.path.join(TMP_VAR, "legacy")
    os.makedirs(os.path.join(old, "stream_1", "20261029"), exist_ok=True)
    open(os.path.join(old, "stream_1", "20261029", "a.mp4"), "wb").write(b"\0" * 8)
    ev_old = M.evaluate(C.normalize({"record_root": old}))
    eq(len(ev_old["channels"]), 0, "stream_* 之类的一层目录不当通道")
    ok("没有识别到任何通道" in ev_old["error"], "无通道时给出明确错误", ev_old["error"])

    # 全局错误：目录不存在 / 没配置
    eq(M.evaluate(C.normalize({"record_root": ""}))["error"], "未配置录像目录", "空目录报错")
    ok(M.evaluate(C.normalize({"record_root": "/nope/zzz"}))["error"].startswith("录像目录不存在"),
       "目录不存在报错")


def mk_easynvr_tree(root):
    """EasyNVR 真实布局：<root>/<摄像头随机ID>/profile_<n>/<YYYYMMDD>/<时间>.mp4"""
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    yday = (now - timedelta(days=1)).strftime("%Y%m%d")
    files = {}
    # 摄像头 A：两个 profile，主码流刚录过，子码流只在昨天录过（未持续录制）
    # 「昨天」的锚点固定到昨天中午，不能写 now - timedelta(days=1, hours=2)：
    # 那是减 26 小时，凌晨 0~2 点跑会落到前天，日报断言就假红（2026-11-08 01:52 实测复现）。
    yday_noon = (now - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    for prof, when, n in (("profile_1", now - timedelta(seconds=90), 3),
                          ("profile_2", yday_noon, 2)):
        d = os.path.join(root, "O27fqV6dYvs3T", prof, when.strftime("%Y%m%d"))
        os.makedirs(d, exist_ok=True)
        for i in range(n):
            fp = os.path.join(d, "%s-%05d.mp4" % (when.strftime("%Y%m%d%H%M%S"), i))
            open(fp, "wb").write(b"\0" * 8)
            ts = when.timestamp() + i
            os.utime(fp, (ts, ts))
            files.setdefault(prof, []).append(fp)
    # 摄像头 B：只有 profile_1，但 3 小时没录像
    d = os.path.join(root, "Xk91mQ2pLzT0", "profile_1", (now - timedelta(hours=3)).strftime("%Y%m%d"))
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, "20261029_old.mp4")
    open(fp, "wb").write(b"\0" * 8)
    ts = (now - timedelta(hours=3)).timestamp()
    os.utime(fp, (ts, ts))
    # 摄像头 C：目录在、profile 在，但一个文件都没有
    os.makedirs(os.path.join(root, "De45adQ0Mn7U", "profile_1", today), exist_ok=True)
    # 干扰项：根目录下的非摄像头目录（比如缩略图）不应被当成通道
    os.makedirs(os.path.join(root, "thumbnails", "20261029"), exist_ok=True)
    _ = yday
    return root, files


def test_easynvr_layout():
    root, _files = mk_easynvr_tree(os.path.join(TMP_VAR, "easynvr"))
    cfg = C.normalize({"record_root": root, "alert_threshold_minutes": "60",
                       "video_exts": ".mp4"})

    # camera 口径：一个摄像头一个通道（默认）
    chans = M.resolve_channels(cfg)
    eq([c["channel"] for c in chans], ["De45adQ0Mn7U", "O27fqV6dYvs3T", "Xk91mQ2pLzT0"],
       "按摄像头聚合，忽略 thumbnails 这类干扰目录")
    a = [c for c in chans if c["channel"] == "O27fqV6dYvs3T"][0]
    eq(a["profiles"], ["profile_1", "profile_2"], "列出该摄像头的 profile")

    ev = {c["channel"]: c for c in M.evaluate(cfg)["channels"]}
    eq(ev["O27fqV6dYvs3T"]["status"], "ok", "主码流刚录过 → 正常")
    ok(ev["O27fqV6dYvs3T"]["files"] == 5, "聚合统计两个 profile 的文件", ev["O27fqV6dYvs3T"]["files"])
    eq(ev["Xk91mQ2pLzT0"]["status"], "silent", "3 小时没录像 → 无新录像")
    eq(ev["De45adQ0Mn7U"]["status"], "no_record", "只有空 profile 目录 → 无录像文件")

    # profile 口径：子码流不录制会一直报"无录像"，所以要能切
    cfg_p = C.normalize({"record_root": root, "alert_threshold_minutes": "60",
                         "video_exts": ".mp4", "channel_layout": "profile"})
    names = [c["channel"] for c in M.resolve_channels(cfg_p)]
    eq(names, ["De45adQ0Mn7U/profile_1", "O27fqV6dYvs3T/profile_1",
               "O27fqV6dYvs3T/profile_2", "Xk91mQ2pLzT0/profile_1"], "profile 口径逐个列出")

    # 别名：显示名替换，告警/日报都用别名
    cfg_a = C.normalize({"record_root": root, "alert_threshold_minutes": "60",
                         "video_exts": ".mp4",
                         "channel_aliases": "O27fqV6dYvs3T=大门\nXk91mQ2pLzT0=后院"})
    ev_a = {c["channel"]: c for c in M.evaluate(cfg_a)["channels"]}
    eq(ev_a["O27fqV6dYvs3T"]["label"], "大门", "别名生效")
    msg = M.build_alert_message(cfg_a, ev_a["Xk91mQ2pLzT0"])
    ok("后院（Xk91mQ2pLzT0）" in msg, "告警用别名+目录名", msg)
    ok("profile：profile_1" in msg, "告警里带 profile 信息", msg)

    # 根目录下的日期目录（如 20261029_event 事件抓拍放在根上）不当通道
    os.makedirs(os.path.join(root, (datetime.now() - timedelta(days=2)).strftime("%Y%m%d")),
                exist_ok=True)
    eq([c["channel"] for c in M.resolve_channels(cfg)],
       ["De45adQ0Mn7U", "O27fqV6dYvs3T", "Xk91mQ2pLzT0"], "根下的日期目录不当通道")

    # 日报按天统计：只数昨天的文件（摄像头 A 的 profile_2 有 2 个）
    now = datetime.now(M.get_tz(cfg))
    text = M.build_daily_report(cfg, now)
    ok("O27fqV6dYvs3T：2 个文件" in text, "日报只统计昨天的录像", text)
    text_a = M.build_daily_report(cfg_a, now)    # 带别名的配置，验日报文案用别名
    ok("大门（O27fqV6dYvs3T）：2 个文件" in text_a, "日报用别名", text_a)


def test_scan_alert_and_silence():
    root, _files = mk_record_tree(os.path.join(TMP_VAR, "record2"))
    cfg = C.normalize({"record_root": root, "alert_threshold_minutes": "60",
                       "alert_silence_minutes": "120", "video_exts": ".mp4",
                       "enabled_push_channels": []})
    state = {}
    res = M.run_scan(cfg, state)
    scopes = sorted(e["scope"] for e in res["events"])
    eq(scopes, [CAM_SILENT, CAM_EMPTY], "首次扫描对两个异常通道告警（合并一条消息）")
    ok(any(e["kind"] == "alert" for e in res["events"]), "事件类型 alert")

    res2 = M.run_scan(cfg, state)
    eq([e["scope"] for e in res2["events"]], [], "静默期内不重复告警")

    # 恢复通知
    f = os.path.join(root, CAM_SILENT, "profile_1")
    deep = os.path.join(f, sorted(os.listdir(f))[0])
    with open(os.path.join(deep, "new.mp4"), "wb") as fh:
        fh.write(b"\0" * 8)
    res3 = M.run_scan(cfg, state)
    rec = [e["scope"] for e in res3["events"] if e["kind"] == "recovery"]
    eq(rec, [CAM_SILENT], "恢复时补发恢复通知")

    # 全局错误也要告警
    bad = C.normalize({"record_root": "/nope/zzz", "enabled_push_channels": []})
    r4 = M.run_scan(bad, {"channels": {}})
    ok(any(e["scope"] == "__global__" for e in r4["events"]), "全局错误（目录不可用）也告警")

    # 状态里存的是「最新文件时间」，不是告警时间
    ok(state["channels"][CAM_OK]["latest"] > 0, "state 记录最新文件 mtime")
    eq(state["channels"][CAM_SILENT]["alerting"], False, "恢复正常后 alerting=False")


def test_daily_schedule():
    cfg = C.normalize({"daily_report_time": "08:00"})
    tz = M.get_tz(cfg)
    base = datetime(2026, 10, 29, 7, 59, tzinfo=tz)
    eq(M.report_due(cfg, {}, base), False, "B3 07:59 不发")
    eq(M.report_due(cfg, {}, base.replace(hour=8, minute=0)), True, "B3 08:00 到点发")
    eq(M.report_due(cfg, {}, base.replace(hour=8, minute=0, second=59)), True, "B3 08:00:59 仍然算到点")
    eq(M.report_due(cfg, {"last_report_day": "2026-10-29"}, base.replace(hour=9)), False,
       "B3 当天已发过 → 不重复")
    eq(M.report_due(cfg, {"last_report_day": "2026-10-28"}, base.replace(hour=15)), True,
       "B3 错过 08:00 后补发（进程重启/NAS 睡眠）")
    # 旧实现是 cur.minute == rm，用 300 秒步长几乎永远命中不了
    hits = 0
    t = datetime(2026, 10, 29, 5, 18, 42, tzinfo=tz)
    for _ in range(288):        # 一天 288 个 5 分钟点
        if t.hour == 8 and t.minute == 0:
            hits += 1
        t += timedelta(seconds=300)
    eq(hits, 0, "B3 复现旧实现：300s 采样一天也不会命中 08:00")

    st = {}
    nxt = M.next_report_dt(cfg, base.replace(hour=9))
    eq(nxt.strftime("%Y-%m-%d %H:%M"), "2026-10-30 08:00", "下次日报时间")

    # v1.0.15：日报开关关掉 → 一条都不发，且 next_report_dt 返回 None（调用方要判空）
    off = C.normalize({"daily_report_enabled": "0", "daily_report_time": "08:00"})
    eq(M.report_due(off, {}, base.replace(hour=9)), False, "日报关闭 → 到点也不发")
    eq(M.next_report_dt(off, base.replace(hour=9)), None, "日报关闭 → next_report_dt 返回 None")
    # 关掉期间不写 last_report_day，重新打开后当天仍能补发（不然会白丢一天）
    eq(M.report_due(cfg, {"last_report_day": "2026-10-28"}, base.replace(hour=15)), True,
       "关过又打开 → 当天到点仍补发")
    # 自定义时间：任意 HH:MM 都按本地时区判定
    late = C.normalize({"daily_report_time": "23:45"})
    eq(M.report_due(late, {}, base.replace(hour=23, minute=44)), False, "自定义 23:45 → 23:44 不发")
    eq(M.report_due(late, {}, base.replace(hour=23, minute=45)), True, "自定义 23:45 → 到点发")


def test_push_plan_and_report_window():
    """v1.0.16：日报统计区间（可跨天）+ 日报推送日 + 告警推送计划。"""
    tz = M.get_tz(C.normalize({}))
    d = C.normalize({})
    eq(d["report_window_from"], "00:00", "默认统计区间起点 00:00")
    eq(d["report_window_to"], "23:59", "默认统计区间终点 23:59")
    eq(d["report_weekdays"], [1, 2, 3, 4, 5, 6, 7], "默认日报推送日 = 每天")
    eq(d["alert_push_weekdays"], [1, 2, 3, 4, 5, 6, 7], "默认告警推送日 = 每天")
    eq(d["alert_window_from"], "00:00", "默认告警时段 00:00")
    eq(d["alert_window_to"], "23:59", "默认告警时段 23:59")
    eq(C.normalize({"report_weekdays": "周一,周三"})["report_weekdays"], [1, 3], "中文星期归一")
    eq(C.normalize({"alert_push_weekdays": "1 3 5"})["alert_push_weekdays"], [1, 3, 5], "空格分隔归一")
    eq(C.normalize({"report_weekdays": ""})["report_weekdays"], [1, 2, 3, 4, 5, 6, 7],
       "空 → 每天（绝不能变成「永不推送」）")
    eq(C.normalize({"report_weekdays": [0, 8, 9]})["report_weekdays"], [1, 2, 3, 4, 5, 6, 7],
       "非法星期 → 每天")
    eq(C.normalize({"report_window_from": "8：00"})["report_window_from"], "08:00", "全角冒号归一")

    # ---- 统计区间
    now = datetime(2026, 10, 29, 9, 0, tzinfo=tz)          # 周四 09:00
    s, e = M.report_window_bounds(C.normalize({}), now)
    eq(datetime.fromtimestamp(s, tz).strftime("%Y-%m-%d %H:%M"), "2026-10-28 00:00",
       "默认区间：昨天 00:00")
    eq(datetime.fromtimestamp(e, tz).strftime("%Y-%m-%d %H:%M"), "2026-10-29 00:00",
       "默认区间：到今天 00:00（23:59 那一分钟不丢）")

    cfg24 = C.normalize({"report_window_from": "08:00", "report_window_to": "08:00"})
    s, e = M.report_window_bounds(cfg24, now)
    eq(datetime.fromtimestamp(s, tz).strftime("%m-%d %H:%M"), "10-28 08:00", "08:00–08:00：起点昨天 08:00")
    eq(datetime.fromtimestamp(e, tz).strftime("%m-%d %H:%M"), "10-29 08:01", "08:00–08:00：终点含 08:00 分钟")
    eq(e - s, 86400 + 60, "08:00–08:00 区间 = 24 小时 + 1 分钟")

    cfg12 = C.normalize({"report_window_from": "18:00", "report_window_to": "06:00"})
    s, e = M.report_window_bounds(cfg12, now)
    eq(datetime.fromtimestamp(s, tz).strftime("%m-%d %H:%M"), "10-28 18:00", "18:00–次日 06:00：起点")
    eq(datetime.fromtimestamp(e, tz).strftime("%m-%d %H:%M"), "10-29 06:01", "18:00–次日 06:00：终点")
    eq(e - s, 12 * 3600 + 60, "跨夜 12 小时区间长度")

    early = datetime(2026, 10, 29, 5, 0, tzinfo=tz)        # 还没到 06:00
    s, e = M.report_window_bounds(cfg12, early)
    eq(datetime.fromtimestamp(s, tz).strftime("%m-%d %H:%M"), "10-27 18:00", "未到终点：起点再退一天")
    eq(datetime.fromtimestamp(e, tz).strftime("%m-%d %H:%M"), "10-28 06:01", "未到终点：终点用昨天 06:00")
    eq(M._report_span_text(cfg12, s, e), "2026-10-27 18:00 — 2026-10-28 06:00", "跨天区间文案")

    # ---- 告警推送计划
    ok(M.alert_push_allowed(C.normalize({}), datetime(2026, 10, 31, 3, 0, tzinfo=tz)),
       "默认每天+全天：凌晨 3 点也允许推送")
    work = C.normalize({"alert_push_weekdays": "1,2,3,4,5",
                        "alert_window_from": "08:00", "alert_window_to": "18:00"})
    ok(M.alert_push_allowed(work, datetime(2026, 10, 28, 10, 0, tzinfo=tz)), "工作日 10:00 → 允许")
    ok(not M.alert_push_allowed(work, datetime(2026, 10, 28, 20, 0, tzinfo=tz)), "工作日 20:00 → 静默")
    ok(not M.alert_push_allowed(work, datetime(2026, 10, 31, 10, 0, tzinfo=tz)), "周六 10:00 → 静默")
    ok(M.alert_push_allowed(work, datetime(2026, 10, 28, 18, 0, tzinfo=tz)), "18:00 端点含在内")
    night = C.normalize({"alert_window_from": "22:00", "alert_window_to": "07:00"})
    ok(M.alert_push_allowed(night, datetime(2026, 10, 28, 23, 30, tzinfo=tz)), "跨夜 23:30 → 允许")
    ok(M.alert_push_allowed(night, datetime(2026, 10, 28, 6, 59, tzinfo=tz)), "跨夜 06:59 → 允许")
    ok(not M.alert_push_allowed(night, datetime(2026, 10, 28, 12, 0, tzinfo=tz)), "跨夜 12:00 → 静默")
    same = C.normalize({"alert_window_from": "08:00", "alert_window_to": "08:00"})
    ok(M.alert_push_allowed(same, datetime(2026, 10, 28, 15, 0, tzinfo=tz)), "起止相同 = 全天")

    eq(M.push_plan_text(work), "工作日 08:00–18:00", "推送计划文案")
    eq(M.push_plan_text(night), "每天 22:00–07:00（跨夜）", "跨夜文案带标注")
    eq(M.push_plan_text(C.normalize({})), "每天 全天", "全天文案")

    # ---- 日报推送日
    weekend_off = C.normalize({"daily_report_time": "08:00", "report_weekdays": "1,2,3,4,5"})
    sat = datetime(2026, 10, 31, 9, 0, tzinfo=tz)          # 周六
    eq(M.report_due(weekend_off, {}, sat), False, "非推送日不发日报")
    eq(M.next_report_dt(weekend_off, datetime(2026, 10, 30, 18, 0, tzinfo=tz)).strftime("%Y-%m-%d %H:%M"),
       "2026-11-02 08:00", "周五 18:00 看到的下次日报 = 下周一 08:00")
    eq(M.next_report_dt(weekend_off, sat).strftime("%Y-%m-%d %H:%M"), "2026-11-02 08:00",
       "周六看到的下次日报 = 周一 08:00")
    eq(M.next_report_dt(C.normalize({"daily_report_time": "08:00"}), sat).strftime("%Y-%m-%d %H:%M"),
       "2026-11-01 08:00", "每天推送时，周六看到的就是周日 08:00")

    # ---- run_scan 闸门：时段外只刷新检测结果，不推进告警状态
    quiet_cfg = C.normalize({"record_root": os.path.join(TMP_VAR, "no-such-dir"),
                             "alert_push_weekdays": "1,2,3,4,5",
                             "alert_window_from": "08:00", "alert_window_to": "18:00"})
    orig_now = M.now_dt
    try:
        M.now_dt = lambda cfg: sat                            # 周六 09:00（非推送日）
        st = {}
        res = M.run_scan(quiet_cfg, st)
        ok(res.get("quiet") is True, "时段外：run_scan 标记 quiet")
        eq(res.get("events"), [], "时段外：不产生任何推送事件")
        eq(st.get("channels"), {}, "时段外：通道告警状态不推进（夜间断流不会被静默吃掉）")
        ok(st.get("quiet_since"), "时段外：记录 quiet_since 只写一次日志")

        M.now_dt = lambda cfg: datetime(2026, 10, 28, 10, 0, tzinfo=tz)   # 周三 10:00
        st2 = {}
        res2 = M.run_scan(quiet_cfg, st2)
        ok(not res2.get("quiet"), "时段内：正常判定")
        eq(len(res2.get("events") or []), 1, "时段内：目录不存在 → 照样产生告警事件")
        ok(not st2.get("quiet_since"), "时段内：清掉 quiet_since")
    finally:
        M.now_dt = orig_now


def test_report_text():
    root, _files = mk_record_tree(os.path.join(TMP_VAR, "record3"))
    cfg = C.normalize({"record_root": root, "video_exts": ".mp4",
                       "alert_threshold_minutes": "60"})
    now = datetime.now(M.get_tz(cfg))
    # 把所有录像文件的时间改到昨天中午，验证「按天统计」
    y = (now - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            os.utime(os.path.join(dirpath, fn), (y.timestamp(), y.timestamp()))
    text = M.build_daily_report(cfg, now)
    ok("录像日报" in text, "日报标题")
    ok("%s：2 个文件" % CAM_OK in text, "日报含通道计数（聚合两个 profile）", text)
    ok("%s：1 个文件" % CAM_SILENT in text, "日报逐通道计数", text)
    ok("%s：0 个文件" % CAM_EMPTY in text, "昨天没录的通道计 0", text)
    ok("合计：3 个录像文件" in text, "日报含合计", text)
    ok("异常通道" in text, "日报提示异常通道")


def test_push(webhook):
    cfg = C.normalize({"enabled_push_channels": "dingtalk",
                       "dingtalk_webhook": webhook})
    MockHandler.mode, MockHandler.bodies = "ok", []
    r = N.push_all(cfg, "hello")
    eq(r[0]["ok"], True, "B6 正常响应判定成功")
    ok(MockHandler.bodies and MockHandler.bodies[0]["text"]["content"] == "hello",
       "B6 钉钉 payload 正确")

    MockHandler.mode = "dingtalk_err"
    r = N.push_all(cfg, "hello")
    eq(r[0]["ok"], False, "B6 HTTP 200 + errcode 310000 → 判失败（旧代码会写「已推送」）")
    ok("310000" in r[0]["detail"], "B6 失败原因带回 errcode", r[0]["detail"])

    MockHandler.mode = "http500"
    r = N.push_all(cfg, "hello")
    eq(r[0]["ok"], False, "B6 HTTP 500 → 判失败")

    r = N.push_all(cfg, "hello", channels=["wechat"])
    eq(r[0]["ok"], False, "未配置 webhook → 失败而不是静默")

    MockHandler.mode = "feishu_ok"
    cfg2 = C.normalize({"enabled_push_channels": "feishu", "feishu_webhook": webhook})
    r = N.push_all(cfg2, "hi")
    eq(r[0]["ok"], True, "飞书 code=0 判定成功")

    # 钉钉加签：与官方算法逐位对齐
    import base64
    import hashlib
    import hmac
    import urllib.parse
    url = N.dingtalk_signed_url("https://x/hook?access_token=t", "SECabc")
    qs = urllib.parse.parse_qs(url.split("?", 1)[1])
    ts = qs["timestamp"][0]
    want = base64.b64encode(
        hmac.new(b"SECabc", ("%s\nSECabc" % ts).encode(), hashlib.sha256).digest()).decode()
    eq(qs["sign"][0], want, "B6 钉钉加签 sign 与官方算法一致")
    ok("timestamp=" in url and "sign=" in url, "B6 加签参数已拼进 URL")
    eq(N.dingtalk_signed_url("https://x/hook", ""), "https://x/hook", "无密钥时不加签")


def test_online_streak():
    root = os.path.join(TMP_VAR, "online")
    now = time.time()

    def touch(fp, ts):
        open(fp, "wb").write(b"\0" * 8)
        os.utime(fp, (ts, ts))

    def cam_day(cam):
        p = os.path.join(root, cam, "profile_1", datetime.now().strftime("%Y%m%d"))
        os.makedirs(p, exist_ok=True)
        return p

    exts = {".mp4"}
    cfg = C.normalize({"record_root": root, "alert_threshold_minutes": "60",
                       "video_exts": ".mp4"})

    # CamA：最近 30 分钟连续，再往前断了 2.5 小时
    d = cam_day("CamA")
    for i in range(31):
        touch(os.path.join(d, "new%02d.mp4" % i), now - (30 - i) * 60)
    for i in range(3):
        touch(os.path.join(d, "old%02d.mp4" % i), now - 3 * 3600 + i * 60)
    secs = M.online_seconds(os.path.join(root, "CamA"), exts, int(now))
    ok(29 * 60 <= secs <= 31 * 60, "在线时长 ≈ 30 分钟", secs)
    ev = {c["channel"]: c for c in M.evaluate(cfg)["channels"]}
    eq(ev["CamA"]["online_str"], "30 分钟", "ok 通道显示在线时长")

    # CamB：只有 5 小时前的旧文件 → 断流 → —
    d2 = os.path.join(root, "CamB", "profile_1",
                      (datetime.now() - timedelta(hours=5)).strftime("%Y%m%d"))
    os.makedirs(d2, exist_ok=True)
    touch(os.path.join(d2, "a.mp4"), now - 5 * 3600)
    ev = {c["channel"]: c for c in M.evaluate(cfg)["channels"]}
    eq(ev["CamB"]["status"], "silent", "CamB 断流")
    eq(ev["CamB"]["online_str"], "—", "断流通道在线时长显示 —")

    # CamC：连续 3 小时
    d3 = cam_day("CamC")
    for i in range(181):
        touch(os.path.join(d3, "c%03d.mp4" % i), now - (180 - i) * 60)
    secs = M.online_seconds(os.path.join(root, "CamC"), exts, int(now))
    ok(179 * 60 <= secs <= 181 * 60, "连续 3 小时", secs)

    # CamD：camera 布局聚合两个 profile（单 profile 只有 4 分钟）
    now2 = time.time()
    for prof in ("profile_1", "profile_2"):
        dp = os.path.join(root, "CamD", prof, datetime.now().strftime("%Y%m%d"))
        os.makedirs(dp, exist_ok=True)
        for i in range(3):
            touch(os.path.join(dp, "x%d.mp4" % i),
                  now2 - (5 - i * 2) * 60 - (0 if prof == "profile_1" else 60))
    secs = M.online_seconds(os.path.join(root, "CamD"), exts, int(now2))
    ok(5 * 60 <= secs <= 7 * 60, "camera 布局聚合两 profile 算在线时长", secs)

    # CamE：连续 5 天跨天不断 → 不再封顶，显示真实天数（>100 小时 → 天/时）
    now3 = time.time()
    base = int(now3) - 5 * 86400
    for i in range(5 * 24 * 60):
        ts = base + i * 60
        day = datetime.fromtimestamp(ts).strftime("%Y%m%d")
        dp = os.path.join(root, "CamE", "profile_1", day)
        os.makedirs(dp, exist_ok=True)
        touch(os.path.join(dp, "e%06d.mp4" % i), ts)
    secs = M.online_seconds(os.path.join(root, "CamE"), exts, int(now3))
    ok(4 * 86400 < secs <= 5 * 86400 + 120, "连续 5 天扫到底不封顶，按真实时长", secs)
    ev = {c["channel"]: c for c in M.evaluate(cfg)["channels"]}
    # 5 天 × 每分钟一个，最新一个就在「现在」，所以实际是 4 天 23 时多 → 显示 4 天 23 时
    eq(ev["CamE"]["online_str"], "4 天 23 时", ">100 小时 → 天/时格式（真实时长，不封顶）")

    # fmt_duration 各档位（覆盖分钟/小时/天/月，无年份）
    eq(M.fmt_duration(30), "不足 1 分钟", "不足 1 分钟")
    eq(M.fmt_duration(3600), "1 小时", "整小时")
    eq(M.fmt_duration(5400), "1 小时 30 分", "小时+分")
    eq(M.fmt_duration(99 * 3600), "99 小时", "99 小时仍用小时档")
    eq(M.fmt_duration(100 * 3600), "4 天 4 时", "100 小时 → 天/时")
    eq(M.fmt_duration(5 * 86400), "5 天", "整天")
    eq(M.fmt_duration(99 * 86400), "99 天", "99 天仍用天档")
    eq(M.fmt_duration(100 * 86400), "3 月 10 天", "100 天 → 月/天")
    eq(M.fmt_duration(180 * 86400), "6 月", "180 天 → 6 个月（最长保留期，无年份）")


def test_normalize_keeps_secret():
    # 设置页「留空=不修改」：apply_wizard 的语义在 config.normalize 里也要成立
    C.save_config({"dingtalk_webhook": "https://a/b", "dingtalk_secret": "SEC1"})
    raw = json.loads(C.CONFIG_FILE.read_text(encoding="utf-8"))
    raw.pop("dingtalk_secret")
    C.save_config(raw)
    eq(C.load_config()["dingtalk_webhook"], "https://a/b", "未提供的键保留默认/原值")


def test_stats():
    """v1.0.18：统计页的区间/在线时长/数量口径。

    统计不落采样库，全靠录像 mtime 反推，所以每个数字都必须能对上手工核算的秒数——
    口径一旦算错，页面上的「在线率」就是假的，比没有更糟。
    """
    import stats as S

    print("-- 统计")
    # ① 段口径：把 mtime 切成「连续录像段」，段尾补一个片段时长
    b = 1_700_000_000
    segs = S.segments([b + 60 * i for i in range(10)])
    eq(len(segs), 1, "统计：10 个 1 分钟切片合成 1 段")
    eq(segs[0][1] - segs[0][0], 600, "统计：段时长 = 跨度 9 分钟 + 1 个片段 = 10 分钟")
    segs = S.segments([b])
    eq(segs[0][1] - segs[0][0], 60, "统计：孤立单文件算 1 个片段（不是 0）")
    segs = S.segments([b, b + 60, b + 4000])
    eq(len(segs), 2, "统计：间隔超过 5 分钟切成两段")
    eq(segs[0][1] - b, 120, "统计：两段 1 分钟录像 = 120 秒，不把断流时间算进在线")

    # ② 时间窗口（时区对齐是整小时，所以直接对 3600 取模）
    cfg0 = C.normalize({"timezone": "Asia/Shanghai"})
    now = M.now_dt(cfg0)
    for key, ndays, single in (("today", 1, True), ("yesterday", 1, True),
                               ("7d", 7, False), ("30d", 30, False)):
        w = S.window_of(cfg0, key, now=now)
        eq(len(w["day_labels"]), ndays, "统计窗口 %s 覆盖天数" % key)
        eq(w["single_day"], single, "统计窗口 %s 单日标记" % key)
        ok(w["end"] > w["start"], "统计窗口 %s 起止有序" % key)
    eq(S.window_of(cfg0, "7d", now=now)["start"] % 3600, 0, "统计窗口起点对齐整点")
    eq(S.window_of(cfg0, "bogus", now=now)["key"], "7d", "非法 range 回落近 7 天")

    # ③ 真实目录树上的聚合：0–2 点连录 → 断 1 小时 → 3–4 点再录
    root = tempfile.mkdtemp(prefix="erm-stats-")
    try:
        base_day = (now - timedelta(days=3)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        ts0 = int(base_day.timestamp())
        cam = "STATCAM00001"
        dd = os.path.join(root, cam, "profile_1", base_day.strftime("%Y%m%d"))
        os.makedirs(dd)
        marks = ([ts0 + 60 * i for i in range(120)] +
                 [ts0 + 3600 * 3 + 60 * i for i in range(60)])
        for t in marks:
            fp = os.path.join(dd, "%d.mp4" % t)
            with open(fp, "wb") as f:
                f.write(b"\0")
            os.utime(fp, (t, t))

        S.clear_cache()
        cfg = C.normalize({"record_root": root, "timezone": "Asia/Shanghai"})
        r = S.build(cfg, "7d", now=now)
        eq(len(r["channels"]), 1, "统计：识别到 1 个通道")
        ch = r["channels"][0]
        eq(ch["files"], 180, "统计：窗口内文件数")
        eq(ch["online_sec"], 10800, "统计：在线时长 = 2 小时 + 1 小时")
        eq(ch["online_pct"], round(100.0 * 10800 / (r["span_sec"] or 1), 1),
           "统计：通道在线率 = 在线 / 窗口")
        eq(r["summary"]["files"], 180, "统计：汇总文件数")
        eq(r["summary"]["online_sec"], 10800, "统计：单通道时汇总 = 该通道（不是它的 2 倍）")
        eq(len(r["bars"]), 7, "统计：7 天视图 7 根柱")
        eq(r["heat"]["kind"], "day", "统计：多日视图热力图按天")
        eq(len(r["heat"]["rows"]), 7, "统计：热力图 7 行")
        eq(len(r["heat"]["cols"]), 24, "统计：热力图 24 列")
        eq(sum(b["files"] for b in r["bars"]), 180, "统计：柱状图文件数与总数一致")
        eq(sum(b["files"] for b in r["bars"]), 180, "统计：柱状图文件数按同一套桶归集")

        # 断流：最后一个文件是 1:59、下一个是 3:00 → 61 分钟（按文件到文件算，不补片段）
        one_hour = [g for g in r["gaps"] if g["minutes"] == 61]
        eq(len(one_hour), 1, "统计：识别出那段 1 小时断流（61 分钟）")
        eq(one_hour[0]["from"], ts0 + 60 * 119, "统计：断流起点 = 段尾最后一个文件")
        eq(one_hour[0]["to"], ts0 + 3600 * 3, "统计：断流终点 = 下一段第一个文件")
        ok("小时" in one_hour[0]["dur_str"], "统计：断流时长带可读文案",
           one_hour[0]["dur_str"])
        eq(r["gap_total"], len(r["gaps"]), "统计：gap_total 与实际条数一致（未截断时）")

        # 单日视图：把"现在"挪到当天 12:00
        r2 = S.build(cfg, "today", now=base_day.replace(hour=12))
        eq(r2["single_day"], True, "统计：today 是单日视图")
        eq(len(r2["bars"]), 12, "统计：12:00 时单日视图 12 个小时柱（00–11 点）")
        eq(r2["heat"]["kind"], "channel", "统计：单日视图热力图按通道")
        eq(len(r2["heat"]["rows"]), 1, "统计：单日视图热力图行数 = 通道数")
        eq(r2["channels"][0]["online_sec"], 10800, "统计：单日视图在线时长一致")

        # 别名生效
        cfg_alias = C.normalize({"record_root": root,
                                 "channel_aliases": {cam: "测试通道"}})
        r3 = S.build(cfg_alias, "7d", now=now)
        eq(r3["channels"][0]["label"], "测试通道", "统计：通道显示别名")

        # 缓存命中后结果必须一致（缓存 key 漏了参数就会串数据）
        r4 = S.build(cfg, "7d", now=now)
        eq(r4["summary"]["files"], 180, "统计：走缓存后结果一致")

        # ④ 错误路径
        r5 = S.build(C.normalize({"record_root": os.path.join(root, "nope")}), "7d")
        ok(r5["error"], "统计：目录不存在时给出 error")
        r6 = S.build(C.normalize({}), "7d")
        ok(r6["error"], "统计：未配置录像目录时给出 error")
        eq(r6["channels"], [], "统计：无数据时通道列表为空（前端据此显示 —）")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_rate_limit_retry(webhook):
    """v1.0.14：飞书 11232/11233 限流要退避重试，且只对限流重试。

    退化前（v1.0.13）行为：限流一次就彻底失败，而 run_daily 还写「已发送」，
    导致 2026-11-01 / 11-02 两天的 09:00 日报静默丢失。
    """
    cfg = C.normalize({"enabled_push_channels": "feishu", "feishu_webhook": webhook})

    # ① 限流 → 退避重试 → 成功（重试延迟给 0.01 秒，让自检跑得快）
    MockHandler.mode, MockHandler.bodies, MockHandler.hits = "feishu_rl_once", [], 0
    r = N.push_one(cfg, "feishu", "hi", retry_delays=(0.01, 0.01))
    ok(r["ok"], "限流后重试成功（11233 → code=0）", r.get("detail"))             
    eq(MockHandler.hits, 2, "限流重试：共发了 2 次")
    eq(r.get("attempts"), 2, "结果里标注尝试次数")
    ok(r.get("retried") is True, "结果里标注发生过重试")
    ok("重试" in r["detail"], "成功详情说明重试过", r["detail"])

    # ② 持续限流 → 用完所有退避次数后如实失败
    MockHandler.mode, MockHandler.hits = "feishu_rl", 0
    r = N.push_one(cfg, "feishu", "hi", retry_delays=(0.01, 0.01))
    eq(r["ok"], False, "持续限流 → 最终失败（不假装成功）")
    eq(MockHandler.hits, 3, "默认 1 次 + 2 次重试 = 3 次尝试")
    ok(r.get("rate_limited") is True, "标记为限流失败，便于上层决定补发")
    ok("11232" in r["detail"], "失败详情带回平台错误码", r["detail"])

    # ③ 非限流错误（钉钉关键词 310000）绝不重试——否则配置问题会被伪装成偶发
    MockHandler.mode, MockHandler.hits = "dingtalk_err", 0
    cfg_dt = C.normalize({"enabled_push_channels": "dingtalk", "dingtalk_webhook": webhook})
    r = N.push_one(cfg_dt, "dingtalk", "hi", retry_delays=(0.01, 0.01))
    eq(r["ok"], False, "钉钉 310000 判失败")
    eq(MockHandler.hits, 1, "配置类错误只发 1 次，不浪费重试")

    # ④ 限流判定表：三个平台都认，且不误伤配置错误
    ok(N._is_rate_limited("feishu", 11232, ""), "飞书 11232 算限流")
    ok(N._is_rate_limited("feishu", 11233, ""), "飞书 11233 算限流")
    ok(N._is_rate_limited("dingtalk", 130101, ""), "钉钉 130101 算限流")
    ok(N._is_rate_limited("wechat", 45009, ""), "企微 45009 算限流")
    ok(not N._is_rate_limited("feishu", 19001, "param error"), "飞书参数错 19001 不算限流")
    ok(not N._is_rate_limited("dingtalk", 310000, "keywords not in content"),
       "钉钉关键词错 310000 不算限流")
    ok(N._is_rate_limited("feishu", None, "xxx frequency limited yyy"),
       "错误码没命中时按文本兜底识别限流")

    # ⑤ 抖动：多通道同时撞限流时不要同一秒一起重发
    vals = [N._jitter(60) for _ in range(20)]
    ok(all(48 <= v <= 72 for v in vals), "抖动在 ±20% 内", vals[:3])
    ok(len(set(round(v, 3) for v in vals)) > 1, "抖动确实有随机性")
    eq(N._jitter(0), 0, "0 秒不抖动")

    # ⑥ 多通道聚合：all_ok / failed_channels 是补发决策的依据
    MockHandler.mode = "feishu_ok"
    ok(N.all_ok([{"channel": "feishu", "ok": True}]), "all_ok 全成功")
    ok(not N.all_ok([{"channel": "feishu", "ok": True}, {"channel": "email", "ok": False}]),
       "all_ok 有失败即 False")
    eq(N.failed_channels([{"channel": "-", "ok": False}]), [],
       "「未启用任何渠道」不算可重试失败")
    eq(N.failed_channels([{"channel": "feishu", "ok": False}]), ["feishu"], "failed_channels 取失败渠道")


def test_daily_retry(webhook):
    """v1.0.14：日报推送失败 → 不写 last_report_day、只落 report_retry，到点补发。"""
    tz = M.get_tz(C.normalize({}))
    day = datetime(2026, 11, 2, 9, 0, 5, tzinfo=tz)
    today = day.strftime("%Y-%m-%d")
    cfg = C.normalize({"daily_report_time": "09:00", "enabled_push_channels": "feishu",
                       "feishu_webhook": webhook, "record_root": TMP_VAR})

    # ① 推送失败：不得谎报「已发送」
    MockHandler.mode, MockHandler.hits = "feishu_rl", 0
    state = {}
    with paused_retry():
        res = M.run_daily(cfg, state, now=day)
    eq(res["ok"], False, "日报失败时 ok=False")
    ok("last_report_day" not in state, "推送失败**不写** last_report_day（v1.0.13 会写，导致丢报）")
    retry = state.get("report_retry") or {}
    eq(retry.get("day"), today, "登记了当天的补发任务")
    eq(retry.get("retries"), 1, "补发计数 = 1")
    eq(retry.get("failed"), ["feishu"], "记下失败渠道，便于页面/日志展示")
    later = day + timedelta(seconds=M.DAILY_RETRY_DELAY - 1)
    eq(M.report_due(cfg, state, now=later), False, "补发未到点不发（避免 15 秒一次硬撞限流）")
    eq(M.report_due(cfg, state, now=day + timedelta(seconds=M.DAILY_RETRY_DELAY + 1)), True,
       "补发到点 → 放行")

    # ② 补发成功：清掉补发状态、写 last_report_day
    MockHandler.mode = "feishu_ok"
    with paused_retry():
        res = M.run_daily(cfg, state, now=day + timedelta(seconds=M.DAILY_RETRY_DELAY + 2))
    eq(res["ok"], True, "补发成功")
    eq(state.get("last_report_day"), today, "补发成功后写 last_report_day")
    ok("report_retry" not in state, "补发成功清掉补发状态")
    eq(M.report_due(cfg, state, now=day + timedelta(hours=5)), False, "当天不再重复发")

    # ③ 补发次数上限：连续失败 DAILY_RETRY_MAX 次后放弃，避免永久性配置错误刷日志
    MockHandler.mode, MockHandler.hits = "feishu_rl", 0
    state = {}
    t = day
    # 只剩「用完所有补发次数」这一种失败结局，HTTP 层只打桩 1 次尝试，
    # 否则每次 run_daily 都要跑满 3 次尝试 × (N+1) 轮，自检白白多花一分多钟。
    saved_delays = N.RETRY_DELAYS
    N.RETRY_DELAYS = ()
    try:
        for i in range(M.DAILY_RETRY_MAX + 1):
            M.run_daily(cfg, state, now=t)
            t = t + timedelta(seconds=M.DAILY_RETRY_DELAY + 1)
    finally:
        N.RETRY_DELAYS = saved_delays
    eq(state.get("last_report_day"), today,
       "补发 %d 次仍失败 → 写 last_report_day 停止重试" % M.DAILY_RETRY_MAX)
    ok("report_retry" not in state, "放弃后清掉补发状态")
    eq(M.report_due(cfg, state, now=t + timedelta(hours=1)), False, "放弃后当天不再发")

    # ④ 手动发送失败：不能把当天自动日报吞掉
    MockHandler.mode, MockHandler.hits = "feishu_rl", 0
    state = {}
    with paused_retry():
        res = M.run_daily(cfg, state, now=day, manual=True)
    eq(res["ok"], False, "手动发送失败如实返回 ok=False")
    ok("last_report_day" not in state, "手动失败不写 last_report_day（到点自动日报照发）")
    ok("report_retry" not in state, "手动失败不登记补发（用户就在页面盯着）")

    # ⑤ 没启用任何渠道：算「已处理」，避免每 15 秒重试一整晚（关掉重试，省时间）
    st2 = {}
    N.RETRY_DELAYS = ()
    try:
        res = M.run_daily(C.normalize({"daily_report_time": "09:00", "record_root": TMP_VAR}),
                          st2, now=day)
    finally:
        N.RETRY_DELAYS = saved_delays
    eq(res["ok"], False, "未启用渠道 → 结果如实为失败")
    eq(st2.get("last_report_day"), today, "未启用渠道时写 last_report_day（无可重试的渠道）")


def test_alert_retry(webhook):
    """v1.0.15：告警推送失败 → 回滚 last_alert_time + 退避补发，别让静默期吞掉告警。"""
    root, _files = mk_record_tree(os.path.join(TMP_VAR, "record_retry"))
    cfg = C.normalize({"record_root": root, "video_exts": ".mp4",
                       "alert_threshold_minutes": "60", "alert_silence_minutes": "120",
                       "enabled_push_channels": "feishu", "feishu_webhook": webhook})

    # ① 首次告警但推送失败（限流）
    MockHandler.mode, MockHandler.hits = "feishu_rl", 0
    state = {"channels": {}}
    with paused_retry():
        res = M.run_scan(cfg, state)
    ok(any(e["kind"] == "alert" for e in res["events"]), "有通道异常 → 产生告警事件")
    alerts = [e["scope"] for e in res["events"] if e["kind"] == "alert"]
    st = state["channels"][alerts[0]]
    ok("last_alert_time" not in st or st["last_alert_time"] < time.time() - 10,
       "推送失败 → last_alert_time 已回滚（否则静默 120 分钟都不会再发）")
    eq(st.get("alert_fails"), 1, "失败计数 = 1")
    ok(st.get("next_alert_retry", 0) > time.time(), "登记了下一次重试时间")
    gap = st.get("next_alert_retry", 0) - int(time.time())
    ok(abs(gap - M.ALERT_RETRY_BACKOFF[0]) <= 2,
       "首次失败约 %d 秒后重试（实测差 %d 秒）" % (M.ALERT_RETRY_BACKOFF[0], gap))
    ok(M.pending_alert_retries(state), "pending_alert_retries 能报出待补发时间（主循环据此提前唤醒）")

    # ② 退避没到点：不重发（免得每轮都撞平台限流）
    st["next_alert_retry"] = int(time.time()) + 300
    MockHandler.hits = 0
    with paused_retry():
        res2 = M.run_scan(cfg, state)
    eq([e for e in res2["events"] if e["kind"] == "alert"], [], "退避未到点不重发")
    eq(MockHandler.hits, 0, "退避未到点时一次请求都没发")

    # ③ 到点重发且成功 → 补发状态清空
    st["next_alert_retry"] = int(time.time()) - 1
    MockHandler.mode, MockHandler.hits = "feishu_ok", 0
    with paused_retry():
        res3 = M.run_scan(cfg, state)
    ok(any(e["kind"] == "alert" for e in res3["events"]), "退避到点 → 重发告警")
    eq(MockHandler.hits, 1, "重发一次请求")
    st = state["channels"][alerts[0]]
    ok(not st.get("next_alert_retry"), "成功后退订补发")
    ok(not st.get("alert_fails"), "成功后清掉失败计数")
    ok(st.get("last_alert_time", 0) > 0, "成功后 last_alert_time 正常推进（进入静默期）")

    # ④ 静默期内不再重复发（原有行为不能破坏）
    MockHandler.hits = 0
    with paused_retry():
        res4 = M.run_scan(cfg, state)
    eq([e for e in res4["events"] if e["kind"] == "alert"], [], "静默期内不重复告警")
    eq(MockHandler.hits, 0, "静默期内不发请求")


def test_ui_shell():
    """v1.0.19：顶部导航 / 亮暗双主题 / 两页结构。纯静态断言，防止改样式时把骨架改坏。"""
    html = open(os.path.join(APP, "www", "index.html"), encoding="utf-8").read()

    # ① 主题：首帧前定主题，避免深色用户看到白闪
    ok("data-theme" in html, "index.html 有主题属性")
    ok("localStorage.getItem('erm.theme')" in html, "主题偏好存 localStorage")
    ok("prefers-color-scheme" in html, "没选过时跟随系统深色偏好")

    # ② 两页导航
    ok('id="tabs"' in html, "有顶部导航容器")
    eq(html.count('data-page="overview"'), 1, "导航项「概览」唯一")
    eq(html.count('data-page="config"'), 1, "导航项「配置」唯一")
    ok('id="page-overview"' in html and 'id="page-config"' in html, "两个页面容器都在")
    eq(html.count('id="tabInd"'), 1, "导航指示条唯一")

    i_ov = html.index('id="page-overview"')
    i_stat = html.index('id="statCard"')
    i_cf = html.index('id="page-config"')
    i_form = html.index('id="cfgForm"')
    ok(i_ov < i_stat < i_cf, "图表卡片在概览页内")
    ok(i_cf < i_form, "配置表单在配置页内")
    ok(i_stat < i_form, "概览排在配置之前（首页即图表）")

    # ③ 主题变量两套齐全：漏一个就会在某个主题下露出硬编码颜色
    for v in ("--bg", "--card", "--ink", "--muted", "--surface", "--track",
              "--heat-0", "--heat-5", "--ind-bg", "--pre-bg"):
        eq(html.count(v + ":"), 2, "变量 %s 亮/暗各定义一次" % v)

    ok("@media (prefers-reduced-motion: reduce)" in html, "尊重系统「减少动态效果」")


def main():
    print("== erm 自检开始（数据目录 %s）" % TMP_VAR)
    srv, webhook = start_mock()
    try:
        for fn in (test_config_types, test_config_io, test_channels_and_evaluate,
                   test_easynvr_layout, test_online_streak,
                   test_scan_alert_and_silence, test_daily_schedule, test_report_text,
                   test_push_plan_and_report_window, test_stats,
                   test_normalize_keeps_secret, test_ui_shell):
            fn()
        test_push(webhook)
        test_rate_limit_retry(webhook)
        test_daily_retry(webhook)
        test_alert_retry(webhook)
    finally:
        srv.shutdown()
        shutil.rmtree(TMP_VAR, ignore_errors=True)

    if FAILS:
        print("\n❌ 失败 %d / %d：" % (len(FAILS), CHECKS[0]))
        for f in FAILS:
            print("   ·", f)
        return 1
    print("\n✅ 全部通过：%d 项断言  SELFTEST_ERM_OK" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
