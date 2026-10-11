# -*- coding: utf-8 -*-
"""导出为 ERP 可导入的 Excel97-2003 (.xls)。"""
from __future__ import annotations

from pathlib import Path

import xlwt


HEADERS = ["Hpbh", "HpUnitName", "S_SLUnit", "dj", "bz_xsmx"]
UNMATCHED_HEADERS = ["客户产品名称", "数量", "备注", "状态"]


def _qty(v) -> int | float:
    try:
        f = float(v or 0)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError):
        return 0


def export_excel(rows, out_path: str | Path):
    """已匹配行写入“导入数据”（hpbh|hpdanunit|sl|dj|bz_xsmx），未匹配行写入“未匹配”。"""
    matched = [r for r in rows if r.erp_code]
    unmatched = [r for r in rows if not r.erp_code]

    wb = xlwt.Workbook(encoding="utf-8")

    ws = wb.add_sheet("导入数据")
    for c, h in enumerate(HEADERS):
        ws.write(0, c, h)
    for i, r in enumerate(matched, start=1):
        ws.write(i, 0, r.erp_code)
        ws.write(i, 1, r.erp_unit)
        ws.write(i, 2, _qty(r.qty))
        ws.write(i, 3, 0)           # dj 默认填 0
        ws.write(i, 4, r.remark or "")
    for c, w in enumerate((14, 12, 8, 10, 30)):
        ws.col(c).width = 256 * w

    if unmatched:
        ws2 = wb.add_sheet("未匹配")
        for c, h in enumerate(UNMATCHED_HEADERS):
            ws2.write(0, c, h)
        for i, r in enumerate(unmatched, start=1):
            ws2.write(i, 0, r.merge)
            ws2.write(i, 1, _qty(r.qty))
            ws2.write(i, 2, r.remark or "")
            ws2.write(i, 3, r.status)
        for c, w in enumerate((40, 8, 30, 12)):
            ws2.col(c).width = 256 * w

    wb.save(str(out_path))
    return len(matched), len(unmatched)
