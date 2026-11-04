#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地提交 + tag 推到 GitHub，并（可选）创建 Release 上传 fpk。

用法：
    python3 tools/release.py                      # 只推代码+tag（走仓库配置的 SSH key）
    GH_TOKEN=ghp_xxx python3 tools/release.py     # 推代码 + 建 Release + 传 fpk
    python3 tools/release.py --dry-run            # 只打印将要做什么

设计要点：
· 推送默认走 SSH（本仓库 .git/config 里的 core.sshCommand 指向 ~/.ssh/id_ed25519_github）；
  给了 GH_TOKEN 才额外走 API 建 Release —— SSH key 不能调 REST API，这是两套凭据。
· token 只经 `git -c http.extraheader=...` 使用，不写进 .git/config、不留在 shell 历史；
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
NOTES = os.path.join(HERE, "docs", "release-%s.md" % TAG)
API = "https://api.github.com"


def token():
    return (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()


def request_url(url, tok, method="GET", payload=None, raw_file=None):
    headers = {
        "Authorization": "Bearer " + tok,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "erm-release-script",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw_file:
        with open(raw_file, "rb") as f:
            data = f.read()
        headers["Content-Type"] = "application/octet-stream"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = r.read()
            return r.status, (json.loads(body) if body[:1] in (b"{", b"[") else body)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def api(tok, path, method="GET", payload=None, raw_file=None):
    return request_url(API + path, tok, method, payload, raw_file)


def git_push(tok, dry):
    """有 token 走 https（token 只在 -c 参数里出现），没 token 走 SSH。"""
    if tok:
        basic = base64.b64encode(("x-access-token:" + tok).encode()).decode()
        cmd = ["git", "-C", HERE,
               "-c", "http.extraheader=Authorization: Basic " + basic,
               "push", "https://github.com/%s.git" % REPO, "HEAD:main", "--follow-tags"]
        shown = " ".join(x if "Basic" not in x else "http.extraheader=<token>" for x in cmd)
    else:
        cmd = ["git", "-C", HERE, "push", "origin", "HEAD:main", "--follow-tags"]
        shown = " ".join(cmd)
    print("+", shown)
    if dry:
        return
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    if r.returncode != 0:
        sys.exit("git push 失败（退出码 %d）" % r.returncode)


def release_body():
    """优先用 hand-written 的发版说明，其次从 CHANGELOG 抽对应段落。"""
    if os.path.isfile(NOTES):
        with open(NOTES, encoding="utf-8") as f:
            return f.read().strip()
    with open(os.path.join(HERE, "CHANGELOG.md"), encoding="utf-8") as f:
        txt = f.read()
    i = txt.find("## " + TAG)
    j = txt.find("\n## v", i + 5)
    return txt[i:j].strip() if i >= 0 else txt[:2000]


def ensure_release(tok, dry):
    code, data = api(tok, "/repos/%s/releases/tags/%s" % (REPO, TAG))
    if code == 200:
        print("· Release %s 已存在：%s" % (TAG, data.get("html_url")))
        return data
    print("· Release %s 不存在，创建中…" % TAG)
    if dry:
        print("  (dry-run) POST /repos/%s/releases tag=%s" % (REPO, TAG))
        return None
    code, data = api(tok, "/repos/%s/releases" % REPO, "POST", {
        "tag_name": TAG,
        "name": "EasyNVR-RecordAlert %s" % TAG,
        "body": release_body(),
        "draft": False,
        "prerelease": False,
    })
    if code not in (200, 201):
        sys.exit("创建 Release 失败：%s %s" % (code, data))
    print("· 已创建：%s" % data.get("html_url"))
    return data


def upload_asset(tok, rel, dry):
    if not os.path.isfile(ASSET):
        sys.exit("找不到附件：%s（先 bash build.sh，再复制成带版本号的名字）" % ASSET)
    name = os.path.basename(ASSET)
    existing = {a["name"] for a in (rel.get("assets") or [])}
    if name in existing:
        print("· 附件 %s 已存在，跳过" % name)
        return
    up = (rel.get("upload_url") or "").split("{")[0]
    if not up:
        sys.exit("Release 没有 upload_url，无法上传")
    print("· 上传 %s（%.1f KB）…" % (name, os.path.getsize(ASSET) / 1024))
    if dry:
        print("  (dry-run) POST %s?name=%s" % (up, name))
        return
    code, data = request_url(up + "?name=" + name, tok, "POST", raw_file=ASSET)
    if code not in (200, 201):
        sys.exit("上传失败：%s %s" % (code, data))
    print("· 上传完成：%s" % data.get("browser_download_url"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-push", action="store_true")
    args = ap.parse_args()
    tok = token()

    print("== 1/3 推送提交与 tag")
    if args.skip_push:
        print("· 跳过")
    else:
        git_push(tok, args.dry_run)

    if not tok:
        print("\n== 2/3 创建 Release：跳过（没有 GH_TOKEN；SSH key 不能调 REST API）")
        print("   网页手建： https://github.com/%s/releases/new?tag=%s" % (REPO, TAG))
        print("   正文粘：   docs/release-%s.md" % TAG)
        print("   附件拖：   %s" % os.path.basename(ASSET))
        return 0

    print("\n== 2/3 创建 Release")
    rel = ensure_release(tok, args.dry_run)
    print("\n== 3/3 上传附件")
    if rel:
        upload_asset(tok, rel, args.dry_run)
    elif args.dry_run:
        print("  (dry-run) 会先创建再上传 %s" % os.path.basename(ASSET))
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
