# -*- coding: utf-8 -*-
"""销售出库单(XS) 查询与改单引擎。

改单口径与 V3 正规保存一致：
- 仅未审核、无收款/结算、无关联(订单/退货/收款)的单可改；系统单号不可改；
- 明细任何变化(数量/单价/货品/备注) = 删旧行 + 插新行，逐行 DML（触发器 @@ROWCOUNT=1 限制）；
- 单头允许改：日期/客户/备注；触发器自动冲旧应收、补新应收、刷新价格历史；
- 新增行未给价时按客户取价规则（最近销价 → 默认价格），见 price.py。
"""

from datetime import datetime
from decimal import Decimal
import uuid

from . import actor
from . import idem
from . import price as price_mod
from . import product as product_mod
from . import sql as db
from .master import _like_clause, _page_args, _paged
from .yxparts.errors import BillConflict

# 制单人/审核人/作废人：这条请求是谁就是谁（没注入身份则回落 SYSDEV）
_who = actor.current

# ---------------------------------------------------------------- 写入口径常量
# ★★ 表头时间一律写 `YYYY-MM-DD HH:mm:ss`（不带毫秒），**不要用 GETDATE()**：
#    GETDATE() 返回 datetime 带 3 位毫秒（如 2026-09-20 16:37:59.033），
#    与桌面端/客户历史数据的形态不一致，前端展示还要再截一次。
# ★★ 制单人（ZDR = 制单人）写**这条请求的操作员**，不再写死 `SYSTEM`（2026-09-29）：
#    值从云端中转帧的 actor 进来（= 令牌里的 v3uid，只有 V3 操作员登录才有），
#    没有身份时回落 `SYSDEV` —— 见 v3agent/actor.py。
#    实体列有三处：单头 `zdrbh`、`UserInsert`、`UserUpdate`（作废另记 `UserVoid`）。
#    ★ 库里存的必须是 `UserInfo.UserID` **代码**：printing._zdr_name 拿它换姓名，
#      存成姓名或自造代码，打印出来就是一串裸值。
TIME_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------- 建单幂等
# ★★ 实现已抽到 v3agent/idem.py（2026-09-22 第十轮），销售与采购**共用一份**。
#   原因：采购建单有完全相同的暴露面（重复提交），而「各写一份」必然漂移
#   —— 一边修了另一边忘。完整说明（为什么用前端 token 而不是内容判重、
#   为什么只放内存、原子占位/回填/释放三条铁律）见 idem.py 文件头。
#
# 用法（三步，顺序不能错）：
#   state, replay_no = idem.begin(client_token)   # 在任何写操作之前
#   if state == "replay": return <上次那张单>
#   try:     ...建单...; idem.finish(token, bill_no)   # 必须在 commit 之后
#   except:  idem.abort(token); raise                  # 失败必释放占位


def _now_str():
    """当前时间，`YYYY-MM-DD HH:mm:ss` 字符串（可安全作为 datetime 列入参）。"""
    return datetime.now().strftime(TIME_FMT)


def _dec(v):
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


def _fmt(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, Decimal):
        return float(v)
    return v


def _load_header(cur, bill_no):
    row = db.q1(
        cur,
        # ★★ 仓库名（2026-09-22 用户需求）：XS.Ckbh 存的是 **CkType.Key_**（本库
        #    实测全库唯一值 `'101-'`），中文名在 CkType.Text（`'主仓库'`）。
        #    所以必须 LEFT JOIN CkType ON ck.Key_ = x.Ckbh 才能拿到可读名。
        #    ★ 口径与采购侧 purchase.py 完全一致（那里是 `ck.Key_ = j.Ckbh`，
        #      输出 ck_name / warehouse_name），别再另造 JOIN 条件。
        #    ★ 本库 Ckbh 全为 '101-' 且 100% 命中 CkType（36604/36604），
        #      ISNULL 只为防御「仓库档案被删」的历史脏单。
        """SELECT x.IdenGuid, x.IdenCol, x.BillNo, x.UseNo, x.NDate, x.ItemBh, k.Itemmc,
                  x.Ckbh, x.CkbhTo, x.BillState, x.ISOK, x.IsReceive, x.IsAuditing, x.IsSend,
                  x.Shipped, x.ReconcileTime, x.AuditingTime, x.TimeVoid, x.UserVoid,
                  x.J_JEFK, x.J_JELOSSUM, x.J_FkAdvance, x.J_FkAdvanceDec, x.J_FkVIP,
                  x.J_JEWF, x.J_JEHSSUM, x.S_SLSUM, x.BZ_XS,
                  ISNULL(x.zdrbh,''), ISNULL(x.UserInsert,''), x.TimeInsert,
                  ISNULL(ck.Text,'')
           FROM XS x LEFT JOIN Khdan k ON k.Itembh = x.ItemBh
                     LEFT JOIN CkType ck ON ck.Key_ = x.Ckbh
           WHERE x.BillNo = %s""",
        (bill_no,),
    )
    if not row:
        return None
    (
        guid, iden, billno, useno, ndate, itembh, itemmc, ckbh, ckbhto, bill_state,
        isok, isrecv, isaud, issend, shipped, rtime, atime, tvoid, uvoid,
        jfk, jlos, jadv, jadvdec, jvip, jewf, jehs, slsum, bz_xs,
        zdrbh, user_insert, time_insert, ck_name,
    ) = row
    return {
        "guid": str(guid), "iden": iden, "bill_no": billno, "use_no": useno,
        "ndate": ndate, "item_bh": itembh, "item_mc": itemmc, "ckbh": ckbh,
        # ★ 仓库中文名（CkType.Text）。可能为空（仓库档案被删），下游一律兜底用编码。
        "ck_name": ck_name or "",
        "ckbh_to": ckbhto, "bill_state": bill_state, "isok": isok,
        "is_receive": isrecv, "is_auditing": isaud, "is_send": issend,
        "shipped": shipped, "reconcile_time": rtime, "auditing_time": atime,
        "time_void": tvoid, "user_void": uvoid,
        "j_jefk": jfk, "j_jelossum": jlos, "j_fkadvance": jadv,
        "j_fkadvancedec": jadvdec, "j_fkvip": jvip,
        "j_jewf": jewf, "j_jehssum": jehs, "s_slsum": slsum, "bz_xs": bz_xs,
        "zdr": zdrbh, "user_insert": user_insert, "time_insert": time_insert,
    }


def _load_lines(cur, guid):
    rows = db.qall(
        cur,
        """SELECT m.IdenCol, m.IdenGuid, m.Hpbh, ISNULL(h.hpmc,''), ISNULL(h.ggxh,''), LTRIM(RTRIM(m.HpUnitName)),
                  m.HpUnitRate, m.S_SLUnit, m.S_SL, m.D_DJ, m.D_DJTax, m.J_JEZK,
                  m.IdenIndex, ISNULL(m.JHOrderNo,''), ISNULL(m.LinkPickNo,''),
                  ISNULL(m.S_DepuFin,0), ISNULL(m.S_THSL,0), m.LinkXSBillGuid,
                  ISNULL(m.BZ_XSMX,''), ISNULL(m.Gifts,0)
           FROM XSMX m LEFT JOIN Hpdan h ON h.hpbh = m.Hpbh
           WHERE m.BillGuid = CONVERT(uniqueidentifier, %s)
           ORDER BY m.IdenIndex, m.IdenCol""",
        (guid,),
    )
    out = []
    for r in rows:
        out.append(
            {
                "iden_col": r[0], "iden_guid": str(r[1]), "hpbh": r[2],
                "name": r[3], "spec": r[4], "unit": r[5], "unit_rate": _dec(r[6]),
                "qty_unit": _dec(r[7]), "qty": _dec(r[8]), "price": _dec(r[9]),
                "price_tax": _dec(r[10]), "amount": _dec(r[11]), "index": r[12],
                "jh_order_no": r[13], "link_pick_no": r[14],
                "s_depufin": _dec(r[15]), "s_thsl": _dec(r[16]),
                "link_xs_bill_guid": r[17],
                # ★ 明细行备注：真实列是 **XSMX.BZ_XSMX**（不是单头的 BZ_XS）
                "remark": str(r[18] or ""),
                # ★ 赠品位（XSMX.Gifts，bit）。口径见 _insert_line 的注释：
                #   赠品行**保留单价与金额**，清的是折后金额与含税金额 ⇒ 这行的 `amount`
                #   （读的就是 J_JEZK）本来就是 0，界面上"行有价、合计不含"就是这么来的。
                "gift": bool(r[19]),
            }
        )
    return out


def bill_revision(h, lines):
    """单据**内容指纹**：改单乐观锁的版本号（2026-10-11）。

    ★ 为什么需要它：`salesbill.update` 是**整单覆盖**语义（前端提交"最终明细完整列表"），
      两人同时改同一张单时，后保存者的快照会把前一个人的改动**静默回滚**，
      而双方都收到"保存成功"。实测三场景见 `scripts/probe-edit-conflict.py`。
      库里既没有锁表、也没有编辑态列，`bill.get` 也不返回版本 ⇒ 必须造一个。

    ★ 为什么不用 `XS.TimeUpdate` 当版本号（实测否决）：
      本库 36617 张单里 `TimeUpdate > TimeInsert` 的只有 20 张，且全部是
      `UserInsert=UserUpdate=SYSTEM`（我们 Agent 自己造的）⇒
      **V3 桌面端改单看不出有维护这一列**，拿它当版本号挡不住桌面端的改动。

    ★ 为什么是「内容派生」而不是「时间戳」：所有端点都会改内容 ⇒ 指纹必然变；
      反之没改内容指纹就不变，不会有假冲突。这是唯一不依赖"任一端自觉"的方案。

    ★★ 只取**前端真正会改的字段**，绝对排除下面这些（否则会误判冲突）：
      · `ReconcileTime`  —— 能改的单该列必空（已对账的不可改），
        但 V3 在别处做对账动作会**单方面改动**它；
      · `IsAuditing` / `IsReceive` / `IsSend` / `UserVoid` / `TimeVoid` ——
        审核/收款/发货/作废是**别的端点的职责**，不是"这条单的内容被改"；
      · `TimeUpdate` / `UserUpdate` / 合计列 —— 由触发器/本函数自己改写。
    """
    import hashlib

    parts = [
        # 单头：只有这三个字段允许改（见 update_bill 的字段白名单）
        "H", _fmt(h.get("ndate")), str(h.get("item_bh") or ""), str(h.get("bz_xs") or ""),
    ]
    for x in lines:
        parts.append("|".join([
            "L",
            str(x.get("iden_guid") or "").lower(),   # ★ 行身份：改单换 guid，必然进指纹
            str(x.get("hpbh") or "").strip(),
            str(x.get("unit") or ""),
            str(_dec(x.get("qty"))),
            str(_dec(x.get("price"))),
            str(x.get("remark") or ""),
            "1" if x.get("gift") else "0",
        ]))
    raw = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def get_bill(cur, bill_no):
    h = _load_header(cur, bill_no)
    if h is None:
        raise LookupError(f"单据不存在：{bill_no}")
    lines = _load_lines(cur, h["guid"])
    reasons = editable_reasons(h, lines)
    return {
        "bill_no": h["bill_no"], "use_no": h["use_no"],
        "ndate": _fmt(h["ndate"]), "bz_xs": h["bz_xs"] or "",
        # ★ 表头时间与制单人：前端/打印层读这两个字段，口径与写入口径一致
        "time_insert": _fmt(h.get("time_insert")),
        "zdr": h.get("zdr") or h.get("user_insert") or "",
        "customer": {"code": h["item_bh"], "name": h["item_mc"]},
        # ★ 发货仓库：warehouse 仍是编码（保持既有契约不破坏调用方），
        #   新增 warehouse_name = CkType.Text（可读中文名，可能为空）。
        "warehouse": h["ckbh"], "warehouse_name": h.get("ck_name") or "",
        "bill_state": h["bill_state"],
        "is_auditing": h["is_auditing"], "is_receive": h["is_receive"],
        "is_send": h["is_send"], "shipped": h["shipped"],
        # ★ 2026-09-20：真实状态词 + ISOK 原义 + 作废信息（与列表同源 _status_of）
        "status": _status_of(h["is_auditing"], h["user_void"], h["time_void"]),
        "is_ok": bool(h["isok"]),
        "is_draft": (not h["isok"]) and not _void_flag(h["user_void"], h["time_void"]),
        "user_void": h["user_void"] or "", "time_void": _fmt(h["time_void"]),
        "reconcile_time": _fmt(h["reconcile_time"]), "auditing_time": _fmt(h["auditing_time"]),
        "totals": {"amount": float(h["j_jewf"]), "amount_hs": float(h["j_jehssum"]), "qty": float(h["s_slsum"])},
        "lines": [
            {
                "iden_guid": x["iden_guid"], "index": x["index"], "hpbh": x["hpbh"],
                "name": x["name"], "spec": x.get("spec", ""), "unit": x["unit"],
                "qty": float(x["qty"]),
                # ★ 多单位：`qty` 是**基本单位数量**（库存与成本口径），
                #   界面上"3 包"要显示成 3 包，就得同时拿到 `qty_unit` 与**当时倍率**。
                #   _load_lines 早就把这两列查出来了，是这个投影把它们丢了 ——
                #   前端只能自己拿现在的倍率回算，而倍率是可改的，历史单据就会算错。
                "qty_unit": float(x.get("qty_unit") or 0),
                "unit_rate": float(x.get("unit_rate") or 0),
                "price": float(x["price"]), "amount": float(x["amount"]),
                # ★ 明细行备注（XSMX.BZ_XSMX）—— 详情页要展示它
                "remark": x.get("remark", ""),
                "gift": bool(x.get("gift")),
            }
            for x in lines
        ],
        "editable": not reasons,
        "editable_reasons": reasons,
        # ★ 改单乐观锁的版本号（2026-10-11）：改单页打开时记下它，保存时原样回传；
        #   `salesbill.update` 比对不符即拒（BILL_CONFLICT）。见 bill_revision 的长注释。
        "revision": bill_revision(h, lines),
        # ★ 能力标记：前端/调试台可以据此判断这一版 Agent 是否支持乐观锁。
        #   若某天有人跳到旧版云端，看到没有 revision 就能立刻定位"乐观锁静默失效"。
        "supports": ["revision"],
    }


def editable_reasons(h, lines):
    reasons = []
    if h["bill_state"] != 0:
        reasons.append("单据状态非正常(作废/红冲)")
    # ★★ ISOK 不是「作废位」！真实语义（2026-09-20 用户纠正 + 本库实证）：
    #     ISOK=1 正式单 / ISOK=0 草稿单，**两者都还是未审核**；
    #     作废时 ISOK 置 0 并写入 UserVoid(作废人) + TimeVoid(作废时间)。
    #    所以「草稿」和「作废」都是 ISOK=0，区分只能靠 UserVoid/TimeVoid。
    #    旧代码这里写 `if not h["isok"]: reasons.append("ISOK=0")`，
    #    把 9 张草稿单误判成不可改 —— 草稿本来就该能改。
    if h["time_void"] or h["user_void"]:
        reasons.append("已作废")          # ← 真正的作废位
    if h["is_auditing"]:
        reasons.append("已审核")
    if h["is_receive"]:
        reasons.append("已收款")
    if h["is_send"]:
        reasons.append("已发送")
    if h["shipped"]:
        reasons.append("已发货")
    if h["reconcile_time"]:
        reasons.append("已对账")
    if h["auditing_time"]:
        reasons.append("已有审核时间")
    for k in ("j_jefk", "j_jelossum", "j_fkadvance", "j_fkadvancedec", "j_fkvip"):
        if _dec(h[k]) != 0:
            reasons.append("存在收款/结算金额")
            break
    for x in lines:
        if x["jh_order_no"] or x["link_pick_no"] or x["s_depufin"] or x["s_thsl"] or x["link_xs_bill_guid"]:
            reasons.append(f"明细 {x['hpbh']} 有关联(订单/拣货/退货/委托)")
            break
    return reasons


# ------------------------------------------------------------------ 单据列表

# 列表筛选：前端 tab → SQL 条件。
# ★★ 用「审核状态」(IsAuditing) 而不是「单据状态」(BillState) ——
#    前端 tab 的语义是「未审核/已审核」，与 BillState(0=正常/-1=红冲) 是两回事。
# ★ 这几条与采购（purchase._STATE_SQL）**同一套词汇**：
#    线上词表只有 pending / audited / void 三个，别在别的层再造新词。
#
# ★★★ 2026-09-20 纠正（用户口径 + 本库实证）：
#    ISOK **不是**「作废位」。真实语义：
#        ISOK=1  → 正式单，未审核
#        ISOK=0  → 草稿单，未审核
#        作废    → ISOK 置 0，并写 UserVoid(作废人) + TimeVoid(作废时间)
#    即「草稿」与「作废」**都是 ISOK=0**，只能用 UserVoid/TimeVoid 区分。
#    旧写法 `void: ISNULL(x.ISOK,1)=0` 把 9 张草稿单全算成作废，
#    而 2 张真作废单（UserVoid='SYSTEM'）反而混在里面 —— 两个 tab 都错。
#
#    本库实测（XS 共 80 张）：
#        旧口径：pending 68 / audited 1 / void 11
#        新口径：pending 79 / audited 1 / void 2
#    差异 11 张：9 张草稿回归 pending（本来就该能改/能审），2 张真作废进 void。
#
# ★ `void` 的两列都判：UserVoid 是 NOT NULL 默认 ''，
#    TimeVoid 是 NULL；只判长度或只判 NULL 都会漏。两个用 OR 兜住。
_VOID_SQL = "(ISNULL(x.UserVoid,'')<>'' OR x.TimeVoid IS NOT NULL)"

_STATE_SQL = {
    # 未审核 = 正式未审核(ISOK=1) + 草稿(ISOK=0)，两种 ISOK 都算；
    # 只要不是「已作废」就归 pending。ISOK 在这里已无判别作用。
    "pending": "ISNULL(x.IsAuditing,0)=0 AND NOT " + _VOID_SQL,
    # ★ 2026-09-23：已审核也要排除作废 —— 库里存在「IsAuditing=1 且已作废」的历史组合
    #   （作废前的审核位没被清），不排除的话「已审核」tab 会混进作废单。
    "audited": "x.IsAuditing=1 AND NOT " + _VOID_SQL,
    "void": _VOID_SQL,
    # ★★★ 2026-09-23（需求）：作废单**默认不进「全部」**。
    #   前端「全部」tab 与首页「最近销售单」都**不发 state**（state 为空），
    #   而缺省语义现在 = 「未作废的全部」（pending + audited）。
    #   作废单只在显式筛「已作废」(state=void) 时才出现。
    "all": "NOT " + _VOID_SQL,
}

# ★ 兼容别名：早期 H5 包 / 小程序包发的是 unapproved / approved。
#   别名**不能删** —— 删了那两个还在跑的客户端会直接 500。
_STATE_ALIAS = {
    "unapproved": "pending", "unaudited": "pending", "unchecked": "pending",
    "approved": "audited", "audited": "audited",
    "voided": "void", "canceled": "void", "cancelled": "void",
    "all": "all",
}


def _state_clause(state):
    """归一化 state → (SQL 条件 or None, 额外参数列表)。

    ★★★ 这里就是「销售列表查未审核/已审核报
        invalid literal for int() with base 10: 'unapproved'」的根因。
        老实现直接 `int(state)`，把前端传的字符串 token 当 BillState 数字用：
          - 类型上 → ValueError → 整个 op 失败 → 500
          - 语义上 → BillState 是「单据状态」，跟「审核状态」不是一个概念

    ★ 未知 token **显式抛错**，不静默返回全量。
      静默是最坑的：前端参数写错会表现成「筛选没实现」，要查三层才能定位。
      宁可 500 并给出可选值，也不要给一个看起来正常的错结果。

    ★★★ 2026-09-23：缺省（前端「全部」tab / 首页「最近销售单」都不发 state）
      默认**排除作废单**，与 state=all 同义；作废单只在 state=void 时返回。
    """
    if state is None or state == "":
        state = "all"
    s = str(state).strip().lower()
    if s in _STATE_ALIAS:
        s = _STATE_ALIAS[s]
    if s in _STATE_SQL:
        return _STATE_SQL[s], []
    # 向后兼容：老口径的纯数字 BillState（0=正常 / -1=红冲 …）
    if s.lstrip("-").isdigit():
        return "x.BillState = %s", [int(s)]
    raise ValueError(
        "未知的 state：%r（可选 pending/audited/void，兼容 unapproved/approved）" % (state,)
    )


def _void_flag(user_void, time_void):
    """真作废判定：UserVoid 有值 或 TimeVoid 非空。

    ★ 供列表/详情共用，改口径只改这一处，别在两处各写一遍。
    """
    return bool((user_void or "").strip()) or (time_void is not None)


def _status_of(is_auditing, user_void, time_void):
    """收敛成前端词表的 pending / audited / void（与采购同源）。

    ★ 顺序很重要：先判作废，再判审核。
      作废单 IsAuditing 通常是 0，但不能假设 —— 万一一张单先审后废，
      按「作废优先」才符合用户直觉（已作废的单不该出现在已审核 tab）。
    """
    if _void_flag(user_void, time_void):
        return "void"
    return "audited" if is_auditing else "pending"


def _row_to_bill(r):
    return {
        "bill_no": r[0], "use_no": r[1], "ndate": _fmt(r[2]),
        "customer": {"code": r[3], "name": r[4]},
        "is_auditing": bool(r[5]), "bill_state": r[6], "is_ok": bool(r[7]),
        "amount": float(r[8] or 0), "qty": float(r[9] or 0),
        # ★ 2026-09-20 新增：真实状态词 + 草稿标记 + 作废信息。
        #   `status` 是前端应优先使用的字段（与采购列表同名列，口径一致）；
        #   `is_ok` 保留原义（ISOK 位），前端**不要**再拿它当「是否作废」。
        "status": _status_of(r[5], r[10], r[11]),
        "is_draft": (not r[7]) and not _void_flag(r[10], r[11]),
        "user_void": r[10] or "", "time_void": _fmt(r[11]),
        # ★ 仓库编码 + 中文名（CkType.Text）。列表项也带，供列表/打印展示。
        "warehouse": r[12] or "", "warehouse_name": r[13] or "",
        # ★★ 2026-10-10 补：明细行数（「N 种」）。与采购列表**同一口径**
        #   （purchase.py 的 `line_count`）—— 列表接口为性能不带明细数组，
        #   行数只能在 SQL 里用相关子查询数出来，前端读 items.length 恒为 0。
        #   前端 BillRow 拿它拼「N 种 · M 件」；缺了就只能显示「M 件」，
        #   于是出现「采购有几种几件、销售只有几件」这种两页不一致。
        "line_count": int(r[14] or 0),
    }


def list_bills(cur, date_from=None, date_to=None, customer=None, keyword=None,
               state=None, limit=100, offset=0):
    """销售出库单(XS) 分页列表。

    ★ 返回 **{items,total,limit,offset,hasMore}**（与商品/客户/采购一致），
      不再返回裸数组 —— 之前前端拿不到 total，「加载更多」永远不出现，
      等于列表没有分页能力。

    ★★ 排序 `NDate DESC, Idencol DESC`，用 `ROW_NUMBER()`：
       目标库是 **SQL Server 2008 R2**，不支持 OFFSET/FETCH。
       单用 Idencol 排序在补录单场景会跟用户看到的日期顺序不一致。

    ★★★ 【不要尝试的两段式优化】（2026-10-12 实测否决，教训要留着）
       压测发现本查询 p50 ≈ 62ms，而 `ORDER BY NDate DESC` 单键只要 0.6ms
       —— 差 100 倍，因为 XS 聚集索引 `IX_XS_NDate` **只含 NDate**，
       加上第二键 Idencol 就无法用索引排序，退化成 36615 行全表排序。
       看起来"先用单键索引定位日期边界、再在窄集合内双键排序"能白拿 50 倍，
       实测也确实快（0.6~1.8ms）。**但它是错的**，已否决，原因是：

         · 单键序（NDate DESC）与双键序（NDate DESC, Idencol DESC）
           在**跨天的补录单**上结果不同。本库实证：
             Idencol=36667 → NDate=2026-09-21（补录）
             Idencol=36668 → NDate=2026-09-19（补录）
           双键序把 36668 排在 36667 之前（Idencol 更大），
           单键序把 09-21 排在 09-19 之前（日期更新）——**顺序相反**。
         · 所以"第一段用单键定位的边界"与"第二段双键编号"对不上，
           实测 offset=0/20/100/500 四档全部与旧实现**不一致**（整页错位）。
         · 陷阱在于：`TOP 1000` 这种截断对比会显示"完全一致"，
           因为差异只出现在被截断的位置附近 —— 必须比对**整页**才不会误判。
         · 真要提速只能给 XS 加 `(NDate DESC, Idencol DESC)` 索引，
           但 XS 是 V3 出厂表、聚集索引由桌面端共用，**不能动结构**。
       ⇒ 结论：接受这 62ms。列表页有分页（limit 默认 100），
         且连接复用已经把建连开销消掉，这条是当前可优化的极限。

    ★ 关键字要能搜到「客户名称」，而客户名在 Khdan 里，
      所以子查询必须带这个 JOIN，外层再取一次是为了拿 Itemmc。
    """
    limit, offset = _page_args(limit, offset)
    where, args = ["1=1"], []
    if date_from:
        where.append("x.NDate >= %s")
        args.append(str(date_from))
    if date_to:
        where.append("x.NDate < DATEADD(day, 1, %s)")
        args.append(str(date_to))
    if customer:
        where.append("x.ItemBh = %s")
        args.append(str(customer))

    st_where, st_args = _state_clause(state)
    if st_where:
        where.append(st_where)
        args.extend(st_args)

    kw = str(keyword or "").strip()
    if kw:
        # ★ 客户编号 x.ItemBh 也要搜：用户按客户编码找单是常态（采购那边就漏过）
        where.append(_like_clause(
            ["x.BillNo", "x.Useno", "x.ItemBh", "k.Itemmc", "ISNULL(x.BZ_XS,'')"],
            kw, args))

    clause = " AND ".join(where)

    total = db.q1(
        cur,
        "SELECT COUNT(*) FROM XS x LEFT JOIN Khdan k ON k.Itembh = x.ItemBh WHERE " + clause,
        tuple(args),
    )[0]

    # ★ 只做一次 % 运算（clause/offset/limit 一起喂）。
    #   分两步 `% clause` 再 `% (...)` 会把 clause 里给 pymssql 的 %s
    #   当成格式符再解析一次 → "not enough arguments for format string"。
    rows = db.qall(
        cur,
        """SELECT t.BillNo, t.Useno, t.NDate, t.ItemBh, ISNULL(t.Itemmc,''),
                  t.IsAuditing, t.BillState, t.ISOK, t.J_JEWF, t.S_SLSUM,
                  ISNULL(t.UserVoid,''), t.TimeVoid,
                  ISNULL(t.Ckbh,''), ISNULL(t.ck_name,''), ISNULL(t.line_count,0)
           FROM (
               SELECT x.BillNo, x.Useno, x.NDate, x.ItemBh, k.Itemmc,
                      x.IsAuditing, x.BillState, x.ISOK, x.J_JEWF, x.S_SLSUM,
                      x.UserVoid, x.TimeVoid, x.Ckbh, ck.Text AS ck_name,
                      (SELECT COUNT(*) FROM XSMX m
                        WHERE m.BillGuid = CONVERT(uniqueidentifier, x.IdenGuid)) AS line_count,
                      ROW_NUMBER() OVER (ORDER BY x.NDate DESC, x.Idencol DESC) AS rn
               FROM XS x LEFT JOIN Khdan k ON k.Itembh = x.ItemBh
                         LEFT JOIN CkType ck ON ck.Key_ = x.Ckbh
               WHERE %s
           ) t
           WHERE t.rn > %d AND t.rn <= %d
           ORDER BY t.rn""" % (clause, offset, offset + limit),
        tuple(args),
    )
    return _paged(rows, total, limit, offset, _row_to_bill)


# ------------------------------------------------------- 某货品的最近销售明细
#
# ★★ 前端曾经这样取「最近销售数据」：拉 `bill.list`（销售单**列表**）然后
#    在每张单里找 `lines[]` —— **列表项根本不含 lines**（契约只给
#    line_count / qty），于是恒为空数组，页面永远显示「暂无历史销售记录」。
#    列表接口为了性能不会带明细，这是设计如此，不是 bug。
#    正确做法是新增一个按货品直接查明细行的 op（下面这个）。
def product_sales_history(cur, hpbh, limit=8, customer=None):
    """某货品(Hpbh)的最近 N 条销售明细行（真实明细表 XSMX JOIN 单头 XS）。

    返回 [{bill_no, use_no, ndate, customer{code,name}, unit, qty, price, amount}]
    排序 `NDate DESC, m.IdenCol DESC`（与列表页一致）。
    ★ 已作废单默认排除（作废单的价格没有参考意义）。
      ★ 2026-09-20 修正：排除条件原来是 `ISNULL(h.ISOK,1)=1`，
        那是把「草稿(ISOK=0)」也当成了作废 —— 草稿单的价格一样没有参考意义，
        但真正的判据是 UserVoid/TimeVoid，不是 ISOK。改为按作废位排除，
        语义更准；对 `ISOK=1` 的正式单行为不变。
    ★ `Hpbh` 在主档里可能带前导/尾随空格，一律 `LTRIM(RTRIM(...))` 比对。
    """
    code = str(hpbh or "").strip()
    if not code:
        raise ValueError("缺少 hpbh")
    limit, _ = _page_args(limit, 0)

    where = ["LTRIM(RTRIM(m.Hpbh)) = %s",
             "(ISNULL(h.UserVoid,'')='' AND h.TimeVoid IS NULL)"]
    args = [code]
    if customer:
        where.append("h.ItemBh = %s")
        args.append(str(customer).strip())
    clause = " AND ".join(where)

    # ★ 只做一次 % 运算（`clause` 里给 pymssql 的 %s 不能再被当格式符解析）。
    rows = db.qall(
        cur,
        """SELECT TOP %d h.BillNo, h.UseNo, h.NDate, h.ItemBh, ISNULL(k.Itemmc,''),
                  ISNULL(m.HpUnitName,''), m.S_SL, m.D_DJTax, m.J_JEHS
           FROM XSMX m
           JOIN XS h ON h.IdenGuid = m.BillGuid
           LEFT JOIN Khdan k ON k.Itembh = h.ItemBh
           WHERE %s
           ORDER BY h.NDate DESC, m.IdenCol DESC""" % (limit, clause),
        tuple(args),
    )
    out = []
    for r in rows:
        qty = _dec(r[6])
        price = _dec(r[7])
        amount = _dec(r[8])
        if amount <= 0 and qty > 0 and price > 0:
            amount = qty * price
        out.append({
            "bill_no": r[0],
            "use_no": r[1],
            "ndate": _fmt(r[2]),
            "customer": {"code": r[3], "name": r[4]},
            "unit": r[5],
            "qty": float(qty),
            "price": float(price),
            "amount": float(amount),
        })
    return {"hpbh": code, "items": out}


def _unit(cur, hpbh, unit):
    """取该货品的某个单位行（名字、倍率、是否基本单位）。

    ★ 2026-09-27：这里原先硬拒 `UnitRate != 1`（"暂不支持，待验证多单位逻辑"），
      等于把 V3 的子表多单位建模在开单侧堵死 —— 资料页能填"1 包 = 10 支"，
      开单却只能按支卖。现在放开，但**只放开这一条**：
      单位必须真实存在于 HpdanUnit、且没被停用，倍率取库里的值，
      明细同时写"当时单位数量 + 当时倍率 + 基本单位数量"，
      所以以后改了倍率也不会回头影响已开的单。
    """
    row = db.q1(
        cur,
        """SELECT h.hpbh, u.UnitName, u.UnitRate, u.UnitBasic, ISNULL(u.UnitStop,0)
           FROM Hpdan h JOIN HpdanUnit u ON h.IdenGuid = u.HpIdenGuid
           WHERE LTRIM(RTRIM(h.hpbh)) = %s AND LTRIM(RTRIM(u.UnitName)) = %s""",
        (hpbh.strip(), unit.strip()),
    )
    if not row:
        raise ValueError(f"货品 {hpbh} 没有「{unit}」这个单位（先在产品资料里加，或改用别的单位）")
    if int(row[4] or 0):
        raise ValueError(f"货品 {hpbh} 的单位「{unit}」已停用，不能再用它开单")
    if _dec(row[2]) <= 0:
        raise ValueError(f"货品 {hpbh} 单位 {unit} 的换算率是 {row[2]}，不合法（先去产品资料改对）")
    return row


def _insert_line(cur, cfg, h, item, position, price_from=None, scheme=None):
    """新增/替换明细行。item: {hpbh, unit, qty, price?, remark?}

    ★ `remark` 落到 **XSMX.BZ_XSMX**（明细行备注），不是单头的 BZ_XS。
      之前这里完全没有写这个列 —— 前端就算传了 remark 也会被静默丢弃。
    ★ scheme: 本单「价格方案」覆盖 {price_trace, price_type_xs}（可空 = 按客户档案）。
    """
    cur.execute("TRUNCATE TABLE #OutGuid")
    hpbh, unit = item["hpbh"].strip(), (item.get("unit") or "").strip()
    urow = _unit(cur, hpbh, unit)
    hpbh = str(urow[0])  # 主档规范编号（可能含空格）
    unit = str(urow[1])  # 单位名以库里的写法为准（大小写/空格差异不该留进单据）
    rate = _dec(urow[2]) or Decimal("1")
    qty = Decimal(str(item["qty"]))
    # ★ 三个数量列的口径（XSMX 就是这么建的）：
    #   S_SLUnit = **当时这个单位的数量**（客户看到并填的 3 包）
    #   S_SL     = **折成基本单位的数量**（3 包 × 10 = 30 支，库存与成本按它走）
    #   拆分逻辑在 product.split_qty —— 与销售/采购共用，两边不许各写一份。
    qty_unit, part, base_qty = product_mod.split_qty(qty, rate)
    remark = str(item.get("remark") or "")
    if len(remark) > 200:
        raise ValueError(f"货品 {hpbh} 明细备注过长（最多 200 字）")
    if qty <= 0:
        raise ValueError(f"货品 {hpbh} 数量必须大于 0")
    if item.get("price") in (None, ""):
        if price_from is None:
            price_from = price_mod.resolve_price(
                cur, cfg, h["item_bh"], hpbh, unit,
                price_trace=(scheme or {}).get("price_trace"),
                price_type_xs=(scheme or {}).get("price_type_xs"),
            )
        price = price_from["price"]
        if price_from.get("manual"):
            price = Decimal("0")
    else:
        price = Decimal(str(item["price"]))
        price_from = {"source": "user", "manual": False}
    if price < 0:
        raise ValueError(f"货品 {hpbh} 单价不能为负")
    amt = (qty * price).quantize(Decimal("0.01"))
    # ★★ 赠品（XSMX.Gifts）的口径是**照 V3 桌面端实测来的**，不是我们定的：
    #   样本 geshi/XK00000003 的赠品行 `D_DJ=6 / J_JE=6 / D_DJZK=6` 全部保留，
    #   清零的是 **`J_JEZK`（折后金额）与 `J_JEHS`（含税金额）**，数量 `S_SL` 照记（库存照扣）。
    #   ⇒ "不进合计"是靠这两列实现的：单头四个 SUM 本来就按 SUM(J_JEZK)/SUM(J_JEHS) 回写，
    #     所以这里清零，头表的应收/含税自动不含赠品，一行求和代码都不用改。
    #   ★ 不要"顺手"把单价也写成 0 —— 那样 J_JESUM 会比 V3 自己录的单少一截，
    #     赠品价值也没了（以后想统计送了多少货就查不出来了）。
    gift = bool(item.get("gift"))
    amt_zk = Decimal("0.00") if gift else amt     # 折后金额（= 计入应收的那一列）
    # ★ `J_JEZKE` 只在赠品行填货值，正常行留 0 —— 这条也是量出来的，不是猜的：
    #   geshi 整册 7 行明细里 `J_JEZKE<>0` 的**只有那一行赠品**（=6=J_JE），
    #   tongda 95,987 行全是 0（那边一笔折扣都没有）⇒ 厂商把它当"被折掉/送掉的那笔钱"单列存。
    #   我们以前整列不写 ⇒ 手机开的赠品单在 V3 里查不出"送了多少货"，
    #   而界面合计/应收都不读这列（头表四个 SUM 用 J_JEZK/J_JEHS/J_JE），写它零风险。
    amt_zk_e = amt if gift else Decimal("0.00")
    cur.execute(
        """INSERT INTO XSMX (BillGuid, Hpbh, HpUnitName, HpUnitRate, S_SLUnit, S_SLPart, S_SL,
            S_Stock, D_DJCurr, D_DJ, D_DJTax, D_DJEx, J_JE, J_JEEx, D_DJZK, J_JEZK, J_JEZKE,
            J_JEHS, J_JEML, Ckbh, ISOK, IdenIndex, BZ_XSMX, Gifts)
           OUTPUT INSERTED.IdenGuid INTO #OutGuid
           VALUES (CONVERT(uniqueidentifier, %s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                   %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s)""",
        (
            # ★ HpUnitRate 以前是写死的字面量 1（多单位被堵死的地方就在这）。
            #   现在写**当时从 HpdanUnit 取到的倍率**，所以日后改了倍率也不会回头
            #   影响这张已开的单 —— 这正是明细要同时存单位名与倍率的原因。
            h["guid"], hpbh, unit, rate, qty_unit, part, base_qty, base_qty,
            price, price, price, price,
            amt, amt, price, amt_zk, amt_zk_e, amt_zk, amt, h["ckbh"], position, remark,
            1 if gift else 0,
        ),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"插入明细失败：{hpbh}")
    row = db.q1(cur, "SELECT TOP 1 IdenGuid FROM #OutGuid")
    if not row:
        raise RuntimeError(f"插入明细后未取到 IdenGuid：{hpbh}")
    new_guid = str(row[0])
    return {
        "hpbh": hpbh, "unit": unit, "qty": qty, "price": price,
        # ★ 回给前端"当时倍率 + 折成基本单位的数量"，界面上的"折合 30 支"才不是
        #   前端自己乘出来的假数（前端拿的倍率可能与库里不一致）。
        "rate": _dec(rate), "qty_unit": _dec(qty), "qty_base": _dec(base_qty),
        "amount": amt, "source": price_from.get("source", "user"),
        "iden_guid": new_guid, "remark": remark, "gift": gift,
        # 计入应收的那一口（赠品行=0），前端改单回读时不必自己再推一遍
        "amount_zk": amt_zk,
    }


def _delete_line(cur, line):
    if line["jh_order_no"] or line["link_pick_no"] or line["s_depufin"] or line["s_thsl"] or line["link_xs_bill_guid"]:
        raise ValueError(f"明细 {line['hpbh']} 有关联(订单/拣货/退货/委托)，不能删除或修改")
    row = db.q1(cur, "SELECT IdenCol FROM XSTHMX WHERE JHGuid = CONVERT(uniqueidentifier, %s)", (line["iden_guid"],))
    if row:
        raise ValueError(f"明细 {line['hpbh']} 被退货单关联，不能删除或修改")
    cur.execute("DELETE FROM XSMX WHERE IdenGuid = CONVERT(uniqueidentifier, %s)", (line["iden_guid"],))
    if cur.rowcount != 1:
        raise RuntimeError(f"删除明细失败：{line['hpbh']}")


def update_bill(cur, cfg, bill_no, header=None, lines=None, expect_revision=None):
    """改单入口。header: {NDate?, ItemBh?, BZ_XS?}；lines: 最终明细完整列表。

    `expect_revision`（可选，2026-10-11）：前端打开改单页时从 `bill.get` 拿到的
    `revision`。**传了就启用乐观锁**：与当前库内指纹不符即拒改（BILL_CONFLICT），
    避免"两人同时改同一张单、后保存者静默回滚前者的改动"。
    ★ 不传 = 完全走原行为（V3 桌面端与老版前端不受影响，向后兼容）。
    """
    h = _load_header(cur, bill_no)
    if h is None:
        raise LookupError(f"单据不存在：{bill_no}")
    old_lines = _load_lines(cur, h["guid"])
    reasons = editable_reasons(h, old_lines)
    if reasons:
        raise ValueError("单据不允许修改：" + "；".join(reasons))

    # ★ 乐观锁：必须在做任何写操作**之前**比对（放在后面的 `ensure_temp` 之后就没意义了）
    if expect_revision not in (None, ""):
        cur_rev = bill_revision(h, old_lines)
        if str(expect_revision).strip() != cur_rev:
            raise BillConflict(
                "这张单已被他人修改，你手上的还是旧内容。请退出后重新打开再改，"
                "否则会把对方的改动覆盖掉。",
                current_revision=cur_rev,
                updated_at=_fmt(h.get("time_insert")),
            )

    header = header or {}
    invalid = set(header) - {"NDate", "ItemBh", "BZ_XS"}
    if invalid:
        raise ValueError(f"单头不允许修改字段：{'、'.join(sorted(invalid))}（系统单号不可改）")

    new_ndate, new_itembh, new_bz = h["ndate"], h["item_bh"], h["bz_xs"] or ""
    if "NDate" in header:
        raw = str(header["NDate"]).strip()
        try:
            new_ndate = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            new_ndate = datetime.strptime(raw[:10], "%Y-%m-%d")
    if "ItemBh" in header and header["ItemBh"] != h["item_bh"]:
        cust = db.q1(cur, "SELECT Itembh FROM Khdan WHERE Itembh = %s", (header["ItemBh"],))
        if not cust:
            raise ValueError(f"客户不存在：{header['ItemBh']}")
        new_itembh = str(header["ItemBh"]).strip()
    if "BZ_XS" in header:
        new_bz = str(header["BZ_XS"] or "")

    lines = lines or []
    line_specs = []
    for i, it in enumerate(lines, 1):
        guid = (str(it.get("iden_guid") or "")).strip().lower()
        hpbh = str(it.get("hpbh") or "").strip()
        if not hpbh:
            raise ValueError(f"第 {i} 行缺货品")
        if it.get("qty") in (None, ""):
            raise ValueError(f"第 {i} 行缺数量")
        if Decimal(str(it.get("qty"))) <= 0:
            raise ValueError(f"第 {i} 行数量必须大于 0")
        line_specs.append(
            {
                "pos": i,
                "iden_guid": guid,
                "hpbh": hpbh,
                "unit": str(it.get("unit") or "").strip() or None,
                "qty": it.get("qty"),
                "price": it.get("price"),
                # ★ 明细行备注：落 XSMX.BZ_XSMX
                "remark": str(it.get("remark") or ""),
                # ★ 赠品位：**故意留 None**（不像 remark 那样转成空串）——
                #   改单时"没带这一键"要能退回原行的值，"带了 false"才是真的取消赠品。
                "gift": None if it.get("gift") is None else bool(it.get("gift")),
            }
        )

    by_guid = {x["iden_guid"].lower(): x for x in old_lines}
    used = set()
    deletes = []
    has_add = has_replace = has_remove = False
    for sp in line_specs:
        old = by_guid.get(sp["iden_guid"]) if sp["iden_guid"] else None
        if old:
            used.add(sp["iden_guid"])
            price_given = sp["price"] not in (None, "")
            new_price = Decimal(str(sp["price"])) if price_given else old["price"]
            new_qty = Decimal(str(sp["qty"]))
            # ★ 没带 gift 这一键 = 不动它（退回原行的值）；带了 false 才是"取消赠品"。
            new_gift = bool(old.get("gift")) if sp.get("gift") is None else sp["gift"]
            sp["gift"] = new_gift
            # ★ 备注也要参与「是否有变化」判定 —— 否则只改备注时会被判成
            #   「未变化」直接跳过，用户以为保存了、其实什么都没写。
            #   赠品位同一个道理：只勾/只取消赠品时必须真的重写这一行。
            if (
                old["hpbh"] == sp["hpbh"] and old["unit"] == (sp["unit"] or old["unit"])
                and old["qty"] == new_qty and old["price"] == new_price
                and (old.get("remark") or "") == sp["remark"]
                and bool(old.get("gift")) == new_gift
            ):
                continue  # 未变化
            deletes.append(old)
            has_replace = True
        else:
            has_add = True
    for sp in line_specs:
        if not sp["unit"]:
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
    for old in old_lines:
        if old["iden_guid"].lower() not in used:
            deletes.append(old)
            has_remove = True
    header_changed = (
        new_ndate != h["ndate"] or new_itembh != h["item_bh"] or new_bz != (h["bz_xs"] or "")
    )
    line_changed = has_add or has_replace or has_remove
    if not header_changed and not line_changed:
        return {"changed": False, "message": "无变化", "bill": get_bill(cur, bill_no)}

    db.ensure_temp(cur, h["guid"], h["iden"], new_ndate, new_itembh)
    try:
        def _sums():
            t = db.q1(
                cur,
                """SELECT SUM(S_SL), SUM(J_JEZK), SUM(J_JEHS), SUM(J_JE)
                   FROM XSMX WHERE BillGuid = CONVERT(uniqueidentifier, %s)""",
                (h["guid"],),
            )
            return tuple(Decimal(str(x or 0)) for x in t)

        def _apply_header():
            qty_sum, amt_zk, amt_hs, amt_je = _sums()
            cur.execute(
                """UPDATE XS SET NDate=%s, ItemBh=%s, BZ_XS=%s,
                                J_JESUM=%s, J_JEHSSUM=%s, J_JEExSUM=%s, J_JEZKSUM=%s, J_JEWF=%s,
                                S_SLUnitSUM=%s, S_SLPartSUM=%s, S_SLSUM=%s,
                                TimeUpdate=%s, UserUpdate=%s
                   WHERE IdenGuid = CONVERT(uniqueidentifier, %s)""",
                (new_ndate, new_itembh, new_bz, amt_je, amt_hs, amt_je, amt_zk, amt_zk,
                 qty_sum, qty_sum, qty_sum, _now_str(), _who(), h["guid"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError("单头更新失败")
            return qty_sum, amt_zk, amt_hs, amt_je

        # 单头先落（日期/客户/备注变化时触发器联动应收与最近价）
        if header_changed:
            _apply_header()

        # 先删（含被替换的旧行）
        for old in deletes:
            _delete_line(cur, old)
        # 再插（新增 + 替换），按提交顺序赋 IdenIndex
        pos = 0
        final_rows = []  # (pos, guid)
        for sp in line_specs:
            old = by_guid.get(sp["iden_guid"]) if sp["iden_guid"] else None
            pos += 1
            if old:
                same = (
                    old["hpbh"] == sp["hpbh"] and old["unit"] == (sp["unit"] or old["unit"])
                    and old["qty"] == Decimal(str(sp["qty"]))
                    and old["price"] == (Decimal(str(sp["price"])) if sp["price"] not in (None, "") else old["price"])
                    # ★ 备注一致才算「真的没变」，否则重插（带上新备注）
                    and (old.get("remark") or "") == sp["remark"]
                )
                if same:
                    final_rows.append((pos, old["iden_guid"]))
                    continue
            price_from = None
            if sp["price"] in (None, ""):
                price_from = price_mod.resolve_price(cur, cfg, new_itembh, sp["hpbh"], sp["unit"])
            info = _insert_line(cur, cfg, {**h, "item_bh": new_itembh, "ndate": new_ndate}, sp, pos, price_from=price_from)
            final_rows.append((pos, info["iden_guid"]))
        # 重排 IdenIndex（纯序号更新，不触发库存联动）
        for pos, row_guid in final_rows:
            cur.execute(
                "UPDATE XSMX SET IdenIndex = %s WHERE IdenGuid = CONVERT(uniqueidentifier, %s)",
                (pos, row_guid),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"重排 IdenIndex 失败：{row_guid}")

        # 明细变化后回写单头合计（触发器按差值净调应收/往来）
        final_sums = _apply_header()
        # 价格历史刷新（同 V3 保存）
        cur.execute("EXEC HxUpdatePrice_Kh_XSPrice @ItemBh = NULL, @BillGuid = %s", (h["guid"],))

        # 事务内自检
        cnt_xsmx = db.q1(cur, "SELECT COUNT(*) FROM XSMX WHERE BillGuid = CONVERT(uniqueidentifier, %s)", (h["guid"],))[0]
        cnt_flow = db.q1(cur, "SELECT COUNT(*) FROM InvenFlow WHERE BillIden = %s", (h["iden"],))[0]
        if cnt_xsmx != cnt_flow:
            raise RuntimeError(f"自检失败：XSMX {cnt_xsmx} 行 vs InvenFlow {cnt_flow} 行")
        check = db.q1(
            cur,
            """SELECT (SELECT COUNT(*) FROM XSMX WHERE BillGuid = CONVERT(uniqueidentifier, %s)),
                          x.S_SLSUM, x.J_JEWF
                   FROM XS x WHERE x.IdenGuid = CONVERT(uniqueidentifier, %s)""",
            (h["guid"], h["guid"]),
        )
        if check[0] != len(line_specs) or _dec(check[1]) != final_sums[0] or _dec(check[2]) != final_sums[1]:
            raise RuntimeError("自检失败：单头合计与明细不一致")
        cur.connection.commit()
    except Exception:
        try:
            cur.execute("SET IDENTITY_INSERT XS OFF")
        except Exception:
            pass
        cur.connection.rollback()
        raise
    return {"changed": True, "bill": get_bill(cur, bill_no)}


def create_bill(cur, cfg, customer, lines, ndate=None, warehouse="101-", note="",
                price_trace=None, price_type_xs=None, client_token=None, commit=True):
    """新建销售出库单（未审核草稿单），口径与桌面新建一致：
    - 走 No_BillID 取单号/流水号，触发器按单头+逐行 DML 联动库存与应收；
    - 提交前自检：XSMX 行数 == InvenFlow 行数、单头合计与明细一致。

    price_trace / price_type_xs：本单选定的「价格方案」（可选）。
    ★ 只对**未给价的行**生效（`item["price"]` 为空时才取价）；
      前端已逐行给价时这两个参数不影响结果。传 None = 按客户档案（原行为）。
    ★ XS 表无价格方案列，所以方案**不落库** —— 仅本次建单取价用一次。

    client_token：前端为「一次提交」生成的幂等键（可选）。
    ★ 同一 token 重复到达时**不再插库**，直接回上次那张单（见文件上方「建单幂等」）。
    ★ 老版本前端不发这个字段 → token 为空 → 完全走原行为，向后兼容。
    """
    from datetime import datetime as _dt

    # ★ 幂等闸门必须在**任何写操作之前**：放在取号/INSERT 之后就没意义了。
    token = str(client_token or "").strip()
    # ★ 幂等键带账套（Step 2 请求级选库）：同一个 token 打到另一本账时不能复用这本的单号
    dbns = idem.db_of(cfg)
    state, replay_no = idem.begin(token, database=dbns)
    if state == "replay":
        bill = get_bill(cur, replay_no)
        bill["idempotent_replay"] = True
        return bill

    try:
        return _create_bill_inner(
            cur, cfg, customer, lines, ndate, warehouse, note,
            price_trace, price_type_xs, token, commit,
        )
    except Exception:
        # ★ 失败必须释放占位：否则用户重试会被「同一笔提交正在处理中」挡住，
        #   而这恰恰是最需要他能重试的时刻。
        idem.abort(token, database=dbns)
        raise


def _create_bill_inner(cur, cfg, customer, lines, ndate, warehouse, note,
                       price_trace, price_type_xs, token, commit):
    """create_bill 的真实实现（幂等闸门已在外层处理）。"""
    from datetime import datetime as _dt

    customer = str(customer or "").strip()
    if not customer:
        raise ValueError("缺少客户")
    cust = db.q1(
        cur,
        "SELECT ItemBh, Itemmc, ISNULL(Ckbh,'') FROM Khdan WHERE LTRIM(RTRIM(ItemBh)) = %s",
        (customer,),
    )
    if not cust:
        raise ValueError("客户不存在：" + customer)
    warehouse = str(warehouse or cust[2] or "101-").strip()
    if not db.q1(cur, "SELECT Key_ FROM CkType WHERE Key_=%s", (warehouse,)):
        raise ValueError("仓库不存在：" + warehouse)

    ndate = ndate or _dt.now()
    if isinstance(ndate, str):
        ndate = ndate.replace("T", " ")[:19]
    note = str(note or "")

    specs = []
    for i, it in enumerate(lines or [], 1):
        hpbh = str(it.get("hpbh") or "").strip()
        qty = Decimal(str(it.get("qty") or 0))
        if not hpbh:
            raise ValueError(f"第 {i} 行缺货品")
        if qty <= 0:
            raise ValueError(f"第 {i} 行数量必须大于 0")
        specs.append(
            {
                "hpbh": hpbh,
                "unit": str(it.get("unit") or "").strip() or None,
                "qty": qty,
                "price": it.get("price"),
                # ★ 明细行备注：落 XSMX.BZ_XSMX
                "remark": str(it.get("remark") or ""),
                # ★ 赠品位：缺省 False（老前端/老断网草稿不带这个键时，行为与改造前一致）
                "gift": bool(it.get("gift")),
            }
        )
    if not specs:
        raise ValueError("至少一行商品明细")

    # 取号：No_BillID(BillXS=2020) 与 XS.IdenCol 同源
    #
    # ★★★ 并发安全（2026-10-11）：走 `db.next_bill_number` —— 应用锁
    #   （sp_getapplock，会话级）保护下**顺序推进**计数器。
    #   原写法「先 SELECT → +1 → UPDATE」四步无锁，READ COMMITTED 下并发两笔
    #   读到同一个号 ⇒ 后提交者撞 PK_XS_BillNo（实测 5 路并发只有 1 路成功）；
    #   改成 X 锁方案又**必然** 1205 死锁且会跳号（用户要求连号）。
    #   详见 v3agent/sql.py 顶部「单据取号（并发安全）」的完整论证。
    # ★ `MAX(IdenCol)` 兜底保留：历史上 BillNoid 与 XS 行号曾不同步。
    # ★ 应用锁在**本函数的 finally 里**释放（见下方 _lock_res）——
    #   必须持有到 COMMIT 之后，否则号还没落库就把锁交出去，等于没锁。
    _lock_res = db.app_lock_resource(idem.db_of(cfg), "xs", 2020)
    _lock_held = False
    try:
        _prefix, nid = db.next_bill_number(
            cur, bill_na_id=2020, database=idem.db_of(cfg), kind="xs",
            # ★ 前缀现读 V3 的配置行（与采购同一套，2026-10-12）：
            #   原先这里写死 "XK"，而单号前缀在 V3 是 `No_BillID.BILLNOLEFT2` 一列，
            #   客户在桌面端改了、这边还在发 XK ⇒ 两边出的号不一样。
            #   实测现网这本账 2020 那行就是 'XK'（XS 138 张全是 XK 前缀），
            #   所以这一改**不改变现在的号**，只是让它跟着真源走。
            prefix_sql="SELECT ISNULL(BILLNOLEFT2,'') FROM No_BillID WHERE BillNaID=2020",
            max_sql="SELECT ISNULL(MAX(IdenCol),0) FROM XS",
        )
        _lock_held = True
        prefix = (_prefix or "XK").strip() or "XK"
    except Exception:
        raise
    bill_no = prefix + str(nid).zfill(8)
    # ★★★ Useno 必须留空 —— **不要再生成 "XS"+序号**（2026-09-22 用户明确要求）
    #
    # 业务事实：V3 里 `BillNo` 与 `Useno` 是**两套完全独立的计数器**：
    #   · BillNo = 系统内部单号，与 IdenCol 同源（XK + 8 位），由 No_BillID(2020) 取号；
    #   · Useno  = **纸张/手工单据号**，是用户拿到的纸质联上的号，与内部序号无关。
    # 实测证据（tongda，ISOK=1 共 36605 行）：
    #   IdenCol=36666 → BillNo=XK00036666 → Useno=XS00242908   ← 两个数字段差 12 万，互不相关
    #   IdenCol= 36668 → BillNo=XK00036668 → Useno=XS00036668   ← 仅我们 Agent 造的 2 行是这样（错）
    #   全库 `Useno` 已用到 XS00242970，而 IdenCol 只到 36668。
    #
    # 旧实现是 `use_no = "XS" + str(nid).zfill(8)` —— 把**内部计数器的值塞进了 Useno**，
    # 制造出一个真实业务里不存在的编号（会被桌面端当成伪造单号）。
    # 现在写空串：单号字段交给用户在桌面端补录实际纸质单号。
    #
    # ★ 空值可行性已实测验证（回滚试验，未留痕）：
    #   · `XS.Useno` 是 nvarchar(60) NOT NULL，无唯一索引/约束（唯一索引只有 PK_XS_BillNo / BillNo）；
    #   · `CheckBillUseNo` 过程只查重复（`UseNo=@UseNo AND IdenCol<>@IdenCol`），空串不冲突；
    #   · 插入 `Useno=''` → 触发器未拒绝，读回 `LEN(Useno)==0`。
    # ★ 注意：不要写 None/NULL —— 该列 NOT NULL；空串才是正确形态。
    use_no = ""
    guid = str(uuid.uuid4())

    # 触发器依赖的临时表必须在单头 INSERT 前就绪
    db.ensure_temp(cur, guid, nid, ndate, customer)
    # ★ 表头时间与制单人：统一 `YYYY-MM-DD HH:mm:ss` + 制单人=这条请求的操作员
    now_str = _now_str()
    cur.execute("SET IDENTITY_INSERT XS ON")
    try:
        cur.execute(
            """INSERT INTO XS (BillNaID, BillNo, Useno, NDate, BillState, ItemBh, Ckbh,
               TaxType, TaxRate, BZ_XS, ISOK, IsReceive, IsAuditing, IsSend,
               TimeInsert, TimeUpdate, UserInsert, UserUpdate, UserVoid, zdrbh, idenguid, Idencol)
               VALUES (2020,%s,%s,%s,0,%s,%s,1,0,%s,1,0,0,0,%s,%s,%s,%s,'',%s,%s,%s)""",
            (bill_no, use_no, ndate, customer, warehouse, note,
             now_str, now_str, _who(), _who(), _who(), guid, nid),
        )
        if cur.rowcount != 1:
            raise RuntimeError("单头插入失败")
        cur.execute("SET IDENTITY_INSERT XS OFF")

        h = {
            "guid": guid,
            "item_bh": customer,
            "ndate": ndate,
            "ckbh": warehouse,
        }
        for i, sp in enumerate(specs, 1):
            _insert_line(cur, cfg, h, sp, i,
                         scheme={"price_trace": price_trace, "price_type_xs": price_type_xs})

        sums = db.q1(
            cur,
            """SELECT SUM(S_SL), SUM(J_JEZK), SUM(J_JEHS), SUM(J_JE)
               FROM XSMX WHERE BillGuid = CONVERT(uniqueidentifier, %s)""",
            (guid,),
        )
        qty_sum, amt_zk, amt_hs, amt_je = [Decimal(str(x or 0)) for x in sums]
        cur.execute(
            """UPDATE XS SET J_JESUM=%s, J_JEHSSUM=%s, J_JEExSUM=%s, J_JEZKSUM=%s, J_JEWF=%s,
               S_SLUnitSUM=%s, S_SLPartSUM=%s, S_SLSUM=%s,
               TimeUpdate=%s, UserUpdate=%s
               WHERE IdenGuid = CONVERT(uniqueidentifier, %s)""",
            (amt_je, amt_hs, amt_je, amt_zk, amt_zk, qty_sum, qty_sum, qty_sum,
             _now_str(), _who(), guid),
        )
        cur.execute("EXEC HxUpdatePrice_Kh_XSPrice @ItemBh = NULL, @BillGuid = %s", (guid,))

        cnt_xsmx = db.q1(
            cur, "SELECT COUNT(*) FROM XSMX WHERE BillGuid = CONVERT(uniqueidentifier, %s)", (guid,)
        )[0]
        cnt_flow = db.q1(cur, "SELECT COUNT(*) FROM InvenFlow WHERE BillIden = %s", (nid,))[0]
        if cnt_xsmx != len(specs) or cnt_flow != len(specs):
            raise RuntimeError(f"自检失败：XSMX {cnt_xsmx} 行 / InvenFlow {cnt_flow} 行")
        if commit:
            cur.connection.commit()
    except Exception:
        cur.connection.rollback()
        raise
    finally:
        # ★ 应用锁必须在 COMMIT（或 ROLLBACK）**之后**才释放：
        #   提前放锁 = 号还没落库就把取号权交出去，等于没锁。
        #   release 失败不影响业务结果，内部已吞异常。
        if _lock_held:
            db.release_number_lock(cur, _lock_res)
    # ★ 提交成功后才回填幂等表。放在 commit 之后是刻意的：
    #   若中途异常回滚，占用会被 idem.abort 释放，同一 token 可以正常重试，
    #   不会出现「第一次失败但 token 已被标记成功」→ 用户重试反而不建单。
    if commit:
        idem.finish(token, bill_no, database=idem.db_of(cfg))
    return get_bill(cur, bill_no)


# ------------------------------------------------------------------ 审核 / 反审 / 复制


def audit_bill(cur, bill_no):
    """审核：未作废、未审核的单 → IsAuditing=1 + AuditingTime=当前时间。

    ★ 只改审核状态位，不触碰金额/库存/明细。
    ★★ 实测（2026-09-22 探针，回滚未留痕）确认：UPDATE XS 触发 TRI_A_XS 时，
       触发器按「撤销旧应收(-J_JEWF) + 应用新应收(+J_JEWF)」两步执行，
       两者抵消 ⇒ 客户应收 J_Due 零变化。审核是**安全的纯状态切换**。
    """
    h = _load_header(cur, bill_no)
    if h is None:
        raise LookupError(f"单据不存在：{bill_no}")
    if _void_flag(h["user_void"], h["time_void"]):
        raise ValueError("单据已作废，不能审核")
    if h["is_auditing"]:
        raise ValueError("单据已审核，无需重复审核")
    # ★ UPDATE XS 也触发 TRI_A_XS（AFTER，不区分 DML 类型），必须先播临时表。
    db.ensure_temp(cur, h["guid"], h["iden"], h["ndate"], h["item_bh"])
    now = _now_str()
    cur.execute(
        """UPDATE XS SET IsAuditing=1, AuditingTime=%s, TimeUpdate=%s, UserUpdate=%s
           WHERE BillNo=%s""",
        (now, now, _who(), bill_no),
    )
    if cur.rowcount != 1:
        raise RuntimeError("审核失败：单据不存在或状态已变化")
    cur.connection.commit()
    return get_bill(cur, bill_no)


def unaudit_bill(cur, bill_no):
    """反审：已审核、未作废的单 → IsAuditing=0 + AuditingTime 清空。

    ★★ 需求红线：作废的单据**无法反审**（作废是终态，只能复制）。
    """
    h = _load_header(cur, bill_no)
    if h is None:
        raise LookupError(f"单据不存在：{bill_no}")
    if _void_flag(h["user_void"], h["time_void"]):
        raise ValueError("单据已作废，不能反审")
    if not h["is_auditing"]:
        raise ValueError("单据未审核，无需反审")
    db.ensure_temp(cur, h["guid"], h["iden"], h["ndate"], h["item_bh"])
    cur.execute(
        """UPDATE XS SET IsAuditing=0, AuditingTime=NULL, TimeUpdate=%s, UserUpdate=%s
           WHERE BillNo=%s""",
        (_now_str(), _who(), bill_no),
    )
    if cur.rowcount != 1:
        raise RuntimeError("反审失败：单据不存在或状态已变化")
    cur.connection.commit()
    return get_bill(cur, bill_no)


def void_bill(cur, bill_no):
    """作废：未作废的单 → ISOK=0 + UserVoid + TimeVoid。

    ★★ 作废是**终态**：作废后无法反审恢复（需求红线），只能复制。
    ★ 作废把 ISOK 1→0，触发 TRI_A_XS 的「撤销应收」分支，客户应收自动冲销。
    ★ 已结算（有收款/结算金额）的单不能作废 —— 触发器会 HxRoll 拒绝，
      这里先友好报错，别让前端撞上存储过程的英文报错。
    """
    h = _load_header(cur, bill_no)
    if h is None:
        raise LookupError(f"单据不存在：{bill_no}")
    if _void_flag(h["user_void"], h["time_void"]):
        raise ValueError("单据已作废，不能重复作废")
    # ★★ 需求红线（2026-09-23）：已审核单**不允许直接作废**，只有未审核单才能作废。
    #   已审核单要先反审再作废（反审是「解锁」，作废是「取消入账」——顺序不能跳）。
    if h["is_auditing"]:
        raise ValueError("单据已审核，不能直接作废，请先反审")
    for k in ("j_jefk", "j_jelossum", "j_fkadvance", "j_fkadvancedec", "j_fkvip"):
        if _dec(h[k]) != 0:
            raise ValueError("单据已存在收款/结算金额，不能作废")
    db.ensure_temp(cur, h["guid"], h["iden"], h["ndate"], h["item_bh"])
    now = _now_str()
    cur.execute(
        """UPDATE XS SET ISOK=0, UserVoid=%s, TimeVoid=%s, TimeUpdate=%s, UserUpdate=%s
           WHERE BillNo=%s""",
        (_who(), now, now, _who(), bill_no),
    )
    if cur.rowcount != 1:
        raise RuntimeError("作废失败：单据不存在或状态已变化")
    cur.connection.commit()
    return get_bill(cur, bill_no)


def copy_bill(cur, cfg, bill_no, ndate=None):
    """复制单据：任意状态（含作废/已审核）→ 新建一张**未审核**单。

    - 复制客户 / 仓库 / 单头备注 / 逐行明细（货品、单位、数量、单价、行备注）；
    - 价格**沿用原单价**（不重新取价，保持原单口径）；
    - 新单日期默认「今天」（ndate=None 时走 create_bill 的 now）；
    - 审核 / 作废状态**不复制** —— 复制出来永远是未审核。
    """
    h = _load_header(cur, bill_no)
    if h is None:
        raise LookupError(f"单据不存在：{bill_no}")
    lines = _load_lines(cur, h["guid"])
    if not lines:
        raise ValueError("原单无明细，无法复制")
    new_lines = [
        {
            "hpbh": x["hpbh"],
            "unit": x["unit"],
            "qty": float(x["qty"]),
            "price": float(x["price"]),
            "remark": x.get("remark", ""),
        }
        for x in lines
    ]
    return create_bill(
        cur, cfg,
        customer=h["item_bh"],
        lines=new_lines,
        ndate=ndate,
        warehouse=h["ckbh"],
        note=h["bz_xs"] or "",
    )
