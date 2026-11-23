#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
漫剧（motion-comic）通用生产管线 —— 单文件自包含，供多智能体（分镜导演/画师/配音/合成）
通过 shell 子命令调用。对任意小说章节 md 通用，无特定作品文案。

来源：D:/AI/code/manju 的 render_manju.py (v1) / manju_av.py (v2) / manju_anim.py (v3)
全部内联，不 import manju 目录。

子命令：
  shots   --chapter <md> --out <shots.json>
      分镜：{title, badge, shots:[{kind:"narration"|"dialogue"|"sys", text}]}
  panels  --shots <shots.json> --out <dir> --ep <N>
      画师：标题卡 + 每镜一张 + 片尾卡 PNG（panel_000.png ...）+ poster.png + panels.json
  tts     --shots <shots.json> --out <dir> [--ep <N>]
      配音：edge_tts 并发合成（逐句验证、断点续跑），44100 单声道，audio.json
  compose --shots <shots.json> (--panels <dir> | --clips <dir>) --audio <dir> --out <ep.mp4> --ep <N>
          [--manifest <path>]
      合成：逐帧推拉/平移/手持抖动 + 颗粒 + 字幕淡入，rawvideo 管道进 ffmpeg，
      语音按镜补零 + 低频 drone amix；cv2/ffmpeg 双重校验；写/合并 manifest；失败 exit 1。

依赖：numpy cv2 PIL imageio_ffmpeg soundfile edge_tts
"""
import os
import re
import sys
import json
import math
import time
import hashlib
import asyncio
import argparse
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import cv2
import imageio_ffmpeg
import soundfile as sf
from PIL import Image, ImageDraw, ImageFont

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------- 常量（取自 render_manju / manju_av）
W, H = 720, 1280
BW, BH = 900, 1600
FPS = 20
SR = 44100

FONT_R = r"C:\Windows\Fonts\msyh.ttc"
FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"
_fonts = {}

VOICE = {
    "narration": "zh-CN-YunjianNeural",   # 低沉男声，旁白
    "dialogue": "zh-CN-YunxiNeural",      # 年轻男声，对白
    "sys": "zh-CN-YunyangNeural",         # 播报腔，系统提示
    "title": "zh-CN-YunjianNeural",
    "end": "zh-CN-YunjianNeural",
}


def get_font(size, bold=False):
    key = (int(size), bool(bold))
    if key in _fonts:
        return _fonts[key]
    path = FONT_B if bold else FONT_R
    f = None
    for cand in (path, r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc"):
        if os.path.exists(cand):
            try:
                f = ImageFont.truetype(cand, int(size))
                break
            except Exception:
                continue
    if f is None:
        f = ImageFont.load_default()
    _fonts[key] = f
    return f


# ---------------------------------------------------------------- palettes（render_manju 原样）
def pal(top, bottom, g1, g2, a1, a2):
    return dict(
        top=tuple(top), bottom=tuple(bottom),
        glows=[(240.0, 340.0, 290.0, g1), (720.0, 1000.0, 350.0, g2)],
        silhouette=tuple(max(6, int(c * 0.42)) for c in bottom),
        ground=tuple(max(5, int(c * 0.30)) for c in top),
        figure=(9, 9, 20),
        accent1=tuple(a1), accent2=tuple(a2),
        rain=(150, 200, 230),
    )


PALETTES = [
    pal((12, 12, 32), (54, 22, 74), (64, 36, 104), (18, 62, 86), (70, 235, 235), (255, 72, 190)),
    pal((16, 10, 26), (78, 28, 40), (96, 40, 30), (40, 20, 70), (255, 190, 90), (255, 96, 120)),
    pal((8, 16, 24), (20, 66, 62), (16, 70, 74), (60, 40, 20), (110, 255, 210), (250, 210, 100)),
    pal((18, 12, 30), (40, 24, 68), (56, 34, 92), (24, 70, 90), (150, 190, 255), (255, 110, 160)),
    pal((10, 14, 28), (30, 46, 78), (40, 60, 92), (70, 26, 70), (120, 220, 255), (255, 96, 210)),
    pal((22, 10, 20), (70, 20, 34), (92, 30, 36), (34, 20, 52), (255, 160, 150), (120, 220, 255)),
    pal((10, 18, 18), (24, 58, 44), (30, 66, 50), (46, 40, 26), (140, 250, 190), (255, 200, 90)),
    pal((14, 10, 34), (44, 20, 66), (52, 30, 96), (16, 56, 78), (110, 210, 255), (235, 120, 255)),
    pal((12, 14, 24), (36, 30, 58), (44, 36, 84), (26, 56, 76), (190, 220, 255), (255, 140, 120)),
    pal((16, 12, 20), (60, 30, 30), (78, 34, 26), (24, 40, 60), (255, 210, 140), (150, 200, 255)),
]


def palette_of(ep):
    return PALETTES[(ep - 1) % len(PALETTES)]


def badge_of(title):
    return re.sub(r"^第[一二三四五六七八九十百零〇0-9]+章\s*", "", title).strip() or title


# ---------------------------------------------------------------- 分镜（render_manju.build_shots 原样）
def wrap_cjk(text, font, maxw):
    lines = []
    for para in str(text).split("\n"):
        cur = ""
        for ch in para:
            if font.getlength(cur + ch) <= maxw:
                cur += ch
            else:
                if cur:
                    lines.append(cur)
                cur = ch
        lines.append(cur)
    return "\n".join([l for l in lines])


def chunk_text(s, maxlen):
    sents = re.findall(r"[^。！？；]*[。！？；]?", s)
    sents = [x for x in sents if x]
    res, cur = [], ""
    for sen in sents:
        if len(cur) + len(sen) <= maxlen:
            cur += sen
            continue
        if cur:
            res.append(cur)
            cur = ""
        while len(sen) > maxlen:
            res.append(sen[:maxlen])
            sen = sen[maxlen:]
        cur = sen
    if cur.strip():
        res.append(cur)
    return [r.strip() for r in res if r.strip()]


def _assemble(lines, maxlen):
    pieces = []
    for l in lines:
        if "【" in l:
            for m in re.finditer(r"【[^】]*】", l):
                pieces.append(("sys", m.group(0)))
            rest = re.sub(r"【[^】]*】", "", l).strip()
            for c in chunk_text(rest, maxlen):
                pieces.append(("txt", c))
            continue
        for c in chunk_text(l, maxlen):
            pieces.append(("txt", c))

    shots, sysbuf = [], []

    def flush():
        if sysbuf:
            shots.append(("sys", "\n".join(sysbuf)))
            sysbuf.clear()

    for kind, txt in pieces:
        if kind == "sys":
            sysbuf.append(txt)
            if sum(len(x) for x in sysbuf) > 68 or len(sysbuf) >= 3:
                flush()
            continue
        flush()
        t = "dialogue" if ("“" in txt or '"' in txt or "”" in txt) else "narration"
        shots.append((t, txt))
    flush()
    return shots


def build_shots(md, target=17):
    title = ""
    lines = []
    for raw in md.split("\n"):
        p = raw.strip()
        if not p:
            continue
        if p.startswith("#"):
            if not title:
                title = p.lstrip("# ").strip()
            continue
        if re.fullmatch(r"[-—_=*·]{2,}", p):
            continue
        p = p.replace("**", "").replace("*", "")
        if p:
            lines.append(p)
    total = sum(len(l) for l in lines)
    maxlen = int(max(46, min(130, total / max(target, 1))))
    shots = []
    for _ in range(5):
        shots = _assemble(lines, maxlen)
        if len(shots) <= 20:
            break
        maxlen = int(maxlen * 1.35)
    return title or "无题", shots[:20]


# ---------------------------------------------------------------- 背景（render_manju.make_panel_bg 原样）
def make_panel_bg(p, rng):
    ys = np.linspace(0.0, 1.0, BH, dtype=np.float32)[:, None, None]
    top = np.array(p["top"], np.float32)
    bot = np.array(p["bottom"], np.float32)
    img = np.repeat(top + (bot - top) * ys, BW, axis=1)
    yy = np.arange(BH, dtype=np.float32)[:, None]
    xx = np.arange(BW, dtype=np.float32)[None, :]
    for (gx, gy, rad, col) in p["glows"]:
        g = np.exp(-(((xx - gx) ** 2 + (yy - gy) ** 2) / (2.0 * rad * rad)))
        img = img + g[:, :, None] * np.array(col, np.float32)
    pim = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(pim)

    hz = int(BH * 0.76)
    x = -30
    while x < BW + 30:
        bw = int(rng.integers(56, 142))
        bh = int(rng.integers(110, 340))
        d.rectangle([x, hz - bh, x + bw, hz], fill=p["silhouette"])
        if rng.random() < 0.3:
            d.line([x + bw // 2, hz - bh, x + bw // 2, hz - bh - int(rng.integers(14, 54))],
                   fill=p["silhouette"], width=2)
        wy = hz - bh + 14
        while wy < hz - 10:
            wx = x + 8
            while wx < x + bw - 8:
                if rng.random() < 0.16:
                    c = p["accent2"] if rng.random() < 0.4 else p["accent1"]
                    d.rectangle([wx, wy, wx + 5, wy + 7],
                                fill=tuple(int(v * 0.55) for v in c))
                wx += 13
            wy += 16
        x += bw + int(rng.integers(3, 18))
    d.rectangle([0, hz, BW, BH], fill=p["ground"])
    for _ in range(46):
        rx = float(rng.uniform(0, BW))
        ry = float(rng.uniform(hz, BH))
        rw = float(rng.uniform(24, 130))
        d.line([(rx, ry), (rx + rw, ry)],
               fill=tuple(min(255, int(c * 0.34)) for c in p["accent1"]), width=1)
    for _ in range(220):
        rx = float(rng.uniform(-20, BW))
        ry = float(rng.uniform(0, BH))
        ln = float(rng.uniform(18, 62))
        d.line([(rx, ry), (rx - 7, ry + ln)],
               fill=tuple(int(c * 0.45) for c in p["rain"]), width=1)
    return pim


# ---------------------------------------------------------------- 人物（manju_av.draw_hero / make_panel 原样）
SKIN = (236, 214, 194)
SKIN_D = (176, 146, 132)
HAIR = (10, 10, 20)


def draw_hero(d, p, cx, base, sc, facing=1, back=False, weapon=True):
    """赛璐璐上色的人物：肤色脸 + 眼睛 + 头发 + 大衣 + 双边轮廓光。"""
    a1, a2 = p["accent1"], p["accent2"]
    coat = (16, 18, 34) if not back else (11, 12, 24)
    w = max(2, int(2.2 * sc))
    f = facing

    # 腿
    d.polygon([(cx - 30 * sc, base), (cx - 22 * sc, base - 150 * sc),
               (cx - 4 * sc, base - 150 * sc), (cx - 9 * sc, base)], fill=coat)
    d.polygon([(cx + 12 * sc, base), (cx + 6 * sc, base - 150 * sc),
               (cx + 26 * sc, base - 150 * sc), (cx + 36 * sc, base)], fill=coat)

    # 大衣/躯干（下摆外张）
    torso = [(cx - 74 * sc, base - 118 * sc), (cx - 58 * sc, base - 300 * sc),
             (cx - 32 * sc, base - 376 * sc), (cx + 32 * sc, base - 376 * sc),
             (cx + 58 * sc, base - 300 * sc), (cx + 78 * sc, base - 112 * sc),
             (cx + 46 * sc, base - 146 * sc), (cx - 44 * sc, base - 152 * sc)]
    d.polygon(torso, fill=coat)

    # 肩线 + 左右轮廓光
    d.line([(cx - 74 * sc, base - 118 * sc), (cx - 58 * sc, base - 300 * sc),
            (cx - 32 * sc, base - 376 * sc)], fill=a1, width=w, joint="curve")
    d.line([(cx + 78 * sc, base - 112 * sc), (cx + 58 * sc, base - 300 * sc),
            (cx + 32 * sc, base - 376 * sc)], fill=a2, width=w, joint="curve")

    # 领口
    d.polygon([(cx - 30 * sc, base - 378 * sc), (cx - 8 * sc, base - 318 * sc),
               (cx + 8 * sc, base - 318 * sc), (cx + 30 * sc, base - 378 * sc),
               (cx + 17 * sc, base - 394 * sc), (cx - 17 * sc, base - 394 * sc)],
              fill=(8, 9, 18))

    # 手臂（一手垂下、一手持械）
    d.polygon([(cx - 58 * sc, base - 300 * sc), (cx - 84 * sc, base - 190 * sc),
               (cx - 66 * sc, base - 176 * sc), (cx - 42 * sc, base - 296 * sc)], fill=coat)
    d.polygon([(cx + 56 * sc, base - 300 * sc), (cx + 96 * sc, base - 196 * sc),
               (cx + 78 * sc, base - 178 * sc), (cx + 40 * sc, base - 296 * sc)], fill=coat)

    # 脖子 + 头
    hy = base - 432 * sc
    hr = 40 * sc
    d.rectangle([cx - 12 * sc, base - 396 * sc, cx + 12 * sc, base - 372 * sc], fill=SKIN_D)
    d.ellipse([cx - hr, hy - hr * 1.12, cx + hr, hy + hr * 1.02], fill=SKIN)
    d.polygon([(cx + hr * 0.25, hy - hr * 1.05), (cx + hr * 0.98, hy - hr * 0.35),
               (cx + hr * 0.72, hy + hr * 0.72), (cx + hr * 0.2, hy + hr * 0.95)],
              fill=SKIN_D)
    # 头发（凌乱短发 + 刘海）
    d.polygon([(cx - hr * 1.04, hy - hr * 0.18), (cx - hr * 1.16, hy - hr * 1.42),
               (cx - hr * 0.32, hy - hr * 1.9), (cx + hr * 0.62, hy - hr * 1.72),
               (cx + hr * 1.06, hy - hr * 0.98), (cx + hr * 1.02, hy - hr * 0.28),
               (cx + hr * 0.72, hy - hr * 0.86), (cx + hr * 0.1, hy - hr * 0.72),
               (cx - hr * 0.5, hy - hr * 0.9)], fill=HAIR)
    d.line([(cx - hr * 1.04, hy - hr * 0.2), (cx - hr * 1.14, hy - hr * 1.4)],
           fill=a1, width=w)
    d.line([(cx + hr * 1.04, hy - hr * 0.3), (cx + hr * 1.06, hy - hr * 1.0)],
           fill=a2, width=w)
    # 眼睛（发光高光，动漫感）
    ey = hy - hr * 0.1
    for exe, ew in ((cx - hr * 0.44, 1.0), (cx + hr * 0.44, 0.9)):
        d.ellipse([exe - 7 * sc * ew, ey - 4 * sc, exe + 7 * sc * ew, ey + 4 * sc], fill=(18, 16, 26))
        d.ellipse([exe - 3.4 * sc * ew, ey - 3 * sc, exe + 1.6 * sc * ew, ey + 2.4 * sc], fill=a1)
        d.ellipse([exe - 1.6 * sc, ey - 2.2 * sc, exe + 0.6 * sc, ey - 0.2 * sc], fill=(255, 255, 255))
    # 眉/嘴
    d.line([(cx - hr * 0.66, ey - 13 * sc), (cx - hr * 0.2, ey - 15 * sc)], fill=HAIR, width=max(2, int(2 * sc)))
    d.line([(cx + hr * 0.2, ey - 15 * sc), (cx + hr * 0.66, ey - 12 * sc)], fill=HAIR, width=max(2, int(2 * sc)))
    d.line([(cx - 6 * sc, hy + hr * 0.52), (cx + 10 * sc, hy + hr * 0.5)], fill=(120, 74, 74), width=max(1, int(1.6 * sc)))

    if weapon and not back:
        x0, y0 = cx + 70 * sc * f, base - 190 * sc
        x1, y1 = cx + 168 * sc * f, base - 452 * sc
        d.line([(x0, y0), (x1, y1)], fill=tuple(int(c * 0.6) for c in a2), width=max(3, int(9 * sc)))
        d.line([(x0, y0), (x1, y1)], fill=(236, 250, 255), width=max(1, int(2 * sc)))
        d.ellipse([x0 - 10 * sc, y0 - 10 * sc, x0 + 10 * sc, y0 + 10 * sc],
                  fill=(12, 12, 22), outline=a2, width=max(1, int(2 * sc)))


def make_panel(p, seed, kind, cx=0.5):
    rng = np.random.default_rng(seed)
    bg = make_panel_bg(p, rng)
    d = ImageDraw.Draw(bg, "RGBA")
    if kind == "duo":
        # 过肩双人：前景大肩膀 + 背对镜头，后景人物面向镜头
        draw_hero(d, p, cx * BW + 210, BH + 120, 3.2, facing=-1, back=True, weapon=False)
        draw_hero(d, p, cx * BW - 120, BH - 60, 1.45, facing=1)
        d.rectangle([0, int(BH * 0.9), BW, BH], fill=(6, 6, 14, 255))
    elif kind == "close":
        draw_hero(d, p, cx * BW, BH + 60, 2.35, facing=1)
    elif kind == "mid":
        draw_hero(d, p, cx * BW + 30, BH - 50, 1.15, facing=-1)
    else:  # none / 环境远景
        draw_hero(d, p, cx * BW - 150, BH - 70, 0.82, facing=1)
        draw_hero(d, p, cx * BW + 175, BH - 40, 1.02, facing=-1, back=True, weapon=False)
    return np.array(bg.convert("RGB"))


# ---------------------------------------------------------------- 帧处理
def crop_zoom(img, zoom, cx, cy):
    zoom = max(1.0, float(zoom))
    w = int(BW / zoom)
    h = int(BH / zoom)
    x = int(cx * BW - w / 2)
    y = int(cy * BH - h / 2)
    x = max(0, min(BW - w, x))
    y = max(0, min(BH - h, y))
    c = np.ascontiguousarray(img[y:y + h, x:x + w])
    return cv2.resize(c, (W, H), interpolation=cv2.INTER_AREA)


def add_grain(fr, rng, amp=3.0):
    n = rng.normal(0.0, amp, (H, W, 1)).astype(np.float32)
    return np.clip(fr.astype(np.float32) + n, 0, 255).astype(np.uint8)


# manju_anim 的快速颗粒：预生成 6 张 uint8 噪声轮换 + cv2.add（视觉等价，~1ms/帧）
_GRAIN_BANK = None


def fast_grain(fr, fi, amp=2):
    global _GRAIN_BANK
    if _GRAIN_BANK is None:
        _GRAIN_BANK = [np.random.default_rng(300 + i).integers(
            0, amp + 1, (H, W, 1), dtype=np.uint8) for i in range(6)]
    return cv2.add(fr, np.repeat(_GRAIN_BANK[fi % 6], 3, axis=2))


# ---------------------------------------------------------------- overlays（render_manju 原样，文案通用化）
def _pack(ov):
    arr = np.array(ov)
    alpha = arr[..., 3]
    ys, xs = np.nonzero(alpha)
    if len(ys) == 0:
        return None
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    return (arr[..., :3], alpha, (y0, y1, x0, x1))


def composite(frame, ovl, fade=1.0):
    if ovl is None or fade <= 0:
        return frame
    rgb, a, (y0, y1, x0, x1) = ovl
    sub = frame[y0:y1, x0:x1].astype(np.float32)
    al = (a[y0:y1, x0:x1].astype(np.float32) / 255.0 * fade)[..., None]
    frame[y0:y1, x0:x1] = (sub * (1 - al) + rgb[y0:y1, x0:x1].astype(np.float32) * al).astype(np.uint8)
    return frame


def fast_composite(frame, ovl, fade=1.0):
    """manju_anim：fade==1 用布尔掩码直接拷贝，仅淡入帧走浮点路径。"""
    if ovl is None or fade <= 0:
        return frame
    rgb, a, (y0, y1, x0, x1) = ovl
    sub = frame[y0:y1, x0:x1]
    al = a[y0:y1, x0:x1]
    src = rgb[y0:y1, x0:x1]
    if fade >= 1.0:
        m = al > 0
        sub[m] = src[m]
    else:
        af = (al.astype(np.float32) / 255.0 * fade)[..., None]
        sub[:] = (sub.astype(np.float32) * (1.0 - af)
                  + src.astype(np.float32) * af).astype(np.uint8)
    return frame


def badge_overlay(p, badge):
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    m = 42
    f = get_font(23, True)
    txt = badge if len(badge) <= 16 else badge[:16] + "…"
    d.rectangle([m, 46, m + 8, 78], fill=p["accent2"])
    d.text((m + 20, 48), txt, font=f, fill=(214, 224, 240))
    return _pack(ov)


def body_overlay(kind, text, p, badge):
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    margin = 46
    if kind == "sys":
        fs, bold = 29, False
        maxw = W - 2 * margin - 96
    elif kind == "dialogue":
        fs, bold = 34, True
        maxw = W - 2 * margin - 52
    else:
        fs, bold = 33, False
        maxw = W - 2 * margin - 52
    font = get_font(fs, bold)
    wrapped = wrap_cjk(text, font, maxw)
    nlines = wrapped.count("\n") + 1
    lh = int(fs * 1.52)
    box_h = nlines * lh + 54
    x0, x1 = margin, W - margin
    y0 = H - 116 - box_h
    if y0 < 140:
        y0 = 140

    if kind == "dialogue":
        fill, edge, tcol = (17, 13, 27, 236), p["accent2"], (240, 243, 252)
    elif kind == "sys":
        fill, edge, tcol = (6, 28, 34, 238), p["accent1"], (126, 250, 244)
    else:
        fill, edge, tcol = (11, 11, 23, 226), p["accent1"], (234, 238, 248)

    d.rounded_rectangle([x0, y0, x1, y0 + box_h], radius=22, fill=fill, outline=edge, width=3)
    if kind == "dialogue":
        d.polygon([(x0 + 58, y0 + box_h - 2), (x0 + 104, y0 + box_h - 2), (x0 + 62, y0 + box_h + 32)],
                  fill=fill, outline=edge)
    if kind == "sys":
        tag = "系统"
        ft = get_font(20, True)
        tw = ft.getlength(tag) + 26
        d.rounded_rectangle([x0 + 22, y0 - 17, x0 + 22 + tw, y0 + 21], radius=10,
                            fill=(8, 40, 48, 255), outline=edge, width=2)
        d.text((x0 + 34, y0 - 12), tag, font=ft, fill=(140, 255, 246))
        for i in range(0, box_h, 9):
            d.line([(x1 - 40, y0 + 10 + i), (x1 - 14, y0 + 10 + i)],
                   fill=(30, 120, 130, 90), width=1)
        ty = y0 + 30
    else:
        d.rectangle([x0, y0 + 12, x0 + 7, y0 + box_h - 12], fill=edge)
        ty = y0 + 27

    d.multiline_text((x0 + 32, ty), wrapped, font=font, fill=tcol, spacing=10)

    packed = _pack(ov)
    b = badge_overlay(p, badge)
    if b is not None and packed is not None:
        rgb2, a2c, box2 = b
        rgb1, a1c, box1 = packed
        ov2 = Image.fromarray(np.dstack([rgb1, a1c]))
        ov2.alpha_composite(Image.fromarray(np.dstack([rgb2, a2c])))
        return _pack(ov2)
    return packed


def title_overlay(title, p, subtitle):
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    margin = 44
    f1 = get_font(54, True)
    wrapped = wrap_cjk(title, f1, W - 2 * margin - 96)
    nlines = wrapped.count("\n") + 1
    lh = int(54 * 1.42)
    box_h = nlines * lh + 78
    y0 = int(H * 0.40) - box_h // 2
    d.rounded_rectangle([margin, y0, W - margin, y0 + box_h], radius=20,
                        fill=(8, 8, 18, 226), outline=p["accent1"], width=3)
    d.rectangle([margin, y0, margin + 10, y0 + box_h], fill=p["accent2"])
    d.multiline_text((margin + 42, y0 + 40), wrapped, font=f1, fill=(240, 244, 252), spacing=12)
    f2 = get_font(24, False)
    d.text((margin + 42, y0 + box_h + 26), subtitle, font=f2, fill=p["accent1"])
    f3 = get_font(22, False)
    d.text((margin + 42, y0 - 46), "漫剧", font=f3, fill=p["accent2"])
    return _pack(ov)


def end_overlay(text, p):
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    f = get_font(38, True)
    wrapped = wrap_cjk(text, f, W - 160)
    d.multiline_text((80, int(H * 0.46)), wrapped, font=f, fill=(236, 240, 250), spacing=12)
    d.line([(80, int(H * 0.62)), (W - 120, int(H * 0.62))], fill=p["accent2"], width=3)
    return _pack(ov)


# ---------------------------------------------------------------- TTS（manju_av.voice_of / audio_for）
def _speak(text, voice, pitch, mp3):
    import edge_tts

    async def go():
        synthesis = edge_tts.Communicate(
            text, voice, pitch=pitch, rate="+10%", connect_timeout=10, receive_timeout=20,
        )
        await asyncio.wait_for(synthesis.save(mp3), timeout=35)

    asyncio.run(go())


def voice_cache_key(text, voice, pitch):
    style = voice if pitch == "+0Hz" else voice + "|" + pitch
    return hashlib.md5((style + "|" + text).encode("utf-8")).hexdigest()[:20]


def verified_voice_samples(path):
    """Read the audio, including its tail, before reusing it in a timeline."""
    try:
        with open(path, "rb") as stream:
            header = stream.read(12)
            if (len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE"
                    or int.from_bytes(header[4:8], "little") + 8 != os.fstat(stream.fileno()).st_size):
                return 0
            stream.seek(0)
            with sf.SoundFile(stream) as wav:
                if wav.samplerate != SR or wav.channels != 1 or wav.subtype != "PCM_16":
                    return 0
                samples = wav.read(dtype="float32")
                if len(samples) != wav.frames or len(samples) < SR // 20:
                    return 0
                if not np.isfinite(samples).all() or float(np.max(np.abs(samples))) <= 0.001:
                    return 0
                return len(samples)
    except (OSError, ValueError, RuntimeError):
        return 0


def voice_of(text, voice, pitch, cache_dir):
    wav = os.path.join(cache_dir, voice_cache_key(text, voice, pitch) + ".wav")
    cached = verified_voice_samples(wav)
    if cached:
        return cached, True
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    last_error = None
    for attempt in range(3):
        try:
            with tempfile.TemporaryDirectory(dir=cache_dir, prefix=".voice-") as staging:
                mp3 = os.path.join(staging, "voice.mp3")
                pending = os.path.join(staging, "voice.wav")
                _speak(text, voice, pitch, mp3)
                result = subprocess.run(
                    [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", mp3,
                     "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", pending],
                    capture_output=True, timeout=25,
                )
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.decode("utf-8", "replace")[-240:])
                count = verified_voice_samples(pending)
                if not count:
                    raise RuntimeError("invalid or silent PCM audio")
                os.replace(pending, wav)
                return count, False
        except Exception as error:
            last_error = error
        if attempt < 2:
            time.sleep(1.2 + attempt * 0.8)
    raise RuntimeError(str(last_error) or type(last_error).__name__) from last_error


def pad_to(data, n):
    if len(data) >= n:
        return data[:n]
    out = np.zeros(n, dtype=np.float32)
    out[:len(data)] = data
    return out


# ---------------------------------------------------------------- 编码（manju_av.encode 原样）
def encode(frames_iter, out_path, dur, voice_wav, freq):
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    af = ("[2:a]volume=0.045,tremolo=f=0.6:d=0.6,afade=t=in:st=0:d=1.4,"
          "afade=t=out:st=%.2f:d=1.8[dr];"
          "[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=mono,apad[v];"
          "[v][dr]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.97[a]"
          % max(0.0, dur - 2.0))
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H),
           "-r", str(FPS), "-i", "-",
           "-i", voice_wav,
           "-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=44100:duration=%.2f" % (freq, dur),
           "-filter_complex", af,
           "-map", "0:v", "-map", "[a]",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", out_path]
    with tempfile.TemporaryFile() as log, subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log) as pr:
        try:
            for fr in frames_iter:
                pr.stdin.write(fr.tobytes())
            pr.stdin.close()
        except BrokenPipeError:
            pass
        except BaseException:
            pr.kill()
            raise
        rc = pr.wait()
        log.seek(0)
        err = log.read(800).decode("utf-8", "ignore")
    if rc != 0:
        raise RuntimeError("ffmpeg rc=%d %s" % (rc, err[:800]))


# ---------------------------------------------------------------- 公共工具
def load_json(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def save_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def manifest_asset(directory, filename):
    root = os.path.realpath(directory)
    path = os.path.realpath(os.path.join(root, filename))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("清单文件路径越过产物目录：%s" % filename)
    return path


def clip_manifest(directory, storyboard):
    manifest = load_json(manifest_asset(directory, "clips.json"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("clips"), list):
        raise ValueError("视频片段清单必须包含 clips 数组")
    seconds = manifest.get("clip_seconds")
    if type(seconds) is not int or seconds not in (3, 4):
        raise ValueError("视频片段时长必须为 3 或 4 秒")
    fingerprint = manifest.get("storyboard_fingerprint")
    if fingerprint is not None:
        value = 0xcbf29ce484222325
        with open(storyboard, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                for byte in chunk:
                    value = ((value ^ byte) * 0x100000001b3) & 0xffffffffffffffff
        if fingerprint != f"{value:016x}":
            raise ValueError("视频片段清单与当前分镜不匹配，请重新生成受影响的镜头")
    return manifest, seconds


def decode_clip(path, target_frames):
    """Yield normalized portrait frames, excluding the video provider's audio."""
    if not path.lower().endswith(".mp4") or not os.path.isfile(path):
        raise ValueError("缺少 MP4 片段：%s" % path)
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    filters = ("fps=%d,scale=%d:%d:force_original_aspect_ratio=increase,"
               "crop=%d:%d,setsar=1" % (FPS, W, H, W, H))
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-xerror",
           "-protocol_whitelist", "file,pipe", "-f", "mov", "-i", path,
           "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", filters,
           "-frames:v", str(target_frames), "-f", "rawvideo", "-pix_fmt", "rgb24", "-threads", "1", "-"]
    with tempfile.TemporaryFile() as log, subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log) as proc:
        count = 0
        last = None
        try:
            for _ in range(target_frames):
                data = proc.stdout.read(W * H * 3)
                if not data:
                    break
                if len(data) != W * H * 3:
                    raise ValueError("视频片段帧不完整：%s" % path)
                last = np.frombuffer(data, dtype=np.uint8).reshape((H, W, 3))
                count += 1
                yield last
            code = proc.wait(timeout=20)
            if code or count < target_frames - 1:
                log.seek(0)
                error = log.read(500).decode("utf-8", "replace").strip()
                raise ValueError("视频片段解码失败或不足预期时长：%s（%d/%d 帧）%s" %
                                 (path, count, target_frames, error))
            if count < target_frames:
                yield last  # 容许提供商输出的末帧取整误差
        finally:
            if proc.poll() is None:
                proc.kill()


def shot_plan_meta(shots, ep):
    """标题卡 + 每镜 + 片尾的镜头元数据（kind→panel 映射 / 推拉 / 平移，取自 manju_av + manju_anim）。

    返回条目列表：role/kind/pk/z0/z1/cx/cy/px/py/ph/seed。
    """
    entries = []
    # 标题卡（manju_av：none 底图；manju_anim：横向平移）
    entries.append(dict(role="title", kind="title", pk="none",
                        z0=1.00, z1=1.05, cx=0.5, cy=0.5,
                        px=-0.04, py=0.0, ph=0.0, seed=5100 + ep))
    for si, sh in enumerate(shots, start=1):
        kind = sh["kind"]
        if kind == "sys":
            pk, z0, z1, cy, px, py = "none", 1.14, 1.04, 0.55, 0.05, 0.0
        elif kind == "dialogue":
            pk, z0, z1, cy, px, py = "duo", 1.02, 1.09, 0.46, -0.05, 0.02
        else:
            pk = "mid" if si % 3 == 1 else "none"
            z0, z1, cy, px, py = 1.04, 1.11, 0.50, 0.045, -0.02
        if si % 2 == 0:
            px, py = -px, -py
        entries.append(dict(role="shot", kind=kind, si=si, pk=pk,
                            z0=z0, z1=z1, cx=0.5, cy=cy,
                            px=px, py=py, ph=si * 1.7, seed=6000 + ep * 31 + si))
    # 片尾（manju_av：close 底图）
    entries.append(dict(role="end", kind="end", pk="close",
                        z0=1.12, z1=1.02, cx=0.5, cy=0.5,
                        px=0.03, py=-0.02, ph=3.1, seed=9000 + ep))
    return entries


def build_overlays(doc, ep, p):
    """按 shots.json 重建全部 overlay（标题/正文/片尾），与 panels 阶段一致。"""
    title, badge = doc["title"], doc["badge"]
    shots = doc["shots"]
    ovls = [title_overlay(title, p, "第 %d 话 · %s" % (ep, badge))]
    for sh in shots:
        ovls.append(body_overlay(sh["kind"], sh["text"], p, badge))
    ovls.append(end_overlay("第 %d 话 完" % ep, p))
    return ovls


# ---------------------------------------------------------------- 子命令：shots
def cmd_shots(args):
    with open(args.chapter, encoding="utf-8") as fh:
        md = fh.read()
    title, shots = build_shots(md)
    doc = dict(title=title, badge=badge_of(title),
               shots=[dict(kind=k, text=t) for k, t in shots])
    save_json(args.out, doc)
    print("[shots] %s | %d shots -> %s" % (title, len(shots), args.out), flush=True)


# ---------------------------------------------------------------- 子命令：panels
def cmd_panels(args):
    doc = load_json(args.shots)
    ep = args.ep
    p = palette_of(ep)
    os.makedirs(args.out, exist_ok=True)
    entries = shot_plan_meta(doc["shots"], ep)
    ovls = build_overlays(doc, ep, p)

    files, panels = [], []
    for i, e in enumerate(entries):
        panel = make_panel(p, e["seed"], e["pk"], e["cx"])
        name = "panel_%03d.png" % i
        cv2.imwrite(os.path.join(args.out, name), cv2.cvtColor(panel, cv2.COLOR_RGB2BGR))
        files.append(name)
        panels.append(panel)
        print("[panel] %s (%s/%s)" % (name, e["role"], e["pk"]), flush=True)

    # 封面：与 manju_av 相同取 plan[min(2, len-1)]
    pi = min(2, len(entries) - 1)
    e = entries[pi]
    post = composite(crop_zoom(panels[pi], 1.03, e["cx"], e["cy"]), ovls[pi], 1.0)
    cv2.imwrite(os.path.join(args.out, "poster.png"), cv2.cvtColor(post, cv2.COLOR_RGB2BGR))

    listing = []
    for name, e in zip(files, entries):
        listing.append(dict(file=name, role=e["role"], kind=e["kind"],
                            z0=e["z0"], z1=e["z1"], cx=e["cx"], cy=e["cy"],
                            px=e["px"], py=e["py"], ph=e["ph"]))
    save_json(os.path.join(args.out, "panels.json"),
              dict(ep=ep, title=doc["title"], panels=listing, poster="poster.png"))
    print("[panels] %d panels + poster -> %s" % (len(files), args.out), flush=True)


# ---------------------------------------------------------------- 子命令：tts
def cmd_tts(args):
    doc = load_json(args.shots)
    ep = args.ep
    cache = os.path.join(args.out, "cache")
    os.makedirs(cache, exist_ok=True)

    lines = [("title", "第%d话。%s" % (ep, doc["badge"]), None)]
    lines += [(sh["kind"], sh["text"], sh.get("speaker")) for sh in doc["shots"]]
    lines.append(("end", "第%d话，完。" % ep, None))

    cast = doc.get("voice_cast", {})
    if not isinstance(cast, dict):
        raise ValueError("voice_cast 必须是角色与音色设置的映射")
    grouped = {}
    for index, (kind, text, speaker) in enumerate(lines):
        if kind not in VOICE or not isinstance(text, str) or not text.strip():
            raise ValueError("第 %d 条配音缺少有效的 kind 或 text" % index)
        style = cast.get(speaker, {}) if isinstance(speaker, str) else {}
        if not isinstance(style, dict):
            raise ValueError("配音角色 %s 的设置须为对象" % speaker)
        voice, pitch = style.get("voice", VOICE[kind]), style.get("pitch", "+0Hz")
        if (not isinstance(voice, str) or not re.fullmatch(r"[A-Za-z]+-[A-Za-z0-9-]+Neural", voice)
                or not isinstance(pitch, str) or not re.fullmatch(r"[+-]\d{1,2}Hz", pitch)):
            raise ValueError("配音角色 %s 的 voice 或 pitch 格式错误" % speaker)
        grouped.setdefault((text, voice, pitch), []).append(index)

    failures = []
    entries = [None] * len(lines)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(voice_of, text, voice, pitch, cache): (text, voice, pitch, indexes)
            for (text, voice, pitch), indexes in grouped.items()
        }
        for future in as_completed(futures):
            text, voice, pitch, indexes = futures[future]
            try:
                samples, cached = future.result()
            except Exception as error:
                failures.append((indexes, str(error)))
                print("[tts] ERROR lines %s: %s" % (indexes, error), flush=True)
                continue
            key = voice_cache_key(text, voice, pitch)
            for index in indexes:
                kind, _, speaker = lines[index]
                minimum = 34 if kind == "title" else 28
                padding = 8 if kind == "end" else 10
                frames = max(minimum, round(samples / SR * FPS) + padding)
                entries[index] = dict(file="cache/" + key + ".wav", kind=kind,
                                      samples=samples, frames=frames,
                                      speaker=speaker, voice=voice, pitch=pitch)
                print("[tts] %02d %s %d samples -> %d frames (%s)" %
                      (index, kind, samples, frames, "cached" if cached else "generated"), flush=True)

    if failures:
        failed = sorted(index for indexes, _ in failures for index in indexes)
        raise RuntimeError("配音未完成，失败条目 %s；成功的音频已保存，重试时继续" % failed)
    with tempfile.TemporaryDirectory(dir=args.out, prefix=".audio-manifest-") as staging:
        pending = os.path.join(staging, "audio.json")
        save_json(pending, dict(ep=ep, sr=SR, fps=FPS, entries=entries,
                                provider="edge-tts"))
        os.replace(pending, os.path.join(args.out, "audio.json"))
    print("[tts] %d lines -> %s" % (len(entries), args.out), flush=True)


# ---------------------------------------------------------------- 子命令：compose
def cmd_compose(args):
    doc = load_json(args.shots)
    video_mode = args.clips is not None
    if video_mode:
        pj, clip_seconds = clip_manifest(args.clips, args.shots)
        visual_dir = args.clips
        entries = pj["clips"]
        clip_frames = clip_seconds * FPS
    else:
        pj = load_json(manifest_asset(args.panels, "panels.json"))
        visual_dir = args.panels
        entries = pj["panels"]
    aj = load_json(manifest_asset(args.audio, "audio.json"))
    ep = args.ep
    p = palette_of(ep)
    ep_name = "ep%02d" % ep
    title, badge = doc["title"], doc["badge"]
    shots = doc["shots"]

    audio_entries = aj["entries"]
    if len(entries) != len(shots) + 2 or len(audio_entries) != len(entries):
        print("[compose] ERROR: 画面(%d)/audio(%d) 与 shots(%d)+2 不一致"
              % (len(entries), len(audio_entries), len(shots)), flush=True)
        sys.exit(1)
    for index, (panel, audio, expected) in enumerate(zip(entries, audio_entries, shot_plan_meta(shots, ep))):
        if (panel.get("role") != expected["role"] or
                panel.get("kind") != expected["kind"] or
                audio.get("kind") != expected["kind"] or
                type(audio.get("frames")) is not int or audio["frames"] <= 0):
            raise ValueError("分镜、画面与音轨顺序或帧数不一致，清单条目 %d" % index)

    ovls = build_overlays(doc, ep, p)
    plan = []
    for i, (e, ae) in enumerate(zip(entries, audio_entries)):
        visual_path = manifest_asset(visual_dir, e["file"])
        wav_path = manifest_asset(args.audio, ae["file"])
        data, sr = sf.read(wav_path, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1).astype(np.float32)
        if sr != SR:
            n = int(round(len(data) * SR / sr))
            idx = np.linspace(0, len(data) - 1, n, dtype=np.float32)
            data = np.interp(idx, np.arange(len(data), dtype=np.float32), data).astype(np.float32)
        n = ae["frames"]
        if video_mode:
            # A clip stays in motion for 3–4s. Hold its last frame if speech
            # needs longer, preserving the last syllable and the subtitle.
            n = max(n, clip_frames, math.ceil(len(data) * FPS / SR))
            plan.append(dict(clip=visual_path, ovl=ovls[i], n=n, audio=data))
        else:
            bgr = cv2.imdecode(np.fromfile(visual_path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                raise ValueError("缺少面板 %s" % e["file"])
            height, width = bgr.shape[:2]
            if (width, height) != (BW, BH):
                crop_w = min(width, max(1, round(height * BW / BH)))
                crop_h = min(height, max(1, round(width * BH / BW)))
                left, top = (width - crop_w) // 2, (height - crop_h) // 2
                bgr = cv2.resize(bgr[top:top + crop_h, left:left + crop_w], (BW, BH))
            panel = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            plan.append(dict(panel=panel, ovl=ovls[i], n=n, audio=data,
                             z0=e["z0"], z1=e["z1"], cx=e["cx"], cy=e["cy"],
                             px=e.get("px", 0.0), py=e.get("py", 0.0), ph=e.get("ph", 0.0)))

    total_frames = sum(s["n"] for s in plan)
    dur = total_frames / float(FPS)

    # 语音轨：每句按该镜时长补零，逐句拼接 → 音画严格对齐（manju_av）
    chunks = []
    for s in plan:
        need = int(round(s["n"] / float(FPS) * SR))
        chunks.append(pad_to(s["audio"], need))
    voice = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)

    # 逐帧：推拉 + 平移 + 正弦手持抖动（manju_anim）+ 雨丝 + 快速颗粒 + 淡入合成
    def frames(pending_poster):
        fi = 0
        for index, s in enumerate(plan):
            n = s["n"]
            if video_mode:
                for k, fr in enumerate(decode_clip(s["clip"], clip_frames)):
                    if index == 0 and k == 0 and pending_poster:
                        Image.fromarray(fr).save(pending_poster)
                    with_subtitle = fast_composite(fr.copy(), s["ovl"], min(1.0, (k + 1) / 4.0))
                    yield with_subtitle
                for _ in range(clip_frames, n):
                    yield with_subtitle
                continue
            for k in range(n):
                t = k / float(max(1, n - 1))
                z = s["z0"] + (s["z1"] - s["z0"]) * t
                wob_x = math.sin(fi * 0.09 + s["ph"]) * 0.008 + math.sin(fi * 0.031) * 0.005
                wob_y = math.sin(fi * 0.13 + s["ph"] * 1.9) * 0.006
                cx = min(0.88, max(0.12, s["cx"] + s["px"] * t + wob_x))
                cy = min(0.88, max(0.12, s["cy"] + s["py"] * t + wob_y))
                fr = crop_zoom(s["panel"], z, cx, cy)
                fr = fast_grain(fr, fi, 2)
                fr = fast_composite(fr, s["ovl"], min(1.0, (k + 1) / 4.0))
                yield fr
                fi += 1

    out_path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    generate_poster = video_mode and "poster" not in pj
    poster_path = (os.path.splitext(out_path)[0] + "-poster.png" if generate_poster else
                   manifest_asset(visual_dir, pj.get("poster", "poster.png")))
    if not generate_poster and not os.path.isfile(poster_path):
        raise ValueError("缺少封面图片")
    manifest = os.path.abspath(args.manifest or os.path.join(os.path.dirname(out_path), "manifest.json"))
    os.makedirs(os.path.dirname(manifest), exist_ok=True)

    # Publish only a verified render. Keep the previous output when encoding
    # fails, and never put an unverified episode in the manifest.
    with tempfile.TemporaryDirectory(prefix=".compose-", dir=os.path.dirname(out_path)) as stage:
        tmp_video = os.path.join(stage, "episode.mp4")
        pending_poster = os.path.join(stage, "poster.png") if generate_poster else None
        voice_wav = os.path.join(stage, "voice.wav")
        sf.write(voice_wav, voice, SR, subtype="PCM_16")
        encode(frames(pending_poster), tmp_video, dur, voice_wav, 92 + ep * 3)
        rec = verify_episode(tmp_video, ep_name, title, len(shots), total_frames, poster_path)
        if video_mode:
            rec.update(visual_mode="video", clip_seconds=clip_seconds, video_model=pj.get("video_model"))
        publish_episode(tmp_video, out_path, manifest, rec, pending_poster)

    print("[ep] %s | %s | shots=%d | %.2fs | %d frames | %.2f MB | audio=%s | open=%s/%s"
          % (ep_name, title, len(shots), rec["seconds"], rec["frames"], rec["bytes"] / 1048576.0,
             rec["has_audio"], rec["first_frame_ok"], rec["last_frame_ok"]), flush=True)
    print("[compose] verified -> %s (manifest: %s)" % (out_path, manifest), flush=True)


def verify_episode(path, ep_name, title, shot_count, expected_frames, poster_path):
    # 校验（manju_av.render_chapter）：cv2 帧数/fps/首尾帧解码 + ffmpeg probe
    cap = cv2.VideoCapture(path)
    nframes = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    ok_first = bool(cap.read()[0])
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, nframes - 1))
    ok_last = bool(cap.read()[0])
    cap.release()
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    probe = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, timeout=30)
    txt = probe.stderr.decode("utf-8", "ignore")
    has_audio = "Audio: aac" in txt
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", txt)
    vdur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0
    size = os.path.getsize(path)

    rec = dict(ep=ep_name, title=title, shots=shot_count, seconds=round(vdur, 2),
               frames=nframes, fps=round(float(fps), 2), bytes=size, poster=poster_path,
               has_audio=has_audio, first_frame_ok=ok_first, last_frame_ok=ok_last)
    if (not (ok_first and ok_last and has_audio) or nframes != expected_frames or
            abs(fps - FPS) > 0.01 or abs(vdur - expected_frames / FPS) > 0.15):
        raise ValueError("成片校验失败，预期 %d 帧：%s" % (expected_frames, json.dumps(rec, ensure_ascii=False)))
    return rec


def publish_episode(tmp_video, out_path, manifest, rec, pending_poster=None):
    # manifest 合并（按 ep 键去重，排序写回）
    recs = load_json(manifest) if os.path.exists(manifest) else []
    if not isinstance(recs, list) or any(not isinstance(r, dict) or not isinstance(r.get("ep"), str)
                                        for r in recs):
        raise ValueError("成片清单格式无效")
    done = {r["ep"]: r for r in recs}
    done[rec["ep"]] = rec
    with tempfile.TemporaryDirectory(prefix=".manifest-", dir=os.path.dirname(manifest)) as stage:
        tmp_manifest = os.path.join(stage, "manifest.json")
        save_json(tmp_manifest, [done[k] for k in sorted(done)])
        os.replace(tmp_video, out_path)
        if pending_poster:
            os.replace(pending_poster, rec["poster"])
        os.replace(tmp_manifest, manifest)


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="通用漫剧生产管线（shots/panels/tts/compose）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("shots", help="分镜：章节 md -> shots.json")
    s.add_argument("--chapter", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_shots)

    s = sub.add_parser("panels", help="画师：渲染面板 PNG + poster + panels.json")
    s.add_argument("--shots", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--ep", type=int, required=True)
    s.set_defaults(fn=cmd_panels)

    s = sub.add_parser("tts", help="配音：edge_tts 逐句合成 + audio.json")
    s.add_argument("--shots", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--ep", type=int, default=1,
                   help="话数（用于标题/片尾念白，默认 1）")
    s.set_defaults(fn=cmd_tts)

    s = sub.add_parser("compose", help="合成：面板或视频片段 + 配音 -> MP4（含校验和清单）")
    s.add_argument("--shots", required=True)
    visuals = s.add_mutually_exclusive_group(required=True)
    visuals.add_argument("--panels")
    visuals.add_argument("--clips")
    s.add_argument("--audio", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--ep", type=int, required=True)
    s.add_argument("--manifest", default=None)
    s.set_defaults(fn=cmd_compose)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
