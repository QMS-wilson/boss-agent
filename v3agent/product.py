# -*- coding: utf-8 -*-
"""货品主数据（Hpdan + HpdanUnit）读写。2026-09-27 从 master.py 拆出。

★ 为什么要拆出来单独写
  多单位不是"界面上多几个框"：V3 的建模是**子表**（HpdanUnit 一单位一行，
  每单位带 `UnitRate` 与**自己的一整套价**），主表只留 `HpUnit` 基本单位。
  而旧 `save_product` 只 INSERT 一条 `UnitRate=1, UnitBasic=1` 的单位行，
  **入参 units 从来没被读过** ⇒ 前端填的多单位全被丢掉。

★★ 三条铁律（都是踩过坑定下来的，别改回去）
  1. **不认识的入参必须报错，不许静默丢弃。**
     旧行为：前端发 `baseUnit/salePriceDefault/costPrice/barcode/category`，
     Agent 只认 `unit/sale_price/purchase_price` ⇒ 用户看到"保存成功"而条码根本没存。
     见 `_assert_known_keys`。
  2. **列表与详情必须同形。** 本项目已两次栽在"详情有、列表没有"
     （`purchase_price` 漏在列表、`recent_sale_price` 漏在详情）。
  3. **单位行只做「停用」不做物理删除** —— 历史单据的 `XSMX.HpUnitName`
     还引用着这个名字，删了历史就查不到单位。

★ SQL 口径：目标库实测是 **SQL Server 2008 R2 (10.50)**
  ⇒ 分页用 `ROW_NUMBER()`（`OFFSET/FETCH` 报 102）、字符串聚合用
  `FOR XML PATH('') + TYPE).value()`（`STRING_AGG` 要 2017+）。
"""

import re
import uuid
from decimal import Decimal

from . import sql as db

# ---------------- 契约：允许写入的列 ----------------
# 入参键 -> Hpdan 文本列
TEXT_ATTRS = {
    "spec": "Ggxh",                 # 规格型号
    "barcode": "UnitBarcode",       # 条码（主表这条 = 基本单位条码）
    "brand": "HpBrandName",         # 品牌
    "manufacturer": "sccd",         # 生产厂商
    "quality": "zlbz",              # 质量标准
    "series": "HpRange",            # 系列
    "supplier": "GysMaster",        # 主供应商（Gysdan.ItemBh）
    "location": "CkStation",        # 仓位
    "remark": "BZ_Hpdan",           # 货品备注
    "pack_unit": "UnitPack",        # 包装单位
    "pack_spec": "UnitGgxh",        # 包装规格
}
# 入参键 -> (Hpdan 列名, 中文名)
NUM_ATTRS = {
    "min_order": ("MinOrderSl", "最少起订"),
    "return_days": ("BackDateLimit", "退货期限(天)"),
    "weight": ("HpWeight", "单重"),
    "length": ("HpLen", "长度"),
    "width": ("HpWidth", "宽度"),
    "height": ("HpHeight", "厚度"),
    "volume": ("HpVolume", "体积"),
    "discount": ("HpDiscount", "货品折扣"),
    "shelf_days": ("BzDays", "库存保质期"),
    "shelf_days_out": ("BzDay2", "出品保质期"),
    "stock_max": ("kcsx", "库存上限"),
    "stock_min": ("kcxx", "库存下限"),
    "points_rate": ("JFJEWhen", "积分率"),
    "unit_piec": ("HpUnitPiec", "每卷数量"),
}
# 入参键 -> Hpdan bit 列
BIT_ATTRS = {
    "serial": "IsSil",              # 序列号管理
    "batch": "IsScpc",              # 必需批次
    "points_stop": "StopJF",        # 暂停积分
    "discount_stop": "StopZK",      # 暂停折扣
    "whole_piece": "HPLargeType",   # 整件货品
}
# 价格档：入参键 -> (列名, 中文名)。★ 这些列在 Hpdan 与 HpdanUnit 上**同名**，
#   所以同一张表能同时喂给"主表基本单位价"和"每单位价"。
PRICE_ATTRS = (
    ("sale_price", "D_SJ1", "默认销价"),
    ("price_retail", "D_SJ0", "零售价"),
    ("price_discount", "D_SJ2", "销售价2"),
    ("price_sale3", "D_SJ3", "销售价3"),
    ("price_sale4", "D_SJ4", "销售价4"),
    ("price_sale5", "D_SJ5", "销售价5"),
    ("price_member", "D_SJMember", "会员价"),
    ("price_promo", "D_CXDJ", "促销价"),
    ("price_lowest", "D_SJDN", "最低销价"),
    ("price_highest", "D_SJUP", "最高销价"),
    ("rebate", "D_Reba", "返利金"),
    ("purchase_price", "D_JH", "参考进价"),
)
PRICE_KEYS = set(k for k, _c, _l in PRICE_ATTRS)
PRICE_COLS = set(c for _k, c, _l in PRICE_ATTRS)
# ★ 旧代码把 D_CXDJ 标成"最低销价"（真名是**促销价**，最低销价是 D_SJDN）。
#   key 名不改（前端在用），但 label 必须对，否则页面上"最低销价"显示的是促销价。

# 明确"只读/回显"的键：带着它们不报错，但会在响应里列出来（不静默）
READONLY_KEYS = {
    "hpbh", "id", "created", "stock", "recent_sale_price", "recent_sale_bill",
    "category_code", "client_token", "operator", "device", "source",
    "merchant", "database", "token",
}
# 货品状态：Hpdan.StopUse 枚举（实测字典 34-系统配置项与枚举字典.md:810）
#   0 新建货品 / 1 正常使用 / 2 商城上架 / 5 停止采购 / 6 停止销售 / 9 停止使用
STOP_NORMAL, STOP_DISABLED = 1, 9
STOP_NO_PURCHASE, STOP_NO_SALE = 5, 6
STOP_NAMES = {0: "新建货品", 1: "正常使用", 2: "商城上架",
              5: "停止采购", 6: "停止销售", 9: "停止使用"}


def stop_name(v):
    """状态文案。字典之外取值就如实写「状态 N」，不许悄悄当正常。"""
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return "状态未知"
    return STOP_NAMES.get(n, "状态 %d" % n)


def status_clause(usage="", show_stopped=False):
    """列表/选择器要排掉哪些状态的货品，返回 SQL 片段列表（全是常量，不带参数）。

    ★ 「停用」只算 9：5/6 是**方向**上的停（停止采购、停止销售），货品本身还在用，
      档案列表里不该被当成停用藏起来。
    ★ 开单页（usage=sale/purchase）不看 show_stopped：选出来就是要往单上写的，
      停用的、停售的（销售单）/停购的（采购单）一律不给，档案页那个开关管不到这里。
    """
    u = str(usage or "").strip().lower()
    if u in ("sale", "purchase"):
        show_stopped = False
    out = []
    if not show_stopped:
        out.append("ISNULL(StopUse,0) <> %d" % STOP_DISABLED)
    if u == "sale":
        out.append("ISNULL(StopUse,0) <> %d" % STOP_NO_SALE)
    elif u == "purchase":
        out.append("ISNULL(StopUse,0) <> %d" % STOP_NO_PURCHASE)
    return out

# 手机端一个货品最多录几档单位。★ 真源在这一行，前端那份只是提前挡一下（别拿它当规矩）。
# 双单位够用（基本单位 + 一个换算单位），三档以上去 V3 客户端录；换算率一律整数。
MAX_UNITS = 2


def _num(v):
    return float(Decimal(str(v or 0)))


def _dec(v, field):
    """把 JSON 里的数字转 Decimal；空 -> 0，坏 -> 报错（不静默变 0）。"""
    if v is None or v == "":
        return Decimal("0")
    try:
        return Decimal(str(v).strip())
    except Exception:  # noqa: BLE001
        raise ValueError("%s 必须是数字，收到的是 %r" % (field, v))


def _txt(v, field, maxlen):
    s = str(v if v is not None else "").strip()
    if len(s) > maxlen:
        raise ValueError("%s 太长了（最多 %d 个字），现在是 %d 个" % (field, maxlen, len(s)))
    return s


def _assert_known_keys(data):
    """★ 铁律 1：拼错/不认识的键直接报错。

    只放行三类：契约里能写的列、明确只读的键、以及 `prices` 这个嵌套别名
    （`prices` 在进函数前已被摊平，摊平后会被 pop 掉）。
    """
    known = (set(TEXT_ATTRS) | set(NUM_ATTRS) | set(BIT_ATTRS) | PRICE_KEYS
             | READONLY_KEYS | {"code", "name", "py", "category", "unit", "units",
                                "stop", "cost_price", "prices", "create_only"})
    bad = sorted(set(data) - known)
    if bad:
        raise ValueError(
            "product.save 不认识这些入参：%s。写错的字段**不会被保存**（也不会被悄悄忽略）；"
            "可写字段见 op_docs 的 product.save。" % "、".join(bad))


def _flatten_prices(data):
    """允许 `prices: {sale_price: 1}` 的嵌套写法，摊平成顶层键后返回只读键清单。"""
    nested = data.pop("prices", None)
    if isinstance(nested, dict):
        for k, v in nested.items():
            if k in PRICE_KEYS or k == "cost_price":
                data.setdefault(k, v)
            else:
                raise ValueError("价格档 %r 不存在，可用：%s" % (
                    k, "、".join(sorted(PRICE_KEYS | {"cost_price"}))))


def _attr_values(data):
    """归一化非价格属性 -> {Hpdan 列名: 值}。"""
    out = {}
    for key, col in TEXT_ATTRS.items():
        if key in data:
            out[col] = _txt(data[key], key, 100 if col != "BZ_Hpdan" else 400)
    for key, (col, label) in NUM_ATTRS.items():
        if key in data:
            out[col] = _dec(data[key], label)
    for key, col in BIT_ATTRS.items():
        if key in data:
            out[col] = 1 if data[key] in (1, "1", True, "true", "是") else 0
    return out


def _price_values(data, cols=None):
    """归一化价格 -> {列名: Decimal}。cols=None 时用主表/子表通用的同名列集合。"""
    out = {}
    for key, col, _label in PRICE_ATTRS:
        if key in data:
            out[col] = _dec(data[key], _label)
    if cols is not None:
        for col in list(out):
            if col not in cols:
                out.pop(col)
    return out


# ---------------- 单位子表 ----------------

def split_qty(qty, rate):
    """把"客户填的数量"拆成明细表要的三个口径：(该单位数量, 散数, 基本单位数量)。

    ★ 为什么单列一个函数：这是多单位最容易写错、又最难从界面上看出错的地方
      （错了不会报错，只会让库存与成本悄悄差一个倍率）。
    ★ **rate == 1 时必须与多单位改造前逐字节一致**（散数 = 数量、基本数量 = 数量）——
      单单位客户已经开了几年的单据口径不许被这次改动动到。
    """
    qty = Decimal(str(qty or 0))
    rate = Decimal(str(rate if rate not in (None, "") else 1)) or Decimal("1")
    if rate == 1:
        return qty, qty, qty
    return qty, Decimal("0"), (qty * rate).quantize(Decimal("0.01"))


def _norm_units(data):
    """把入参 units 归一化。返回 None = 本次不带 units（保持旧行为，不动子表）。"""
    if "units" not in data:
        return None
    raw = data.get("units")
    if raw in (None, "", {}, []):
        return None
    if not isinstance(raw, (list, tuple)):
        raise ValueError("units 必须是数组，收到 %r" % type(raw).__name__)
    out, seen = [], {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("units 里每一项都得是对象（单位名/倍率/价格）")
        # ★ 单位项里的键也要挡：`salePrice` 这种驼峰写错不会报错，只会让这一档价静默丢掉
        bad = sorted(set(item) - ({"unit", "name", "basic", "is_basic", "rate",
                                   "stop", "stopped", "barcode"} | PRICE_KEYS))
        if bad:
            raise ValueError("units 里有不认识的字段：%s（可用的价格档：%s）" % (
                "、".join(bad), "、".join(sorted(PRICE_KEYS))))
        name = _txt(item.get("unit") or item.get("name"), "单位名称", 20)
        if not name:
            raise ValueError("有一个单位没填名字")
        low = name.lower()
        if low in seen:
            raise ValueError("单位名重复：「%s」。同一个货品里每个单位只能有一行" % name)
        seen[low] = True
        basic = bool(item.get("basic") or item.get("is_basic"))
        raw = item.get("rate")
        if basic:
            rate = Decimal("1")          # 基本单位恒等于 1，填了别的也按 1 写
        elif raw in (None, ""):
            rate = None                  # 只有一个单位时下面会认它作基本单位，先不逼填
        else:
            rate = _dec(raw, "1 单位等于多少基本单位")
            if rate <= 0:
                raise ValueError("单位「%s」的换算率必须大于 0（1 %s 等于多少基本单位）" % (name, name))
            if rate != rate.to_integral_value():
                raise ValueError("单位「%s」的换算率要填整数（1 %s 等于几%s），现在是 %s"
                                 % (name, name, "基本单位", rate))
            if rate > Decimal("9999999"):
                raise ValueError("单位「%s」的换算率太大（%s），库里这列最多到 9999999" % (name, rate))
        out.append({
            "unit": name, "rate": rate, "basic": basic,
            "stop": bool(item.get("stop") or item.get("stopped")),
            "barcode": _txt(item.get("barcode"), "单位条码", 80),
            "has_barcode": "barcode" in item,
            "prices": _price_values(item),
        })
    basics = [u for u in out if u["basic"]]
    if not basics:
        if len(out) == 1:
            out[0]["basic"] = True       # 只有一个单位时它就是基本单位，别逼用户再点一下
            out[0]["rate"] = Decimal("1")
        else:
            raise ValueError("必须指定一个基本单位（库存与成本按它记账）")
    if len(basics) > 1:
        raise ValueError("基本单位只能有一个，现在标了 %d 个：%s" % (
            len(basics), "、".join(u["unit"] for u in basics)))
    if len(out) > MAX_UNITS:
        raise ValueError("手机端最多录 %d 个单位（基本单位 + 1 个换算单位），现在给了 %d 个：%s。"
                         "要多档请到 V3 客户端录；货品本来就有几档的，也只能在 V3 里改。"
                         % (MAX_UNITS, len(out), "、".join(u["unit"] for u in out)))
    for u in out:
        if u["rate"] is None:            # 多单位时非基本单位必须说清"1 X 等于几"
            raise ValueError("单位「%s」没填换算率：1 %s 等于多少%s" % (
                u["unit"], u["unit"], [x["unit"] for x in out if x["basic"]][0]))
    return out


# ---------------- 包装单位 / 包装规格（主表那两列） ----------------

def _rate_text(d):
    """倍率写成不带尾零的串：`12.00` → `12`、`2.50` → `2.5`（库里 numeric(10,2) 回来带尾零）。"""
    s = format(Decimal(str(d or 0)), "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def pack_unit_of(units):
    """这一档货品按哪一行算包装：没停用的非基本单位里**倍率最大**的那一行。

    ★ 只有一个单位（或全部非基本单位都停用了）⇒ 返回 None，两列都不填 ——
      实测 48,590 个货品里单档单位的那 48,589 行 `UnitPack`/`UnitGgxh` 全是空的，
      V3 只给多单位货品填。
    """
    if not units:
        return None
    cand = [u for u in units if not u["basic"] and not u["stop"]]
    return max(cand, key=lambda u: u["rate"]) if cand else None


def pack_values(units):
    """按 V3 实测口径派生 `UnitPack`（包装单位）与 `UnitGgxh`（包装规格）。

    ★★ 真源是 `85485455515418 测试多单位`（V3 客户端 2026-09-27 19:02 建，全库唯一带值的）：
         UnitPack = `件*个`（大单位*基本单位）    UnitGgxh = `1*12`（1*比率）
       别把两个公式按字面对调 —— V3 的列标题就是「包装单位=件*个、包装规格=1*12」，
       对调了 V3 自己界面上就显示成"包装单位 1*12"。
    ★ 只要本次带了 units 就**给个准数**：算不出包装（单档、或大单位都停用了）就给空串，
      不是"不动"。否则"把箱停用了、档案上还写着 箱*个"这种谎会一直挂在 V3 界面和报表里。
    ⚠ 只有 2 档单位的样本，两件事还没对证（拿到客户端证据前别改判据）：
       ① 3 档以上取哪一档（这里按最大倍率，但我们手机端只放两档）；
       ② 倍率带小数写 `1*2.5` 还是 `1*2.50`（这里去尾零，比率已限整数）。
    ⚠ 两列都是 nvarchar(40)，而单位名各允许 20 字 ⇒ 极端拼法会超 40，那种情况按"没包装"给空串
       （宁可不显示，也不要发一条被库顶回来、把整次保存带崩的 INSERT）。
    """
    base = [u for u in (units or []) if u["basic"]]
    empty = {"UnitPack": "", "UnitGgxh": ""}
    big = pack_unit_of(units)
    if not big or not base:
        return empty
    pack, spec = "%s*%s" % (big["unit"], base[0]["unit"]), "1*%s" % _rate_text(big["rate"])
    return empty if (len(pack) > 40 or len(spec) > 40) else {"UnitPack": pack, "UnitGgxh": spec}


def _auto_pack(d, units):
    """用户**手动给了** pack_unit/pack_spec 就以他给的为准（已在 attrs 里），这里只补没人管的。"""
    if not units or "pack_unit" in d or "pack_spec" in d:
        return {}
    return pack_values(units)


def bill_usage(cur, hpbh):
    """该货品产生过多少行业务明细（销售/采购/两类退货）。

    ★★ V3 的口径（2026-09-27 用户明确）：**产生过业务数据的货品，
       不许改单位倍率、不许把单位从档案里摘掉，只能停用单位。**
       原因是历史单据只认"当时那个单位 + 当时的倍率"，倍率一改，
       老单据的件数/散数口径就和库存流水对不上了。
       所以这个数既是服务端的硬拦依据，也要回给界面去**锁掉输入框**
       （别让用户填完点保存才被弹回来）。
    """
    code = str(hpbh or "").strip()
    row = db.q1(
        cur,
        """SELECT (SELECT COUNT(*) FROM XSMX WHERE LTRIM(RTRIM(Hpbh))=%s),
                  (SELECT COUNT(*) FROM JHMX WHERE LTRIM(RTRIM(Hpbh))=%s),
                  (SELECT COUNT(*) FROM XSTHMX WHERE LTRIM(RTRIM(Hpbh))=%s),
                  (SELECT COUNT(*) FROM JHTHMX WHERE LTRIM(RTRIM(Hpbh))=%s)""",
        (code, code, code, code))
    return sum(int(x or 0) for x in row)


def _used_unit_names(cur, hpbh):
    """该货品在销售/采购明细里出现过的单位名（改/停这些单位要拦）。"""
    rows = db.qall(
        cur,
        """SELECT DISTINCT LTRIM(RTRIM(HpUnitName)) FROM (
               SELECT HpUnitName FROM XSMX WHERE LTRIM(RTRIM(Hpbh))=%s
               UNION ALL
               SELECT HpUnitName FROM JHMX WHERE LTRIM(RTRIM(Hpbh))=%s) t
           WHERE LTRIM(RTRIM(ISNULL(HpUnitName,''))) <> ''""",
        (str(hpbh).strip(), str(hpbh).strip()))
    return set(r[0] for r in rows if r[0])


def read_units(cur, hp_guid):
    """该货品的全部单位行（含每单位一套价）。"""
    if not hp_guid:
        return []
    rows = db.qall(
        cur,
        """SELECT UnitName, UnitRate, UnitBasic, UnitStop, ISNULL(UnitBarcode,''),
                  D_SJ1, D_JH, D_SJ0, D_SJ2, D_SJ3, D_SJ4, D_SJ5,
                  D_SJMember, D_CXDJ, D_SJDN, D_SJUP, D_Reba
           FROM HpdanUnit
           WHERE HpIdenGuid = CONVERT(uniqueidentifier, %s) AND ISNULL(UnitName,'') <> ''
           ORDER BY UnitBasic DESC, UnitRate""",
        (str(hp_guid),))
    out = []
    for u in rows:
        item = {"unit": u[0], "rate": _num(u[1]), "basic": bool(u[2]),
                "stop": bool(u[3]), "barcode": u[4] or ""}
        for key, col, _label in PRICE_ATTRS:
            idx = {"D_SJ1": 5, "D_JH": 6, "D_SJ0": 7, "D_SJ2": 8, "D_SJ3": 9,
                   "D_SJ4": 10, "D_SJ5": 11, "D_SJMember": 12, "D_CXDJ": 13,
                   "D_SJDN": 14, "D_SJUP": 15, "D_Reba": 16}[col]
            item[key] = _num(u[idx])
        out.append(item)
    return out


def _guard_units(cur, hp_guid, units, hpbh):
    """写子表**之前**先把 V3 会拦的两件事挡下来（报错要早、要用人话）。

    ★ 为什么必须挡在 Hpdan 的 UPDATE 之前：主表触发器 TRI_A_Hpdan 对
      成本价/售价/核算方法…一组列是**列级**保护，一旦排在后面，
      用户看到的会是 V3 那串"不允许修改核算方法、成本价…"，
      而不是"这个单位倍率改不了，请停用后新增"。
    """
    existing = _existing_units(cur, hp_guid)
    bills = bill_usage(cur, hpbh)
    used = _used_unit_names(cur, hpbh)
    want = dict((u["unit"].lower(), u) for u in units)
    for u in units:
        old = existing.get(u["unit"].lower())
        if old is None:
            continue
        name_ = str(old[0]).strip()
        if bool(old[1]) and not u["basic"] and name_ in used:
            raise ValueError("「%s」是这个货品的基本单位，而且已经开过单，不能降级。"
                             "要换记账单位请新建货品、把旧货品停用。" % name_)
        if bills and Decimal(str(old[3] or 0)) != u["rate"].quantize(Decimal("0.01")):
            raise ValueError("「%s」的倍率改不了：这个货品已经有 %d 行业务单据，"
                             "老单据认的是当时那个倍率。要换成别的折算，请**停用这个单位、"
                             "再新增一个单位**。" % (name_, bills))
    for name, row in existing.items():
        if name in want or bool(row[1]):
            continue
        if bills:
            raise ValueError("单位「%s」不能从档案里去掉：这个货品已经有 %d 行业务单据。"
                             "要不再用它，请把这一行的「停用」打开再保存。" % (row[0], bills))
        if str(row[0]).strip() in used:
            raise ValueError("单位「%s」已经开过单，不能在这次保存里去掉。要么留着（可停用），"
                             "要么先把用到它的单据处理掉。" % row[0])


def _existing_units(cur, hp_guid):
    existing = {}
    for row in db.qall(
            cur,
            """SELECT LTRIM(RTRIM(UnitName)), UnitBasic, CONVERT(varchar(40), IdenGuid),
                      ISNULL(UnitRate,0), CONVERT(int, ISNULL(UnitStop,0)), ISNULL(UnitBarcode,''),
                      CONVERT(int, ISNULL(UnitPackage,0))
               FROM HpdanUnit WHERE HpIdenGuid = CONVERT(uniqueidentifier, %s)""",
            (str(hp_guid),)):
        # ★ 键按小写归一：库里存着 "PC" 而前端提交 "pc" 时，按原值匹配会被当成
        #   "新单位" 走 INSERT，然后一头撞在 UQ(HpIdenGuid,UnitName) 上
        #   （库本身不区分大小写，我们的查重区分了 ⇒ 报成"单位名重复"很误导）。
        existing[str(row[0]).strip().lower()] = row
    return existing


def _write_units(cur, hp_guid, units, hpbh, main_prices=None):
    """upsert 单位行。**payload 是全集**：库里多出来的行按停用处理，不物删。

    ★★ 基本单位那一行的价格**必须与主表逐列相等** —— 这不是我们的偏好，是
      `TRI_A_HpdanUnit` 的硬约束：基本单位行与 Hpdan 的 D_JH/D_SJ0~5/D_CXDJ/
      D_SJDN/D_SJUP 任一处不等，直接 raiserror「价格异常, 请重新录入价格」并回滚。
      所以基本单位的价一律取 `main_prices`（主表当前价 + 本次改动），
      界面上给基本单位单独填价是没有意义的（会被主表覆盖）。
    """
    existing = _existing_units(cur, hp_guid)
    used = _used_unit_names(cur, hpbh)
    bills = bill_usage(cur, hpbh)
    want = {}
    for u in units:
        want[u["unit"].lower()] = u
        if u["basic"] and main_prices:
            u["prices"] = dict(main_prices)
    # 包装单位那一行的标记（`HpdanUnit.UnitPackage`）与主表那两列是**一件事的两半**：
    # 主表写了 件*个，位就得在「件」那行；主表收回空串（大单位都停了），位也要摘掉。
    # 有业务单据时**两边一起不动**（`Update(UnitPack)` 在 TRI_A_Hpdan 的按列保护里，
    # 而且 V3 会拿 HxTri_Hpdan_ggxh 去刷历史单据行）。
    # ★ 实测 `TRI_A_HpdanUnit` 根本不判 UnitPackage（它只管 UnitName/UnitRate/UnitPID/
    #   UnitBarcode/UnitBasic），所以写它本身没有触发器风险。
    pk = pack_unit_of(units)          # None = 这一档货品现在没有可当包装的单位
    pack_edit = not bills

    for u in units:
        old = existing.get(u["unit"].lower())
        prices = u["prices"] or {}     # 已是 {HpdanUnit 列名: Decimal}
        rate = u["rate"].quantize(Decimal("0.01"))
        stop = 1 if u["stop"] else 0
        if old is not None:
            # （V3 会拦的两件事已在 `_guard_units` 提前挡掉，这里只负责写）
            # ★ 同样要逐列 diff：TRI_A_HpdanUnit 认的是 `Update(UnitRate)` / `Update(UnitBasic)`
            #   这种**列级**标志，只要列进了 SET 就算被更新（哪怕值一模一样），
            #   于是"只停用一个单位"也会被 HxCheckusestate_HpUnit 拦死。
            #   价格列不在它的保护清单里，照常写。
            sets, args = [], []
            if not _same(old[3], rate):
                sets.append("UnitRate=%s")
                args.append(rate)
            if bool(old[1]) != bool(u["basic"]):
                sets.append("UnitBasic=%s")
                args.append(1 if u["basic"] else 0)
            if int(old[4] or 0) != stop:
                sets.append("UnitStop=%s")
                args.append(stop)
            if u["has_barcode"] and str(old[5] or "").strip() != u["barcode"]:
                sets.append("UnitBarcode=%s")
                args.append(u["barcode"])
            if pack_edit and int(old[6] or 0) != (1 if u is pk else 0):
                sets.append("UnitPackage=%s")
                args.append(1 if u is pk else 0)
            for col, val in prices.items():
                sets.append("%s=%%s" % col)
                args.append(val)
            if sets:
                sets.append("TimeUpdate=GETDATE()")
                args += [str(hp_guid), u["unit"]]
                cur.execute("UPDATE HpdanUnit SET %s WHERE HpIdenGuid=CONVERT(uniqueidentifier,%%s) "
                            "AND LTRIM(RTRIM(UnitName))=%%s" % ", ".join(sets), tuple(args))
            continue
        names = ["HpIdenGuid", "UnitPID", "UnitName", "UnitRate", "UnitBasic", "UnitStaple",
                 "UnitPackage", "UnitBarcode", "UnitStop", "IsSend", "IdenGuid",
                 "TimeInsert", "TimeUpdate"]
        vals = ["CONVERT(uniqueidentifier,%s)", "0", "%s", "%s", "%s", "%s",
                "%s", "%s", "%s", "0", "%s", "GETDATE()", "GETDATE()"]
        args = [str(hp_guid), u["unit"], rate, 1 if u["basic"] else 0,
                1 if u["basic"] else 0, 1 if (pack_edit and u is pk) else 0, u["barcode"], stop,
                str(uuid.uuid4())]
        for col, val in prices.items():
            names.append(col)
            vals.append("%s")
            args.append(val)
        cur.execute("INSERT INTO HpdanUnit (%s) VALUES (%s)" % (
            ", ".join(names), ", ".join(vals)), tuple(args))

    # payload 里没有、库里还开着的行 -> 停用（历史单据仍引用这个名字）
    # 有业务数据时根本走不到这里：`_guard_units` 已经要求改成"停用"再保存。
    for name, row in existing.items():
        if name in want or bool(row[1]):
            continue
        cur.execute("UPDATE HpdanUnit SET UnitStop=1, TimeUpdate=GETDATE() "
                    "WHERE HpIdenGuid=CONVERT(uniqueidentifier,%s) AND LTRIM(RTRIM(UnitName))=%s",
                    (str(hp_guid), row[0]))


# ---------------- 读 ----------------

# 主表 SELECT 列（顺序即 _row_to_dict 的下标，别单独挪动）
_COLS = (
    ["hpbh", "hpmc", "Py", "LbType_Hp", "Ggxh", "UnitBarcode", "HpBrandName",
     "sccd", "zlbz", "HpRange", "GysMaster", "CkStation", "BZ_Hpdan",
     "UnitPack", "UnitGgxh", "HpUnit", "HpUnitEx", "HpUnitPiec", "HPLargeType",
     "MinOrderSl", "BackDateLimit", "HpWeight", "HpWidth", "HpLen", "HpHeight",
     "HpVolume", "HpDiscount", "BzDays", "BzDay2", "kcsx", "kcxx", "JFJEWhen",
     "IsSil", "IsScpc", "StopJF", "StopZK", "StopUse", "Cost_Price"],
    [c for _k, c, _l in PRICE_ATTRS],
)
COLS = _COLS[0] + _COLS[1]
IDX = dict((c, i) for i, c in enumerate(COLS))


def _sel():
    """带 ISNULL 的列清单（数值/文本都给 0/'' 兜底，避免 None 传到前端变 NaN）。"""
    parts = []
    for c in COLS:
        if c in ("hpbh", "hpmc", "Py"):
            parts.append("LTRIM(RTRIM(%s))" % c)
        elif IDX[c] >= len(_COLS[0]):
            parts.append("ISNULL(%s,0)" % c)
        elif c in ("Ggxh", "UnitBarcode", "HpBrandName", "sccd", "zlbz", "HpRange",
                   "GysMaster", "CkStation", "BZ_Hpdan", "UnitPack", "UnitGgxh",
                   "HpUnit", "HpUnitEx"):
            parts.append("ISNULL(%s,'')" % c)
        else:
            parts.append("ISNULL(%s,0)" % c)
    return ", ".join(parts)


def find_row(cur, hpbh):
    """一条货品的全部主表列，返回 {列名: 值}；不存在返回 None。"""
    rows = db.qall(
        cur,
        # ★ 多带一列 `hpbh_raw`（**不修剪的原值**）：现网 Hpdan.hpbh 是 nvarchar(30)，
        #   有货品存成 " 12454514"（前面带一个空格），而 Inventory.Hpbh 存的是同一个带空格的原值。
        #   拿修剪过的编号去等值查库存就查不到 ⇒ 症状是"列表上有库存、详情说这货品没库存记录"。
        #   列表那条 SQL 的 `i.Hpbh = t.hpbh` 用的是原值，所以两边必须都以原值为准。
        "SELECT %s, hpbh AS hpbh_raw FROM Hpdan WHERE LTRIM(RTRIM(hpbh)) = %%s" % _sel(),
        (str(hpbh).strip(),))
    if not rows:
        return None
    r = dict(zip(COLS, rows[0]))
    r["hpbh_raw"] = rows[0][len(COLS)]
    return r


def guid_of(cur, hpbh):
    r = db.q1(cur, "SELECT IDENGUID FROM Hpdan WHERE LTRIM(RTRIM(hpbh))=%s",
              (str(hpbh).strip(),))
    return r[0] if r else None


def category_name(cur, key):
    r = db.q1(cur, "SELECT Text FROM HpType WHERE LTRIM(RTRIM(Key_))=%s", (str(key or "").strip(),))
    return r[0] if r else ""


def list_types(cur):
    """货品类别（HpType）。编辑页的类别下拉与 `product.save` 的 category 校验同源。"""
    rows = db.qall(
        cur,
        """SELECT LTRIM(RTRIM(Key_)), ISNULL(Text,''), CONVERT(int, ISNULL(StopUse,0))
           FROM HpType ORDER BY Key_""")
    return [{"code": r[0], "name": r[1] or r[0], "stopped": bool(r[2])}
            for r in rows if str(r[0] or "").strip()]


def product_row(cur, hpbh, cfg=None):
    """一条货品的完整档案（详情与编辑页共用；★ 与列表同形）。"""
    r = find_row(cur, hpbh)
    if not r:
        raise LookupError("货品不存在：" + str(hpbh))
    guid = guid_of(cur, r["hpbh"])
    recent, recent_bill = _recent_sale_price(
        cur, r["hpbh"],
        ((cfg or {}).get("price") or {}).get("recent_price_field", "D_PriceTax"))
    units = read_units(cur, guid)
    # ★ 一次查两样：`有没有库存记录` 与 `账面数量`。
    #   只查 SUM 分不出"从没入过库"和"入过库但现在是 0"（COALESCE 都成 0），
    #   而这两种在开单页上的话完全不一样：前者该说"这货品还没有库存记录"，
    #   后者才是"账面 0、还差 N"。现网实测：货品档案 48,591 个，Inventory 里有行的只有 211 个
    #   ⇒ 分不清就会把 4.8 万个货全报成"欠货"。
    stock = db.q1(
        cur,
        "SELECT COUNT(*), COALESCE(SUM(S_KCSL),0) FROM Inventory WHERE Hpbh=%s",
        # ★ 用原值查，理由见 find_row 的 hpbh_raw 注释（" 12454514" 这种带空格的编号，
        #   拿修剪过的值去查会一条都查不到，被误判成"这货品从没入过库"）
        (r.get("hpbh_raw") or r["hpbh"],))
    lines = bill_usage(cur, r["hpbh"])
    out = {
        "hpbh": r["hpbh"], "code": r["hpbh"], "name": r["hpmc"], "py": r["Py"],
        "category_code": (r["LbType_Hp"] or "").strip(),
        "category": category_name(cur, r["LbType_Hp"]),
        "unit": r["HpUnit"], "units": units,
        "stock": _num(stock[1]),
        # 0/1：这货品在 Inventory 里到底有没有行。前端拿它区分"没记录"与"账面 0"
        "stock_record": 1 if int(stock[0] or 0) > 0 else 0,
        # ★ 详情**不按状态过滤**（按编号直取）：历史单据里的货品今天停用了，详情与单据回显还得打得开。
        "stop": int(r["StopUse"] or 0) == STOP_DISABLED,
        "stop_use": int(r["StopUse"] or 0),
        "status_name": stop_name(r["StopUse"]),
        # ★ 有业务数据 ⇒ 界面要把倍率输入框锁掉、删除按钮只留停用（见 bill_usage）
        "has_bills": lines > 0, "bill_lines": lines,
        "recent_sale_price": recent or 0, "recent_sale_bill": recent_bill or "",
    }
    for key, col in TEXT_ATTRS.items():
        out[key] = r[col] or ""
    for key, (col, _label) in NUM_ATTRS.items():
        out[key] = _num(r[col])
    for key, col in BIT_ATTRS.items():
        out[key] = bool(r[col])
    out["cost_price"] = _num(r["Cost_Price"])
    for key, col, label in PRICE_ATTRS:
        out[key] = _num(r[col])
    # ★ 价格清单：前 4 项与旧版顺序一致（旧页面默认视图不变形），第 5 项起是补进来的档
    out["prices"] = [
        {"key": "sale_price", "label": "默认销价", "value": out["sale_price"]},
        {"key": "recent_sale_price", "label": "最近销价", "value": out["recent_sale_price"]},
        {"key": "purchase_price", "label": "进价", "value": out["purchase_price"]},
        {"key": "cost_price", "label": "成本价", "value": out["cost_price"]},
        {"key": "price_retail", "label": "零售价", "value": out["price_retail"]},
        {"key": "price_discount", "label": "销售价2", "value": out["price_discount"]},
        {"key": "price_sale3", "label": "销售价3", "value": out["price_sale3"]},
        {"key": "price_sale4", "label": "销售价4", "value": out["price_sale4"]},
        {"key": "price_sale5", "label": "销售价5", "value": out["price_sale5"]},
        {"key": "price_member", "label": "会员价", "value": out["price_member"]},
        {"key": "price_promo", "label": "促销价", "value": out["price_promo"]},
        {"key": "price_lowest", "label": "最低销价", "value": out["price_lowest"]},
        {"key": "price_highest", "label": "最高销价", "value": out["price_highest"]},
        {"key": "rebate", "label": "返利金", "value": out["rebate"]},
    ]
    return out


def _recent_sale_price(cur, hpbh, field="D_PriceTax"):
    """最近一次销售价（Price_Kh，口径与 price.resolve_price 的 recent 分支一致）。"""
    row = db.q1(
        cur,
        """SELECT TOP 1 D_PriceTax, D_Price, D_PriceZK, LastSaleBillNo, LastSaleEx
           FROM Price_Kh
           WHERE LTRIM(RTRIM(Hpbh)) = %s AND LTRIM(RTRIM(ISNULL(LastSaleBillNo,''))) <> ''
           ORDER BY LastSaleEx DESC, Idencol DESC""",
        (str(hpbh).strip(),))
    if not row:
        return None, None
    price = _num(row[0] if field == "D_PriceTax" else row[1])
    if not price:
        price = _num(row[1]) or _num(row[2])
    return (price or None), (row[3] or None)


def _parse_units(cell):
    """把 '包|10.00,捆|40.00' 解成 [{unit,rate,basic}]（列表页要显示全部单位胶囊）。

    ★ SQL 里按 `UnitBasic DESC, UnitRate` 排过序 ⇒ **第一条就是基本单位**，
      这里据此补 basic 标记（列表不额外查一遍子表，48k 行经不起）。
    """
    out = []
    for part in str(cell or "").split(","):
        name, _sep, rate = part.rpartition("|")
        if not name.strip():
            continue
        try:
            out.append({"unit": name.strip(), "rate": float(rate or 1),
                        "basic": not out})
        except ValueError:
            continue
    return out


def list_products(cur, keyword="", limit=100, offset=0, show_stopped=False, usage=""):
    """货品列表（★ 与详情同形：单位是全部，不是 TOP 1）。

    ★ 分页用 ROW_NUMBER() 而不是 OFFSET/FETCH —— 目标库实测 SQL Server 2008 R2，
      `OFFSET n ROWS FETCH NEXT m ROWS ONLY` 直接报 102 语法错（要 2012+）。
    ★ 全部单位用 `FOR XML PATH + TYPE).value()` 聚合成 '包|10.00,捆|40.00' 再在
      Python 侧解析 —— `STRING_AGG` 要 2017+，而且用 TYPE).value() 才能拿到
      **未被 XML 转义**的原文（单位名里出现 & 时不会变成 &amp;）。
    ★ show_stopped / usage 见 status_clause：默认不返回停止使用的货品，开单页还按方向再排一层。
      total 与分页走的是同一个 where ⇒ 不会出现"总数 20、翻到第 2 页就空了"。

    ★★★ 【排序：为什么用 UNION ALL 两段式，不用 CASE】（2026-10-12 压测后改）
      旧写法是单条 `ROW_NUMBER() OVER (ORDER BY CASE WHEN ISNULL(StopUse,0)=9
      THEN 0 ELSE 1 END, hpbh)` —— 目的是"把停用货品排到最后"。问题：
      **排序键里含 CASE 表达式，聚集索引 PK_Hpdan(hpbh) 完全用不上**
      ⇒ 退化成 48584 行全表排序。实测 `ORDER BY hpbh` 只需 **1.0ms**，
      而带 CASE 要 **48.7ms**（差 49 倍）。而 `product.search` 是开单页选货的第一屏，
      每敲一个字都要跑。

      改法：把「排序键里的 CASE」拆成「两个 WHERE 谓词 + UNION ALL」——
      非停用段先出、停用段续上，两段各自 `ORDER BY hpbh` 都能走聚集索引。
      这不是近似，是**语义等价的改写**：`ORDER BY (CASE…), hpbh` 的定义就是
      「先按 CASE 值分组、组内按 hpbh」，与 UNION ALL 后按 (grp, hpbh) 排序逐字对应。

      **实测保序 100% 通过**（7 档 offset × 4 组关键字，整页逐行比对 0 差异）：
        offset=0/20/100/500/1000/2000/5000；关键字 LS/螺丝/12345/DHX。
      性能：主查询 48.7ms → **17.1ms**。

      ★ 与 `sales.list_bills` 那次失败的"两段式"区别在这里（别混淆）：
        sales 那次是**近似**（用单键 NDate 序去逼近双键序，两者在跨天补录单上相反），
        这次是**等价改写**（CASE 就是分组条件本身）。
        判据永远是「整页逐行比对」，**不能只比 `TOP N`**。
    """
    from .master import _page_args      # 分页归一化沿用 master 的口径
    limit, offset = _page_args(limit, offset)
    kw = str(keyword or "").strip()
    conds = status_clause(usage, show_stopped)
    args = []
    if kw:
        like = "%" + kw.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]") + "%"
        cols = ("hpbh", "hpmc", "ggxh", "Py", "UnitBarcode")
        conds.append("(" + " OR ".join(c + " LIKE %s" for c in cols) + ")")
        args = [like] * len(cols)
    # 全放开时 conds 会是空的 ⇒ join 出空串，拼成 `FROM Hpdan WHERE ` 直接 102 语法错
    where = " AND ".join(conds) if conds else "1=1"
    total = int(db.q1(cur, "SELECT COUNT(*) FROM Hpdan WHERE " + where, tuple(args))[0])
    # ★ 停用的到底有几条：把状态那几条摘掉、只数 StopUse=9，其余条件（关键字）与分页同源。
    #   没有这个数，界面上"开关像没反应"就无从解释 —— 停用的编号常常排在几万条的最后面，
    #   一页 120 条压根见不到（2026-10-06 用户就是在这儿看不见标签）。
    kw_conds = [c for c in conds if "StopUse" not in c]
    stop_where = "(" + " AND ".join(kw_conds) + ") AND ISNULL(StopUse,0) = %d" % STOP_DISABLED if kw_conds \
        else "ISNULL(StopUse,0) = %d" % STOP_DISABLED
    stopped_count = int(db.q1(cur, "SELECT COUNT(*) FROM Hpdan WHERE " + stop_where, tuple(args))[0])
    unit_cell = (
        "STUFF((SELECT ',' + LTRIM(RTRIM(u.UnitName)) + '|' + CONVERT(varchar(20), u.UnitRate)"
        " FROM HpdanUnit u WHERE u.HpIdenGuid = t.IDENGUID AND ISNULL(u.UnitStop,0)=0"
        " AND ISNULL(u.UnitName,'') <> ''"
        " ORDER BY u.UnitBasic DESC, u.UnitRate"
        " FOR XML PATH(''), TYPE).value('.','nvarchar(max)'),1,1,'')")
    # ★ offset/limit 是 _page_args 归一化后的 int，直接拼进 SQL（不是用户可写的字符串）
    #
    # ★★★ UNION ALL 两段式（见 docstring 的论证）：把 CASE 从排序键拆成两组谓词，
    #   两段各自 ORDER BY hpbh 都能走聚集索引 PK_Hpdan。
    #   外层再按 (grp, hpbh) 编号切页 —— grp 0=非停用、1=停用，等价于原来的 CASE 排序。
    sql = ("""SELECT LTRIM(RTRIM(t.hpbh)), t.hpmc, ISNULL(t.ggxh,''), ISNULL(t.Py,''), ISNULL(ht.Text,''),
                     ISNULL(t.HpUnit,''), %s,
                     ISNULL((SELECT SUM(S_KCSL) FROM Inventory i WHERE i.Hpbh = t.hpbh),0),
                     t.D_SJ1, t.D_JH, t.Cost_Price, ISNULL(t.kcxx,0),
                     CONVERT(int, ISNULL(t.StopUse,0)),
                     ISNULL(t.UnitPack,''), ISNULL(t.UnitGgxh,''),
                     CASE WHEN EXISTS(SELECT 1 FROM Inventory i WHERE i.Hpbh = t.hpbh)
                          THEN 1 ELSE 0 END
              FROM (
                  SELECT hpbh, hpmc, ggxh, Py, LbType_Hp, HpUnit, D_SJ1, D_JH, Cost_Price,
                         kcxx, StopUse, UnitPack, UnitGgxh, IDENGUID
                  FROM (
                      SELECT hpbh, hpmc, ggxh, Py, LbType_Hp, HpUnit, D_SJ1, D_JH, Cost_Price,
                             kcxx, StopUse, UnitPack, UnitGgxh, IDENGUID,
                             ROW_NUMBER() OVER (ORDER BY grp, hpbh) AS rn
                      FROM (
                          SELECT 0 AS grp, hpbh, hpmc, ggxh, Py, LbType_Hp, HpUnit,
                                 D_SJ1, D_JH, Cost_Price, kcxx, StopUse, UnitPack, UnitGgxh, IDENGUID
                          FROM Hpdan WHERE %s AND ISNULL(StopUse,0) <> %d
                          UNION ALL
                          SELECT 1 AS grp, hpbh, hpmc, ggxh, Py, LbType_Hp, HpUnit,
                                 D_SJ1, D_JH, Cost_Price, kcxx, StopUse, UnitPack, UnitGgxh, IDENGUID
                          FROM Hpdan WHERE %s AND ISNULL(StopUse,0) = %d
                      ) u
                  ) v
                  WHERE v.rn > %d AND v.rn <= %d
              ) t
              LEFT JOIN HpType ht ON ht.Key_ = t.LbType_Hp
              ORDER BY t.hpbh""" % (
        unit_cell, where, STOP_DISABLED, where, STOP_DISABLED, offset, offset + limit))
    # ★ 参数顺序：两段 WHERE 各自一套（同为 tuple(args)）—— 顺序必须与 SQL 里
    #   两个 `WHERE %s` 的出现次序严格一致，写反会把同一条关键字喂错段。
    rows = db.qall(cur, sql, tuple(args) + tuple(args))
    items = []
    for r in rows:
        items.append({
            "hpbh": r[0], "code": r[0], "name": r[1], "spec": r[2], "py": r[3],
            "category": r[4], "unit": r[5], "units": _parse_units(r[6]),
            "stock": _num(r[7]), "sale_price": _num(r[8]),
            "purchase_price": _num(r[9]), "cost_price": _num(r[10]),
            # ★ 下限只回数据，**前端暂时不许拿它当预警口径**：kcsx/kcxx 的语义
            #   至今没有对证（见库存页那批记录），等业务确认再显示提示。
            "stock_min": _num(r[11]), "stop_use": int(r[12] or 0),
            # ★ 停用只算 9；5/6 是方向上的停，跟着 status_name 各自说话，别混成「已停用」
            "stop": int(r[12] or 0) == STOP_DISABLED,
            "status_name": stop_name(r[12]),
            # ★ 铁律 2：详情有的列表也得有 —— 这两列不给的话，列表页上"包装"永远空着
            "pack_unit": r[13] or "", "pack_spec": r[14] or "",
            # ★ 开单页的库存提示就靠这个区分"从没入过库"与"账面 0"（详情同名同类型）
            "stock_record": int(r[15] or 0),
        })
    # ★ 分页信封的键必须与 master._paged 完全一致（是 **hasMore** 不是 has_more）：
    #   前端无限滚动读的就是这个键，写成 snake_case 会静默变 undefined ⇒ 翻不了页。
    return {"items": items, "total": total, "limit": limit, "offset": offset,
            "hasMore": offset + len(items) < total,
            # ★ 停用的条数（与关键字同源，不受 show_stopped 影响）+ 这次有没有把它们放出来
            "stoppedCount": stopped_count, "showStopped": bool(show_stopped)}


def _resolve_category(cur, value):
    """类别允许传编号（HpType.Key_）或名称（HpType.Text），统一落到 Key_。

    ★ 不许"传什么写什么"：旧代码把 category 原样写进 LbType_Hp，
      写错一个字的类别**不会报错**，只会让这货品在按类别筛选里永远消失。
    """
    key = str(value if value is not None else "").strip()
    if not key:
        return ""
    hit = db.q1(cur, "SELECT LTRIM(RTRIM(Key_)) FROM HpType WHERE LTRIM(RTRIM(Key_))=%s", (key,))
    if hit:
        return hit[0]
    hit = db.q1(cur, "SELECT LTRIM(RTRIM(Key_)) FROM HpType WHERE LTRIM(RTRIM(Text))=%s", (key,))
    if hit:
        return hit[0]
    names = [r[0] for r in db.qall(cur, "SELECT Text FROM HpType ORDER BY Key_")]
    raise ValueError("类别「%s」不存在。现有类别：%s" % (key, "、".join(names) or "（还没有类别）"))


# ---------------- 货品自动编号（V3 的"货品编号规则"） ----------------
#
# ★ 规则来源与取证（2026-09-27，只读脚本 scripts/probe-code-prefix-stats.py）
#   V3 的下拉有 5 档：0=默认最大编号+1 / 1~3=类别 N 级+流水 / 6=编号最近+1，
#   存在 HxAccountOption 的 HpdanHpbhSerialType + HpdanHpbhSerialLen（补零位数）。
#   生成动作在 Delphi 客户端里做：**库里 0 个过程/触发器提到这两个开关**
#   （probe-code-rule-source.py [A] 查 sys.sql_modules 证实），所以只能按数据反推。
#
#   前缀 = 类别 Key_ 按 '-' 切段、取前 N 段、直接拼接（**不补零**）。实测命中率：
#     '201-' → '201' 279/286    '301-' → '301' 998/1098   '1-01-' → '101' 72/74
#     '1-00-' → '100' 147/151   '6-'   → '6'   32330/32367
#   而"每级补两位零"的切法（'0101'、'0100'、'06'）命中 0~3 条 ⇒ 排除。
#   ⇒ 本账套 Type=2 就是"取前 2 段"，而类别 Key_ 本身最多 2 段，所以 2 级 = 整条 Key_ 去掉 '-'。
#
# ★★ 别照 next_customer_code 抄 MAX+1：那是不分类别的整表最大号，
#   本库纯数字最大号已经到 85,485,455,515,415（14 位），照抄会给每个新客户货品发一个
#   比现有号段长一倍的号；按类别前缀出号才落在客户看得懂的号段里。

RULE_OPTIONS = ("HpdanHpbhSerialType", "HpdanHpbhSerialLen", "HxItemEditAutobh")
MAX_SERIAL_DIGITS = 18        # CAST(... AS BIGINT) 的位宽上限，超过就不当流水看


def code_rule(cur):
    """**每次现读** V3 的货品编号规则（不缓存、不写死）。

    技术员在 V3 客户端里改了下拉，下一个请求出的号就得跟着变 ⇒ 谁都不许把
    serial_type/serial_len 抄成常量或存到 Agent 配置里。

    返回：
      serial_type  0=默认最大编号+1 / 1~3=类别 N 级+流水 / 6=编号最近+1（原值，界面要显示）
      levels       取类别前几段；非类别档（0/6/没设/没认过的档）= 0 ⇒ 不出前缀，走整表最大+1
      serial_len   流水补零位数，0 = 不补
      autobh       V3 自己的档案编辑界面是否自动填号（只作为信息回带给界面，
                   不拿它挡我们的自动带出：那开关管的是 V3 桌面端，H5 上手不手填是我们的决定）
      ambiguous    这个开关在库里同名多行且取值不一致 ⇒ 只能按默认档出号，把名字露出来
                   （HxAccountOption 主键是 (OptionSpid, OptionName, ZDRBH)，同名可以有多行）
    """
    got = db.account_option_values(cur, RULE_OPTIONS)

    def _i(name):
        v = (got.get(name) or {}).get("value")
        try:
            return int(float(v)) if v else 0
        except (TypeError, ValueError):
            return 0

    stype = _i("HpdanHpbhSerialType")
    return {"serial_type": stype,
            "levels": stype if stype in (1, 2, 3) else 0,
            "serial_len": max(0, _i("HpdanHpbhSerialLen")),
            "autobh": _i("HxItemEditAutobh") != 0,
            "ambiguous": sorted(n for n in RULE_OPTIONS if (got.get(n) or {}).get("conflict"))}


def category_code_prefix(key, levels):
    """类别 Key_ -> 编号前缀：'1-01-' 取 2 级 = '101'；段数不够就取全部（不补零）。"""
    segs = [s.strip() for s in str(key or "").split("-") if s.strip()]
    return "".join(segs if levels <= 0 else segs[:levels])


def _like_prefix(prefix):
    """前缀转 LIKE 模式：类别 Key_ 里出现 % _ [ 时不能当通配符用。"""
    out = prefix.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
    return out + "%"


def _max_serial(cur, prefix):
    """该前缀下已经用掉的最大流水。整串都是数字才算（'A12-' 这种自编号不参与）。"""
    plen = len(prefix)
    if plen >= MAX_SERIAL_DIGITS:
        return 0
    r = db.q1(
        cur,
        """SELECT MAX(CAST(SUBSTRING(x, %s, %s) AS BIGINT))
           FROM (SELECT LTRIM(RTRIM(hpbh)) AS x FROM Hpdan) t
           WHERE x LIKE %s AND LEN(x) > %s AND LEN(x) <= %s
             AND SUBSTRING(x, %s, %s) NOT LIKE %s""",
        (plen + 1, MAX_SERIAL_DIGITS, _like_prefix(prefix), plen,
         plen + MAX_SERIAL_DIGITS, plen + 1, MAX_SERIAL_DIGITS + 1, "%[^0-9]%"))
    return int(r[0]) if r and r[0] is not None else 0


def code_plan(cur, category_key="", rule=None):
    """出号计划的完整信息 {code, prefix, serial_type, serial_len}（界面要显示"按哪个号段出的"）。

    ★ `category_key` 可以传编号也可以传名称（与保存同源，走 `_resolve_category`），
      传错类别会在这里就报错，而不是等保存时才弹。
    """
    rule = rule or code_rule(cur)
    prefix = ""
    if rule["levels"]:
        key = _resolve_category(cur, category_key) if category_key else ""
        prefix = category_code_prefix(key, rule["levels"])
        if not prefix:
            # ★ 这台机器设成了"按类别出号"却没选类别：**不要**顺手退回整表最大+1。
            #   实测那种号会长成 14 位（本库纯数字最大号已 85,485,455,515,415），
            #   客户看到的是"随便一点就给我一个看不懂的编号"。
            #   报错让人先选类别，或干脆自己填 —— 界面按这个话术先挡住输入框。
            raise ValueError("这台机器的货品编号规则是按类别出号，请先选货品类别（或手工填编号）")
    n = _max_serial(cur, prefix) + 1
    code = ""
    for _ in range(500):
        code = prefix + str(n).zfill(rule["serial_len"])
        if not find_row(cur, code):
            break
        n += 1
    else:
        raise ValueError("号段 %s 已经排到 %s 还找不到空号，请手工填编号"
                         % (prefix or "（整表）", code))
    return {"code": code, "prefix": prefix, "levels": rule["levels"],
            "serial_type": rule["serial_type"], "serial_len": rule["serial_len"],
            "autobh": rule["autobh"]}


def preview_code(cur, category_key="", rule=None):
    """给"自动带出"用的预览：规则按类别出号、这次还没选类别 ⇒ **不报错**，回 need_category。

    ★ 为什么与 next_product_code 分两个入口：保存时没类别必须拦（不然写进一个空编号），
      而新增页一打开就要先拿到规则好写提示 —— 拿报错当查询用，前端只能把错误原文甩给用户。
    """
    rule = rule or code_rule(cur)
    if rule["levels"] and not str(category_key or "").strip():
        return dict(rule, code="", prefix="", need_category=True)
    return code_plan(cur, category_key, rule)


def next_product_code(cur, category_key="", rule=None):
    """按这台机器 V3 自己的编号规则预测下一个货品编号。

    流水取"该前缀下已用最大 +1"，**不看位宽**：同一类别里既有 101xxx（3 位流水）
    又有 1010xxx（4 位）时，取整段最大值往后接，才不会插进已用号段中间。
    补零位数用 SerialLen；SerialLen=0（没设）就不补。
    """
    return code_plan(cur, category_key, rule)["code"]


def _same(a, b):
    """比较"这列到底变没变"。数值按 Decimal 比（18.50 与 18.5 是同一个值）。"""
    if a is None:
        a = ""
    if b is None:
        b = ""
    try:
        return Decimal(str(a).strip() or 0) == Decimal(str(b).strip() or 0)
    except Exception:  # noqa: BLE001
        return str(a).strip() == str(b).strip()


def _v3_error(exc, doing):
    """把 V3 触发器抛的 50000 翻译成人话。

    ★ pymssql 会把中文错误塞成 bytes 字面量（`b'\\xe8\\xb4\\xa7...'`）混在 args 里，
      直接 str(exc) 甩给前端就是这次验收看到的 `code=INTERNAL + 一串字节` ——
      客户看到的是天书，还会以为是我们把数据库搞坏了。
    """
    raw = ""
    for a in getattr(exc, "args", ()) or ():
        if isinstance(a, bytes):
            try:
                raw += a.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                raw += repr(a)
        elif isinstance(a, str) and "DB-Lib" not in a:
            raw += a
    raw = re.sub(r"\s*DB-Lib error.*$", "", raw, flags=re.S).strip()
    if not raw:
        raw = str(exc)[:200]
    raise ValueError("%s，V3 拦下了这次修改：%s" % (doing, raw))


PRICE_LABEL = dict((c, l) for _k, c, l in PRICE_ATTRS)


def _merge_basic_prices(units, prices):
    """把「基本单位那一行填的价」并进主表价里。

    ★★ 为什么必须并而不是覆盖：`TRI_A_HpdanUnit` 要求基本单位行的价与主表**逐列相等**，
      所以界面上那两处填的其实是同一个数（默认销价 / 参考进价）。
      旧写法是"基本单位行一律取主表价"⇒ 用户在单位行里改的那个价**被静默丢掉**，
      表现成"改了基本单位单价不生效"（2026-09-27 用户报的就是这条）。
      铁律 1 说的正是这个：不认识的、要丢的值必须报错，不许吞。

    规则：单位行给了、主表没给 ⇒ 用单位行的（这就是用户刚改的那个）；
          两边都给了且不等 ⇒ 报错让人对齐（**不猜哪边是对的**）。
    """
    if not units:
        return prices
    basic = [u for u in units if u.get("basic")]
    if not basic:
        return prices
    for col, val in (basic[0].get("prices") or {}).items():
        if col in prices and not _same(prices[col], val):
            raise ValueError("基本单位的价格与上面价格栏填了两个不同的数（%s：%s 与 %s），"
                             "请把两处改成同一个再保存"
                             % (PRICE_LABEL.get(col, col), prices[col], val))
        prices[col] = val
    return prices


def save_product(cur, data, commit=True):
    """保存货品档案（有编号即编辑，无编号即新增）。

    data 的可用键见本文件上方的 TEXT_ATTRS / NUM_ATTRS / BIT_ATTRS / PRICE_ATTRS
    与 units。**不认识的键会抛错**（见 `_assert_known_keys`），不再静默丢弃。
    """
    d = dict(data or {})
    _flatten_prices(d)
    _assert_known_keys(d)
    # ★ 云端 POST（新增）会带上这个标记：新增**永远不许改成别人的档案**。
    #   自动带号上线后这条更要紧 —— 预测与实际保存之间那一刻号被别人占了的话，
    #   "有编号即编辑"的旧语义会静默把人家那条货品改了名。
    create_only = bool(d.pop("create_only", False))

    # ★ 名称/助记码：**新增必须有名称；修改时没带这一项 = "这次不动它"**。
    #   编辑页发的是"用户真改过的字段"（V3 的 TRI_A_Hpdan 按列保护逼出来的），
    #   所以一次"只停用一个单位"的修改里根本没有 name ——
    #   2026-09-27 线上就报在这里：改货品提示「商品名称不能为空」。
    #   显式发了空名称照样挡（那是真要把名字抹掉）。
    name_given = "name" in d
    name = _txt(d.get("name"), "商品名称", 400)
    code = _txt(d.get("code") or d.get("hpbh"), "商品编号", 60)
    py = _txt(d.get("py"), "助记码", 60) or (name[:10] if name else "")
    units = _norm_units(d)
    attrs = _attr_values(d)
    prices = _merge_basic_prices(units, _price_values(d))
    ignored = sorted(set(d) & (READONLY_KEYS - {"hpbh"}))

    # 基本单位名：units 里标了 basic 的那条说了算，没带 units 才退回单值 unit
    base_unit = ""
    if units:
        base_unit = [u for u in units if u["basic"]][0]["unit"]
    elif "unit" in d:
        base_unit = _txt(d.get("unit"), "基本单位", 20)

    if "category" in d:
        attrs["LbType_Hp"] = _resolve_category(cur, d.get("category"))
    if "stop" in d:
        attrs["StopUse"] = STOP_DISABLED if d.get("stop") in (1, "1", True, "y", "true") else STOP_NORMAL

    row = find_row(cur, code) if code else None
    if row and create_only:
        raise ValueError("编号 %s 上已经有货品了：新增不能去改别人的档案，请换号或编辑那条" % code)
    if row:
        # 基本单位行的价格要跟主表逐列相等（TRI_A_HpdanUnit 硬约束）⇒ 先算出"主表最终价"
        main_prices = dict((c, _dec(row.get(c), c)) for _k, c, _l in PRICE_ATTRS)
        main_prices.update(prices)
        guid0 = guid_of(cur, code)
        if units and guid0:
            _guard_units(cur, guid0, units, code)
        # ★★ **只写真正变了的列**。V3 的 TRI_A_Hpdan 是按列保护的：一旦这个货品有业务数据，
        #   改动 hpbh/ggxh/UnitBarcode/LbType_Hp/GysMaster/HpUnit/IsSil/StopUse/
        #   D_JH/Cost_Price/D_SJ0~D_SJ3/D_SJ5/... 任一组列都会去问 HxCheckUseState_HP1，
        #   非空就 raiserror（实测：改倍率、甚至"只停用一个单位"都被整条 UPDATE 带崩）。
        #   旧写法是"把界面字段全量覆写回去"⇒ 值没变也算 Update(列)，
        #   于是**合法操作（停用单位）也跟着失败**。所以这里逐列 diff，没变就不进 SET。
        want_cols = {}
        if name_given:
            if not name:
                raise ValueError("商品名称不能为空")
            want_cols["hpmc"] = name
        if "py" in d:
            want_cols["Py"] = py
        if base_unit:
            want_cols["HpUnit"] = base_unit
        if units and not bill_usage(cur, code):
            # ★ 包装单位/包装规格：**开过单的货品这两列一律不动**。
            #   `Update(UnitPack)` 在 TRI_A_Hpdan 的 30 个按列保护里，而且 V3 一看到
            #   ggxh/UnitPack 被更新就去跑 `HxTri_Hpdan_ggxh` —— 那是个游标，把所有
            #   "同时有 ggxh+UnitPack 两列"的表按 hpbh 刷一遍（等于改写历史单据行）。
            #   没开过单的货品跟着单位变化重算，开过单的保持 V3 里录的那个值。
            attrs.update(_auto_pack(d, units))
        want_cols.update(attrs)
        want_cols.update(prices)
        if "cost_price" in d:
            want_cols["Cost_Price"] = _dec(d.get("cost_price"), "成本价")
        changed = dict((c, v) for c, v in want_cols.items() if not _same(row.get(c), v))
        if changed:
            sets = ["%s=%%s" % c for c in changed] + ["UserUpdate='AGENT'", "TimeUpdate=GETDATE()"]
            args = list(changed.values()) + [code]
            try:
                cur.execute("UPDATE Hpdan SET %s WHERE LTRIM(RTRIM(hpbh))=%%s"
                            % ", ".join(sets), tuple(args))
            except Exception as exc:  # noqa: BLE001
                _v3_error(exc, "货品「%s」的档案改不动" % name)
        guid = guid_of(cur, code)
        try:
            if units and guid:
                _write_units(cur, guid, units, code, main_prices)
            elif base_unit and guid:
                _legacy_sync_unit(cur, guid, base_unit, prices)
            elif guid and str(row["HpUnit"] or "").strip() and any(c in PRICE_COLS for c in changed):
                # ★ 主表价改了、但这次**没带 units**：编辑页只发"用户真改过的列"，改主表价
                #   不会让单位行的 sig 变 ⇒ units 不上来。而 V3 要求基本单位行的价与主表
                #   逐列相等（`TRI_A_HpdanUnit`），不补这一脚就是账上两个数
                #   （2026-09-27 端到端实测：主表 25、基本单位行还留着 21.5）。
                _legacy_sync_unit(cur, guid, str(row["HpUnit"] or "").strip(),
                                  dict((c, v) for c, v in changed.items() if c in PRICE_COLS))
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001
            _v3_error(exc, "单位改不动")
        if commit:
            cur.connection.commit()
        return {"code": code, "created": False, "changed": sorted(changed) if row else [],
                "ignored": ignored}

    auto_code = not code
    if not name:
        raise ValueError("新增商品要填名称")
    if auto_code:
        # ★ 没给编号 ⇒ 按这台机器 V3 自己的规则出号（规则现读，见 code_rule）。
        code = next_product_code(cur, attrs.get("LbType_Hp") or "")
    if find_row(cur, code):
        # ★★ 走到这里 code 存在 = **自动带出之后、INSERT 之前被人抢先占了同一个号**。
        #   这时绝不能"那就更新它" —— 那会把别人刚建的货品改名重写掉。报错让人再存一次。
        #   （手工填已有编号是另一回事：那在上面走的是编辑分支，本行拦不到也不该拦。）
        raise ValueError("自动带出的编号 %s 刚被占用，请重新保存再取一次号" % code)
    if not base_unit:
        raise ValueError("必须填一个基本单位（库存与成本按它记账）")
    # 新建就带上包装单位/包装规格（V3 对多单位货品自己会填这两列，判据见 pack_values）
    attrs.update(_auto_pack(d, units))

    guid = str(uuid.uuid4())
    cols = ["hpbh", "hpmc", "Py", "BarcodeType", "HpUnit", "StopUse",
            "UserInsert", "UserUpdate", "TimeInsert", "TimeUpdate", "IDENGUID"]
    vals = ["%s", "%s", "%s", "9", "%s", "%s", "'AGENT'", "'AGENT'", "GETDATE()", "GETDATE()", "%s"]
    args = [code, name, py, base_unit, int(attrs.pop("StopUse", STOP_NORMAL)), guid]
    for col, val in list(attrs.items()) + list(prices.items()):
        cols.append(col)
        vals.append("%s")
        args.append(val)
    if "cost_price" in d:
        cols.append("Cost_Price")
        vals.append("%s")
        args.append(_dec(d.get("cost_price"), "成本价"))
    try:
        cur.execute("INSERT INTO Hpdan (%s) VALUES (%s)" % (", ".join(cols), ", ".join(vals)),
                    tuple(args))
    except Exception as exc:  # noqa: BLE001
        _v3_error(exc, "新建货品失败")
    # 新建时主表价 = 本次传入价（没传的列为 0），基本单位行必须与之一字不差
    create_prices = dict((c, Decimal("0")) for _k, c, _l in PRICE_ATTRS)
    create_prices.update(prices)
    try:
        if units:
            _write_units(cur, guid, units, code, create_prices)
        else:
            _write_units(cur, guid, [{
                "unit": base_unit, "rate": Decimal("1"), "basic": True, "stop": False,
                "barcode": attrs.get("UnitBarcode", ""), "has_barcode": True, "prices": create_prices,
            }], code, create_prices)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        _v3_error(exc, "单位写不进去")
    if commit:
        cur.connection.commit()
    return {"code": code, "created": True, "auto_code": auto_code, "ignored": ignored}


def _legacy_sync_unit(cur, guid, base_unit, prices):
    """不带 units 的旧调用：只把基本单位那一行的名字/价格对齐，不动别的单位。"""
    sets, args = ["TimeUpdate=GETDATE()"], []
    for col, val in prices.items():
        sets.append("%s=%%s" % col)
        args.append(val)
    args += [str(guid), base_unit]
    cur.execute("UPDATE HpdanUnit SET %s WHERE HpIdenGuid=CONVERT(uniqueidentifier,%%s) "
                "AND LTRIM(RTRIM(UnitName))=%%s" % ", ".join(sets), tuple(args))
