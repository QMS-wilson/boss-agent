# -*- coding: utf-8 -*-
"""本地 SQLite 存储：ERP 物料档案（备用）+ 匹配记忆库。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .core import _cell_to_str, _read_matrix, norm_text


CODE_PATTERNS = ["物料编码", "编码", "料号", "物料号", "编号", "代号", "产品编码"]
BARCODE_PATTERNS = ["条码", "条形码", "二维码", "商品码"]
NAME_PATTERNS = ["零件名称", "物料名称", "名称", "品名", "货品名称"]
SPEC_PATTERNS = ["采购规格", "规格型号", "规格", "型号", "尺寸"]
UNIT_PATTERNS = ["单位", "计量单位"]
VENDOR_PATTERNS = ["供应商", "厂家", "品牌", "供货商"]
MAT_PATTERNS = ["材料", "材质", "物料材质"]


def _find_col(header_row: list, patterns: list[str]) -> int | None:
    for i, cell in enumerate(header_row):
        s = _cell_to_str(cell)
        for p in patterns:
            if p in s:
                return i
    return None


class Storage:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS erp_items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT DEFAULT '',
                barcode TEXT DEFAULT '',
                name TEXT DEFAULT '',
                spec TEXT DEFAULT '',
                unit TEXT DEFAULT '',
                vendor TEXT DEFAULT '',
                material TEXT DEFAULT '',
                name_n TEXT DEFAULT '',
                spec_n TEXT DEFAULT '',
                mat_n TEXT DEFAULT '',
                merge_n TEXT DEFAULT '',
                UNIQUE(code, barcode)
            );
            CREATE INDEX IF NOT EXISTS idx_erp_name ON erp_items(name_n);

            CREATE TABLE IF NOT EXISTS memory(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merge_n TEXT NOT NULL UNIQUE,
                customer_name TEXT DEFAULT '',
                code TEXT DEFAULT '',
                unit TEXT DEFAULT '',
                match_count INTEGER DEFAULT 1,
                last_used TEXT DEFAULT ''
            );
            """
        )
        # 旧版本升级：memory 缺 merge_n 时**补列**（原版这里 DROP TABLE 重建）。
        # ★ 常驻 Agent 里绝不能删用户的匹配记忆：记忆库是"越用越准"的资产，
        #   一次升级把它清空，等于让用户白干几个月。SQLite 的 ALTER 加列
        #   对已有行填默认值，足够兼容旧表结构。
        mem_cols = {r[1] for r in cur.execute("PRAGMA table_info(memory)").fetchall()}
        if "merge_n" not in mem_cols:
            cur.execute("ALTER TABLE memory ADD COLUMN merge_n TEXT DEFAULT ''")
            cur.execute(
                "UPDATE memory SET merge_n = customer_name "
                "WHERE (merge_n IS NULL OR merge_n = '') AND customer_name <> ''"
            )
        erp_cols = {r[1] for r in cur.execute("PRAGMA table_info(erp_items)").fetchall()}
        if "merge_n" not in erp_cols:
            cur.execute("ALTER TABLE erp_items ADD COLUMN merge_n TEXT DEFAULT ''")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_erp_merge ON erp_items(merge_n)")
        self.conn.commit()

    # ---------------- ERP 物料档案（Excel 备用数据源） ----------------
    def all_erp_items(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT id, code, barcode, name, spec, unit, vendor, material, name_n, spec_n, mat_n, merge_n "
            "FROM erp_items ORDER BY id"
        )
        items = []
        for r in cur.fetchall():
            d = dict(r)
            name_n = d.get("name_n") or ""
            spec_n = d.get("spec_n") or ""
            d["key_a"] = name_n + spec_n
            d["key_b"] = ""
            d["spec_a"] = d.get("spec") or ""
            d["spec_a_n"] = spec_n
            d["spec_b"] = ""
            d["spec_b_n"] = ""
            d["sys_name"] = d.get("name") or ""
            d["sys_spec"] = d.get("spec") or ""
            items.append(d)
        return items

    def import_erp_items(self, path: str | Path) -> int:
        """从 Excel 导入 ERP 物料档案（备用数据源），返回新增条数。"""
        path = Path(path)
        added = 0
        for sheet_name, matrix in _read_matrix(path):
            header_idx = None
            for i, row in enumerate(matrix):
                cells = [_cell_to_str(c) for c in row]
                has_code = any(any(p in c for p in CODE_PATTERNS) for c in cells)
                has_name = any(any(p in c for p in NAME_PATTERNS) for c in cells)
                if has_code and has_name:
                    header_idx = i
                    break
            if header_idx is None:
                continue
            header = matrix[header_idx]
            ci = {
                "code": _find_col(header, CODE_PATTERNS),
                "barcode": _find_col(header, BARCODE_PATTERNS),
                "name": _find_col(header, NAME_PATTERNS),
                "spec": _find_col(header, SPEC_PATTERNS),
                "unit": _find_col(header, UNIT_PATTERNS),
                "vendor": _find_col(header, VENDOR_PATTERNS),
                "material": _find_col(header, MAT_PATTERNS),
            }
            for r in range(header_idx + 1, len(matrix)):
                row = matrix[r]
                code = _cell_to_str(row[ci["code"]]) if ci["code"] is not None else ""
                barcode = _cell_to_str(row[ci["barcode"]]) if ci["barcode"] is not None else ""
                name = _cell_to_str(row[ci["name"]]) if ci["name"] is not None else ""
                spec = _cell_to_str(row[ci["spec"]]) if ci["spec"] is not None else ""
                if not (code or barcode):
                    continue
                unit = _cell_to_str(row[ci["unit"]]) if ci["unit"] is not None else ""
                vendor = _cell_to_str(row[ci["vendor"]]) if ci["vendor"] is not None else ""
                material = _cell_to_str(row[ci["material"]]) if ci["material"] is not None else ""
                name_n = norm_text(name)
                spec_n = norm_text(spec)
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO erp_items(code, barcode, name, spec, unit, vendor, material, name_n, spec_n, mat_n, merge_n) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        code,
                        barcode,
                        name,
                        spec,
                        unit,
                        vendor,
                        material,
                        name_n,
                        spec_n,
                        norm_text(material),
                        name_n + spec_n,
                    ),
                )
                added += cur.rowcount
        self.conn.commit()
        return added

    # ---------------- 匹配记忆 ----------------
    def find_memory(self, merge_n: str) -> dict | None:
        cur = self.conn.execute("SELECT * FROM memory WHERE merge_n=?", (merge_n,))
        row = cur.fetchone()
        return dict(row) if row else None

    def save_memory(self, merge_n: str, customer_name: str, code: str, unit: str):
        self.conn.execute(
            """
            INSERT INTO memory(merge_n, customer_name, code, unit, match_count, last_used)
            VALUES (?,?,?,?,1, datetime('now','localtime'))
            ON CONFLICT(merge_n) DO UPDATE SET
                code=excluded.code, unit=excluded.unit,
                match_count=match_count+1, last_used=datetime('now','localtime')
            """,
            (merge_n, customer_name, code, unit),
        )
        self.conn.commit()

    def bump_memory(self, mem_id: int):
        self.conn.execute(
            "UPDATE memory SET match_count=match_count+1, last_used=datetime('now','localtime') WHERE id=?",
            (mem_id,),
        )
        self.conn.commit()

    def all_memory(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT id, customer_name, code, unit, match_count, last_used "
            "FROM memory ORDER BY match_count DESC, last_used DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def delete_memory(self, mem_id: int):
        self.conn.execute("DELETE FROM memory WHERE id=?", (mem_id,))
        self.conn.commit()

    def clear_memory(self):
        self.conn.execute("DELETE FROM memory")
        self.conn.commit()

    def close(self):
        """关掉连接（ops 是短生命周期，用完要还）。"""
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
