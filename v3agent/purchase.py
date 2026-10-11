# -*- coding: utf-8 -*-
"""采购(进货)模块：进货单 JH / JHMX、供应商 Gysdan 的查询。

★★ 表名映射（2026-09-20 直连 shiwantongda 库实测确认，别再猜）：
    JH      —— 进货单主表（单号 JH00000001…，不是前端 mock 里的 CG 开头）
    JHMX    —— 进货单明细（BillGuid → JH.idenguid）
    Gysdan  —— 供应商档案（Itembh / Itemmc / StopUse）
    GysType —— 供应商分类（key_ / parent / text）
    CkType  —— 仓库（Key_ / Text；JH.Ckbh='101-' → Text='主仓库'）
    Price_Gys —— 供应商价格历史（D_Price = 最近进货价，LastStockBillNo）

★ 为什么没有 CG* 表：本库 452 张表里搜 'cg' 一个都没有，采购就是 JH。
  V3 的命名是拼音首字母：JH=进货、XS=销售、Khdan=客户单、Hpdan=货品单。

★★ 分页/参数校验一律复用 master.py 的实现，不要在这里重写：
   `_like_clause` 承载着一个非常阴的 `%` 格式化坑（见 master.py 注释），
   复制一份出来迟早会漂移。直接 import 私有名是有意为之。

★ 状态口径（2026-09-20 用户纠正 + 本库实证；实测 JH 共 19 张：
  17 张 IsOk=1、3 张 IsOk=0 且 UserVoid 全空 → 全为草稿、零作废）：
    IsOk=1            → pending 正式单，未审核
    IsOk=0 无作废痕迹 → pending 草稿单，未审核
    IsOk=0 且 UserVoid/TimeVoid 有值 → void 已作废
    IsAuditing=1      → audited 已审核
  ★★ `IsOk` **不是**「作废位」！旧的「IsOk=0 → void」写法把草稿误报成作废。
     作废的唯一判据是 UserVoid(作废人) / TimeVoid(作废时间)。
  ★ 前端原先写的是「已入库/待确认」——那是 mock 编的，
    与 V3 的真实状态位对不上，已改为上面这套。
"""

from datetime import datetime
from decimal import Decimal
import uuid

from . import actor
from . import idem
from . import product as product_mod
from . import sql as db
from .master import _like_clause, _page_args, _paged, contact_status_name, stopped_bit

# 制单人/经手人：这条请求是谁就是谁（没注入身份则回落 SYSDEV）
_who = actor.current

# ---------------------------------------------------------------- 写入口径常量
# ★★ 与 sales.py 完全同源，改一处必须两处一起改（口径必须一致）：
#    - 表头时间一律 `YYYY-MM-DD HH:mm:ss`（不带毫秒），**不要用 GETDATE()**；
#    - 制单人写**这条请求的操作员**（云端从令牌注入，见 v3agent/actor.py），
#      没有 V3 操作员身份时回落 `SYSDEV`；历史上这里写死 'SYSTEM'（JH 全部 16 张、
#      XS 存量 62 张都是它），所以手机开的单看不出是谁开的。
#    ★ 供应商建档/修改走的是同一个身份（Gysdan 的 UserInsert/UserUpdate）——
#      都是"这条请求谁动的"，不另立一套口径。
TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _now_str():
    return datetime.now().strftime(TIME_FMT)


def _dec(v):
    """写库路径用严格取数（与 sales._dec 同源），坏值直接归 0 而不是抛。"""
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


def _num(v):
    """宽容取数：任何转不成数字的值都退化成 0，绝不抛异常。

    ★ 为什么不写成 master._num 那样严格：
      这是**只读列表面**。某一行某个字段类型意外（或将来 V3 改列类型），
      不该让整张单、整个列表 500。宁可显示 0 让用户看得见单据，
      也不要一个字段把页面打空。
      （对比：写库路径必须严格，那里用 master._num。）
    """
    if v is None or v == "":
        return 0.0
    try:
        return float(Decimal(str(v)))
    except (ArithmeticError, ValueError, TypeError):
        return 0.0


def _fmt(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, Decimal):
        return float(v)
    return v


def _void_flag(user_void, time_void):
    """真作废判定：UserVoid 有值 或 TimeVoid 非空（与 sales._void_flag 同源）。"""
    return bool((user_void or "").strip()) or (time_void is not None)


def _status(is_auditing, user_void, time_void):
    """把 V3 的状态位收敛成一个前端可用的枚举。

    ★★★ 2026-09-20 纠正（用户口径 + 本库实证）：
        原来判的是 `if not is_ok: return "void"` —— 那是错的。
        ISOK(采购表里叫 **IsOk**) 不是「作废位」：
            IsOk=1 → 正式单，未审核
            IsOk=0 → 草稿单，未审核
            作废   → IsOk 置 0，并写 UserVoid(作废人) + TimeVoid(作废时间)
        即草稿与作废**都是 IsOk=0**，只能靠 UserVoid/TimeVoid 区分。
        本库实测（JH 共 19 张）：17 张 IsOk=1、3 张 IsOk=0 且全是空 UserVoid
        = 全为草稿、**零作废**。旧口径会把这 3 张草稿报成「已作废」。

    ★ 顺序：先判作废，再判审核。作废单不该出现在已审核 tab。
    """
    if _void_flag(user_void, time_void):
        return "void"
    return "audited" if is_auditing else "pending"


# ---------------------------------------------------------------- 进货单


def _header_sql():
    # ★ 末尾这三列（优惠金额 / 预付 / 预付抵扣）是 2026-10-08 补的：
    #   `TRI_A_JH` 自己会判 `ABS(J_JEHSSUM) < ABS(J_JEFK)+ABS(J_JELosSUM)` 与
    #   `Deleted.J_JEFK<>0 OR J_JELOSSUM<>0`，判不过就 `HxRoll`。
    #   以前这里只取 J_JEFK，于是"挂着优惠金额的单"在页面上显示可改、提交才被库打回。
    #   新列一律加在 SELECT 末尾 —— 上面那串 r[n] 是按下标取的，插中间会整体错位。
    return """SELECT j.idenguid, j.idencol, j.BillNo, j.Useno, j.NDate,
                     j.ItemBh, ISNULL(g.Itemmc,''), j.Ckbh, ISNULL(ck.Text,''),
                     j.JHType, j.J_JESUM, j.J_JEHSSUM, j.S_SLSUM,
                     j.IsAuditing, j.IsCheckUp, j.IsOk, ISNULL(j.BZ_JH,''),
                     j.Fkfs, j.TaxType, ISNULL(j.TaxRate,0),
                     ISNULL(j.AssessorBh,''), j.AuditingTime,
                     ISNULL(j.UserInsert,''), j.TimeInsert, j.PrintCount,
                     ISNULL(j.ZK,0), ISNULL(j.J_FkJePay,0), ISNULL(j.J_JEFK,0),
                     ISNULL(j.UserVoid,''), j.TimeVoid,
                     ISNULL(j.J_JELosSUM,0), ISNULL(j.J_FkAdvance,0), ISNULL(j.J_FkAdvanceDec,0)
              FROM JH j
              LEFT JOIN Gysdan g ON g.Itembh = j.ItemBh
              LEFT JOIN CkType ck ON ck.Key_ = j.Ckbh
              WHERE j.BillNo = %s"""


def _load_header(cur, bill_no):
    r = db.q1(cur, _header_sql(), (str(bill_no).strip(),))
    if not r:
        return None
    return {
        "guid": str(r[0]), "idencol": r[1], "bill_no": r[2], "use_no": r[3],
        "ndate": r[4], "supplier_code": r[5], "supplier_name": r[6],
        "ckbh": r[7], "ck_name": r[8], "jh_type": r[9],
        "amount": r[10], "amount_hs": r[11], "qty": r[12],
        "is_auditing": r[13], "is_check_up": r[14], "is_ok": r[15],
        "note": r[16], "fkfs": r[17], "tax_type": r[18], "tax_rate": r[19],
        "assessor": r[20], "auditing_time": r[21],
        "user_insert": r[22], "time_insert": r[23], "print_count": r[24],
        "zk": r[25], "fk_pay": r[26], "fk_total": r[27],
        "user_void": r[28] or "", "time_void": _fmt(r[29]),
        "los_sum": r[30], "fk_advance": r[31], "fk_advance_dec": r[32],
    }


def _lines_sql():
    # ★ 末尾这七列是 2026-10-08 补的，为了 `editable_reasons` 能真读明细。
    #   `TRI_A_JHMX` 删行那半段判的是 `Deleted.S_THSL<>0` 与 `JHTHMX.LinkGuid`，
    #   插行那半段判 `JHOrderNo` 是否还在 `JHOrder` 里 —— 这几列以前根本没取，
    #   所以预检只能看单头，"这行已经被退过货"这种事一定要到提交才炸。
    #   同样只加在 SELECT 末尾：`_map_line` 是按 r[n] 下标取的。
    return """SELECT m.IDENGUID, m.IDENCOL, m.Hpbh, ISNULL(h.hpmc,''), ISNULL(h.ggxh,''),
                     m.HpUnitName, m.HpUnitRate, m.S_SL, m.S_SLPack, m.S_SLPart,
                     m.D_DJ, m.D_DJZK, m.J_JE, m.J_JEZK, m.J_JEHS,
                     ISNULL(m.Scpc,''), ISNULL(m.BZ_JHMX,''), ISNULL(m.Gifts,0),
                     ISNULL(m.D_JHPrice,0), m.LatelyStock,
                     ISNULL(m.Ckbh,''), m.BZDate,
                     ISNULL(m.S_THSL,0), ISNULL(m.S_THSLUnit,0),
                     ISNULL(m.JHOrderNo,''), m.JHOrderID, m.LinkBillImportMx,
                     ISNULL(m.ISOk,0), ISNULL(m.Scpc2,'')
              FROM JHMX m
              LEFT JOIN Hpdan h ON h.hpbh = m.Hpbh
              WHERE m.BillGuid = CONVERT(uniqueidentifier, %s)
              ORDER BY m.IDENCOL, m.IDENGUID"""


def _map_line(r):
    return {
        "iden_guid": str(r[0]), "index": r[1], "hpbh": r[2],
        "name": r[3], "spec": r[4], "unit": r[5],
        "unit_rate": _num(r[6]),
        "qty": _num(r[7]), "qty_pack": _num(r[8]), "qty_part": _num(r[9]),
        "price": _num(r[10]), "price_zk": _num(r[11]),
        "amount": _num(r[12]), "amount_zk": _num(r[13]), "amount_hs": _num(r[14]),
        "batch": r[15],
        # ★★ 明细行备注：字段名统一为 **`remark`**（2026-09-22 第十轮）。
        #
        # 原先这里叫 `note` —— 但**写入侧的参数名是 `remark`**
        # （见 `create_bill` 里 `item.get("remark")` 与建行时的 `"remark": remark`）。
        # 一读一写两个名字 ⇒ 前端 `toPurchaseLine` 映射的是 `note`、
        # 而页面提交时写的是 `remark` ⇒ **改备注回读不到**（与销售侧
        # `XSMX.BZ_XSMX` 那次「四层都要带」是同一类坑）。
        # 现在统一成 `remark`（与销售单一行口径一致），列仍是 `JHMX.BZ_JHMX`。
        # ★ 前端适配层兼容读 `remark ?? note`，老缓存不会失效。
        "remark": r[16], "gift": bool(r[17]),
        "price_jh": _num(r[18]),
        # ★★ `LatelyStock` / `LastStockEx` 在 JHMX 里是 **datetime**，不是库存数量！
        #    名字极具误导性（第一次就按 _num 取，Decimal('2026-09-11 ...') 直接崩）。
        #    实测列类型 = datetime，值多为 NULL。语义是「最近入库时间」。
        "lately_stock_at": _fmt(r[19]),
        "warehouse": r[20], "bz_date": _fmt(r[21]),
        # ↓ 预检用的四个关联痕迹（列名与触发器判的一致，键名与销售一行同一套词）
        "qty_return": _num(r[22]), "qty_return_unit": _num(r[23]),
        "jh_order_no": r[24],
        "jh_order_id": (str(r[25]).lower() if r[25] else ""),
        "link_import_mx": (str(r[26]).lower() if r[26] else ""),
        "line_is_ok": bool(r[27]), "batch2": r[28],
    }


def _load_lines(cur, guid):
    return [_map_line(r) for r in db.qall(cur, _lines_sql(), (str(guid),))]


def get_bill(cur, bill_no):
    """整单：单头 + 明细 + V3 是否允许改单。"""
    h = _load_header(cur, bill_no)
    if h is None:
        raise LookupError("采购单不存在：" + str(bill_no))
    lines = _load_lines(cur, h["guid"])
    reasons = editable_reasons(h, lines)
    return {
        "bill_no": h["bill_no"], "use_no": h["use_no"],
        "ndate": _fmt(h["ndate"]),
        "supplier": {"code": h["supplier_code"], "name": h["supplier_name"]},
        "warehouse": h["ckbh"], "warehouse_name": h["ck_name"],
        "jh_type": h["jh_type"],
        "status": _status(h["is_auditing"], h["user_void"], h["time_void"]),
        "is_auditing": h["is_auditing"], "is_check_up": h["is_check_up"],
        "is_ok": h["is_ok"],
        # ★ 2026-09-20：草稿标记 + 作废信息（与列表同源 _status）
        "is_draft": (not h["is_ok"]) and not _void_flag(h["user_void"], h["time_void"]),
        "user_void": h["user_void"], "time_void": h["time_void"],
        "note": h["note"], "fkfs": h["fkfs"], "tax_rate": _num(h["tax_rate"]),
        "assessor": h["assessor"], "auditing_time": _fmt(h["auditing_time"]),
        "created_by": h["user_insert"], "created_at": _fmt(h["time_insert"]),
        "print_count": h["print_count"],
        "totals": {
            "amount": _num(h["amount"]), "amount_hs": _num(h["amount_hs"]),
            "qty": _num(h["qty"]), "zk": _num(h["zk"]),
            "paid": _num(h["fk_pay"]), "paid_total": _num(h["fk_total"]),
            "line_count": len(lines),
        },
        "lines": lines,
        "editable": not reasons,
        "editable_reasons": reasons,
    }


def editable_reasons(h, lines):
    """改单限制。照销售单（sales.editable_reasons）的口径，但只取 JH 真有的位。

    ★ 不编造不存在的位：JH 没有 BillState / IsReceive / IsSend / Shipped，
      也没有 J_Fkvip（2026-10-08 对 schema dump 数过：JH 51 列里没这一列），
      所以销售那五项金额判据在这里只对得上三项。
    ★★ 2026-09-20 纠正：判「已作废」用 UserVoid/TimeVoid，**不再用 IsOk**。
       IsOk=0 只是草稿单，草稿本来就该能改 —— 旧写法 `if not h["is_ok"]`
       会把草稿单全部锁死，用户改不了自己刚存了一半的单。
    ★★ 2026-10-08：`lines` 这一遍终于真的读了。以前签名收了它却从头到尾没用过，
       于是「这行已经退过货」「这行是从进货订单转来的」这两类**必然**被 `TRI_A_JHMX`
       用 `HxRoll` 打回的情况，在页面上一律显示成可改。
       下面每一条判据都对着触发器正文写，没有一条是我自己发明的限制。
    """
    reasons = []
    if h["user_void"] or h["time_void"]:
        reasons.append("单据已作废")      # ← 真正的作废位
    if h["is_auditing"]:
        reasons.append("已审核")
    if h["auditing_time"]:
        reasons.append("已有审核时间")
    if _num(h["fk_pay"]) != 0 or _num(h["fk_total"]) != 0:
        reasons.append("已存在付款/结算金额")
    # 优惠金额：TRI_A_JH 拿它跟已付并成一堆"已经结掉的钱"来判
    # （`ABS(J_JEHSSUM) < ABS(J_JEFK)+ABS(J_JELosSUM)` 直接 HxRoll('TRI_A_JH2')）
    if _num(h.get("los_sum")) != 0:
        reasons.append("已存在优惠金额")
    if _num(h.get("fk_advance")) - _num(h.get("fk_advance_dec")) != 0:
        reasons.append("已存在预付款")
    for x in lines:
        # 删行那半段：Deleted.S_THSL<>0，或 JHTHMX 里挂着这行 ⇒ HxRoll('TRI_U_JHMX_1')
        if _num(x.get("qty_return")) != 0:
            reasons.append("明细 %s 已有退货数量" % x["hpbh"])
        # 插行那半段：JHOrderNo 必须还在 JHOrder 里（'DataValid_LinkBillItembh'）。
        # 逐行删+重插会把订单/导入关联丢掉，所以与销售同一口径先挡住。
        if x.get("jh_order_no") or x.get("jh_order_id") or x.get("link_import_mx"):
            reasons.append("明细 %s 有关联(进货订单/导入)" % x["hpbh"])
    return reasons


# ★★ 与 sales._STATE_SQL **同一套词汇**：pending / audited / void。
# ★★★ 2026-09-20 纠正：`IsOk` **不是**「作废位」！
#     IsOk=1 正式单(未审核) / IsOk=0 草稿单(未审核)，
#     作废时 IsOk 置 0 且写 UserVoid + TimeVoid。
#     旧写法 `"void": "j.IsOk=0"` 会把草稿单误报成作废。
#     JH 有 UserVoid(nvarchar,NOT NULL,默认'') 与 TimeVoid(datetime,NULL) 两列，
#     与 XS 同名同义 —— 两列都要判，只判其一会漏。
_VOID_SQL = "(ISNULL(j.UserVoid,'')<>'' OR j.TimeVoid IS NOT NULL)"

_STATE_SQL = {
    # 前端 tab → SQL 条件。不在这里过滤的话前端就要把全部行拉回来自己筛。
    # pending = 未审核（正式 + 草稿都算），只要没作废就归这里
    "pending": "ISNULL(j.IsAuditing,0)=0 AND NOT " + _VOID_SQL,
    "audited": "j.IsAuditing=1",
    "void": _VOID_SQL,
}


def list_bills(cur, date_from=None, date_to=None, supplier=None, keyword=None,
               state=None, limit=100, offset=0):
    """进货单分页列表。

    ★★ 排序用 `NDate DESC, idencol DESC` 而不是 idencol 单列：
       实测 16 张单的日期与 idencol 同序，但真实业务里补录单会让两者不一致，
       用户关心的是「最近的采购」，所以以日期为第一键。

    ★★ 用 ROW_NUMBER() 不用 OFFSET/FETCH（目标库 SQL Server 2008 R2）。
       关键字要能搜到「供应商名」，而供应商名在 Gysdan 里，
       所以子查询里必须带上这个 JOIN，外层再取一次是为了拿 Itemmc。
    """
    limit, offset = _page_args(limit, offset)
    where, args = ["1=1"], []
    if date_from:
        where.append("j.NDate >= %s")
        args.append(str(date_from))
    if date_to:
        where.append("j.NDate < DATEADD(day, 1, %s)")
        args.append(str(date_to))
    if supplier:
        where.append("j.ItemBh = %s")
        args.append(str(supplier))
    st = str(state or "").strip().lower()
    if st in _STATE_SQL:
        where.append(_STATE_SQL[st])
    kw = str(keyword or "").strip()
    if kw:
        # ★ 必须带上 `j.ItemBh`（供应商编号）：
        #   第一版只搜了单号/流水号/供应商名/备注，结果搜 'ces001' 得 0 条 ——
        #   而 ces001 正是本库唯一的供应商。用户搜供应商编号是常态。
        where.append(_like_clause(
            ["j.BillNo", "j.Useno", "j.ItemBh", "g.Itemmc", "ISNULL(j.BZ_JH,'')"],
            kw, args))
    clause = " AND ".join(where)

    total = db.q1(
        cur,
        "SELECT COUNT(*) FROM JH j LEFT JOIN Gysdan g ON g.Itembh = j.ItemBh WHERE " + clause,
        tuple(args),
    )[0]

    # ★ 只做一次 % 运算（把 clause/offset/limit 一起喂进去）。
    #   分成两步 `% clause` 再 `% (...)` 会把 clause 里给 pymssql 的 %s
    #   当成格式符再解析一次 —— 这正是 master._like_clause 注释里那个坑。
    rows = db.qall(
        cur,
        """SELECT t.BillNo, t.NDate, t.ItemBh, ISNULL(g.Itemmc,''), t.Ckbh,
                  ISNULL(ck.Text,''), t.J_JESUM, t.J_JEHSSUM, t.S_SLSUM,
                  t.IsAuditing, t.IsOk, t.idencol, ISNULL(t.BZ_JH,''),
                  ISNULL(t.line_count,0), ISNULL(t.UserVoid,''), t.TimeVoid,
                  t.IsCheckUp
           FROM (
               SELECT j.BillNo, j.NDate, j.ItemBh, j.Ckbh, j.J_JESUM, j.J_JEHSSUM,
                      j.S_SLSUM, j.IsAuditing, j.IsOk, j.idencol, j.BZ_JH,
                      j.UserVoid, j.TimeVoid, j.IsCheckUp,
                      (SELECT COUNT(*) FROM JHMX m WHERE m.BillGuid = j.idenguid) AS line_count,
                      ROW_NUMBER() OVER (ORDER BY j.NDate DESC, j.idencol DESC) AS rn
               FROM JH j LEFT JOIN Gysdan g ON g.Itembh = j.ItemBh
               WHERE %s
           ) t
           LEFT JOIN Gysdan g ON g.Itembh = t.ItemBh
           LEFT JOIN CkType ck ON ck.Key_ = t.Ckbh
           WHERE t.rn > %d AND t.rn <= %d
           ORDER BY t.rn""" % (clause, offset, offset + limit),
        tuple(args),
    )
    return _paged(rows, total, limit, offset, lambda r: {
        "bill_no": r[0], "ndate": _fmt(r[1]),
        "supplier": {"code": r[2], "name": r[3]},
        "warehouse": r[4], "warehouse_name": r[5],
        "amount": _num(r[6]), "amount_hs": _num(r[7]), "qty": _num(r[8]),
        "is_auditing": r[9], "is_ok": r[10],
        # ★ 2026-09-20：状态改用「作废位(UserVoid/TimeVoid) + 审核位」判定，
        #   不再用 IsOk；并补 is_draft 让前端能标「草稿」。
        "status": _status(r[9], r[14], r[15]),
        "is_draft": (not r[10]) and not _void_flag(r[14], r[15]),
        "user_void": r[14] or "", "time_void": _fmt(r[15]),
        "line_count": int(r[13] or 0),
        "note": r[12],
    })


# ---------------------------------------------------------------- 供应商


def _find_supplier(cur, code):
    # ★ 类别口径与客户一致（2026-09-21）：Lbtype_Gys 是类别编号（GysType.Key_），
    #   显示名要 JOIN GysType 取 Text。旧实现把 "type" 指向 GysType(int)（恒 0，
    #   前端 falsy 后显示空），供应商类别从来没显示对过。
    return db.q1(
        cur,
        """SELECT TOP 1 g.Itembh, g.Itemmc, ISNULL(g.Py,''), ISNULL(g.GysType,0),
                  ISNULL(g.Lbtype_Gys,''),
                  ISNULL(NULLIF(gt.Text,''), ISNULL(NULLIF(gt.typetxt,''), g.Lbtype_Gys)),
                  ISNULL(g.Lxr,''), ISNULL(g.SJ,''),
                  ISNULL(g.Tel,''), ISNULL(g.Fax,''), ISNULL(g.Address,''),
                  ISNULL(g.BankName,''), ISNULL(g.BankID,''), ISNULL(g.BZ_Gysdan,''),
                  g.J_DueSum, g.J_Due, g.J_Advance, g.StopUse, g.PriceTypeJH, g.Ckbh
           FROM Gysdan g LEFT JOIN GysType gt ON gt.Key_ = g.Lbtype_Gys
           WHERE LTRIM(RTRIM(g.Itembh)) = %s""",
        (str(code).strip(),),
    )


def supplier_row(cur, code):
    r = _find_supplier(cur, code)
    if not r:
        raise LookupError("供应商不存在：" + str(code))
    return {
        "code": r[0], "name": r[1], "py": r[2],
        # ★ type=类别名称（显示）；type_code=Lbtype_Gys 原值（编辑保存写回）。
        #   gys_type 是 GysType(int) 原始值，仅供诊断；grade 保留旧别名兼容。
        "type": r[5] or "", "type_code": r[4], "gys_type": r[3], "grade": r[4],
        "contact": r[6], "phone": r[7], "tel": r[8],
        "fax": r[9], "address": r[10], "bank_name": r[11], "bank_id": r[12],
        "remark": r[13], "due_sum": _num(r[14]), "due": _num(r[15]),
        "advance": _num(r[16]), "stop_use": stopped_bit(r[17]), "stop": stopped_bit(r[17]),
        "status_name": contact_status_name(r[17]),
        "price_type_jh": r[18], "warehouse": r[19],
    }


def list_suppliers(cur, keyword="", limit=100, offset=0, show_stopped=False):
    """供应商分页列表（空关键字 = 全部）。

    ★ 过滤口径跟 master.list_customers 一致：默认 StopUse=0 且排除占位行 '---'。
      show_stopped 只给档案列表页那个「显示已停用」用；开单页选供应商永远不传。
      Gysdan 实测只有 1 家（ces001 测试供应商），但这个口径是为真实账套写的。
    """
    limit, offset = _page_args(limit, offset)
    kw = str(keyword or "").strip()
    where, args = (["Itembh <> '---'"] if show_stopped
                   else ["StopUse=0", "Itembh <> '---'"]), []
    if kw:
        where.append(_like_clause(["Itembh", "Itemmc", "Py", "Lxr", "Tel"], kw, args))
    clause = " AND ".join(where)
    total = db.q1(cur, "SELECT COUNT(*) FROM Gysdan WHERE " + clause, tuple(args))[0]
    # ★ 暂停往来有几家（与关键字同源，不受 show_stopped 影响），界面上要说得出差额
    stop_where = " AND ".join(["StopUse=1"] + [w for w in where if w != "StopUse=0"])
    stopped_count = int(db.q1(cur, "SELECT COUNT(*) FROM Gysdan WHERE " + stop_where,
                              tuple(args))[0])
    rows = db.qall(
        cur,
        """SELECT g.Itembh, g.Itemmc, ISNULL(g.Py,''), ISNULL(g.GysType,0),
                  ISNULL(g.Lbtype_Gys,''),
                  ISNULL(NULLIF(gt.Text,''), ISNULL(NULLIF(gt.typetxt,''), g.Lbtype_Gys)),
                  ISNULL(g.Lxr,''), ISNULL(g.SJ,''), ISNULL(g.Tel,''), ISNULL(g.Address,''),
                  g.J_Due, g.J_Advance, g.StopUse, g.idencol
           FROM (
               SELECT Itembh, Itemmc, Py, GysType, Lbtype_Gys, Lxr, SJ, Tel, Address,
                      J_Due, J_Advance, StopUse, idencol,
                      ROW_NUMBER() OVER (ORDER BY
                        CASE WHEN ISNULL(StopUse,0) = 1 THEN 0 ELSE 1 END, Itembh) AS rn
               FROM Gysdan WHERE %s
           ) g
           LEFT JOIN GysType gt ON gt.Key_ = g.Lbtype_Gys
           WHERE g.rn > %d AND g.rn <= %d
           ORDER BY g.rn""" % (clause, offset, offset + limit),
        tuple(args),
    )
    out = _paged(rows, total, limit, offset, lambda r: {
        "code": r[0], "name": r[1], "py": r[2],
        "type": r[5] or "", "type_code": r[4], "gys_type": r[3],
        "contact": r[6], "phone": r[7], "tel": r[8], "address": r[9],
        "due": _num(r[10]), "advance": _num(r[11]), "stop_use": stopped_bit(r[12]),
        # ★ 与 supplier_row（详情）同形，且三个档案列表的状态键一致（stop / status_name）
        "stop": stopped_bit(r[12]), "status_name": contact_status_name(r[12]),
    })
    out["stoppedCount"] = stopped_count
    out["showStopped"] = bool(show_stopped)
    return out


def list_supplier_types(cur):
    """供应商分类（GysType）。★ 表结构和 KhType 不完全一样：列名是小写 key_/text。"""
    rows = db.qall(
        cur,
        "SELECT key_, ISNULL(text,''), ISNULL(typetxt,'') FROM GysType "
        "WHERE Parent = 'O_' AND StopUse = 0 ORDER BY idencol",
    )
    return [{"code": r[0], "name": r[1] or r[2] or r[0]} for r in rows]


def next_supplier_code(cur):
    """新供应商编号：G + 5 位自增（本账套暂无 G 开头供应商，从 G00001 起）。

    ★ 编号规则参照客户（Khdan → K+5 位）；不能与 Khdan.Itembh 冲突 ——
      TRI_A_Gysdan 会拦「客户资料已经存在该编号」（TRI_A_Gysdan5），
      这里提前自检给出可读错误。
    """
    r = db.q1(
        cur,
        "SELECT MAX(CAST(SUBSTRING(Itembh,2,30) AS BIGINT)) FROM Gysdan WHERE Itembh LIKE 'G[0-9]%'",
    )
    return "G" + str(int(r[0] or 0) + 1).zfill(5)


def save_supplier(cur, data, commit=True):
    """供应商新增/编辑（2026-09-21）。

    data: {code?, name, type_code?(=Lbtype_Gys), contact?, phone?(=SJ),
           tel?, address?, remark?(=BZ_Gysdan), bank_name?, bank_id?, py?}

    ★★ INSERT 必给列清单来自 INFORMATION_SCHEMA 实测（probe-gysdan-structure.py）：
       Gysdan 有 30+ 个 NOT NULL 且无默认值的列（idencol 是自增，不写）。
       数值/开关列的默认值全部照抄账套现存唯一真实行（ces001）：
       PriceTypeJH=21、PriceTrace=101、其余 0/''。
    ★ 触发器 TRI_A_Gysdan 的约束（INSERT 也走）：
       - Itembh 非空；
       - Itembh 不得与 Khdan 冲突；
       - Lbtype_Gys 必须存在于 GysType 或为空串 → 写库前自检，给出可读错误。
    ★ 只做单行操作 —— 触发器 `@@ROWCOUNT <> 1 即 return`，批量会被静默跳过。
    """
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("供应商名称不能为空")
    code = str(data.get("code") or "").strip()
    type_code = str(data.get("type_code") or data.get("category") or "").strip()
    contact = str(data.get("contact") or "").strip()
    phone = str(data.get("phone") or "").strip()
    tel = str(data.get("tel") or "").strip()
    address = str(data.get("address") or "").strip()
    remark = str(data.get("remark") or "").strip()
    bank_name = str(data.get("bank_name") or "").strip()
    bank_id = str(data.get("bank_id") or "").strip()

    if type_code:
        t = db.q1(
            cur,
            "SELECT COUNT(*) FROM GysType WHERE Key_ = %s AND StopUse = 0",
            (type_code,),
        )[0]
        if not t:
            raise ValueError("供应商类别不存在：" + type_code)

    ex = _find_supplier(cur, code) if code else None
    if ex:
        py = str(data.get("py") or (ex[2] or name[:10]))[:20]
        cur.execute(
            """UPDATE Gysdan SET Itemmc=%s, Py=%s, Lbtype_Gys=%s,
               Lxr=%s, SJ=%s, Tel=%s, Address=%s, BZ_Gysdan=%s,
               BankName=%s, BankID=%s,
               UserUpdate=%s, TimeUpdate=%s
               WHERE Itembh=%s""",
            (name, py, type_code, contact, phone, tel, address, remark,
             bank_name, bank_id, _who(), _now_str(), code),
        )
        if commit:
            cur.connection.commit()
        return {"code": code, "created": False}

    code = code or next_supplier_code(cur)
    if _find_supplier(cur, code):
        raise ValueError("供应商编号已存在：" + code)
    if db.q1(cur, "SELECT COUNT(*) FROM Khdan WHERE Itembh = %s", (code,))[0]:
        raise ValueError("编号与客户档案冲突，请换一个：" + code)
    guid = str(uuid.uuid4())
    py = str(data.get("py") or name[:10])[:20]
    cur.execute(
        """INSERT INTO Gysdan (
               Itembh, Itemmc, ItemType, GysType, py, Lxr, Lbtype_Gys,
               AreaTypeKey, SJ, Tel, Fax, PostID, Address,
               BankName, BankID, email, Http, BZ_Gysdan,
               J_DueSum, DefaID, J_Due, J_Advance, J_QcYfje,
               PriceTypeJH, PriceTypeXS, PriceTrace,
               CheckOutType, CheckOutDay, CheckOutDate,
               Ckbh, LastStockBillNo,
               UserInsert, UserUpdate, TimeUpdate, StopUse, IsSend, idenguid)
           VALUES (
               %s, %s, 0, 0, %s, %s, %s,
               '', %s, %s, '', '', %s,
               %s, %s, '', '', %s,
               0, 0, 0, 0, 0,
               21, 0, 101,
               0, 0, 0,
               '', '',
               %s, '', %s, 0, 0, %s)""",
        (code, name, py, contact, type_code, phone, tel, address,
         bank_name, bank_id, remark, _who(), _now_str(), guid),
    )
    if commit:
        cur.connection.commit()
    return {"code": code, "created": True}


def supplier_prices(cur, code, limit=200):
    """某供应商的进货价格历史（Price_Gys）。供编辑页带出最近进价。"""
    rows = db.qall(
        cur,
        """SELECT TOP %d p.Hpbh, ISNULL(h.hpmc,''), ISNULL(h.ggxh,''), p.HpUnitName,
                  p.D_Price, p.D_PriceTax, ISNULL(p.LastStockBillNo,''), p.TimeUpdate
           FROM Price_Gys p LEFT JOIN Hpdan h ON h.hpbh = p.Hpbh
           WHERE LTRIM(RTRIM(p.Itembh)) = %%s
           ORDER BY p.idencol""" % min(int(limit or 200), 500),
        (str(code).strip(),),
    )
    return [
        {
            "hpbh": r[0], "name": r[1], "spec": r[2], "unit": r[3],
            "price": _num(r[4]), "price_tax": _num(r[5]),
            "last_bill_no": r[6], "updated_at": _fmt(r[7]),
        }
        for r in rows
    ]


# ---------------------------------------------------------------- 新建进货单
#
# ★★ 为什么这么写（2026-09-20 直连库实测 + 读触发器源码得出，别再试错）：
#   1. JH 有 **47 个 NOT NULL 且无默认值** 的列，JHMX 有 **72 个**。
#      手写列清单极易漏项（漏一个非空列就整单失败）。因此 INSERT 采用
#      **全列显式赋值**，按真实列名一次写全，便于逐列核对。
#   2. `TRI_A_JH` 触发 INSERT/UPDATE/DELETE，联动 **应付账款**
#      （HxUpdateAccountPayable）、供应商最近进货时间；它**不写库存流水**。
#   3. `TRI_A_JHMX` 才写库存流水：内部调
#      `HxUpdateInvenFlowAdd 1020, JH.idencol, NDate, 1, Ckbh, Hpbh, ...`
#      → InvenFlow 里以 `BillNaid=1020`、`LinkGuid=JHMX.IdenGuid` 落行。
#   4. **两个触发器都依赖临时表 `#Bill` / `#BillMx`**（`HxUpdateBillTemp`、
#      `IF not Exists(SELECT 1 FROM #BillMx ...)`）。不先建表就直接 INSERT，
#      必报 `208 Invalid object name '#Bill'`。用 db.ensure_temp 预置。
#   5. 取号：`No_BillID` 的 1020 行 `BillNoid` 实测为 **0**（销售 2020 是 76）。
#      所以和李生销售单一样，必须以 `MAX(idencol)+1` 兜底，
#      否则会与既有单号撞车。前缀来自 BILLNOLEFT2='JH'。
#   6. 数量口径：JH/JHMX 是**进货入库**，S_SL 为正数（销售出库才是负）。


def _junit(cur, hpbh, unit):
    """取货品单位并校验换算率为 1（与 sales._unit 同口径）。"""
    row = db.q1(
        cur,
        """SELECT h.hpbh, u.UnitName, u.UnitRate, u.UnitBasic, ISNULL(u.UnitStop,0)
           FROM Hpdan h JOIN HpdanUnit u ON h.IdenGuid = u.HpIdenGuid
           WHERE LTRIM(RTRIM(h.hpbh)) = %s AND LTRIM(RTRIM(u.UnitName)) = %s""",
        (hpbh.strip(), (unit or "").strip()),
    )
    if not row:
        raise ValueError(f"货品 {hpbh} 没有「{unit}」这个单位（先在产品资料里加，或改用别的单位）")
    if int(row[4] or 0):
        raise ValueError(f"货品 {hpbh} 的单位「{unit}」已停用，不能再用它开单")
    if _dec(row[2]) <= 0:
        raise ValueError(f"货品 {hpbh} 单位 {unit} 的换算率是 {row[2]}，不合法（先去产品资料改对）")
    return row


def _insert_jhmx(cur, cfg, guid, ckbh, item, position):
    """插入一行进货明细。item: {hpbh, unit, qty, price?, remark?}

    ★ 备注落 **JHMX.BZ_JHMX**（另有 BZ_JHMX2..15 共 15 个备用备注列，默认空）。
    """
    cur.execute("TRUNCATE TABLE #OutGuid")
    hpbh = str(item.get("hpbh") or "").strip()
    unit = str(item.get("unit") or "").strip()
    urow = _junit(cur, hpbh, unit)
    hpbh = str(urow[0])  # 主档规范编号
    unit = str(urow[1])  # 单位名以库里的写法为准
    rate = _dec(urow[2]) or Decimal("1")
    qty = Decimal(str(item.get("qty")))
    if qty <= 0:
        raise ValueError(f"货品 {hpbh} 数量必须大于 0")
    # ★ 与销售同口径（拆分逻辑共用 product.split_qty）：
    #   S_SLUnit=这个单位的数量、S_SL=折成基本单位的数量。
    qty_unit, part, base_qty = product_mod.split_qty(qty, rate)
    remark = str(item.get("remark") or "")
    if len(remark) > 100:
        raise ValueError(f"货品 {hpbh} 明细备注过长（BZ_JHMX 最多 100 字）")
    if item.get("price") in (None, ""):
        raise ValueError(f"货品 {hpbh} 缺单价（采购单必须显式给价，不自动取价）")
    price = Decimal(str(item["price"]))
    if price < 0:
        raise ValueError(f"货品 {hpbh} 单价不能为负")
    amt = (qty * price).quantize(Decimal("0.01"))
    # ★★ JHMX.IDENCOL 是 **IDENTITY 自增列**，必须**不写**（由服务端分配）。
    #    带值写入会报 544 Cannot insert explicit value for identity column
    #    when IDENTITY_INSERT is set to OFF。
    #    （对比：JH.idencol 也是自增，但单头按 V3 口径用 IDENTITY_INSERT ON 显式指定，
    #      明细则完全交给自增 —— 这是与桌面端一致的行为。）
    # ★★ 赠品位（JHMX.Gifts）口径与销售侧同一条，且都是照 V3 桌面端实测来的：
    #   单价与金额照记（赠品价值要留得下来），清零的是**折后金额 J_JEZK 与含税金额 J_JEHS**
    #   ⇒ 单头合计按这两列求和，所以"不进合计"自动成立。
    #   （采购侧读早就把 Gifts 查出来了 —— `_lines_sql` 里就有 `gift`，
    #    以前写侧一直是常量 0，等于"读得到、永远写不进去"。）
    gift = bool(item.get("gift"))
    amt_zk = Decimal("0.00") if gift else amt
    # ★ `J_JEZKE` 只在赠品行填货值（正常行留 0）。这条规则是从**销售**侧样本量出来的
    #   （geshi 整册只有那一行赠品非 0；tongda 95,987 行全 0），
    #   采购侧目前没有 V3 手录的赠品单可对，所以这里是**同形推断** ——
    #   两张明细表这一列同名同位，头表求和也不读它，写错的后果只是"少一列统计"，
    #   等哪天有进货赠品样本再用 probe-jezke.py 复核一次。
    amt_zk_e = amt if gift else Decimal("0.00")
    cur.execute(
        """INSERT INTO JHMX (BillGuid, Hpbh, HpUnitName, HpUnitRate, S_Pie, S_PRate,
            S_SLUnit, S_SLPack, S_SLPart, S_SL, S_SLText, S_SLEx, S_CheckQty, S_CheckDif,
            HpLen, HpWidth, HpHeight, HpDensity, HpArea, HpVolume, HpWeight, HpModule,
            R_SLModule, R_SLArea, R_SLVolume, R_SLWeight, Scpc, Scpc2,
            D_DJ, D_DJTax, D_DJLast, J_JE, ZK, D_DJZK, J_JEZK, J_JEZKE,
            J_JEFee01, J_JEFee02, J_JEFee03, J_JEFee04, J_JEFee05,
            TaxRate, J_SE, J_JEHS, Gifts, D_JHPrice, LastStockBillNo,
            D_Price, D_PriceZk, Ckbh, JHOrderNo, JHOrderUseNo,
            S_THSL, S_THSLUnit, ISOk, BZ_JHMX,
            BZ_JHMX2, BZ_JHMX3, BZ_JHMX4, BZ_JHMX5, BZ_JHMX6, BZ_JHMX7,
            BZ_JHMX8, BZ_JHMX9, BZ_JHMX10, BZ_JHMX11, BZ_JHMX12, BZ_JHMX13,
            BZ_JHMX14, BZ_JHMX15)
            OUTPUT INSERTED.IDENGUID INTO #OutGuid
            VALUES (CONVERT(uniqueidentifier, %s), %s, %s, %s, 0, 0,
                    %s, 0, %s, %s, '', 0, 0, 0,
                    0, 0, 0, 0, 0, 0, 0, 0,
                    0, 0, 0, 0, '', '',
                    %s, %s, 0, %s, 0, %s, %s, %s,
                    0, 0, 0, 0, 0,
                    0, 0, %s, %s, 0, '',
                    0, 0, %s, '', '',
                    0, 0, 1, %s,
                    '', '', '', '', '', '',
                    '', '', '', '', '', '',
                    '', '')""",
        (
            # 17 个占位符，顺序必须与 VALUES 中 %s 出现次序严格一致：
            # BillGuid, Hpbh, HpUnitName, HpUnitRate, S_SLUnit, S_SLPart, S_SL,
            # D_DJ, D_DJTax, J_JE, D_DJZK, J_JEZK, J_JEZKE, J_JEHS, Gifts, Ckbh, BZ_JHMX
            guid, hpbh, unit, rate,
            qty_unit, part, base_qty,
            price, price, amt, price, amt_zk, amt_zk_e, amt_zk,
            1 if gift else 0,
            ckbh, remark,
        ),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"插入进货明细失败：{hpbh}")
    row = db.q1(cur, "SELECT TOP 1 IDENGUID FROM #OutGuid")
    if not row:
        raise RuntimeError(f"插入明细后未取到 IDENGUID：{hpbh}")
    return {
        "hpbh": hpbh, "unit": unit, "qty": qty, "price": price,
        # ★ 与销售一致：把当时倍率与折成基本单位的数量回给前端
        "rate": _dec(rate), "qty_unit": _dec(qty), "qty_base": _dec(base_qty),
        "amount": amt, "iden_guid": str(row[0]), "remark": remark,
    }


def create_bill(cur, cfg, supplier, lines, ndate=None, warehouse=None, note="",
                client_token=None, commit=True):
    """新建进货单（未审核草稿单）。

    ★ 口径与销售单 create_bill 一致：取号 → ensure_temp → 单头 INSERT →
      逐行明细 INSERT → 合计回填 → 自检（JHMX 行数 == InvenFlow 行数）。
    ★ 表头时间 `YYYY-MM-DD HH:mm:ss`、制单人 zdrbh/UserInsert/UserUpdate = 这条请求的操作员。

    client_token：前端为「一次提交」生成的幂等键（可选）。
    ★ 与销售单**同一套机制**（`v3agent/idem.py`，第十轮抽取为共用）：
      同一 token 重复到达时**不再插库**，直接回上次那张单。
    ★ 采购建单的暴露面与销售完全一样（真机连点保存 = 多张进货单），
      但原实现没有任何防护 —— 前端 `saving` 锁只能挡连点，
      挡不住「请求已到、单已插库、响应丢失」这种重试。
    ★ 老版本前端不发该字段 ⇒ token 为空 ⇒ 完全走原行为（向后兼容）。
    """
    # ★ 幂等闸门必须在**任何写操作之前**（放在取号/INSERT 之后就没意义了）。
    token = str(client_token or "").strip()
    # ★ 幂等键带账套（Step 2 请求级选库）：同一个 token 打到另一本账时不能复用这本的单号
    dbns = idem.db_of(cfg)
    state, replay_no = idem.begin(token, database=dbns)
    if state == "replay":
        bill = get_bill(cur, replay_no)
        bill["idempotent_replay"] = True
        return bill

    try:
        return _create_bill_inner(cur, cfg, supplier, lines, ndate, warehouse, note,
                                  token, commit)
    except Exception:
        # ★ 失败必须释放占位：否则用户重试会被「同一笔提交正在处理中」挡住，
        #   而这恰恰是最需要他能重试的时刻。
        idem.abort(token, database=dbns)
        raise


def _create_bill_inner(cur, cfg, supplier, lines, ndate, warehouse, note, token, commit):
    """create_bill 的真实实现（幂等闸门已在外层处理）。"""
    supplier = str(supplier or "").strip()
    if not supplier:
        raise ValueError("缺少供应商")
    sup = db.q1(
        cur,
        "SELECT Itembh, Itemmc, ISNULL(Ckbh,'') FROM Gysdan WHERE LTRIM(RTRIM(Itembh)) = %s",
        (supplier,),
    )
    if not sup:
        raise ValueError("供应商不存在：" + supplier)
    supplier = str(sup[0]).strip()
    warehouse = str(warehouse or sup[2] or "101-").strip()
    if not db.q1(cur, "SELECT Key_ FROM CkType WHERE Key_=%s", (warehouse,)):
        raise ValueError("仓库不存在：" + warehouse)

    if ndate is None or str(ndate).strip() == "":
        ndate = datetime.now()
    elif isinstance(ndate, str):
        raw = ndate.replace("T", " ").strip()
        try:
            ndate = datetime.strptime(raw[:19], TIME_FMT)
        except ValueError:
            ndate = datetime.strptime(raw[:10], "%Y-%m-%d")

    note = str(note or "")
    # ★★ 注意：备注要写**两个**列，长度上限不同，必须按最小的那个卡：
    #    JH.NoteCode = nvarchar(20)  ← 最紧，历史上前端传长备注必报
    #                                  8152 String or binary data would be truncated
    #    JH.BZ_JH    = nvarchar(200)
    #    桌面端把同一段文本同时写进这两列，所以这里也保持一致。
    if len(note) > 20:
        raise ValueError("单头备注过长（NoteCode 最多 20 字）")

    specs = []
    for i, it in enumerate(lines or [], 1):
        hpbh = str(it.get("hpbh") or "").strip()
        qty = it.get("qty")
        if not hpbh:
            raise ValueError(f"第 {i} 行缺货品")
        if qty in (None, ""):
            raise ValueError(f"第 {i} 行缺数量")
        if Decimal(str(qty)) <= 0:
            raise ValueError(f"第 {i} 行数量必须大于 0")
        specs.append(
            {
                "hpbh": hpbh,
                "unit": str(it.get("unit") or "").strip(),
                "qty": Decimal(str(qty)),
                "price": it.get("price"),
                "remark": str(it.get("remark") or ""),
                # ★ 赠品位：缺省 False（老前端/老断网草稿不带这一键时行为不变）
                "gift": bool(it.get("gift")),
            }
        )
    if not specs:
        raise ValueError("至少一行商品明细")

    # ---- 取号：BillJH(1020)，前缀来自 No_BillID.BILLNOLEFT2
    #
    # ★★★ 并发安全（2026-10-11）：与销售单**同一套机制**（`db.next_bill_number`）——
    #   应用锁（sp_getapplock，会话级，资源名按账套+类型分）保护下顺序推进计数器。
    #   原写法是「先 SELECT → +1 → UPDATE」四步无锁，与销售侧完全同构 ⇒ 同样会撞号，
    #   只是 JH 单量小没暴露。踩坑论证见 v3agent/sql.py「单据取号（并发安全）」。
    _lock_res = db.app_lock_resource(idem.db_of(cfg), "jh", 1020)
    _lock_held = False
    try:
        prefix, nid = db.next_bill_number(
            cur, bill_na_id=1020, database=idem.db_of(cfg), kind="jh",
            # 前缀不参与竞态，锁前读一次；缺省 "JH"
            prefix_sql="SELECT ISNULL(BILLNOLEFT2,'') FROM No_BillID WHERE BillNaID=1020",
            # ★ BillNoid 实测为 0，必须以 MAX(idencol)+1 兜底，否则与既有单号撞车
            max_sql="SELECT ISNULL(MAX(idencol),0) FROM JH",
        )
        _lock_held = True
        prefix = (prefix or "JH").strip()
    except Exception:
        raise
    bill_no = prefix + str(nid).zfill(8)
    guid = str(uuid.uuid4())
    now_str = _now_str()

    # 触发器依赖的临时表必须在单头 INSERT 前就绪（否则 208 Invalid object name '#Bill'）
    db.ensure_temp(cur, guid, nid, ndate, supplier)
    cur.execute("SET IDENTITY_INSERT JH ON")
    try:
        # ★★ JH **没有 BillNaID 列**（那是 XS 的写法）——单据类型由 No_BillID.BillTable='JH'
        #    定位，单头本身只有 JHType 字段承载业务分类（实测真实单为 ''）。
        #    第一次写 `INSERT INTO JH (BillNaID, ...)` 直接报 207 Invalid column name。
        cur.execute(
            """INSERT INTO JH (BillNo, Useno, NDate, JHType, ItemBh, NoteCode,
               ywbh, zdrbh, Ckbh, TaxRate, TaxType, J_JESUM, J_JEHSSUM, J_CalcLos,
               J_SESUM, S_SLPackSUM, S_SLPartSUM, S_SLSUM, S_PieSUM, ZK, J_JezkSum,
               J_JezkeSum, FkDay, Fkfs, AccName, J_FkJePay, J_FkLos, J_FkAdvance,
               J_FkAdvanceDec, J_JEFK, J_JELosSUM, J_JETH, J_JEWF, BZ_JH, PrintCount,
               BillHpType, IsAuditing, AssessorBh, AssessorBh2, IsCheckUp, IsOk,
               UserInsert, UserUpdate, UserVoid, TimeInsert, TimeUpdate, idenguid, idencol)
               VALUES (%s,'',%s,'',%s,%s,
                       '',%s,%s,0,1,0,0,0,
                       0,0,0,0,0,0,0,
                       0,%s,'现款','',0,0,0,
                       0,0,0,0,0,%s,0,
                       '',0,'','',0,1,
                       %s,%s,'',%s,%s,%s,%s)""",
            (bill_no, ndate, supplier, note,
             _who(), warehouse, ndate, note,
             _who(), _who(), now_str, now_str, guid, nid),
        )
        if cur.rowcount != 1:
            raise RuntimeError("进货单头插入失败")
        cur.execute("SET IDENTITY_INSERT JH OFF")

        inserted = []
        for i, sp in enumerate(specs, 1):
            inserted.append(_insert_jhmx(cur, cfg, guid, warehouse, sp, i))

        # ---- 合计回填（金额口径与桌面一致：J_JEWF=J_JEZK 汇总，J_JEHSSUM=J_JEHS 汇总）
        sums = db.q1(
            cur,
            """SELECT SUM(S_SL), SUM(J_JEZK), SUM(J_JEHS), SUM(J_JE)
               FROM JHMX WHERE BillGuid = CONVERT(uniqueidentifier, %s)""",
            (guid,),
        )
        qty_sum, amt_zk, amt_hs, amt_je = [Decimal(str(x or 0)) for x in sums]
        cur.execute(
            """UPDATE JH SET J_JESUM=%s, J_JEHSSUM=%s, J_JezkSum=%s, J_JEWF=%s,
                   J_SESUM=0, S_SLSUM=%s, S_SLPartSUM=%s, S_SLPackSUM=0,
                   TimeUpdate=%s, UserUpdate=%s
               WHERE idenguid = CONVERT(uniqueidentifier, %s)""",
            (amt_je, amt_hs, amt_zk, amt_zk, qty_sum, qty_sum,
             _now_str(), _who(), guid),
        )

        # ---- 事务内自检：明细行数 == 库存流水行数（LinkGuid 关联）
        cnt_mx = db.q1(
            cur, "SELECT COUNT(*) FROM JHMX WHERE BillGuid = CONVERT(uniqueidentifier, %s)", (guid,)
        )[0]
        cnt_flow = db.q1(
            cur, "SELECT COUNT(*) FROM InvenFlow WHERE BillNaid=1020 AND BillIden=%s", (nid,)
        )[0]
        if cnt_mx != len(specs) or cnt_flow != len(specs):
            raise RuntimeError(
                f"自检失败：JHMX {cnt_mx} 行 / InvenFlow {cnt_flow} 行（期望 {len(specs)}）"
            )
        if commit:
            cur.connection.commit()
    except Exception:
        try:
            cur.execute("SET IDENTITY_INSERT JH OFF")
        except Exception:
            pass
        cur.connection.rollback()
        raise
    finally:
        # ★ 应用锁必须在 COMMIT（或 ROLLBACK）**之后**才释放：
        #   提前放锁 = 号还没落库就把取号权交出去，等于没锁。
        if _lock_held:
            db.release_number_lock(cur, _lock_res)
    # ★ 提交成功后才回填幂等表。放在 commit 之后是刻意的：
    #   若中途异常回滚，占用会被 `idem.abort` 释放，同一 token 可以正常重试，
    #   不会出现「第一次失败但 token 已被标记成功」→ 用户重试反而不建单。
    if commit:
        idem.finish(token, bill_no, database=idem.db_of(cfg))
    return get_bill(cur, bill_no)


# ---------------------------------------------------------------- 改进货单


def _same_code(a, b):
    """比货品编号/单位：**两边都剥空白再比**。

    ★ 这条不是防御性写法，是实测逼出来的：`_insert_jhmx` 落库的 hpbh 取的是
      `Hpdan.hpbh` 的**原值**（`_junit` 回来的那一列），而调用方传进来的通常是
      前端 trim 过的那一份。库里这批编号带前后空白 ⇒ 直接 `==` 会把"根本没动的行"
      判成改过，白白重插一次、还把它挪到明细末尾。
    """
    return str(a if a is not None else "").strip() == str(b if b is not None else "").strip()


def _delete_line(cur, line):
    """删一行进货明细。

    ★ `TRI_A_JHMX` 删行那半段自己会判退货关联并 `HxRoll`，但那是一句
      「TRI_U_JHMX_1 + 货品编号」级别的库错误，人看不懂。这里先查一遍给人话原因。
    ★ 逐行删不是优化，是**必须**：该触发器开头就是 `IF @@ROWCOUNT<>1 ... Return`，
      一条 DELETE 带走两行时它**静默什么都不做** —— 库存流水留在原地，
      单看着改对了，账上的现存量与 InvenFlow 从此对不上，且不报任何错。
    """
    if _num(line.get("qty_return")) != 0:
        raise ValueError(f"明细 {line['hpbh']} 已有退货数量，不能删除或修改")
    if line.get("jh_order_no") or line.get("jh_order_id") or line.get("link_import_mx"):
        raise ValueError(f"明细 {line['hpbh']} 有关联(进货订单/导入)，不能删除或修改")
    row = db.q1(
        cur, "SELECT IdenCol FROM JHTHMX WHERE LinkGuid = CONVERT(uniqueidentifier, %s)",
        (line["iden_guid"],),
    )
    if row:
        raise ValueError(f"明细 {line['hpbh']} 被退货单关联，不能删除或修改")
    cur.execute("DELETE FROM JHMX WHERE IdenGuid = CONVERT(uniqueidentifier, %s)",
                (line["iden_guid"],))
    if cur.rowcount != 1:
        raise RuntimeError(f"删除明细失败：{line['hpbh']}")


def update_bill(cur, cfg, bill_no, header=None, lines=None):
    """改进货单。header: {NDate?, ItemBh?, BZ_JH?}；lines: **最终**明细完整列表。

    形状照 `sales.update_bill`（单头白名单 → 按 iden_guid 逐行 diff → 先删后插 →
    合计回填两次 → 事务内自检），但有四处采购特有的差别，都是实测出来的：

    ① JH 没有 BillState / IsReceive / IsSend / Shipped 那几位 ⇒ 可改判定见
       `editable_reasons`（作废痕迹 / 已审核 / 已付款 / 有优惠 / 明细有退货或订单关联）。
    ② **JHMX 没有 `IdenIndex` 列**（XSMX 有）。改过的行是"删+重插"，`IDENCOL` 是自增，
       于是重插的那一行会排到明细末尾。销售那边靠重排 IdenIndex 避开了这件事；
       采购这边没有 V3 认的序号列可用 —— 不打算为排版去发明一个桌面端不读的列，
       那会让手机与桌面看到的行序不一致。这是**已知的表现差异，不是数据错误**。
    ③ 仓库 `Ckbh` 不在单头白名单里，但**每一行 JHMX 都带 Ckbh**：重插时必须沿用
       这张单原来的仓库，否则一次改价会把库存从 A 仓搬到 B 仓。
    ④ 采购不自动取价（`_insert_jhmx` 就是这条口径），所以这里没有销售那步
       `price_mod.resolve_price`；"没带 price"只允许发生在**命中旧行**时（＝不动它的原价）。
    """
    h = _load_header(cur, bill_no)
    if h is None:
        raise LookupError("采购单不存在：" + str(bill_no))
    old_lines = _load_lines(cur, h["guid"])
    reasons = editable_reasons(h, old_lines)
    if reasons:
        raise ValueError("单据不允许修改：" + "；".join(reasons))

    header = header or {}
    invalid = set(header) - {"NDate", "ItemBh", "BZ_JH"}
    if invalid:
        raise ValueError("单头不允许修改字段：%s（系统单号不可改）" % "、".join(sorted(invalid)))

    new_ndate = h["ndate"]
    new_itembh = h["supplier_code"]
    new_note = h["note"] or ""
    if "NDate" in header:
        raw = str(header["NDate"]).replace("T", " ").strip()
        try:
            new_ndate = datetime.strptime(raw[:19], TIME_FMT)
        except ValueError:
            new_ndate = datetime.strptime(raw[:10], "%Y-%m-%d")
    if "ItemBh" in header and str(header["ItemBh"]).strip() != new_itembh:
        sup = db.q1(cur, "SELECT Itembh FROM Gysdan WHERE LTRIM(RTRIM(Itembh)) = %s",
                    (str(header["ItemBh"]).strip(),))
        if not sup:
            raise ValueError("供应商不存在：" + str(header["ItemBh"]))
        new_itembh = str(sup[0]).strip()
    if "BZ_JH" in header:
        new_note = str(header["BZ_JH"] or "")
        # ★ 备注落**两个**列，长度按最紧的那个卡（与建单同一口径：
        #   JH.NoteCode nvarchar(20) 先炸，BZ_JH 是 200）
        if len(new_note) > 20:
            raise ValueError("单头备注过长（NoteCode 最多 20 字）")

    # ★★ `lines` 的三种给法必须分开：**没带这个键**＝不动明细；**带了空数组**＝报错；
    #    **带了非空数组**＝这就是最终的完整明细（只有没在里面出现的旧行才删）。
    #    销售那版写的是 `lines = lines or []`，之后"没被认领的旧行全部删掉"，
    #    于是"只改单头、不带 lines"会把整单明细删光 —— 那个写法不照抄。
    touch_lines = lines is not None
    if touch_lines and not lines:
        raise ValueError("lines 不能是空数组：要动明细就得给出**最终的完整列表**"
                         "（一张单至少一行）；不想动明细就别带这个键")
    line_specs = []
    for i, it in enumerate(lines or [], 1):
        hpbh = str(it.get("hpbh") or "").strip()
        if not hpbh:
            raise ValueError(f"第 {i} 行缺货品")
        if it.get("qty") in (None, ""):
            raise ValueError(f"第 {i} 行缺数量")
        if Decimal(str(it.get("qty"))) <= 0:
            raise ValueError(f"第 {i} 行数量必须大于 0")
        line_specs.append({
            "pos": i,
            "iden_guid": str(it.get("iden_guid") or "").strip().lower(),
            "hpbh": hpbh,
            "unit": str(it.get("unit") or "").strip() or None,
            "qty": it.get("qty"),
            "price": it.get("price"),
            "remark": str(it.get("remark") or ""),
            # ★ 没带 gift 这一键 = 不动它（退回原行的值）；带了 false 才是真的取消赠品。
            #   与 sales.update_bill 同一个理由，别改成 bool()。
            "gift": None if it.get("gift") is None else bool(it.get("gift")),
        })

    # 不动明细时，期望行数就是原有行数（后面两处自检都按这个数比）
    want_rows = len(line_specs) if touch_lines else len(old_lines)

    by_guid = {x["iden_guid"].lower(): x for x in old_lines}
    used = set()
    deletes = []
    has_add = has_replace = has_remove = False
    for sp in line_specs:
        old = by_guid.get(sp["iden_guid"]) if sp["iden_guid"] else None
        if not old:
            has_add = True
            continue
        used.add(sp["iden_guid"])
        new_qty = Decimal(str(sp["qty"]))
        new_price = Decimal(str(sp["price"])) if sp["price"] not in (None, "") else old["price"]
        new_gift = bool(old.get("gift")) if sp.get("gift") is None else sp["gift"]
        sp["gift"] = new_gift
        sp["price"] = new_price
        # ★ 备注与赠品位都要参与"有没有变"的判定，否则只改备注会被判成未变化直接跳过
        #   —— 用户以为保存了、其实什么都没写（销售侧真出过这个 bug）。
        if (_same_code(old["hpbh"], sp["hpbh"])
                and _same_code(old["unit"], sp["unit"] or old["unit"])
                and old["qty"] == new_qty and old["price"] == new_price
                and (old.get("remark") or "") == sp["remark"]
                and bool(old.get("gift")) == new_gift):
            continue
        sp["unit"] = sp["unit"] or old["unit"]
        deletes.append(old)
        has_replace = True
    # ★ 只有"这次真给了明细"才把没被认领的旧行判成删除。不带 lines 时这一整段必须跳过，
    #   否则一次只改备注的保存会把明细全删了（这就是销售那版 `lines or []` 的坑）。
    if touch_lines:
        for old in old_lines:
            if old["iden_guid"].lower() not in used:
                deletes.append(old)
                has_remove = True

    # 没带单位的行（新行）：取该货品的基本单位，与销售同一查法
    for sp in line_specs:
        if sp["unit"]:
            continue
        row = db.q1(
            cur,
            """SELECT TOP 1 h.hpbh, u.UnitName
               FROM Hpdan h JOIN HpdanUnit u ON h.IdenGuid = u.HpIdenGuid
               WHERE LTRIM(RTRIM(h.hpbh)) = %s ORDER BY u.UnitBasic DESC, u.UnitRate""",
            (sp["hpbh"].strip(),),
        )
        if not row:
            raise ValueError(f"货品 {sp['hpbh']} 无单位")
        sp["hpbh"] = str(row[0])
        sp["unit"] = str(row[1])

    header_changed = (new_ndate != h["ndate"] or new_itembh != h["supplier_code"]
                      or new_note != (h["note"] or ""))
    line_changed = touch_lines and (has_add or has_replace or has_remove)
    if not header_changed and not line_changed:
        # ★ 返回形状与"真改了"那条路径**完全一致**（一张扁平的单 + changed 标记）。
        #   销售那版这里返回的是 {"changed":False,"message":...,"bill":{...}} —— 多套一层，
        #   调用方必须分两种情况取值，前端拿到的是一个没有 lines 键的怪对象。
        bill = get_bill(cur, bill_no)
        bill["changed"] = False
        bill["message"] = "无变化"
        return bill

    # 临时表必须早于**第一条** DML：AFTER 触发器不区分 INSERT/UPDATE/DELETE，
    # 而它读 #Bill / #BillMx（见本文件 622 行那段口径注释）。
    db.ensure_temp(cur, h["guid"], h["idencol"], new_ndate, new_itembh)
    try:
        def _sums():
            t = db.q1(
                cur,
                """SELECT SUM(S_SL), SUM(J_JEZK), SUM(J_JEHS), SUM(J_JE)
                   FROM JHMX WHERE BillGuid = CONVERT(uniqueidentifier, %s)""",
                (h["guid"],),
            )
            return tuple(Decimal(str(x or 0)) for x in t)

        def _apply_header():
            qty_sum, amt_zk, amt_hs, amt_je = _sums()
            cur.execute(
                """UPDATE JH SET NDate=%s, ItemBh=%s, BZ_JH=%s, NoteCode=%s,
                                J_JESUM=%s, J_JEHSSUM=%s, J_JezkSum=%s, J_JEWF=%s,
                                J_SESUM=0, S_SLSUM=%s, S_SLPartSUM=%s, S_SLPackSUM=0,
                                TimeUpdate=%s, UserUpdate=%s
                   WHERE idenguid = CONVERT(uniqueidentifier, %s)""",
                (new_ndate, new_itembh, new_note, new_note,
                 amt_je, amt_hs, amt_zk, amt_zk, qty_sum, qty_sum,
                 _now_str(), _who(), h["guid"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError("单头更新失败")
            return qty_sum, amt_zk, amt_hs, amt_je

        # 单头先落（日期/供应商/备注变化时触发器联动应付与供应商最近进货时间）
        if header_changed:
            _apply_header()

        for old in deletes:
            _delete_line(cur, old)
        for sp in line_specs:
            old = by_guid.get(sp["iden_guid"]) if sp["iden_guid"] else None
            if old:
                same = (_same_code(old["hpbh"], sp["hpbh"])
                        and _same_code(old["unit"], sp["unit"] or old["unit"])
                        and old["qty"] == Decimal(str(sp["qty"]))
                        and old["price"] == sp["price"]
                        and (old.get("remark") or "") == sp["remark"]
                        and bool(old.get("gift")) == bool(sp["gift"]))
                if same:
                    continue
            # ★ ckbh 一律沿用这张单原来的仓库（见函数头 ③）
            _insert_jhmx(cur, cfg, h["guid"], h["ckbh"], sp, sp["pos"])

        # 明细落定后再回写一次合计（触发器按差值净调应付）
        final_sums = _apply_header()
        # 供应商最近进价刷新（销售侧对偶是 HxUpdatePrice_Kh_XSPrice；两个都吃 #BillMx）
        cur.execute("EXEC HxUpdatePrice_Gys_JHPrice @ItemBh = NULL, @BillGuid = %s", (h["guid"],))

        # ---- 事务内自检
        cnt_mx = db.q1(
            cur, "SELECT COUNT(*) FROM JHMX WHERE BillGuid = CONVERT(uniqueidentifier, %s)",
            (h["guid"],))[0]
        # ★ 必须带 BillNaid=1020：JH.idencol 与 XS.idencol 是**两个独立的自增序列**，
        #   只按 BillIden 数会把销售流水一起数进来（建单那边就是这么写的，这里保持一致）。
        cnt_flow = db.q1(
            cur, "SELECT COUNT(*) FROM InvenFlow WHERE BillNaid=1020 AND BillIden=%s",
            (h["idencol"],))[0]
        if cnt_mx != want_rows or cnt_flow != cnt_mx:
            raise RuntimeError(
                f"自检失败：JHMX {cnt_mx} 行 / InvenFlow {cnt_flow} 行（期望 {want_rows}）")
        check = db.q1(
            cur,
            """SELECT (SELECT COUNT(*) FROM JHMX WHERE BillGuid = CONVERT(uniqueidentifier, %s)),
                          j.S_SLSUM, j.J_JEWF
                   FROM JH j WHERE j.idenguid = CONVERT(uniqueidentifier, %s)""",
            (h["guid"], h["guid"]),
        )
        if (check[0] != want_rows or _dec(check[1]) != final_sums[0]
                or _dec(check[2]) != final_sums[1]):
            raise RuntimeError("自检失败：单头合计与明细不一致")
        cur.connection.commit()
    except Exception:
        cur.connection.rollback()
        raise
    bill = get_bill(cur, bill_no)
    bill["changed"] = True
    return bill
