# -*- coding: utf-8 -*-
"""rmf → 版式模板 转换器（模型 N：段尾几何 ↔ 下一段 cap；x 偏移 -19.24px）。

用法：python scripts/rmf2tpl.py [rmf路径] [-o 模板.json]
无参 = 已拉取的 BillXS 折后价。
"""
import glob
import json
import os
import re
import struct
import sys

X_OFF = 19.24          # rmf px@96 → PDF px@96（页原点差）
ROW = 24               # 行距 px@96


def load(p):
    raw = open(p, "rb").read()
    # 页尺寸：锚定头部 [1000,1000] u32 对，其后 8..120 字节内首个
    # (W,H)（0.1mm，W 为 10 的倍数）且后随千级边距值
    wm = hm = 0.0
    for m in range(200, len(raw) - 8):
        if struct.unpack_from("<II", raw, m) == (1000, 1000):
            for o in range(m + 8, min(m + 120, len(raw) - 20)):
                w, h = struct.unpack_from("<II", raw, o)
                nxt = struct.unpack_from("<I", raw, o + 8)[0]
                if w % 10 == 0 and 200 <= w <= 2500 and 150 <= h <= 30000 \
                   and 1000 <= nxt <= 30000 and nxt % 100 == 0:
                    wm, hm = w / 10.0, h / 10.0
                    break
            if wm:
                break
    if not wm:
        w, h = struct.unpack_from("<II", raw, 510)
        wm, hm = w / 10.0, h / 10.0
    return raw, wm, hm


def caps_and_geoms(raw):
    heads = [m.start() for m in re.finditer(rb"\x0f\x00.\x00\xff\xff", raw, re.S)]
    bounds = [0] + heads + [len(raw)]        # seg0=prologue；seg k(≥1)=第 k-1 条记录
    names, caps, geoms = [], [], []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        seg = raw[s:e]
        m = re.match(rb"\x0f\x00(.)\x00\xff\xff", seg, re.S)
        name = ""
        if m:
            nb = bytearray()
            j = m.end()
            while j + 1 < len(seg):
                c = struct.unpack_from("<H", seg, j)[0]
                if c == 0:
                    break
                nb += struct.pack("<H", c)
                j += 2
            name = nb.decode("utf-16-le", "replace")
        # cap：字符串记录 = [u8 类型][u8 长度][00 00 00][数据]，类型 0x10=UTF-16、0x12=UTF-8
        cap = ""
        for o in range(80, min(300, len(seg) - 6)):
            if not (seg[o] in (0x10, 0x11, 0x12) and 2 <= seg[o + 1] <= 120
                    and seg[o + 2] == 0 and seg[o + 3] == 0 and seg[o + 4] == 0):
                continue
            n = seg[o + 1]
            blen = n * 2 if seg[o] == 0x10 else n
            data = seg[o + 5:o + 5 + blen]
            try:
                t = data.decode("utf-16-le" if seg[o] == 0x10 else "utf-8")
            except Exception:
                continue
            core = t.replace("\r\n", "|").replace("\r", "|").replace("\n", "|").strip("|").strip()
            if len(core) >= 1 and (re.search(r"[\u4e00-\u9fff]", core)
                                   or (core.startswith("[") and "]" in core)
                                   or ":" in core):
                cap = core
                break
        # 几何：四元组 (x1,y1,x2,y2) 步长4链，值域合法且行高 12~400
        g = []
        for o in range(100, len(seg) - 15):
            x1, y1, x2, y2 = struct.unpack_from("<iiii", seg, o)
            if 0 <= x1 < x2 <= 820 and 5 <= y1 < y2 <= 500 \
               and 12 <= y2 - y1 <= 400 and x2 - x1 >= 12:
                g.append((x1, y1, x2, y2))
        # 去重：同起点(<12px)的链只保留首个（错位链是噪声）
        seen = set()
        g2 = []
        for r in sorted(g, key=lambda t: (t[0], t[1])):
            if r[0] < 12 or any(abs(r[0]-s) < 12 and abs(r[1]-s2) < 6
                                for s, s2 in seen):
                continue
            seen.add((r[0], r[1]))
            g2.append(r)
        names.append(name)
        caps.append(cap.split("\r")[0].split("\n")[0])
        geoms.append(g2)
    return names, caps, geoms


def build_template(path):
    raw, wm, hm = load(path)
    names, caps, geoms = caps_and_geoms(raw)
    # 配对：段 i 的最后一个几何 ↔ 段 i+1 的 cap；段序第一个 cap ↔ 头部 prologue 的几何
    elems = []
    # 竖排联次条：一字一行的 Memo 沿右列 (rmf x∈[751,781]) 流排，把碎片格并成条带
    col = [(a, b, c, d) for gs in geoms for a, b, c, d in gs
           if 749 <= a <= 753 and 779 <= c <= 783]
    strip = None
    if col:
        strip = max(col, key=lambda r: r[3] - r[1])[1:3]   # 最高碎片=条带主体
    for i in range(len(names) - 1):
        cap = caps[i + 1]
        if not cap or not geoms[i]:
            continue
        cells = [[round(a - X_OFF, 2), b, round(c - X_OFF, 2), d]
                 for a, b, c, d in geoms[i]]
        if "|" in cap and strip:
            cells = [[751 - X_OFF, strip[0], 781 - X_OFF, strip[1]]]
        elems.append({
            "name": names[i + 1], "text": cap,
            "cells_px96": cells,
            "cell_px96": cells[-1],
        })
    return {"pageMM": [wm, hm], "rowCount24px": hm * 96 / 25.4 / ROW,
            "elements": elems, "rawHeads": {"names": names, "caps": caps}}


def main():
    args = []
    skip = False
    for a in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if a == "-o":
            skip = True
            continue
        if not a.startswith("-"):
            args.append(a)
    p = args[0] if args else None
    if not p:
        d0 = glob.glob(r"D:\重构\进销存-本地Agent源码\print_formats\_pulled\*")[0]
        p = [f for f in glob.glob(os.path.join(d0, "BillXS__*")) if "\u6298\u540e\u4ef7" in f][0]
    t = build_template(p)
    out = None
    if "-o" in sys.argv:
        out = sys.argv[sys.argv.index("-o") + 1]
    js = json.dumps(t, ensure_ascii=False, indent=1)
    print(js if out else js[:400])
    if out:
        open(out, "w", encoding="utf-8").write(js)
        print("已写出", out, "元素数", len(t["elements"]))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
