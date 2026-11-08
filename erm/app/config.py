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
    # ---- 源端 EasyNVR 录像方式（v1.1.7，必选）----
    # 这是「判据的物理前提」，不是显示项：工具判断断流靠的是「多久没有新录像文件」，
    # 而这个时长在三种录像方式下含义完全不同，选错就会天天误报或整夜漏报。
    #   continuous = 全天连续录像（定时录像）：没新文件 = 一定出问题了
    #   motion     = 移动侦测录像：没新文件是常态，只能用「超长静默」兜底
    #   mixed      = 白天连续 + 夜间侦测：白天照 continuous 判，夜间按 motion 放过
    # 空字符串 = 还没选（升级上来的老配置）。运行期按 continuous 处理，但设置页会
    # 拦住保存并提示 —— 宁可让用户明确选一次，也不要猜。
    "record_mode": "",
    # motion 模式（以及 mixed 的夜间）最长可接受静默。1440 = 24 小时：
    # 移动侦测下一天一个文件都没有，基本可以确定是摄像头/录像链路出问题了。
    "record_mode_motion_max_minutes": 1440,
    # mixed 模式的「白天」区间：只有这段时间按连续录像要求
    "record_mode_day_from": "07:00",
    "record_mode_day_to": "19:00",
    # ---- 源端 EasyNVR 服务巡检（v1.2.0 P0）----
    # 录像文件的 mtime 只能证明「有没有录」，证明不了「EasyNVR 自己还活着」。
    # 服务挂了 / 授权过期时，所有通道会一起静默，看起来跟「家里没人」一模一样。
    # GET /health 不需要凭据，顺手探一次就能把这两件事分开；填了地址并打开才会真去探。
    "nvr_health_enabled": False,
    "nvr_base_url": "",                # 如 http://192.168.11.6:10000（只保留 scheme://host:port）
    "nvr_health_interval": 300,        # 秒，>=60
    "nvr_expire_warn_days": 7,         # 授权剩余 ≤ N 天开始提醒，0 = 不提醒
    # 扫描与告警
    "scan_interval": 300,              # 秒，>=30
    "alert_threshold_minutes": 60,     # 分钟，>=1
    "alert_silence_minutes": 120,      # 分钟，>=0（0 = 同一异常只告警一次，恢复后才重新告警）
    "notify_recovery": True,           # 恢复时补一条「已恢复」
    # 日报
    "daily_report_enabled": True,      # 关掉后不再自动发日报（「补发昨日日报」按钮仍可用）
    "daily_report_time": "08:00",      # HH:MM（本地时区），到点即发、当天只发一次
    # 日报统计区间：HH:MM 含端点，from == to 表示整天 24 小时。
    #   to > from  → 同一个自然日内，如 08:00–18:00
    #   to <= from → 跨天，如 08:00–次日 08:00（填 08:00 与 08:00 即 24 小时）
    "report_window_from": "00:00",
    "report_window_to": "23:59",
    # 日报推送日：1=周一 … 7=周日。空列表按「每天」处理（不会因为漏填而永不推送）
    "report_weekdays": [1, 2, 3, 4, 5, 6, 7],
    # 告警推送计划：只在「这些天的这个时段」内真发推送，其余时间照常检测、只写日志
    # （v1.0.16：夜里/周末不想被吵，就把它调成工作日 08:00–18:00）
    "alert_push_weekdays": [1, 2, 3, 4, 5, 6, 7],
    "alert_window_from": "00:00",      # 起止相同 = 全天；from > to 表示跨夜，如 22:00–07:00
    "alert_window_to": "23:59",
    # 推送
    "enabled_push_channels": [],       # 装了没配 webhook 就不推送，只写日志（避免每轮报失败）
    "dingtalk_webhook": "",
    "dingtalk_secret": "",             # 钉钉「加签」密钥；用「自定义关键词」时留空
    "feishu_webhook": "",
    "wechat_webhook": "",
    # 邮件（SMTP 直发）。飞牛系统自带的「通知邮箱」不对外提供发送接口，
    # 所以这里自己连 SMTP；想用同一个邮箱，就把授权码在这里再填一次。
    "email_smtp_host": "",             # 如 smtp.qq.com / smtp.163.com
    "email_smtp_port": 465,            # 465=SSL  587/25=STARTTLS（按加密方式自动纠正）
    "email_smtp_security": "ssl",      # ssl | starttls | none
    "email_smtp_user": "",             # 登录账号（一般是邮箱地址）
    "email_smtp_pass": "",             # 授权码/应用专用密码（不是登录密码）
    "email_from": "",                  # 发件人，空 = 用 email_smtp_user
    "email_to": [],                    # 收件人列表
    "email_subject_prefix": "[EasyNVR 录像监控]",
    # ---- 备份（v1.1.0）----
    # 备份的就是这份 config.json（webhook、别名、阈值全在里面），外加可选的运行状态
    # 与任意额外文件。目录故意不给默认值：宁可让用户明确选一块盘，也不要往系统盘乱写。
    "backup_enabled": False,           # 关掉后只能手动备份
    "backup_dir": "",                  # 绝对路径，如 /vol3/1000/backup/erm
    "backup_time": "03:00",            # HH:MM，到点即备份，当天只备一次
    "backup_weekdays": [1, 2, 3, 4, 5, 6, 7],
    "backup_keep": 30,                 # 只保留最近 N 份，0 = 不清理
    "backup_include_config": True,     # config.json（必留，否则备份没意义，但仍给你关的自由）
    "backup_include_state": False,     # state.json：告警状态与「今天日报发过没」，一般不用备
    "backup_extra_files": [],          # 额外文件，每行一个绝对路径
    # 录像文件后缀（EasyNVR 默认 mp4；HLS 模式会有 .ts）
    "video_exts": [".mp4", ".ts", ".mov", ".mkv", ".flv", ".avi", ".m4v", ".mpg", ".mpeg"],
    # 时区：告警时间文案与「昨天」的边界都按它算
    "timezone": "Asia/Shanghai",
}

PUSH_CHANNELS = ("dingtalk", "feishu", "wechat", "email")
PUSH_LABEL = {"dingtalk": "钉钉", "feishu": "飞书", "wechat": "企业微信", "email": "电子邮件"}

# 邮件加密方式 → 默认端口（用户只改加密方式时把端口纠正到该协议的标准端口）
SMTP_SECURITY_PORTS = {"ssl": 465, "starttls": 587, "none": 25}
SMTP_SECURITIES = tuple(SMTP_SECURITY_PORTS)

# 源端 EasyNVR 录像方式（v1.1.7）：判据的物理前提，必选
RECORD_MODES = ("continuous", "motion", "mixed")
RECORD_MODE_LABEL = {
    "continuous": "全天连续录像",
    "motion": "移动侦测录像",
    "mixed": "白天连续 + 夜间侦测",
}
# 一句话说清每种方式对「没有新录像」的解释 —— 设置页提示与告警文案共用
RECORD_MODE_NOTE = {
    "continuous": "任何时段都持续写文件，没有新录像即可判定为异常",
    "motion": "画面有变化才录，没有新录像是常态；只用超长静默兜底",
    "mixed": "白天按连续录像要求，夜间按侦测放过（夜间没人是正常的）",
}


def record_mode_error(cfg):
    """必选项校验：没选/选了个不认识的值就返回错误文案，正常返回空串。

    单独抽成函数是为了让自检能直接测「必选」这件事，不用起 HTTP 服务。
    """
    mode = str((cfg or {}).get("record_mode") or "").strip().lower()
    if not mode:
        return ("「源端 EasyNVR 录像方式」是必选项，请选择："
                "全天连续录像 / 移动侦测录像 / 白天连续 + 夜间侦测")
    if mode not in RECORD_MODES:
        return "「源端 EasyNVR 录像方式」取值非法：%s（应为 %s）" % (
            mode, " / ".join(RECORD_MODES))
    return ""


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
        # 转换失败时 default 也要过一遍 clamp，否则会出现「默认值 25 被夹成 1」
        # （as_int("abc", 25, 1, 65535) 曾经返回 1）。default 本来就该是合法值。
        n = default
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


WEEKDAY_NAMES = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
WEEKDAY_ALL = (1, 2, 3, 4, 5, 6, 7)
WEEKDAY_WORK = (1, 2, 3, 4, 5)
WEEKDAY_WEEKEND = (6, 7)


def norm_weekdays(value, default=WEEKDAY_ALL):
    """推送日：[1..7]（1=周一，与 datetime.isoweekday() 对齐）。

    接受 [1,3] / "1,3,5" / "1 3 5" / "周一,周三"；非法项丢掉；**空结果回落 default**。
    「空」不能解释成「永不推送」——用户漏勾一次就永久静默，属于最难排查的坑。
    """
    raw = value
    if isinstance(raw, str):
        s = raw.strip().lower()
        for name, num in (("星期一", 1), ("星期二", 2), ("星期三", 3), ("星期四", 4),
                          ("星期五", 5), ("星期六", 6), ("星期日", 7), ("星期天", 7)):
            s = s.replace(name, str(num))
        for name, num in (("周一", 1), ("周二", 2), ("周三", 3), ("周四", 4),
                          ("周五", 5), ("周六", 6), ("周日", 7), ("周天", 7)):
            s = s.replace(name, str(num))
        s = s.replace("，", ",").replace("；", ",").replace(";", ",")
        s = s.replace("\u3000", " ").replace(" ", ",")
        raw = s
    out = []
    for item in as_list(raw):
        n = as_int(item, 0, 0, 9)
        if 1 <= n <= 7 and n not in out:
            out.append(n)
    if not out:
        return list(default)
    return sorted(out)


def is_mail_addr(value) -> bool:
    """够用的邮箱校验：有且只有一个 @，两边都不空，域名带点。不追求 RFC 完备。"""
    s = str(value or "").strip().strip("<>").strip()
    if not s or s.count("@") != 1:
        return False
    local, _, domain = s.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if any(ch in s for ch in " \t\r\n,;，；"):
        return False
    return True


def _norm_mail_list(value):
    """收件人：支持 'a@x.com, b@y.com' / 每行一个 / 列表。非法项直接丢掉并去重。

    不走 as_list：那个只认逗号/分号，收件人常按行分行写（表单是多行文本框）。
    """
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        text = str(value or "").replace("，", ",").replace("；", ",")
        text = text.replace(";", ",").replace("\r", "\n")
        raw = []
        for line in text.split("\n"):
            raw.extend(line.split(","))
    out = []
    for item in raw:
        addr = str(item).strip().strip("<>").strip()
        if addr and is_mail_addr(addr) and addr not in out:
            out.append(addr)
    return out


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

    # v1.1.7：源端录像方式。这里**不**替用户兜底成 continuous —— 留空就是留空，
    # 由 record_mode_error() 在保存时拦住。运行期 monitor 会把空值当 continuous
    # 用（升级上来的实例不至于突然停摆），但配置本身保留「未选择」这个事实。
    mode = str(cfg.get("record_mode") or "").strip().lower()
    cfg["record_mode"] = mode if mode in RECORD_MODES else ""
    # 兜底阈值至少要比普通告警阈值宽，否则选 motion 等于没选（下限 5 分钟，
    # 上限 7 天：再长就失去「兜底」意义了）
    cfg["record_mode_motion_max_minutes"] = as_int(
        cfg.get("record_mode_motion_max_minutes"), 1440, 5, 10080)
    cfg["record_mode_day_from"] = norm_hhmm(cfg.get("record_mode_day_from"), "07:00")
    cfg["record_mode_day_to"] = norm_hhmm(cfg.get("record_mode_day_to"), "19:00")

    cfg["scan_interval"] = as_int(cfg.get("scan_interval"), 300, 30)
    cfg["alert_threshold_minutes"] = as_int(cfg.get("alert_threshold_minutes"), 60, 1)
    cfg["alert_silence_minutes"] = as_int(cfg.get("alert_silence_minutes"), 120, 0)
    # ---- 源端服务巡检（v1.2.0 P0）----
    cfg["nvr_health_enabled"] = as_bool(cfg.get("nvr_health_enabled"), False)
    # 地址只留 scheme://host:port：用户常把 /cloud/ 或整条页面地址粘进来，
    # 原样保留会让请求打到首页 HTML 上，报「返回的不是 JSON」，难查。
    url = str(cfg.get("nvr_base_url") or "").strip().rstrip("/")
    if url:
        _u = urllib_parse(url)
        if _u.scheme and _u.netloc:
            url = "%s://%s" % (_u.scheme.lower(), _u.netloc)
    cfg["nvr_base_url"] = url
    cfg["nvr_health_interval"] = as_int(cfg.get("nvr_health_interval"), 300, 60)
    cfg["nvr_expire_warn_days"] = as_int(cfg.get("nvr_expire_warn_days"), 7, 0, 365)
    cfg["notify_recovery"] = as_bool(cfg.get("notify_recovery"), True)
    # 旧配置里没有这个键 → 默认开（升级后行为不变，不会突然停掉日报）
    cfg["daily_report_enabled"] = as_bool(cfg.get("daily_report_enabled"), True)
    cfg["daily_report_time"] = norm_hhmm(cfg.get("daily_report_time"), "08:00")
    # v1.0.16：日报统计区间 + 推送日（旧配置没有 → 00:00–23:59 / 每天，行为不变）
    cfg["report_window_from"] = norm_hhmm(cfg.get("report_window_from"), "00:00")
    cfg["report_window_to"] = norm_hhmm(cfg.get("report_window_to"), "23:59")
    cfg["report_weekdays"] = norm_weekdays(cfg.get("report_weekdays"))
    # 告警推送计划：默认每天 + 全天（旧配置升级后照旧 24 小时都推）
    cfg["alert_push_weekdays"] = norm_weekdays(cfg.get("alert_push_weekdays"))
    cfg["alert_window_from"] = norm_hhmm(cfg.get("alert_window_from"), "00:00")
    cfg["alert_window_to"] = norm_hhmm(cfg.get("alert_window_to"), "23:59")

    channels = [c for c in as_list(cfg.get("enabled_push_channels")) if c in PUSH_CHANNELS]
    if not channels:
        channels = []
    cfg["enabled_push_channels"] = channels

    for key in ("dingtalk_webhook", "dingtalk_secret", "feishu_webhook", "wechat_webhook", "timezone"):
        cfg[key] = str(cfg.get(key) or "").strip()

    # ---- 邮件 ----
    # 兼容常见写法：端口填 "465" / 带空格 / 带协议前缀（smtps://smtp.qq.com）
    host = str(cfg.get("email_smtp_host") or "").strip()
    for prefix in ("smtps://", "smtp://", "https://", "http://"):
        if host.lower().startswith(prefix):
            host = host[len(prefix):]
    host = host.strip().rstrip("/")
    if "/" in host:                     # 只保留主机名，丢掉误粘的路径
        host = host.split("/", 1)[0]
    cfg["email_smtp_host"] = host

    sec = str(cfg.get("email_smtp_security") or "ssl").strip().lower()
    if sec in ("tls", "start_tls", "smtp", "plain-tls"):
        sec = "starttls"
    if sec in ("plain", "no", "off", "close"):
        sec = "none"
    cfg["email_smtp_security"] = sec if sec in SMTP_SECURITIES else "ssl"
    cfg["email_smtp_port"] = as_int(cfg.get("email_smtp_port"),
                                    SMTP_SECURITY_PORTS[cfg["email_smtp_security"]], 1, 65535)
    cfg["email_smtp_user"] = str(cfg.get("email_smtp_user") or "").strip()
    # 授权码按原样保留（部分邮箱的授权码里没有空格，但 Gmail 应用密码含空格，不能 strip 掉中间）
    cfg["email_smtp_pass"] = str(cfg.get("email_smtp_pass") or "").strip()
    cfg["email_from"] = str(cfg.get("email_from") or "").strip()
    cfg["email_to"] = _norm_mail_list(cfg.get("email_to"))
    cfg["email_subject_prefix"] = (str(cfg.get("email_subject_prefix") or "").strip()
                                   or DEFAULT_CONFIG["email_subject_prefix"])

    exts = []
    for e in as_list(cfg.get("video_exts")):
        if not e.startswith("."):
            e = "." + e
        exts.append(e.lower())
    cfg["video_exts"] = exts or list(DEFAULT_CONFIG["video_exts"])

    # ---- 备份（v1.1.0）----
    cfg["backup_enabled"] = as_bool(cfg.get("backup_enabled"), False)
    cfg["backup_dir"] = str(cfg.get("backup_dir") or "").strip().rstrip("/")
    cfg["backup_time"] = norm_hhmm(cfg.get("backup_time"), "03:00")
    cfg["backup_weekdays"] = norm_weekdays(cfg.get("backup_weekdays"))
    cfg["backup_keep"] = as_int(cfg.get("backup_keep"), 30, 0, 999)
    cfg["backup_include_config"] = as_bool(cfg.get("backup_include_config"), True)
    cfg["backup_include_state"] = as_bool(cfg.get("backup_include_state"), False)
    # 额外文件：只收绝对路径，重复项按首次出现去重（避免同一份文件在备份里出现两次）
    extras, seen = [], set()
    for raw in as_list(cfg.get("backup_extra_files")):
        p = str(raw or "").strip()
        if not p or p in seen:
            continue
        seen.add(p)
        extras.append(p)
    cfg["backup_extra_files"] = extras
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
