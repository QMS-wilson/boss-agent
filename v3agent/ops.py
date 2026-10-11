# -*- coding: utf-8 -*-
"""白名单操作（只暴露这里定义的 op，不对外透传 SQL）。"""

from . import auth, inventory, master, options, printing, purchase, reports, sales, sql as db
from .__init__ import __version__
from .op_docs import DOCS
from .price import resolve_price
from .yxparts import service as yxparts


OPS = {}


def _flag(v):
    """查询开关（show_stopped 这类）归一成布尔。

    ★ 云端没透传 / 传了空串 / 传了 "0" 都算**关**：默认值必须是"少给数据"而不是"多给"，
      老版云端配新版 Agent 时才不会把停用的档案整个倒进列表。
    """
    if isinstance(v, bool):
        return v
    if v is None or v == "":
        return False
    try:
        return int(v) != 0
    except (TypeError, ValueError):
        return str(v).strip().lower() in ("1", "true", "y", "yes", "on")


def _usage(v):
    """开单方向：只认 sale/purchase，别的（包括拼错的）一律当档案页。"""
    u = str(v or "").strip().lower()
    return u if u in ("sale", "purchase") else ""


def op(name):
    def wrap(fn):
        OPS[name] = fn
        return fn
    return wrap


def catalog():
    """{op: {cat,label,desc,note?,example}} —— 后台调试台的说明与示例参数。

    ★ 为什么由 Agent 报给云端而不是云端写死：动作清单是 Agent 自报的，云端只认这份
      （没上报的一律 OP_NOT_PUBLISHED）。说明若写在云端，两边就会漂移，
      症状是调试台上"某个动作选了没反应"或"少了说明"，误导人。
    ★ 只返回**已注册**的 op；缺说明的会带一个 `desc:"（缺说明）"` 露出来，
      而不是静默消失 —— 验收脚本 verify-install-id.py 也守着这条。
    """
    out = {}
    for name in OPS:
        d = DOCS.get(name)
        out[name] = dict(d) if d else {"cat": "未分类", "label": name,
                                       "desc": "（缺说明）", "example": {}}
    return out


@op("system.ping")
def _ping(cur, cfg, params):
    db.q1(cur, "SELECT 1")
    return {"pong": True}


@op("auth.v3check")
def _auth_v3check(cur, cfg, params):
    """用 V3 的操作员表验口令（比对只发生在这台机器上，不回哈希也不回明文）。"""
    return auth.check(cur, params.get("user") or params.get("login"),
                      str(params.get("password") or ""))


@op("auth.v3users")
def _auth_v3users(cur, cfg, params):
    """登录页那个操作员下拉的数据源：只回编号/姓名/是否停用。"""
    return auth.list_users(cur, (params or {}).get("limit"))


@op("bill.get")
def _bill_get(cur, cfg, params):
    bill_no = str(params.get("bill_no") or "").strip()
    if not bill_no:
        raise ValueError("缺少 bill_no")
    return sales.get_bill(cur, bill_no)


@op("bill.list")
def _bill_list(cur, cfg, params):
    return sales.list_bills(
        cur,
        date_from=params.get("date_from"),
        date_to=params.get("date_to"),
        customer=params.get("customer"),
        keyword=params.get("keyword"),
        state=params.get("state"),
        limit=params.get("limit") or 100,
        offset=params.get("offset") or 0,
    )


@op("product.salesHistory")
def _product_sales_history(cur, cfg, params):
    """某货品的最近销售明细行（新增销售单的「最近销售」面板）。

    ★ 不要改用 bill.list 反查 —— 销售单**列表**不含明细行，永远查不到。
    """
    hpbh = str(params.get("hpbh") or "").strip()
    if not hpbh:
        raise ValueError("缺少 hpbh")
    return sales.product_sales_history(
        cur,
        hpbh,
        limit=params.get("limit") or 8,
        customer=params.get("customer"),
    )


@op("price.resolve")
def _price_resolve(cur, cfg, params):
    for key in ("customer", "hpbh"):
        if not params.get(key):
            raise ValueError(f"缺少 {key}")
    # ★ 价格方案覆盖（2026-09-22）：前端「新增销售单」页的「价格方案」选择
    #   通过这两个参数传下来（price_trace=201 → 最近销价；price_type_xs=0..5/13）。
    #   ★ 必须是「没传就是 None」，不能用 `or 0` 兜底 —— 0 是合法取值（零售价，
    #     也恰好是本库 127 个客户的默认值），用 or 会把「明确选了零售价」
    #     和「没传」混为一谈，虽然当前结果相同，但语义是错的。
    ptrace = params.get("price_trace")
    ptype = params.get("price_type_xs")
    r = resolve_price(
        cur, cfg,
        str(params["customer"]).strip(),
        str(params["hpbh"]).strip(),
        (str(params.get("unit") or "").strip()) or None,
        price_trace=(int(ptrace) if ptrace not in (None, "") else None),
        price_type_xs=(int(ptype) if ptype not in (None, "") else None),
    )
    r["price"] = float(r["price"])
    r["rate"] = float(r["rate"])
    return r


@op("salesbill.update")
def _bill_update(cur, cfg, params):
    bill_no = str(params.get("bill_no") or "").strip()
    if not bill_no:
        raise ValueError("缺少 bill_no")
    # ★ 乐观锁（可选，2026-10-11）：前端从 bill.get 拿到 revision，保存时回传。
    #   传了才校验 → 老前端与 V3 桌面端完全不受影响（向后兼容）。
    return sales.update_bill(
        cur, cfg, bill_no,
        header=params.get("header") or None,
        lines=params.get("lines") or None,
        expect_revision=params.get("revision"),
    )


@op("salesbill.create")
def _bill_create(cur, cfg, params):
    # ★ 价格方案（可选，2026-09-22）：只影响「未给价的行」的取价。
    #   与 price.resolve 同一套取值语义：None/"": 不覆盖；否则整数覆盖。
    ptrace = params.get("price_trace")
    ptype = params.get("price_type_xs")
    return sales.create_bill(
        cur,
        cfg,
        customer=params.get("customer"),
        lines=params.get("lines") or [],
        ndate=params.get("ndate"),
        warehouse=params.get("warehouse"),
        note=params.get("note") or "",
        price_trace=(int(ptrace) if ptrace not in (None, "") else None),
        price_type_xs=(int(ptype) if ptype not in (None, "") else None),
        # ★ 幂等键（2026-09-22 第九轮）：前端为「一次提交」生成的 token。
        #   同一 token 重复到达时不再插库，直接回上次那张单 —— 见
        #   sales.py 文件上方的「建单幂等」说明。
        #   老版本前端不发该字段 ⇒ None ⇒ 完全走原行为（向后兼容）。
        client_token=params.get("client_token"),
    )


@op("salesbill.audit")
def _bill_audit(cur, cfg, params):
    bill_no = str(params.get("bill_no") or "").strip()
    if not bill_no:
        raise ValueError("缺少 bill_no")
    return sales.audit_bill(cur, bill_no)


@op("salesbill.unaudit")
def _bill_unaudit(cur, cfg, params):
    bill_no = str(params.get("bill_no") or "").strip()
    if not bill_no:
        raise ValueError("缺少 bill_no")
    return sales.unaudit_bill(cur, bill_no)


@op("salesbill.void")
def _bill_void(cur, cfg, params):
    bill_no = str(params.get("bill_no") or "").strip()
    if not bill_no:
        raise ValueError("缺少 bill_no")
    return sales.void_bill(cur, bill_no)


@op("salesbill.copy")
def _bill_copy(cur, cfg, params):
    bill_no = str(params.get("bill_no") or "").strip()
    if not bill_no:
        raise ValueError("缺少 bill_no")
    return sales.copy_bill(cur, cfg, bill_no, ndate=params.get("ndate"))


# ---------------------------------------------------------------- 采购(进货)


@op("purchase.list")
def _purchase_list(cur, cfg, params):
    """进货单分页列表。★ 复用 master._page_args，非法分页参数不会 500。"""
    return purchase.list_bills(
        cur,
        date_from=params.get("date_from"),
        date_to=params.get("date_to"),
        supplier=params.get("supplier"),
        keyword=params.get("keyword"),
        state=params.get("state"),
        limit=params.get("limit"),
        offset=params.get("offset"),
    )


@op("purchase.get")
def _purchase_get(cur, cfg, params):
    bill_no = str(params.get("bill_no") or "").strip()
    if not bill_no:
        raise ValueError("缺少 bill_no")
    return purchase.get_bill(cur, bill_no)


@op("purchase.create")
def _purchase_create(cur, cfg, params):
    """新建进货单。★ 表头时间 YYYY-MM-DD HH:mm:ss + ZDR=SYSTEM（见 purchase 常量）。"""
    return purchase.create_bill(
        cur,
        cfg,
        supplier=params.get("supplier"),
        lines=params.get("lines") or [],
        ndate=params.get("ndate"),
        warehouse=params.get("warehouse"),
        note=params.get("note") or "",
        # ★ 幂等键（2026-09-22 第十轮）：与销售单共用 v3agent/idem.py。
        #   真机连点保存 / 响应丢失后重试，都不会在 JHMX 里多出一张单。
        #   老版本前端不发该字段 ⇒ None ⇒ 完全走原行为（向后兼容）。
        client_token=params.get("client_token"),
    )


@op("purchase.update")
def _purchase_update(cur, cfg, params):
    """改进货单。header 只认 NDate/ItemBh/BZ_JH，lines 是**最终**完整明细（含 iden_guid）。

    ★ 与 `salesbill.update` 一样**不带 client_token**：PUT 按单号天然幂等，
      真正的重放防护只在建单那一侧。
    """
    bill_no = str(params.get("bill_no") or "").strip()
    if not bill_no:
        raise ValueError("缺少 bill_no")
    return purchase.update_bill(
        cur, cfg, bill_no,
        header=params.get("header") or None,
        lines=params.get("lines") or None,
    )


@op("archive.stopStatus")
def _archive_stop_status(cur, cfg, params):
    """停用的档案清单（客户/供应商/货品）——界面上每枚「停用」标签的唯一数据源。"""
    return master.stop_status(cur, cap=int(params.get("cap") or 2000))


@op("supplier.search")
def _supplier_search(cur, cfg, params):
    return purchase.list_suppliers(
        cur,
        keyword=str(params.get("keyword") or ""),
        limit=params.get("limit"),
        offset=params.get("offset"),
        show_stopped=_flag(params.get("show_stopped")),
    )


@op("supplier.get")
def _supplier_get(cur, cfg, params):
    code = str(params.get("code") or "").strip()
    if not code:
        raise ValueError("缺少 code")
    return purchase.supplier_row(cur, code)


@op("supplier.types")
def _supplier_types(cur, cfg, params):
    return purchase.list_supplier_types(cur)


@op("supplier.prices")
def _supplier_prices(cur, cfg, params):
    code = str(params.get("code") or "").strip()
    if not code:
        raise ValueError("缺少 code")
    return purchase.supplier_prices(cur, code, params.get("limit") or 200)


@op("supplier.save")
def _supplier_save(cur, cfg, params):
    """供应商新增/编辑（2026-09-21）。body 即 save_supplier 的 data。"""
    return purchase.save_supplier(cur, params)


@op("product.search")
def _product_search(cur, cfg, params):
    keyword = str(params.get("keyword") or "").strip()
    return master.list_products(
        cur,
        keyword=keyword,
        limit=int(params.get("limit") or 100),
        offset=int(params.get("offset") or 0),
        show_stopped=_flag(params.get("show_stopped")),
        usage=_usage(params.get("usage")),
    )


@op("product.get")
def _product_get(cur, cfg, params):
    # ★ 传 cfg：product_row 需要它取「最近销价」的字段口径（price.recent_price_field）。
    return master.product_row(cur, str(params.get("hpbh") or ""), cfg)


@op("product.save")
def _product_save(cur, cfg, params):
    return master.save_product(cur, params)


@op("product.types")
def _product_types(cur, cfg, params):
    """货品类别清单（编辑页的类别下拉）。★ 保存时的类别校验与它同源。"""
    return master.list_types(cur)


@op("product.nextcode")
def _product_nextcode(cur, cfg, params):
    """按这台机器 V3 自己的编号规则预测下一个货品编号（新增页"自动带出"用）。

    ★ 只读、不占号：预测到真正保存之间可能已被占用，保存时 `save_product` 会再查一次重。
    ★ 规则是按类别的、但这次还没选类别 ⇒ **不报错**，回 `need_category`（界面要拿这份
      规则去写提示，报错就没法显示了）；保存路径该拦的还是拦。
    """
    return master.preview_code(cur, str(params.get("category") or "").strip())


@op("customer.search")
def _customer_search(cur, cfg, params):
    keyword = str(params.get("keyword") or "").strip()
    return master.list_customers(
        cur,
        keyword=keyword,
        limit=int(params.get("limit") or 100),
        offset=int(params.get("offset") or 0),
        show_stopped=_flag(params.get("show_stopped")),
    )


@op("customer.get")
def _customer_get(cur, cfg, params):
    return master.customer_row(cur, str(params.get("code") or ""))


@op("customer.types")
def _customer_types(cur, cfg, params):
    return master.list_customer_types(cur)


@op("customer.save")
def _customer_save(cur, cfg, params):
    return master.save_customer(cur, params)


@op("system.dashboard")
def _dashboard(cur, cfg, params):
    from datetime import datetime as _dt
    _today = _dt.now().strftime("%Y-%m-%d")
    return master.dashboard(
        cur,
        month_start=params.get("month_start") or (_today[:8] + "01"),
        today=params.get("today") or _today,
    )


def _dates(params):
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    return (str(params.get("date_from") or today[:8] + "01"),
            str(params.get("date_to") or today))


@op("report.sales.summary")
def _report_sales_summary(cur, cfg, params):
    d1, d2 = _dates(params)
    return reports.sales_summary(cur, d1, d2, params.get("customer"))


@op("report.sales.trend")
def _report_sales_trend(cur, cfg, params):
    d1, d2 = _dates(params)
    return reports.sales_trend(cur, d1, d2, params.get("granularity") or "day", params.get("customer"))


@op("report.customer.ranking")
def _report_customer_ranking(cur, cfg, params):
    d1, d2 = _dates(params)
    return reports.customer_ranking(cur, d1, d2, int(params.get("limit") or 20))


@op("report.product.ranking")
def _report_product_ranking(cur, cfg, params):
    d1, d2 = _dates(params)
    return reports.product_ranking(cur, d1, d2, int(params.get("limit") or 20))


@op("diag.report.gradient")
def _diag_report_gradient(cur, cfg, params):
    """只读诊断：报表查询调色板（定位大数据量崩溃用）。"""
    res = reports.diag_gradient(cur, params.get("only"))
    res["database"] = (cfg.get("sql") or {}).get("database", "")
    return res


@op("report.receivables.summary")
def _report_receivables(cur, cfg, params):
    return reports.receivables_summary(
        cur,
        keyword=params.get("keyword") or "",
        only_receivable=params.get("only_receivable", True),
        limit=int(params.get("limit") or 200),
        aging=params.get("aging") or "",
        # ★ 只影响"列出来的客户"那一段；合计与账龄一直是全量（见 reports.py 的拍板注释）
        show_stopped=_flag(params.get("show_stopped")),
    )


@op("report.customer.product.detail")
def _report_customer_product(cur, cfg, params):
    d1, d2 = _dates(params)
    return reports.customer_product_detail(
        cur,
        customer=params.get("customer") or params.get("customerId") or "",
        date_from=d1,
        date_to=d2,
        limit=int(params.get("limit") or 200),
    )


@op("report.customer.statement")
def _report_customer_statement(cur, cfg, params):
    d1, d2 = _dates(params)
    return reports.customer_statement(
        cur,
        customer=params.get("customer") or params.get("customerId") or "",
        date_from=d1,
        date_to=d2,
        # ★ with_lines 默认关：只有导出"月份对账单"那张逐货品行的版式才要这一层。
        #   走的是**同一个 op 名** ⇒ 云端在途闸门仍按 report 池计权，不用另登记；
        #   代价是"带明细"这种更重的活和轻量对账共用同一个位，所以它只在点名时给。
        with_lines=_flag(params.get("with_lines") or params.get("withLines")),
        line_limit=params.get("line_limit") or params.get("lineLimit"),
    )


# ---------------------------------------------------------------- 采购报表 / 毛利


@op("report.purchase.summary")
def _report_purchase_summary(cur, cfg, params):
    d1, d2 = _dates(params)
    return reports.purchase_summary(cur, d1, d2, params.get("supplier"))


@op("report.purchase.trend")
def _report_purchase_trend(cur, cfg, params):
    d1, d2 = _dates(params)
    return reports.purchase_trend(cur, d1, d2, params.get("granularity") or "day")


@op("report.supplier.ranking")
def _report_supplier_ranking(cur, cfg, params):
    d1, d2 = _dates(params)
    return reports.supplier_ranking(cur, d1, d2, int(params.get("limit") or 20))


@op("report.profit.summary")
def _report_profit_summary(cur, cfg, params):
    d1, d2 = _dates(params)
    return reports.profit_summary(cur, d1, d2)


@op("print.salesbill")
def _print_salesbill(cur, cfg, params):
    # ★ 2026-09-23：透传 printer / format（打印页可选打印机 + 打印格式）。
    return printing.print_bill(
        cur,
        cfg,
        bill_no=str(params.get("bill_no") or params.get("billNo") or ""),
        mode=params.get("mode") or "system",
        copies=int(params.get("copies") or 1),
        printer=params.get("printer") or params.get("printer_name") or params.get("printerName"),
        fmt=params.get("format") or params.get("fmt"),
        # ★ dpi 仅在 mode=image 时用；留空 ⇒ Agent 按页数自适应（防小程序 setData 撑爆）
        dpi=params.get("dpi"),
    )


@op("print.format.get")
def _print_format_get(cur, cfg, params):
    return printing.print_format(cur, params.get("bill_name") or params.get("billName") or "销售出货单")


@op("print.format.list")
def _print_format_list(cur, cfg, params):
    return printing.list_named_formats(
        cur,
        rpt_type=params.get("rpt_type") or params.get("rptType") or "BillXS",
        db_name=params.get("db") or "AccMaster",
    )


@op("print.format.def")
def _print_format_def(cur, cfg, params):
    return printing.get_named_format(
        cur,
        rpt_name=params.get("rpt_name") or params.get("name"),
        rpt_type=params.get("rpt_type") or params.get("rptType") or "BillXS",
        db_name=params.get("db") or "AccMaster",
        include_definition=bool(params.get("include_definition")),
        include_text=bool(params.get("include_text")),
    )


@op("print.printer.list")
def _print_printer_list(cur, cfg, params):
    return printing.list_printers(cur, cfg)


@op("agent.capabilities")
def _agent_capabilities(cur, cfg, params):
    """Agent 能力 + 数据库连通性。

    ★★ 为什么 dbStatus 要用**独立连接**探活（不能复用 cur）：
       调用方 agent.py 的 dispatch() 是
           with db.session(cfg) as conn: data = handler(cur, cfg, params)
       —— `pymssql.connect()` 在数据库连不上时会**直接抛异常**，
       于是整个 op 失败，云端拿不到 capabilities，
       前端连「Agent 在线」都读不到。也就是说：**数据库挂掉这件消息本身
       传不出去**，页面只会显示一团空白，反而更难排查。
       所以这里必须自己开一条连接去试，失败也照样返回结构化结果。

    返回字段（前端「我的」页消费）：
        dbStatus   : 'healthy' | 'error'   —— 数据库是否可查
        dbError    : str | None            —— 失败原因（截断，供排查）
        printers   : list
        printFormats: dict
    """
    caps = {
        "name": cfg.get("agent", {}).get("name", ""),
        "version": __version__,
        "ops": sorted(OPS.keys()),
        "printers": [],
        "printFormats": {"count": 0, "items": []},
        "dbStatus": "error",
        "dbError": None,
        # ★ 账套可见性（2026-09-24）：Agent 现在能在运行期切库，所以"连的哪套账"
        #   必须回传给前端显示。只给配置值不够 —— 配置写 A 实际连 B 是本项目
        #   踩过的坑（DEFAULTS 的 name/database 就不一致），故同时回传实测值。
        "database": (cfg.get("sql") or {}).get("database") or "",
        "databaseVerified": None,
    }

    # ---- 1) 数据库探活（独立连接，失败也不抛）----
    try:
        with db.session(cfg) as probe_conn:
            probe_cur = probe_conn.cursor()
            probe_cur.execute("SELECT 1")
            row = probe_cur.fetchone()
            if row and int(row[0]) == 1:
                caps["dbStatus"] = "healthy"
            else:
                caps["dbStatus"] = "error"
                caps["dbError"] = "SELECT 1 返回异常"
            try:
                caps["databaseVerified"] = db.current_database(probe_cur)
            except Exception:  # noqa: BLE001
                caps["databaseVerified"] = None
    except Exception as exc:  # noqa: BLE001
        caps["dbStatus"] = "error"
        caps["dbError"] = str(exc)[:200]

    # ---- 1b) 可切换账套清单（只在显式要时才扫，别拖慢常态 capabilities）----
    if params.get("withAccountSets"):
        try:
            caps["accountSets"] = db.account_sets(cfg)["databases"]
        except Exception:  # noqa: BLE001
            caps["accountSets"] = []

    # ---- 1c) 本店 V3 的业务开关（决定界面露不露某些功能；读不到一律回 None=不知道）----
    try:
        caps["accountOptions"] = options.account_options(cur)
    except Exception:  # noqa: BLE001
        # capabilities 不能因为这一块挂掉：它挂了前端连「Agent 在线」都读不到
        caps["accountOptions"] = None

    # ---- 2) 打印机 / 打印格式（依赖 cur，失败降级为空）----
    # ★ dbStatus 已单独探活，这里即便失败也不能影响上面结论
    try:
        caps["printers"] = printing.list_printers(cur, cfg)
    except Exception:  # noqa: BLE001
        caps["printers"] = []
    try:
        caps["printFormats"] = printing.list_named_formats(cur, "BillXS", "AccMaster")
    except Exception:  # noqa: BLE001
        caps["printFormats"] = {"count": 0, "items": []}

    return caps


@op("inventory.query")
def _inventory_query(cur, cfg, params):
    return inventory.stock_query(
        cur,
        keyword=params.get("keyword") or "",
        warehouse=params.get("warehouse") or "",
        flag=params.get("flag") or "all",
        limit=int(params.get("limit") or 200),
        offset=int(params.get("offset") or 0),
        product=params.get("hpbh") or params.get("product") or "",
    )


@op("inventory.flow")
def _inventory_flow(cur, cfg, params):
    d1, d2 = (params.get("date_from"), params.get("date_to"))
    if not d1 and not d2:
        # 不传期间时给一个足够宽的窗口，避免把全库流水一次拉回来
        d1 = "2000-01-01"
    return inventory.stock_flow(
        cur,
        product=params.get("hpbh") or params.get("product") or "",
        date_from=d1,
        date_to=d2,
        limit=int(params.get("limit") or 500),
    )


@op("agent.info")
def _info(cur, cfg, params):
    return {
        "name": cfg["agent"]["name"],
        "ops": sorted(OPS),
        "database": (cfg.get("sql") or {}).get("database") or "",
        "databaseVerified": db.current_database(cur),
    }


# ---------------------------------------------------------------------------
# 燕秀零件处理（客户零件清单 → 匹配 hpbh → 由客户确认后建单）
#   实现全在 v3agent/yxparts/，这里只是薄薄一层注册；桌面标签页与手机端共用同一套逻辑。
# ---------------------------------------------------------------------------

@op("yxparts.match")
def _yx_match(cur, cfg, params):
    return yxparts.match(cur, cfg, params)


@op("yxparts.suggest")
def _yx_suggest(cur, cfg, params):
    return yxparts.suggest(cur, cfg, params)


@op("yxparts.remember")
def _yx_remember(cur, cfg, params):
    return yxparts.remember(cur, cfg, params)


@op("yxparts.memory.list")
def _yx_memory_list(cur, cfg, params):
    return yxparts.memory_list(cfg, params)


@op("yxparts.memory.delete")
def _yx_memory_delete(cur, cfg, params):
    return yxparts.memory_delete(cfg, params)


@op("yxparts.capabilities")
def _yx_caps(cur, cfg, params):
    return yxparts.capabilities(cur, cfg, params)


@op("yxparts.importCustomers")
def _yx_import_customers(cur, cfg, params):
    return yxparts.import_customers(cur, cfg, params)
