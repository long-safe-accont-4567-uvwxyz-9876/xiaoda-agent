#!/usr/bin/env python3
"""AI 生成图 → NSIS 安装器素材（164×314 / 150×57, 24位BMP）。

流程：按目标宽高比裁剪 → LANCZOS 缩小 → 修补掉 AI 绘制的文字（逐列垂直插值，
保留背景横向纹理与纵向渐变）→ 用 DejaVu 重绘清晰文字 → 存 BMP（覆盖 assets/）
→ 输出 3x 放大预览 PNG 供目检。
"""
from PIL import Image, ImageDraw, ImageFont

SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SANS_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def inpaint_v(im, bbox, pad=4):
    """bbox 区域用上下两侧像素逐列插值填充（纵向渐变背景上抹掉文字）。"""
    x0, y0, x1, y1 = bbox
    px = im.load()
    h = im.height
    ty, by = max(0, y0 - pad), min(h - 1, y1 + pad)
    for x in range(max(0, x0), min(im.width, x1)):
        c0, c1 = px[x, ty], px[x, by]
        for y in range(max(0, y0), min(h, y1)):
            t = (y - ty) / (by - ty)
            px[x, y] = tuple(round(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))


def draw_spaced(draw, xy, text, font, fill, spacing=0):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + spacing


def sample_dark(im, bbox, n=12):
    """bbox 内最暗的 n 个像素取平均——即原文字颜色。"""
    px = sorted(im.getpixel((x, y)) for x in range(bbox[0], bbox[2])
                for y in range(bbox[1], bbox[3]))
    darkest = px[:n]
    return tuple(round(sum(c[i] for c in darkest) / n * 0.92) for i in range(3))


# ── 侧栏：164×314 ──────────────────────────────────────────────
src = Image.open("/tmp/gen-sidebar.png").convert("RGB")
CW = round(src.height * 164 / 314)              # 802
cx0 = 163                                       # 右移窗口保住右上四叶草簇
im = src.crop((cx0, 0, cx0 + CW, src.height)).resize((164, 314), Image.LANCZOS)
cap = (36, 292, 112, 304)                       # 原文字缩放后所在区域
color = sample_dark(im, cap)
inpaint_v(im, cap)
d = ImageDraw.Draw(im)
f = ImageFont.truetype(SANS, 8)
tw = d.textlength("XIAODA AGENT", font=f) + 3 * 2
draw_spaced(d, ((164 - tw) / 2, 293), "XIAODA AGENT", f, color, spacing=2)
im.save("/home/orangepi/ai-agent/assets/installer-sidebar.bmp")
im.resize((492, 942), Image.NEAREST).save("/tmp/preview-sidebar.png")
print("sidebar caption color:", color)

# ── 头部：150×57 ───────────────────────────────────────────────
# 先在原图分辨率上抹掉 AI 画的 wordmark（上下带均值逐列插值），再裁剪缩小
src = Image.open("/tmp/gen-header.png").convert("RGB")
bx0, by0, bx1, by1 = 380, 440, 1060, 605        # wordmark 区域（含反锯齿边）
px = src.load()
strip = 30
tops, bots = [], []
for x in range(bx0, bx1):
    tops.append([sum(px[x, y][i] for y in range(by0 - strip, by0)) / strip for i in range(3)])
    bots.append([sum(px[x, y][i] for y in range(by1, by1 + strip)) / strip for i in range(3)])
# 横向平滑列颜色：风絮点迹会污染个别列，平滑后填充带与柔彩背景一致
K = 41
for arr in (tops, bots):
    sm = []
    for i in range(len(arr)):
        lo, hi = max(0, i - K // 2), min(len(arr), i + K // 2 + 1)
        sm.append([sum(arr[j][c] for j in range(lo, hi)) / (hi - lo) for c in range(3)])
    arr[:] = sm
for x in range(bx0, bx1):
    c0, c1 = tops[x - bx0], bots[x - bx0]
    for y in range(by0, by1):
        t = (y - by0) / (by1 - by0)
        px[x, y] = tuple(round(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))

CW = 1160                                       # 左对齐构图，右侧留呼吸位
CH = round(CW * 57 / 150)                       # 441
im = src.crop((0, 300, CW, 300 + CH)).resize((150, 57), Image.LANCZOS)
d = ImageDraw.Draw(im)
color = sample_dark(im, (10, 20, 40, 38))       # 从四叶草取文字色
size = 13
while size > 8:
    f = ImageFont.truetype(SANS_B, size)
    if d.textlength("Xiaoda Agent", font=f) <= 88:
        break
    size -= 1
d.text((48, 28), "Xiaoda Agent", font=f, fill=color, anchor="lm")
im.save("/home/orangepi/ai-agent/assets/installer-header.bmp")
im.resize((600, 228), Image.NEAREST).save("/tmp/preview-header.png")
print("header font size:", size, "text color:", color)
