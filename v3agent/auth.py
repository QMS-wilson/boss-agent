# -*- coding: utf-8 -*-
"""用 V3 自己的操作员表（`UserInfo`）登录：口令比对只发生在客户本地。

★★ 为什么放在 Agent 而不是云端
  `UserInfo` 在**账套库**里（`AccMaster` 没有这张表），而账套库只在客户那台机器上。
  云端要校验就得把口令（甚至哈希）搬上网 —— 那是给自己造一个新泄露面。
  所以：App 把口令发给云端，云端**原样转给该商户的 Agent**，Agent 本地比对，
  只回"过 / 不过 + 是谁 + 权限摘要"。哈希与明文都不回。

★★ 三条纪律（都是这一页数据逼出来的，别改回去）
  1. **空口令不放行**。库里"没设口令"的操作员存的正是 `MD5("")`
     （因为 V3 的存法是 MD5(Base64(明文))，而 Base64("") 是空串）—— 等于"知道编号就能进"，
     而这些账号还带着看全部库存/全部销售单的权限。所以空口令必须挡，
     并且话术要告诉他"去 V3 里给这个操作员设口令"，不能只说"口令不对"。
     ★ 2026-09-29 实测本账套 8 个操作员里 5 个还是这个值。
  2. **口令错与账号不存在回同一句话**（`BAD_CREDENTIAL`）。编号是 `101/102/103` 这种
     可枚举的东西，"这个操作员不存在"和"口令不对"分开说就是给人送枚举器。
     （`NO_PASSWORD` 仍然会暴露"这个编号存在"—— 这是可用性的让步，
     兜底靠云端限流，见"还没做"。）
  3. **0 不等于"关着"**。`J_FkLos`/`RgDiscountMin` 在 V3 里 0 是"没配"，
     权限摘要一律给 `None`（=不知道），只有明确配过才给值。
     ★ 第一版**不做任何权限拦截**（用户 2026-09-28 拍板：权限暂不处理，落入计划），
       这里只是把字段与三态形状先立起来，免得以后接的时候改契约。

★ 登录名：先按 `UserID` 精确匹配，匹配不到再按 `UserName`（老板记不住 101，
  记得住"业务"）。按名称命中多个时**报错让人改用编号**，不猜是哪一个。
"""

import base64
import hashlib

# V3 里"没设口令"的操作员全都是这个值 —— 注意它不是巧合：
# V3 的存法是 MD5(Base64(明文))，而 Base64("") 就是空串，所以没设口令 = MD5("")。
EMPTY_MD5 = "d41d8cd98f00b204e9800998ecf8427e"

# ★★ 口令算法（2026-09-29 逆向确定，不是猜的）：
#    `UserInfo.UserPassword` = **十六进制小写的 MD5( Base64(明文) )**，库里存成大写。
#    证据：客户端二进制（WiseAdv/Pos/WiseSrv/WiseMangr）里的 MD5 常量表与 IV 都是**标准值**
#    （我一开始以为是"改过的 MD5"，那是我手抄表抄错了一项），而七组真值全部吻合：
#      ''→D41D8CD9…、123→6FD742A6…、111→8D76BB1F…、222→4134EF71…、333→964641E0…、
#      888→24781BEF…、123456→87D9BB40…（六对由用户在 V3 界面当场设进去，只读回采）
#    ⇒ Base64 在这里**不是加密**，只是编码；拿到库的人对弱口令做字典攻击比普通 MD5 还直接。
_COLS = ("UserID", "UserName", "UserPassword", "IsStop")


def _hashes(text):
    """明文 → V3 可能存下的那几个摘要。

    ★ 为什么要给两个：Delphi 客户端把口令当 **ANSI(简体中文=GBK)** 字节读，
      所以真值是 MD5(Base64(gbk))；而我们从手机端收到的是 UTF-8 字符串。
      纯 ASCII 口令两种编码完全一样，一旦客户用中文当口令就会分叉 ——
      只认一种等于把设了中文口令的人永久锁在门外，那就是一通查不出来的电话。
      两种都比一遍：都需要知道正确口令，没有放宽任何安全性。
    """
    out = []
    for enc in ("gbk", "utf-8"):
        try:
            raw = text.encode(enc)
        except (UnicodeEncodeError, LookupError):
            continue
        out.append(hashlib.md5(base64.b64encode(raw)).hexdigest())
    return out


def _norm(v):
    return str(v if v is not None else "").strip()


def find_user(cur, login):
    """按编号取，取不到再按名称取。返回 (row, ambiguous_names)。

    row = {"id","name","password","stopped"} 或 None；
    ambiguous_names = 按名称命中多个人时的姓名清单（让调用方说清"请用编号"）。
    """
    login = _norm(login)
    if not login:
        return None, []
    row = _fetch(cur, "UserID", login)
    if row:
        return row, []
    rows = _fetch_all(cur, "UserName", login)
    if len(rows) > 1:
        return None, [r["name"] for r in rows]
    return (rows[0] if rows else None), []


def _fetch(cur, col, value):
    rows = _fetch_all(cur, col, value)
    return rows[0] if rows else None


def _fetch_all(cur, col, value):
    # ★ 列名只可能是下面两个白名单值（我们拼进 SQL 的东西必须是自己给的常量）
    if col not in ("UserID", "UserName"):
        raise ValueError("内部错误：不允许的查询列 " + str(col))
    cur.execute(
        """SELECT LTRIM(RTRIM(%s)) AS uid, LTRIM(RTRIM(ISNULL(UserName,''))) AS uname,
                  LTRIM(RTRIM(ISNULL(UserPassword,''))) AS upwd,
                  CONVERT(int, ISNULL(IsStop,0)) AS stopped
           FROM UserInfo WHERE LTRIM(RTRIM(%s)) = %%s""" % (col, col), (value,))
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        out.append({"id": _norm(d["uid"]), "name": _norm(d["uname"]),
                    "password": _norm(d["upwd"]), "stopped": bool(d["stopped"])})
    return out


def verify(row, password):
    """纯比对（不连库，方便离线验）。返回 {"ok","code","message","user","perms"}。"""
    if row is None:
        return _result(False, "BAD_CREDENTIAL", "操作员编号或口令不对，请核对后重试")
    if row["stopped"]:
        return _result(False, "USER_STOPPED",
                       "操作员「%s」在 V3 里已被停用，不能登录手机端" % (row["name"] or row["id"]))
    stored = row["password"].lower()
    if not stored or stored == EMPTY_MD5:
        return _result(False, "NO_PASSWORD",
                       "操作员「%s」在 V3 里还没设口令，手机端不允许空口令登录；"
                       "请先在 V3 的「操作员」里给它设一个" % (row["name"] or row["id"]))
    if stored not in _hashes(password):
        # ★ 不回"口令错"以外的区分，也不回哈希
        return _result(False, "BAD_CREDENTIAL", "操作员编号或口令不对，请核对后重试")
    return _result(True, "OK", "", user={"id": row["id"], "name": row["name"]},
                   perms=_perms(row))


def _perms(row):
    """第一版全是 None（=V3 里没配/我们还没读）。★ 只有 False 才允许前端隐藏功能。"""
    return {"maxDiscountLoss": None, "minDiscount": None, "canAudit": None,
            "billScope": None, "stockScope": None, "customerScope": None}


def _result(ok, code, message, user=None, perms=None):
    return {"ok": ok, "code": code, "message": message,
            "user": user or None, "perms": perms or _perms({})}


def check(cur, login, password):
    """Agent op `auth.v3check` 的入口：查人 + 比对，一次给全。

    ★ 口令**只进不出**：返回值里没有任何口令/哈希字段（门守着这条）。
    """
    row, ambiguous = find_user(cur, login)
    if ambiguous:
        return _result(False, "AMBIGUOUS_NAME",
                       "V3 里有好几个操作员都叫「%s」，请改用操作员编号登录" % "、".join(ambiguous))
    return verify(row, password)


#: 清单一次最多回多少个：V3 里常见一家十几个，给到 200 足够；
#: 设上限不是为了省流量，是防有人拿一本被人塞了几万行的库把这条免登录通道撑爆。
LIST_MAX = 200


def list_users(cur, limit=LIST_MAX):
    """Agent op `auth.v3users`：列出这本账的操作员，给登录页那个下拉用。

    ★ 只回 `id / name / stopped` 三个字段 —— SELECT 里连 `UserPassword` 都不写，
      不是"查出来再删掉"。少写一列就少一个漏出去的机会，这条链是**没有令牌也能打到**的。
    ★ `stopped` 要给出去：停用的操作员在登录时必被拒（USER_STOPPED），
      与其让人在下拉里挑一个注定失败的，不如当场标出来不能选。
    """
    n = max(1, min(int(limit or LIST_MAX), LIST_MAX))
    cur.execute(
        "SELECT TOP (%s) LTRIM(RTRIM(UserID)) AS uid, "
        "LTRIM(RTRIM(ISNULL(UserName,''))) AS uname, "
        "CONVERT(int, ISNULL(IsStop,0)) AS stopped "
        "FROM UserInfo ORDER BY UserID", (n,))
    cols = [d[0] for d in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        out.append({"id": _norm(d["uid"]), "name": _norm(d["uname"]),
                    "stopped": bool(d["stopped"])})
    return {"users": out, "truncated": len(out) >= n}

