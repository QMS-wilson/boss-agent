# -*- coding: utf-8 -*-
"""库存：V3「库存状况表」口径的现存量查询 + 「收发存」流水。

★★★ 为什么不直接用视图 VInventoryMx（2026-09-25 读视图定义实测）：
    视图里那一列是官方 bug ——
        j_kcje = SUM(Inventory.s_kcsl)      ← 「库存金额」被写成了**数量**
        /* 真正的算法被注释在旁边：
           HpCostType<>5 → SUM(s_kcsl * hpdan.cost_price)
           HpCostType =5 → SUM(inventory.j_kcje) */
    实测证据：编号 11874 现存 90、成本 0.8，视图返回 j_kcje=90（=数量），
    而 Inventory.J_KCJE=72（=90×0.8，正确金额）。
    ⇒ 维度（品名/规格/单位/仓库名/下限）照视图的 JOIN 结构取，
      **金额一律自己算 SUM(Inventory.J_KCJE)**，成本均价用 AVG(Inventory.Cost_Price)。

★ 口径与视图保持一致（实测 shiwantongda 视图 207 行 = 本模块 207 行）：
    Hpdan INNER JOIN CkType ON 1=1 LEFT JOIN Inventory ON hpbh+ckbh
    WHERE IsNotCalcStock=0 AND HpCalcTypeBase<91 AND (StopUse=0 OR s_kcsl<>0)
  注意 Hpdan 里 48,584/48,586 个货品是 StopUse=1（停用），所以库存页只会出现
  「有库存记录的 + 未停用的」，这不是 bug，是 V3 自己的过滤。

★ 编号脏空格：Inventory 与 Hpdan 两侧都存在带前导空格的编号（各 2 行）。
  先聚合 Inventory（小表）再对 Hpdan 侧做 trim JOIN —— 既不漏品名，
  也不会像「在 Hpdan 主键上套 trim 去 JOIN 大明细表」那样把索引打废
  （那个坑见 reports.py::_product_map 的说明）。
"""

from decimal import Decimal

from . import sql as db

# 单据类型 → 中文名（InvenFlow.BillNaid；本账套实测只出现这两种）
_BILL_NAMES = {1020: "进货单", 2020: "销售出货单"}

_FLAG_SQL = {
    "positive": "ISNULL(t.qty, 0) > 0",
    "negative": "ISNULL(t.qty, 0) < 0",
    "empty": "ISNULL(t.qty, 0) = 0",
    "all": "1=1",
}


def _num(v):
    return float(Decimal(str(v or 0)))


def _where(keyword="", warehouse="", flag="all", product=""):
    """返回 (附加过滤条件列表, 参数)。"""
    conds = [_FLAG_SQL.get(flag, _FLAG_SQL["all"])]
    args = []
    code = str(product or "").strip()
    if code:
        conds.append("LTRIM(RTRIM(h.hpbh)) = %s")
        args.append(code)
    kw = str(keyword or "").strip()
    if kw:
        like = "%" + kw + "%"
        conds.append("(LTRIM(RTRIM(h.hpbh)) LIKE %s OR ISNULL(h.hpmc,'') LIKE %s "
                     "OR ISNULL(h.ggxh,'') LIKE %s OR ISNULL(h.py,'') LIKE %s)")
        args += [like, like, like, like]
    ck = str(warehouse or "").strip()
    if ck:
        conds.append("k.key_ = %s")
        args.append(ck)
    return conds, args


# 主体 FROM：先聚合 Inventory，再挂 Hpdan / CkType
_FROM = """
    FROM Hpdan h WITH (NOLOCK)
    INNER JOIN CkType k WITH (NOLOCK) ON 1 = 1
    LEFT JOIN (
        SELECT Ckbh, LTRIM(RTRIM(Hpbh)) AS hp,
               SUM(ISNULL(S_KCSL, 0))   AS qty,
               SUM(ISNULL(J_KCJE, 0))   AS amount,
               AVG(ISNULL(Cost_Price, 0)) AS cost,
               SUM(ISNULL(S_KCQC, 0))   AS opening,
               SUM(ISNULL(S_KCWay, 0))  AS transit,
               SUM(ISNULL(S_KCLock, 0)) AS locked
        FROM Inventory WITH (NOLOCK)
        GROUP BY Ckbh, LTRIM(RTRIM(Hpbh))
    ) t ON t.hp = LTRIM(RTRIM(h.hpbh)) AND t.Ckbh = k.key_
    WHERE ISNULL(h.IsNotCalcStock, 0) = 0
      AND ISNULL(h.HpCalcTypeBase, 0) < 91
      AND (h.StopUse = 0 OR ISNULL(t.qty, 0) <> 0)
"""


def stock_query(cur, keyword="", warehouse="", flag="all", limit=200, offset=0, product=""):
    """现存量列表 + 同口径合计。

    列就按需求那 6 个：仓库名 / 现存数 / 单位 / 规格 / 成本均价 / 库存金额
    （外加编号、品名、下限值 —— 下限只随数据返回，是否判定由客户端的「库存预警」开关决定）。
    """
    limit = min(max(int(limit or 200), 1), 500)
    offset = max(int(offset or 0), 0)
    conds, args = _where(keyword, warehouse, flag, product)
    where = " AND ".join(conds)

    rows = db.qall(
        cur,
        f"""SELECT TOP {limit + offset}
                   k.key_, ISNULL(k.Text, ''), LTRIM(RTRIM(h.hpbh)), ISNULL(h.hpmc, ''),
                   ISNULL(h.ggxh, ''), ISNULL(h.hpunit, ''),
                   ISNULL(t.qty, 0), ISNULL(t.amount, 0), ISNULL(t.cost, 0),
                   ISNULL(t.opening, 0), ISNULL(t.transit, 0), ISNULL(t.locked, 0),
                   ISNULL(h.kcxx, 0), ISNULL(t.hp, '')
            {_FROM} AND {where}
            ORDER BY ISNULL(t.amount, 0) DESC, LTRIM(RTRIM(h.hpbh))""",
        tuple(args),
    )
    page = rows[offset:offset + limit]

    items = [{
        "warehouseId": r[0], "warehouse": r[1], "productId": r[2], "name": r[3],
        "spec": r[4], "unit": r[5],
        "qty": _num(r[6]), "amount": _num(r[7]), "costPrice": _num(r[8]),
        "opening": _num(r[9]), "transit": _num(r[10]), "locked": _num(r[11]),
        # ★ 下限 = Hpdan.kcxx，上限 = Hpdan.kcsx（2026-09-25 用户确认列名语义）。
        #   两个账套实测 48,586 个货品里 kcxx 与 kcsx **全为 0**（从没录过），
        #   所以这里只如实把数带出去，判定与标色由客户端的「库存预警」开关决定 ——
        #   不能在前端写死「低于 50 算偏低」，那是没有依据的规则。
        "lowLimit": _num(r[12]),
    } for r in page]

    total = db.q1(
        cur,
        f"""SELECT COUNT(*), ISNULL(SUM(t.qty), 0), ISNULL(SUM(t.amount), 0),
                   SUM(CASE WHEN ISNULL(t.qty,0) > 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN ISNULL(t.qty,0) < 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN ISNULL(t.qty,0) = 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN ISNULL(t.cost,0) > 0 THEN 1 ELSE 0 END)
            {_FROM} AND {where}""",
        tuple(args),
    )
    matched = int(total[0] or 0)
    return {
        "keyword": keyword or "", "warehouse": warehouse or "", "flag": flag,
        "items": items,
        "total": {
            "kinds": matched,
            "qty": _num(total[1]),
            "amount": _num(total[2]),
            "inStock": int(total[3] or 0),
            "negative": int(total[4] or 0),
            "zero": int(total[5] or 0),
            "costed": int(total[6] or 0),
        },
        "offset": offset, "limit": limit,
        "truncated": matched > offset + len(items),
    }


def stock_flow(cur, product="", date_from=None, date_to=None, limit=500):
    """收发存：某货品的逐笔出入库与结存（InvenFlow）。

    ★★ 结存必须自己算，不能用 InvenFlow.S_KCSL（2026-09-25 实测）：
      那一列是 V3 记账时的快照，本账套前期根本没维护（出货 8 件仍写 0.00），
      后期才连续 —— 螺丝8*55 末笔 S_KCSL=-5,556，而 Inventory 现存量是 -13,050。
      自己按「窗口前累计 + 逐笔(入−出)」递推，末笔才等于 Inventory 现存量
      （已验证 Σ(S_SLA−S_SLD) == Inventory.S_KCSL）。

    ★ InvenFlow.HPID 存的是 Hpdan.IdenCol（不是编号），BillIden 是单据的 IdenCol，
      所以品名按 IdenCol 关联 Hpdan、单号按 IdenCol 回 XS/JH 取。
    """
    limit = min(max(int(limit or 500), 1), 2000)
    code = str(product or "").strip()
    if not code:
        raise ValueError("缺少货品编号")

    head = db.q1(
        cur,
        """SELECT LTRIM(RTRIM(h.hpbh)), ISNULL(h.hpmc,''), ISNULL(h.ggxh,''), ISNULL(h.hpunit,'')
           FROM Hpdan h WHERE LTRIM(RTRIM(h.hpbh)) = %s""",
        (code,),
    )
    if not head:
        raise ValueError(f"货品 {code} 不存在")

    conds = ["LTRIM(RTRIM(h.hpbh)) = %s"]
    args = [code]
    if date_from:
        conds.append("f.NDate >= %s")
        args.append(date_from)
    if date_to:
        conds.append("f.NDate < DATEADD(day, 1, %s)")
        args.append(date_to)
    where = " AND ".join(conds)

    # 窗口开始前的累计 = 本窗口的期初结存
    pre = ["LTRIM(RTRIM(h.hpbh)) = %s"]
    pre_args = [code]
    if date_from:
        pre.append("f.NDate < %s")
        pre_args.append(date_from)
    opening = _num(db.q1(
        cur,
        f"""SELECT ISNULL(SUM(ISNULL(f.S_SLA,0) - ISNULL(f.S_SLD,0)), 0)
            FROM InvenFlow f WITH (NOLOCK) JOIN Hpdan h WITH (NOLOCK) ON h.IdenCol = f.HPID
            WHERE {' AND '.join(pre)}""",
        tuple(pre_args))[0])

    # 多取一行：只有真取到 limit+1 行才算截断（避免「正好等于 limit」误报）
    rows = db.qall(
        cur,
        f"""SELECT TOP {limit + 1}
                   CONVERT(varchar(10), f.NDate, 120), f.BillNaid, ISNULL(f.S_SLA, 0), ISNULL(f.S_SLD, 0),
                   ISNULL(f.D_Cost_Price, 0), ISNULL(f.J_JEA, 0), ISNULL(f.J_JED, 0),
                   ISNULL(f.BillIden, 0),
                   (SELECT TOP 1 x.BillNo FROM XS x WHERE x.IdenCol = f.BillIden AND f.BillNaid = 2020),
                   (SELECT TOP 1 j.BillNo FROM JH j WHERE j.IdenCol = f.BillIden AND f.BillNaid = 1020)
            FROM InvenFlow f WITH (NOLOCK)
            JOIN Hpdan h WITH (NOLOCK) ON h.IdenCol = f.HPID
            WHERE {where}
            ORDER BY f.NDate, f.IdenCol""",
        tuple(args),
    )
    truncated = len(rows) > limit
    rows = rows[:limit]

    # ★★ 合计与期末必须**先于截断**算。旧写法是在 rows[:limit] 上累加 in_sum/out_sum，
    #   流水超过上限时 summary.closing 其实是"前 500 笔之后"的中间余额 ——
    #   它恰好等于最后一行的 balance，而前端那句「期末结存不等于最后一行的结存」
    #   正好把事情说反了，人还会拿这个"期末"当现存量用。
    agg = db.q1(
        cur,
        f"""SELECT ISNULL(SUM(ISNULL(f.S_SLA,0)),0), ISNULL(SUM(ISNULL(f.S_SLD,0)),0)
            FROM InvenFlow f WITH (NOLOCK) JOIN Hpdan h WITH (NOLOCK) ON h.IdenCol = f.HPID
            WHERE {where}""",
        tuple(args))
    in_total, out_total = (_num(agg[0]), _num(agg[1])) if agg else (0.0, 0.0)

    items = []
    run = opening
    for r in rows:
        in_qty, out_qty = _num(r[2]), _num(r[3])
        run += in_qty - out_qty
        kind = int(r[1] or 0)
        items.append({
            "date": r[0],
            "billKind": kind,
            "typeName": _BILL_NAMES.get(kind, f"单据类型{kind}"),
            "billNo": r[8] or r[9] or "",
            "inQty": in_qty,
            "outQty": out_qty,
            "balance": round(run, 4),
            "costPrice": _num(r[4]),
            "inAmount": _num(r[5]),
            "outAmount": _num(r[6]),
        })
    return {
        "product": {"id": head[0], "name": head[1], "spec": head[2], "unit": head[3]},
        "period": {"startDate": date_from, "endDate": date_to},
        "items": items,
        "summary": {
            "rows": len(items),
            "opening": round(opening, 4),
            "inQty": round(in_total, 4),
            "outQty": round(out_total, 4),
            "netQty": round(in_total - out_total, 4),
            "closing": round(opening + in_total - out_total, 4),
        },
        "truncated": truncated,
        "rowLimit": limit,
    }
