# -*- coding: utf-8 -*-
"""现场自检：把"这台机器到底行不行"变成一次点击和一个文件。

为什么必须有它（2026-09-26，接 3 个客户 4 个账套之前）：
  · 装机有现场和远程两种，远程时唯一能拿到的东西就是**对方屏幕上的几行字**；
    没有自检，技术员只能问"你双击看看有没有报错"。
  · 现有门全是**平台侧**的（打云端、打 stub），没有一条是"在客户机上自证"。
  · 财鲸V3 的账套是**别人装好的**：库名对不对、是不是真账套（有 XS/Hpdan/Khdan/No_BillID）、
    有没有打印方案，这些不在我们手里，只能在装机现场一次性查清楚。

★ 只读：连库只做元数据查询与既有只读 op，绝不写业务数据、绝不改配置。
★ 结果同时给两个出口：结构化 dict（界面渲染）与纯文本（发回给平台方）。
"""

import json
import os
import socket
import time

from . import config as cfgmod
from . import sql as db

BIZ_TABLES = ("XS", "Hpdan", "Khdan", "No_BillID")
LOG_WARN_MB = 50.0


def _item(key, label, ok, detail="", next_step=""):
    return {"key": key, "label": label, "ok": ok, "detail": str(detail or "")[:400],
            "next_step": next_step}


def _cfg_items(cfg):
    s = cfg.get("sql") or {}
    a = cfg.get("agent") or {}
    missing = [k for k in ("name", "secret") if not str(a.get(k) or "").strip()]
    missing += ["sql." + k for k in ("database", "user", "password") if not str(s.get(k) or "").strip()]
    detail = "商户码=%s  账套=%s  SQL=%s:%s/%s  版本=%s" % (
        a.get("name") or "(空)", s.get("database") or "(空)",
        s.get("server") or "(空)", s.get("port") or "(空)", s.get("user") or "(空)",
        _version())
    if a.get("install_id"):
        detail += "  装机指纹=%s" % a["install_id"]
    if missing:
        # ★ 具体缺什么、去哪儿填，交给 config.validate_identity 那一份规则（原来这里
        #   自己列了一遍必填项 = 第六份副本，改一处漏五处）。它的话术是给人看的：
        #   「商户码（agent.name）没填：一家一个，由平台方开通时给出」。
        from . import config as cfgmod
        problems = cfgmod.validate_identity(cfg) or ["缺少必填项：" + "、".join(missing)]
        return _item("config", "配置完整性", False,
                     "缺少必填项：" + "；".join(problems),
                     "补齐 config.json 这几项再启动；Agent 现在会拒绝上线而不是猜默认值。"
                     "不想手改 JSON 就双击 V3AgentGui.exe，用界面上的『本机配置』填")
    if a.get("install_id"):
        return _item("config", "配置完整性", True, detail,
                     "★ 这台机器已经有装机指纹 ⇒ 这个目录**不要整份拷给另一家**："
                     "带着同一个 install_id 上线会被云端按「同指纹不同机」拒连（4002）。"
                     "每台新机都用安装器重新生成 config.json")
    return _item("config", "配置完整性", True, detail,
                 "首次启动会自动生成装机指纹（install_id）并写回 config.json")


def _version():
    try:
        from . import __version__
        return __version__
    except Exception:  # noqa: BLE001
        return "?"


def _sql_conn(cfg):
    """连一次本机 SQL Server。失败要区分「连不上实例」和「凭据不对」——现场这两条的处置完全不同。"""
    try:
        conn = db.connect(cfg)
        return conn, None
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "20002" in msg or "ConnectionRefused" in msg.lower() or "AnyHost" in msg:
            return None, _item("sql", "能否连上 SQL Server 实例", False, msg,
                               "在本机跑 `sqlcmd -S 127.0.0.1,1433 -U sa -P <密码>`；连不上就去 "
                               "SQL 配置管理器把 TCP/IP 协议启用、重启 SQL Server 服务（防火墙放行 1433）")
        if "Login failed" in msg or "18456" in msg:
            return None, _item("sql", "SQL 登录（账号/口令）", False, msg,
                               "账号或口令不对：和 V3 装机的人核对 SQL Server 登录名与口令，"
                               "改完 config.json 再点一次自检")
        return None, _item("sql", "SQL 连接", False, msg,
                           "先看 config.json 的 server/port/user/password 四项，再确认 SQL Server 服务在跑")


def _account_sets(cfg):
    try:
        sets = db.account_sets(cfg)
    except Exception as exc:  # noqa: BLE001
        return _item("account_sets", "本机账套清单", False, exc, "连不上实例，先解决上面那条")
    err = sets.get("error")
    if err:
        return _item("account_sets", "本机账套清单", False, err, "同上：先解决 SQL 连接")
    rows = sets.get("databases") or []
    names = [r.get("name") for r in rows]
    want = str((cfg.get("sql") or {}).get("database") or "")
    hits = [r for r in rows if r.get("name") == want]
    if not hits:
        return _item("account_sets", "这个商户的账套在本机存在吗", False,
                     "配置写的账套「%s」不在本机；本机有：%s" % (want or "(空)", "、".join(names) or "(无)"),
                     "用 scripts\\switch-db.py --list 核对后改 config.sql.database —— "
                     "名字写对但连到别的库，客户看到的数字会来自另一套账")
    hit = hits[0]
    missing = hit.get("missing") or []
    if not hit.get("isAccountSet"):
        return _item("account_sets", "这个库是不是财鲸V3 的账套", False,
                     "「%s」缺业务表：%s（本机库：%s）" % (want, "、".join(missing) or "?", "、".join(names)),
                     "这套账没装 V3 或不是业务库（AccMaster/报表引擎库会被排除）——"
                     "让客户方在 V3 里把账套建完整再接")
    # ★ 两本账的机器最容易卡在这里：本机**有**那本库，但这个进程没被允许读它。
    #   远程排障时这一行必须看得见，否则人只能猜"是不是云端没放行"。
    allowed = cfgmod.allowed_databases(cfg)
    detail = "配置账套「%s」存在且业务表齐全（%s）。本机共 %d 套：%s" % (
        want, "、".join(BIZ_TABLES), len(names), "、".join(names))
    if len(names) > 1:
        detail += "　｜这个进程允许被读到：%s" % "、".join(allowed)
    left_out = [d["name"] for d in rows
                if d.get("isAccountSet") and d["name"] not in allowed]
    if left_out:
        return _item("account_sets", "本机账套清单", True, detail,
                     "本机还有没放行的账套（%s）。要让这位老板两本都能读，"
                     "在安装目录跑：scripts\\switch-db.py --allow \"%s\""
                     % ("、".join(left_out), ",".join([want] + left_out)))
    return _item("account_sets", "本机账套清单", True, detail,
                 "同一台机器多套账时，切库只在这份清单里选；切完看 status.json 的 database_verified")


def _verified(cfg, conn):
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        got = db.current_database(cur)
    except Exception as exc:  # noqa: BLE001
        return _item("verified", "实测库名 DB_NAME()", False, exc, "连上了但读不到当前库，把这条原文发给平台方")
    want = str((cfg.get("sql") or {}).get("database") or "")
    if got != want:
        return _item("verified", "实测库名 DB_NAME()", False, "实测=%s，配置=%s" % (got, want),
                     "以实测为准：现在这一台读的就是「%s」这套账。要么改配置，要么这就是另一家的账" % got)
    return _item("verified", "实测库名 DB_NAME()", True, "实测=%s（与配置一致）" % got)


def _money(cfg):
    """把"这个数是多少"打在自检里：现场唯一能证明"接的就是这套账"的动作。

    4 个账套里接错一个，页面数字看着全都正常（2026-09-26 00:38 真发生过），
    所以让技术员拿这个合计去和财鲸V3 客户端对一遍 —— 对不上当场就能发现。
    """
    try:
        from .agent import LocalAgent
        agent = LocalAgent(cfg)
        res = agent.dispatch("report.receivables.summary", {"limit": 1})
    except Exception as exc:  # noqa: BLE001
        return _item("money", "读一条真业务数（应收合计）", False, exc, "同上：业务查询挂了先发给平台方")
    if not res.get("ok"):
        return _item("money", "读一条真业务数（应收合计）", False,
                     json.dumps(res, ensure_ascii=False)[:250], "看错误码，别在现场重试写库动作")
    d = res.get("data") or {}
    total = d.get("totalReceivable", d.get("receivable_total", d.get("total")))
    count = d.get("customerCount")
    detail = "应收合计=%s  应收客户数=%s" % (total, count)
    if d.get("agingNote"):
        detail += "（账龄口径：%s）" % d["agingNote"]
    if total in (None, ""):
        return _item("money", "读一条真业务数（应收合计）", False, detail,
                     "接口通了但没有合计字段，把这条发给平台方")
    if not count:
        return _item("money", "读一条真业务数（应收合计）", True, detail,
                     "这套账现在没有应收数据：新账套正常，老账套为 0 就要怀疑连错了库")
    return _item("money", "读一条真业务数（应收合计）", True, detail,
                 "★ 拿这个数与财鲸V3 客户端里的应收合计对一遍：对不上就是接错了账套，"
                 "后面所有页面看着都「正常」，但都是另一套账的钱")


def _business(cfg):
    """跑一条真只读 op：agent.capabilities 一次性带回 版本/实测账套/op 数/打印机/打印方案。"""
    try:
        from .agent import LocalAgent
        agent = LocalAgent(cfg)
        res = agent.dispatch("agent.capabilities", {"withAccountSets": True})
    except Exception as exc:  # noqa: BLE001
        return _item("business", "跑通一条只读 op", False, exc,
                     "这一项挂了说明业务查询本身有问题（视图/存储过程缺），把原文发给平台方")
    if not res.get("ok"):
        return _item("business", "跑通一条只读 op", False, json.dumps(res, ensure_ascii=False)[:300],
                     "看 res 里的错误码；写库动作不要在现场试")
    d = res.get("data") or {}
    formats = d.get("printFormats") or []
    printers = d.get("printers") or []
    detail = "op 数=%s  实测账套=%s  打印机=%d 台  打印方案=%d 个" % (
        len(d.get("ops") or []), d.get("databaseVerified"), len(printers), len(formats))
    # ★ 把"这家到底开了哪些开关"写进报告：这是唯一一份技术员会整头发回来的东西，
    #   没有这一行的话，"这家跑的是旧包还是配置真没开"在远程支持时根本分不开。
    ao = d.get("accountOptions")
    if isinstance(ao, dict):
        def _t(v):
            return {True: "开", False: "没开"}.get(v, "未知")
        pc = ao.get("productCode") or {}
        detail += "  本店开关=多单位%s/包装单位%s/品牌%s/单位开单%s  编号=取%s级补%s位" % (
            _t(ao.get("multiUnit")), _t(ao.get("multiUnitPackage")),
            _t(ao.get("useBrandName")), _t(ao.get("billQtyByUnit")),
            pc.get("levels"), pc.get("serial_len"))
        # ★ 批次/序列号一起报：这两项决定货品页露不露录入项，而"为什么我这边没有批次"
        #   是远程支持最常问的一句 —— 报告里有这一行就不用来回猜是不是没开。
        detail += "  批次=%s 序列号=%s" % (_t(ao.get("useBatch")), _t(ao.get("useSerial")))
        if ao.get("ambiguous"):
            detail += "  ⚠这些开关同名多行且值不一致（按默认档）=%s" % "、".join(ao["ambiguous"])
    else:
        detail += "  ⚠没读到店里的开关（accountOptions 缺失 = 这台机器跑的还是旧包）"
    if not formats:
        return _item("business", "业务查询与打印", False, detail,
                     "这台机器读不到打印方案：V3 里 `AccMaster.dbo.Report_FormatSet` 是空的或凭据没权限。"
                     "客户一定会问「能不能打单」，这条不通就先别承诺打印")
    return _item("business", "业务查询与打印", True, detail)


def _cloud(cfg):
    a = cfg.get("agent") or {}
    url = str(a.get("relay_url") or "")
    host, port = "127.0.0.1", 8000
    try:
        rest = url.split("://", 1)[1]
        hostport = rest.split("/", 1)[0]
        host = hostport.split(":")[0]
        if ":" in hostport:
            port = int(hostport.split(":")[1])
    except Exception:  # noqa: BLE001
        pass
    try:
        s = socket.create_connection((host, port), timeout=5)
        s.close()
        return _item("cloud", "云端中转可达（%s:%d）" % (host, port), True, "TCP 通",
                     "TCP 通但 status.json 里 connected=false ⇒ 多半是商户码/密钥不对，"
                     "或被云端按同指纹拒连（4002）")
    except Exception as exc:  # noqa: BLE001
        return _item("cloud", "云端中转可达（%s:%d）" % (host, port), False, exc,
                     "这台机器出不了网 / 端口被防火墙挡：先确认能否访问 http://%s/，"
                     "再确认没有杀毒软件拦 V3AgentGui.exe" % host)


def _runtime_files(cfg_path):
    base = os.path.dirname(os.path.abspath(cfg_path)) if cfg_path else os.getcwd()
    logs = os.path.join(base, "logs")
    lines = []
    st = None
    st_path = os.path.join(logs, "agent.status.json")
    if os.path.isfile(st_path):
        try:
            with open(st_path, encoding="utf-8") as fh:
                st = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            lines.append("status.json 读不出来：%s" % exc)
    if st is None:
        run_item = _item("running", "后台 Agent 是否在跑", False, "没有 %s" % st_path,
                         "点「启动 Agent」或跑安装器的「一键安装并启动」；"
                         "启动后回到这里再点一次自检")
    else:
        age = int(time.time() - float(st.get("updated_ts") or 0))
        okc = bool(st.get("connected")) and age < 90
        run_item = _item("running", "后台 Agent 是否在跑", okc,
                         "connected=%s  version=%s  实测账套=%s  %d 秒前更新" % (
                             st.get("connected"), st.get("version"),
                             st.get("database_verified"), age),
                         "超过 90 秒没更新说明进程已死或被顶掉：点「启动 Agent」，"
                         "云端同名只留一条连接，别在同一目录里起两份"
                         if not okc else "")
    log_item = None
    try:
        os.makedirs(logs, exist_ok=True)
        probe = os.path.join(logs, ".write-probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        lp = os.path.join(logs, "agent.log")
        mb = os.path.getsize(lp) / 1048576.0 if os.path.isfile(lp) else 0.0
        warn = mb > LOG_WARN_MB
        log_item = _item("logs", "日志目录可写", True,
                         "%s（agent.log %.1f MB）" % (logs, mb),
                         "agent.log 到 %.0f MB 了：现在不轮转，远程排障时先让对方打包发回来再清理，"
                         "别直接删（事故留档在里面）" % mb if warn else "")
    except Exception as exc:  # noqa: BLE001
        log_item = _item("logs", "日志目录可写", False, exc,
                         "目录没权限（多半是安装到了 Program Files）：换到 C:\\V3Agent 重装")
    return [run_item, log_item], st


def run(cfg, cfg_path=None):
    """返回 {"items":[...], "ok":bool, "text":str, "written":路径}。"""
    items = [_cfg_items(cfg)]
    conn, err = _sql_conn(cfg)
    if err:
        items.append(err)
    else:
        items.append(_item("sql", "能否连上 SQL Server 实例", True,
                           "%s:%s 登录名 %s" % ((cfg.get("sql") or {}).get("server"),
                                                 (cfg.get("sql") or {}).get("port"),
                                                 (cfg.get("sql") or {}).get("user"))))
        items.append(_account_sets(cfg))
        v = _verified(cfg, conn)
        if v:
            items.append(v)
        items.append(_business(cfg))
        items.append(_money(cfg))
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    items.append(_cloud(cfg))
    extra, st = _runtime_files(cfg_path)
    items += extra
    ok = all(i["ok"] for i in items)
    text = format_text(items, ok)
    written = ""
    if cfg_path:
        try:
            logs = os.path.join(os.path.dirname(os.path.abspath(cfg_path)), "logs")
            os.makedirs(logs, exist_ok=True)
            written = os.path.join(logs, "selfcheck-%s.txt" % time.strftime("%Y%m%d-%H%M"))
            with open(written, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except Exception:  # noqa: BLE001
            written = ""
    return {"items": items, "ok": ok, "text": text, "written": written, "status": st}


def format_text(items, ok):
    lines = ["财鲸V3 本地 Agent 现场自检  %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "=" * 60]
    for it in items:
        mark = "✓" if it["ok"] else "✗"
        lines.append("%s %s：%s" % (mark, it["label"], it["detail"]))
        if it.get("next_step"):
            lines.append("    → " + it["next_step"])
    lines.append("=" * 60)
    lines.append("结论：%s" % ("全部通过，可以交付" if ok else
                          "有未通过项，按上面「→」处理后再点一次自检"))
    lines.append("（把本文件整份发给平台方就够了：logs\\selfcheck-*.txt）")
    return "\n".join(lines)
