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
    mode = "ok"          # ok | dingtalk_err | http500 | feishu_ok
    bodies = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        MockHandler.bodies.append(json.loads(self.rfile.read(n).decode("utf-8")))
        if MockHandler.mode == "http500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        if MockHandler.mode == "dingtalk_err":
            body = json.dumps({"errcode": 310000, "errmsg": "keywords not in content"}).encode()
        elif MockHandler.mode == "feishu_ok":
            body = json.dumps({"code": 0, "msg": "success"}).encode()
        else:
            body = json.dumps({"errcode": 0, "errmsg": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
    for prof, when, n in (("profile_1", now - timedelta(seconds=90), 3),
                          ("profile_2", now - timedelta(days=1, hours=2), 2)):
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
    d = os.path.join(root, "CamA", "profile_1", datetime.now().strftime("%Y%m%d"))
    os.makedirs(d, exist_ok=True)

    def touch(fp, ts):
        open(fp, "wb").write(b"\0" * 8)
        os.utime(fp, (ts, ts))

    # 最近 30 分钟每分钟一个文件（连续在线）；再往前是 3 小时前的旧文件（中间断了 2.5 小时）
    for i in range(31):
        touch(os.path.join(d, "new%02d.mp4" % i), now - (30 - i) * 60)
    for i in range(3):
        touch(os.path.join(d, "old%02d.mp4" % i), now - 3 * 3600 + i * 60)

    exts = {".mp4"}
    secs, capped = M.online_streak(os.path.join(root, "CamA"), exts, int(now))
    eq(capped, False, "窗口内找到了断点")
    ok(29 * 60 <= secs <= 31 * 60, "在线时长 ≈ 30 分钟", secs)
    eq(M.fmt_duration(secs), "30 分钟", "格式化：分钟")

    cfg = C.normalize({"record_root": root, "alert_threshold_minutes": "60",
                       "video_exts": ".mp4"})
    ev = {c["channel"]: c for c in M.evaluate(cfg)["channels"]}
    eq(ev["CamA"]["online_str"], "30 分钟", "ok 通道显示在线时长")

    # 断流中的通道不在线 → —
    d2 = os.path.join(root, "CamB", "profile_1",
                      (datetime.now() - timedelta(hours=5)).strftime("%Y%m%d"))
    os.makedirs(d2, exist_ok=True)
    touch(os.path.join(d2, "a.mp4"), now - 5 * 3600)
    ev = {c["channel"]: c for c in M.evaluate(cfg)["channels"]}
    eq(ev["CamB"]["status"], "silent", "CamB 断流")
    eq(ev["CamB"]["online_str"], "—", "断流通道在线时长显示 —")

    # 窗口内一直连续且窗口被截断（更早还有数据）→ capped → ">N 天"
    d3 = os.path.join(root, "CamC", "profile_1", datetime.now().strftime("%Y%m%d"))
    os.makedirs(d3, exist_ok=True)
    for i in range(181):                       # 最近 3 小时每分钟一个，同一天内
        touch(os.path.join(d3, "c%03d.mp4" % i), now - (180 - i) * 60)
    secs, capped = M.online_streak(os.path.join(root, "CamC"), exts, int(now))
    eq(capped, False, "只有 1 天数据、窗口没截断 → 不封顶，显示真实时长")
    ok(179 * 60 <= secs <= 181 * 60, "CamC 在线时长 ≈ 3 小时", secs)
    # 再补一个昨天的日期目录：这时 2 个目录，窗口=1 天会扫「今天+昨天」，正好扫完 → 不封顶
    dy = os.path.join(root, "CamC", "profile_1",
                      (datetime.now() - timedelta(days=1)).strftime("%Y%m%d"))
    os.makedirs(dy, exist_ok=True)
    touch(os.path.join(dy, "y.mp4"), now - 86400)
    old_days = M.ONLINE_STREAK_DAYS
    try:
        # 现在 2 个目录，窗口=1 天 → 扫「今天+昨天」正好扫完 → 不封顶
        M.ONLINE_STREAK_DAYS = 1
        secs, capped = M.online_streak(os.path.join(root, "CamC"), exts, int(now))
        eq(capped, False, "今天+昨天都扫完，没有更早数据 → 不封顶")
        ok(179 * 60 <= secs <= 181 * 60, "跨零点的连续不被误判成断点", secs)
    finally:
        M.ONLINE_STREAK_DAYS = old_days
    # 再补前天：窗口=1 天时扫到「今天+昨天」之外还有前天 → 截断 → 封顶
    d2 = os.path.join(root, "CamC", "profile_1",
                      (datetime.now() - timedelta(days=2)).strftime("%Y%m%d"))
    os.makedirs(d2, exist_ok=True)
    touch(os.path.join(d2, "z.mp4"), now - 2 * 86400)
    old_days = M.ONLINE_STREAK_DAYS
    try:
        # 窗口 = 1 天：现在 3 个日期目录（今天/昨天/前天），扫 max_days+1=2 个后还有更早 → 截断
        M.ONLINE_STREAK_DAYS = 1
        ms, tr = M._recent_mtimes(os.path.join(root, "CamC"), exts, 1)
        eq(tr, True, "窗口+1 天之外还有更早数据 → 截断标记为真")
        ok(len(ms) == 181 + 1, "多扫一个目录只为覆盖最老那天的边界", len(ms))
        ev = {c["channel"]: c for c in M.evaluate(cfg)["channels"]}
        eq(ev["CamC"]["online_str"], "3 小时", "最老那天是部分覆盖的，不拿它当断点 → 显示真实时长")
        # 窗口 = 2 天 → 扫「今天+昨天+前天」正好扫完，没有更早 → 不封顶
        M.ONLINE_STREAK_DAYS = 2
        secs, capped = M.online_streak(os.path.join(root, "CamC"), exts, int(now))
        eq(capped, False, "全部日期目录都扫到 → 不封顶（不受最老那天部分覆盖影响）")
    finally:
        M.ONLINE_STREAK_DAYS = old_days

    # fmt_duration 各档位
    eq(M.fmt_duration(30), "不足 1 分钟", "格式化：不足 1 分钟")
    eq(M.fmt_duration(3600), "1 小时", "格式化：整小时")
    eq(M.fmt_duration(5400), "1 小时 30 分", "格式化：小时+分")
    eq(M.fmt_duration(172800), "2 天", "格式化：整天")
    eq(M.fmt_duration(180000), "2 天 2 小时", "格式化：天+小时")

    # camera 布局聚合两个 profile 的 mtime 算 streak
    now2 = time.time()
    for prof in ("profile_1", "profile_2"):
        dp = os.path.join(root, "CamD", prof, datetime.now().strftime("%Y%m%d"))
        os.makedirs(dp, exist_ok=True)
        for i in range(3):                     # 每个 profile 各 3 个、错开 2 分钟，聚合后间隔 ≤ 2 分钟
            touch(os.path.join(dp, "x%d.mp4" % i),
                  now2 - (5 - i * 2) * 60 - (0 if prof == "profile_1" else 60))
    secs, capped = M.online_streak(os.path.join(root, "CamD"), exts, int(now2))
    ok(5 * 60 <= secs <= 7 * 60 and not capped,
       "camera 布局聚合两个 profile 的 mtime 算在线时长（单 profile 只有 4 分钟）", secs)

    # 封顶：最近几天全部连续（每分钟一个文件、按整分钟连续排列，天与天之间不断）
    now3 = time.time()
    base = int(now3) - 5 * 86400             # 5 天前开始，每分钟一个文件，连到刚刚
    for i in range(5 * 24 * 60):
        ts = base + i * 60
        day = datetime.fromtimestamp(ts).strftime("%Y%m%d")
        dp = os.path.join(root, "CamE", "profile_1", day)
        os.makedirs(dp, exist_ok=True)
        touch(os.path.join(dp, "e%06d.mp4" % i), ts)
    old_days = M.ONLINE_STREAK_DAYS
    try:
        M.ONLINE_STREAK_DAYS = 3
        secs, capped = M.online_streak(os.path.join(root, "CamE"), exts, int(now3))
        eq(capped, True, "3 天内全连续、更早还有数据 → 封顶")
        ev = {c["channel"]: c for c in M.evaluate(cfg)["channels"]}
        eq(ev["CamE"]["online_str"], ">3 天", "封顶显示 >3 天")
    finally:
        M.ONLINE_STREAK_DAYS = old_days


def test_normalize_keeps_secret():
    # 设置页「留空=不修改」：apply_wizard 的语义在 config.normalize 里也要成立
    C.save_config({"dingtalk_webhook": "https://a/b", "dingtalk_secret": "SEC1"})
    raw = json.loads(C.CONFIG_FILE.read_text(encoding="utf-8"))
    raw.pop("dingtalk_secret")
    C.save_config(raw)
    eq(C.load_config()["dingtalk_webhook"], "https://a/b", "未提供的键保留默认/原值")


def main():
    print("== erm 自检开始（数据目录 %s）" % TMP_VAR)
    srv, webhook = start_mock()
    try:
        for fn in (test_config_types, test_config_io, test_channels_and_evaluate,
                   test_easynvr_layout, test_online_streak,
                   test_scan_alert_and_silence, test_daily_schedule, test_report_text,
                   test_normalize_keeps_secret):
            fn()
        test_push(webhook)
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
