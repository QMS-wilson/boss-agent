# -*- coding: utf-8 -*-
"""生成 V3 本地 Agent 的图标资源（三个方案 × 多尺寸）。

为什么自己画而不用现成素材：
  * 图标要在 16px 下还能认出 —— 必须针对小尺寸简化，AI 生成的位图缩到 16px 会糊成一团。
  * 超采样 8 倍绘制再降采样，边缘才干净。

三个方案（语义都指向"本地 ↔ 云端的中继"）：
  A 双向中继 —— 两个反向粗箭头，最直白
  B 桥接节点 —— 左本地 / 中绿色中继 / 右云端，三点一线
  C 桥     —— 两个桥墩 + 一道拱，隐喻"连接两岸"

输出：
  assets/icons/v3agent_<v>.ico          多尺寸 Windows 图标（16/24/32/48/64/128/256）
  assets/icons/v3agent_<v>_<n>.png      各尺寸 PNG
  assets/icons/preview.png              三方案 × 多尺寸对比预览
"""
import os
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "icons")

BG = (15, 23, 42, 255)        # #0f172a 与界面顶栏同色
WHITE = (255, 255, 255, 255)
GREEN = (34, 197, 94, 255)    # #22c55e
SS = 8                        # 超采样倍数
ICO_SIZES = [256, 128, 64, 48, 32, 24, 16]
PNG_SIZES = [256, 64, 32, 16]


def _bg(d, S):
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=BG)


def _draw_a(d, S, simple):
    """双向中继：上箭头白（本地→云端）、下箭头绿（云端→本地）。"""
    m = S * 0.205
    lw = int(S * (0.100 if simple else 0.072))
    aw = S * (0.135 if simple else 0.108)
    ah = S * (0.125 if simple else 0.100)
    y1, y2 = S * 0.345, S * 0.655
    d.line([(m, y1), (S - m - aw * 0.5, y1)], fill=WHITE, width=lw)
    d.polygon([(S - m, y1), (S - m - aw, y1 - ah), (S - m - aw, y1 + ah)], fill=WHITE)
    d.line([(S - m, y2), (m + aw * 0.5, y2)], fill=GREEN, width=lw)
    d.polygon([(m, y2), (m + aw, y2 - ah), (m + aw, y2 + ah)], fill=GREEN)


def _draw_b(d, S, simple):
    """桥接节点：左本地(白) — 中继(绿大) — 右云端(白)。"""
    cy = S * 0.5
    rs = S * (0.105 if simple else 0.085)
    rm = S * (0.185 if simple else 0.165)
    lw = int(S * (0.062 if simple else 0.05))
    lx, rx = S * 0.245, S * 0.755
    d.line([(lx, cy), (rx, cy)], fill=WHITE, width=lw)
    for x in (lx, rx):
        d.ellipse([x - rs, cy - rs, x + rs, cy + rs], fill=WHITE)
    d.ellipse([S * 0.5 - rm, cy - rm, S * 0.5 + rm, cy + rm], fill=GREEN)


def _draw_c(d, S, simple):
    """桥：两个桥墩 + 一道拱。"""
    pw = S * (0.115 if simple else 0.10)
    top, bot = S * 0.56, S * 0.80
    d.rounded_rectangle([S * 0.175, top, S * 0.175 + pw, bot], radius=pw / 2, fill=WHITE)
    d.rounded_rectangle([S * 0.825 - pw, top, S * 0.825, bot], radius=pw / 2, fill=WHITE)
    lw = int(S * (0.105 if simple else 0.085))
    pad = lw / 2
    d.arc([S * 0.175 - pad + lw / 2, S * 0.28 + pad, S * 0.825 + pad - lw / 2, S * 0.84 - pad],
          start=180, end=360, fill=GREEN, width=lw)


DRAW = {"A": _draw_a, "B": _draw_b, "C": _draw_c}
NAME = {"A": "双向中继", "B": "桥接节点", "C": "桥"}


def mockup(variant):
    """放进真实场景看：任务栏(24px) / 桌面(48px) / 窗口标题栏(16px)。"""
    try:
        f12 = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 12)
        f11 = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 11)
    except Exception:  # noqa: BLE001
        f12 = f11 = ImageFont.load_default()
    W, H = 580, 272
    im = Image.new("RGBA", (W, H), (241, 245, 249, 255))
    d = ImageDraw.Draw(im)

    ty = 18
    d.rounded_rectangle([14, ty, W - 14, ty + 40], radius=7, fill=(30, 41, 59, 255))
    d.text((28, ty + 14), "任务栏（24px）", fill=(148, 163, 184, 255), font=f11)
    x = W - 14 - 38
    for i in range(4):
        if i == 1:
            im.alpha_composite(render(variant, 24), (x, ty + 8))
            d.rectangle([x + 4, ty + 37, x + 20, ty + 40], fill=(34, 197, 94, 255))
        else:
            d.rounded_rectangle([x + 3, ty + 11, x + 21, ty + 29], radius=4,
                                fill=(71, 85, 105, 255))
        x -= 38

    dy = 78
    d.text((28, dy + 8), "桌面（48px）", fill=(100, 116, 139, 255), font=f11)
    im.alpha_composite(render(variant, 48), (100, dy))
    d.text((76, dy + 54), "V3 本地 Agent", fill=(15, 23, 42, 255), font=f12)
    d.text((76, dy + 70), "控制台", fill=(15, 23, 42, 255), font=f12)

    cy = 200
    d.rounded_rectangle([14, cy, W - 14, cy + 36], radius=7, fill=(255, 255, 255, 255),
                        outline=(203, 213, 225, 255), width=1)
    im.alpha_composite(render(variant, 16), (26, cy + 10))
    d.text((50, cy + 10), "V3 本地 Agent 控制台", fill=(15, 23, 42, 255), font=f12)
    d.text((W - 74, cy + 10), "16px", fill=(148, 163, 184, 255), font=f11)
    return im


def render(variant, size):
    """按目标尺寸绘制：<=32px 走简化版，避免细节糊掉。"""
    simple = size <= 32
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    _bg(d, S)
    DRAW[variant](d, S, simple)
    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    made = {}
    for v in ("A", "B", "C"):
        pngs = {}
        for s in ICO_SIZES:
            im = render(v, s)
            if s in PNG_SIZES:
                p = os.path.join(OUT, "v3agent_%s_%d.png" % (v, s))
                im.save(p)
                pngs[s] = p
            made.setdefault(v, []).append(im)
        # 组装多尺寸 ICO（各尺寸都是自己画的，不是从 256 硬缩）
        imgs = sorted(made[v], key=lambda i: -i.width)
        ico = os.path.join(OUT, "v3agent_%s.ico" % v)
        imgs[0].save(ico, format="ICO", sizes=[(i.width, i.height) for i in imgs],
                     append_images=imgs[1:])
        print("%s(%s) -> %s  [%s]" % (v, NAME[v], os.path.basename(ico),
                                      "/".join(str(i.width) for i in imgs)))

    # 对比预览：3 行（方案）× 4 列；16/32/64 按真实像素 1:1 显示，256 缩到 128
    cols = [256, 64, 32, 16]
    show = {256: 128, 64: 64, 32: 32, 16: 16}
    col_w = [152, 84, 50, 34]
    row_h, gap, left, head = 152, 16, 132, 24
    W = left + sum(col_w) + gap
    H = gap + head + 3 * row_h + gap
    pv = Image.new("RGBA", (W, H), (241, 245, 249, 255))
    dd = ImageDraw.Draw(pv)
    try:
        f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 12)
        fb = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", 13)
    except Exception:  # noqa: BLE001
        f = fb = ImageFont.load_default()
    x = left
    for c, s in enumerate(cols):
        dd.text((x + 6, 6), "%dpx" % s, fill=(100, 116, 139, 255), font=f)
        x += col_w[c]
    for r, v in enumerate(("A", "B", "C")):
        y = gap + head + r * row_h
        dd.text((10, y + 56), "%s · %s" % (v, NAME[v]), fill=(15, 23, 42, 255), font=fb)
        x = left
        for c, s in enumerate(cols):
            ic = render(v, s)
            w = show[s]
            if w != s:
                ic = ic.resize((w, w), Image.LANCZOS)
            pv.alpha_composite(ic, (x + (col_w[c] - w) // 2, y + (128 - w) // 2))
            x += col_w[c]
    pv.save(os.path.join(OUT, "preview.png"))
    print("preview -> %s" % os.path.join(OUT, "preview.png"))

    for v in ("A", "B", "C"):
        mockup(v).save(os.path.join(OUT, "mockup_%s.png" % v))
    print("mockups -> mockup_A/B/C.png")


if __name__ == "__main__":
    main()
