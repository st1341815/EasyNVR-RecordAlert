#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地提交 + tag 推到 GitHub，并创建 Release 上传 fpk。

用法：
    GH_TOKEN=ghp_xxx python3 tools/release.py            # 真跑
    GH_TOKEN=ghp_xxx python3 tools/release.py --dry-run  # 只打印将要做什么

设计要点：
· token 只经 `git -c http.extraheader=...` 使用，不写进 .git/config、不留在 shell 历史里；
· Release 已存在时自动跳过创建，只补传缺失的附件（可重复执行）；
· 附件名固定 EasyNVR-RecordAlert-<版本>.fpk，与 fnpack.json 里的 download_url 对齐。
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "st1341815/EasyNVR-RecordAlert"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "1.0.17"
TAG = "v" + VERSION
ASSET = os.path.join(HERE, "dist", "EasyNVR-RecordAlert-%s.fpk" % VERSION)
API = "https://api.github.com"


def token():
    t = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if not t:
        sys.exit("缺少 GH_TOKEN（GitHub Personal Access Token，需 repo 权限）")
    return t


def api(path, tok, method="GET", payload=None, raw_file=None):
    url = API + path
    data = None
    headers = {
        "Authorization": "Bearer " + tok,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "erm-release-script",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw_file:
        with open(raw_file, "rb") as f:
            data = f.read()
        headers["Content-Type"] = "application/octet-stream"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            return r.status, (json.loads(body) if body.strip().startswith(b"{") or
                              body.strip().startswith(b"[") else body)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def git_push(tok, dry):
    basic = base64.b64encode(("x-access-token:" + tok).encode()).decode()
    cmd = ["git", "-C", HERE,
           "-c", "http.extraheader=Authorization: Basic " + basic,
           "push", "https://github.com/%s.git" % REPO, "HEAD:main", "--follow-tags"]
    print("+", " ".join(x if "Basic" not in x else "http.extraheader=<token>" for x in cmd))
    if dry:
        return
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode != 0:
        sys.exit("git push 失败（退出码 %d）" % r.returncode)


def release_body():
    with open(os.path.join(HERE, "CHANGELOG.md"), encoding="utf-8") as f:
        txt = f.read()
    i = txt.find("## v1.0.17")
    j = txt.find("## v1.0.15")
    return txt[i:j].strip() if i >= 0 and j > i else txt[:2000]


def ensure_release(tok, dry):
    code, data = api("/repos/%s/releases/tags/%s" % (REPO, TAG), tok)
    if code == 200:
        print("· Release %s 已存在：%s" % (TAG, data.get("html_url")))
        return data
    print("· Release %s 不存在，创建中…" % TAG)
    if dry:
        print("  (dry-run) POST /repos/%s/releases tag=%s" % (REPO, TAG))
        return None
    code, data = api("/repos/%s/releases" % REPO, tok, "POST", {
        "tag_name": TAG,
        "name": "%s %s" % ("EasyNVR-RecordAlert", TAG),
        "body": release_body(),
        "draft": False,
        "prerelease": False,
    })
    if code not in (200, 201):
        sys.exit("创建 Release 失败：HTTP %s %s" % (code, data))
    print("  ✓ %s" % data.get("html_url"))
    return data


def ensure_asset(tok, rel, dry):
    name = os.path.basename(ASSET)
    if not os.path.isfile(ASSET):
        sys.exit("找不到附件：%s" % ASSET)
    have = {a["name"] for a in (rel.get("assets") or [])}
    if name in have:
        print("· 附件 %s 已存在，跳过" % name)
        return
    size = os.path.getsize(ASSET)
    print("· 上传 %s（%d 字节）" % (name, size))
    if dry:
        print("  (dry-run) POST %s?name=%s" % (rel.get("upload_url", ""), name))
        return
    up = rel["upload_url"].split("{")[0] + "?name=" + urllib.request.quote(name)
    req = urllib.request.Request(up, data=open(ASSET, "rb").read(), method="POST", headers={
        "Authorization": "Bearer " + tok,
        "Content-Type": "application/octet-stream",
        "User-Agent": "erm-release-script",
    })
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.loads(r.read())
            print("  ✓ %s (%d bytes)" % (d.get("browser_download_url"), d.get("size", 0)))
    except urllib.error.HTTPError as e:
        sys.exit("上传失败：HTTP %s %s" % (e.code, e.read().decode("utf-8", "replace")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    tok = token() if not a.dry_run else "dry"
    print("== 1/3 推送提交与 tag")
    git_push(tok, a.dry_run)
    print("== 2/3 创建 Release %s" % TAG)
    rel = ensure_release(tok, a.dry_run)
    print("== 3/3 上传附件")
    if rel:
        ensure_asset(tok, rel, a.dry_run)
    print("完成。")


if __name__ == "__main__":
    main()
