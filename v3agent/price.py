# -*- coding: utf-8 -*-
"""客户取价规则（V3 口径）：
1) 客户 PriceTrace=201（最近销价跟踪）时，先取 Price_Kh 该客户该货品的最近销价；
2) 无最近销价 → 取默认价格（货品/单位档案价格，列由配置指定，默认 D_SJ1）；
3) 仍无价格 → price=0，标记 needs_manual。

★★ 价格方案覆盖（2026-09-22 新增）：
   上面的 PriceTypeXS / PriceTrace 历来只读**客户档案**，所以「同一个客户
   永远只能有一个价格口径」。但实际业务里同一客户不同单子可能要走不同方案
   （例如急单走零售价、批量单走批发价），V3 桌面端也是在单据上选方案的。

   本模块现在允许调用方通过 `price_trace` / `price_type_xs` 参数**覆盖**
   客户档案里的配置；不传则完全按原逻辑（读档案），保证既有调用方零影响。

   ★ XS 表本身**没有**价格方案列（已实测：XS 前 19 列无任何 Price 方案字段），
     所以「本单选了什么方案」**不落库**，只在本单创建/取价这一次生效。
     这不是遗漏，是 V3 数据模型如此 —— 别去 XS 上找这个字段。
"""

from decimal import Decimal

from . import sql as db


ALLOWED_COLUMNS = {
    "D_SJ0",
    "D_SJ1",
    "D_SJ2",
    "D_SJ3",
    "D_SJ4",
    "D_SJ5",
    "D_SJMember",
    "D_XSPrice",
    "D_XSPriceTax",
}

# ★ PriceTypeXS（客户档案 / 单据「价格方案」的值）→ Hpdan/HpdanUnit 价格列。
#   0..5 与 D_SJ0..D_SJ5 是 V3 的固定对应；13=最低销价 对应 D_CXDJ
#   （HpdanUnit 实测存在该列）。
PRICE_TYPE_COLUMN = {
    0: "D_SJ0",        # 零售价
    1: "D_SJ1",        # 批发价
    2: "D_SJ2",        # 优惠价
    3: "D_SJ3",        # 销售价3
    4: "D_SJ4",        # 销售价4
    5: "D_SJ5",        # 销售价5
    13: "D_CXDJ",      # 最低销价
}

# 允许作为价格来源的列全集 = 白名单 ∪ 价格方案映射用到的列。
# ★ D_CXDJ 不在 ALLOWED_COLUMNS 里（它不属于「档案价」语义），
#   但价格方案 13 会用到，所以这里必须放行 —— 两者是不同用途的清单，
#   以前混用导致「选了最低销价」会抛 ValueError。
PRICE_COLUMNS_OK = ALLOWED_COLUMNS | set(PRICE_TYPE_COLUMN.values())


def _dec(v):
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


def _master_hpbh(cur, hpbh):
    """按去除首尾空格的编号定位主档，返回主档规范编号（可能含空格）或 None。"""
    row = db.q1(
        cur,
        "SELECT TOP 1 hpbh FROM Hpdan WHERE LTRIM(RTRIM(hpbh)) = %s",
        (hpbh.strip(),),
    )
    return str(row[0]) if row else None


def resolve_price(cur, cfg, item_bh, hpbh, unit=None,
                  price_trace=None, price_type_xs=None):
    """返回 {price, source, unit, rate, recent, manual}。

    price_trace / price_type_xs：可选覆盖参数（来自单据页面的「价格方案」选择）。
    传了就优先于客户档案里的同名配置；不传（None）则完全按档案来。
    """
    code = hpbh.strip()
    master_hpbh = _master_hpbh(cur, code)
    if master_hpbh is None:
        return {"price": Decimal("0"), "source": "none", "unit": unit,
                "rate": Decimal("1"), "recent": None, "manual": True}
    row = db.q1(
        cur,
        "SELECT ISNULL(PriceTypeXS,0), ISNULL(PriceTrace,0) FROM Khdan WHERE Itembh = %s",
        (item_bh,),
    )
    if not row:
        raise ValueError(f"客户不存在：{item_bh}")
    db_type_xs, db_trace = int(row[0]), int(row[1])

    # ★★ 覆盖：单据页面选的价格方案优先于客户档案。
    #   ★★★ 变量名必须与参数区分开（db_type_xs / db_trace）。
    #   曾经的写法 `price_type_xs, price_trace = int(row[0]), int(row[1])` 直接
    #   把**入参覆盖成库里的值**，于是后面 `if price_type_xs is not None` 永远为真、
    #   但用的全是档案值 —— 表现为「传了方案参数却毫无效果」的静默吞参数。
    #   实测抓到：传 price_type_xs=1 时 SQL 里仍是 D_SJ0（档案值 0）。
    #   这类 bug 不会报错、只会静默算错价，改这里务必保留区分。
    if price_trace is not None:
        db_trace = int(price_trace)
    if price_type_xs is not None:
        db_type_xs = int(price_type_xs)
    price_trace, price_type_xs = db_trace, db_type_xs

    recent = None
    if price_trace == 201:
        # 最近销价：优先同单位，其次任意单位
        if unit:
            recent = db.q1(
                cur,
                """SELECT TOP 1 HpUnitName, D_PriceTax, D_Price, D_PriceZK, LastSaleBillNo, LastSaleEx
                   FROM Price_Kh
                   WHERE Itembh = %s AND LTRIM(RTRIM(Hpbh)) = %s AND HpUnitName = %s
                     AND LTRIM(RTRIM(ISNULL(LastSaleBillNo,''))) <> ''
                   ORDER BY LastSaleEx DESC, Idencol DESC""",
                (item_bh, code, unit),
            )
        if recent is None:
            recent = db.q1(
                cur,
                """SELECT TOP 1 HpUnitName, D_PriceTax, D_Price, D_PriceZK, LastSaleBillNo, LastSaleEx
                   FROM Price_Kh
                   WHERE Itembh = %s AND LTRIM(RTRIM(Hpbh)) = %s
                     AND LTRIM(RTRIM(ISNULL(LastSaleBillNo,''))) <> ''
                   ORDER BY LastSaleEx DESC, Idencol DESC""",
                (item_bh, code),
            )
    if recent is not None:
        field = cfg["price"].get("recent_price_field", "D_PriceTax")
        price = _dec(recent[1] if field == "D_PriceTax" else recent[2])
        if price <= 0:
            price = _dec(recent[2]) or _dec(recent[3])
        if price > 0:
            return {
                "price": price,
                "source": "recent",
                "unit": recent[0],
                "rate": Decimal("1"),
                "recent": {"bill_no": recent[4], "sale_date": str(recent[5])},
                "manual": False,
            }

    # 默认价格：单位档案 → 基本单位档案 → 货品档案
    # ★★ 列的选择（2026-09-22 修正）：
    #   以前这里**硬读配置** `cfg["price"]["default_price_column"]`（本账套写死 D_SJ1），
    #   而上面刚查出来的 `price_type_xs` 读完之后**从未被使用** ——
    #   于是无论客户档案里选的是零售价还是最低销价，取价永远走 D_SJ1（批发价）。
    #   这是一个静默错价：界面显示了方案，取价却无视它。
    #   现在：先按 price_type_xs 映射到对应价格列；映射不到才回落到配置。
    col = PRICE_TYPE_COLUMN.get(int(price_type_xs)) if price_type_xs is not None else None
    if not col:
        col = cfg["price"].get("default_price_column", "D_SJ1")
    if col not in PRICE_COLUMNS_OK:
        raise ValueError(f"非法默认价格列：{col}")
    price, unit_name, rate = Decimal("0"), unit, Decimal("1")
    rows = db.qall(
        cur,
        f"""SELECT u.UnitName, u.UnitRate, u.UnitBasic, u.{col}, h.{col}
            FROM HpdanUnit u JOIN Hpdan h ON h.IdenGuid = u.HpIdenGuid
            WHERE h.Hpbh = %s ORDER BY CASE WHEN u.UnitName = %s THEN 0 ELSE 1 END, u.UnitBasic DESC""",
        (master_hpbh, unit or ""),
    )
    for r in rows:
        p = _dec(r[3] if unit and r[0].strip() == unit.strip() else r[4])
        if p > 0:
            price, unit_name, rate = p, r[0], _dec(r[1])
            break
    if price <= 0:
        row = db.q1(cur, f"SELECT {col} FROM Hpdan WHERE Hpbh = %s", (master_hpbh,))
        if row:
            price = _dec(row[0])
    return {
        "price": price,
        "source": "default" if price > 0 else "none",
        "unit": unit_name or unit,
        "rate": rate,
        "recent": None,
        "manual": price <= 0,
    }
