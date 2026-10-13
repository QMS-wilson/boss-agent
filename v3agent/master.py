# -*- coding: utf-8 -*-
"""主数据扩展：客户/产品查询与保存、业务看板统计（均走白名单 op）。"""

import uuid
from decimal import Decimal

from . import sql as db


def _num(v):
    return float(Decimal(str(v or 0)))


# 往来单位状态：Khdan.StopUse / Gysdan.StopUse 是 bit（0 正常往来 / 1 暂停往来），
# 与 Hpdan.StopUse 那套 tinyint 状态码不是一回事（货品见 product.py 的 STOP_NAMES）。
_STOP_BIT_TRUTHY = ("1", "true", "y", "-1", "on")


def stopped_bit(v):
    """把 bit 列读回来的 True/1/'1' 归一成布尔。

    ★ 读不懂就当**没停**（返回 False）：宁可多列一条，也不要因为一个 'X' 就把客户从
      老板的列表和开单面板里整个藏掉 —— 那是把人往外赶，不是过滤。
    """
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return False
    try:
        return int(v) != 0
    except (TypeError, ValueError):
        return str(v).strip().lower() in _STOP_BIT_TRUTHY


def contact_status_name(v):
    return "暂停往来" if stopped_bit(v) else "正常往来"


# ---------------------------------------------------------------------------
# 停用档案清单：界面上"档案名字旁边那枚标签"的**唯一数据源**（2026-10-06）。
#
# ★ 为什么不是每条 SQL 都加一列 stop：单据列表/详情、库存、报表三个排行、对账、
#   草稿箱、首页最近单据…渲染档案名的地方一共 30 来处（逐个点数过）。
#   逐处 JOIN 档案表加一列：改不完、以后新增页面一定漏，而且每处都要重做一遍
#   "六档状态怎么翻文案"的口径。而"这个编号现在停不停"跟它出现在哪张单上无关，
#   所以一次把**停着的那些**取回，前端集中查表贴标签。
# ★ 只回停着的：正常档案是绝大多数（现网 48,591 条货品里停用的 10 条），结果天然很小。
# ★ 编号两侧可能带空格（现网实测有 " 12454514" 这种）⇒ 同时回 trim 过的和不带的，
#   前端两个都建索引，免得"库里存的是带空格的、单上抄的是不带空格的"就漏标。
# ---------------------------------------------------------------------------
def stop_status(cur, cap=2000):
    from .product import STOP_DISABLED, STOP_NAMES, STOP_NO_PURCHASE, STOP_NO_SALE

    cap = int(cap or 2000)
    out = {"cap": cap}

    partners = (("customers", "Khdan", "ItemBh"), ("suppliers", "Gysdan", "Itembh"))
    for key, table, col in partners:
        # ★ '---' 那行必须排掉：它是 V3 的「已删除的客户」汇总行（现网实测 StopUse 也是 1），
        #   不排就会顶在清单第一条，界面上给一个根本不存在的客户贴标签。
        rows = db.qall(
            cur,
            "SELECT LTRIM(RTRIM(%s)), %s, Itemmc FROM %s "
            "WHERE ISNULL(StopUse,0) = 1 AND %s <> '---' ORDER BY %s" % (col, col, table, col, col),
            cap=cap + 1,
        )
        out["truncated_" + key] = len(rows) > cap
        out[key] = [{"code": str(r[0] or "").strip(), "codeRaw": str(r[1] or ""),
                     "name": r[2], "status": 1, "statusName": "暂停往来"}
                    for r in rows[:cap]]

    prods = db.qall(
        cur,
        "SELECT LTRIM(RTRIM(hpbh)), hpbh, hpmc, CONVERT(int, ISNULL(StopUse,0)) FROM Hpdan "
        "WHERE ISNULL(StopUse,0) IN (%d, %d, %d) ORDER BY hpbh"
        % (STOP_DISABLED, STOP_NO_SALE, STOP_NO_PURCHASE),
        cap=cap + 1,
    )
    out["truncated_products"] = len(prods) > cap
    out["products"] = [{"code": str(r[0] or "").strip(), "codeRaw": str(r[1] or ""),
                        "name": r[2], "status": int(r[3] or 0),
                        "statusName": STOP_NAMES.get(int(r[3] or 0), "停止使用")}
                       for r in prods[:cap]]
    return out


def _find_customer(cur, code):
    return db.q1(
        cur,
        """SELECT TOP 1 k.ItemBh, k.Itemmc, ISNULL(k.Py,''), ISNULL(kt.Text,''),
                  ISNULL(k.Lxr,''), ISNULL(k.SJ,''), ISNULL(k.Tel,''), ISNULL(k.Address,''),
                  ISNULL(k.BZ_Khdan,''), k.PriceTypeXS, k.PriceTrace,
                  k.J_Due, k.J_SaleSUM, ISNULL(k.LbType_Kh,''), k.StopUse
           FROM Khdan k
           LEFT JOIN KhType kt ON kt.Key_ = k.LbType_Kh
           WHERE LTRIM(RTRIM(k.ItemBh)) = %s""",
        (str(code).strip(),),
    )


def customer_row(cur, code):
    r = _find_customer(cur, code)
    if not r:
        raise LookupError("客户不存在：" + str(code))
    # ★ 累计销售从 XS 实时聚合：Khdan.J_SaleSUM 恒 0（V3 客户端不维护该字段），
    #   真实累计销售只能从销售单主表 XS 按客户 SUM(J_JEWF) 算。
    #   业绩口径与 reports/dashboard 一致：BillState=0 且 ISOK=1（正式单，排除草稿/作废）。
    sale = db.q1(
        cur,
        "SELECT COALESCE(SUM(J_JEWF),0) FROM XS WHERE ItemBh=%s AND BillState=0 AND ISOK=1",
        (str(code).strip(),),
    )
    return {
        "code": r[0], "name": r[1], "py": r[2], "category": r[3],
        # ★ category 是类别名称（JOIN KhType 的 Text，供显示）；category_code 是
        #   类别编号（LbType_Kh 原值，供编辑保存写回），两者都要返回。
        "category_code": r[13],
        "contact": r[4], "phone": r[5], "tel": r[6], "address": r[7],
        "remark": r[8], "price_type_xs": r[9], "price_trace": r[10],
        "due": _num(r[11]), "sale_sum": _num(sale[0]),
        # ★ 详情也带状态：客户停用后从历史单据点进来，页面得说得清"这位已暂停往来"
        #   （详情**不按状态过滤**，否则老单的客户直接查无此人）。
        "stop_use": stopped_bit(r[14]), "stop": stopped_bit(r[14]),
        "status_name": contact_status_name(r[14]),
    }


def _to_int(v, default):
    """把可能是 str/None/float/垃圾 的值安全转成 int。

    ★ 为什么不能直接 int(v)：op 参数来自 HTTP/WS 的 JSON，
      前端或脚本可能传 'abc'、'12.5'、''、None。
      直接 int() 会 ValueError → 整个接口 500。
      分页参数坏了不该让请求失败，退回默认值即可。
    """
    if v is None or v == "":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))     # '12.5' / 12.5
        except (TypeError, ValueError):
            return default


def _page_args(limit, offset):
    """归一化分页参数。上限 500（前端一页最多要 300~500）。

    ★ 全部走 _to_int，任何非法输入都退回默认，绝不抛异常。
    """
    limit = _to_int(limit, 100)
    if limit < 1:
        limit = 1
    limit = min(limit, 500)
    offset = _to_int(offset, 0)
    if offset < 0:
        offset = 0
    return limit, offset


def _like_clause(fields, kw, args):
    """拼一个 LIKE 子句，**返回已完全格式化好的字符串**。

    ★★ 踩过的坑（2026-09-19，连踩三次，务必看）：
       本项目 pymssql 连接用的是 `tds_version='7.0'`（DB-Lib/CT-Lib 后端），
       参数占位符是 **pyformat**（`%s`），而 pymssql 内部会再做一次
       `sql % params`。因此：

       1) 不能用 `?` 占位 —— DB-Lib 不认，报 102
          "Incorrect syntax near '?'"。
       2) 把 `LIMIT/OFFSET` 的整数直接写进 SQL 文本时必须转义 `%%d`/`%%s`。
       3) ★ 最阴的一条：给 `%` 传的 `clause` 字符串里**不能残留任何 `%s`**，
          否则 `"... %s ..." % clause % (a, b)` 外层那次 `%` 会继续在 clause
          里找格式符，报 "not enough arguments for format string"。
          → 所以把 LIKE 的 `%s` 就地替换成**字面量**（值仍由 args 走参数化，
            这里只填进 SQL 文本的是 `%s` 本身，真正的值不拼进来）。

    返回形如：`(hpbh LIKE %s OR hpmc LIKE %s)`，其中 `%s` 是给 pymssql 的占位符。

    ★★ 关键字里的 LIKE 通配符必须转义（2026-10-12，与 product.py 同一口径）：
      SQL Server 的 LIKE 里 `%` 匹配任意串、`_` 匹配任意单字、`[` 开字符类。
      不转义时用户搜一个 `%` 就等于"匹配所有"（几万条档案一次全捞出来，
      还顺带把 `truncated`/`limit` 那套分页判断绕过去），搜 `_` 会命中所有单字占位。
      这不是注入（值始终走参数绑定），是**语义被用户的输入改写**。
      product.py 从第一天就转义了，这一份没有 ⇒ 客户/供应商/单据行搜索与货品搜索行为不一致。
    """
    esc = str(kw).replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
    parts = []
    for f in fields:
        parts.append("ISNULL(%s,'') LIKE %%s" % f)
        args.append("%" + esc + "%")
    return "(" + " OR ".join(parts) + ")"


def _paged(rows, total, limit, offset, mapper):
    """统一分页返回结构。

    ★ 返回的是**对象**不是裸数组（2026-09 起）：
        {items:[...], total, limit, offset, hasMore}
    调用方（ops / cloudapi / 前端 / miniapp）按 items 取数据。
    """
    items = [mapper(r) for r in rows]
    total = int(total or 0)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "hasMore": (offset + len(items)) < total,
    }


def list_customers(cur, keyword="", limit=100, offset=0, show_stopped=False):
    """客户分页列表。空关键字 = 全部。

    ★ 2026-09-19 起支持真分页（原先是 SELECT TOP N，翻页无效）。

    ★★ 必须用 ROW_NUMBER() 而不是 OFFSET/FETCH：
       实测目标库是 **SQL Server 2008 R2 (10.50)**，
       OFFSET/FETCH 是 SQL Server 2012+ 语法，在 2008 上直接报
       「Incorrect syntax near 'OFFSET'」（错误 102）。
       ROW_NUMBER() 是 2005+ 语法，2008 可用。
    ORDER BY ItemBh 稳定，翻页不会漏行/重行。
    ★ show_stopped：默认只给正常往来的客户（StopUse=0）。档案列表页那个「显示已停用」把它打开；
      开单页选客户永远不传 ⇒ 停用的客户不会出现在选择面板里。
      详情（customer_row）不按这个过滤 —— 客户停用了，他名下历史单还得能打开、能回显名字。
    """
    limit, offset = _page_args(limit, offset)
    kw = str(keyword or "").strip()
    where, args = (["ItemBh <> '---'"] if show_stopped
                   else ["StopUse=0", "ItemBh <> '---'"]), []
    if kw:
        where.append(_like_clause(["ItemBh", "Itemmc", "Py"], kw, args))
    clause = " AND ".join(where)
    total = db.q1(cur, "SELECT COUNT(*) FROM Khdan WHERE " + clause, tuple(args))[0]
    # ★ 暂停往来有几家（与关键字同源，不受 show_stopped 影响）：界面上要靠它说
    #   "另有 N 家没显示"，否则开关打开后那几家排在最后，看着像没反应。
    stop_where = " AND ".join(["StopUse=1"] + [w for w in where if w != "StopUse=0"])
    stopped_count = int(db.q1(cur, "SELECT COUNT(*) FROM Khdan WHERE " + stop_where,
                              tuple(args))[0])
    rows = db.qall(
        cur,
        """SELECT t.ItemBh, t.Itemmc, ISNULL(t.Py,''), ISNULL(kt.Text,''),
                  ISNULL(t.Lxr,''), ISNULL(t.SJ,''), ISNULL(t.Tel,''), ISNULL(t.Address,''),
                  t.PriceTrace, t.J_Due, t.J_SaleSUM, t.PriceTypeXS, t.StopUse
           FROM (
               SELECT ItemBh, Itemmc, Py, LbType_Kh, Lxr, SJ, Tel, Address,
                      PriceTrace, J_Due, J_SaleSUM, PriceTypeXS, StopUse,
                      ROW_NUMBER() OVER (ORDER BY
                        CASE WHEN ISNULL(StopUse,0) = 1 THEN 0 ELSE 1 END, ItemBh) AS rn
               FROM Khdan WHERE %s
           ) t
           LEFT JOIN KhType kt ON kt.Key_ = t.LbType_Kh
           WHERE t.rn > %d AND t.rn <= %d
           ORDER BY t.rn""" % (clause, offset, offset + limit),
        tuple(args),
    )
    out = _paged(rows, total, limit, offset, lambda r: {
        "code": r[0], "name": r[1], "py": r[2], "category": r[3],
        "contact": r[4], "phone": r[5], "tel": r[6], "address": r[7],
        "price_trace": r[8], "due": _num(r[9]), "sale_sum": _num(r[10]),
        # ★ price_type_xs 必须**与 customer_row（详情）同形**（2026-09-22 第九轮补）。
        #
        #   为什么必须补：销售单页面「价格方案」要**默认取客户档案**
        #   （PriceTrace + PriceTypeXS 这一对，见 sales/edit.vue::pickCustomer）。
        #   而弹窗里选客户走的是本函数（customer.search），之前只 SELECT 了
        #   PriceTrace、**没有 PriceTypeXS** —— 实测 126 个客户里
        #   price_type_xs 出现 **0 次** ⇒ 适配层 num(undefined) 恒得 0 ⇒
        #   页面把方案默认成了「零售价」，与客户档案（实测 201=最近销价 125/126）
        #   完全不同。**静默错价**：界面显示一个方案，取价按另一个口径走。
        #
        #   ★ 详情接口 customer_row 一直是有这个字段的 —— 所以这是个
        #     「两个接口不同形」的经典坑：页面从列表进、从详情进，拿到的形状不一样。
        #     凡是在两个接口都会用到的字段，两处都必须给。
        "price_type_xs": r[11],
        # ★ 状态三件套与货品/供应商列表同形：档案页把「显示已停用」打开后，
        #   行上要能说出这条为什么平时看不见（前端不许自己猜成「已停用」了事）。
        "stop_use": stopped_bit(r[12]), "stop": stopped_bit(r[12]),
        "status_name": contact_status_name(r[12]),
    })
    out["stoppedCount"] = stopped_count
    out["showStopped"] = bool(show_stopped)
    return out


def list_customer_types(cur):
    rows = db.qall(
        cur,
        "SELECT Key_, Text FROM KhType WHERE Parent = 'O_' AND StopUse = 0 ORDER BY IDENCOL",
    )
    return [{"code": r[0], "name": r[1]} for r in rows]


def next_customer_code(cur):
    r = db.q1(
        cur,
        "SELECT MAX(CAST(SUBSTRING(ItemBh,2,30) AS BIGINT)) FROM Khdan WHERE ItemBh LIKE 'K[0-9]%'",
    )
    return "K" + str(int(r[0] or 0) + 1).zfill(5)


def save_customer(cur, data, commit=True):
    """data: {code?, name, category?, contact?, phone?, tel?, address?, remark?,
             price_type_xs?, price_trace?}

    价格方案（2026-09-21）：PriceTypeXS 0=零售价 1=批发价 2=优惠价 3=销售价3
    4=销售价4 5=销售价5 13=最低销价；PriceTrace 200=按价格方案、201=最近销价。
    """
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("客户名称不能为空")
    category = str(data.get("category") or "").strip()
    contact = str(data.get("contact") or "").strip()
    phone = str(data.get("phone") or "").strip()
    tel = str(data.get("tel") or "").strip()
    address = str(data.get("address") or "").strip()
    remark = str(data.get("remark") or "").strip()
    code = str(data.get("code") or "").strip()
    price_type_xs = _to_int(data.get("price_type_xs"), 0)
    price_trace = _to_int(data.get("price_trace"), 200)

    ex = _find_customer(cur, code) if code else None
    if ex:
        py = str(data.get("py") or (ex[2] or name[:10]))
        cur.execute(
            """UPDATE Khdan SET Itemmc=%s, Py=%s, LbType_Kh=%s,
               Lxr=%s, SJ=%s, Tel=%s, Address=%s, BZ_Khdan=%s,
               PriceTypeXS=%s, PriceTrace=%s,
               UserUpdate='AGENT', TimeUpdate=GETDATE()
               WHERE ItemBh=%s""",
            (name, py, category, contact, phone, tel, address, remark,
             price_type_xs, price_trace, code),
        )
        if commit:
            cur.connection.commit()
        return {"code": code, "created": False}

    code = code or next_customer_code(cur)
    if _find_customer(cur, code):
        raise ValueError("客户编号已存在：" + code)
    guid = str(uuid.uuid4())
    py = str(data.get("py") or name[:10])
    cur.execute(
        """INSERT INTO Khdan (ItemBh, Itemmc, Py, LbType_Kh, Lxr, SJ, Tel, Address,
           BZ_Khdan, ItemType, PriceTypeXS, PriceTrace, DefaID, StopUse, Audited, IsSend,
           UserInsert, UserUpdate, TimeInsert, TimeUpdate, IdenGuid)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,5,%s,%s,0,0,0,0,'AGENT','AGENT',GETDATE(),GETDATE(),%s)""",
        (code, name, py, category, contact, phone, tel, address, remark,
         price_type_xs, price_trace, guid),
    )
    if commit:
        cur.connection.commit()
    return {"code": code, "created": True}


# ---------------------------------------------------------------------------
# 货品主数据：2026-09-27 多单位改造时拆到 v3agent/product.py。
#
# ★ 这里只留同名薄壳。两个原因：
#   1) ops.py 按 master.save_product / master.product_row 调，历史脚本也按这些名字
#      断言源码（verify-pyz-r14 就查 master._recent_sale_price 在不在）——
#      删名字只会让门以"函数不存在"红掉，那不是回归，是门在说谎。
#   2) 货品现在牵动 Hpdan + HpdanUnit 两张表，代码量不该和"客户/看板"挤一个文件。
# ★ 注意 find_product 的返回**变了**：旧版返回位置元组（r[7] 那种），
#   新版返回 {列名: 值} 的字典。位置下标在补列时会整体错位 —— 这正是
#   上一轮 D_SJ5/D_CXDJ 被漏掉的根因，所以不再保留元组形态。
# ---------------------------------------------------------------------------
from . import product as _product  # noqa: E402

find_product = _product.find_row
product_row = _product.product_row
list_products = _product.list_products
list_types = _product.list_types
save_product = _product.save_product
read_units = _product.read_units
code_rule = _product.code_rule
category_code_prefix = _product.category_code_prefix
next_product_code = _product.next_product_code
code_plan = _product.code_plan
preview_code = _product.preview_code
_recent_sale_price = _product._recent_sale_price


def dashboard(cur, month_start=None, today=None):
    """经营看板：本月金额与**同口径张数** / 今日金额与张数 / 应收合计（全量、排掉占位行）。

    ★ 张数与金额必须是同一个 WHERE：前端「客单价 = 金额 / 张数」是拿这两个数直接相除的，
      两边口径不同就是一笔摊错的数。
    ★ 2026-10-08 补 `month_count` —— 在这之前前端只有金额，界面上那两行只能不显示
      （旧写法是拿 0 冒充"这个月一单没开"）。
    """
    def qsum(sql, params=()):
        return _num(db.q1(cur, sql, params)[0])

    month = qsum(
        "SELECT COALESCE(SUM(J_JEWF),0) FROM XS WHERE BillState=0 AND ISOK=1 AND NDate>=%s AND NDate<DATEADD(day,1,%s)",
        (month_start, today),
    )
    # ★ 张数**必须与上面那笔金额同一谓词**：客单价 = month_amount / month_count，
    #   两边口径不同就会算出"没卖过的单也摊了钱"的数（前端 utils/money.js 只负责取整，
    #   不负责替我圆场）。
    month_count = int(
        db.q1(
            cur,
            "SELECT COUNT(*) FROM XS WHERE BillState=0 AND ISOK=1 AND NDate>=%s AND NDate<DATEADD(day,1,%s)",
            (month_start, today),
        )[0]
    )
    today_amount = qsum(
        "SELECT COALESCE(SUM(J_JEWF),0) FROM XS WHERE BillState=0 AND ISOK=1 AND NDate>=%s AND NDate<DATEADD(day,1,%s)",
        (today, today),
    )
    today_count = int(
        db.q1(
            cur,
            "SELECT COUNT(*) FROM XS WHERE BillState=0 AND ISOK=1 AND NDate>=%s AND NDate<DATEADD(day,1,%s)",
            (today, today),
        )[0]
    )
    # ★ 首页「应收合计」按**全量**客户算（含暂停往来）：钱不会因为把人标成停用就消失。
    #   旧写法带 StopUse=0，现网实测会把 3 家 ¥1,823.38 直接从合计里抹掉，
    #   而应收账款页与客户排行里那位还在 ⇒ 首页数字跟哪儿都对不上。
    receivable = qsum("SELECT COALESCE(SUM(J_Due),0) FROM Khdan "
                      "WHERE ItemBh <> '---' AND J_Due>0")
    return {
        "month_amount": month,
        "month_count": month_count,
        "today_amount": today_amount,
        "today_count": today_count,
        "receivable_total": receivable,
    }
