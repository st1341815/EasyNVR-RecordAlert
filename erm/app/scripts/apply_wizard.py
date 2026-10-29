#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把安装向导 / 设置页的 wizard_* 环境变量合并进 config.json。

飞牛不会替应用写配置文件：向导字段是以 `wizard_<field>` 环境变量传给
install_callback / config_callback 的，必须自己落到盘上（文档评审 A3）。

用法（在 cmd 脚本里）：
    python3 "$DEST/scripts/apply_wizard.py" "$VAR/config.json"

输出一行 JSON 摘要到 stdout，供 cmd 脚本写进 TRIM_TEMP_LOGFILE / 日志。
"""

import json
import os
import sys
from pathlib import Path

APP_DEST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DEST)
import config as C  # noqa: E402

# wizard 字段 → 配置键；kind 决定怎么解析
MAP = [
    ("wizard_record_root", "record_root", "str"),
    ("wizard_monitor_channels", "monitor_channels", "list"),
    ("wizard_channel_include", "channel_include", "str"),
    ("wizard_scan_interval", "scan_interval", "int"),
    ("wizard_alert_threshold_minutes", "alert_threshold_minutes", "int"),
    ("wizard_alert_silence_minutes", "alert_silence_minutes", "int"),
    ("wizard_daily_report_time", "daily_report_time", "str"),
    ("wizard_timezone", "timezone", "str"),
    ("wizard_dingtalk_webhook", "dingtalk_webhook", "str"),
    ("wizard_dingtalk_secret", "dingtalk_secret", "str"),
    ("wizard_feishu_webhook", "feishu_webhook", "str"),
    ("wizard_wechat_webhook", "wechat_webhook", "str"),
]

PUSH_MAP = [
    ("wizard_push_dingtalk", "dingtalk"),
    ("wizard_push_feishu", "feishu"),
    ("wizard_push_wechat", "wechat"),
]


def main():
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else C.CONFIG_FILE
    existing = {}
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    cfg = C.normalize(existing)

    applied = {}
    for env_key, cfg_key, kind in MAP:
        # 本次向导没提供、或提供了但为空 → 保留原值。
        # 「设置」页的 initValue 是静态的（拿不到当前配置），空值若当成"清空"，
        # 用户只是在设置页点了保存就会把已填好的录像目录/Webhook 抹掉。
        raw = os.environ.get(env_key)
        if raw is None or str(raw).strip() == "":
            continue
        if kind == "list":
            cfg[cfg_key] = C.as_list(raw)
        elif kind == "int":
            cfg[cfg_key] = C.as_int(raw, cfg[cfg_key])
        else:
            cfg[cfg_key] = raw.strip()
        applied[cfg_key] = cfg[cfg_key]

    # 推送渠道：三个独立开关（避免依赖「多选控件」这种未验证的能力）
    picks = []
    any_flag = False
    for env_key, name in PUSH_MAP:
        raw = os.environ.get(env_key)
        if raw is None or str(raw).strip() == "":
            continue                           # 未指定 → 保留原渠道组合
        any_flag = True
        if C.as_bool(raw, False):
            picks.append(name)
    if any_flag:
        cfg["enabled_push_channels"] = picks
        applied["enabled_push_channels"] = picks

    cfg = C.save_config(cfg)                   # 内部 normalize + 原子写 + 0600

    warns = []
    root = cfg.get("record_root") or ""
    if not root:
        warns.append("录像目录未填写")
    elif not os.path.isdir(root):
        warns.append("录像目录不存在：%s" % root)
    elif not os.access(root, os.R_OK | os.X_OK):
        warns.append("当前用户读不到 %s（请到「应用中心 → 设置 → 授权目录」授权后重启应用）" % root)
    if not cfg.get("enabled_push_channels"):
        warns.append("未启用任何推送渠道，告警只会写日志")
    for ch in cfg.get("enabled_push_channels") or []:
        if not (cfg.get("%s_webhook" % ch) or "").strip():
            warns.append("已启用 %s 但 webhook 为空" % C.PUSH_LABEL.get(ch, ch))

    print(json.dumps({"ok": True, "path": str(target), "config": cfg,
                      "applied": applied, "warnings": warns}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
