#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置备份：手动 + 每日定时，单 JSON 快照，可列表 / 恢复 / 删除 / 按份数清理。

为什么用单个 JSON 而不是 tar/zip：
  · 只用标准库，不引入解包器；单文件恢复不涉及压缩包路径穿越这类问题。
  · 备份的是配置本身，直接就能看、能 diff —— 出问题不用先解压才知道里面是什么。
  · config + state 通常十几 KB，压缩收益为零。

为什么不备份日志：
  日志是排障用的过程文件，动辄几十 MB，恢复它没有意义（还会把当前日志糊掉）。
  要留证据去「查看日志」里复制，不要塞进备份。

为什么恢复时保留当前的 backup_* 设置：
  备份设置属于「管理面」—— 从旧快照恢复业务配置时，不应该顺手把备份开关、
  备份目录也退回旧值，否则会出现「恢复完配置，备份功能自己关了」的坑。
"""

import base64
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import config as C

PREFIX = "erm-backup-"
SUFFIX = ".json"
# 只认自己写出来的文件名，防止 restore/delete 被 ../ 之类的话术带出备份目录
NAME_RE = re.compile(r"^erm-backup-\d{8}-\d{6}(?:-\d+)?\.json$")

MAX_EXTRA_BYTES = 2 * 1024 * 1024      # 单个额外文件上限 2 MB
MAX_IMPORT_BYTES = 8 * 1024 * 1024     # 手动导入的备份文件上限 8 MB（正常十几 KB）
PEEK_MAX_BYTES = 256 * 1024            # 列表页最多解析这么大的备份文件

# 恢复 config 时**不覆盖**的键（管理面，属于当前实例的部署选择）
PROTECTED_ON_RESTORE = (
    "backup_enabled", "backup_dir", "backup_time", "backup_weekdays", "backup_keep",
    "backup_include_config", "backup_include_state", "backup_extra_files",
)


# ------------------------------------------------------------------ 时间

def _tz(cfg):
    name = str((cfg or {}).get("timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def now_dt(cfg=None, ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), _tz(cfg))


def today_str(cfg=None, now=None):
    return (now or now_dt(cfg)).strftime("%Y-%m-%d")


def _hm(cfg):
    raw = str((cfg or {}).get("backup_time") or "03:00").strip()
    try:
        hh, mm = raw.split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return 3, 0


# ------------------------------------------------------------------ 目录

def dir_of(cfg):
    """返回备份目录 Path；没配就返回 None（不猜默认位置，避免往用户盘上乱写）。"""
    raw = str((cfg or {}).get("backup_dir") or "").strip()
    if not raw:
        return None
    return Path(raw)


def dir_status(cfg, create=False):
    """检查备份目录能否用。返回 (ok, 说明)。"""
    d = dir_of(cfg)
    if d is None:
        return False, "未设置备份目录"
    if not d.is_absolute():
        return False, "备份目录必须是绝对路径"
    if not d.exists():
        if not create:
            return False, "目录不存在：%s" % d
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return False, "创建目录失败：%s" % e
    if not d.is_dir():
        return False, "不是目录：%s" % d
    if not os.access(str(d), os.W_OK):
        return False, "目录不可写：%s" % d
    return True, str(d)


# ------------------------------------------------------------------ 采集

def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _collect_extra(cfg):
    """按用户填的绝对路径收集额外文件；读不了就跳过并记一条警告。"""
    out, warns = [], []
    for raw in (cfg.get("backup_extra_files") or []):
        raw = str(raw or "").strip()
        if not raw:
            continue
        p = Path(raw).expanduser()
        if not p.is_absolute():
            warns.append("跳过（不是绝对路径）：%s" % raw)
            continue
        if not p.is_file():
            warns.append("跳过（文件不存在）：%s" % raw)
            continue
        try:
            size = p.stat().st_size
            if size > MAX_EXTRA_BYTES:
                warns.append("跳过（超过 %d KB）：%s" % (MAX_EXTRA_BYTES // 1024, raw))
                continue
            blob = p.read_bytes()
        except Exception as e:
            warns.append("读取失败 %s：%s" % (raw, e))
            continue

        item = {"path": str(p), "size": size}
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            item.update({"kind": "binary", "data": base64.b64encode(blob).decode("ascii")})
        else:
            try:
                item.update({"kind": "json", "data": json.loads(text)})
            except Exception:
                item.update({"kind": "text", "data": text})
        out.append(item)
    return out, warns


# ------------------------------------------------------------------ 备份

def run_backup(cfg, state=None, reason="manual", version="", now=None):
    """写一份备份。reason: manual / scheduled / pre-restore。"""
    ok, detail = dir_status(cfg, create=True)
    if not ok:
        return {"ok": False, "error": detail}
    d = Path(detail)

    now = now or now_dt(cfg)
    files = {}
    if cfg.get("backup_include_config", True):
        obj = _read_json(C.CONFIG_FILE)
        if obj is not None:
            files["config.json"] = obj
    if cfg.get("backup_include_state", False):
        obj = _read_json(C.STATE_FILE)
        if obj is not None:
            files["state.json"] = obj

    extra, warns = _collect_extra(cfg)
    if not files and not extra:
        return {"ok": False, "error": "没有可备份的内容：配置和运行状态都没勾，额外文件也没读到"}

    items = list(files) + [e["path"] for e in extra]
    doc = {
        "app": C.APP_NAME,
        "kind": "erm-backup",
        "version": version or "",
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "created_ts": int(now.timestamp()),
        "reason": reason,
        "items": items,
        "files": files,
        "extra": extra,
    }
    text = json.dumps(doc, ensure_ascii=False, indent=2)

    stamp = now.strftime("%Y%m%d-%H%M%S")
    name = PREFIX + stamp + SUFFIX
    n = 2
    while (d / name).exists():                 # 同一秒连点两下也不覆盖
        name = "%s%s-%d%s" % (PREFIX, stamp, n, SUFFIX)
        n += 1
    path = d / name

    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        try:
            tmp.unlink()
        except Exception:
            pass
        return {"ok": False, "error": "写备份失败：%s" % e}
    # 备份里含 webhook / 加签密钥 / SMTP 授权码，别让同机其他账号读到
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass

    removed = prune(cfg, exclude=name)
    if isinstance(state, dict):
        state["last_backup_day"] = today_str(cfg, now)
        state["last_backup_ts"] = int(now.timestamp())
        state["last_backup_file"] = name
    return {
        "ok": True,
        "file": name,
        "path": str(path),
        "size": len(text.encode("utf-8")),
        "items": items,
        "removed": removed,
        "warnings": warns,
        "created_at": doc["created_at"],
        "reason": reason,
    }


def _peek(path):
    try:
        if path.stat().st_size > PEEK_MAX_BYTES:
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _hsize(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def _name_time(name):
    """从备份文件名里取时间（erm-backup-YYYYMMDD-HHMMSS[-n].json）。

    列表用文件名而不是 mtime：手动导入的备份会沿用原始 created_at 当文件名时间戳，
    它的 mtime 是「导入那一刻」，直接拿 mtime 显示会和文件名/内容自相矛盾。
    """
    m = re.match(r"^erm-backup-(\d{8})-(\d{6})", str(name or ""))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except Exception:
        return None


def list_backups(cfg, limit=100):
    d = dir_of(cfg)
    if d is None or not d.is_dir():
        return []
    rows = []
    for p in d.glob(PREFIX + "*" + SUFFIX):
        if not NAME_RE.match(p.name) or not p.is_file():
            continue
        rows.append(p)
    rows.sort(key=lambda p: p.name, reverse=True)      # 文件名里的时间戳就是时间序
    out = []
    for p in rows[:max(1, int(limit))]:
        try:
            st = p.stat()
        except Exception:
            continue
        meta = _peek(p)
        nt = _name_time(p.name)
        out.append({
            "file": p.name,
            "size": st.st_size,
            "time_str": (nt or now_dt(cfg, st.st_mtime)).strftime("%Y-%m-%d %H:%M:%S"),
            "reason": meta.get("reason") or "",
            "items": meta.get("items") or [],
            "version": meta.get("version") or "",
            "readable": bool(meta),
        })
    return out


def count_backups(cfg):
    """只数份数，不解析内容 —— status 每 15 秒调一次，不必每次都把几十份备份读一遍。"""
    d = dir_of(cfg)
    if d is None or not d.is_dir():
        return 0
    try:
        return sum(1 for p in d.glob(PREFIX + "*" + SUFFIX)
                   if NAME_RE.match(p.name) and p.is_file())
    except Exception:
        return 0


def prune(cfg, keep=None, exclude=""):
    """只保留最近 N 份。返回被删掉的文件名。"""
    d = dir_of(cfg)
    if d is None or not d.is_dir():
        return []
    try:
        keep = int(keep if keep is not None else (cfg.get("backup_keep") or 0))
    except Exception:
        return []
    if keep <= 0:
        return []
    rows = [p for p in d.glob(PREFIX + "*" + SUFFIX)
            if NAME_RE.match(p.name) and p.is_file()]
    rows.sort(key=lambda p: p.name, reverse=True)
    removed = []
    for p in rows[keep:]:
        if p.name == exclude:
            continue
        try:
            p.unlink()
            removed.append(p.name)
        except Exception:
            pass
    return removed


# ------------------------------------------------------------------ 导入

def import_file(cfg, name, text, version=""):
    """把用户从本机选上来的备份 JSON 收进备份目录。

    两条原则：
      · 不信任上传方的文件名 —— 一律按自己的命名规则重新生成，避免覆盖已有快照、
        或借文件名把文件写到备份目录之外。
      · 只做校验和落盘，不顺手恢复 —— 导入和套用分开，用户还能在列表里核对一眼。
    文件名时间戳取备份内容里的 created_at（导进来的是三天前的份，就还显示三天前），
    取不到才退回当前时间。
    """
    ok, detail = dir_status(cfg, create=True)
    if not ok:
        return {"ok": False, "error": detail}
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "文件是空的"}
    n = len(text.encode("utf-8"))
    if n > MAX_IMPORT_BYTES:
        return {"ok": False, "error": "文件太大（%s，上限 %s）" % (_hsize(n), _hsize(MAX_IMPORT_BYTES))}
    try:
        doc = json.loads(text)
    except Exception as e:
        return {"ok": False, "error": "不是合法 JSON：%s" % e}
    if not isinstance(doc, dict) or doc.get("kind") != "erm-backup":
        return {"ok": False, "error": "不是本应用的备份文件（缺 kind=erm-backup）"}
    files = doc.get("files")
    files = files if isinstance(files, dict) else {}
    extra = doc.get("extra") or []
    if not isinstance(files.get("config.json"), dict) and not extra:
        return {"ok": False, "error": "这份备份里既没有 config.json，也没有额外文件，导进来也没用"}

    d = Path(detail)
    stamp_dt = None
    ca = str(doc.get("created_at") or "").strip()
    if ca:
        try:
            stamp_dt = datetime.strptime(ca, "%Y-%m-%d %H:%M:%S")
        except Exception:
            stamp_dt = None
    stamp = (stamp_dt or now_dt(cfg)).strftime("%Y%m%d-%H%M%S")
    base = PREFIX + stamp + SUFFIX
    fname, k = base, 2
    while (d / fname).exists():
        fname = "%s%s-%d%s" % (PREFIX, stamp, k, SUFFIX)
        k += 1
    path = d / fname

    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception as e:
        try:
            tmp.unlink()
        except Exception:
            pass
        return {"ok": False, "error": "写入备份目录失败：%s" % e}

    items = list(files) + [str(e.get("path")) for e in extra if isinstance(e, dict) and e.get("path")]
    removed = prune(cfg, exclude=fname)
    return {
        "ok": True,
        "file": fname,
        "original": str(name or ""),
        "size": n,
        "items": items,
        "reason": doc.get("reason") or "imported",
        "version": doc.get("version") or version,
        "created_at": doc.get("created_at") or "",
        "removed": removed,
    }


# ------------------------------------------------------------------ 恢复 / 删除

def restore(cfg, name, state=None, version=""):
    ok, detail = dir_status(cfg, create=False)
    if not ok:
        return {"ok": False, "error": detail}
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        return {"ok": False, "error": "备份文件名不合法"}
    p = Path(detail) / name
    if not p.is_file():
        return {"ok": False, "error": "备份不存在：%s" % name}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": "备份文件读不出来：%s" % e}

    files = doc.get("files") or {}
    saved = files.get("config.json")
    if not isinstance(saved, dict):
        return {"ok": False, "error": "这份备份里没有 config.json，恢复不了配置"}

    # 先把当前配置存一份，恢复错了还能回去
    pre = run_backup(cfg, state, reason="pre-restore", version=version)

    cur = C.load_config()
    merged = dict(cur)
    for k, v in saved.items():
        if k in PROTECTED_ON_RESTORE:
            continue
        merged[k] = v
    new_cfg = C.normalize(merged)
    C.save_config(new_cfg)

    # 运行状态（备份时勾了「运行状态」才有）：只填回去，不整体覆盖 ——
    # last_backup_* 是备份自身的水位线，跟着回退会造成重复备份，跳过。
    restored_files = []
    st_saved = files.get("state.json")
    if isinstance(st_saved, dict) and isinstance(state, dict):
        for k, v in st_saved.items():
            if k.startswith("last_backup_"):
                continue
            state[k] = v
        restored_files.append("state.json")
    restored_files.append("config.json")

    restored_keys = sorted(k for k in saved if k not in PROTECTED_ON_RESTORE)
    skipped = sorted(k for k in saved if k in PROTECTED_ON_RESTORE)
    return {
        "ok": True,
        "restored_from": name,
        "restored_files": restored_files,
        "restored_keys": restored_keys,
        "skipped_keys": skipped,
        "pre_backup": pre.get("file") or "",
        "extra_files": [e.get("path") for e in (doc.get("extra") or [])],
        "config": new_cfg,
        "created_at": doc.get("created_at") or "",
    }


def remove(cfg, name):
    ok, detail = dir_status(cfg, create=False)
    if not ok:
        return {"ok": False, "error": detail}
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        return {"ok": False, "error": "备份文件名不合法"}
    p = Path(detail) / name
    if not p.is_file():
        return {"ok": False, "error": "备份不存在：%s" % name}
    try:
        p.unlink()
    except Exception as e:
        return {"ok": False, "error": "删除失败：%s" % e}
    return {"ok": True, "deleted": name}


# ------------------------------------------------------------------ 定时

def backup_due(cfg, state=None, now=None):
    """到点该不该自动备份。与日报同款判定：当天该跑、时间已过、今天还没跑过。"""
    if not cfg.get("backup_enabled"):
        return False
    if not str(cfg.get("backup_dir") or "").strip():
        return False
    days = cfg.get("backup_weekdays") or list(C.WEEKDAY_ALL)
    now = now or now_dt(cfg)
    if now.isoweekday() not in [int(x) for x in days]:
        return False
    hh, mm = _hm(cfg)
    if (now.hour, now.minute) < (hh, mm):
        return False
    st = state if isinstance(state, dict) else {}
    return st.get("last_backup_day") != today_str(cfg, now)


def next_backup_str(cfg, state=None, now=None):
    if not cfg.get("backup_enabled"):
        return "未启用"
    if not str(cfg.get("backup_dir") or "").strip():
        return "未设置备份目录"
    days = [int(x) for x in (cfg.get("backup_weekdays") or list(C.WEEKDAY_ALL))]
    hh, mm = _hm(cfg)
    now = now or now_dt(cfg)
    for i in range(0, 15):
        day = now + timedelta(days=i)
        if day.isoweekday() not in days:
            continue
        cand = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand > now:
            return cand.strftime("%Y-%m-%d %H:%M")
    return "—"
