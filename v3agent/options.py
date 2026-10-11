# -*- coding: utf-8 -*-
"""本店 V3 的业务开关 → capabilities（决定界面**露不露**某个功能）。

★ 为什么必须回给前端：同一套 H5/小程序要服务多家账套，而 V3 的
  「多单位 / 包装单位 / 数量按单位 / 货品品牌 / 编号规则」都是**每家在自己客户端里开**的
  （HxAccountOption，按账套各一份）。写死"H5 永远显示多单位"= 给没开的那家
  露一个它根本存不进去的入口 —— 换一家就现原形。

★★ 三态，且**只有明确 False 才隐藏**：
     True  = 开了
     False = 明确没开（值为 '0' 或空）
     None  = 不知道：库里没这一行 / 同名多行且值冲突 / 值看不懂 / 这张表读不出来
   把"不知道"当成"没开"，后果是一次查询抖动就让客户整片功能消失，比多露一个入口严重得多。
   （与 toCapabilities 里 `dbStatusKnown` 的处理是同一套纪律。）
"""

from . import product
from . import sql as db

# 界面会消费的开关：键 = 回给前端的驼峰名，值 = HxAccountOption 里的开关名
SWITCHES = {
    "multiUnit": "MultiUnit",                  # 启用多单位（HpdanUnit 每单位一行）
    "multiUnitPackage": "MultiUnitPackage",    # 启用包装单位
    "billQtyByUnit": "HxBillQtyByUnit",        # 开单数量按单位（明细落 S_SLUnit/S_SL）
    "useBrandName": "UseHpBrandName",          # 启用货品品牌
    # ★ 这两个是「能力开关」，界面判据与上面几条**相反**：只有明确 true 才显示入口。
    #   批次/序列号一旦开了就要贯穿到单据与库存（销售单还得选批次），
    #   没开的店先把框露出来 = 让人勾一个后面全链路都不生效的假开关。
    "useBatch": "UseScpc",                     # V3 的批次管理（UseScpc2/3 是分版本，先认主开关）
    "useSerial": "UseSerial",                  # V3 的序列号管理
}
_TRUTHY = {"1", "true", "y", "-1"}
_FALSEY = {"0", "", "false", "n"}


def _three_state(v):
    """'1'/'True' → True；'0'/空/'False' → False；看不懂 → None（=不知道，别隐藏）。"""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in _TRUTHY:
        return True
    if s in _FALSEY:
        return False
    return None        # '2'、'on' 这类：这家 V3 到底怎么解释没有依据，别替它决定


def account_options(cur):
    """capabilities.accountOptions：开关三态 + 货品编号规则 + 读不明白的东西。

    读不到（老库没这张表 / 权限不够）时**整块返回 None 值而不是抛错** ——
    capabilities 挂了会连带把「Agent 在线」也显示不出来，那是更大的事故。
    """
    names = tuple(SWITCHES.values())
    try:
        got = db.account_option_values(cur, names)
    except Exception:  # noqa: BLE001
        got = {}
    out = dict((k, _three_state((got.get(v) or {}).get("value")))
               for k, v in SWITCHES.items())
    out["ambiguous"] = sorted(str(v) for v in SWITCHES.values()
                              if (got.get(v) or {}).get("conflict"))
    try:
        rule = product.code_rule(cur)
    except Exception:  # noqa: BLE001
        rule = {"serial_type": None, "levels": None, "serial_len": None,
                "autobh": None, "ambiguous": []}
    out["productCode"] = rule
    out["ambiguous"] = out["ambiguous"] + [x for x in (rule.get("ambiguous") or [])
                                           if x not in out["ambiguous"]]
    return out
