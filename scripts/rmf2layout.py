# -*- coding: utf-8 -*-
"""rmf → 版式模板 解析器（试跑：与已验证版式真值比对，不依赖渲染）。

用法：python scripts/rmf2layout.py <rmf路径> [--truth]
输出：控件清单（角色/绑定/矩形/字体）+ 与 _V3 真值的逐项比对。
"""
import glob
import os
import re
import struct
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PX_W = int(round(210 / 25.4 * 96))   # 页宽 px@96（A4），实际从页头取
PX_H = int(round(297 / 25.4 * 96))


def load(p):
    raw = open(p, "rb").read()
    w, h = struct.unpack_from("<II", raw, 510)
    return raw, w / 10.0, h / 10.0        # mm


def records(raw):
    heads = [m.start() for m in re.finditer(rb"\x0f\x00.\x00\xff\xff", raw, re.S)] + [len(raw)]
    out = []
    for k in range(len(heads) - 1):
        s, e = heads[k], heads[k + 1]
        seg = raw[s:e]
        m = re.match(rb"\x0f\x00(.)\x00\xff\xff(.+?)\x00\x00", seg, re.S)
        name = m.group(2).decode("utf-16-le", "replace") if m else "?"
        out.append((name, seg))
    return out


def cap_of(seg):
    """内容模板：'[' 或中文起始的 ASCII+UTF-8 混合串。"""
    for m in re.finditer(rb"[\x20-\x7e\xe4-\xe9]", seg[100:200], re.S):
        i = 100 + m.start()
        buf = bytearray()
        j = i
        while j < len(seg) and (0x20 <= seg[j] < 0x7f or seg[j] >= 0x80 or seg[j] in (13, 10)):
            buf.append(seg[j])
            j += 1
        try:
            t = bytes(buf).decode("utf-8")
        except Exception:
            continue
        t = t.strip()
        if t and (t.startswith("[") or re.search(r"[\u4e00-\u9fff]", t) or "[" in t) and len(t) > 3:
            return t
    return ""


def fonts_of(seg):
    """子记录 05 00 len 00 ff ff + UTF-16 串：取字形/字体名。"""
    out = []
    for m in re.finditer(rb"\x05\x00(.\x00)\xff\xff", seg, re.S):
        n = m.group(1)[0]
        i = m.end()
        try:
            t = seg[i:i + n * 2].decode("utf-16-le")
        except Exception:
            continue
        if t.isprintable() and len(t) >= 3:
            out.append(t)
    # 字体的 lfHeight：紧跟字体名字节后的 i32（-px）
    heights = []
    for m in re.finditer(rb"Serif\x00{1,2}|GB2312|Arial", seg):
        j = m.end()
        while j < len(seg) - 3 and seg[j] == 0:
            j += 1
        if j + 4 <= len(seg):
            v = struct.unpack_from("<i", seg, j)[0]
            if -60 <= v <= -6:
                heights.append(v)
    return out, heights


def i32s(seg, lo=140):
    v = []
    for o in range(lo, len(seg) - 3):
        x = struct.unpack_from("<i", seg, o)[0]
        if 0 <= x <= 3000:
            v.append((o, x))
    return v


def rect_chains(seg):
    """找 (x1,y1,x2,y2) 步长4、同对齐、递增合法 的四元组链。"""
    found = []
    for o in range(140, len(seg) - 15):
        x1, y1, x2, y2 = struct.unpack_from("<iiii", seg, o)
        if 0 <= x1 < x2 <= PX_W + 40 and 0 <= y1 < y2 <= PX_H + 40 and (y2 - y1) in (24, 28, 30, 48, 56):
            found.append((o, o % 4, (x1, y1, x2, y2)))
    # 同对齐链去重：保留每条对齐链的起点序列
    return found


ROLE = [
    (r"^\[HxCorpName\]销售出货单", "标题"),
    (r"^\[SqlMaster\.\"ndate\"", "开单日期"),
    (r"单号:.*BillNo", "单号行"),
    (r"^\u5ba2\u6237:$", "标签-客户"),
    (r"itemmc", "值-客户名"),
    (r"^\u7535\u8bdd:$", "标签-电话"), (r"\"tel\"", "值-电话"),
    (r"^\u5730\u5740:$", "标签-地址"), (r"\"address\"", "值-地址"),
    (r"^\u4f20\u771f:$", "标签-传真"), (r"\"fax\"", "值-传真"),
    (r"hpbh", "列-编号"), (r"hpmc", "列-名称"), (r"hpunit", "列-单位"),
    (r"s_slUnit", "列-数量"), (r"IIF.*d_dj", "列-单价"), (r"j_jehs\"\]", "列-金额"),
    (r"bz_XSMX", "列-备注"), (r"_RM_Line", "线条"),
    (r"^\u9875\u8ba1:", "标签-页计"), (r"CurrToBigNum\(\[Sum", "值-页计大写"),
    (r"^sum\(.*s_slUnit", "值-页计数量"), (r"^\[Sum\(.*j_jehs", "值-页计金额"),
    (r"CurrToBigNum\(\[SqlMaster", "值-总大写"), (r"s_slSUM", "值-总数量"),
    (r"j_jehsSUM", "值-总金额"), (r"^\u5907\u6ce8:", "标签-备注"), (r"\"bz_XS\"", "值-备注"),
    (r"ZDRMC", "值-制单人"), (r"YWMC", "值-业务员"),
]


def role_of(cap):
    if not cap:
        return ""
    c = cap.replace("\r", "").replace("\n", "").strip()
    for pat, r in ROLE:
        if re.search(pat, c):
            return r
    return "?" + c[:14]


def main():
    p = sys.argv[1] if len(sys.argv) > 1 else None
    if not p:
        d0 = glob.glob(r"D:\重构\进销存-本地Agent源码\print_formats\_pulled\*")[0]
        p = [f for f in glob.glob(os.path.join(d0, "BillXS__*")) if "折后价" in f][0]
    raw, wm, hm = load(p)
    print("rmf:", os.path.basename(p), "| 页面 %.1f x %.1f mm" % (wm, hm))
    PW = int(round(wm / 25.4 * 96))
    rows = []
    for name, seg in records(raw):
        cap = cap_of(seg)
        r = role_of(cap)
        if not r:
            continue
        fonts, heights = fonts_of(seg)
        rects = [(o, t) for o, a, t in rect_chains(seg)]
        rows.append((name, r, cap, rects, heights))
    print("%-9s %-12s %-34s %s" % ("记录", "角色", "模板", "矩形(x1,y1,x2,y2)"))
    for name, r, cap, rects, hs in rows:
        rs = " ".join(str(t) for _, t in rects[:4])
        print("%-9s %-12s %-34s %s  h=%s" % (name[:9], r[:12], cap.replace("\r", "").replace("\n", "")[:32], rs, hs))

    # ---- 与真值比对 ----
    TRUTH = {  # px@96，来自已验证引擎常量
        "行顶:单号": 89, "行顶:客户": 113, "行顶:地址": 137, "行顶:表头": 161,
        "行顶:明细1": 185, "行顶:明细2": 209, "行顶:表尾1": 233, "行顶:表尾2": 257,
        "列界": [37.76, 66.72, 150.72, 400.64, 435.68, 522.56, 601.6, 677.6, 753.6],
        "框左右": [57, 773],
    }
    allx, ally = set(), set()
    for _, _, _, rects, _ in rows:
        for _, (x1, y1, x2, y2) in rects:
            allx.update((x1, x2))
            ally.update((y1, y2))
    def hit(v, pool, tol=1):
        return any(abs(v - u) <= tol for u in pool)
    print()
    print("== 真值覆盖检查（容差±1px） ==")
    for k in ("行顶:单号", "行顶:客户", "行顶:地址", "行顶:表头", "行顶:明细1", "行顶:明细2", "行顶:表尾1", "行顶:表尾2"):
        print("  %-10s %4d  %s" % (k, TRUTH[k], "OK" if hit(TRUTH[k], ally) else "**缺失**"))
    miss = [x for x in TRUTH["列界"] if not hit(x, allx)]
    print("  列界缺失:", miss if miss else "无（9条全命中）")
    print("  框左右:", TRUTH["框左右"], "OK" if all(hit(x, allx) for x in TRUTH["框左右"]) else "缺失")


if __name__ == "__main__":
    main()
