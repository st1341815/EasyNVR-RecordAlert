#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断流清单（固定 48 小时 + 时长档位）的端到端自检。

不依赖 pytest，直接 python3 跑：造一棵假的 EasyNVR 录像目录树，喂给 stats.build()，
断言清单的口径与档位。重点验证三件事：

  1. 清单窗口是滚动的「最近 48 小时」，和页面选的 range 无关（今天 / 近 30 天都一样）；
  2. 「整个窗口都没录像」的通道要出现在清单里（这正是最容易漏的一类故障）；
  3. 档位边界：正好 600 秒归「≤10 分钟」，正好 3600 秒归「30 分钟–1 小时」。
"""
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

FAILS = []


def check(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name + ("   " + detail if detail else ""))
    if not ok:
        FAILS.append(name)


def mkfile(root, cam, prof, day, name, mtime):
    d = root / cam / prof / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"x")
    os.utime(f, (mtime, mtime))


def day_of(cfg, ts):
    return datetime.fromtimestamp(ts, M.get_tz(cfg)).strftime("%Y%m%d")


def main():
    root = Path(tempfile.mkdtemp(prefix="erm_gapt_"))
    cfg = dict(C.DEFAULT_CONFIG)
    cfg["record_root"] = str(root)
    cfg["channel_layout"] = "camera"
    exts = set(cfg["video_exts"])          # 注意：带点的扩展名（".mp4"）
    now = datetime.now(M.get_tz(cfg))
    now_ts = int(now.timestamp())
    tz = M.get_tz(cfg)

    # ---- 造数据 ----------------------------------------------------------
    # CAM_A：窗口内两次录像，中间 1.5 小时空洞；末尾距现在 30 分钟（30 分钟档）
    mkfile(root, "CAM_A", "profile_1", day_of(cfg, now_ts - 3 * 3600), "a.mp4",
           now_ts - 3 * 3600)
    mkfile(root, "CAM_A", "profile_1", day_of(cfg, now_ts - 1800), "b.mp4",
           now_ts - 1800)
    # CAM_B：最后一次录像是 60 小时前 → 整个 48 小时窗口都空（最该报的那一条）
    mkfile(root, "CAM_B", "profile_1", day_of(cfg, now_ts - 60 * 3600), "c.mp4",
           now_ts - 60 * 3600)
    # CAM_C：目录在、今天也建了日期目录，但一个文件都没有（从没录过，不算断流）
    (root / "CAM_C" / "profile_1" / day_of(cfg, now_ts)).mkdir(parents=True)
    # CAM_D：整窗只有一段，正好 10:00 长（档位边界）：起点 = 窗口起点 + 0：00，终点 +10:00
    stats.clear_cache()

    # ---- 1. 窗口 --------------------------------------------------------
    w = stats.gap_window(cfg, now=now)
    check("窗口长度 = 48 小时", w["end"] - w["start"] == 48 * 3600,
          "%d 秒" % (w["end"] - w["start"]))
    check("窗口滚动到今天", w["end"] == now_ts)
    check("日期目录跨 3 天", len(w["date_dirs"]) == 3, str(w["date_dirs"]))
    check("窗口有展示文案", w["label"] == "最近 48 小时" and bool(w["start_str"]))

    # ---- 2. 档位边界 -----------------------------------------------------
    check("600 秒 → ≤10 分钟", stats.gap_bucket_of(600) == "le10")
    check("601 秒 → 10–30 分钟", stats.gap_bucket_of(601) == "m10_30")
    check("1800 秒 → 10–30 分钟", stats.gap_bucket_of(1800) == "m10_30")
    check("1801 秒 → 30 分钟–1 小时", stats.gap_bucket_of(1801) == "m30_60")
    check("3600 秒 → 30 分钟–1 小时", stats.gap_bucket_of(3600) == "m30_60")
    check("3601 秒 → > 1 小时", stats.gap_bucket_of(3601) == "gt60")

    # ---- 3. gaps_recent 的三种形态 ---------------------------------------
    stats.clear_cache()
    ga = stats.gaps_recent(cfg, str(root / "CAM_A"), exts, w, int(now_ts))
    dura = [(int(e - s), stats.gap_bucket_of(int(e - s))) for s, e in ga]
    check("CAM_A 抓到 2 段", len(ga) == 2, str(dura))
    check("CAM_A 段一（2.5 小时洞）→ > 1 小时", dura[0][1] == "gt60", str(dura))
    check("CAM_A 段二（30 分钟尾巴）→ 10–30 分钟", dura[1][1] == "m10_30", str(dura))
    check("CAM_A 最大洞 ≈ 2.5 小时", max(d for d, _ in dura) >= 7200, str(dura))

    gb = stats.gaps_recent(cfg, str(root / "CAM_B"), exts, w, int(now_ts))
    check("CAM_B 报「整窗无录像」", len(gb) == 1 and gb[0][0] == w["start"],
          str(gb))
    check("CAM_B 该段覆盖整个窗口",
          int(gb[0][1] - gb[0][0]) == 48 * 3600)

    gc = stats.gaps_recent(cfg, str(root / "CAM_C"), exts, w, int(now_ts))
    check("CAM_C 从没录过 → 不算断流", gc == [], str(gc))

    gn = stats.gaps_recent(cfg, str(root / "CAM_NOPE"), exts, w, int(now_ts))
    check("通道目录不存在 → 空", gn == [], str(gn))

    # ---- 4. build() 的口径不随 range 变 ---------------------------------
    stats.clear_cache()
    d_today = stats.build(cfg, "today")
    stats.clear_cache()
    d_30d = stats.build(cfg, "30d")

    for tag, d in (("today", d_today), ("30d", d_30d)):
        check("[%s] gap_window 有起止文案" % tag,
              "start_str" in d["gap_window"] and "end_str" in d["gap_window"])
        check("[%s] 清单非空" % tag, len(d["gaps"]) > 0, "%d 段" % len(d["gaps"]))
        check("[%s] 每家通道的 label 有值" % tag,
              all(g["label"] for g in d["gaps"]))
        check("[%s] 每条都有档位和文案" % tag,
              all(g["bucket"] in stats.GAP_BUCKET_LABELS and g["bucket_label"]
                  and g["dur_str"] for g in d["gaps"]))
        check("[%s] 档位计数之和 = 总数" % tag,
              sum(b["count"] for b in d["gap_buckets"][1:]) == d["gap_total"],
              "%s vs %s" % ([b["count"] for b in d["gap_buckets"]], d["gap_total"]))
        check("[%s] 首项是「全部」" % tag,
              d["gap_buckets"][0]["key"] == "all" and
              d["gap_buckets"][0]["count"] == d["gap_total"])

    ids = lambda d: [(g["channel"], g["from"], g["to"]) for g in d["gaps"]]
    check("清单与页面 range 无关", ids(d_today) == ids(d_30d),
          "%d vs %d 段" % (len(d_today["gaps"]), len(d_30d["gaps"])))
    check("today 的 KPI 断流数仍按范围算（≤ 48h 清单数）",
          d_today["summary"]["gap_total"] <= d_today["gap_total"] + 1,
          "KPI %d / 清单 %d" % (d_today["summary"]["gap_total"], d_today["gap_total"]))
    check("CAM_B 在两个视图里都出现",
          any(g["channel"] == "CAM_B" for g in d_today["gaps"]) and
          any(g["channel"] == "CAM_B" for g in d_30d["gaps"]))
    check("CAM_C 从不出现",
          not any(g["channel"] == "CAM_C" for g in d_today["gaps"] + d_30d["gaps"]))
    check("断流按结束时间倒序",
          all(d_today["gaps"][i]["to"] >= d_today["gaps"][i + 1]["to"]
              for i in range(len(d_today["gaps"]) - 1)))

    shutil.rmtree(root, ignore_errors=True)
    print()
    if FAILS:
        print("FAILED: %d 项 —— %s" % (len(FAILS), "; ".join(FAILS)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
