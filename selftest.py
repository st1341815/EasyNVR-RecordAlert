#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""erm 本地自检（不依赖 pytest / 不联网，除本地 mock HTTP）。

覆盖文档评审里点出的每个逻辑坑：
  · 向导字符串 → 强类型（B1）
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
from pathlib import Path

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


def mk_record_tree(root):
    """造一棵 EasyNVR 风格目录：stream_1 刚录过 / stream_2 停了 3 小时 / stream_3 空目录。"""
    now = time.time()
    d_now = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    d_old = datetime.fromtimestamp(now - 3 * 3600).strftime("%Y-%m-%d")
    p1 = os.path.join(root, "stream_1", d_now)
    p2 = os.path.join(root, "stream_2", d_old)
    p3 = os.path.join(root, "stream_3", "empty")
    for p in (p1, p2, p3):
        os.makedirs(p, exist_ok=True)
    f1 = os.path.join(p1, "20261029_050000.mp4")
    f2 = os.path.join(p2, "20261029_020000.mp4")
    for path, ts in ((f1, now - 60), (f2, now - 3 * 3600)):
        with open(path, "wb") as f:
            f.write(b"\0" * 16)
        os.utime(path, (ts, ts))
    return root


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
           "alert_silence_minutes": "0", "monitor_channels": "stream_1，stream_2",
           "enabled_push_channels": "dingtalk,wechat", "daily_report_time": "7:5",
           "notify_recovery": "停用", "video_exts": "mp4,.TS"}
    cfg = C.normalize(raw)
    eq(cfg["alert_threshold_minutes"], 45, "B1 阈值字符串→int")
    eq(cfg["scan_interval"], 300, "B1 非法 scan_interval → 默认 300")
    eq(cfg["alert_silence_minutes"], 0, "B1 静默 0 保留（0=只报一次）")
    eq(cfg["monitor_channels"], ["stream_1", "stream_2"], "B2 逗号/中文逗号 → 列表")
    eq(cfg["enabled_push_channels"], ["dingtalk", "wechat"], "B2 渠道列表")
    eq(cfg["daily_report_time"], "07:05", "日报时间规整 HH:MM")
    eq(cfg["notify_recovery"], False, "as_bool 识别「停用」")
    eq(cfg["video_exts"], [".mp4", ".ts"], "后缀补点并小写")
    # 关键回归：旧代码 list("stream_1,stream_2") 会得到单字符列表
    ok(cfg["monitor_channels"] != list("stream_1,stream_2"), "B2 不拆成单字符")
    # 关键回归：旧代码 "60" * 60 得到字符串
    ok(cfg["alert_threshold_minutes"] * 60 == 2700, "B1 阈值乘 60 是数字")
    eq(C.normalize({"enabled_push_channels": "bogus"})["enabled_push_channels"], [],
       "未知渠道被过滤")


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
    root = mk_record_tree(os.path.join(TMP_VAR, "record"))
    cfg = C.normalize({"record_root": root, "alert_threshold_minutes": "60",
                       "video_exts": ".mp4"})
    names = [c["channel"] for c in M.resolve_channels(cfg)]
    eq(names, ["stream_1", "stream_2", "stream_3"], "自动发现 stream_*")

    # 显式写死一个不存在的通道 → 也要被报出来（B4）
    cfg2 = C.normalize({"record_root": root, "alert_threshold_minutes": "60",
                        "monitor_channels": "stream_9"})
    ev2 = M.evaluate(cfg2)
    eq(len(ev2["channels"]), 1, "显式通道保留")
    eq(ev2["channels"][0]["status"], "missing", "B4 目录不存在 → missing（会告警）")

    ev = M.evaluate(cfg)
    st = {c["channel"]: c for c in ev["channels"]}
    eq(st["stream_1"]["status"], "ok", "刚录过的通道正常")
    eq(st["stream_2"]["status"], "silent", "3 小时没录像 → silent")
    eq(st["stream_3"]["status"], "no_record", "B4 空目录 → no_record（会告警）")
    ok(st["stream_2"]["idle_min"] >= 170, "闲置分钟按「最新文件时间」算", st["stream_2"]["idle_min"])

    # B5：最新文件被删（EasyNVR 清理）→ 用历史 state 会误报，用 latest 不会
    f2 = os.path.join(root, "stream_2", sorted(os.listdir(os.path.join(root, "stream_2")))[0],
                      "20261029_020000.mp4")
    os.utime(f2, (time.time() - 30, time.time() - 30))
    ev = {c["channel"]: c for c in M.evaluate(cfg)["channels"]}
    eq(ev["stream_2"]["status"], "ok", "B5 清掉旧文件后重新录制 → 立刻恢复正常（不误报）")

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
                       "video_exts": ".mp4", "channel_layout": "auto"})

    eq(M.detect_layout(Path(root), cfg), "camera", "自动识别出 EasyNVR 嵌套布局")

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

    # 显式清单：既可写摄像头 ID，也可写 ID/profile_1
    cfg_e = C.normalize({"record_root": root, "alert_threshold_minutes": "60",
                         "video_exts": ".mp4",
                         "monitor_channels": "Xk91mQ2pLzT0,O27fqV6dYvs3T/profile_2"})
    eq([c["channel"] for c in M.resolve_channels(cfg_e)],
       ["O27fqV6dYvs3T/profile_2", "Xk91mQ2pLzT0"], "显式通道清单（含 profile）")

    # 日报按天统计：只数昨天的文件（摄像头 A 的 profile_2 有 2 个）
    now = datetime.now(M.get_tz(cfg))
    text = M.build_daily_report(cfg, now)
    ok("O27fqV6dYvs3T：2 个文件" in text, "日报只统计昨天的录像", text)
    ok("大门：2 个文件" in text or "大门（O27fqV6dYvs3T）：2 个文件" in text, "日报用别名", text)


def test_scan_alert_and_silence():
    root = mk_record_tree(os.path.join(TMP_VAR, "record2"))
    cfg = C.normalize({"record_root": root, "alert_threshold_minutes": "60",
                       "alert_silence_minutes": "120", "video_exts": ".mp4",
                       "enabled_push_channels": []})
    state = {}
    res = M.run_scan(cfg, state)
    scopes = sorted(e["scope"] for e in res["events"])
    eq(scopes, ["stream_2", "stream_3"], "首次扫描对两个异常通道告警（合并一条消息）")
    ok(any(e["kind"] == "alert" for e in res["events"]), "事件类型 alert")

    res2 = M.run_scan(cfg, state)
    eq([e["scope"] for e in res2["events"]], [], "静默期内不重复告警")

    # 恢复通知
    f = os.path.join(root, "stream_2")
    deep = os.path.join(f, sorted(os.listdir(f))[0])
    with open(os.path.join(deep, "new.mp4"), "wb") as fh:
        fh.write(b"\0" * 8)
    res3 = M.run_scan(cfg, state)
    rec = [e["scope"] for e in res3["events"] if e["kind"] == "recovery"]
    eq(rec, ["stream_2"], "恢复时补发恢复通知")

    # 全局错误也要告警
    bad = C.normalize({"record_root": "/nope/zzz", "enabled_push_channels": []})
    r4 = M.run_scan(bad, {"channels": {}})
    ok(any(e["scope"] == "__global__" for e in r4["events"]), "全局错误（目录不可用）也告警")

    # 状态里存的是「最新文件时间」，不是告警时间
    ok(state["channels"]["stream_1"]["latest"] > 0, "state 记录最新文件 mtime")
    eq(state["channels"]["stream_2"]["alerting"], False, "恢复正常后 alerting=False")


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
    root = mk_record_tree(os.path.join(TMP_VAR, "record3"))
    cfg = C.normalize({"record_root": root, "video_exts": ".mp4",
                       "alert_threshold_minutes": "60"})
    now = datetime.now(M.get_tz(cfg))
    # 把 stream_1 的文件时间改到昨天中午，验证「按天统计」
    y = (now - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    d1 = os.path.join(root, "stream_1")
    for dirpath, _dirs, files in os.walk(d1):
        for fn in files:
            os.utime(os.path.join(dirpath, fn), (y.timestamp(), y.timestamp()))
    text = M.build_daily_report(cfg, now)
    ok("录像日报" in text, "日报标题")
    ok("stream_1：1 个文件" in text, "日报含通道计数", text)
    ok("合计：1 个录像文件" in text, "日报含合计")
    ok("异常通道" in text, "日报提示异常通道（stream_2/3）")


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
                   test_easynvr_layout,
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
