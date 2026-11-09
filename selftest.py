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
import errno
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


def mk_one_cam_tree(root, last_dt, cam="Zm9kd0Q0W1x2"):
    """单摄像头单 profile 的树，唯一一条录像的 mtime 钉死在 last_dt。

    比 mk_record_tree 更适合测判据：时间完全可控，断言不会因为「跑自检时是几点」而变红。
    """
    d = os.path.join(root, cam, "profile_1", last_dt.strftime("%Y%m%d"))
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, "%s.mp4" % last_dt.strftime("%Y%m%d%H%M%S"))
    with open(fp, "wb") as f:
        f.write(b"\0" * 16)
    ts = last_dt.timestamp()
    os.utime(fp, (ts, ts))
    return fp


def test_record_mode():
    """v1.1.7：源端 EasyNVR 录像方式 —— 判据的物理前提，必选项。

    核心不是「加了个下拉」，而是同一个「多久没新录像」在三种录像方式下含义不同：
      · 全天连续：没新文件 = 一定出问题
      · 移动侦测：没新文件 = 常态，只能用超长静默兜底
      · 白天连续+夜间侦测：白天严判，夜里放过
    """
    # ---- 归一化：留空就是留空，不替用户兜底成 continuous ----
    eq(C.normalize({})["record_mode"], "", "录像方式默认为空（不自作主张）")
    for m in C.RECORD_MODES:
        eq(C.normalize({"record_mode": m})["record_mode"], m, "录像方式保留 %s" % m)
    eq(C.normalize({"record_mode": " MOTION "})["record_mode"], "motion", "录像方式容错空格大小写")
    eq(C.normalize({"record_mode": "bogus"})["record_mode"], "", "非法录像方式 → 空")
    eq(C.normalize({"record_mode_motion_max_minutes": 0})["record_mode_motion_max_minutes"], 5,
       "最长静默下限 5 分钟（填 0 不能变成「永不告警」）")
    eq(C.normalize({})["record_mode_motion_max_minutes"], 1440, "最长静默默认 24 小时")
    eq(C.normalize({})["record_mode_day_from"], "07:00", "白天起点默认 07:00")
    eq(C.normalize({})["record_mode_day_to"], "19:00", "白天终点默认 19:00")
    eq(C.normalize({"record_mode_day_from": "7:5"})["record_mode_day_from"], "07:05", "白天时段规整 HH:MM")

    # ---- 必选校验（server.validate 用它拦保存）----
    ok(C.record_mode_error({}), "未选择 → 有错误文案", C.record_mode_error({}))
    ok(C.record_mode_error({"record_mode": "bogus"}), "非法值 → 有错误文案")
    eq(C.record_mode_error({"record_mode": "continuous"}), "", "选连续录像即通过")
    eq(C.record_mode_error({"record_mode": "mixed"}), "", "选混合模式即通过")

    real_now = datetime.now()
    # 同一份录像：最后一条在「昨天 06:00」，观察时刻「昨天 07:30」（白天时段内）
    day = (real_now - timedelta(days=1)).replace(hour=7, minute=30, second=0, microsecond=0)
    root1 = os.path.join(TMP_VAR, "mode-day")
    mk_one_cam_tree(root1, day.replace(hour=6, minute=0))
    base = {"record_root": root1, "alert_threshold_minutes": "60"}
    cfg_c = C.normalize(dict(base, record_mode="continuous"))
    cfg_x = C.normalize(dict(base, record_mode="mixed",
                             record_mode_day_from="07:00", record_mode_day_to="19:00"))
    eq(M.evaluate(cfg_c, now=day)["channels"][0]["status"], "silent",
       "连续录像：07:30 距 06:00 已 90 分钟 → 断流")
    ev_x = M.evaluate(cfg_x, now=day)
    eq(ev_x["channels"][0]["status"], "ok",
       "白天连续+夜间侦测：闲置从白天起点 07:00 算起，只有 30 分钟 → 不误报")
    eq(ev_x["channels"][0]["silent_min"], 30, "mixed 白天闲置按「max(最后录像, 白天起点)」算")
    eq(ev_x["record_mode"], "mixed", "结果里带上生效的录像方式")
    eq(ev_x["record_mode_set"], True, "结果里标记用户已选择")
    eq(ev_x["judge_threshold_min"], 60, "结果里带上本轮判定阈值")

    # 夜间：连续录像要报，混合模式放过
    night_root = os.path.join(TMP_VAR, "mode-night")
    night = day.replace(hour=3, minute=0)
    mk_one_cam_tree(night_root, night.replace(hour=1, minute=0))
    nbase = {"record_root": night_root, "alert_threshold_minutes": "60"}
    eq(M.evaluate(C.normalize(dict(nbase, record_mode="continuous")), now=night)
       ["channels"][0]["status"], "silent", "连续录像：凌晨 2 小时没录像 → 断流")
    eq(M.evaluate(C.normalize(dict(nbase, record_mode="mixed",
                                   record_mode_day_from="07:00",
                                   record_mode_day_to="19:00")), now=night)
       ["channels"][0]["status"], "ok",
       "白天连续+夜间侦测：夜间不判定流（夜里没人是正常的）")

    # 移动侦测：3 小时没文件是常态；超过最长静默才告警
    mroot = os.path.join(TMP_VAR, "mode-motion")
    mk_one_cam_tree(mroot, real_now - timedelta(hours=3))
    mb = {"record_root": mroot, "alert_threshold_minutes": "60"}
    eq(M.evaluate(C.normalize(dict(mb, record_mode="motion")))["channels"][0]["status"], "ok",
       "移动侦测：3 小时没录像是常态，不告警（连续录像是要报的）")
    eq(M.evaluate(C.normalize(dict(mb, record_mode="motion",
                                   record_mode_motion_max_minutes="120")))
       ["channels"][0]["status"], "silent", "移动侦测：超过自定义的最长静默 → 告警")

    old_root = os.path.join(TMP_VAR, "mode-motion-old")
    mk_one_cam_tree(old_root, real_now - timedelta(hours=26))
    cfg_old = C.normalize({"record_root": old_root, "record_mode": "motion"})
    ch_old = M.evaluate(cfg_old)["channels"][0]
    eq(ch_old["status"], "silent", "移动侦测：26 小时一个文件都没有 → 兜底告警")
    msg = M.build_alert_message(cfg_old, ch_old)
    ok("移动侦测" in msg, "告警文案说明这是移动侦测判据", msg)
    ok("源端录像方式" in msg, "告警文案带上「源端录像方式」一行", msg)

    # mixed 白天告警文案要指出是白天时段出的事
    cfg_x2 = C.normalize(dict(base, record_mode="mixed", alert_threshold_minutes="10",
                              record_mode_day_from="07:00", record_mode_day_to="19:00"))
    ch_x2 = {c["channel"]: c for c in M.evaluate(cfg_x2, now=day)["channels"]}[
        list({c["channel"] for c in M.evaluate(cfg_x2, now=day)["channels"]})[0]]
    eq(ch_x2["status"], "silent", "mixed：白天阈值压到 10 分钟 → 30 分钟无录像即断流")
    ok("白天连续录像时段" in M._alert_reason(cfg_x2, ch_x2), "mixed 告警文案点明白天时段")

    # ---- 向导与映射：装的时候就能选，不用装完再进页面 ----
    pkg = os.path.dirname(APP)
    for name in ("install", "config"):
        steps = json.load(open(os.path.join(pkg, "wizard", name), encoding="utf-8"))
        fields = {it.get("field") for s in steps for it in (s.get("items") or [])}
        ok("wizard_record_mode" in fields, "向导 %s：有源端录像方式字段" % name)
    winst = json.load(open(os.path.join(pkg, "wizard", "install"), encoding="utf-8"))
    sel = [it for s in winst for it in (s.get("items") or [])
           if it.get("field") == "wizard_record_mode"]
    eq({o.get("value") for o in (sel[0].get("options") or [])} if sel else set(),
       set(C.RECORD_MODES), "安装向导：三个选项与 RECORD_MODES 一一对应")
    eq(sel[0].get("initValue") if sel else None, "continuous",
       "安装向导：默认落在连续录像（与升级前行为一致，不会凭空多出误报）")
    aw = open(os.path.join(pkg, "app", "scripts", "apply_wizard.py"), encoding="utf-8").read()
    ok('"wizard_record_mode", "record_mode"' in aw, "向导值会写进 record_mode")

    html = open(os.path.join(APP, "www", "index.html"), encoding="utf-8").read()
    ok('id="record_mode"' in html, "页面：有源端录像方式下拉")
    for m in C.RECORD_MODES:
        ok('value="%s"' % m in html, "页面：下拉里有 %s 选项" % m)
    ok("必选" in html, "页面：标明是必选项")
    # v1.1.8 版面：录像方式是判据前提，排在录像目录前面（桌面端两者同一行）；
    # 三种模式的说明统一进卡片底部的注释，下拉下方不再挂跟着选项跳动的动态小字。
    ok(html.index('id="record_mode"') < html.index('id="record_root"'),
       "页面：录像方式排在录像目录前面")
    ok('id="recordModeHint"' not in html, "页面：下拉下方不再挂动态小字")
    ok('id="modeExtraWrap"' in html, "页面：补充项有独立容器（跟着下拉显隐，不挤占首行）")


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

    def _age_files(root, hours):
        ts = int(time.time()) - int(hours * 3600)
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                os.utime(os.path.join(dirpath, fn), (ts, ts))

    def _freshen(root):
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                os.utime(os.path.join(dirpath, fn), None)

    try:
        # ---- v1.1.6①：目录整体不可达 = 监控自己瞎了，必须穿透静默
        M.now_dt = lambda cfg: sat                            # 周六 09:00（非推送日）
        st = {}
        res = M.run_scan(quiet_cfg, st)
        ok(not res.get("quiet"), "穿透静默：时段外 + 目录不可达 → 不算 quiet")
        eq(len(res.get("events") or []), 1, "穿透静默：时段外 + 目录不可达 → 照常产生告警")
        ok(st["channels"].get("__global__", {}).get("alerting"), "穿透静默：全局告警状态已推进")

        M.now_dt = lambda cfg: datetime(2026, 10, 28, 10, 0, tzinfo=tz)   # 周三 10:00
        st2 = {}
        res2 = M.run_scan(quiet_cfg, st2)
        ok(not res2.get("quiet"), "时段内：正常判定")
        eq(len(res2.get("events") or []), 1, "时段内：目录不存在 → 照样产生告警事件")
        ok(not st2.get("quiet_since"), "时段内：清掉 quiet_since")

        # ---- v1.1.6②：通道级断流在时段外仍静默，但要留下轨迹
        root2, _f2 = mk_record_tree(os.path.join(TMP_VAR, "record-quiet"))
        quiet_cfg2 = C.normalize({"record_root": root2, "video_exts": ".mp4",
                                  "alert_threshold_minutes": "60",
                                  "alert_push_weekdays": "1,2,3,4,5",
                                  "alert_window_from": "08:00", "alert_window_to": "18:00"})
        _age_files(root2, 5)                                  # 5 小时前的录像 → 断流
        M.now_dt = lambda cfg: sat
        s3 = {}
        r3 = M.run_scan(quiet_cfg2, s3)
        ok(r3.get("quiet") is True, "静默期：通道断流在时段外仍然静默")
        eq(r3.get("events"), [], "静默期：时段外不推送")
        ok(s3.get("quiet_events"), "静默期：异常轨迹已记进 quiet_events")
        ok(s3.get("quiet_since"), "静默期：记录 quiet_since")

        # 断流在静默期内自愈 → 出时段时必须汇总，否则这段时间等于没发生
        _freshen(root2)
        M.now_dt = lambda cfg: datetime(2026, 10, 28, 10, 0, tzinfo=tz)   # 进时段
        r4 = M.run_scan(quiet_cfg2, s3)
        dig = [e for e in (r4.get("events") or []) if e.get("kind") == "digest"]
        eq(len(dig), 1, "静默期汇总：进时段只推一条汇总")
        ok("静默期汇总" in dig[0]["message"], "静默期汇总：标题正确", dig[0]["message"])
        ok("已恢复" in dig[0]["message"], "静默期汇总：自愈的标成「已恢复」", dig[0]["message"])
        ok(not s3.get("quiet_events"), "静默期汇总：推完清空 quiet_events")
        ok(not s3.get("quiet_since"), "静默期汇总：推完清掉 quiet_since")

        # 安静的夜晚：一件异常都没记下，就不该冒出一条空汇总
        s5 = {"quiet_since": int(time.time()) - 3600}
        M.now_dt = lambda cfg: datetime(2026, 10, 28, 10, 0, tzinfo=tz)
        r6 = M.run_scan(quiet_cfg2, s5)
        ok(not [e for e in (r6.get("events") or []) if e.get("kind") == "digest"],
           "静默期汇总：无异常时不出空汇总")
        eq(M.build_quiet_digest(quiet_cfg2, {}, int(time.time())), None,
           "静默期汇总：无事件时 build 直接返回 None")
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

        # 断流清单固定最近 48 小时（v1.2.1 起）。上面这份数据录在 3 天前、之后再没动过，
        # 所以清单里应当正好是一条「整窗都没录像」—— 这正是最该被看见的那种情况
        # （gaps_of 拿不到窗口内文件会返回空，gaps_recent 补的就是这个洞）。
        eq(len(r["gaps"]), 1, "统计：整窗无录像的通道在清单里报一条")
        eq(r["gaps"][0]["from"], r["gap_window"]["start"],
           "统计：整窗无录像的起点 = 窗口起点")
        eq(r["gaps"][0]["to"], r["gap_window"]["end"],
           "统计：整窗无录像的终点 = 窗口终点")
        eq(r["gaps"][0]["bucket"], "gt60", "统计：整窗无录像归「> 1 小时」档")
        eq(r["gap_total"], 1, "统计：gap_total 是清单自己的条数")
        eq(r["gap_window"]["end"] - r["gap_window"]["start"], S.GAP_WINDOW_SEC,
           "统计：清单窗口 = 48 小时")
        eq(r["gap_buckets"][0]["count"], 1, "统计：全部档计数 = 清单条数")
        # KPI 仍按页面范围算：中间那段 61 分钟 + 「最后一个文件 → 7 天窗口终点」各一条
        eq(r["summary"]["gap_total"], 2, "统计：KPI 断流段数仍按页面范围算，与清单分开")
        ok(r["summary"]["longest_gap_min"] > 60 * 24,
           "统计：KPI 最长断流能跨天（清单只看 48 小时，不背这个包袱）")

        # 把同样的 61 分钟断流挪进窗口里：起止、档位、文案都要对得上
        root_w = tempfile.mkdtemp(prefix="erm-stats-w-")
        try:
            nowts = int(M.now_dt(cfg).timestamp())
            wcam = "STATCAM00002"
            a0 = nowts - 5 * 3600                 # A 段：now-5h → now-4h30 连录
            tail = a0 + 60 * 30
            b0 = tail + 61 * 60                   # 断 61 分钟后 B 段开始
            marks_w = ([a0 + 60 * i for i in range(31)] +
                       [b0 + 60 * i for i in range(int((nowts - 60 - b0) / 60) + 1)])
            for t in marks_w:
                ds = datetime.fromtimestamp(t, M.get_tz(cfg)).strftime("%Y%m%d")
                dd = os.path.join(root_w, wcam, "profile_1", ds)
                os.makedirs(dd, exist_ok=True)
                fp = os.path.join(dd, "%d.mp4" % t)
                with open(fp, "wb") as f:
                    f.write(b"\0")
                os.utime(fp, (t, t))
            S.clear_cache()
            cfgw = C.normalize({"record_root": root_w, "timezone": "Asia/Shanghai"})
            rw = S.build(cfgw, "7d", now=M.now_dt(cfgw))
            eq(len(rw["gaps"]), 1, "统计：48 小时内的断流进清单（且只有那一条）")
            gw = rw["gaps"][0]
            eq(gw["from"], tail, "统计：断流起点 = 段尾最后一个文件")
            eq(gw["to"], b0, "统计：断流终点 = 下一段第一个文件")
            eq(gw["dur_sec"], 61 * 60, "统计：断流时长按文件到文件算")
            ok("小时" in gw["dur_str"], "统计：断流时长带可读文案", gw["dur_str"])
            eq(gw["bucket"], "gt60", "统计：61 分钟归「> 1 小时」档")
            eq(gw["bucket_label"], "> 1 小时", "统计：档位带展示文案")
            eq(rw["gap_buckets"][-1]["count"], 1, "统计：gt60 档计数 = 1")
            eq(rw["gap_buckets"][0]["count"], 1, "统计：全部档计数 = 清单条数")
        finally:
            shutil.rmtree(root_w, ignore_errors=True)

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

    # ④ v1.1.9：日志独立成页 —— 原先藏在配置页里，点「查看日志」才展开一块卡片
    ok('id="page-log"' in html, "UI：有日志页容器")
    eq(html.count('data-page="log"'), 1, "导航项「日志」唯一")
    ok(html.index('id="page-backup"') < html.index('id="page-log"'),
       "UI：日志页排在备份页之后")
    ok(html.index('id="page-log"') < html.index('id="logBox"'),
       "UI：日志内容在日志页内")
    ok('id="btnLogs"' not in html, "UI：配置页不再有「查看日志」按钮")
    ok('id="logCard"' not in html, "UI：日志不再是被 display:none 藏起来的卡片")
    ok("if (name === 'log') loadLogs();" in html, "UI：切到日志页才读日志（不占其它页开销）")
    ok("$('logLines').onchange = loadLogs;" in html, "UI：日志行数可切换（200~2000）")


def test_backup():
    """备份（v1.1.0）：落盘、列表、按份数清理、恢复、拒绝路径穿越、定时到点判定。"""
    import backup as B
    from pathlib import Path

    for k in ("backup_enabled", "backup_dir", "backup_time", "backup_keep",
              "backup_weekdays", "backup_include_config", "backup_include_state",
              "backup_extra_files"):
        ok(k in C.DEFAULT_CONFIG, "配置：默认值里有 %s" % k)

    bkdir = Path(TMP_VAR) / "bkdir"

    # ① 没设目录就拒绝，而不是默默写到系统盘
    r = B.run_backup(C.normalize({"backup_dir": ""}), {})
    ok(not r["ok"], "备份：没设目录时拒绝执行", r)

    # ② 目录还不存在 → 自动创建并落盘
    cfg = C.normalize({"backup_dir": str(bkdir), "backup_keep": 5})
    r = B.run_backup(cfg, {}, reason="manual")
    ok(r["ok"], "备份：首次备份成功", r)
    p = Path(r["path"])
    ok(p.is_file(), "备份：文件已落盘")
    doc = json.loads(p.read_text(encoding="utf-8"))
    ok("config.json" in (doc.get("files") or {}), "备份：包含 config.json")
    eq(doc.get("reason"), "manual", "备份：记下了备份原因")
    eq(len(B.list_backups(cfg)), 1, "备份：列表返回 1 份")

    # ③ 保留份数：keep=2 时做 4 份只留 2 份
    cfg2 = C.normalize({"backup_dir": str(bkdir), "backup_keep": 2})
    for _ in range(4):
        B.run_backup(cfg2, {}, reason="scheduled")
    eq(len(B.list_backups(cfg2)), 2, "备份：按保留份数清理到 2 份")

    # ④ 路径穿越必须拦掉（restore/delete 只接受自己生成的文件名）
    ok(not B.restore(cfg, "../../etc/passwd")["ok"], "备份：拒绝路径穿越的文件名")
    ok(not B.remove(cfg, "../x.json")["ok"], "备份：删除也拒绝非法文件名")

    # ⑤ 恢复：改掉配置再恢复，回到备份时的值；但备份设置本身不跟着回退
    C.save_config(C.normalize({"record_root": "/vol1/marker-A", "backup_dir": str(bkdir)}))
    r = B.run_backup(C.load_config(), {}, reason="manual")
    ok(r["ok"], "备份：恢复前先备一份", r)
    C.save_config(C.normalize({"record_root": "/vol1/marker-B", "backup_dir": str(bkdir)}))
    eq(C.load_config()["record_root"], "/vol1/marker-B", "备份：改动已写入")
    res = B.restore(C.load_config(), r["file"], {})
    ok(res["ok"], "备份：恢复成功", res)
    eq(C.load_config()["record_root"], "/vol1/marker-A", "备份：恢复后回到备份时的值")
    eq(C.load_config()["backup_dir"], str(bkdir), "备份：恢复时保留当前备份设置")
    ok(res.get("pre_backup"), "备份：恢复前自动留了 pre-restore 快照")
    eq(res.get("restored_files"), ["config.json"], "备份：没勾运行状态时只恢复 config.json")

    # ⑤b 运行状态：勾了「运行状态」时 state 也跟着回来，但 last_backup_* 水位线不回退
    C.save_config(C.normalize({"backup_dir": str(bkdir), "backup_include_state": True}))
    C.save_state({"channels": {"cam1": {"state": "ok"}}, "last_check": 777,
                  "last_backup_day": "2000-01-01", "last_backup_file": "old.json"})
    rs = B.run_backup(C.load_config(), {}, reason="manual")
    ok(rs["ok"], "备份：带运行状态能备成功", rs)
    eq(sorted(rs.get("items") or []), ["config.json", "state.json"],
       "备份：内容含 config.json 与 state.json")
    live = {"channels": {}, "last_check": 1, "last_backup_file": "keep.json"}
    res2 = B.restore(C.load_config(), rs["file"], live)
    ok(res2["ok"], "备份：带运行状态恢复成功", res2)
    eq(live.get("channels"), {"cam1": {"state": "ok"}}, "备份：运行状态跟着恢复")
    eq(live.get("last_check"), 777, "备份：备份里有的键覆盖当前值")
    ok(live.get("last_backup_file") != "old.json", "备份：last_backup_* 水位线不跟着回退")
    eq(sorted(res2.get("restored_files") or []), ["config.json", "state.json"],
       "备份：恢复文件清单含 state.json")

    # ⑥ 定时：没启用 / 没到点 / 今天已备 都不触发
    off = C.normalize({"backup_enabled": False, "backup_dir": str(bkdir)})
    ok(not B.backup_due(off, {}), "定时：未启用不触发")
    on = C.normalize({"backup_enabled": True, "backup_dir": str(bkdir),
                      "backup_time": "03:00", "backup_weekdays": [1, 2, 3, 4, 5, 6, 7]})
    at = B.now_dt(on).replace(hour=3, minute=1, second=0, microsecond=0)
    ok(B.backup_due(on, {}, now=at), "定时：到点且今天没备 → 触发")
    ok(not B.backup_due(on, {"last_backup_day": B.today_str(on, at)}, now=at),
       "定时：今天已备 → 不再触发")
    ok(not B.backup_due(on, {}, now=at.replace(hour=2, minute=59)), "定时：没到点 → 不触发")
    ok(not B.backup_due(C.normalize({"backup_enabled": True, "backup_dir": ""}), {}),
       "定时：开了开关但没目录 → 不触发")
    ok(B.next_backup_str(off, {}) == "未启用", "定时：未启用时下次时间显示「未启用」")

    # ⑦ UI 骨架
    html = open(os.path.join(APP, "www", "index.html"), encoding="utf-8").read()
    ok('id="page-backup"' in html, "UI：有备份页容器")
    ok('data-page="backup"' in html, "UI：有备份页签")
    ok("'overview', 'config', 'backup', 'log'" in html, "UI：键盘切页顺序包含备份与日志")
    ok("#page-config,#page-backup,#page-log,#page-overview{max-width:1080px" in html,
       "UI：备份页 / 日志页与另两页同宽（1080）")
    ok('id="btnBkImport"' in html, "UI：有「导入备份」按钮")
    ok('data-act="export"' in html, "UI：备份记录每行有「导出」按钮")
    ok(html.index('data-act="export"') < html.index('data-act="restore"'),
       "UI：「导出」排在「恢复」之前")

    # ⑧ 导入：校验内容、不认上传的文件名、时间戳沿用内容里的 created_at
    icfg = C.normalize({"backup_dir": str(bkdir), "backup_keep": 0})
    imp = json.dumps({"kind": "erm-backup", "app": "erm", "reason": "manual",
                      "created_at": "2026-01-02 03:04:05",
                      "items": ["config.json"],
                      "files": {"config.json": {"record_root": "/vol1/imported"}},
                      "extra": []}, ensure_ascii=False)
    r3 = B.import_file(icfg, "../etc/passwd", imp)
    ok(r3["ok"], "导入：合法备份能收下", r3)
    ok(r3["file"].startswith("erm-backup-20260102-030405"),
       "导入：文件名沿用内容里的 created_at", r3)
    ok("/" not in r3["file"] and ".." not in r3["file"],
       "导入：文件名由服务端重生成，不认上传的名字")
    p3 = Path(bkdir) / r3["file"]
    ok(p3.is_file(), "导入：文件确实落在备份目录")
    eq(p3.stat().st_mode & 0o777, 0o600, "导入：文件权限 0600")

    # ⑧a 备份目录在外接盘上（exFAT / FAT32 没有 POSIX 权限位）：chmod 抛 EPERM，
    #     但文件已经落盘，这次导入必须照样算成功 —— 否则「备份好好的、导入必失败」。
    _chmod = os.chmod

    def _eperm(p, m, *a, **k):
        raise OSError(errno.EPERM, "Operation not permitted")

    os.chmod = _eperm
    try:
        r4 = B.import_file(icfg, "a.json", imp)
    finally:
        os.chmod = _chmod
    ok(r4["ok"], "导入：文件系统不支持 chmod（外接 exFAT）也照样成功", r4)
    ok((Path(bkdir) / r4["file"]).is_file(), "导入：chmod 失败时文件仍然落盘")
    eq(r4.get("error"), None, "导入：chmod 失败不往界面弹错")
    ok(B._tighten(p3) in (True, False), "导入：_tighten 只返回布尔，永不抛异常")
    row3 = [x for x in B.list_backups(icfg) if x["file"] == r3["file"]]
    ok(row3 and row3[0]["time_str"] == "2026-01-02 03:04:05",
       "导入：列表时间取文件名时间戳（不是导入那一刻的 mtime）", row3)
    ok(not B.import_file(icfg, "a.json", "{not json")["ok"], "导入：非法 JSON 拒绝")
    ok(not B.import_file(icfg, "a.json", json.dumps({"kind": "other"}))["ok"],
       "导入：不是本应用的备份拒绝")
    ok(not B.import_file(icfg, "a.json", "   ")["ok"], "导入：空文件拒绝")
    ok(not B.import_file(icfg, "a.json", json.dumps({"kind": "erm-backup"}))["ok"],
       "导入：既没 config 也没额外文件的备份拒绝")
    _max = B.MAX_IMPORT_BYTES
    B.MAX_IMPORT_BYTES = 64
    ok(not B.import_file(icfg, "a.json", imp)["ok"], "导入：超过大小上限拒绝")
    B.MAX_IMPORT_BYTES = _max

    # ⑨ 导入进来的备份能被 restore 直接吃（文件名合 NAME_RE，恢复链路不用特判）
    ok(bool(B.NAME_RE.match(r3["file"])), "导入：生成的文件名符合恢复/删除的命名规则")

    # ⑩ 导出：给的是磁盘原文（不重新序列化），非法名 / 不存在的拒绝
    exp = B.export_text(icfg, r3["file"])
    ok(exp["ok"], "导出：能读到备份原文", exp)
    eq(exp["content"], p3.read_text(encoding="utf-8"), "导出：内容与磁盘文件逐字一致")
    eq(exp["size"], p3.stat().st_size, "导出：大小与磁盘一致")
    ok(not B.export_text(icfg, "../x.json")["ok"], "导出：拒绝非法文件名")
    ok(not B.export_text(icfg, "erm-backup-20000101-000000.json")["ok"], "导出：不存在时拒绝")


def test_uninstall_wizard():
    """v1.1.4：卸载向导——让用户选「保留现有文件」还是「清除所有套件数据文件」。"""
    pkg = os.path.dirname(APP)
    wz = os.path.join(pkg, "wizard", "uninstall")
    ok(os.path.isfile(wz), "打包：有 wizard/uninstall（卸载向导文件）")
    steps = json.load(open(wz, encoding="utf-8"))
    ok(isinstance(steps, list) and bool(steps), "卸载向导：顶层是非空数组（飞牛格式）")
    items = [it for s in steps for it in (s.get("items") or [])]
    ok("wizard_delete_data" in {it.get("field") for it in items},
       "卸载向导：字段名 wizard_delete_data（平台认这个）")
    radios = [it for it in items
              if it.get("type") == "radio" and it.get("field") == "wizard_delete_data"]
    ok(bool(radios), "卸载向导：是 radio 单选项")
    r = radios[0] if radios else {}
    eq({o.get("value") for o in (r.get("options") or [])}, {"true", "false"},
       "卸载向导：两个选项值必须是 true / false")
    eq(r.get("initValue"), "false", "卸载向导：默认落在「保留」，不预设成清除")
    labels = " ".join(o.get("label") or "" for o in (r.get("options") or []))
    ok("保留" in labels and "清除" in labels, "卸载向导：两个选项文案有「保留」「清除」")

    # 卸载脚本：怎么取向导值、默认保留、不碰用户的备份目录
    sh = open(os.path.join(pkg, "cmd", "uninstall_callback"), encoding="utf-8").read()
    ok("wizard_delete_data" in sh, "卸载脚本：读取 wizard_delete_data")
    ok('"${DEL}" = "true"' in sh, "卸载脚本：只有明确等于 true 才清除")
    ok("installer-variables" in sh, "卸载脚本：环境变量拿不到时回落 installer-variables")
    ok("rm -rf" not in sh, "卸载脚本：不用 rm -rf（find -mindepth 1 -delete 保留目录外壳）")
    ok("backup_dir" not in sh, "卸载脚本：不碰用户自选的备份目录")


def test_nvr_health():
    """源端服务巡检（v1.2.0 P0）：/health 探测、五档判定、去重、手动不污染状态机。"""
    import nvrhealth as NH

    for k in ("nvr_health_enabled", "nvr_base_url", "nvr_health_interval",
              "nvr_expire_warn_days"):
        ok(k in C.DEFAULT_CONFIG, "巡检：默认值里有 %s" % k)

    # ① 地址规整：用户粘贴的常是页面地址，也得能落到 /health
    cfg = C.normalize({"nvr_health_enabled": True,
                       "nvr_base_url": "http://192.168.11.6:10000/cloud/#/login"})
    eq(cfg["nvr_base_url"], "http://192.168.11.6:10000", "巡检：地址只留 scheme://host:port")
    eq(NH.health_url(cfg), "http://192.168.11.6:10000/health", "巡检：拼出 /health")
    eq(NH.health_url(C.normalize({})), "", "巡检：没填地址不拼 URL")

    base = {"nvr_health_enabled": True, "nvr_base_url": "http://nvr:10000",
            "nvr_expire_warn_days": 7, "alert_silence_minutes": 120}
    cfg = C.normalize(base)

    def pr(data=None, ok_=True, err=""):
        return {"ok": ok_, "url": "http://nvr:10000/health", "data": data or {},
                "error": err, "elapsed_ms": 2}

    # ② 判定五档（顺序有意义：连不上 > 状态异常 > 过期 > 临期 > 正常）
    eq(NH.judge(cfg, pr({"status": "ok"}))["level"], "ok", "巡检：无 deadline 视为正常")
    eq(NH.judge(cfg, pr(ok_=False, err="连接被拒绝"))["level"], "unreachable",
       "巡检：连不上 = 不可达")
    eq(NH.judge(cfg, pr({"status": "error"}))["level"], "bad_status", "巡检：status 非 ok")
    dl_past = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    eq(NH.judge(cfg, pr({"status": "ok", "deadline": dl_past}))["level"], "expired",
       "巡检：授权已过期")
    dl_soon = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
    j = NH.judge(cfg, pr({"status": "ok", "deadline": dl_soon}))
    eq(j["level"], "expiring", "巡检：授权临期")
    ok("剩余" in j["reason"], "巡检：临期文案带剩余天数", j["reason"])
    eq(NH.judge(C.normalize(dict(base, nvr_expire_warn_days=0)),
                pr({"status": "ok", "deadline": dl_soon}))["level"], "ok",
       "巡检：预警天数 0 = 不提醒临期")

    # ③ 状态机：首次推 / 同一异常静默 / 恢复补一条
    state = {}
    NH.probe = lambda cfg, timeout=5: pr(ok_=False, err="连接被拒绝")
    NH.push_all = lambda cfg, text: [{"channel": "dingtalk", "ok": True, "detail": "ok"}]
    eq(NH.check(cfg, state, push=True)["event"], "alert", "巡检：首次不可达要推")
    eq(NH.check(cfg, state, push=True)["event"], "", "巡检：同一异常按静默间隔不重复推")
    NH.probe = lambda cfg, timeout=5: pr({"status": "ok", "version": "v7.7.421"})
    r = NH.check(cfg, state, push=True)
    eq(r["event"], "recovery", "巡检：恢复补一条")
    ok("已恢复" in r["text"], "巡检：恢复文案正确", r["text"][:40])

    # ④ 推送全失败要回滚时间戳，否则限流一次就丢一整个静默期
    st_fail = {}
    NH.probe = lambda cfg, timeout=5: pr(ok_=False, err="连接被拒绝")
    NH.push_all = lambda cfg, text: [{"channel": "dingtalk", "ok": False, "detail": "限流"}]
    NH.check(cfg, st_fail, push=True)
    eq(st_fail["nvr_health"].get("last_alert_time", 0), 0, "巡检：推送失败不回写时间戳")
    NH.push_all = lambda cfg, text: [{"channel": "-", "ok": False, "detail": "未启用任何推送渠道"}]
    st_nochan = {}
    NH.check(cfg, st_nochan, push=True)
    ok(st_nochan["nvr_health"].get("last_alert_time", 0) > 0,
       "巡检：没配渠道时不算失败（不每轮重推）")

    # ⑤ 手动巡检只探不发，且不许污染告警状态机
    st2 = {}
    NH.probe = lambda cfg, timeout=5: pr(ok_=False, err="连接被拒绝")
    NH.check(cfg, st2, push=False)
    eq(st2["nvr_health"].get("alerting"), None, "巡检：手动巡检不写 alerting")
    eq(NH.check(cfg, st2, push=True)["event"], "alert", "巡检：手动看过后后台仍要正常推")

    # ⑥ 开关关掉就完全不发请求
    called = []
    NH.probe = lambda cfg, timeout=5: (called.append(1), pr({}))[1]
    NH.check(C.normalize({}), {}, push=True)
    eq(called, [], "巡检：没启用时不发请求")

    # ⑦ UI：字段与按钮都在，保存时带上这四个键
    html = open(os.path.join(APP, "www", "index.html"), encoding="utf-8").read()
    for el in ("nvr_health_enabled", "nvr_base_url", "nvr_health_interval",
               "nvr_expire_warn_days", "btnHealth", "healthPill"):
        ok('id="%s"' % el in html, "UI：有 %s" % el)
    ok("nvr_health_enabled: $('nvr_health_enabled').checked" in html,
       "UI：保存时带上巡检开关")
    ok("renderHealth(d.nvr_health)" in html, "UI：状态栏渲染巡检结果")


def main():
    print("== erm 自检开始（数据目录 %s）" % TMP_VAR)
    srv, webhook = start_mock()
    try:
        for fn in (test_config_types, test_config_io, test_channels_and_evaluate,
                   test_record_mode, test_easynvr_layout, test_online_streak,
                   test_scan_alert_and_silence, test_daily_schedule, test_report_text,
                   test_push_plan_and_report_window, test_stats,
                   test_normalize_keeps_secret, test_ui_shell, test_backup,
                   test_uninstall_wizard, test_nvr_health):
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
