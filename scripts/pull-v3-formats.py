# -*- coding: utf-8 -*-
"""只读拉取 V3 全部打印方案（HxSysReport），解出 rmf 落盘 + 纸张尺寸/控件统计。

输出目录：print_formats/_pulled/<时间戳>/
  <RptType>__<RptName>.rmf        每个方案的解压定义（ReportMachine 3.0）
  summary.json / 控制台表格        纸张（0.1mm@510/514）、Memo 记录数、文本控件数
"""
import json
import os
import re
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from v3agent import sql as db

CFG = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def inflate(blob):
    b = bytes(blob or b"")
    for off in (4, 0):
        if b[off:off + 2] == b"\x78\x9c":
            try:
                return zlib.decompress(b[off:])
            except Exception:
                continue
    return b


def page_size(raw):
    """rmf 头部 510/514 = 页宽/页高（0.1mm）。越界/不合理给 None。"""
    if len(raw) < 518:
        return None
    w, h = struct.unpack_from("<II", raw, 510)
    if 300 <= w <= 20000 and 200 <= h <= 30000:
        return (w, h)
    return None


def u16_texts(raw):
    """UTF-16LE 连续可打印串（控件标签/绑定字段名）。"""
    out = []
    cur = []
    for i in range(0, len(raw) - 1, 2):
        ch = chr(raw[i] | (raw[i + 1] << 8))
        if ch.isprintable():
            cur.append(ch)
        else:
            if len(cur) >= 2:
                out.append("".join(cur))
            cur = []
    if len(cur) >= 2:
        out.append("".join(cur))
    return out


cfg = json.loads(open(CFG, encoding="utf-8").read())
conn = db.connect(cfg)
cur = conn.cursor()
print("连接库 =", (cfg.get("sql") or {}).get("database"))

rows = db.qall(
    cur,
    """SELECT RptType, RptName, Rptdefa, sysrpt, RptCustom, RptVerName,
              DATALENGTH(RptMemo), DATALENGTH(RptMemo2), RptMemo, RptMemo2
       FROM [AccMaster].dbo.HxSysReport ORDER BY RptType, IDENCOL""")

out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "print_formats", "_pulled", time.strftime("%Y%m%d-%H%M%S"))
os.makedirs(out_dir, exist_ok=True)

summary = []
for rpt_type, name, dft, sysrpt, custom, ver, n1, n2, memo, memo2 in rows:
    raw = inflate(memo2 or b"") or inflate(memo or b"")
    rec = {"rptType": rpt_type, "name": name, "isDefault": bool(dft),
           "sysRpt": bool(sysrpt or 0), "custom": bool(custom or 0), "verName": ver,
           "blobBytes": int(n2 or n1 or 0), "inflatedBytes": len(raw)}
    ps = page_size(raw)
    rec["paper_0p1mm"] = list(ps) if ps else None
    rec["paperMM"] = [ps[0] / 10.0, ps[1] / 10.0] if ps else None
    rec["memos"] = len(re.findall(rb"M\x00e\x00m\x00o\x00", raw))
    rec["cjkLabels"] = sorted({t for t in u16_texts(raw)
                               if re.search(r"[\u4e00-\u9fff]", t)})[:40]
    if raw:
        safe = re.sub(r'[\\/:*?"<>|]', "_", "%s__%s" % (rpt_type, name))
        p = os.path.join(out_dir, safe + ".rmf")
        with open(p, "wb") as f:
            f.write(raw)
        rec["file"] = os.path.basename(p)
    summary.append(rec)

with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=1)

print()
print("%-9s %-30s %-8s %-11s %s" % ("类型", "方案", "默认", "纸张mm", "Memo/解压B"))
for r in summary:
    pm = ("%.0fx%.0f" % tuple(r["paperMM"])) if r["paperMM"] else "?"
    print("%-9s %-30s %-8s %-11s %d / %d" % (
        r["rptType"], r["name"][:28], "★" if r["isDefault"] else "", pm,
        r["memos"], r["inflatedBytes"]))
print()
print("共 %d 个方案 → %s" % (len(summary), out_dir))
conn.close()
