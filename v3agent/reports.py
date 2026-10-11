# -*- coding: utf-8 -*-
"""经营报表 op：销售汇总/趋势、客户与商品排行、应收与账龄、客户货品明细、毛利。"""

from datetime import datetime as _dt, timedelta

from decimal import Decimal

from . import master
from . import sql as db


def _num(v):
    return float(Decimal(str(v or 0)))


def _day(s):
    """'2026-09-01' / datetime → date；无效返回 None。"""
    if hasattr(s, "date"):
        return s.date()
    try:
        return _dt.strptime(str(s or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _prev_span(date_from, date_to):
    """等长上一周期（紧邻 date_from 之前）。任一端无效 → (None, None)。"""
    d1, d2 = _day(date_from), _day(date_to)
    if not d1 or not d2 or d2 < d1:
        return None, None
    end = d1 - timedelta(days=1)
    return (end - timedelta(days=(d2 - d1).days)).isoformat(), end.isoformat()


def _gaps(date_from, date_to, active_days, min_run=3, keep=3):
    """期间内的出货断档：连续无单日 >= min_run 天。

    ★ 为什么要它：本库实测「本月 −81.3%」全部来自 09/06-09/18 的 13 天空档，
      不标注断档就报环比，等于让老板把「没开单」读成「业绩下滑」。
    ★ 右端按今天截断：期间含未来日期时，未来那几天不是「断档」。
    """
    d1, d2 = _day(date_from), _day(date_to)
    if not d1 or not d2:
        return []
    today = _dt.now().date()
    if d2 > today:
        d2 = today
    have = {d for d in (_day(x) for x in active_days) if d}
    runs, cur_run = [], None
    d = d1
    while d <= d2:
        if d in have:
            if cur_run and (cur_run[1] - cur_run[0]).days + 1 >= min_run:
                runs.append(cur_run)
            cur_run = None
        else:
            cur_run = cur_run or [d, d]
            cur_run[1] = d
        d += timedelta(days=1)
    if cur_run and (cur_run[1] - cur_run[0]).days + 1 >= min_run:
        runs.append(cur_run)
    out = [{"from": a.isoformat(), "to": b.isoformat(), "days": (b - a).days + 1} for a, b in runs]
    out.sort(key=lambda r: -r["days"])
    return out[:keep]



def _range(cur, date_from=None, date_to=None, customer=None, alias="x"):
    where = [f"{alias}.BillState=0", f"{alias}.ISOK=1"]
    args = []
    if date_from:
        where.append(f"{alias}.NDate >= %s")
        args.append(date_from)
    if date_to:
        where.append(f"{alias}.NDate < DATEADD(day, 1, %s)")
        args.append(date_to)
    if customer:
        where.append(f"{alias}.ItemBh = %s")
        args.append(str(customer).strip())
    return " AND ".join(where), tuple(args)


def _product_map(cur, hps):
    """按货品编号批量取「品名/规格」：返回 {trim 后的 hpbh: (hpmc, ggxh)}。

    ★★★ 为什么不用 `LEFT JOIN Hpdan h ON h.hpbh = t.hp`（2026-09-23 修复）：
      那条 JOIN 的前提是「Hpdan 的脏行（hpbh 带前导空格）不参与任何销售」。
      实测该前提**已被打破**：
        · Hpdan 里 `螺丝（杯头）` 的 hpbh = ' 1123454552'（len=11，10 位 + 1 空格）
        · Hpdan 里 `防水胶圈`   的 hpbh = ' 12454514'（len=9，8 位 + 1 空格）
      这两个货品在**有效销售明细**（BillState=0 AND ISOK=1）里各有 4 / 2 行
      （单号 XK00036668~36672）。聚合子查询产出的是 `LTRIM(RTRIM(m.Hpbh))`
      （已 trim，无空格），拿它去 `= h.hpbh`（带空格）必然不匹配 → LEFT JOIN 落 NULL
      → `ISNULL(h.hpmc,'')` ⇒ **品名整列空白**（这正是「螺丝（杯头）/防水胶圈品名不显示」）。
      而反向修法「JOIN 条件里给 Hpdan 侧套 trim」会毁掉 PK_Hpdan 聚集索引
      （48584 行全扫 + 排序，P50 1.7s 暴涨到 48~89s，饿死 Agent 心跳）——绝不能走。

    ⇒ 正确形态：聚合查询**完全不 JOIN Hpdan**，只产出 trim 后的编号；
      再用本函数以 `IN + trim` **一次性**取回名称（只对 ≤limit 个编号取，
      Hpdan 48584 行单表扫一次，代价可控），脏行 100% 命中。
      这样既保住聚合查询的性能（无大表 JOIN），又修掉品名空白。

    ★ 分批 200：避免 IN 列表过长撑爆 SQL 长度 / 参数上限。
    """
    keys = [str(k).strip() for k in hps if str(k or "").strip()]
    if not keys:
        return {}
    keys = list(dict.fromkeys(keys))  # 去重保序
    out = {}
    for i in range(0, len(keys), 200):
        chunk = keys[i:i + 200]
        marks = ",".join(["%s"] * len(chunk))
        for r in db.qall(
            cur,
            f"""SELECT LTRIM(RTRIM(hpbh)), ISNULL(hpmc,''), ISNULL(ggxh,'')
                FROM Hpdan WHERE LTRIM(RTRIM(hpbh)) IN ({marks})""",
            tuple(chunk),
        ):
            out[r[0]] = (r[1], r[2])
    return out


def _name_of(pmap, hp):
    """从 _product_map 结果取 (品名, 规格)，缺省为空串。"""
    return pmap.get(hp) or ("", "")


_SUM_SQL = """SELECT COUNT(*), COALESCE(SUM(x.J_JEWF),0), COALESCE(AVG(x.J_JEWF),0),
                    COALESCE(SUM(x.S_SLSUM),0), COUNT(DISTINCT x.ItemBh),
                    COUNT(DISTINCT CONVERT(varchar(10), x.NDate, 120))
             FROM XS x WHERE {where}"""


def _sales_agg(cur, where, args):
    row = db.q1(cur, _SUM_SQL.format(where=where), args)
    return {
        "salesCount": int(row[0] or 0),
        "salesAmount": _num(row[1]),
        "averageBillAmount": round(_num(row[2]), 2),
        "salesQty": _num(row[3]),
        "customerCount": int(row[4] or 0),
        "activeDays": int(row[5] or 0),
    }


def sales_summary(cur, date_from=None, date_to=None, customer=None):
    where, args = _range(cur, date_from, date_to, customer)
    out = {"dateFrom": date_from, "dateTo": date_to, "customer": customer or ""}
    out.update(_sales_agg(cur, where, args))
    # ★ 销售数量口径：用单头 `XS.S_SLSUM`，不 JOIN XSMX 明细。
    #   本库逐单核对 36610 张单 0 张不一致（SUM(XS.S_SLSUM)=SUM(XSMX.S_SL)=944366.36），
    #   且省掉一次明细表 JOIN。核对语句见 scripts/verify-report-qty.py。
    days = [r[0] for r in db.qall(
        cur,
        f"SELECT DISTINCT CONVERT(varchar(10), x.NDate, 120) FROM XS x WHERE {where} ORDER BY 1",
        args)]
    out["gaps"] = _gaps(date_from, date_to, days)
    p1, p2 = _prev_span(date_from, date_to)
    if p1:
        pw, pa = _range(cur, p1, p2, customer)
        prev = _sales_agg(cur, pw, pa)
        prev.update({"dateFrom": p1, "dateTo": p2})
        out["previous"] = prev
    else:
        out["previous"] = None
    return out



def sales_trend(cur, date_from=None, date_to=None, granularity="day", customer=None):
    gran = "month" if str(granularity).lower().startswith("m") else "day"
    key = "CONVERT(varchar(7), x.NDate, 120)" if gran == "month" else "CONVERT(varchar(10), x.NDate, 120)"
    where, args = _range(cur, date_from, date_to, customer)
    rows = db.qall(
        cur,
        f"""SELECT {key} AS k, COUNT(*), COALESCE(SUM(x.J_JEWF),0)
            FROM XS x WHERE {where}
            GROUP BY {key} ORDER BY {key}""",
        args,
    )
    return {
        "granularity": gran,
        "items": [{"date": r[0], "count": int(r[1] or 0), "amount": _num(r[2])} for r in rows],
    }


def customer_ranking(cur, date_from=None, date_to=None, limit=20):
    limit = min(int(limit or 20), 100)
    where, args = _range(cur, date_from, date_to)
    rows = db.qall(
        cur,
        f"""SELECT TOP {limit} x.ItemBh, ISNULL(k.Itemmc,''), COUNT(*), COALESCE(SUM(x.J_JEWF),0)
            FROM XS x LEFT JOIN Khdan k ON k.Itembh = x.ItemBh
            WHERE {where}
            GROUP BY x.ItemBh, k.Itemmc
            ORDER BY COALESCE(SUM(x.J_JEWF),0) DESC""",
        args,
    )
    return [
        {"customerId": r[0], "customerName": r[1], "salesCount": int(r[2] or 0), "salesAmount": _num(r[3])}
        for r in rows
    ]


def product_ranking(cur, date_from=None, date_to=None, limit=20):
    """货品销售排行（按金额倒序）。

    ★★★ 性能红线（2026-09-22 实测，务必看）：
       原写法把 trim 放在 JOIN 条件里
         `LEFT JOIN Hpdan h ON LTRIM(RTRIM(h.hpbh)) = LTRIM(RTRIM(m.Hpbh))`
       而 Hpdan 的主键 `PK_Hpdan` 正是 **CLUSTERED (hpbh)** ——
       一旦列上套了 `LTRIM(RTRIM())`，**索引彻底不可用**，执行计划退化为
       `Clustered Index Scan ×2 + Sort ×4 + Hash Match`（全表扫 48586 行 + 排序）。
       真库（tongda, 期间明细 95959 行）实测：
         ① 现状（trim 在 JOIN 里）        1650~1710 ms
         ② **先聚合再 JOIN（本写法）**     652 ms
         ③ 不 trim                       1142 ms
         ④ 不要 Hpdan（无名称）            639 ms
       更严重的是**内存授予不足/排序溢出 tempdb 时会暴涨到 48~89 s**（实测到 89s）——
       足以饿死 Agent 的 WebSocket 心跳，导致「Agent 假死 + 前端 502」。

    ⇒ 正确形态：**先把明细聚合到「每货品一行」，再拿这个小结果集去 JOIN 主表**。
      此时 trim 只作用在 ≤limit 行上，代价可忽略。

    ★★ 第三轮优化（2026-09-22 v2）：**JOIN 条件里 Hpdan 侧不再套 trim**。
      实测（tongda）：
        · 双边 trim：P50=643ms，计划 `Clustered Index Scan×3 + Hash Match×3`（⊕ 主表仍全扫）
        · 单边 trim：P50=514ms，计划 `Clustered Index Scan×2 + **Clustered Index Seek×1**
          + Nested Loops + Hash Match×2`  ⇒ **-20% 且终于走 seek**
      语义等价性已实测验证：三种写法在 tongda 上产出**逐字节相同**的前 20 行。

    ★★★ 2026-09-23 订正（此前的「安全性依据」已失效，务必看）：
      旧记录写「Hpdan 侧需 trim 的 2 行（' 1123454552' / ' 12454514'）**从未出现在
      任何有效销售明细里**（各 0 行）」——**该前提已被实测推翻**：
        · ' 1123454552'（品名「螺丝（杯头）」）：有效明细 4 行 / 4 单
        · ' 12454514'（品名「防水胶圈」）    ：有效明细 2 行 / 2 单
        （单号 XK00036668~36672，BillState=0 AND ISOK=1；旧记录应是当时这批单还没产生）
      ⇒ `LEFT JOIN Hpdan h ON h.hpbh = t.hp`（t.hp 已 trim）对这 2 个货品 **必然落 NULL**
        ⇒ 品名空白。用户报的「客户货品明细里螺丝（杯头）/防水胶圈品名不显示」即此。
      ⇒ 现改为**聚合查询不 JOIN Hpdan**，品名/规格另用 `_product_map`（IN + trim）取，
        既避免 JOIN 套 trim 毁索引，又对脏行 100% 正确。原来的
        `OUTER APPLY` 备选方案不再需要。
    """
    limit = min(int(limit or 20), 100)
    where, args = _range(cur, date_from, date_to)
    # ★ 2026-09-23：聚合查询不再 JOIN Hpdan（原 `LEFT JOIN Hpdan h ON h.hpbh = t.hp`
    #   对「脏行 + 有销售」的货品会漏品名，详见 _product_map docstring）。
    rows = db.qall(
        cur,
        f"""SELECT TOP {limit} t.hp, t.q, t.a
            FROM (
                SELECT LTRIM(RTRIM(m.Hpbh)) AS hp,
                       COALESCE(SUM(m.S_SL),0) AS q,
                       COALESCE(SUM(m.J_JEZK),0) AS a
                FROM XSMX m
                JOIN XS x ON x.IdenGuid = m.BillGuid
                WHERE {where}
                GROUP BY LTRIM(RTRIM(m.Hpbh))
            ) t
            ORDER BY t.a DESC""",
        args,
    )
    pmap = _product_map(cur, [r[0] for r in rows])
    out = []
    for r in rows:
        name, spec = _name_of(pmap, r[0])
        out.append({"productId": r[0], "productName": name, "spec": spec,
                    "quantity": _num(r[1]), "salesAmount": _num(r[2])})
    return out


_AGING_BUCKETS = (
    ("0-30", "30天内", 0, 30),
    ("31-60", "31-60天", 31, 60),
    ("61-90", "61-90天", 61, 90),
    ("90+", "90天以上", 91, 10 ** 9),
)


def _aging_key(days):
    if days is None:
        return "unknown"
    for key, _label, lo, hi in _AGING_BUCKETS:
        if lo <= days <= hi:
            return key
    return "unknown"


def _last_ship(cur):
    """每个客户「最后一张有效出货单」→ {编号: (日期, 距今天数)}。

    ★ 一次 GROUP BY 扫描，不在 Khdan 查询里做 OUTER APPLY/JOIN：
      客户档案与销售单头各自一次遍历，比 126 次相关子查询稳。
    """
    today = _dt.now().date()
    out = {}
    for bh, last in db.qall(
        cur, "SELECT ItemBh, MAX(NDate) FROM XS WHERE BillState=0 AND ISOK=1 GROUP BY ItemBh"
    ):
        d = _day(last)
        if d:
            out[str(bh or "").strip()] = (d.isoformat(), (today - d).days)
    return out


def receivables_summary(cur, keyword="", only_receivable=True, limit=50, aging="",
                        show_stopped=False):
    """应收账款汇总 + 账龄分段。

    ★ '---' 是 V3 的「已删除的客户」汇总行（本库挂 45,298.14），不是真客户，
      必须排除；否则账龄分段合计对不上应收总额。
    ★ 账龄口径 = 客户最后一张有效出货单距今天数。当前账套无收款流水
      （XSSK/KHXSSK 均 0 行），无法按单据核销算「逾期」，所以只叫账龄。
    ★ 分段与合计基于**全量**客户，不受 keyword / limit / aging 过滤影响。

    ★★ 2026-10-06 拍板：**合计与账龄算全量，暂停往来的客户照算**（用户原话口径：
       钱不会因为停用就消失）。现网实测差别不小 —— 3 家暂停往来的客户挂着 ¥1,823.38，
       旧写法（`StopUse=0`）把它从首页应收合计里整个抹掉，而客户排行、对账单、
       应收账款页里那位客户又都还在，账就对不上。
       ⇒ `StopUse=0` 只用于**列出来的那部分客户**（默认不把暂停往来的塞进列表，
          要看得靠 `show_stopped=1`），绝不参与合计/账龄分段。
    """
    limit = min(int(limit or 50), 300)
    agg_base = ["ItemBh <> '---'"]
    if only_receivable:
        agg_base.append("J_Due > 0")
    base = list(agg_base)
    if not show_stopped:
        base.append("StopUse=0")
    where, args = list(base), []
    kw = str(keyword or "").strip()
    if kw:
        like = "%" + kw + "%"
        where.append("(ItemBh LIKE %s OR Itemmc LIKE %s OR ISNULL(Py,'') LIKE %s)")
        args += [like, like, like]

    last = _last_ship(cur)

    total = Decimal("0")
    count = 0
    buckets = {k: {"key": k, "label": lb, "customers": 0, "amount": 0.0} for k, lb, _a, _b in _AGING_BUCKETS}
    unknown = {"key": "unknown", "label": "无出货记录", "customers": 0, "amount": 0.0}
    # 暂停往来那部分单独记一份：合计含它们，但默认列表不含 ⇒ 界面要能说出"少列出来的是哪些钱"
    hidden = {"count": 0, "amount": 0.0}
    for bh, due, stopped in db.qall(
            cur, "SELECT ItemBh, J_Due, ISNULL(StopUse,0) FROM Khdan WHERE %s" % ' AND '.join(agg_base)):
        amt = Decimal(str(due or 0))
        total += amt
        count += 1
        key = _aging_key(last.get(str(bh or "").strip(), (None, None))[1])
        slot = buckets.get(key) or unknown
        slot["customers"] += 1
        slot["amount"] = round(slot["amount"] + _num(amt), 2)
        if stopped:
            hidden["count"] += 1
            hidden["amount"] = round(hidden["amount"] + _num(amt), 2)

    rows = db.qall(
        cur,
        f"""SELECT ItemBh, Itemmc, ISNULL(Py,''), J_Due, J_Advance, ISNULL(StopUse,0)
            FROM Khdan WHERE {' AND '.join(where)} ORDER BY J_Due DESC""",
        tuple(args),
        cap=1000,
    )
    items = []
    for r in rows:
        code = str(r[0] or "").strip()
        ld, days = last.get(code, ("", None))
        key = _aging_key(days)
        if aging and key != aging:
            continue
        items.append({
            "customerId": code, "customerName": r[1], "py": r[2],
            "receivable": _num(r[3]), "advance": _num(r[4]),
            "lastShipDate": ld, "daysSince": days, "agingKey": key,
            # ★ 开着「显示已停用」翻出来的人要知道这几行为什么平时看不见
            "stopped": bool(r[5]), "statusName": "暂停往来" if r[5] else "正常往来",
        })
    full_hits = len(items)
    truncated = full_hits > limit
    if truncated:
        items = items[:limit]

    aging_list = [v for v in buckets.values() if v["customers"]]
    if unknown["customers"]:
        aging_list.append(unknown)
    return {
        "totalReceivable": _num(total),
        "customerCount": count,
        "aging": aging_list,
        "agingTotal": round(sum(b["amount"] for b in aging_list), 2),
        "agingNote": "账龄按客户最后一张有效出货单距今天数分段；当前账套无收款流水，无法按单据核销计算逾期",
        "items": items,
        "matchedCount": full_hits,
        "truncated": truncated,
        "filteredBy": aging or "",
        # ★ 合计含暂停往来的客户，而默认列表不含 ⇒ 把差额如实给出去，
        #   界面才能说"另有 N 家暂停往来 ¥X，已计入合计"（不这么做的化，
        #   页面合计永远比列出来的加起来大，看着像算错了）。
        "stoppedHidden": None if show_stopped else (hidden if hidden["count"] else None),
        "showStopped": bool(show_stopped),
    }



def customer_product_detail(cur, customer, date_from=None, date_to=None, limit=200):
    """某客户的货品明细。

    ★ Hpbh 两侧都可能带前导空格（shiwantongda 实测 XSMX 11/176 行，
      Hpdan 里同款编号也带空格）。不 trim 的三个后果：
        ① 同一货品按空格拆成多组；② JOIN 语义依赖「两边同样脏」；
        ③ 输出带空格编号 → 详情页 `WHERE LTRIM(RTRIM(h.hpbh))=%s` 匹配不上 → 详情空白。

    ★★★ 但 trim **绝不能放在 JOIN 条件里**（2026-09-22 性能实测）：
      `LEFT JOIN Hpdan h ON LTRIM(RTRIM(h.hpbh)) = LTRIM(RTRIM(m.Hpbh))`
      会让 Hpdan 的主键聚集索引（PK_Hpdan = CLUSTERED hpbh）彻底失效
      → 全表扫描 + 排序，真库上稳态 1.7s、排序溢出时暴涨到 48~89s
      （足以饿死 Agent 心跳）。
      ⇒ 改为**先聚合再 JOIN**：trim 只作用在聚合后的 ≤limit 行上，代价可忽略。
      语义与旧写法一致（旧写法 GROUP BY 也按 trim 后的值分组）。

    ★★ 第三轮优化曾把 JOIN 写成 `h.hpbh = t.hp`（Hpdan 侧不 trim，为走 Clustered Index Seek）。
      但 2026-09-23 实测发现该写法对「Hpdan 脏行 + 确有销售」的货品会漏品名
      （「螺丝（杯头）」「防水胶圈」，详见 _product_map 与 product_ranking 的订正说明）。
      ⇒ 现改为**聚合不 JOIN + `_product_map`（IN + trim）单独取名称**，
        兼顾性能与脏行正确性。
    """
    limit = min(int(limit or 200), 500)
    cust = master.customer_row(cur, customer)
    where, args = _range(cur, date_from, date_to, customer)
    _grouped = f"""SELECT LTRIM(RTRIM(m.Hpbh)) AS hp, ISNULL(m.HpUnitName,'') AS unit,
                          COALESCE(SUM(m.S_SL),0) AS q,
                          COALESCE(SUM(m.J_JEZK),0) AS a,
                          COUNT(DISTINCT x.BillNo) AS bc
                   FROM XSMX m
                   JOIN XS x ON x.IdenGuid = m.BillGuid
                   WHERE {where}
                   GROUP BY LTRIM(RTRIM(m.Hpbh)), m.HpUnitName"""
    # ★ 全量合计单独算：明细被 limit 截断时，合计仍是真值。
    #   （旧写法把「已取回的行」加起来当合计 → 大客户 + 大期间会静默偏小。）
    agg = db.q1(cur, f"SELECT COUNT(*), COALESCE(SUM(t.q),0), COALESCE(SUM(t.a),0) FROM ({_grouped}) t", args)
    total_kinds = int(agg[0] or 0)
    rows = db.qall(cur, f"SELECT TOP {limit} t.hp, t.unit, t.q, t.a, t.bc FROM ({_grouped}) t ORDER BY t.a DESC", args)
    # ★ 2026-09-23 修复「品名不显示」：品名/规格不靠 JOIN Hpdan 取（详见 _product_map）。
    pmap = _product_map(cur, [r[0] for r in rows])
    items = []
    for r in rows:
        qty, amt = Decimal(str(r[2] or 0)), Decimal(str(r[3] or 0))
        name, spec = _name_of(pmap, r[0])
        items.append({
            "productId": r[0], "productName": name, "spec": spec, "unit": r[1],
            "quantity": _num(qty), "amount": _num(amt), "billCount": int(r[4] or 0),
            "averagePrice": _num((amt / qty).quantize(Decimal("0.0001"))) if qty else 0,
        })
    return {
        "customer": {"id": cust["code"], "name": cust["name"]},
        "period": {"startDate": date_from, "endDate": date_to},
        "summary": {
            "productCount": total_kinds,
            "totalQuantity": _num(agg[1]),
            "totalAmount": _num(agg[2]),
        },
        "items": items,
        "truncated": total_kinds > len(items),
        "rowLimit": limit,
    }



def customer_statement(cur, customer, date_from=None, date_to=None, limit=1000,
                       with_lines=False, line_limit=3000):
    """客户往来对账单。

    当前账套 XSSK/KHXSSK/WasteBook 均无收款流水，因此：
    - 期末应收 = Khdan.J_Due（V3 账面未结；正=客户欠我方，负=我方欠货）
    - 收款项 = 0
    - 期初应收 = 期末应收 - 本期出货 + 本期收款
    - 明细为期间销售出货流水（借：应收增加）

    ★ limit（2026-09-22 新增）：明细行上限，默认 1000。
      改造前此处**无任何上限**，大客户 + 大日期范围会把整段流水全拉进内存
      （这是「偶发崩溃」的内存峰值来源之一）。现用 `qall(cap=...)` 分批取，
      取满即停，并在返回体里用 `truncated` 明确告知前端（不悄悄丢数据）。
      ★ 2026-10-04 纠正：`summary` 的金额**必须**用 SQL 聚合算。旧代码虽然这么写着，
        实际是在**被 cap 截断的明细行上逐行累加** sales，再由
        `opening = closing - sales + payment` 倒推期初 ⇒ 明细一超上限，本期出货偏小、
        期初被推高同样的差额，四个数与流水行的余额互相对不上，
        而前端还挂着"借贷合计仍按全量计算"的说明。现在两边都说真话。

    ★ with_lines（2026-10-08 新增，默认 False ⇒ 返回体与改造前**逐字节一致**）：
      「月份对账单」那张版式是**一行一个货品**，而这里的 items 是**一行一张单**，
      所以导出时按同一条件再取一层 XSMX 明细。只在调用点名要的时候取 ——
      这一层比单头多一个数量级的行（现网 tongda 一本 95972 行明细 / 3 万余张单），
      页面每次查对账都背上是白给的开销。
    """
    limit = min(int(limit or 1000), 5000)
    cust = master.customer_row(cur, customer)
    closing = Decimal(str(cust.get("due") or 0))
    where, args = _range(cur, date_from, date_to, customer)
    one = db.q1(cur, f"SELECT COALESCE(SUM(COALESCE(x.J_JEWF,0)),0) FROM XS x WHERE {where}", args)
    sales = Decimal(str((one[0] if one else 0) or 0))
    rows = db.qall(
        cur,
        f"""SELECT x.BillNo, x.NDate, COALESCE(x.J_JEWF,0)
            FROM XS x WHERE {where} ORDER BY x.NDate, x.BillNo""",
        args,
        cap=limit + 1,      # ★ 多取一行：`len(rows) >= limit` 在"正好这么多行"时会误报截断
    )
    truncated = len(rows) > limit
    rows = rows[:limit]
    items = []
    for r in rows:
        amt = Decimal(str(r[2] or 0))
        items.append({
            "date": r[1].strftime("%Y-%m-%d") if hasattr(r[1], "strftime") else str(r[1])[:10],
            "type": "SALE", "typeName": "销售出货", "billId": r[0], "billNo": r[0],
            "description": "销售出货", "debit": _num(amt), "credit": 0, "balance": 0,
        })
    payment = Decimal("0")
    opening = closing - sales + payment
    running = opening
    for it in items:
        running += Decimal(str(it["debit"])) - Decimal(str(it["credit"]))
        it["balance"] = _num(running)
    out = {
        "customer": {"id": cust["code"], "name": cust["name"]},
        "period": {"startDate": date_from, "endDate": date_to},
        "summary": {
            "openingBalance": _num(opening),
            "salesAmount": _num(sales),
            "paymentAmount": _num(payment),
            "otherReceivable": 0,
            "otherPayable": 0,
            "closingBalance": _num(closing),
        },
        "items": items,
        "truncated": truncated,
        "rowLimit": limit,
        "note": "账套当前无收款流水；期初应收 = 期末应收 − 本期出货 + 本期收款（倒推）",
    }
    if not with_lines:
        return out
    line_limit = min(int(line_limit or 3000), 20000)
    by_bill, got, lines_cut = _statement_lines(cur, where, args, line_limit)
    for it in items:
        it["lines"] = by_bill.get(it["billNo"]) or []
    # ★ 截断口径要说两遍：`truncated` 管**单**（流水行），`linesTruncated` 管**货品行**。
    #   只报前者的话，导出表里"合计比客户手里的少"会查不到原因。
    out["linesTruncated"] = lines_cut
    out["lineLimit"] = line_limit
    out["lineCount"] = got
    return out


def _statement_lines(cur, where, args, line_limit):
    """对账单的**逐行货品**明细（"月份对账单"版式要的那一层，厂商那张是一行一个货品）。

    ★ 数量取 **S_SLUnit**（开单时那个单位的数量），不是 S_SL（基本数量）。
      实测厂商那张对账单印的是 S_SL，于是多单位行会出「180 × 60 ≠ 900」
      （180 个、单价 60 是**每件**、900 = 15 件 × 60）。2026-10-08 定：本版改取
      开单单位数量，让「数量 × 单价 = 应收金额」在同一行里成立。
      两本账实测（shiwantongda 249 行 / tongda 95972 行）**没有**「S_SLUnit 为 0
      而 S_SL 有值」的明细行 ⇒ 不写回落分支（写了也抓不到，纯属猜）。

    ★ 金额取 `J_JEZK`（折后金额），与单头那四个 SUM 同一个口径
      （实测 Σ明细 J_JEZK == XS.J_JEZKSUM，逐单对拍见 scripts/）。
      赠品行这列本来就是 0，而版式里赠品行的应收金额印成**空格**而不是 0。

    ★ 单价取 `D_DJ`。有折扣的行会「数量 × 单价 > 应收金额」—— 这是厂商同一版的画法
      （版式里没有折扣列），要改就得动版式，不在这次范围内。

    ★ 品名不靠 JOIN Hpdan（同 customer_product_detail 里那条性能教训：
      JOIN 条件里套 trim 会毁掉 PK_Hpdan 聚集索引，实测 1.7s → 48~89s），
      先取回明细、再用 `_product_map` 按 trim 后的编号一次性批量补名称。
    """
    rows = db.qall(
        cur,
        f"""SELECT x.BillNo, m.IdenCol, LTRIM(RTRIM(m.Hpbh)),
                   COALESCE(m.S_SLUnit,0), COALESCE(m.D_DJ,0), COALESCE(m.J_JEZK,0),
                   ISNULL(m.Gifts,0), ISNULL(m.BZ_XSMX,'')
            FROM XSMX m JOIN XS x ON x.IdenGuid = m.BillGuid
            WHERE {where} ORDER BY x.NDate, x.BillNo, m.IdenCol""",
        args,
        cap=line_limit + 1,     # 多取一行：正好这么多行时不能误报截断
    )
    truncated = len(rows) > line_limit
    rows = rows[:line_limit]
    pmap = _product_map(cur, [r[2] for r in rows])
    by_bill = {}
    for r in rows:
        name, spec = _name_of(pmap, r[2])
        by_bill.setdefault(r[0], []).append({
            "lineNo": int(r[1] or 0),
            "productId": r[2],
            "productName": name,
            "spec": spec,
            "quantity": _num(r[3]),
            "price": _num(r[4]),
            "amount": _num(r[5]),
            "gift": bool(r[6]),
            "remark": str(r[7] or "").strip(),
        })
    return by_bill, len(rows), truncated


# ---------------------------------------------------------------- 只读诊断
#
# ★ 用途：定位「大数据量下报表接口崩溃」。同样一条 SQL：
#     源码环境 1.5~2.2s 正常；exe（PyInstaller）47s 后进程消失且无 traceback。
#   本 op 把查询梯度在 **Agent 进程内部**逐档执行，回包写日志，
#   从而区分「SQL/pymssql 问题」还是「冻结环境问题」。
#   ★ 只读，不写任何数据；正式发布前应移除或保留为诊断入口（不影响业务）。

_DIAG_CASES = [
    ("s1_XS_count", "SELECT COUNT(*) FROM XS WHERE BillState=0 AND ISOK=1"),
    ("s2_XSMX_count", "SELECT COUNT(*) FROM XSMX"),
    ("s3_XSMX_all", "SELECT Hpbh, S_SL, J_JEZK FROM XSMX"),
    ("s4_join_count",
     "SELECT COUNT(*) FROM XSMX m JOIN XS x ON x.IdenGuid = m.BillGuid "
     "WHERE x.BillState=0 AND x.ISOK=1"),
    ("s5_join_rows",
     "SELECT m.Hpbh, m.S_SL, m.J_JEZK FROM XSMX m JOIN XS x ON x.IdenGuid = m.BillGuid "
     "WHERE x.BillState=0 AND x.ISOK=1"),
    ("s6_trim_join_count",
     "SELECT COUNT(*) FROM XSMX m JOIN XS x ON x.IdenGuid = m.BillGuid "
     "LEFT JOIN Hpdan h ON LTRIM(RTRIM(h.hpbh)) = LTRIM(RTRIM(m.Hpbh)) "
     "WHERE x.BillState=0 AND x.ISOK=1"),
    ("s7_full_group",
     "SELECT TOP 20 t.hp, ISNULL(h.hpmc,''), ISNULL(h.ggxh,''), t.q, t.a "
     "FROM (SELECT LTRIM(RTRIM(m.Hpbh)) AS hp, COALESCE(SUM(m.S_SL),0) AS q, "
     "COALESCE(SUM(m.J_JEZK),0) AS a FROM XSMX m "
     "JOIN XS x ON x.IdenGuid = m.BillGuid "
     "WHERE x.BillState=0 AND x.ISOK=1 "
     "GROUP BY LTRIM(RTRIM(m.Hpbh))) t "
     "LEFT JOIN Hpdan h ON LTRIM(RTRIM(h.hpbh)) = t.hp "
     "ORDER BY t.a DESC"),
    ("s8_statement_rows",
     "SELECT x.BillNo, x.NDate, COALESCE(x.J_JEWF,0) FROM XS x "
     "WHERE x.BillState=0 AND x.ISOK=1 ORDER BY x.NDate, x.BillNo"),
    ("s9_xsmx_join_billno",
     "SELECT TOP 20 m.Hpbh, m.S_SL, COALESCE(m.J_JEZK,0) FROM XSMX m "
     "JOIN XS x ON x.IdenGuid = m.BillGuid "
     "WHERE x.BillState=0 AND x.ISOK=1 ORDER BY m.S_SL DESC"),
]


def diag_gradient(cur, only=None):
    """逐档执行诊断 SQL，回填每档耗时/行数/异常。只读。"""
    import time as _t
    out = []
    for name, sql in _DIAG_CASES:
        if only and name not in (only if isinstance(only, (list, tuple)) else [only]):
            continue
        t0 = _t.time()
        item = {"case": name}
        try:
            cur.execute(sql)
            rows = cur.fetchall()
            item.update({"ok": True, "ms": round((_t.time() - t0) * 1000), "rows": len(rows)})
        except Exception as exc:  # noqa: BLE001
            item.update({"ok": False, "ms": round((_t.time() - t0) * 1000),
                         "err": type(exc).__name__ + ": " + str(exc)[:200]})
        out.append(item)
    return {"db": getattr(cur, "_dbname", ""), "results": out}


# ---------------------------------------------------------------- 采购(进货)报表
#
# ★ 业绩口径与销售侧 reports 对齐：排除草稿/作废（采购 IsOk=0 即草稿或作废），
#   只统计正式单（IsOk=1）。采购没有 BillState / 红冲概念，故只判 IsOk。
# ★ 金额字段用 JH.J_JEWF（折后进货总额，与销售 XS.J_JEWF 同源同义）。


def _purchase_range(date_from=None, date_to=None, supplier=None):
    where = ["j.IsOk=1"]
    args = []
    if date_from:
        where.append("j.NDate >= %s")
        args.append(date_from)
    if date_to:
        where.append("j.NDate < DATEADD(day, 1, %s)")
        args.append(date_to)
    if supplier:
        where.append("j.ItemBh = %s")
        args.append(str(supplier).strip())
    return " AND ".join(where), tuple(args)


def purchase_summary(cur, date_from=None, date_to=None, supplier=None):
    where, args = _purchase_range(date_from, date_to, supplier)
    row = db.q1(
        cur,
        f"""SELECT COUNT(*), COALESCE(SUM(j.J_JEWF),0), COALESCE(SUM(j.S_SLSUM),0),
                   COUNT(DISTINCT j.ItemBh)
            FROM JH j WHERE {where}""",
        args,
    )
    return {
        "dateFrom": date_from, "dateTo": date_to, "supplier": supplier or "",
        "purchaseCount": int(row[0] or 0),
        "purchaseAmount": _num(row[1]),
        "purchaseQty": _num(row[2]),
        "supplierCount": int(row[3] or 0),
    }


def purchase_trend(cur, date_from=None, date_to=None, granularity="day"):
    gran = "month" if str(granularity).lower().startswith("m") else "day"
    key = "CONVERT(varchar(7), j.NDate, 120)" if gran == "month" else "CONVERT(varchar(10), j.NDate, 120)"
    where, args = _purchase_range(date_from, date_to)
    rows = db.qall(
        cur,
        f"""SELECT {key} AS k, COUNT(*), COALESCE(SUM(j.J_JEWF),0)
            FROM JH j WHERE {where}
            GROUP BY {key} ORDER BY {key}""",
        args,
    )
    return {
        "granularity": gran,
        "items": [{"date": r[0], "count": int(r[1] or 0), "amount": _num(r[2])} for r in rows],
    }


def supplier_ranking(cur, date_from=None, date_to=None, limit=20):
    limit = min(int(limit or 20), 100)
    where, args = _purchase_range(date_from, date_to)
    rows = db.qall(
        cur,
        f"""SELECT TOP {limit} j.ItemBh, ISNULL(g.Itemmc,''), COUNT(*), COALESCE(SUM(j.J_JEWF),0)
            FROM JH j LEFT JOIN Gysdan g ON g.Itembh = j.ItemBh
            WHERE {where}
            GROUP BY j.ItemBh, g.Itemmc
            ORDER BY COALESCE(SUM(j.J_JEWF),0) DESC""",
        args,
    )
    return [
        {"supplierId": r[0], "supplierName": r[1],
         "purchaseCount": int(r[2] or 0), "purchaseAmount": _num(r[3])}
        for r in rows
    ]


# ---------------------------------------------------------------- 毛利
#
# ★ 口径（估算，非财务精算）：毛利 = 期间销售折后额 - 期间销售成本。
#   成本 = Σ(销售明细每行数量绝对值 × 该商品加权平均采购价)。
#   加权平均采购价 = JHMX 该商品 SUM(J_JEZK) / SUM(S_SL)。
#   ★ 无采购记录的商品成本按 0 计（会高估毛利），已用 note 标注。
#
# ★★★ 性能（2026-09-22）：两个派生表 JOIN 时，
#   `ON LTRIM(RTRIM(m.Hpbh)) = p.hp` 里的 m 侧表达式同样让 XSMX 无法走索引。
#   改为「先把 XSMX 侧按货品聚合（同时把 trim 做掉），再与均价表 JOIN」——
#   参与 trim JOIN 的行数从「全部期间明细」降到「期间内的货品数」。


# 有采购均价的货品占比低于此值 ⇒ 毛利不可信，前端应隐藏而不是显示虚高数字。
_PROFIT_MIN_COVERAGE = 0.6


def profit_summary(cur, date_from=None, date_to=None):
    where, args = _range(cur, date_from, date_to)
    row = db.q1(
        cur,
        f"SELECT COALESCE(SUM(x.J_JEWF),0) FROM XS x WHERE {where}",
        args,
    )
    sales = Decimal(str(row[0] or 0))
    # ★ 先按 trim 后的货品聚合出「每货品数量」，再 JOIN 加权均价表（性能见文件头说明）。
    #   同一次扫描顺带取「有多少货品能取到均价」——这就是毛利的可信度分母。
    agg = db.q1(
        cur,
        f"""SELECT COALESCE(SUM(t.qty * ISNULL(p.avg_price, 0)), 0),
                   COUNT(*), SUM(CASE WHEN p.hp IS NULL OR p.avg_price IS NULL THEN 1 ELSE 0 END)
            FROM (
                SELECT LTRIM(RTRIM(m.Hpbh)) AS hp,
                       SUM(ABS(m.S_SL)) AS qty
                FROM XSMX m
                JOIN XS x ON x.IdenGuid = m.BillGuid
                WHERE {where}
                GROUP BY LTRIM(RTRIM(m.Hpbh))
            ) t
            LEFT JOIN (
                SELECT LTRIM(RTRIM(Hpbh)) AS hp,
                       CASE WHEN SUM(S_SL) <> 0 THEN SUM(J_JEZK)/SUM(S_SL) ELSE 0 END AS avg_price
                FROM JHMX GROUP BY LTRIM(RTRIM(Hpbh))
            ) p ON p.hp = t.hp""",
        args,
    )
    cost = Decimal(str(agg[0] or 0))
    sold_products = int(agg[1] or 0)
    unpriced = int(agg[2] or 0)
    costed_products = sold_products - unpriced
    coverage = round(costed_products / sold_products, 4) if sold_products else 0.0
    # ★ 其余三项成本来源（用于页面向客户解释「为什么不出毛利」）
    det = db.q1(
        cur,
        f"""SELECT COUNT(*), SUM(CASE WHEN ISNULL(m.J_JECost,0)<>0 THEN 1 ELSE 0 END),
                   COALESCE(SUM(ISNULL(m.J_JECost,0)),0)
            FROM XSMX m JOIN XS x ON x.IdenGuid = m.BillGuid WHERE {where}""",
        args,
    )
    mst = db.q1(cur, """SELECT COUNT(*),
                               SUM(CASE WHEN ISNULL(Cost_Price,0)>0 THEN 1 ELSE 0 END),
                               SUM(CASE WHEN ISNULL(D_JHPrice,0)>0 THEN 1 ELSE 0 END)
                        FROM Hpdan""")
    jh = db.q1(cur, "SELECT COUNT(*), COALESCE(SUM(J_JEWF),0) FROM JH WHERE IsOk=1")
    profit = sales - cost
    margin = (profit / sales).quantize(Decimal("0.0001")) if sales else Decimal("0")
    available = sold_products > 0 and coverage >= _PROFIT_MIN_COVERAGE
    sources = {
        "purchaseAvgPrice": {"costedProducts": costed_products, "soldProducts": sold_products,
                             "coverage": coverage},
        "detailCost": {"rows": int(det[0] or 0), "costedRows": int(det[1] or 0),
                       "amount": _num(det[2])},
        "masterCostPrice": {"products": int(mst[1] or 0), "total": int(mst[0] or 0)},
        "purchaseBills": {"count": int(jh[0] or 0), "amount": _num(jh[1])},
    }
    return {
        "dateFrom": date_from, "dateTo": date_to,
        "salesAmount": _num(sales),
        "costAmount": _num(cost),
        "grossProfit": _num(profit),
        "grossMargin": _num(margin),
        "available": available,
        "costCoverage": coverage,
        "costedProducts": costed_products,
        "soldProducts": sold_products,
        "sources": sources,
        "note": ("成本按各商品加权平均采购价估算，无采购记录的商品成本按 0 计"
                 if available else
                 f"仅 {costed_products}/{sold_products} 个在售货品有采购均价，"
                 f"档案成本价 {int(mst[1] or 0)}/{int(mst[0] or 0)}，"
                 f"进货单 {int(jh[0] or 0)} 张 —— 成本来源不足，毛利不可核算"),
    }

