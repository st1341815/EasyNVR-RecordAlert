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
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_NAME = "erm"
GATEWAY_PREFIX = "/app/" + APP_NAME
VERSION = "1.0.7"

SELF = os.path.abspath(__file__)
APP_DEST = os.path.dirname(SELF)           # app/ 的内容直接铺到 target 根
WWW = os.path.join(APP_DEST, "www")
SOCK_PATH = os.path.join(APP_DEST, APP_NAME + ".sock")

sys.path.insert(0, APP_DEST)
import config as C          # noqa: E402
import monitor as M         # noqa: E402
import notify as N          # noqa: E402

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
    for ch in chans:
        if not (cfg.get("%s_webhook" % ch) or "").strip():
            warnings.append("已启用 %s 但 webhook 为空" % C.PUSH_LABEL.get(ch, ch))
        elif not str(cfg.get("%s_webhook" % ch)).startswith("http"):
            errors.append("%s webhook 必须以 http(s):// 开头" % C.PUSH_LABEL.get(ch, ch))
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


def get_state():
    return STATE


def next_scan_eta():
    last = snapshot().get("last_check") or 0
    iv = max(30, int(CFG.get("scan_interval") or 300))
    return max(0, int(last + iv - time.time()))


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
        "layout": last_res.get("layout") or cfg.get("channel_layout") or "auto",
        "channel_count": len(last_res.get("channels") or []),
        "root": last_res.get("root") or cfg.get("record_root") or "",
        "error": last_res.get("error") or "",
        "alerting": last_res.get("alerting", 0),
        "last_check": st.get("last_check") or 0,
        "last_check_str": M.fmt_ts(cfg, st.get("last_check") or 0),
        "next_scan_sec": next_scan_eta(),
        "next_daily_str": M.next_report_dt(cfg).strftime("%Y-%m-%d %H:%M"),
        "last_report_day": st.get("last_report_day") or "",
        "warnings": warnings,
        "errors": errors,
    }


# ---------------------------------------------------------------- API

def handle_action(body, query):
    act = (body.get("action") or (query.get("action", [""])[0]) or "status").strip()

    if act in ("info", "status"):
        return status_payload()

    if act == "scan":
        with SCAN_LOCK:
            res = M.run_scan(get_cfg(), STATE, force_alert=bool(body.get("force")))
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
        merged.update(incoming)
        new_cfg = C.normalize(merged)
        warnings, errors = validate(new_cfg)
        if errors:
            return {"ok": False, "error": "；".join(errors), "warnings": warnings}
        with LOCK:
            CFG = new_cfg
        C.save_config(new_cfg)
        log.info("配置已更新：%s", json.dumps({k: new_cfg[k] for k in
                 ("record_root", "monitor_channels", "alert_threshold_minutes",
                  "daily_report_time", "enabled_push_channels")}, ensure_ascii=False))
        out = status_payload()
        out["warnings"] = warnings
        out["saved"] = True
        return out

    if act == "test_push":
        cfg = get_cfg()
        text = N.test_text(cfg)
        results = N.push_all(cfg, text)
        if body.get("channel"):
            results = [N.push_one(cfg, body["channel"], text)]
        return {"ok": all(r.get("ok") for r in results), "results": results}

    if act == "daily_now":
        with SCAN_LOCK:
            res = M.run_daily(get_cfg(), STATE)
        return {"ok": True, "message": res["message"], "pushed": res["pushed"]}

    if act == "reset_state":
        with SCAN_LOCK:
            STATE.clear()
            STATE["last_report_day"] = ""
            C.save_state(STATE)
        return {"ok": True, "msg": "运行状态已清空（下次扫描按「首次发现」处理）"}

    if act == "logs":
        n = int(body.get("lines") or query.get("lines", ["120"])[0] or 120)
        n = max(1, min(n, 2000))
        return {"ok": True, "text": tail_log(n)}

    if act == "doctor":
        return doctor_payload()

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
    layout = chans[0].get("layout") if chans else M.detect_layout(Path(root), cfg) \
        if root and os.path.isdir(root) else "-"
    add("通道布局", True,
        {"camera": "摄像头聚合（一个摄像头=一个通道）",
         "profile": "按 profile 分开（主码流/子码流各算一个）",
         "flat": "单层目录（channel_include 规则）"}.get(layout, str(layout)))
    add("识别到通道", bool(chans),
        "；".join(c["label"] if c["label"] == c["channel"]
                 else "%s ⇐ %s" % (c["label"], c["channel"]) for c in chans) or
        "没有识别到通道。EasyNVR 应是 <摄像头ID>/profile_N/<日期>/，也可在配置里手填通道")
    exts = set(cfg.get("video_exts") or [])
    add("录像后缀", True, " ".join(sorted(exts)))
    for ch in cfg.get("enabled_push_channels") or []:
        add("%s webhook" % C.PUSH_LABEL.get(ch, ch), bool(cfg.get("%s_webhook" % ch)),
            C.mask_secret(cfg.get("%s_webhook" % ch)))
    add("钉钉加签密钥", True, "已配置" if cfg.get("dingtalk_secret") else "未配置（用「自定义关键词」时正常）")
    add("Python", True, "%s（%s）" % (sys.version.split()[0], sys.executable))
    return {"ok": all(c["ok"] for c in checks if c["item"] not in
                      ("钉钉加签密钥", "录像目录已配置")), "checks": checks}


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
