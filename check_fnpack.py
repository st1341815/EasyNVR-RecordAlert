#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnpack.json（FnDepot V2 外部源）自检。

校验：schema 取值、URL 真实可达、附件 size/sha256 与本地包一致、
包内 manifest 与应用键名/版本/平台/显示名交叉一致。

    PYTHONPATH= python3 check_fnpack.py
"""

import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FN = os.path.join(HERE, "fnpack.json")
FPK = os.path.join(HERE, "dist", "erm.fpk")

CATS = {"影音娱乐", "系统工具", "编程开发", "AI赋能", "生活服务",
        "智能智控", "教育学习", "游戏地带", "硬件驱动"}
PLATFORMS = {"all", "x86", "arm"}

checks = []


def ck(cond, name, detail=""):
    checks.append((bool(cond), name, detail))


def head(url, timeout=25):
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "fnpack-check"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, len(r.read()), r.headers.get("Content-Type", "")


def main():
    raw = open(FN, "rb").read()
    ck(not raw.startswith(b"\xef\xbb\xbf"), "无 BOM")
    spec = json.loads(raw.decode("utf-8"))
    ck(spec.get("schema_version") == "2", "schema_version 是字符串 \"2\"", spec.get("schema_version"))

    si = spec.get("source_info") or {}
    ck(all(si.get(k) for k in ("name", "author", "homepage", "description")),
       "source_info 字段齐全", list(si))
    ck("id" not in si, "source_info 不写 id")

    apps = spec.get("apps") or {}
    ck(len(apps) == 1 and "erm" in apps, "apps 键 = appname（erm）", list(apps))
    app = apps["erm"]

    ck(bool(app.get("display_name")), "有 display_name")
    ck(bool(app.get("desc")) and "<" in app["desc"], "desc 是 HTML 摘要")
    ck(set(app.get("platform") or []) <= PLATFORMS and app.get("platform"), "platform 取值合法", app.get("platform"))
    cats = app.get("categories") or []
    ck(0 < len(cats) <= 2 and set(cats) <= CATS, "categories ≤2 且在白名单内", cats)
    ck(app.get("run_as") in ("package", "root"), "run_as 合法", app.get("run_as"))
    ck(app.get("install_type") in ("", "root"), "install_type 合法", app.get("install_type"))
    ck(app.get("is_docker") is False, "is_docker = false")
    ck(app.get("service_port") == "", "service_port 为空（微应用走网关）")

    releases = app.get("releases") or {}
    ck(bool(releases), "有 releases")
    local_pkg = open(FPK, "rb").read()
    local_sha = hashlib.sha256(local_pkg).hexdigest()

    for ver, rel in releases.items():
        ck("." in ver, "版本号形如 X.Y.Z", ver)
        ck(bool(rel.get("changelog")), "版本有 changelog", ver)
        ck(bool(rel.get("updated_at")), "版本有 updated_at", ver)
        pkgs = rel.get("packages") or {}
        ck(set(pkgs) <= PLATFORMS and pkgs, "packages 架构键合法（all/x86/arm）", list(pkgs))
        for arch, p in pkgs.items():
            url = p.get("download_url") or ""
            ck(url.startswith("https://github.com/st1341815/EasyNVR-RecordAlert/releases/download/"),
               "download_url 指向本仓库 Release", url)
            ck(url.endswith("EasyNVR-RecordAlert-%s.fpk" % ver),
               "附件名 = 项目名+版本.fpk", url.rsplit("/", 1)[-1])
            if arch == "all" and p.get("size") == len(local_pkg):
                ck(p.get("sha256") == local_sha, "sha256 与本地 dist/erm.fpk 一致", p.get("sha256"))
            else:
                print("  · 跳过 size 比对（%s/%s 不是本地构建）" % (ver, arch))

    # URL 实测（匿名）
    print("  · 实测 URL 可达性…")
    st, n, ctype = head(app["icon_url"])
    ck(st == 200 and n < 500 * 1024 and "image" in ctype, "icon_url 可下载且是图片",
       "%s %s B %s" % (st, n, ctype))
    try:
        import io
        data = urllib.request.urlopen(app["icon_url"], timeout=25).read()
        w, h = struct.unpack(">II", data[16:24])
        ck((w, h) == (256, 256), "icon 尺寸 256×256", (w, h))
    except Exception as e:
        ck(False, "icon 尺寸读取", e)

    for u in app.get("preview_urls") or []:
        st, n, ctype = head(u)
        ck(st == 200 and "image" in ctype, "preview 可下载: " + u.rsplit("/", 1)[-1],
           "%s %s B" % (st, n))

    for ver, rel in releases.items():
        p = (rel.get("packages") or {}).get("all") or {}
        st, n, ctype = head(p["download_url"])
        ck(st == 200 and n == p.get("size"), "Release 附件可匿名下载且大小一致",
           "%s %s B" % (st, n))

    # 包内 manifest 交叉核对
    tmp = tempfile.mkdtemp(prefix="fnpack-chk-")
    subprocess.run(["tar", "xzf", FPK, "-C", tmp], check=True)
    man = {}
    for line in open(os.path.join(tmp, "manifest"), encoding="utf-8"):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            man[k.strip()] = v.strip()
    ck(man.get("appname") == "erm", "包内 appname = 应用键名", man.get("appname"))
    ck(list(releases)[0] == man.get("version"), "最新 release 版本 = 包内 version",
       "%s vs %s" % (list(releases)[0], man.get("version")))
    ck(man.get("display_name") == app.get("display_name"), "包内 display_name 一致",
       man.get("display_name"))
    ck(man.get("platform") in app.get("platform"), "包内 platform 与源一致", man.get("platform"))
    ck(man.get("ctl_stop", "").lower() == "true", "包内 ctl_stop = true（否则会被归为 nostart）")
    subprocess.run(["rm", "-rf", tmp])

    bad = [c for c in checks if not c[0]]
    print()
    for ok, name, detail in checks:
        print(("  ✔ " if ok else "  ✘ ") + name + (("  → " + str(detail)) if (detail and not ok) else ""))
    print()
    if bad:
        print("❌ 失败 %d / %d" % (len(bad), len(checks)))
        return 1
    print("✅ 全部通过：%d 项检查  FNPCK_OK" % len(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
