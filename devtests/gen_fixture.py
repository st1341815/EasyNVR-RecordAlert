#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给前端渲染自检造一份真实数据：假 EasyNVR 目录树 → stats.build() → JSON。

输出 /tmp/fixture_gaps.json，供 devtests/render_gaps.js 拿去喂 gapList()。
数据形态与 test_gap_window.py 一致（CAM_A 有两段、CAM_B 整窗空、CAM_C 从没录过）。
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "erm" / "app"
sys.path.insert(0, str(APP))

import config as C            # noqa: E402
import monitor as M           # noqa: E402
import stats                  # noqa: E402

OUT = Path("/tmp/fixture_gaps.json")


def mkfile(root, cam, prof, day, name, mtime):
    d = root / cam / prof / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"x")
    os.utime(f, (mtime, mtime))


def main():
    root = Path(tempfile.mkdtemp(prefix="erm_fx_"))
    cfg = dict(C.DEFAULT_CONFIG)
    cfg["record_root"] = str(root)
    cfg["channel_layout"] = "camera"
    now_ts = int(datetime.now(M.get_tz(cfg)).timestamp())

    def day(ts):
        return datetime.fromtimestamp(ts, M.get_tz(cfg)).strftime("%Y%m%d")

    mkfile(root, "CAM_A", "profile_1", day(now_ts - 3 * 3600), "a.mp4", now_ts - 3 * 3600)
    mkfile(root, "CAM_A", "profile_1", day(now_ts - 1800), "b.mp4", now_ts - 1800)
    mkfile(root, "CAM_B", "profile_1", day(now_ts - 60 * 3600), "c.mp4", now_ts - 60 * 3600)
    (root / "CAM_C" / "profile_1" / day(now_ts)).mkdir(parents=True)

    stats.clear_cache()
    d = stats.build(cfg, "today")
    OUT.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    print("wrote %s  (%d 段 断流, %d 条通道)" %
          (OUT, len(d["gaps"]), len(d["channels"])))
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
