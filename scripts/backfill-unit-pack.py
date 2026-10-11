# -*- coding: utf-8 -*-
"""一次性回填：给已有的多单位货品补 `Hpdan.UnitPack` / `Hpdan.UnitGgxh`（+ 包装那行的 UnitPackage 位）。

★ 为什么单独一个脚本，而不是拿 save_product 跑一遍：
  派生规则**只有一份**，就是 `v3agent.product.pack_values`（真源：V3 客户端建的 85485455515418
  = `件*个` / `1*12`）。这里只重发那条 UPDATE 语句，不重算规则，也不碰价格/单位行以外的任何东西。
★ 默认**干跑**：只打印要写什么，一个字都不动；加 `--apply` 才写。
★ 有业务单据的货品一律跳过：`Update(UnitPack)` 在 `TRI_A_Hpdan` 的按列保护里会被 V3 拒，
  而且 V3 会接着跑 `HxTri_Hpdan_ggxh` 把所有带这两列的表按 hpbh 刷一遍（= 改写历史单据行）。

用法：
  <venvPy> scripts/backfill-unit-pack.py                 # 干跑（默认那三条）
  <venvPy> scripts/backfill-unit-pack.py --apply
  <venvPy> scripts/backfill-unit-pack.py --codes 85485455515416,85485455515417 --apply
"""
import argparse
import json
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass
import pymssql  # noqa: E402

from v3agent import product  # noqa: E402

DEFAULT_CODES = ["15474547787889", "85485455515417", "85485455515416"]
CFG = json.load(open(os.environ.get("JXC_AGENT_CONFIG", "D:\\config.json"), encoding="utf-8"))["sql"]
ap = argparse.ArgumentParser()
ap.add_argument("--db", default=None)
ap.add_argument("--codes", default=",".join(DEFAULT_CODES))
ap.add_argument("--apply", action="store_true", help="真写（默认干跑）")
a = ap.parse_args()
db = a.db or CFG["database"]
codes = [c.strip() for c in str(a.codes).split(",") if c.strip()]
conn = pymssql.connect(server=CFG["server"], port=str(CFG["port"]), user=CFG["user"],
                       password=CFG["password"], database=db,
                       tds_version=CFG.get("tds_version", "7.0"))
cur = conn.cursor()
print("账套 %s ｜ %s" % (db, "★ 真写" if a.apply else "干跑（要写加 --apply）"))

for code in codes:
    row = product.find_row(cur, code)
    if not row:
        print("\n%s：库里没有这条，跳过" % code)
        continue
    guid = product.guid_of(cur, code)
    rows = product.read_units(cur, guid)
    bills = product.bill_usage(cur, code)
    units = [{"unit": u["unit"], "rate": Decimal(str(u["rate"])), "basic": bool(u["basic"]),
              "stop": bool(u["stop"])} for u in rows]
    got = product.pack_values(units)
    now = (row["UnitPack"] or "", row["UnitGgxh"] or "")
    print("\n%s %s 基本单位=%r 单位=%s" % (
        code, row["hpmc"], row["HpUnit"],
        "、".join("%s:%s%s" % (u["unit"], u["rate"], "(停)" if u["stop"] else "") for u in units)))
    print("    库里现在是 UnitPack=%r UnitGgxh=%r" % now)
    print("    按 V3 口径应该是      UnitPack=%r UnitGgxh=%r" % (got["UnitPack"], got["UnitGgxh"]))
    if bills:
        print("    ⚠ 已有 %d 行业务单据 ⇒ **不动**（V3 会拦 Update(UnitPack)，还会去刷历史单据行）" % bills)
        continue
    if now == (got["UnitPack"], got["UnitGgxh"]):
        print("    已经是对的，不写")
        continue
    if not a.apply:
        print("    干跑：本条会写这两列（+ 把包装那行的 UnitPackage 置 1）")
        continue
    cur.execute("UPDATE Hpdan SET UnitPack=%s, UnitGgxh=%s, UserUpdate='AGENT', "
                "TimeUpdate=GETDATE() WHERE LTRIM(RTRIM(hpbh))=%s",
                (got["UnitPack"], got["UnitGgxh"], code))
    pk = product.pack_unit_of(units)
    if pk:
        # 只把包装那一档**点亮**，不去摘别的行（这脚本管不到 V3 自己可能标过的别的档）
        cur.execute("UPDATE HpdanUnit SET UnitPackage=1, TimeUpdate=GETDATE() "
                    "WHERE HpIdenGuid=CONVERT(uniqueidentifier,%s) AND LTRIM(RTRIM(UnitName))=%s "
                    "AND ISNULL(UnitPackage,0)=0", (str(guid), pk["unit"]))
    conn.commit()
    print("    ✓ 已写入")

print("\n复验（只读）：")
cur.execute("SELECT LTRIM(RTRIM(hpbh)), LTRIM(RTRIM(ISNULL(UnitPack,''))), "
            "LTRIM(RTRIM(ISNULL(UnitGgxh,''))) FROM Hpdan WHERE hpbh IN (%s) "
            "ORDER BY hpbh" % ", ".join(["%s"] * len(codes)), tuple(codes))
for r in cur.fetchall():
    print("  %s  UnitPack=%r  UnitGgxh=%r" % (str(r[0]).strip(), r[1], r[2]))
conn.close()
