#!/bin/bash
# 一键构建 EasyNVR 录像监控 的 .fpk
# 用法：bash build.sh [fnpack路径]   （默认 /usr/local/bin/fnpack）
set -e
cd "$(dirname "$0")"

# 沙箱/宿主可能注入全局 PYTHONPATH（里面的 Pillow/pydantic 是坏的），一律清掉
export PYTHONPATH=

PKG=erm
FN="${1:-/usr/local/bin/fnpack}"
[ -x "$FN" ] || { echo "找不到 fnpack：$FN" >&2; exit 1; }

echo "[1/6] 清理 __pycache__ / *.pyc"
find "$PKG" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$PKG" -name '*.pyc' -delete 2>/dev/null || true

echo "[2/6] 校准权限"
find "$PKG" -type d -exec chmod 755 {} +
chmod 755 "$PKG"/cmd/*
chmod 755 "$PKG"/app/server.py "$PKG"/app/monitor.py "$PKG"/app/config.py \
          "$PKG"/app/notify.py "$PKG"/app/scripts/*.py
find "$PKG" -type f ! -path "$PKG/cmd/*" ! -name '*.py' -exec chmod 644 {} +

echo "[3/6] 校验 JSON 文件（privilege / resource / wizard / ui/config）"
for f in "$PKG/config/privilege" "$PKG/config/resource" \
         "$PKG/wizard/install" "$PKG/wizard/config" "$PKG/app/ui/config"; do
    [ -f "$f" ] || { echo "缺少 $f" >&2; exit 1; }
    python3 -c "import json,sys;json.load(open(sys.argv[1],encoding='utf-8'))" "$f" \
        || { echo "JSON 非法：$f" >&2; exit 1; }
    echo "   ok  $f"
done

echo "[4/6] 校验 manifest（飞牛是平铺 key = value，不能有 [section]）"
grep -q '^\[' "$PKG/manifest" && { echo "manifest 里出现 [section]，飞牛不认" >&2; exit 1; }
for k in appname version display_name desc source platform maintainer \
         desktop_uidir desktop_applaunchname ctl_stop checkport; do
    grep -qE "^${k}[[:space:]]*=" "$PKG/manifest" || { echo "manifest 缺少 $k" >&2; exit 1; }
done
echo "   ok  $PKG/manifest（$(grep -E '^version' "$PKG/manifest" | awk '{print $3}')）"

echo "[5/6] 语法 + 导入自检"
python3 -m py_compile "$PKG"/app/*.py "$PKG"/app/scripts/*.py
find "$PKG" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
# 语法通过不等于能跑：模块级代码里的 NameError（引用顺序写错）只有真导入才会暴露，
# 这次实测就踩过（server.py 在 import config 之前引用了 C.VAR_DIR）。
TMPVAR="$(mktemp -d)"
TRIM_PKGVAR="$TMPVAR" python3 -c "
import sys
sys.path.insert(0, '$PKG/app')
import config, monitor, notify, server
print('   import ok:', server.VERSION)
"
rm -rf "$TMPVAR"
find "$PKG" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

echo "[6/6] 打包"
mkdir -p dist
rm -f "$PKG.fpk"
"$FN" build -d "$PKG"
mv -f "$PKG.fpk" dist/
ls -la "dist/$PKG.fpk"
sha256sum "dist/$PKG.fpk"

VER="$(awk '/^version[[:space:]]*=/{print $3; exit}' "$PKG/manifest")"
echo
echo "构建完成：dist/$PKG.fpk（v${VER}）"
echo "安装（本机）：sudo appcenter-cli install-fpk $(pwd)/dist/$PKG.fpk --volume-id 1 --yes"
