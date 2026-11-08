#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EasyNVR 录像监控 —— fnOS 统一网关常驻服务（单进程 = 网关 + 监控线程）。

为什么用一个进程干两件事：
  · cmd/main 只需管一个 pid，停止/重启语义干净（不会出现"网关停了监控还在跑"）；
  · 监控线程与网关共享同一份配置与状态，前端改配置后**立即生效**，无需重启应用。

监听 Unix Socket（$TRIM_APPDEST/erm.sock），由 fnOS 统一网关（micro_app）转发：
  /app/erm/            → www/index.html
  /app/erm/api         → JSON API（GET ?action=xx / POST {"action": xx}）

盘面约定：
  · 程序        $TRIM_APPDEST（= /volX/@appcenter/<app>，升级会整体替换）
  · 配置/状态/日志 $TRIM_PKGVAR（= /volX/@appdata/<app>，升级保留、卸载保留）
"""

import fcntl
import json
import os
import signal
import socket
import socketserver
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

APP_NAME = "erm"
GATEWAY_PREFIX = "/app/" + APP_NAME
VERSION = "1.1.8"
# 请求体上限：导入备份是唯一的大请求（备份文件正常十几 KB）。
# 超限在读完之前就拒绝，别把几百 MB 先读进内存再报错。
MAX_BODY_BYTES = 12 * 1024 * 1024

SELF = os.path.abspath(__file__)
APP_DEST = os.path.dirname(SELF)           # app/ 的内容直接铺到 target 根
WWW = os.path.join(APP_DEST, "www")
SOCK_PATH = os.path.join(APP_DEST, APP_NAME + ".sock")

sys.path.insert(0, APP_DEST)
import backup as B          # noqa: E402
import config as C          # noqa: E402
import monitor as M         # noqa: E402
import notify as N          # noqa: E402
import stats as S           # noqa: E402

LOCK = threading.RLock()          # 保护 CFG / STATE 的读写
SCAN_LOCK = threading.Lock()      # 保证同一时刻只有一次扫描在改 STATE
CFG = C.load_config()
STATE = C.load_state()            # 唯一一份运行状态（后台线程与 API 共用）
STARTED_AT = int(time.time())
STOP_EVENT = threading.Event()
M.setup_logging()
log = M.logger

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


# ---------------------------------------------------------------- 配置校验

def validate(cfg):
    """保存前的体检。返回 warnings（不阻止保存）与 errors（阻止保存）。"""
    warnings, errors = [], []
    # v1.1.7：源端录像方式是必选项 —— 判据的物理前提，选错会天天误报或整夜漏报，
    # 所以这里用 error 拦住保存，而不是模棱两可地替用户猜一个。
    mode_err = C.record_mode_error(cfg)
    if mode_err:
        errors.append(mode_err)
    elif cfg.get("record_mode") == "mixed":
        if M.hm_to_min(cfg.get("record_mode_day_from"), 7 * 60) == \
                M.hm_to_min(cfg.get("record_mode_day_to"), 19 * 60):
            warnings.append("白天时段起止相同，等于全天都按「连续录像」要求判定，"
                            "「夜间侦测放过」不会生效；如果你的夜间是侦测录像，请把起止分开")
    root = cfg.get("record_root") or ""
    if not root:
        warnings.append("录像目录为空，监控不会工作")
    elif not os.path.isdir(root):
        warnings.append("录像目录当前不存在：%s（保存后配置生效，但需要目录可用才会开始监控）" % root)
    elif not os.access(root, os.R_OK | os.X_OK):
        warnings.append("当前用户读不到该目录，请到「应用中心 → 本应用 → 设置 → 授权目录」"
                        "添加并设为可读，然后重启应用")

    chans = cfg.get("enabled_push_channels") or []
    if not chans:
        warnings.append("没有启用任何推送渠道，告警只会写日志")
    # 整点推送高峰：飞书自定义机器人共用内置应用（appID 1500），整点常被平台限流
    # 日报关掉时这条提醒没有意义（用户本来就不发）
    try:
        if cfg.get("daily_report_enabled", True):
            hh, mm = [int(x) for x in str(cfg.get("daily_report_time") or "08:00").split(":")]
            if mm == 0 and "feishu" in chans:
                warnings.append("日报时间设为整点 %02d:00：飞书在整点推送高峰常返回 11232/11233 限流，"
                                "建议错峰到 %02d:%02d（失败也会自动补发，但错峰更省事）" % (hh, hh, 5))
    except Exception:
        pass

    # v1.0.16：日报统计区间 / 告警推送计划的说明性提示
    try:
        if M.hm_to_min(cfg.get("report_window_from"), 0) == \
                M.hm_to_min(cfg.get("report_window_to"), 23 * 60 + 59):
            warnings.append("日报统计区间起止相同（%s），会按整整 24 小时统计"
                            % cfg.get("report_window_from"))
        all_days = list(cfg.get("alert_push_weekdays") or []) == list(C.WEEKDAY_ALL)
        if not (all_days and M.window_text(cfg.get("alert_window_from"),
                                           cfg.get("alert_window_to")) == "全天"):
            warnings.append("告警推送计划：%s —— 时段外只检测并写日志，不推送；"
                            "进入时段后的第一轮扫描会立刻补发" % M.push_plan_text(cfg))
    except Exception:
        pass
    for ch in chans:
        if ch == "email":
            w, e = _validate_email(cfg)
            warnings.extend(w)
            errors.extend(e)
            continue
        if not (cfg.get("%s_webhook" % ch) or "").strip():
            warnings.append("已启用 %s 但 webhook 为空" % C.PUSH_LABEL.get(ch, ch))
        elif not str(cfg.get("%s_webhook" % ch)).startswith("http"):
            errors.append("%s webhook 必须以 http(s):// 开头" % C.PUSH_LABEL.get(ch, ch))

    # ---- 备份（v1.1.0）----
    bdir = (cfg.get("backup_dir") or "").strip()
    if cfg.get("backup_enabled") and not bdir:
        warnings.append("已开启定时备份，但没填备份目录 —— 定时任务不会执行")
    if bdir:
        if not os.path.isabs(bdir):
            errors.append("备份目录必须是绝对路径（如 /vol3/1000/backup/erm）")
        elif not os.path.isdir(bdir):
            warnings.append("备份目录当前不存在：%s（第一次备份时会自动创建）" % bdir)
        elif not os.access(bdir, os.W_OK):
            warnings.append("备份目录不可写：%s（检查应用授权目录）" % bdir)
    bad_extras = [p for p in (cfg.get("backup_extra_files") or []) if not os.path.isabs(p)]
    if bad_extras:
        warnings.append("额外文件里有非绝对路径，备份时会跳过：%s" % "、".join(bad_extras[:3]))
    return warnings, errors


def _validate_email(cfg):
    """邮件渠道的配置体检。服务器/发件人/收件人属于「填错必然发不出去」，直接阻止保存。"""
    warnings, errors = [], []
    host = (cfg.get("email_smtp_host") or "").strip()
    user = (cfg.get("email_smtp_user") or "").strip()
    sender = (cfg.get("email_from") or "").strip() or user
    tos = cfg.get("email_to") or []
    if not host:
        errors.append("已启用电子邮件但 SMTP 服务器为空")
    elif " " in host or host.count(".") == 0:
        errors.append("SMTP 服务器看起来不是主机名：%s" % host)
    if not sender:
        warnings.append("已启用电子邮件但发件人为空（多数邮箱要求发件人 = 登录账号）")
    elif not C.is_mail_addr(sender):
        errors.append("发件人地址无效：%s" % sender)
    if not tos:
        errors.append("已启用电子邮件但没有有效的收件人")
    if not (cfg.get("email_smtp_pass") or "").strip():
        warnings.append("SMTP 密码/授权码为空（很多邮件服务器要求鉴权，会发不出去；"
                        "QQ/163 等要用「授权码」而不是登录密码）")
    if user and not C.is_mail_addr(user):
        warnings.append("SMTP 登录账号不是邮箱格式：%s（部分服务器允许，多数不允许）" % user)
    return warnings, errors


def cfg_public(cfg):
    """给前端的配置（webhook 照实返回：页面本身在 fnOS 登录态之后；日志里才做遮蔽）。"""
    out = dict(cfg)
    return out


def snapshot():
    """只读副本（给前端/日志用，避免调用方拿到内部对象）。"""
    with LOCK:
        return json.loads(json.dumps(STATE, ensure_ascii=False))


def get_cfg():
    with LOCK:
        return dict(CFG)


def reload_cfg():
    """从磁盘重新读配置。恢复备份后必须走这里，否则内存里的 CFG 还是旧的。"""
    global CFG
    with LOCK:
        CFG = C.load_config()
        return dict(CFG)


def get_state():
    return STATE


def next_scan_eta():
    last = snapshot().get("last_check") or 0
    iv = max(30, int(CFG.get("scan_interval") or 300))
    return max(0, int(last + iv - time.time()))


def next_daily_str(cfg):
    """指标区「下次日报」。日报开关关掉时明确显示「已关闭」，不要留个假时间。"""
    if not cfg.get("daily_report_enabled", True):
        return "已关闭"
    nxt = M.next_report_dt(cfg)
    return nxt.strftime("%Y-%m-%d %H:%M") if nxt else "已关闭"


def status_payload():
    cfg = get_cfg()
    st = snapshot()
    last_res = st.get("last_result") or {}
    warnings, errors = validate(cfg)
    return {
        "ok": True,
        "app": "EasyNVR 录像监控",
        "version": VERSION,
        "mode": "fnos-gateway",
        "pid": os.getpid(),
        "uptime_sec": int(time.time()) - STARTED_AT,
        "paths": {"app": APP_DEST, "var": str(C.VAR_DIR), "socket": SOCK_PATH,
                  "config": str(C.CONFIG_FILE), "log": str(C.LOG_DIR / "monitor.log")},
        "config": cfg_public(cfg),
        "state": st,
        "channels": last_res.get("channels") or [],
        "layout": last_res.get("layout") or cfg.get("channel_layout") or "camera",
        # v1.1.7：源端录像方式（判据前提）—— 直接读配置，不走 last_result 缓存：
        # 它本来就是「配置属性」，不是扫描结果，缓存一份只会让页面显示和下拉框打架。
        "record_mode": M.record_mode(cfg),
        "record_mode_set": M.record_mode_chosen(cfg),
        "record_mode_text": M.record_mode_text(cfg),
        "channel_count": len(last_res.get("channels") or []),
        "root": last_res.get("root") or cfg.get("record_root") or "",
        "error": last_res.get("error") or "",
        "alerting": last_res.get("alerting", 0),
        "last_check": st.get("last_check") or 0,
        "last_check_str": M.fmt_ts(cfg, st.get("last_check") or 0),
        "next_scan_sec": next_scan_eta(),
        "next_daily_str": next_daily_str(cfg),
        "report_plan": M.report_plan_text(cfg),
        "push_plan": M.push_plan_text(cfg),
        "push_allowed": M.alert_push_allowed(cfg),
        "quiet_since": st.get("quiet_since") or 0,
        "last_report_day": st.get("last_report_day") or "",
        "warnings": warnings,
        "errors": errors,
        "backup": backup_summary(cfg, st),
    }


def backup_summary(cfg, st=None):
    """「备份」页与顶栏要的一小份摘要。除列目录数份数外不做别的 IO。"""
    st = st if isinstance(st, dict) else STATE
    ok, msg = B.dir_status(cfg)
    last_ts = st.get("last_backup_ts") or 0
    return {
        "enabled": bool(cfg.get("backup_enabled")),
        "dir": cfg.get("backup_dir") or "",
        "dir_ok": ok,
        "dir_msg": msg,
        "time": cfg.get("backup_time"),
        "weekdays": cfg.get("backup_weekdays"),
        "keep": cfg.get("backup_keep"),
        "include_state": bool(cfg.get("backup_include_state")),
        "extra_count": len(cfg.get("backup_extra_files") or []),
        "count": B.count_backups(cfg),
        "next_str": B.next_backup_str(cfg, st),
        "last_day": st.get("last_backup_day") or "",
        "last_file": st.get("last_backup_file") or "",
        "last_str": M.fmt_ts(cfg, last_ts) if last_ts else "—",
    }


# ---------------------------------------------------------------- API

def handle_action(body, query):
    act = (body.get("action") or (query.get("action", [""])[0]) or "status").strip()

    if act in ("info", "status"):
        return status_payload()

    if act == "scan":
        # 页面上的「立即检测」= 人就在旁边看着，默认绕过推送计划强制发一次
        force = body.get("force")
        force_alert = True if force is None else bool(force)
        with SCAN_LOCK:
            res = M.run_scan(get_cfg(), STATE, force_alert=force_alert)
        out = status_payload()
        out["scan"] = res["result"]
        out["events"] = [{"scope": e["scope"], "kind": e["kind"]} for e in res["events"]]
        out["pushed"] = res["pushed"]
        return out

    if act == "save_config":
        global CFG
        incoming = body.get("config") or {}
        if not isinstance(incoming, dict):
            return {"ok": False, "error": "config 必须是对象"}
        merged = dict(get_cfg())
        old_cfg = dict(merged)                  # 判据字段是否变化，要和保存前比
        merged.update(incoming)
        new_cfg = C.normalize(merged)
        warnings, errors = validate(new_cfg)
        if errors:
            return {"ok": False, "error": "；".join(errors), "warnings": warnings}
        with LOCK:
            CFG = new_cfg
        C.save_config(new_cfg)
        # 「配置即生效」：改的是判据本身（录像方式/阈值/布局/目录）时，页面上的
        # 判定结果不能还是上一套判据算出来的。这里只重算 last_result 让页面立刻
        # 跟上，不动 alerting、不推送 —— 告警/恢复仍由下一轮正式扫描处理，
        # 免得「只是改了个阈值」就凭空发一轮通知。
        judge_keys = ("record_mode", "record_mode_motion_max_minutes",
                      "record_mode_day_from", "record_mode_day_to",
                      "alert_threshold_minutes", "channel_layout", "record_root")
        if any(old_cfg.get(k) != new_cfg.get(k) for k in judge_keys):
            try:
                with SCAN_LOCK:
                    STATE["last_result"] = M.evaluate(new_cfg, with_counts=False)
                C.save_state(STATE)
            except Exception as exc:                # 重算失败不能让保存失败
                log.warning("配置已保存，但重算判定失败：%s", exc)
        log.info("配置已更新：%s", json.dumps({k: new_cfg[k] for k in
                 ("record_root", "channel_layout", "record_mode", "alert_threshold_minutes",
                  "daily_report_enabled", "daily_report_time", "enabled_push_channels")},
                 ensure_ascii=False))
        out = status_payload()
        out["warnings"] = warnings
        out["saved"] = True
        return out

    if act == "test_push":
        cfg = get_cfg()
        text = N.test_text(cfg)
        results = N.push_all(cfg, text)
        ch = body.get("channel")
        if ch:
            if ch not in C.PUSH_CHANNELS:
                return {"ok": False, "error": "未知渠道：%s" % ch, "results": []}
            results = [N.push_one(cfg, ch, text)]
        return {"ok": all(r.get("ok") for r in results), "results": results}

    if act == "daily_now":
        with SCAN_LOCK:
            res = M.run_daily(get_cfg(), STATE, manual=True)
        return {"ok": True, "message": res["message"], "pushed": res["pushed"],
                "sent": res.get("ok", False)}

    if act == "reset_state":
        with SCAN_LOCK:
            STATE.clear()
            STATE["last_report_day"] = ""
            C.save_state(STATE)
        return {"ok": True, "msg": "运行状态已清空（下次扫描按「首次发现」处理）"}

    if act == "stats":
        rng = (body.get("range") or (query.get("range", [""])[0])
               or S.DEFAULT_RANGE)
        out = S.build(get_cfg(), rng)
        out["ok"] = True
        return out

    if act == "logs":
        n = int(body.get("lines") or query.get("lines", ["120"])[0] or 120)
        n = max(1, min(n, 2000))
        return {"ok": True, "text": tail_log(n)}

    if act == "doctor":
        return doctor_payload()

    # ---- 备份（v1.1.0）----
    if act == "backup_now":
        with LOCK:
            res = B.run_backup(get_cfg(), STATE, reason="manual", version=VERSION)
            if res.get("ok"):
                C.save_state(STATE)
        out = status_payload()
        out["backup_result"] = res
        return out

    if act == "backup_list":
        cfg = get_cfg()
        ok, msg = B.dir_status(cfg)
        return {"ok": True, "dir": cfg.get("backup_dir") or "", "dir_ok": ok, "dir_msg": msg,
                "rows": B.list_backups(cfg, limit=200)}

    if act == "backup_restore":
        name = str(body.get("file") or "")
        with LOCK:
            res = B.restore(get_cfg(), name, STATE, version=VERSION)
            if res.get("ok"):
                reload_cfg()
                C.save_state(STATE)
        out = status_payload()
        # config 整份不回给前端：前端只需要知道「恢复了什么、旧的存哪了」
        out["restore"] = {k: v for k, v in res.items() if k != "config"}
        return out

    if act == "backup_delete":
        res = B.remove(get_cfg(), str(body.get("file") or ""))
        out = status_payload()
        out["deleted"] = res
        return out

    if act == "backup_export":
        # 返回原文，前端用 Blob 落成本地文件。走 JSON 通道而不是让浏览器直接下载，
        # 是因为所有请求都过飞牛网关的 unix socket，附件响应容易被网关改写。
        res = B.export_text(get_cfg(), str(body.get("file") or ""))
        out = status_payload() if not res.get("ok") else {"ok": True}
        out["exported"] = res
        return out

    if act == "backup_import":
        # 文件内容由前端读成文本传上来：不解析 multipart，少一套出错的地方。
        # 只管收进备份目录；要套用请再点列表里的「恢复」—— 导入 ≠ 立即覆盖现网配置。
        res = B.import_file(get_cfg(), str(body.get("name") or ""),
                            body.get("content") or "", version=VERSION)
        out = status_payload()
        out["imported"] = res
        return out

    return {"ok": False, "error": "未知动作：%s" % act}


def tail_log(n=120):
    p = C.LOG_DIR / "monitor.log"
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except FileNotFoundError:
        return "（日志尚未生成）"
    except Exception as e:
        return "读取日志失败：%s" % e


def doctor_payload():
    """自检：让用户/我一眼看出"为什么没告警"。"""
    cfg = get_cfg()
    root = cfg.get("record_root") or ""
    checks = []

    def add(name, ok, detail=""):
        checks.append({"item": name, "ok": bool(ok), "detail": detail})

    add("录像目录已配置", bool(root), root or "未填写")
    add("录像目录存在", os.path.isdir(root) if root else False,
        root if os.path.isdir(root) else (root or "未填写"))
    readable = bool(root) and os.path.isdir(root) and os.access(root, os.R_OK | os.X_OK)
    add("录像目录可读", readable,
        "可读" if readable else "不可读：请到「应用中心 → 本应用 → 设置 → 授权目录」"
                                "添加该目录并设为可读，然后重启应用")
    chans = M.resolve_channels(cfg) if root and os.path.isdir(root) else []
    layout = cfg.get("channel_layout") or "camera"
    add("通道布局", True,
        {"camera": "摄像头聚合（一个摄像头=一个通道）",
         "profile": "按 profile 分开（主码流/子码流各算一个）"}.get(layout, str(layout)))
    add("识别到通道", bool(chans),
        "；".join(c["label"] if c["label"] == c["channel"]
                 else "%s ⇐ %s" % (c["label"], c["channel"]) for c in chans) or
        "没有识别到通道。EasyNVR 的录像应为 <摄像头ID>/profile_N/<日期>/，"
        "请确认「录像目录」指的正是 easynvr_rec 这一层")
    exts = set(cfg.get("video_exts") or [])
    add("录像后缀", True, " ".join(sorted(exts)))
    for ch in cfg.get("enabled_push_channels") or []:
        if ch == "email":
            sec = {"ssl": "SSL", "starttls": "STARTTLS", "none": "明文"}.get(
                cfg.get("email_smtp_security"), cfg.get("email_smtp_security"))
            add("SMTP 服务器", bool(cfg.get("email_smtp_host")),
                "%s:%s（%s）" % (cfg.get("email_smtp_host") or "未填写",
                                cfg.get("email_smtp_port"), sec))
            add("收件人", bool(cfg.get("email_to")),
                "、".join(cfg.get("email_to") or []) or "未配置")
            add("SMTP 授权码", True, "已配置" if cfg.get("email_smtp_pass") else "未配置")
            continue
        add("%s webhook" % C.PUSH_LABEL.get(ch, ch), bool(cfg.get("%s_webhook" % ch)),
            C.mask_secret(cfg.get("%s_webhook" % ch)))
    add("钉钉加签密钥", True, "已配置" if cfg.get("dingtalk_secret") else "未配置（用「自定义关键词」时正常）")
    add("Python", True, "%s（%s）" % (sys.version.split()[0], sys.executable))
    return {"ok": all(c["ok"] for c in checks if c["item"] not in
                      ("钉钉加签密钥", "录像目录已配置", "SMTP 授权码")), "checks": checks}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "ERMGateway/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _strip(self):
        parsed = urlparse(self.path)
        p = parsed.path
        if p.startswith(GATEWAY_PREFIX):
            p = p[len(GATEWAY_PREFIX):]
        if not p or p == "/":
            p = "/index.html"
        return p, parsed.query

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b""
            if not raw:
                return {}
            obj = json.loads(raw.decode("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def do_GET(self):
        p, q = self._strip()
        if p == "/api" or p.startswith("/api"):
            qd = parse_qs(q)
            try:
                self._json(handle_action({"action": (qd.get("action", [""])[0])}, qd))
            except Exception as e:
                log.error("API 异常: %s\n%s", e, traceback.format_exc())
                self._json({"ok": False, "error": str(e)}, 500)
            return
        self._static(p)

    def do_POST(self):
        p, q = self._strip()
        if not (p == "/api" or p.startswith("/api")):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n > MAX_BODY_BYTES:
            self.close_connection = True
            self._json({"ok": False, "error": "请求体太大（%s，上限 %s）"
                        % (B._hsize(n), B._hsize(MAX_BODY_BYTES))}, 413)
            return
        body = self._body()
        try:
            self._json(handle_action(body, parse_qs(q)))
        except Exception as e:
            log.error("API 异常: %s\n%s", e, traceback.format_exc())
            self._json({"ok": False, "error": str(e)}, 500)

    do_HEAD = do_GET

    def _static(self, rel):
        if ".." in rel:
            self._json({"ok": False, "error": "bad request"}, 400)
            return
        rel = rel.lstrip("/")
        fpath = os.path.join(WWW, rel)
        if not os.path.realpath(fpath).startswith(os.path.realpath(WWW)):
            self._json({"ok": False, "error": "forbidden"}, 403)
            return
        if not os.path.isfile(fpath):
            self._json({"ok": False, "error": "not found: %s" % rel}, 404)
            return
        ctype = MIME.get(os.path.splitext(fpath)[1].lower(), "application/octet-stream")
        with open(fpath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True
    # 关键：HTTP/1.1 keep-alive 下每个连接都有一个 handler 线程阻塞在 readline 上，
    # 默认 block_on_close=True 会让 server_close() 无限期 join 这些线程 ——
    # 表现就是「收到 SIGTERM、日志也写了停止，但进程不退出」，应用中心一直卡在停止中。
    block_on_close = False

    def get_request(self):
        request, client_address = super().get_request()
        return request, ("unix", 0)


# ---------------------------------------------------------------- 启动

def acquire_singleton():
    """单实例锁（flock）。

    为什么需要：pid 文件不靠谱。升级时 target 被整体替换、进程被 SIGKILL、
    或者 pid 文件被清掉，都会出现"旧进程还在跑 + 新进程又起来"，于是两个进程
    各自扫描、各自发通知 —— 实测踩过（同一条恢复通知发了两遍）。
    flock 随进程消失自动释放，比 pid 文件可靠。
    """
    var = str(C.VAR_DIR)
    os.makedirs(var, exist_ok=True)
    lock_path = os.path.join(var, APP_NAME + ".lock")
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh                      # 必须在进程存活期间保持引用


def start_monitor():
    th = threading.Thread(
        target=M.monitor_loop,
        args=(get_cfg, get_state, STOP_EVENT, None, SCAN_LOCK),
        name="erm-monitor", daemon=True)
    th.start()
    return th


def start_backup():
    th = threading.Thread(target=backup_loop, name="erm-backup", daemon=True)
    th.start()
    return th


def backup_loop():
    """定时备份：每 15 秒看一眼到点没有。

    单独一个线程而不是挂进 monitor_loop —— 扫描要持 SCAN_LOCK，遇到超大目录可能拖上几分钟，
    备份不该排在它后面（凌晨的备份不能因为一次扫描推迟到天亮）。两者写的 state 键不重叠。
    """
    while not STOP_EVENT.is_set():
        try:
            cfg = get_cfg()
            if B.backup_due(cfg, STATE):
                with LOCK:
                    res = B.run_backup(cfg, STATE, reason="scheduled", version=VERSION)
                    if res.get("ok"):
                        C.save_state(STATE)
                if res.get("ok"):
                    log.info("定时备份完成：%s（%s 字节）", res.get("file"), res.get("size"))
                    if res.get("removed"):
                        log.info("按保留份数清理：%s", "、".join(res["removed"]))
                else:
                    log.warning("定时备份失败：%s", res.get("error"))
        except Exception as e:
            log.exception("备份线程异常: %s", e)
        STOP_EVENT.wait(15)


def main():
    C.VAR_DIR.mkdir(parents=True, exist_ok=True)

    lock_fh = acquire_singleton()
    if lock_fh is None:
        msg = "已有实例在运行，本次启动直接退出（单实例锁 %s.lock）" % APP_NAME
        log.warning(msg)
        sys.stderr.write(msg + "\n")
        return 0

    if os.path.exists(SOCK_PATH):
        try:
            os.unlink(SOCK_PATH)
        except OSError:
            pass

    def on_term(signum, frame):
        log.info("收到信号 %s，停止服务", signum)
        STOP_EVENT.set()
        # 兜底：3 秒后无论如何强退（防止某个 handler 线程还卡在 keep-alive 读上）
        threading.Timer(3.0, lambda: os._exit(0)).start()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    start_monitor()
    start_backup()

    try:
        server = UnixHTTPServer(SOCK_PATH, Handler)
    except OSError as e:
        log.error("socket 绑定失败 %s: %s", SOCK_PATH, e)
        sys.stderr.write("bind failed: %s\n" % e)
        return 1
    os.chmod(SOCK_PATH, 0o660)
    log.info("网关就绪 socket=%s pid=%s version=%s", SOCK_PATH, os.getpid(), VERSION)
    try:
        server.serve_forever()
    finally:
        STOP_EVENT.set()
        try:
            lock_fh.close()
        except Exception:
            pass
        server.server_close()
        if os.path.exists(SOCK_PATH):
            try:
                os.unlink(SOCK_PATH)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
