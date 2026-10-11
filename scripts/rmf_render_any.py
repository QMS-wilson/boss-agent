# -*- coding: utf-8 -*-
"""通用 rmf 模板渲染器（盲测第二格式用）。

用法：python scripts/rmf_render_any.py <模板json> <数据json> <输出png> [--pdf 输出pdf]
数据 json：{"corp":..,"values":{绑定名:文本}, "align":{元素序号:l/r/c},
            "size":{元素序号:pt}, "pushdown":px, "strip_pitch":px}
主格选择：cells 中 x1>=30 且面积最大者；竖排（cap 含'|'）逐字流排。
"""
import json
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from v3agent import printing as P

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def resolve_text(cap, data):
    """把 [SqlMaster."x"] / [SqlDetail."x"] / 函数 替换为数据值。"""
    v = data["values"]
    t = cap
    if "[公司名称]" in t:
        t = t.replace("[公司名称]", v.get("corp", ""))
    if "[HxCorpName]" in t:
        t = t.replace("[HxCorpName]", v.get("corp", ""))
    for k in ("CurrToBigNum", "Sum", "sum"):
        pass
    import re
    def sub(m):
        return str(v.get(m.group(1), v.get(m.group(1).lower(), "")))
    t = re.sub(r'\[Sql(?:Master|Detail)\."(\w+)"\]', sub, t)
    t = re.sub(r'\[CurrToBigNum\(\[Sum\([^)]*\)[^]]*\]\)\]',
               lambda mm: v.get("big", ""), t)
    t = re.sub(r'\[(?:Sum|sum)\([^"]*"(\w+)"[^]]*\)\]', sub, t)
    t = re.sub(r'\[(?:Sum|sum)\(([^(),]+)[^]]*\)\]',
               lambda mm: mm.group(1), t)
    t = t.replace("[_RM_DateTime]", v.get("printtime", ""))
    t = t.replace("[页码]", v.get("page", ""))
    return t


def main_cell(e):
    cs = [c for c in e.get("cells_px96", [e["cell_px96"]])
          if c[0] >= 30 and c[2] - c[0] >= 12]
    if not cs:
        return None
    rows = [c for c in cs if 12 <= c[3] - c[1] <= 40] or cs
    return max(rows, key=lambda c: (c[2] - c[0]) * (c[3] - c[1]))


_WDC = {"h": None}


def text_w_px(s, size_pt):
    """GDI 实测字宽（px@96）——右对齐/居中精度的来源（V3 同源度量）。"""
    import ctypes
    from ctypes import wintypes
    api = P._gdi()
    gdi32 = api["gdi32"]
    if _WDC["h"] is None:
        _WDC["h"] = gdi32.CreateDCW("DISPLAY", None, None, None)
        gdi32.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR,
                                                ctypes.c_int,
                                                ctypes.POINTER(wintypes.SIZE)]
        gdi32.GetTextExtentPoint32W.restype = wintypes.BOOL
    hdc = _WDC["h"]
    f = P._gdi_font(hdc, size_pt)
    old = gdi32.SelectObject(hdc, f)
    sz = wintypes.SIZE()
    gdi32.GetTextExtentPoint32W(hdc, str(s), len(str(s)), ctypes.byref(sz))
    gdi32.SelectObject(hdc, old)
    return sz.cx


def build(tpl, data):
    els = []
    rpp = data.get("rows_per_page")
    if rpp:                       # V3 规则：明细带固定 rpp 行，不足补空行；
        push = (rpp - 1) * 24     # 表尾下推 = (rpp-设计行数1)×24
        data = dict(data); data["pushdown"] = push
    else:
        push = data.get("pushdown", 0)
    pitch = data.get("strip_pitch", 14.05)
    dsize = data.get("size", {})
    dalign = data.get("align", {})
    for k, e in enumerate(tpl["elements"]):
        cap = e["text"]
        if cap.startswith("[_RM"):
            continue
        cell = main_cell(e)
        ov = data.get("cell", {}).get(str(k))
        if ov:
            cell = ov
        if not cell:
            continue
        xa = data.get("xadd", 0)
        cell = [cell[0] + xa, cell[1], cell[2] + xa, cell[3]]
        size = dsize.get(str(k), dsize.get("def", 9.96))
        fpx = size / 72.0 * 96.0          # 字高 px@96
        if "|" in cap:                    # 竖排条：逐字流排（V3 首字基线=格顶+12.1）
            y = cell[1] + 12.1 * size / 9.96
            for ch in cap.split("|"):
                if ch:
                    cx = (cell[0] + cell[2]) / 2 - text_w_px(ch, size) / 2
                    els.append(("v", ch, round(cx, 2), round(y, 2), size))
                y += pitch
            continue
        text = resolve_text(cap, data)
        if not text.strip():
            continue
        al = dalign.get(str(k))
        if al is None:
            core = text.rstrip()
            al = "r" if (cap.endswith(":") or any(op in cap for op in
                 ("s_sl", "D_DJ", "J_JE", "Sum", "sum"))) else "l"
        w = text_w_px(text, size)
        if al == "r":
            x = cell[2] - 2.6 - w
        elif al == "c":
            x = (cell[0] + cell[2]) / 2 - w / 2
        else:
            x = cell[0] + 2.4
        pf = data.get("push_from", 140)
        y1p = cell[1] + (push if cell[1] >= pf else 0)
        y2p = cell[3] + (push if cell[3] >= pf else 0)
        cy = (y1p + y2p) / 2
        if size > 15:                     # 大字号（标题）：V3 底对齐
            y = y2p - 3.8
        else:
            y = cy + 0.35 * fpx
        els.append(("e%d" % k, text, round(x, 2), round(y, 2), size))
    sq = data.get("seq")
    if sq:
        xa = data.get("xadd", 0)
        c = [sq["cell"][0] + xa, sq["cell"][1], sq["cell"][2] + xa, sq["cell"][3]]
        for i in range(sq["count"]):
            s = str(i + 1)
            cx = (c[0] + c[2]) / 2 - text_w_px(s, sq.get("size", 9.96)) / 2
            els.append(("seq%d" % i, s, round(cx, 2),
                        round(c[1] + 24 * i + (c[3] - c[1]) / 2
                              + 0.35 * sq.get("size", 9.96) / 72 * 96, 2),
                        sq.get("size", 9.96)))
    return els


def _norm_cell(c, xa, push, pf):
    x1, y1, x2, y2 = c
    x1 += xa
    x2 += xa
    if x2 > 755:                      # 右边缘让位给竖排联次条（V3 表格线止于 751）
        x2 = 751
    if x1 < 50 or x2 - x1 < 14 or y2 - y1 < 12 or y2 - y1 > 44:
        return None
    y1 += push if y1 >= pf else 0
    y2 += push if y2 >= pf else 0
    return (x1, y1, x2, y2)


def grid_segments(tpl, data):
    """格线 = 单元格矩形的边。明细带按 rows_per_page 复制补空行；
    页脚合并格（全宽单格）天然无内部竖线 —— 与 V3 一致。"""
    xa = data.get("xadd", 0)
    rpp = data.get("rows_per_page") or 1
    push = (rpp - 1) * 24
    pf = data.get("push_from", 140)
    cells = []
    detail_row = None
    gt = data.get("grid_top", 94)       # V3 只有表格区画框：表头行顶以上不画
    for e in tpl["elements"]:
        if e["text"].startswith("[_RM") or "|" in e["text"]:
            continue
        for c in e.get("cells_px96", []):
            nc = _norm_cell(c, xa, push, pf)
            if nc and nc[1] >= gt:
                cells.append(nc)
                if abs(nc[1] - (118 if 118 >= pf else 118 + push)) < 1 and 24 <= nc[3] - nc[1] <= 26:
                    detail_row = detail_row or []
                    detail_row.append(nc)
    # 补空行：把明细首行格复制 rpp-1 次
    if detail_row:
        base_y = detail_row[0][1]
        for i in range(1, rpp):
            for x1, y1, x2, y2 in detail_row:
                cells.append((x1, base_y + 24 * i, x2, base_y + 24 * i + (y2 - y1)))
    hs, vs = set(), set()
    for x1, y1, x2, y2 in cells:
        hs.add((round(y1, 1), round(x1, 1), round(x2, 1)))
        hs.add((round(y2, 1), round(x1, 1), round(x2, 1)))
        vs.add((round(x1, 1), round(y1, 1), round(y2, 1)))
        vs.add((round(x2, 1), round(y1, 1), round(y2, 1)))
    return sorted(hs), sorted(vs)


def draw_page(hdc, api, hs, vs, els):
    gdi32 = api["gdi32"]
    pen = gdi32.CreatePen(api["PS_SOLID"], 0, 0)
    old = gdi32.SelectObject(hdc, pen)
    for y, x0, x1 in hs:
        gdi32.MoveToEx(hdc, int(round(x0)), int(round(y)), None)
        gdi32.LineTo(hdc, int(round(x1)), int(round(y)))
    for x, y0, y1 in vs:
        gdi32.MoveToEx(hdc, int(round(x)), int(round(y0)), None)
        gdi32.LineTo(hdc, int(round(x)), int(round(y1)))
    gdi32.SelectObject(hdc, old)
    gdi32.DeleteObject(pen)
    gdi32.SetBkMode(hdc, api["TRANSPARENT"])
    gdi32.SetTextColor(hdc, 0)
    gdi32.SetTextAlign(hdc, api["TA_BASELINE"])
    cur = None
    old_f = None
    for role, text, x, y, size in els:
        s = str(text)
        if not s.strip():
            continue
        lh = -int(round(float(size) / 72.0 * 96.0))
        if lh != cur:
            f = P._gdi_font(hdc, size)
            old_f = gdi32.SelectObject(hdc, f) if old_f is None else gdi32.SelectObject(hdc, f)
            cur = lh
        gdi32.TextOutW(hdc, int(round(x)), int(round(y)), s, len(s))


def write_pdf_from_rgb(rgb, w, h, mm_w, mm_h, path):
    """把 RGB 位图封装成 PDF：MediaBox 严格等于格式定义的纸张（mm→pt）。
    单页、零依赖（zlib+struct）；位图 300dpi，打印质量足够预览/复刻比对。"""
    import struct
    pt_w = round(mm_w / 25.4 * 72, 2)
    pt_h = round(mm_h / 25.4 * 72, 2)
    img = zlib.compress(rgb, 6)
    content = ("q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (pt_w, pt_h)).encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
         "/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>" % (pt_w, pt_h)).encode(),
        (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
         b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n" % (w, h, len(img))
         + img + b"\nendstream"),
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    open(path, "wb").write(bytes(out))


def render(tpl, data, out_png, out_pdf=None):
    els = build(tpl, data)
    mm = tpl.get("pageMM") or [210, 297]
    page = data.get("page_px96", [round(mm[0] / 25.4 * 96), round(mm[1] / 25.4 * 96)])
    hs, vs = grid_segments(tpl, data)
    import ctypes
    api = P._gdi()
    gdi32 = api["gdi32"]

    def dib(dpi):
        w = int(round(page[0] / 96.0 * dpi))
        h = int(round(page[1] / 96.0 * dpi))
        hdc_s = gdi32.CreateDCW("DISPLAY", None, None, None)
        hdc_m = gdi32.CreateCompatibleDC(hdc_s)
        bmi = api["BITMAPINFO"]()
        bmi.bmiHeader.biSize = ctypes.sizeof(api["BITMAPINFOHEADER"])
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = api["BI_RGB"]
        bits = ctypes.c_void_p()
        hb = gdi32.CreateDIBSection(hdc_m, ctypes.byref(bmi), api["DIB_RGB_COLORS"],
                                    ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(hdc_m, hb)
        P._gdi_prepare(hdc_m, dpi, dpi)
        gdi32.PatBlt(hdc_m, 0, 0, w, h, 0x00FF0062)
        draw_page(hdc_m, api, hs, vs, els)
        buf = ctypes.string_at(bits, w * h * 4)
        mv = memoryview(buf)
        flat = b"".join(bytes(mv[(h - 1 - yy) * w * 4:(h - yy) * w * 4]) for yy in range(h))
        rgb = bytearray(w * h * 3)
        rgb[0::3] = flat[2::4]
        rgb[1::3] = flat[1::4]
        rgb[2::3] = flat[0::4]
        gdi32.SelectObject(hdc_m, old)
        gdi32.DeleteObject(hb)
        gdi32.DeleteDC(hdc_m)
        gdi32.DeleteDC(hdc_s)
        return bytes(rgb), w, h

    rgb, w, h = dib(150)
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    open(out_png, "wb").write(P._png_encode(w, h, rgb))
    print("PNG →", out_png, "元素", len(els))
    if out_pdf:
        rgb3, w3, h3 = dib(300)
        write_pdf_from_rgb(rgb3, w3, h3, mm[0], mm[1], out_pdf)
        print("PDF →", out_pdf, "页 %.1fx%.1f pt（= %.0fx%.0fmm，与格式定义一致）"
              % (mm[0] / 25.4 * 72, mm[1] / 25.4 * 72, mm[0], mm[1]))


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    tpl = json.load(open(a[0], encoding="utf-8"))
    data = json.load(open(a[1], encoding="utf-8"))
    pdf = a[3] if len(a) > 3 else None
    render(tpl, data, a[2], pdf)
