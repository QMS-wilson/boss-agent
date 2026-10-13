# -*- coding: utf-8 -*-
"""连接 SQL Server 2008 R2，读取 hpdan 表的货品资料。"""
from __future__ import annotations

from .core import norm_text


def _connect(cfg: dict):
    import pymssql

    kwargs = {
        "server": cfg.get("server", "").strip(),
        "port": int(cfg.get("port", 1433) or 1433),
        "database": cfg.get("database", "").strip(),
        "login_timeout": 8,
        # 老版本 SQL Server（2005/2008/2008R2）不认新版 TDS 协议，
        # 强制 7.0 保证握手成功；较新数据库也兼容。
        "tds_version": str(cfg.get("tds_version", "7.0")),
    }
    if cfg.get("windows_auth"):
        # 不传 user/password 即使用 Windows 集成认证
        # （注意：跨电脑、非域环境通常连不上，建议用 SQL 账号）
        pass
    else:
        kwargs["user"] = cfg.get("user", "").strip()
        kwargs["password"] = cfg.get("password", "")
    return pymssql.connect(**kwargs)


def _quote(name: str) -> str:
    return f"[{name.strip().strip('[]')}]"


def fetch_items(cfg: dict) -> list[dict]:
    """抓取 HPKhType 中 (hpmckh, ggxhkh, hpbhkh, hpbh, hpunitname)，返回物料列表。"""
    table = _quote(cfg.get("table", "HPKhType"))
    f_match = _quote(cfg.get("field_match", "hpmckh"))
    f_spec = _quote(cfg.get("field_spec", "GgxhKh"))
    f_spec2 = _quote(cfg.get("field_spec2", "HpbhKh"))
    f_code = _quote(cfg.get("field_code", "hpbh"))
    f_unit = _quote(cfg.get("field_unit", "HpUnitName"))
    sql = f"SELECT {f_match} AS m, {f_spec} AS a, {f_spec2} AS b, {f_code} AS c, {f_unit} AS u FROM {table}"

    rows: list[dict] = []
    conn = _connect(cfg)
    try:
        cur = conn.cursor(as_dict=True)
        cur.execute(sql)
        for r in cur.fetchall():
            m = (r.get("m") or "").strip()
            a = (r.get("a") or "").strip()
            b = (r.get("b") or "").strip()
            code = (r.get("c") or "").strip()
            unit = (r.get("u") or "").strip()
            if not m:
                continue
            rows.append(
                {
                    "code": code,
                    "unit": unit,
                    "name": m,
                    "spec_a": a,
                    "spec_b": b,
                    "name_n": norm_text(m),
                    "spec_a_n": norm_text(a),
                    "spec_b_n": norm_text(b),
                    "mat_n": "",
                    "key_a": norm_text(m) + norm_text(a),
                    "key_b": norm_text(m) + norm_text(b),
                }
            )
    finally:
        conn.close()
    return rows


def _table_columns(conn, table: str) -> dict[str, str]:
    """返回 {小写列名: 实际列名}。"""
    name = table.strip("[]").split(".")[-1]
    cur = conn.cursor()
    cur.execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=%s",
        (name,),
    )
    return {r[0].lower(): r[0] for r in cur.fetchall()}


def fetch_hpdan_sys_map(full_cfg: dict) -> tuple[dict[str, dict], list[str]]:
    """从 hpdan 取系统产品名/规格：{hpbh: {sys_name, sys_spec}}。"""
    conn_cfg = full_cfg.get("sqlserver", {})
    hp = full_cfg.get("hpdan", {})
    table = hp.get("table", "hpdan")
    f_name = hp.get("name_field", "hpmc")
    f_code = hp.get("code_field", "hpbh")
    f_spec = hp.get("spec_field", "Ggxh")

    conn = _connect(conn_cfg)
    try:
        cols = _table_columns(conn, table)
        missing: list[str] = []
        for f in (f_name, f_code, f_spec):
            if f.lower() not in cols:
                missing.append(f)
        if missing:
            return {}, missing
        actual = {k: cols[k.lower()] for k in (f_name, f_code, f_spec)}
        # 拼音码是**可选列**：有就一起取（搜 `lsbt` 靠它），某个客户的库没这列也不许把
        # 整份映射废掉 —— 那会让系统品名一起消失，比少一个搜索入口严重得多。
        f_py = hp.get("py_field", "Py")
        py_col = cols.get(f_py.lower())
        sql = (
            f"SELECT [{actual[f_code]}] AS c, [{actual[f_name]}] AS n, [{actual[f_spec]}] AS s"
            + (f", [{py_col}] AS p" if py_col else ",'' AS p")
            + f" FROM [{table.strip('[]').split('.')[-1]}]"
        )
        cur = conn.cursor(as_dict=True)
        cur.execute(sql)
        mapping: dict[str, dict] = {}
        for r in cur.fetchall():
            code = (r.get("c") or "").strip()
            if not code:
                continue
            mapping[code] = {
                "sys_name": (r.get("n") or "").strip(),
                "sys_spec": (r.get("s") or "").strip(),
                "sys_py": (r.get("p") or "").strip(),
            }
        return mapping, []
    finally:
        conn.close()


def test_connection(cfg: dict) -> tuple[bool, str]:
    # 兼容两种调用方式：直接传 sqlserver 配置，或传 {"sqlserver": {...}}
    if isinstance(cfg.get("sqlserver"), dict):
        cfg = cfg["sqlserver"]
    host = cfg.get("server", "").strip() or "(空)"
    port = cfg.get("port", 1433)
    try:
        rows = fetch_items(cfg)
        return True, f"连接成功，读取 {len(rows)} 条货品记录"
    except Exception as exc:
        return False, f"服务器 {host}:{port} 连接失败：\n{exc}"


def fetch_hpdan_lookup(full_cfg: dict) -> dict[str, dict]:
    """读取 hpdan（货品名称→hpbh/hpunit），返回 {归一化货品名称: {code, unit}}。"""
    conn_cfg = full_cfg.get("sqlserver", {})
    hp = full_cfg.get("hpdan", {})
    table = _quote(hp.get("table", "hpdan"))
    f_name = _quote(hp.get("name_field", "hpmc"))
    f_code = _quote(hp.get("code_field", "hpbh"))
    f_unit = _quote(hp.get("unit_field", "hpunit"))
    sql = f"SELECT {f_name} AS n, {f_code} AS c, {f_unit} AS u FROM {table}"

    lookup: dict[str, dict] = {}
    conn = _connect(conn_cfg)
    try:
        cur = conn.cursor(as_dict=True)
        cur.execute(sql)
        for r in cur.fetchall():
            n = (r.get("n") or "").strip()
            if not n:
                continue
            key = norm_text(n)
            if key not in lookup:
                lookup[key] = {
                    "code": (r.get("c") or "").strip(),
                    "unit": (r.get("u") or "").strip(),
                }
    finally:
        conn.close()
    return lookup


def insert_customer_items(full_cfg: dict, items: list[dict], lbtype_kh: str = "21") -> dict:
    """向 HPKhType 批量插入客户品名对照。

    items: [{goods_name, cust_name, spec, actual, code, unit}, ...]
    返回 {"inserted": n, "skipped_dup": n, "skipped_pk": n, "failed": [(货品名称, 原因), ...], "outcomes": [...]}
    """
    conn_cfg = full_cfg.get("sqlserver", {})
    table = _quote(conn_cfg.get("table", "HPKhType"))
    lbtype = str(lbtype_kh or "21")

    conn = _connect(conn_cfg)
    try:
        cur = conn.cursor()
        inserted = 0
        skipped_dup = 0
        skipped_pk = 0
        failed: list[tuple[str, str]] = []
        outcomes: list[dict] = []
        for it in items:
            code = (it.get("code") or "").strip()
            unit = (it.get("unit") or "").strip()
            cust = (it.get("cust_name") or "").strip()
            spec = (it.get("spec") or "").strip()
            actual = (it.get("actual") or "").strip()
            goods = it.get("goods_name") or ""
            if not code or not cust:
                failed.append((goods, "缺少货品编号或客户品名"))
                outcomes.append({**it, "status": "失败：缺少货品编号或客户品名"})
                continue
            # 重复检查：客户品名+采购规格+实际尺寸 完全一致则跳过
            cur.execute(
                f"SELECT COUNT(*) FROM {table} "
                "WHERE LTRIM(RTRIM([HpmcKh])) = LTRIM(RTRIM(%s)) "
                "AND LTRIM(RTRIM([GgxhKh])) = LTRIM(RTRIM(%s)) "
                "AND LTRIM(RTRIM([HpbhKh])) = LTRIM(RTRIM(%s))",
                (cust, spec, actual),
            )
            if cur.fetchone()[0] > 0:
                skipped_dup += 1
                outcomes.append({**it, "status": "已存在，跳过"})
                continue
            try:
                cur.execute(
                    f"INSERT INTO {table} "
                    "([LbType_Kh],[Hpbh],[HpUnitName],[HpbhKh],[HpmcKh],[GgxhKh],[BarcodeKh],[D_SJ],[TimeUpdate],[IdenGuid]) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,GETDATE(),NEWID())",
                    (lbtype, code, unit, actual, cust, spec, "", 0.0),
                )
            except Exception as exc:
                msg = str(exc)
                if "2627" in msg or "duplicate" in msg.lower():
                    skipped_pk += 1
                    outcomes.append({**it, "status": "已存在(主键冲突)，跳过"})
                    continue
                raise
            inserted += 1
            outcomes.append({**it, "status": "已导入"})
        conn.commit()
        return {
            "inserted": inserted,
            "skipped_dup": skipped_dup,
            "skipped_pk": skipped_pk,
            "failed": failed,
            "outcomes": outcomes,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 游标版：给 Agent 的 op 用（复用 dispatch 已开好的连接/事务，不自己连库）
#
# 桌面版（上面的 fetch_*）自己 connect/close，因为它是独立程序；
# op 跑在 dispatch 里，连接、事务、账套路由都是 Agent 的，所以这里只收游标。
# ★ 注意：dispatch 给的游标是**元组游标**（con.cursor() 默认 as_dict=False），
#   所以下面一律按位置取值，不能像桌面版那样用列名。
# ---------------------------------------------------------------------------

def fetch_items_cur(cur, full_cfg: dict) -> list[dict]:
    """同 fetch_items，但使用调用方给的游标。"""
    cfg = full_cfg.get("sqlserver", {})
    table = _quote(cfg.get("table", "HPKhType"))
    f_match = _quote(cfg.get("field_match", "hpmckh"))
    f_spec = _quote(cfg.get("field_spec", "GgxhKh"))
    f_spec2 = _quote(cfg.get("field_spec2", "HpbhKh"))
    f_code = _quote(cfg.get("field_code", "hpbh"))
    f_unit = _quote(cfg.get("field_unit", "HpUnitName"))
    cur.execute(f"SELECT {f_match}, {f_spec}, {f_spec2}, {f_code}, {f_unit} FROM {table}")
    rows: list[dict] = []
    for m, a, b, code, unit in cur.fetchall():
        m = (m or "").strip()
        if not m:
            continue
        a = (a or "").strip()
        b = (b or "").strip()
        rows.append({
            "code": (code or "").strip(), "unit": (unit or "").strip(),
            "name": m, "spec_a": a, "spec_b": b,
            "name_n": norm_text(m), "spec_a_n": norm_text(a), "spec_b_n": norm_text(b),
            "mat_n": "", "key_a": norm_text(m) + norm_text(a), "key_b": norm_text(m) + norm_text(b),
        })
    return rows


def fetch_hpdan_sys_map_cur(cur, full_cfg: dict) -> tuple[dict[str, dict], list[str]]:
    """同 fetch_hpdan_sys_map，但使用调用方给的游标；返回 (映射, 缺字段列表)。"""
    hp = full_cfg.get("hpdan", {})
    table = str(hp.get("table", "hpdan")).strip("[]").split(".")[-1]
    f_name = hp.get("name_field", "hpmc")
    f_code = hp.get("code_field", "hpbh")
    f_spec = hp.get("spec_field", "Ggxh")
    cur.execute("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=%s", (table,))
    have = {str(r[0]).lower(): str(r[0]) for r in cur.fetchall()}
    missing = [f for f in (f_name, f_code, f_spec) if f.lower() not in have]
    if missing:
        return {}, missing
    py_col = have.get(str(hp.get("py_field", "Py")).lower())
    cur.execute(f"SELECT [{have[f_code.lower()]}], [{have[f_name.lower()]}], [{have[f_spec.lower()]}],"
                + (f" [{py_col}]" if py_col else " ''")
                + f" FROM [{table}]")
    mapping: dict[str, dict] = {}
    for code, name, spec, py in cur.fetchall():
        code = (code or "").strip()
        if not code:
            continue
        mapping[code] = {"sys_name": (name or "").strip(), "sys_spec": (spec or "").strip(),
                         "sys_py": (py or "").strip()}
    return mapping, []


def fetch_hpdan_lookup_cur(cur, full_cfg: dict) -> dict[str, dict]:
    """同 fetch_hpdan_lookup，但使用调用方给的游标：{归一化货品名称: {code, unit}}。"""
    hp = full_cfg.get("hpdan", {})
    table = _quote(hp.get("table", "hpdan"))
    f_name = _quote(hp.get("name_field", "hpmc"))
    f_code = _quote(hp.get("code_field", "hpbh"))
    f_unit = _quote(hp.get("unit_field", "hpunit"))
    cur.execute(f"SELECT {f_name}, {f_code}, {f_unit} FROM {table}")
    lookup: dict[str, dict] = {}
    for n, c, u in cur.fetchall():
        n = (n or "").strip()
        if not n:
            continue
        key = norm_text(n)
        if key not in lookup:
            lookup[key] = {"code": (c or "").strip(), "unit": (u or "").strip()}
    return lookup


def count_rows_cur(cur, table: str) -> int:
    """表行数（表名走白名单校验，不接受外部任意串）。"""
    name = str(table or "").strip().strip("[]").split(".")[-1]
    if not name.replace("_", "").isalnum():
        return -1
    cur.execute(f"SELECT COUNT(*) FROM [{name}]")
    row = cur.fetchone()
    return int(row[0] or 0)


def lbtype_exists_cur(cur, lbtype_kh: str) -> bool:
    """客户类别编号是否在 KHType 里（触发器 TRI_A_HPKhType 会校验它）。"""
    cur.execute("SELECT COUNT(*) FROM KHType WHERE RTRIM(Key_) = RTRIM(%s)", (str(lbtype_kh or ""),))
    return int(cur.fetchone()[0] or 0) > 0


def hpdan_ex_exists_cur(cur, hpbh: str, unit: str) -> bool:
    """货品+计量单位是否在 HpdanEx 里（触发器另一道校验）。"""
    cur.execute(
        "SELECT COUNT(*) FROM HpdanEx WHERE RTRIM(Hpbh) = RTRIM(%s) AND RTRIM(UnitName) = RTRIM(%s)",
        (str(hpbh or ""), str(unit or "")),
    )
    return int(cur.fetchone()[0] or 0) > 0


def hpkh_exists_cur(cur, full_cfg: dict, lbtype_kh: str, hpbh: str, unit: str) -> bool:
    """主键判重：LbType_Kh + Hpbh + HpUnitName（比原工具多一个单位维度）。"""
    table = _quote(full_cfg.get("sqlserver", {}).get("table", "HPKhType"))
    cur.execute(
        f"SELECT COUNT(*) FROM {table} WHERE RTRIM(LbType_Kh)=RTRIM(%s) "
        "AND RTRIM(Hpbh)=RTRIM(%s) AND RTRIM(HpUnitName)=RTRIM(%s)",
        (str(lbtype_kh or ""), str(hpbh or ""), str(unit or "")),
    )
    return int(cur.fetchone()[0] or 0) > 0


def insert_hpkh_cur(cur, full_cfg: dict, lbtype_kh: str, code: str, unit: str,
                    actual: str, cust: str, spec: str) -> None:
    """插一行客户品名对照。

    ★ 必须**逐行 INSERT**：TRI_A_HPKhType 开头就是 `if @@ROWCOUNT <> 1 ... return`，
      一次插多行触发器直接跳过（Price_Kh 就不会同步）——原桌面工具也是逐行插的。
    ★ 触发器会校验 KHType（客户类别）与 HpdanEx（货品+单位），不满足会 HxRoll 回滚，
      错误以 SQL 异常抛回，由调用方逐行记账。
    """
    table = _quote(full_cfg.get("sqlserver", {}).get("table", "HPKhType"))
    cur.execute(
        f"INSERT INTO {table} "
        "([LbType_Kh],[Hpbh],[HpUnitName],[HpbhKh],[HpmcKh],[GgxhKh],[BarcodeKh],[D_SJ],[TimeUpdate],[IdenGuid]) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,GETDATE(),NEWID())",
        (str(lbtype_kh or ""), code, unit, actual, cust, spec, "", 0.0),
    )
