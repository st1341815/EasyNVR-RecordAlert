#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EasyNVR 录像监控 —— 配置读写（强类型归一化）。

设计要点（对应文档评审 A7 / B1 / B2）：
  · 所有运行期数据落在 $TRIM_PKGVAR（= /volX/@appdata/<app>），不放 target：
    target 在升级时会被整体替换，放里面等于每次升级都丢配置；且 target 文件多为 0644，
    本机任何用户都能读到 webhook（webhook 等同口令）。
  · 配置文件是外部输入（安装向导 / 设置页 / 手改 / 旧版本遗留），
    读进来第一件事就是强类型化，绝不指望下游自己转。
"""

import json
import os
import tempfile
from pathlib import Path

APP_NAME = "erm"

DEFAULT_CONFIG = {
    # 录像目录（安装向导不再询问，装好后在应用页面里填）
    "record_root": "",
    # 通道布局。EasyNVR 的录像目录只有一种形态：
    #   <录像根目录>/<摄像头随机ID>/profile_<通道号>/<YYYYMMDD>/<录像文件>
    # 所以通道一律自动发现，不需要（也不支持）手填通道清单或通配规则。
    #   camera  = 一个摄像头算一个通道（聚合它下面所有 profile_N；默认，
    #             子码流不录制时不会误报）
    #   profile = 每个 <ID>/profile_N 各算一个通道（主码流挂了但子码流还在时能发现）
    "channel_layout": "camera",
    # 摄像头别名：{"O27fqV6dYvs3T": "大门", "O27fqV6dYvs3T/profile_1": "大门主码流"}
    "channel_aliases": {},
    # 扫描与告警
    "scan_interval": 300,              # 秒，>=30
    "alert_threshold_minutes": 60,     # 分钟，>=1
    "alert_silence_minutes": 120,      # 分钟，>=0（0 = 同一异常只告警一次，恢复后才重新告警）
    "notify_recovery": True,           # 恢复时补一条「已恢复」
    # 日报
    "daily_report_time": "08:00",      # HH:MM（本地时区），到点即发、当天只发一次
    # 推送
    "enabled_push_channels": [],       # 装了没配 webhook 就不推送，只写日志（避免每轮报失败）
    "dingtalk_webhook": "",
    "dingtalk_secret": "",             # 钉钉「加签」密钥；用「自定义关键词」时留空
    "feishu_webhook": "",
    "wechat_webhook": "",
    # 录像文件后缀（EasyNVR 默认 mp4；HLS 模式会有 .ts）
    "video_exts": [".mp4", ".ts", ".mov", ".mkv", ".flv", ".avi", ".m4v", ".mpg", ".mpeg"],
    # 时区：告警时间文案与「昨天」的边界都按它算
    "timezone": "Asia/Shanghai",
}

PUSH_CHANNELS = ("dingtalk", "feishu", "wechat")
PUSH_LABEL = {"dingtalk": "钉钉", "feishu": "飞书", "wechat": "企业微信"}


def _app_var() -> Path:
    """应用数据目录（$TRIM_PKGVAR），本地开发时回落 /var/apps/<app>/var。"""
    p = os.environ.get("TRIM_PKGVAR")
    if p:
        return Path(p)
    return Path("/var/apps") / APP_NAME / "var"


VAR_DIR = _app_var()
CONFIG_FILE = VAR_DIR / "config.json"
STATE_FILE = VAR_DIR / "state.json"
LOG_DIR = VAR_DIR / "logs"


# ---------------------------------------------------------------- 类型归一化

def as_int(value, default, lo=None, hi=None):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(n, lo)
    if hi is not None:
        n = min(n, hi)
    return n


def as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "启用"):
        return True
    if s in ("0", "false", "no", "off", "停用", ""):
        return False
    return default


def as_list(value):
    """'a,b' / 'a，b' / ['a','b'] / 'a' → ['a','b']。

    注意：绝对不能用 list(str)，那会把 'a,b' 拆成单字符。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = str(value).replace("，", ",").replace(";", ",").replace("；", ",").split(",")
    out = []
    for item in raw:
        s = str(item).strip()
        if s and s not in out:
            out.append(s)
    return out


def as_alias_map(value):
    """别名表：dict，或 "ID=名称" 的多行文本（页面上就是多行文本框）。"""
    out = {}
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, str):
        pairs = []
        for line in value.replace("；", ";").splitlines():
            for seg in line.replace(";", "\n").split("\n"):
                if "=" in seg:
                    pairs.append(tuple(seg.split("=", 1)))
                elif "：" in seg:
                    pairs.append(tuple(seg.split("：", 1)))
        items = pairs
    else:
        items = []
    for k, v in items:
        key = str(k or "").strip().strip("/")
        name = str(v or "").strip()
        if key and name:
            out[key] = name
    return out


def norm_hhmm(value, default="08:00"):
    s = str(value or "").strip().replace("：", ":")
    parts = s.split(":")
    if len(parts) < 2:
        return default
    h = as_int(parts[0], -1, 0, 23)
    m = as_int(parts[1], -1, 0, 59)
    if h < 0 or m < 0:
        return default
    return "%02d:%02d" % (h, m)


def normalize(raw):
    """把任意来源的 dict 归一成强类型配置。"""
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if v is not None})

    cfg["record_root"] = str(cfg.get("record_root") or "").strip().rstrip("/")
    # 已废弃的两个键（v1.0.8）：手工通道清单 monitor_channels、单层目录发现规则
    # channel_include。EasyNVR 目录里没有 stream_* 这种东西，通道一律按
    # <摄像头ID>/profile_N 自动发现 —— 旧配置里残留的这两个键在这里直接丢掉，
    # 下次保存时 config.json 也就干净了。
    for legacy in ("monitor_channels", "channel_include"):
        cfg.pop(legacy, None)
    layout = str(cfg.get("channel_layout") or "camera").strip().lower()
    # 旧值 auto（自动识别）与 flat（单层 stream_* 目录）都归到 camera：
    # 真实 EasyNVR 布局下 auto 本来就等价于 camera，flat 已经没有意义。
    cfg["channel_layout"] = layout if layout in ("camera", "profile") else "camera"
    cfg["channel_aliases"] = as_alias_map(cfg.get("channel_aliases"))

    cfg["scan_interval"] = as_int(cfg.get("scan_interval"), 300, 30)
    cfg["alert_threshold_minutes"] = as_int(cfg.get("alert_threshold_minutes"), 60, 1)
    cfg["alert_silence_minutes"] = as_int(cfg.get("alert_silence_minutes"), 120, 0)
    cfg["notify_recovery"] = as_bool(cfg.get("notify_recovery"), True)
    cfg["daily_report_time"] = norm_hhmm(cfg.get("daily_report_time"), "08:00")

    channels = [c for c in as_list(cfg.get("enabled_push_channels")) if c in PUSH_CHANNELS]
    if not channels:
        channels = []
    cfg["enabled_push_channels"] = channels

    for key in ("dingtalk_webhook", "dingtalk_secret", "feishu_webhook", "wechat_webhook", "timezone"):
        cfg[key] = str(cfg.get(key) or "").strip()

    exts = []
    for e in as_list(cfg.get("video_exts")):
        if not e.startswith("."):
            e = "." + e
        exts.append(e.lower())
    cfg["video_exts"] = exts or list(DEFAULT_CONFIG["video_exts"])
    return cfg


# ---------------------------------------------------------------- 读写

def load_config():
    raw = {}
    try:
        if CONFIG_FILE.is_file():
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return normalize(raw)


def _atomic_write(path: Path, text: str, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_config(cfg):
    cfg = normalize(cfg)
    _atomic_write(CONFIG_FILE, json.dumps(cfg, indent=2, ensure_ascii=False), 0o600)
    return cfg


def load_state():
    try:
        if STATE_FILE.is_file():
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(st, dict):
                return st
    except Exception:
        pass
    return {}


def save_state(state):
    _atomic_write(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False), 0o600)


def mask_secret(url: str) -> str:
    """给前端回显用：只保留 host + 前 6 位路径，避免整条 webhook 到处打印。"""
    url = str(url or "")
    if not url:
        return ""
    try:
        u = urllib_parse(url)
        path = u.path or ""
        shown = path[:12] + ("…" if len(path) > 12 else "")
        return "%s://%s%s" % (u.scheme, u.netloc, shown)
    except Exception:
        return url[:12] + "…"


def urllib_parse(url):
    from urllib.parse import urlparse
    return urlparse(url)
