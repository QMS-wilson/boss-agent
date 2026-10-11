# -*- coding: utf-8 -*-
"""每个动作（op）的中文说明与示例参数 —— 后台「调试工具」下拉里显示的就是这份。

★ 为什么放在 Agent 这一侧而不是写死在云端页面里：
  动作清单是 Agent 自报的（`hello.ops`），云端只认这份清单（没上报的一律 `OP_NOT_PUBLISHED`）。
  如果说明写在云端，就会出现"云端有 48 个动作的说明、Agent 只报 46 个"这种漂移，
  而漂移的症状是调试台上有个动作选了没反应 / 少了说明 —— 误导人比没有更糟。
  所以**清单与说明同源**：都从 Agent 出来，云端只转发。

★ 每条都必须有 `desc`。`v3agent/ops.py` 的 `catalog()` 会把缺说明的动作显式标出来，
  验收脚本 `verify-install-id.py` 也有一条断言守着 —— 加了新 op 忘了写说明会当场红。

字段含义：
  cat     : 分组（下拉里按组排，调试时先选类别比翻 48 行快）
  label   : 中文名，跟在动作标识前面
  desc    : 一句话说清"这个动作做什么"（下拉里就显示这一句）
  note    : 可选。踩过的坑、不该怎么用、返回里的关键字段 —— 只在这一条被选中时展开
  example : 选中后自动填进参数框的示例 JSON。值要能直接跑（用真库里有意义的形态，
            日期一律写成明显的占位样式，避免被误当真实数据）
"""

DOCS = {
    # ---------------- 连接与状态 ----------------
    "auth.v3check": {
        "cat": "连接与状态", "label": "V3 操作员口令校验",
        "desc": "读本机账套的 V3 操作员表（UserInfo）校验登录：口令比对只发生在客户这台机器上",
        "note": "★ 只读，但**不回任何口令/哈希**。规则（2026-09-28 拍板）："
                "操作员被停用 → USER_STOPPED；V3 里没设口令 → NO_PASSWORD（手机端不放行空口令，"
                "让人去 V3 设）；账号不存在与口令不对**同一句话** BAD_CREDENTIAL（编号可枚举，"
                "分开说就是送枚举器）；按名称命中多人 → AMBIGUOUS_NAME。"
                "★ 第一版 `perms` 全部是 null（=V3 里没配/还没读），**不做任何权限拦截**；"
                "接云端登录时注意：这个动作要在**没有令牌**的情况下可达，"
                "并且云端必须按「商户+操作员」限流、登录请求体不许进审计日志。",
        "example": {"user": "101", "password": "（V3 里那个操作员的口令）"}},
    "auth.v3users": {
        "cat": "连接与状态", "label": "V3 操作员清单",
        "desc": "列这本账的操作员编号与姓名（登录页选操作员的下拉用），只回 id/name/stopped",
        "note": "★ 只读，且 SELECT 里**根本不写 UserPassword 这一列**（不是查出来再删）——"
                "少写一列就少一个漏出去的机会。停用的操作员照样回，靠 `stopped` 标出来，"
                "让界面当场不让选，而不是让人挑一个注定登录失败的。"
                "★ 接云端时注意：这条是**免登录**的 /api/v1/auth/options 的数据源，"
                "等于把这家有哪些人暴露给拿得到链接的人（产品定的，见云端 op 说明），"
                "所以云端必须限流、审计不落参数、Agent 离线时要降级成手输而不是报错。",
        "example": {"limit": 200}},
    "system.ping": {
        "cat": "连接与状态", "label": "连通性探测",
        "desc": "只跑一条 SELECT 1，验证 Agent 到本机数据库这条链是通的",
        "note": "云端判活与排障的第一步。它失败说明数据库连不上，而不是网络问题。",
        "example": {}},
    "agent.info": {
        "cat": "连接与状态", "label": "Agent 概况",
        "desc": "返回版本号、机器名、当前账套等自报信息",
        "example": {}},
    "agent.capabilities": {
        "cat": "连接与状态", "label": "能力与数据库状态",
        "desc": "动作清单 + 打印机 + 打印格式 + 数据库连通性 + **本店 V3 的业务开关**"
                "（前端「我的」页与功能入口用它）",
        "note": "数据库探活用了一条独立连接：pymssql 连不上时会直接抛异常，"
                "若复用同一个游标，「数据库挂了」这条消息本身就传不出去，"
                "页面只会一片空白，反而更难查。所以这里失败也返回结构化结果。\n"
                "accountOptions 是**三态**（true/false/null）：null=库里没这行、同名多行值冲突、"
                "或值看不懂 ⇒ 界面**照常显示**该功能，只有明确 false 才隐藏。"
                "（把「不知道」当「没开」= 一次查询抖动让客户的整片功能消失。）"
                "productCode 是货品编号规则，每次现读，V3 里改完下一个请求就跟着变。",
        "example": {"withAccountSets": True}},
    "system.dashboard": {
        "cat": "看板与报表", "label": "经营看板",
        "desc": "本月销售额与张数 / 今日销售额与单数 / 未收往来账合计",
        "note": "返回 month_amount、month_count、today_amount、today_count、receivable_total；"
                "不传日期就默认本月与今天。month_count 与 month_amount **同一谓词**，"
                "两者相除才是这批单的客单价（前端 utils/money.js 取整到分）。"
                "没有「待审核张数」，也没有环比 —— 那两个数移动端别猜。",
        "example": {"month_start": "YYYY-MM-01", "today": "YYYY-MM-DD"}},

    # ---------------- 单据 ----------------
    "bill.list": {
        "cat": "单据", "label": "销售单列表",
        "desc": "按日期 / 客户 / 关键字 / 状态分页查销售单",
        "note": "列表**不含明细行**。要看某货品的销售明细用 product.salesHistory，"
                "这里反查是查不到的。",
        "example": {"date_from": "YYYY-MM-01", "date_to": "YYYY-MM-DD", "customer": "",
                    "keyword": "", "state": "", "limit": 20, "offset": 0}},
    "bill.get": {
        "cat": "单据", "label": "销售单明细",
        "desc": "按单号取一张销售单的表头与全部明细行",
        "example": {"bill_no": "XS00000001"}},
    "salesbill.create": {
        "cat": "单据", "label": "新建销售单",
        "desc": "写库动作：新建一张销售单（表头 + 明细行）",
        "note": "★ 这是**会改数据**的动作，在调试台上点之前先确认账套。"
                "client_token 用于防重复提交，同值重复提交只落一张。"
                "lines 里的 gift=1 表示这一行是赠品：单价与金额照记，但折后金额与含税金额记 0，"
                "所以它不进应收合计、数量照扣库存（口径照 V3 桌面端实测，不是这里定的）。",
        "example": {"customer": "000005", "ndate": "YYYY-MM-DD 10:00:00", "warehouse": "101-",
                    "note": "调试台建的单", "price_type_xs": "", "price_trace": False,
                    "client_token": "debug-0001",
                    "lines": [{"hpbh": "1123454552", "qty": 1, "unit": "个", "price": 0, "gift": 0}]}},
    "salesbill.update": {
        "cat": "单据", "label": "修改销售单",
        "desc": "写库动作：按单号改表头并整体替换明细行",
        "note": "★ 会改数据。lines 是**全量替换**，不是增量。",
        "example": {"bill_no": "XS00000001", "header": {"note": "改备注"},
                    "lines": [{"hpbh": "1123454552", "qty": 2, "unit": "个", "price": 1.8, "gift": 0}]}},
    "salesbill.audit": {
        "cat": "单据", "label": "审核销售单",
        "desc": "写库动作：审核一张销售单（审核后库存与往来账才生效）",
        "note": "★ 会改数据，且影响库存与应收。", "example": {"bill_no": "XS00000001"}},
    "salesbill.unaudit": {
        "cat": "单据", "label": "反审核销售单",
        "desc": "写库动作：把已审核的销售单退回未审核",
        "note": "★ 会改数据。", "example": {"bill_no": "XS00000001"}},
    "salesbill.void": {
        "cat": "单据", "label": "作废销售单",
        "desc": "写库动作：作废一张销售单（留痕，不删记录）",
        "note": "★ 会改数据。作废是带经办人与时间的留痕操作。",
        "example": {"bill_no": "XS00000001"}},
    "salesbill.copy": {
        "cat": "单据", "label": "复制销售单",
        "desc": "写库动作：按已有单号复制一张新单，可换日期",
        "note": "★ 会改数据。", "example": {"bill_no": "XS00000001", "ndate": "YYYY-MM-DD"}},
    "purchase.list": {
        "cat": "单据", "label": "进货单列表",
        "desc": "按日期 / 供应商 / 关键字 / 状态分页查进货单",
        "example": {"date_from": "YYYY-MM-01", "date_to": "YYYY-MM-DD", "supplier": "",
                    "keyword": "", "state": "", "limit": 20, "offset": 0}},
    "purchase.get": {
        "cat": "单据", "label": "进货单明细",
        "desc": "按单号取一张进货单的表头与全部明细行",
        "example": {"bill_no": "JH00000022"}},
    "purchase.create": {
        "cat": "单据", "label": "新建进货单",
        "desc": "写库动作：新建一张进货单",
        "note": "★ 会改数据。表头时间格式是 YYYY-MM-DD HH:mm:ss。",
        "example": {"supplier": "ces001", "ndate": "YYYY-MM-DD 10:00:00", "warehouse": "101-",
                    "note": "调试台建的单", "client_token": "debug-0002",
                    "lines": [{"hpbh": "1123454552", "qty": 1, "unit": "个", "price": 0, "gift": 0}]}},
    "purchase.update": {
        "cat": "单据", "label": "修改进货单",
        "desc": "写库动作：按单号改进货单表头与明细（带 iden_guid 的行走 diff，没带的算新增）",
        "note": "★ 会改数据，且影响库存与应付。header 只认 NDate/ItemBh/BZ_JH（单号不可改）；"
                "lines 是**最终完整明细**。已审核/已付款/有优惠/明细带退货或订单关联的一律拒改。"
                "★ JHMX 没有 IdenIndex：重插的行会排到明细末尾。",
        "example": {"bill_no": "JH00000001", "header": {"BZ_JH": "改备注"},
                    "lines": [{"iden_guid": "", "hpbh": "1123454552", "qty": 2,
                               "unit": "个", "price": 1.8, "gift": 0}]}},

    # ---------------- 基础资料 ----------------
    "product.search": {
        "cat": "基础资料", "label": "货品搜索",
        "desc": "按编号 / 名称 / 拼音码分页搜货品，带回库存与售价",
        "note": "默认不返回「停止使用」的货品。show_stopped=1 连停用的一起给（档案页那个开关）；"
                "usage=sale 另排「停止销售」、usage=purchase 另排「停止采购」，"
                "且这时 show_stopped 失效（开单页不该选到停掉的东西）。"
                "每项带 stop_use / status_name，界面按状态文案说，别自己猜成「已停用」。",
        "example": {"keyword": "螺丝", "limit": 20, "offset": 0}},
    "product.get": {
        "cat": "基础资料", "label": "货品详情",
        "desc": "按货品编号取一条货品的完整档案",
        "example": {"hpbh": "1123454552"}},
    "product.save": {
        "cat": "基础资料", "label": "新增/编辑货品",
        "desc": "写库动作：保存货品档案（有编号即编辑，无编号即新增）。"
                "多单位走 units 数组（一单位一行，每单位可带自己的一套价）",
        "note": "★ 会改数据。★ **不认识的入参会直接报错**，不会静默丢弃"
                "（旧版前端发 baseUnit/salePriceDefault 被丢掉就是这么发现的）。"
                "units 里必须且只能有一个 basic:true，基本单位倍率恒为 1；"
                "★ 一个货品最多 2 档单位（基本 + 1 个换算单位），换算率必须是整数 —— "
                "三档以上请到 V3 客户端录；"
                "库里没有的单位行会新增，本次没提交且没开过单的单位行按停用处理（不物删）。"
                "★ 带 2 档以上单位的货品会自动派生主表的 `pack_unit`（包装单位，如 `件*个`）与"
                "`pack_spec`（包装规格，如 `1*12`），并把包装那一档单位行的 UnitPackage 置 1 —— "
                "判据照 V3 客户端建的 85485455515418；手动传这两键则以传入为准，"
                "开过业务单据的货品不重算（Update(UnitPack) 受 V3 按列保护）。",
        "example": {"code": "", "name": "新货品", "unit": "个", "py": "XH",
                    "spec": "20mm", "barcode": "6901234567890", "category": "管材",
                    "brand": "某某", "supplier": "G00001", "cost_price": 8,
                    "sale_price": 10, "price_retail": 12, "price_member": 11,
                    "min_order": 40, "shelf_days": 365, "batch": False, "serial": False,
                    "stop": False,
                    "units": [{"unit": "个", "basic": True, "sale_price": 10},
                              {"unit": "包", "rate": 20, "sale_price": 190,
                               "purchase_price": 160, "barcode": "6901234567891"}]}},
    "product.types": {
        "cat": "基础资料", "label": "货品类别清单",
        "desc": "列出 HpType 的货品类别（编号 + 名称），供产品编辑页的类别下拉用",
        "example": {}},
    "product.nextcode": {
        "cat": "基础资料", "label": "货品编号自动带出",
        "desc": "按这台机器 V3 自己的「货品编号规则」预测下一个货品编号"
                "（类别前缀 + 该号段最大流水+1，按流水长度补零）",
        "note": "★ 只读、不占号：预测与保存之间可能被别人用掉，保存时还会再查重一次。"
                "category 留空且本店规则是按类别出号 ⇒ 回 need_category=true（不是报错），"
                "界面拿它写「先选类别」的提示；真到保存时没类别才会拦。",
        "example": {"category": "1-01-"}},
    "product.salesHistory": {
        "cat": "基础资料", "label": "某货品的销售明细",
        "desc": "查一个货品的最近销售明细行（新增销售单页的「最近销售」面板用它）",
        "note": "★ 不要改用 bill.list 反查 —— 销售单列表不含明细行，永远查不到。",
        "example": {"hpbh": "1123454552", "customer": "", "limit": 8}},
    "archive.stopStatus": {
        "cat": "基础资料", "label": "停用档案清单",
        "desc": "一次回暂停往来的客户/供应商与停止使用、停止销售、停止采购的货品（编号 + 状态文案）",
        "note": "只回**停着的**那些，正常档案不回 ⇒ 结果很小。界面拿它给每一处渲染档案名的地方"
                "贴标签（单据、库存、报表、草稿、首页），不再逐条 SQL 加列。"
                "编号同时回 trim 过的与原值（现网有 \" 12454514\" 这种带空格的），两个都建索引才不会漏标。"
                "超过 cap 会回 truncated_xxx=true，那种情况下后段会漏标——不静默。",
        "example": {"cap": 2000}},
    "supplier.search": {
        "cat": "基础资料", "label": "供应商搜索",
        "desc": "按名称 / 编号 / 拼音码分页查供应商，带回应付与预付",
        "note": "默认只回正常往来的（StopUse=0）。show_stopped=1 连暂停往来的一起回，"
                "只给档案页那个开关用；开单页选供应商不传，暂停往来的一律不出现在面板里。",
        "example": {"keyword": "", "limit": 20, "offset": 0}},
    "supplier.get": {
        "cat": "基础资料", "label": "供应商详情",
        "desc": "按编号取一个供应商的档案", "example": {"code": "ces001"}},
    "supplier.types": {
        "cat": "基础资料", "label": "供应商类别",
        "desc": "列供应商类别（下拉数据源）", "example": {}},
    "supplier.prices": {
        "cat": "基础资料", "label": "供应商进价记录",
        "desc": "查某供应商的历史进货价", "example": {"code": "ces001", "limit": 20}},
    "supplier.save": {
        "cat": "基础资料", "label": "新增/编辑供应商",
        "desc": "写库动作：保存供应商档案", "note": "★ 会改数据。",
        "example": {"code": "", "name": "新供应商", "py": "XGYS", "phone": ""}},
    "customer.search": {
        "cat": "基础资料", "label": "客户搜索",
        "desc": "按名称 / 编号 / 拼音码分页查客户，带回应收与预付",
        "note": "默认只回正常往来的（StopUse=0）。show_stopped=1 连暂停往来的一起回，"
                "只给档案页那个开关用；开单页选客户不传。占位行 '---' 两种情况都不回。",
        "example": {"keyword": "", "limit": 20, "offset": 0}},
    "customer.get": {
        "cat": "基础资料", "label": "客户详情",
        "desc": "按编号取一个客户的档案", "example": {"code": "000005"}},
    "customer.types": {
        "cat": "基础资料", "label": "客户类别",
        "desc": "列客户类别（下拉数据源）", "example": {}},
    "customer.save": {
        "cat": "基础资料", "label": "新增/编辑客户",
        "desc": "写库动作：保存客户档案", "note": "★ 会改数据。",
        "example": {"code": "", "name": "新客户", "py": "XKH", "phone": ""}},
    "price.resolve": {
        "cat": "基础资料", "label": "取价",
        "desc": "按客户 + 货品 + 单位算出应该用的售价（客户协议价优先于默认价）",
        "note": "开单页选完货品后调它取价；price_trace 打开会带回是**哪条规则**命中的价，排价常用。",
        "example": {"customer": "000005", "hpbh": "1123454552", "unit": "个",
                    "price_type_xs": "", "price_trace": True}},

    # ---------------- 库存 ----------------
    "inventory.query": {
        "cat": "库存", "label": "库存查询",
        "desc": "分页查库存数量与金额，可按仓库、类别标记过滤",
        "note": "金额列在 V3 某些版本里是坏的（厂商侧问题），对不上时以数量为准。",
        "example": {"keyword": "", "warehouse": "", "flag": "all", "hpbh": "",
                    "limit": 20, "offset": 0}},
    "inventory.flow": {
        "cat": "库存", "label": "出入库流水",
        "desc": "查某货品一段时间内的出入库流水",
        "note": "★ V3 自带的结存快照不可信，页面上的结存是按流水自己递推出来的。",
        "example": {"hpbh": "1123454552", "date_from": "YYYY-MM-01", "date_to": "YYYY-MM-DD",
                    "limit": 50}},

    # ---------------- 报表 ----------------
    "report.sales.summary": {
        "cat": "报表", "label": "销售汇总",
        "desc": "一段时间内的销售额 / 单数 / 均价",
        "example": {"date_from": "YYYY-MM-01", "date_to": "YYYY-MM-DD", "customer": ""}},
    "report.sales.trend": {
        "cat": "报表", "label": "销售趋势",
        "desc": "按天/周/月粒度的销售趋势序列",
        "example": {"date_from": "YYYY-MM-01", "date_to": "YYYY-MM-DD",
                    "granularity": "day", "customer": ""}},
    "report.customer.ranking": {
        "cat": "报表", "label": "客户排行",
        "desc": "按销售额排名的客户榜", "example": {"limit": 10}},
    "report.product.ranking": {
        "cat": "报表", "label": "货品排行",
        "desc": "按销量/销售额排名的货品榜",
        "note": "大数据量时是这台机器上最慢的动作之一（实测可跑几十秒），"
                "所以它走后台任务而不是同步等待。",
        "example": {"limit": 10}},
    "report.receivables.summary": {
        "cat": "报表", "label": "往来账与账龄",
        "desc": "客户应收汇总，可按账龄段与关键字过滤",
        "note": "账龄口径是自己按最早未收单算的；某些客户返回 '---' 表示无发生额，不是零。"
                "★ 合计与账龄分段一律按全量客户算（含暂停往来的），show_stopped 只决定"
                "items 列不列那几家，并回 stoppedHidden={count,amount} 让界面说清差额；"
                "占位行 '---'（已删除的客户汇总）任何时候都不算进合计。",
        "example": {"keyword": "", "aging": "", "only_receivable": True, "limit": 200}},
    "report.customer.product.detail": {
        "cat": "报表", "label": "客户×货品明细",
        "desc": "某客户在某段时间内买过哪些货品的明细",
        "example": {"customer": "000005", "date_from": "YYYY-MM-01", "date_to": "YYYY-MM-DD",
                    "limit": 50}},
    "report.customer.statement": {
        "cat": "报表", "label": "客户对账单",
        "desc": "某客户一段时间的对账流水（发生额、余额逐笔）",
        "note": "with_lines=1 才返回 items[].lines（一行一个货品的货品层）；"
                "默认关 —— 这一层比流水行多一个数量级，只有导出「月份对账单」版式要。"
                "lines 里 quantity=开单单位数量(S_SLUnit)、price=单价(D_DJ)、"
                "amount=折后金额(J_JEZK)，实测 Σamount == 单头 J_JEZKSUM。",
        "example": {"customer": "000005", "date_from": "YYYY-MM-01", "date_to": "YYYY-MM-DD",
                    "with_lines": 0}},
    "report.purchase.summary": {
        "cat": "报表", "label": "进货汇总",
        "desc": "一段时间内的进货额与单数，可按供应商过滤",
        "example": {"date_from": "YYYY-MM-01", "date_to": "YYYY-MM-DD", "supplier": ""}},
    "report.purchase.trend": {
        "cat": "报表", "label": "进货趋势",
        "desc": "按天/周/月粒度的进货趋势",
        "example": {"date_from": "YYYY-MM-01", "date_to": "YYYY-MM-DD", "granularity": "day"}},
    "report.supplier.ranking": {
        "cat": "报表", "label": "供应商排行",
        "desc": "按进货额排名的供应商榜", "example": {"limit": 10}},
    "report.profit.summary": {
        "cat": "报表", "label": "毛利汇总",
        "desc": "一段时间的毛利与毛利率",
        "note": "★ 成本数据不完整的账套会返回 null（页面显示「—」），不会假装是 0。",
        "example": {"date_from": "YYYY-MM-01", "date_to": "YYYY-MM-DD"}},
    "diag.report.gradient": {
        "cat": "报表", "label": "报表诊断（只读）",
        "desc": "逐级放大查询量，定位报表在哪个数据量上崩",
        "note": "排障用；只读，不改数据。", "example": {"only": ""}},

    # ---------------- 打印 ----------------
    "print.salesbill": {
        "cat": "打印", "label": "打印销售单",
        "desc": "在 Agent 所在那台电脑上调用默认或指定打印机打一张销售单",
        "note": "mode=system 走系统打印；mode=image 返回位图给前端预览，"
                "此时 dpi 留空由 Agent 按页数自适应（小程序 setData 有 1MB 上限）。",
        "example": {"bill_no": "XS00000001", "mode": "system", "copies": 1,
                    "printer": "", "format": "", "dpi": None}},
    "print.printer.list": {
        "cat": "打印", "label": "打印机列表",
        "desc": "列出这台机器上可用的打印机", "example": {}},
    "print.format.list": {
        "cat": "打印", "label": "打印方案列表",
        "desc": "列 V3 报表引擎里的打印方案（销售出货单是 rpt_type=BillXS）",
        "example": {"rpt_type": "BillXS", "db": "AccMaster"}},
    "print.format.get": {
        "cat": "打印", "label": "取 V3 打印格式",
        "desc": "返回某单据的纸张、分页、行高、明细查询 SQL 与字段坐标布局",
        "note": "bill_name 可换购/销/盘点/调拨等单据。",
        "example": {"bill_name": "销售出货单"}},
    "print.format.def": {
        "cat": "打印", "label": "取打印方案定义",
        "desc": "取一个报表方案的原始定义（ReportMachine 3.0）",
        "example": {"rpt_name": "销售出货单(折后价)", "rpt_type": "BillXS", "db": "AccMaster",
                    "include_text": True}},

    # ---------------- 燕秀零件处理 ----------------
    "yxparts.match": {
        "cat": "燕秀零件处理", "label": "零件清单匹配",
        "desc": "把手机上传的表格矩阵按桌面同一套逻辑解析并匹配 HPKhType，返回逐行 hpbh/单位/状态",
        "note": "App 只传原始二维数组（含表头行），表头识别/归一化/匹配全在 Agent；"
                "单次上限 2000 行、512KB。表头认不出回 YX_HEADER_NOT_FOUND（不会静默回空行），"
                "本账套物料库为空回 YX_ACCOUNT_SET_EMPTY。",
        "example": {"sheets": [{"name": "全部1", "rows": [
            ["序号", "零件名称", "数量", "采购规格"],
            ["1", "杯头螺丝", "2", "M6x20"]]}]}},
    "yxparts.suggest": {
        "cat": "燕秀零件处理", "label": "匹配候选（相似度）",
        "desc": "给未匹配行推荐候选物料，与桌面双击弹窗同一函数同一排序",
        "example": {"name": "杯头螺丝", "spec": "M6x35", "actual": "", "keyword": ""}},
    "yxparts.remember": {
        "cat": "燕秀零件处理", "label": "记住人工确认",
        "desc": "把人工确认的「客户写法 → hpbh」写进本地记忆库，下次同写法自动命中",
        "note": "只写 Agent 本地记忆库（logs/yx_matchlib.db），不动业务库；"
                "hpbh 必须存在于物料库，否则回 YX_ITEM_NOT_FOUND。",
        "example": {"key": "杯头螺丝M6x20", "customerName": "杯头螺丝M6x20",
                    "hpbh": "000200064", "unit": "支"}},
    "yxparts.memory.list": {
        "cat": "燕秀零件处理", "label": "匹配记忆列表",
        "desc": "列出本地匹配记忆（客户写法、编号、单位、命中次数、最后使用时间）",
        "example": {}},
    "yxparts.memory.delete": {
        "cat": "燕秀零件处理", "label": "删除匹配记忆",
        "desc": "按 id 删除一条匹配记忆（删错了写法会重新变未匹配）",
        "example": {"id": 1}},
    "yxparts.capabilities": {
        "cat": "燕秀零件处理", "label": "物料库现状",
        "desc": "回报当前账套的 HPKhType / hpdan 行数、客户类别编号与记忆条数",
        "note": "App 用它显示「客户品名库还没铺」的引导；HPKhType 为 0 时匹配一定零命中。"
                "lbtypes 是本库 KHType 里的客户类别清单（导入界面选它）；"
                "配置里的类别不在清单时，lbtypeKh 自动让位给清单第一项。",
        "example": {}},
    "yxparts.importCustomers": {
        "cat": "燕秀零件处理", "label": "导入燕秀品名（写 HPKhType）",
        "desc": "按「货品名称」查 hpdan 取编号/单位，逐行写入 HPKhType 客户品名对照",
        "note": "逐行 INSERT（HPKhType 触发器要求单行）；判重按主键 LbType_Kh+Hpbh+HpUnitName；"
                "触发器还会校验 KHType（客户类别）与 HpdanEx（货品+单位）。"
                "部分失败不算错，逐行结果在 data.outcomes；整批不可执行才回错误码。"
                "入参二选一：rows（已解析行）或 sheets（App 传来的原始矩阵，表头在此识别）；"
                "dryRun=true 只预检不写库，返回逐行结果给界面预览。",
        "example": {"lbtypeKh": "K01-", "dryRun": True, "sheets": [
            {"name": "Sheet1", "rows": [
                ["货品名称", "客户品名", "采购规格", "实际尺寸"],
                ["不锈钢内六角6*20", "杯头螺丝", "M6*20", ""]]}]}},
}
