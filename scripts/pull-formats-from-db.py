# -*- coding: utf-8 -*-
"""只读：从指定账套库拉 HxSysReport 打印方案，rmf 解压落盘。

用法：python scripts/pull-formats-from-db.py [库名，默认 shiwantongda]
输出：print_formats/_pulled_<库名>/<时间戳>/  + summary.json
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

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DBNAME = sys.argv[1] if len(sys.argv) > 1 else "shiwantongda"
assert re.fullmatch(r"[\w]+", DBNAME), "库名只允许字母数字下划线"   # 防注入：库名不能参数化


def inflate(blob):
    b = bytes(blob or b"")
    for off in (4, 0):
        if b[off:off + 2] == b"\x78\x9c":
            try:
                return zlib.decompress(b[off:])
            except Exception:
                continue
    return b


def page_mm(raw):
    for m in range(200, len(raw) - 8):
        if struct.unpack_from("<II", raw, m) == (1000, 1000):
            for o in range(m + 8, min(m + 120, len(raw) - 20)):
                w, h = struct.unpack_from("<II", raw, o)
                nxt = struct.unpack_from("<I", raw, o + 8)[0]
                if w % 10 == 0 and 200 <= w <= 2500 and 150 <= h <= 30000 \
                   and 1000 <= nxt <= 30000 and nxt % 100 == 0:
                    return [w / 10.0, h / 10.0]
            break
    return None


cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
cfg = json.loads(open(cfg_path, encoding="utf-8").read())
conn = db.connect(cfg)
cur = conn.cursor()
print("实例默认库 =", (cfg.get("sql") or {}).get("database"), "→ 读取 [", DBNAME, "]")

rows = db.qall(
    cur,
    f"""SELECT RptType, RptName, Rptdefa, sysrpt, RptCustom,
               DATALENGTH(RptMemo), DATALENGTH(RptMemo2), RptMemo, RptMemo2
        FROM [{DBNAME}].dbo.HxSysReport ORDER BY RptType, IDENCOL""")

out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "print_formats", "_pulled_" + DBNAME, time.strftime("%Y%m%d-%H%M%S"))
os.makedirs(out_dir, exist_ok=True)

summary = []
for rpt_type, name, dft, sysrpt, custom, n1, n2, memo, memo2 in rows:
    raw = inflate(memo2 or b"") or inflate(memo or b"")
    rec = {"rptType": rpt_type, "name": name, "isDefault": bool(dft),
           "sysRpt": bool(sysrpt or 0), "custom": bool(custom or 0),
           "inflatedBytes": len(raw), "paperMM": page_mm(raw)}
    if raw:
        safe = re.sub(r'[\\/:*?"<>|]', "_", "%s__%s" % (rpt_type, name))
        with open(os.path.join(out_dir, safe + ".rmf"), "wb") as f:
            f.write(raw)
        rec["file"] = safe + ".rmf"
    summary.append(rec)

json.dump(summary, open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)

import collections
by = collections.Counter(r["rptType"] for r in summary)
print("\n总方案数 =", len(summary), "| RptType 种数 =", len(by))
print("\n== 销售出货单 BillXS ==")
for r in summary:
    if r["rptType"] == "BillXS":
        pm = "%.0fx%.0fmm" % tuple(r["paperMM"]) if r["paperMM"] else "?"
        print("  %-32s %-11s %dB 默认=%s 自建=%s" % (
            r["name"][:30], pm, r["inflatedBytes"], r["isDefault"], r["custom"]))
print("\n其它类型 top10:", by.most_common(10))
print("落盘目录:", out_dir)
conn.close()
