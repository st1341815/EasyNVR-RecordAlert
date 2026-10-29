#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端演练：不安装应用，直接以「已装好」的目录形态拉起 server.py，
用与用户现场一致的 EasyNVR 目录结构验证通道识别、判定、日报文案。

    PYTHONPATH= python3 e2e_local.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta

BASE = "/tmp/erm-e2e"
APP = os.path.join(BASE, "app")
VAR = os.path.join(BASE, "var")
REC = os.path.join(BASE, "NVR", "easynvr_rec")
SOCK = os.path.join(BASE, "app", "erm.sock")
SRC_APP = os.environ.get("ERM_SRC_APP") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "erm", "app")


def api(action, **kw):
    body = json.dumps(dict(action=action, **kw)).encode()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect(SOCK)
    s.sendall(b"POST /app/erm/api HTTP/1.1\r\nHost: localhost\r\n"
              b"Content-Type: application/json\r\nContent-Length: "
              + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
    data = b""
    while True:
        c = s.recv(65536)
        if not c:
            break
        data += c
    s.close()
    return json.loads(data.split(b"\r\n\r\n", 1)[1].decode())


def mk(path, when, n=1, prefix=""):
    os.makedirs(path, exist_ok=True)
    for i in range(n):
        fp = os.path.join(path, "%s%s-%05d.mp4" % (prefix, when.strftime("%Y%m%d%H%M%S"), i))
        open(fp, "wb").write(b"\0" * 16)
        ts = when.timestamp() + i
        os.utime(fp, (ts, ts))


def build_tree():
    now = datetime.now()
    d_today = now.strftime("%Y%m%d")
    d_yday = (now - timedelta(days=1)).strftime("%Y%m%d")
    # 摄像头 1：主码流刚录过；子码流只在昨天录过
    mk(os.path.join(REC, "O27fqV6dYvs3T", "profile_1", d_today), now - timedelta(seconds=40), 4)
    mk(os.path.join(REC, "O27fqV6dYvs3T", "profile_2", d_yday), now - timedelta(days=1, hours=3), 3)
    # 摄像头 2：3 小时没录像
    mk(os.path.join(REC, "Xk91mQ2pLzT0", "profile_1", (now - timedelta(hours=3)).strftime("%Y%m%d")),
       now - timedelta(hours=3), 2)
    # 摄像头 3：目录/profile 都在，但没文件
    os.makedirs(os.path.join(REC, "De45adQ0Mn7U", "profile_1", d_today), exist_ok=True)
    # 干扰项：事件抓拍目录（里面是 jpg，不是录像）、缩略图目录
    ev = os.path.join(REC, "O27fqV6dYvs3T", "profile_1", d_today + "_event")
    os.makedirs(ev, exist_ok=True)
    for i in range(5):
        open(os.path.join(ev, "20261029%06d_%d.jpg" % (i, i)), "wb").write(b"\0" * 8)
    os.makedirs(os.path.join(REC, "thumbnails", d_today), exist_ok=True)


def main():
    shutil.rmtree(BASE, ignore_errors=True)
    shutil.copytree(SRC_APP, APP)
    os.makedirs(VAR, exist_ok=True)
    build_tree()

    env = dict(os.environ, TRIM_APPDEST=APP, TRIM_PKGVAR=VAR, PYTHONPATH="")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.Popen([sys.executable, "-X", "utf8", os.path.join(APP, "server.py")],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(40):
        if os.path.exists(SOCK):
            break
        time.sleep(0.25)
    if not os.path.exists(SOCK):
        out = proc.stdout.read().decode() if proc.stdout else ""
        print("启动失败:\n" + out)
        return 1

    fails = []

    def check(cond, name, detail=""):
        print(("  ✔ " if cond else "  ✘ ") + name + (("  → " + str(detail)) if (detail and not cond) else ""))
        if not cond:
            fails.append(name)

    try:
        # 1) 初始配置（模拟向导）：录像目录指到 EasyNVR 根
        r = api("save_config", config={"record_root": REC, "alert_threshold_minutes": "60",
                                      "scan_interval": "60", "video_exts": ".mp4"})
        check(r.get("ok"), "保存配置", r)

        st = api("scan")
        chans = {c["channel"]: c for c in st["channels"]}
        print("\n[通道识别] layout=%s  共 %d 个" % (st.get("layout"), st.get("channel_count", 0)))
        for c in st["channels"]:
            print("   %-18s %-10s idle=%-6s files=%-3s profiles=%s"
                  % (c["channel"], c["tag"], c["idle_min"], c["files"], ",".join(c["profiles"])))
        check(st.get("layout") == "camera", "自动识别为摄像头聚合", st.get("layout"))
        check("thumbnails" not in chans, "thumbnails 之类目录未被当成通道", list(chans))
        check(set(chans) == {"O27fqV6dYvs3T", "Xk91mQ2pLzT0", "De45adQ0Mn7U"}, "识别到 3 个摄像头", list(chans))
        check(chans["O27fqV6dYvs3T"]["status"] == "ok", "刚录过的摄像头正常", chans["O27fqV6dYvs3T"]["status"])
        check(chans["O27fqV6dYvs3T"]["files"] == 7, "文件数聚合两个 profile（4+3）", chans["O27fqV6dYvs3T"]["files"])
        check(chans["Xk91mQ2pLzT0"]["status"] == "silent", "3 小时没录像 → 无新录像", chans["Xk91mQ2pLzT0"]["status"])
        check(chans["De45adQ0Mn7U"]["status"] == "no_record", "空 profile → 无录像文件", chans["De45adQ0Mn7U"]["status"])
        check(len(st.get("events") or []) == 2, "两个异常通道合并成一次告警", st.get("events"))
        check((st.get("pushed") or [{}])[0].get("detail") == "未启用任何推送渠道",
              "未配渠道时如实提示（默认不启用渠道）", st.get("pushed"))

        # 2) 别名 + 日报文案
        r = api("save_config", config={"channel_aliases": "O27fqV6dYvs3T=大门\nXk91mQ2pLzT0=后院"})
        check(r.get("ok") and r["config"]["channel_aliases"] == {"O27fqV6dYvs3T": "大门", "Xk91mQ2pLzT0": "后院"},
              "别名保存为映射", r.get("config", {}).get("channel_aliases"))
        st = api("scan")
        labels = {c["channel"]: c["label"] for c in st["channels"]}
        check(labels["O27fqV6dYvs3T"] == "大门", "通道显示名用别名", labels)

        d = api("daily_now")
        print("\n[昨日日报]\n" + d["message"] + "\n")
        check("大门（O27fqV6dYvs3T）：3 个文件" in d["message"], "日报用别名且只数昨天", d["message"])
        check("后院（Xk91mQ2pLzT0）：0 个文件" in d["message"], "昨天没录的通道计 0", d["message"])

        # 3) 切到 profile 口径
        r = api("save_config", config={"channel_layout": "profile"})
        st = api("scan")
        names = [c["channel"] for c in st["channels"]]
        print("\n[profile 口径] " + "  ".join(names))
        check("O27fqV6dYvs3T/profile_2" in names, "profile 口径拆出子码流", names)
        check(st.get("layout") == "profile", "布局返回值跟随配置", st.get("layout"))

        # 4) doctor
        doc = api("doctor")
        print("\n[自检]")
        for c in doc["checks"]:
            print("   " + ("✅" if c["ok"] else "⚠️") + " " + c["item"] + " | " + str(c["detail"]))
        check(any(c["item"] == "通道布局" for c in doc["checks"]), "自检报告通道布局")

        # 5) 权限提示（把目录当不存在，验证错误文案）
        r = api("save_config", config={"record_root": "/vol9/不存在的目录"})
        check(any("不存在" in w for w in r.get("warnings", [])), "目录不存在时给出警告", r.get("warnings"))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\n" + ("✅ 端到端全部通过" if not fails else "❌ 失败 %d 项：%s" % (len(fails), fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
