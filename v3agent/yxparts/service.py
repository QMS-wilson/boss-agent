# -*- coding: utf-8 -*-
"""燕秀零件处理：op 的业务实现（与桌面标签页同一套 core/storage/sqlsource）。

约定（与《燕秀集成方案》第 5、7 章一致）：
  * 解析/归一化/匹配只用本包一份实现，App 只负责把表格读成矩阵；
  * 记忆库落在 Agent 运行目录的 logs/yx_matchlib.db，桌面页与 App 共用；
  * 业务错误一律抛 YxError（带 YX_ 错误码），由 dispatch 翻译成 error 信封。
"""
from __future__ import annotations

import json
import os
import sys

from . import core as yxcore
from . import sqlsource as yxsql
from . import storage as yxstorage
from .errors import YxError

# 上限（超出即报错，不做静默截断）
LIMIT_ROWS = 2000          # 单次最多处理的行数
LIMIT_BYTES = 512 * 1024   # 矩阵 JSON 的字节上限
MAX_CELL = 500             # 单个单元格文本上限

DEFAULT_LBTYPE_KH = "801-"  # 现网数据用的客户类别编号（旧环境是 21-）


# ---------------------------------------------------------------------------
# 配置与路径
# ---------------------------------------------------------------------------

def runtime_dir() -> str:
    """Agent 运行目录（与 agent_daemon.runtime_dir 同口径：项目根/exe 目录下的 logs）。"""
    env = os.environ.get("V3AGENT_RUNTIME_DIR")
    if env:
        base = env
    elif getattr(sys, "frozen", False):
        base = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "logs")
    else:
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return base


def memory_path() -> str:
    return os.path.join(runtime_dir(), "yx_matchlib.db")


def memory(cfg=None):
    """开一个记忆库句柄（用完要 close）。"""
    return yxstorage.Storage(memory_path())


def yx_cfg(cfg) -> dict:
    """把 Agent 的 cfg 折成燕秀那套配置（表名/字段/客户类别编号）。

    ★ 只读不写：op 不改 Agent 的 config.json；要改字段在 `yanxiu` 段里配。
    """
    sql = (cfg or {}).get("sql") or {}
    yx = (cfg or {}).get("yanxiu") or {}
    db = str(sql.get("database") or "")
    by_db = yx.get("lbtypeKhByDb") or {}
    return {
        "sqlserver": {
            "server": sql.get("server", ""), "port": int(sql.get("port", 1433) or 1433),
            "database": db, "user": sql.get("user", ""), "password": sql.get("password", ""),
            "windows_auth": False, "tds_version": str(sql.get("tds_version", "7.0")),
            "table": yx.get("table", "HPKhType"),
            "field_match": yx.get("fieldMatch", "hpmckh"),
            "field_spec": yx.get("fieldSpec", "GgxhKh"),
            "field_spec2": yx.get("fieldSpec2", "HpbhKh"),
            "field_code": yx.get("fieldCode", "hpbh"),
            "field_unit": yx.get("fieldUnit", "HpUnitName"),
        },
        "hpdan": {
            "table": yx.get("hpdanTable", "hpdan"),
            "name_field": yx.get("hpdanName", "hpmc"),
            "code_field": yx.get("hpdanCode", "hpbh"),
            "unit_field": yx.get("hpdanUnit", "hpunit"),
            "spec_field": yx.get("hpdanSpec", "Ggxh"),
            "lbtype_kh": by_db.get(db) or yx.get("lbtypeKh") or DEFAULT_LBTYPE_KH,
        },
    }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _cell(v) -> str:
    """App 传来的单元格 → 文本（与 core._cell_to_str 同口径：整数不带 .0）。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if hasattr(v, "strftime"):      # 日期/时间：与桌面 _cell_to_str 同格式
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else str(v)
    return str(v).strip()


def _matrix(rows, sheet_name: str) -> list[list[str]]:
    out: list[list[str]] = []
    for ri, row in enumerate(rows):
        if not isinstance(row, (list, tuple)):
            raise YxError("YX_BAD_REQUEST", "表格数据格式不对，请重新选择文件",
                          {"field": "rows[%d]" % ri, "sheet": sheet_name})
        cells = []
        for ci, c in enumerate(row):
            txt = _cell(c)
            if len(txt) > MAX_CELL:
                raise YxError("YX_CELL_TOO_LONG",
                              "第 %d 行有异常超长的内容，请检查原表" % (ri + 1),
                              {"sheet": sheet_name, "row": ri, "col": ci, "len": len(txt)})
            cells.append(txt)
        out.append(cells)
    return out


def _sheet_diag(matrix) -> tuple[int, bool]:
    """返回 (表头行号, 表头下面是否还有内容)。认不出表头时行号为 -1。"""
    for i, row in enumerate(matrix):
        cells = [yxcore._cell_to_str(c) for c in row]
        has_spec = any(any(k in c for k in yxcore.SPEC_HEADER_KEYS) for c in cells)
        if any("零件名称" in c for c in cells) and has_spec:
            rest = any(any(str(c).strip() for c in r) for r in matrix[i + 1:])
            return i, rest
    return -1, False


def _load_library(cur, cfg) -> tuple[list[dict], dict, list[str]]:
    """读物料库（HPKhType）+ 系统产品名映射（hpdan）。"""
    yx = yx_cfg(cfg)
    items = yxsql.fetch_items_cur(cur, yx)
    sys_map, missing = yxsql.fetch_hpdan_sys_map_cur(cur, yx)
    return items, sys_map, missing


def _row_out(row, matcher) -> dict:
    keys = [k for k in (row.merge_a_n, row.merge_b_n if row.actual else "") if k]
    codes = set()
    for it in matcher.items:
        if it.get("key_a") in keys:
            codes.add(it.get("code") or "")
        elif row.actual and (it.get("spec_b") or "").strip() and it.get("key_b") in keys:
            codes.add(it.get("code") or "")
    codes.discard("")
    return {
        "lineNo": 0, "seq": row.seq, "sheet": row.sheet, "moldNo": row.mold_no,
        "name": row.name, "spec": row.spec, "actual": row.actual, "material": row.material,
        "qty": row.qty, "remark": row.remark, "customerName": row.merge,
        "hpbh": row.erp_code, "unit": row.erp_unit, "sysName": row.sys_name,
        "status": row.status, "source": row.source, "ambiguous": len(codes) > 1,
    }


# ---------------------------------------------------------------------------
# op 实现
# ---------------------------------------------------------------------------

def match(cur, cfg, params) -> dict:
    """矩阵 → 与桌面一致的逐行匹配结果。"""
    sheets = params.get("sheets")
    if not sheets:
        one = params.get("sheet")
        sheets = [one] if isinstance(one, dict) else None
    if not isinstance(sheets, list) or not sheets:
        raise YxError("YX_NO_SHEETS", "没读到工作表，请重新选择文件")

    total = sum(len((s or {}).get("rows") or []) for s in sheets if isinstance(s, dict))
    if total > LIMIT_ROWS:
        raise YxError("YX_TOO_MANY_ROWS", "行数超出上限（%d 行），请拆分文件后重试" % total,
                      {"actual": total, "limit": LIMIT_ROWS})

    # 先把单元格文本化（datetime/Decimal/数字一视同仁），再算体积——客户端传数字或
    # 日期对象也不会在这里炸；体积按文本化后的实际大小算，避免"看着小、转完超限"。
    sheets_txt = []
    for s in sheets:
        if not isinstance(s, dict):
            raise YxError("YX_BAD_REQUEST", "表格数据格式不对，请重新选择文件", {"field": "sheets[]"})
        name = str(s.get("name") or "")
        rows = s.get("rows") or []
        if not isinstance(rows, list):
            raise YxError("YX_BAD_REQUEST", "表格数据格式不对，请重新选择文件", {"field": "sheet.rows"})
        sheets_txt.append((name, _matrix(rows, name)))
    payload = json.dumps([[n, m] for n, m in sheets_txt], ensure_ascii=False)
    if len(payload.encode("utf-8")) > LIMIT_BYTES:
        raise YxError("YX_PAYLOAD_TOO_LARGE", "文件太大，请拆分后重试",
                      {"bytes": len(payload.encode("utf-8")), "limit": LIMIT_BYTES})

    parsed, reasons, head_rows = [], [], []
    for name, matrix in sheets_txt:
        header_idx, has_data = _sheet_diag(matrix)
        if not head_rows and matrix:
            head_rows = [[yxcore._cell_to_str(c) for c in r][:12] for r in matrix[:3]]
        if header_idx < 0:
            reasons.append({"sheet": name, "reason": "header"})
            continue
        if not has_data:
            reasons.append({"sheet": name, "reason": "empty"})
            continue
        part_rows = yxcore._parse_sheet(matrix, "手机上传", name or "Sheet1")
        if part_rows:
            parsed.extend(part_rows)
        else:
            reasons.append({"sheet": name, "reason": "empty"})

    if not parsed:
        if reasons and all(r["reason"] == "header" for r in reasons):
            raise YxError("YX_HEADER_NOT_FOUND",
                          "没识别到零件清单表头：需要「零件名称」+「采购规格/规格型号/规格/型号」，请确认文件是否正确",
                          {"needKeywords": ["零件名称"] + list(yxcore.SPEC_HEADER_KEYS),
                           "headRows": head_rows, "sheets": reasons})
        if any(r["reason"] == "empty" for r in reasons):
            raise YxError("YX_SHEET_EMPTY", "表头识别到了，但下面没有数据行",
                          {"sheets": reasons})
        raise YxError("YX_ALL_SHEETS_EMPTY", "这个文件里没有可处理的零件行", {"sheets": reasons})

    items, sys_map, _missing = _load_library(cur, cfg)
    if not items:
        raise YxError("YX_ACCOUNT_SET_EMPTY",
                      "本账套的客户品名库是空的，请先在电脑端用「导入燕秀品名」铺数据",
                      {"database": (cfg.get("sql") or {}).get("database") or ""})

    store = memory(cfg)
    try:
        matcher = yxcore.Matcher(store)
        matcher.load_hpdan(items, sys_map)
        out_rows = []
        for i, row in enumerate(parsed, start=1):
            matcher.auto_match(row)
            item = _row_out(row, matcher)
            item["lineNo"] = i
            out_rows.append(item)
        memory_rows = len(store.all_memory())
    finally:
        store.close()

    matched = sum(1 for r in out_rows if r["hpbh"])
    by_source = {}
    for r in out_rows:
        if r["hpbh"]:
            by_source[r["status"]] = by_source.get(r["status"], 0) + 1
    return {
        "rows": out_rows,
        "stats": {"total": len(out_rows), "matched": matched,
                  "unmatched": len(out_rows) - matched, "bySource": by_source},
        "library": {"rows": len(items), "hpdanRows": len(sys_map), "memoryRows": memory_rows,
                    "source": matcher.source_label},
        "sheets": [{"name": s.get("name") or "", "parsed": True} for s in sheets],
    }


def suggest(cur, cfg, params) -> dict:
    """未匹配行的候选（与桌面双击弹窗同一函数、同一排序）。

    ★ `search=1`：在整库里按关键词搜（App 候选框顶部那栏），不套相似度门槛。
      一次最多回 50 条 —— 手机上一次渲染几百条会明显卡，而且那也看不清。
    """
    items, sys_map, _missing = _load_library(cur, cfg)
    if not items:
        raise YxError("YX_ACCOUNT_SET_EMPTY", "本账套的客户品名库是空的，请先导入燕秀品名",
                      {"database": (cfg.get("sql") or {}).get("database") or ""})
    searching = bool(params.get("search"))
    keyword = str(params.get("keyword") or "")
    if searching and not keyword.strip():
        raise YxError("YX_SEARCH_NO_KEYWORD", "要按关键词搜货品，先输入两个字再搜")
    row = yxcore.PartRow(source_file="", sheet="", mold_no="", seq="", part_no_orig="",
                         name=str(params.get("name") or ""), material="",
                         qty=0, spec=str(params.get("spec") or ""),
                         actual=str(params.get("actual") or ""), vendor="", heat="", remark="")
    store = memory(cfg)
    try:
        matcher = yxcore.Matcher(store)
        matcher.load_hpdan(items, sys_map)
        cands = matcher.suggest(row, keyword=keyword, search=searching,
                                top=int(params.get("top") or 50) if searching else None)
    finally:
        store.close()
    out = []
    for score, it in cands:
        info = sys_map.get(it.get("code") or "") or {}
        out.append({"hpbh": it.get("code") or "", "hpmcKh": it.get("name") or "",
                    "hpmc": info.get("sys_name") or it.get("sys_name") or "",
                    "specA": it.get("spec_a") or "", "specB": it.get("spec_b") or "",
                    "unit": it.get("unit") or "", "score": round(float(score), 4),
                    # 拼音码带回去：手机上搜的就是它，列表里得让人看见自己搜的东西
                    "py": (sys_map.get(it.get("code") or "") or {}).get("sys_py") or ""})
    # search / total 回给前端：列表标题要写清"这是按关键词在整库里搜的"还是"按相似度给的候选"，
    # 否则同一个框换个关键词，人不知道看到的列表是怎么来的。
    return {"items": out, "search": bool(searching), "keyword": keyword,
            "total": len(out), "capped": bool(searching and len(out) >= 50)}


def remember(cur, cfg, params) -> dict:
    """人工确认 → 写本地记忆库（不写业务库）。"""
    key = yxcore.norm_text(params.get("key") or params.get("customerName") or "")
    code = str(params.get("hpbh") or "").strip()
    unit = str(params.get("unit") or "").strip()
    if not code:
        raise YxError("YX_BAD_HPBH", "请选择要记住的货品编号")
    if not key:
        raise YxError("YX_BAD_REQUEST", "缺少客户写法（名称+规格）", {"field": "key"})

    items, _sys_map, _missing = _load_library(cur, cfg)
    if not items:
        raise YxError("YX_ACCOUNT_SET_EMPTY", "本账套的客户品名库是空的，请先导入燕秀品名",
                      {"database": (cfg.get("sql") or {}).get("database") or ""})
    hit = next((it for it in items if (it.get("code") or "") == code), None)
    if hit is None:
        raise YxError("YX_ITEM_NOT_FOUND", "这个货品编号在物料库里不存在", {"hpbh": code})

    store = memory(cfg)
    try:
        store.save_memory(key, str(params.get("customerName") or key), code, unit)
        key_b = ""
        if params.get("actual") and params.get("keyB"):
            key_b = yxcore.norm_text(params.get("keyB"))
            if key_b and key_b != key:
                store.save_memory(key_b, str(params.get("customerName") or key), code, unit)
        total = len(store.all_memory())
    finally:
        store.close()
    return {"saved": 1, "keyA": key, "keyB": key_b, "memoryRows": total}


def memory_list(cfg, params) -> dict:
    store = memory(cfg)
    try:
        items = store.all_memory()
    finally:
        store.close()
    return {"items": items, "total": len(items)}


def memory_delete(cfg, params) -> dict:
    try:
        mem_id = int(params.get("id") or 0)
    except (TypeError, ValueError):
        mem_id = 0
    if mem_id <= 0:
        raise YxError("YX_BAD_REQUEST", "缺少要删除的记忆编号", {"field": "id"})
    store = memory(cfg)
    try:
        store.delete_memory(mem_id)
        total = len(store.all_memory())
    finally:
        store.close()
    return {"deleted": 1, "memoryRows": total}


def capabilities(cur, cfg, params) -> dict:
    """物料库/记忆库现状（App 用它显示"库还没铺"的引导）。"""
    yx = yx_cfg(cfg)
    hpkh = yxsql.count_rows_cur(cur, yx["sqlserver"]["table"])
    hpdan = yxsql.count_rows_cur(cur, yx["hpdan"]["table"])
    lbtypes = lbtypes_cur(cur)
    want = yx["hpdan"]["lbtype_kh"]
    # 配置里那个类别若不在本库清单里（比如默认 801- 而本库是 K01-），
    # 就把默认值让给清单里的第一项 —— 免得导入时才发现"客户类别不存在"。
    default_lb = want if any(x["key"] == want for x in lbtypes) else (lbtypes[0]["key"] if lbtypes else want)
    store = memory(cfg)
    try:
        mem_rows = len(store.all_memory())
    finally:
        store.close()
    return {"database": (cfg.get("sql") or {}).get("database") or "",
            "hpkhTypeRows": hpkh, "hpdanRows": hpdan,
            "lbtypeKh": default_lb, "lbtypeConfigured": want,
            "lbtypes": lbtypes, "memoryRows": mem_rows,
            "memoryPath": memory_path()}


def lbtypes_cur(cur) -> list[dict]:
    """客户类别清单（KHType.Key_ / Text）—— 导入界面的"客户类别"下拉用它。"""
    try:
        cur.execute("SELECT RTRIM(Key_), RTRIM(Text) FROM KHType ORDER BY Key_")
        return [{"key": (r[0] or "").strip(), "text": (r[1] or "").strip()} for r in cur.fetchall()]
    except Exception:  # noqa: BLE001  老库没有 Text 列也不该把整页打挂
        try:
            cur.execute("SELECT RTRIM(Key_) FROM KHType ORDER BY Key_")
            return [{"key": (r[0] or "").strip(), "text": ""} for r in cur.fetchall()]
        except Exception:  # noqa: BLE001
            return []


def _alive(cur) -> bool:
    try:
        cur.execute("SELECT 1")
        cur.fetchall()
        return True
    except Exception:  # noqa: BLE001
        return False


def import_customers(cur, cfg, params) -> dict:
    """导入燕秀品名 → 写 HPKhType（逐行 INSERT，部分失败不整体回滚）。

    两种入参（App 只发矩阵，不发解析结果 —— 与 A1.5 同一条原则）：
      · `rows`   ：`[{goodsName, custName, spec, actual}]`，已解析好的行；
      · `sheets` ：`[{name, rows}]` 原始矩阵，模板表头在这里识别。

    `dryRun=true` 只做**预检**（查编号/单位、判重），一行都不写，返回逐行结果给界面预览。
    """
    dry = bool(params.get("dryRun"))
    rows = params.get("rows") or []
    if not rows and params.get("sheets"):
        # ★ 解析出来是 snake_case（桌面端 ui.py 一直这么用），op 这边统一成 camelCase，
        #   免得下面每条判断都要写两种键名。
        rows = [{"goodsName": r.get("goods_name", ""), "custName": r.get("cust_name", ""),
                 "spec": r.get("spec", ""), "actual": r.get("actual", "")}
                for r in yxcore.parse_import_sheets(params.get("sheets"))]
        if not rows:
            raise YxError("YX_HEADER_NOT_FOUND",
                          "没识别到导入模板表头：需要「货品名称」+「客户品名」两列"
                          "（采购规格、实际尺寸可选）",
                          {"needKeywords": ["货品名称/物料名称", "客户品名/客户产品名称"]})
    if not isinstance(rows, list) or not rows:
        raise YxError("YX_BAD_REQUEST", "没有要导入的行", {"field": "rows"})
    if len(rows) > LIMIT_ROWS:
        raise YxError("YX_TOO_MANY_ROWS", "行数超出上限（%d 行），请拆分后重试" % len(rows),
                      {"actual": len(rows), "limit": LIMIT_ROWS})

    yx = yx_cfg(cfg)
    lbtypes = lbtypes_cur(cur)
    want = yx["hpdan"]["lbtype_kh"]
    default_lb = want if any(x["key"] == want for x in lbtypes) else (lbtypes[0]["key"] if lbtypes else want)
    lbtype = str(params.get("lbtypeKh") or default_lb)
    if not yxsql.lbtype_exists_cur(cur, lbtype):
        raise YxError("YX_LBTYPE_NOT_FOUND",
                      "客户类别「%s」在 KHType 里不存在，请先在 V3 里维护这个客户类别" % lbtype,
                      {"lbtypeKh": lbtype, "lbtypes": lbtypes})
    lookup = yxsql.fetch_hpdan_lookup_cur(cur, yx)

    # ---- 第一步：逐行预检，算出"打算怎么做"（预检与实际写入共用这一份判断）----
    plan: list[dict] = []
    for it in rows:
        goods = str((it or {}).get("goodsName") or "").strip()
        cust = str((it or {}).get("custName") or "").strip()
        spec = str((it or {}).get("spec") or "").strip()
        actual = str((it or {}).get("actual") or "").strip()
        info = lookup.get(yxcore.norm_text(goods)) if goods else None
        row = {"goodsName": goods, "custName": cust, "spec": spec, "actual": actual,
               "hpbh": "", "unit": "", "status": "", "reason": ""}
        if not cust:
            row.update(status="失败", reason="缺少客户品名")
        elif not info or not info.get("code"):
            row.update(status="失败", reason="货品名称在 hpdan 里查不到")
        else:
            row["hpbh"] = info["code"]
            row["unit"] = info.get("unit") or ""
            if not row["unit"] or not yxsql.hpdan_ex_exists_cur(cur, row["hpbh"], row["unit"]):
                row.update(status="失败", reason="货品或计量单位在 HpdanEx 里不存在")
            elif yxsql.hpkh_exists_cur(cur, yx, lbtype, row["hpbh"], row["unit"]):
                row.update(status="已存在", reason="同一客户类别+货品+单位已有记录")
            else:
                row.update(status="可导入")
        plan.append(row)

    if dry:
        return {"dryRun": True, "lbtypeKh": lbtype, "lbtypes": lbtypes, "rows": plan,
                "summary": {"total": len(plan),
                            "importable": sum(1 for r in plan if r["status"] == "可导入"),
                            "exists": sum(1 for r in plan if r["status"] == "已存在"),
                            "failed": sum(1 for r in plan if r["status"] == "失败"),
                            "withPrice": 0}}

    # ---- 第二步：真正写库（逐行 INSERT；部分失败不整体回滚）----
    inserted = skipped_dup = skipped_pk = 0
    failed: list[dict] = []
    outcomes: list[dict] = []
    for row in plan:
        goods, cust = row["goodsName"], row["custName"]
        code, unit, spec, actual = row["hpbh"], row["unit"], row["spec"], row["actual"]
        if row["status"] == "失败":
            failed.append({"goodsName": goods, "reason": row["reason"], "hpbh": code})
            outcomes.append({"goodsName": goods, "custName": cust, "status": "失败：" + row["reason"]})
            continue
        if row["status"] == "已存在":
            skipped_dup += 1
            outcomes.append({"goodsName": goods, "custName": cust, "hpbh": code,
                             "status": "已存在，跳过"})
            continue
        try:
            yxsql.insert_hpkh_cur(cur, yx, lbtype, code, unit, actual, cust, spec)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "2627" in msg or "duplicate" in msg.lower():
                skipped_pk += 1
                outcomes.append({"goodsName": goods, "custName": cust, "hpbh": code,
                                 "status": "已存在(主键冲突)，跳过"})
                continue
            failed.append({"goodsName": goods, "reason": msg[:200], "hpbh": code})
            outcomes.append({"goodsName": goods, "custName": cust, "hpbh": code,
                             "status": "失败：" + msg[:120]})
            if not _alive(cur):   # 触发器 HxRoll 把事务回滚了 → 不能再继续
                raise YxError("YX_IMPORT_FAILED",
                              "导入中断（事务已回滚）：%s" % msg[:200],
                              {"inserted": inserted, "failed": failed})
            continue
        inserted += 1
        outcomes.append({"goodsName": goods, "custName": cust, "hpbh": code, "status": "已导入"})

    if inserted == 0 and skipped_dup == 0 and skipped_pk == 0:
        raise YxError("YX_IMPORT_EMPTY", "这些行都缺少货品编号或客户品名，没法导入",
                      {"failed": failed})
    # 写完之后回一份逐行结果（界面直接复用预检那张表，只是状态变成"已导入/已存在/失败"）
    for row in plan:
        row["status"] = ("已导入" if row["status"] == "可导入" else row["status"])
    return {"inserted": inserted, "skippedDup": skipped_dup, "skippedPk": skipped_pk,
            "failed": failed, "outcomes": outcomes, "lbtypeKh": lbtype,
            "rows": plan, "lbtypes": lbtypes}
