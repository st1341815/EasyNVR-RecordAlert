#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标（纯标准库，不依赖 Pillow）。

产出：
  erm/ICON.PNG                 64x64   （应用列表）
  erm/ICON_256.PNG             256x256 （应用详情）
  erm/app/ui/images/icon_64.png
  erm/app/ui/images/icon_256.png

图形：深松绿圆角底 + 白色镜头环 + 红色录制点（表示"监控中"）。
"""

import os
import struct
import zlib

BG = (63, 111, 95)        # #3f6f5f
BG_DARK = (46, 84, 72)
FG = (250, 250, 247)
RED = (196, 74, 58)
SS = 4                    # 超采样倍数（抗锯齿）


def rounded_rect_mask(x, y, size, r):
    """圆角矩形内部判定（坐标已是像素中心）。"""
    cx = min(max(x, r), size - r)
    cy = min(max(y, r), size - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def render(size):
    n = size * SS
    buf = [[(0, 0, 0, 0)] * n for _ in range(n)]
    r = n * 0.22
    cx = cy = n / 2

    # 底：竖向微渐变 + 圆角
    for y in range(n):
        t = y / max(1, n - 1)
        col = tuple(int(BG[i] * (1 - t) + BG_DARK[i] * t) for i in range(3))
        for x in range(n):
            if rounded_rect_mask(x + 0.5, y + 0.5, n, r):
                buf[y][x] = col + (255,)

    # 镜头环（外环白、内空），用两个半径做环
    r_out = n * 0.30
    r_in = n * 0.21
    for y in range(n):
        for x in range(n):
            d2 = (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2
            if r_in ** 2 <= d2 <= r_out ** 2:
                buf[y][x] = FG + (255,)
            elif d2 < r_in ** 2:
                # 镜头玻璃：稍微压暗背景，形成层次
                buf[y][x] = (int(BG[0] * 0.70 + 18), int(BG[1] * 0.70 + 18),
                             int(BG[2] * 0.70 + 18), 255)

    # 录制红点（右上）
    rdot = n * 0.085
    dx, dy = cx + n * 0.235, cy - n * 0.235
    for y in range(n):
        for x in range(n):
            d2 = (x + 0.5 - dx) ** 2 + (y + 0.5 - dy) ** 2
            if d2 <= rdot ** 2:
                buf[y][x] = RED + (255,)
            elif d2 <= (rdot * 1.28) ** 2 and buf[y][x][3] > 0:
                a = buf[y][x]
                buf[y][x] = (a[0], a[1], a[2], 255)

    # 下采样（均值抗锯齿）
    out = [[(0, 0, 0, 0)] * size for _ in range(size)]
    for y in range(size):
        for x in range(size):
            rs = gs = bs = as_ = 0
            for j in range(SS):
                for i in range(SS):
                    p = buf[y * SS + j][x * SS + i]
                    a = p[3] / 255.0
                    rs += p[0] * a
                    gs += p[1] * a
                    bs += p[2] * a
                    as_ += p[3]
            cnt = SS * SS
            alpha = as_ / cnt
            if alpha <= 0:
                out[y][x] = (0, 0, 0, 0)
            else:
                w = as_ / 255.0
                out[y][x] = (int(rs / w), int(gs / w), int(bs / w), int(round(alpha)))
    return out


def write_png(path, pixels):
    size = len(pixels)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b""
    for row in pixels:
        raw += b"\x00" + b"".join(bytes(px) for px in row)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
           chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    return len(png)


def main():
    base = os.path.dirname(os.path.abspath(__file__))
    pkg = os.path.join(base, "erm")
    targets = [
        (64, os.path.join(pkg, "ICON.PNG")),
        (256, os.path.join(pkg, "ICON_256.PNG")),
        (64, os.path.join(pkg, "app", "ui", "images", "icon_64.png")),
        (256, os.path.join(pkg, "app", "ui", "images", "icon_256.png")),
    ]
    for size, path in targets:
        n = write_png(path, render(size))
        print("%-58s %5d px  %6d B" % (os.path.relpath(path, base), size, n))


if __name__ == "__main__":
    main()
