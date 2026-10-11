# -*- coding: utf-8 -*-
"""rmf 模板 → 效果图（PNG + PDF）。

数据 = 真实单 XK00000084（即 V3 导出 PDF 那张），逐格绑定 → 由 rmf2tpl 解析出的
单元格坐标驱动绘制。与引擎 render_bill_image 共用 GDI 内核（打印/预览同码）。

用法：python scripts/rmf_render_tpl.py [--pdf]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from v3agent import printing as P

TPL = r"D:\重构\.workbuddy\tmp\tpl-zhehoujia.json"
OUT_PNG = r"D:\重构\打印渲染预览\rmf模板-折后价-XK00000084.png"
OUT_PDF = r"D:\重构\打印渲染预览\rmf模板-折后价-XK00000084.pdf"

DATA = {
    "ndate": "2026-09-22 16:55:42", "billno": "单号:XK00000084 第1/共1页",
    "khmc": "大江塑胶模具", "tel": "", "addr": "", "fax": "",
    "rows": [{"hpbh": "1123454552", "hpmc": "螺丝（杯头）", "unit": "个",
              "qty": "1个", "prc": "1.8", "amt": "1.80", "bz": ""}],
    "yj_big": "壹元捌角整", "yj_qty": "1.00", "yj_amt": "1.80",
    "hj_big": "壹元捌角整", "hj_qty": "1.00", "hj_amt": "1.80",
    "bz_xs": "", "zdr": "系统管理员", "yw": "", "corp": "同达",
}

PY = 17.0          # 正文基线 = 格顶 + 17（11.04pt 标定值）
SZ = 11.04
TITLE_SZ = 15.96


def bind(cap):
    d = DATA
    rows = d["rows"]
    if "[HxCorpName]" in cap:
        return d["corp"] + cap.replace("[HxCorpName]", ""), "title"
    kv = {
        '[SqlMaster."ndate"]': d["ndate"], '[SqlMaster."BillNo"]': None,
        '[SqlMaster."itemmc"]': d["khmc"], '[SqlMaster."tel"]': d["tel"],
        '[SqlMaster."address"]': d["addr"], '[SqlMaster."fax"]': d["fax"],
        '[SqlDetail."hpbh"]': rows[0]["hpbh"], '[SqlDetail."hpmc"]': rows[0]["hpmc"],
        '[SqlDetail."hpunit"]': rows[0]["unit"], '[SqlDetail."s_slUnit"]': rows[0]["qty"],
        '[SqlDetail."j_jehs"]': rows[0]["amt"], '[SqlDetail."bz_XSMX"]': rows[0]["bz"],
        '[SqlMaster."j_jehssum"]': d["hj_big"], '[SqlMaster."s_slSUM"]': d["hj_qty"],
        '[SqlMaster."j_jehsSUM"]': d["hj_amt"], '[SqlMaster."bz_XS"]': d["bz_xs"],
        '[SqlMaster."ZDRMC"]': d["zdr"], '[SqlMaster."YWMC"]': d["yw"],
    }
    if "单号:" in cap:
        return d["billno"], "right48"
    if cap.startswith("[IIF"):
        return rows[0]["prc"], "right26"
    if cap.startswith("[_RM_"):
        return "", None                # 内部控件（线/框），不渲染文本
    if cap.startswith("[CurrToBigNum([Sum"):
        return d["yj_big"], "left24"
    if cap.startswith("[CurrToBigNum([SqlMaster"):
        return d["hj_big"], "left24"
    if cap.startswith("[sum([SqlDetail"):
        return d["yj_qty"], "right26"
    if cap.startswith("[Sum([SqlDetail"):
        return d["yj_amt"], "right26"
    for k, v in kv.items():
        if k in cap:
            rule = "title"
            if v is None:
                return "", None
            return v, _rule_for(cap)
    if cap.endswith(":") or cap.endswith("："):
        return cap, "right48"          # 标签
    return cap, "center"               # 表头文字


def _rule_for(cap):
    if any(k in cap for k in ("tel", "fax", "address", "s_slUnit", "j_jehs", "d_dj",
                              "s_slSUM", "j_jehsSUM")):
        return "right26"
    if any(k in cap for k in ("hpbh", "hpmc", "itemmc", "ndate", "ZDRMC", "YWMC", "bz_XS")):
        return "left24"
    return "left24"


def main():
    t = json.load(open(TPL, encoding="utf-8"))
    els = []
    hl = set()
    cols = set()
    for e in t["elements"]:
        x1, y1, x2, y2 = e["cell_px96"]
        hl.update([y1, y2])
        cols.update([x1, x2])
        text, rule = bind(e["text"])
        if not text or rule is None:
            continue
        # _tw 返回 pt，统一换成 px@96
        w = P._tw(text, TITLE_SZ if rule == "title" else SZ) / P._V3_PX2PT
        if rule == "right48":
            x = x2 - 4.8 - w
        elif rule == "right26":
            x = x2 - 2.6 - w
        elif rule == "center":
            x = (x1 + x2) / 2 - w / 2
        else:
            x = x1 + 2.4
        y = (y1 - 24 + 15.32) if rule == "title" else y1 + PY
        size = TITLE_SZ if rule == "title" else SZ
        els.append(("tpl", text, round(x, 2), round(y, 2), size))
    # 明细序号：ReportMachine 行号自动打（rmf 无绑定），落在首列格中心
    seq_cell = next((e["cell_px96"] for e in t["elements"]
                     if e["cell_px96"][1] == 185), None)
    if seq_cell:
        for i in range(len(DATA["rows"])):
            s = str(i + 1)
            cx = (seq_cell[0] + seq_cell[2]) / 2 - P._tw(s) / P._V3_PX2PT / 2
            els.append(("tpl", s, round(cx, 2), 185 + 24 * i + PY, SZ))
    # 竖线只取表头带（y1==161）的列界——页脚带格子划分不同，不是表格列
    cols = sorted({x for e in t["elements"] if e["cell_px96"][1] == 161
                   for x in (e["cell_px96"][0], e["cell_px96"][2])})
    # 标题几何在 prologue（尚未解析）→ 先用 PDF 标定值占位
    els.insert(0, ("tpl", DATA["corp"] + "销售出货单", 312.8, 80.32, TITLE_SZ))
    # 引擎列线常量按 rmf 覆盖（pt = px*0.75）
    P._V3_COL_X_PT = [c * P._V3_PX2PT for c in cols]
    hl_final = [y for y in (113, 137, 161, 185, 209, 233, 257, 281)]
    pg = {"page": 1, "elements": els, "h_lines_px": hl_final}
    lay = {"pages": [pg]}

    import ctypes
    api = P._gdi()
    gdi32 = api["gdi32"]
    # ---- PNG（150dpi，内存 DIB） ----
    dpi = 150
    w = int(round(210 / 25.4 * dpi))
    h = int(round(297 / 25.4 * dpi))
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
    P._gdi_draw_page(hdc_m, pg)
    buf = ctypes.string_at(bits, w * h * 4)
    mv = memoryview(buf)
    rowsb = [bytes(mv[(h - 1 - yy) * w * 4:(h - yy) * w * 4]) for yy in range(h)]
    flat = b"".join(rowsb)
    rgb = bytearray(w * h * 3)
    rgb[0::3] = flat[2::4]
    rgb[1::3] = flat[1::4]
    rgb[2::3] = flat[0::4]
    os.makedirs(os.path.dirname(OUT_PNG), exist_ok=True)
    open(OUT_PNG, "wb").write(P._png_encode(w, h, bytes(rgb)))
    print("PNG →", OUT_PNG)
    gdi32.SelectObject(hdc_m, old)
    gdi32.DeleteObject(hb)
    gdi32.DeleteDC(hdc_m)
    gdi32.DeleteDC(hdc_s)

    if "--pdf" in sys.argv:
        hdc = gdi32.CreateDCW("WINSPOOL", "Microsoft Print to PDF", None, None)
        if not hdc:
            print("无 Microsoft Print to PDF，跳过")
            return
        P._gdi_prepare(hdc, gdi32.GetDeviceCaps(hdc, api["LOGPIXELSX"]),
                       gdi32.GetDeviceCaps(hdc, api["LOGPIXELSY"]))
        di = api["DOCINFOW"]()
        di.cbSize = ctypes.sizeof(api["DOCINFOW"])
        di.lpszDocName = "rmf模板-折后价-XK00000084"
        di.lpszOutput = OUT_PDF
        if gdi32.StartDocW(hdc, ctypes.byref(di)) > 0:
            gdi32.StartPage(hdc)
            P._gdi_draw_page(hdc, pg)
            gdi32.EndPage(hdc)
            gdi32.EndDoc(hdc)
            print("PDF →", OUT_PDF)
        else:
            print("StartDoc 失败")
        gdi32.DeleteDC(hdc)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
