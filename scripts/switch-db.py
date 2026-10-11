# -*- coding: utf-8 -*-
"""切换账套（sql.database）—— 测试时在 tongda / shiwantongda 之间来回跳。

用法
    python scripts\\switch-db.py --list                    看有哪些账套、当前是哪个
    python scripts\\switch-db.py --db tongda               切换（后台在跑=热切，没跑=改配置）
    python scripts\\switch-db.py --db tongda --yes         跳过二次确认（给脚本用）
    python scripts\\switch-db.py --db tongda --config 发布程序\\config.json
    python scripts\\switch-db.py --wait 120 --db shiwantongda

★ 热切换是怎么发生的
    本脚本**不直接改运行中的东西**，只写一个 logs\\agent.dbcmd.json 哨兵；
    真正换库的是后台 Agent —— 它挑「没有在途请求」的时刻换（建单不会跨库撕裂），
    自己把 config.json 落盘，再用 SELECT DB_NAME() 实测确认。
    这边只轮询 status.json 里那个**实测**值，确认到位才退 0。

★ 为什么要实测而不是相信配置
    这个项目出过一次「名字说 shiwantongda、实际连 tongda」的分叉：
    内置默认值 agent.name 与 sql.database 本就指向两套账，而发布目录与 dist
    各有一份 config.json。所以判断切没切成功，只认数据库自己报的库名。
"""

import argparse
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 跟随脚本位置，不写死盘符
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")   # 控制台默认 GBK
    except Exception:  # noqa: BLE001
        pass

import agent_daemon as dm                                     # noqa: E402
from v3agent import config as cfgmod                          # noqa: E402
from v3agent import sql as v3sql                              # noqa: E402


def resolve_config(explicit=None):
    if explicit:
        p = explicit if os.path.isabs(explicit) else os.path.join(ROOT, explicit)
        if not os.path.isfile(p):
            raise SystemExit("找不到配置文件：%s" % p)
        return os.path.abspath(p)
    env = os.environ.get("V3_AGENT_CONFIG")
    if env and os.path.isfile(env):
        return os.path.abspath(env)
    for d in (ROOT, os.getcwd()):
        p = os.path.join(d, "config.json")
        if os.path.isfile(p):
            return p
    raise SystemExit("找不到 config.json（用 --config 指定）")


def show_list(cfg):
    info = v3sql.account_sets(cfg)
    if info["error"]:
        print("查询失败：%s" % info["error"])
        return 1
    print("实例 %s:%s 上的库（已排除系统库/只读库）：" % (
        cfg["sql"].get("server"), cfg["sql"].get("port", 1433)))
    if not info["databases"]:
        print("  （一个都没有）")
        return 1
    for d in info["databases"]:
        mark = "← 当前" if d["active"] else "   "
        if d["isAccountSet"]:
            print("  %s  %-16s 账套 ✓" % (mark, d["name"]))
        else:
            print("  %s  %-16s 不是账套（缺 %s）" % (mark, d["name"], "、".join(d["missing"])))
    ok = [d["name"] for d in info["databases"] if d["isAccountSet"]]
    print("\n可切换目标：%s" % "、".join(ok))
    print("切换：python scripts\\switch-db.py --db <名字>")
    allowed = cfgmod.allowed_databases(cfg)
    print("本机允许读：%s%s" % ("、".join(allowed) or "(空)",
          "" if len(allowed) > 1 else "　（没登记清单 ⇒ 只允许当前这一本）"))
    print("  一台主机两本账要同时对外服务时：python scripts\\switch-db.py --allow \"%s,另一本\""
          % (allowed[0] if allowed else "当前库"))
    return 0


def confirm(target, current):
    print("当前账套：%s" % (current or "(未知)"))
    print("切到　　：%s" % target)
    print("★ 切完之后，手机/小程序上看到的客户、商品、单据、报表都会变成 %s 的数据。" % target)
    print("  两套账的客户名和单号长得很像，数字看着都「正常」—— 请确认真的要切。")
    ans = input("输入 yes 确认，其它任意键放弃：").strip().lower()
    return ans in ("yes", "y")


def wait_for_switch(target, wait_sec):
    """轮询 status.json，直到 database_verified == target。"""
    deadline = time.time() + wait_sec
    last_note = ""
    while time.time() < deadline:
        st = dm.read_status() or {}
        verified = st.get("database_verified") or ""
        note = st.get("db_switch") or ""
        if note and note != last_note:
            print("  %s" % note)
            last_note = note
        if verified == target:
            cfg_now = ""
            try:
                cfg_now = cfgmod.load(resolve_config()).get("sql", {}).get("database", "")
            except Exception:  # noqa: BLE001
                pass
            print("✓ 已切换：%s（DB_NAME() 实测）；config.json = %s" % (target, cfg_now or "?"))
            if cfg_now and cfg_now != target:
                print("★ 注意：实测已是 %s，但配置文件里还是 %s（落盘失败？重启会回到旧库）"
                      % (target, cfg_now))
                return 2
            return 0
        pid, alive = dm.read_pid()
        if not alive:
            print("× 后台 Agent 已退出，切换中断（最后状态：%s）" % (st.get("state") or "?"))
            return 1
        time.sleep(0.5)
    print("× 超时 %ds 仍未确认切换；最后状态：%s" % (wait_sec, last_note or "(无)"))
    return 1


def apply_allow(path, cfg, allow_arg, yes=False):
    """登记"这台机器允许被读到哪几本账"（config.sql.databases）。

    ★ 为什么不直接让人改 config：这个清单是**越界防线**，写错一个字母的症状是
      "另一本账永远读不到"，而人会先去查云端。所以这里先用 account_sets 实测每本库
      在不在本机、是不是真账套，不合格当场拒，不让它进配置文件。
    ★ 不改 sql.database：登记清单 ≠ 切库。当前在读哪本仍然由热切换决定。
    """
    dbs = cfgmod.parse_databases(allow_arg)
    if not dbs:
        print("× 清单是空的。要撤回成「只读当前这本」，直接删掉 config.json 里的 sql.databases。")
        return 1
    print("配置：%s" % path)
    print("本机当前在读：%s" % (cfgmod.get_database(cfg) or "(未知)"))
    info = v3sql.account_sets(cfg)
    if info["error"]:
        print("× 连不上本机 SQL Server，没法核对库名：%s" % info["error"])
        return 1
    real = {d["name"]: d for d in info["databases"]}
    bad = [d for d in dbs if d not in real]
    if bad:
        print("× 这几本在本机不存在：%s" % "、".join(bad))
        print("  本机有的库：%s" % "、".join(sorted(real)))
        return 1
    notset = [d for d in dbs if not real[d]["isAccountSet"]]
    if notset and not yes:
        print("× 这几本缺业务表（不是财鲸V3 账套）：%s"
              % "、".join("%s（缺 %s）" % (d, "、".join(real[d]["missing"])) for d in notset))
        print("  确认要继续就用 --yes（但读到的会是空数字，排查时极像\"这家还没开张\"）")
        return 1
    if not yes:
        print("将登记为允许读：%s（第一个是默认库）" % "、".join(dbs))
        ans = input("输入 yes 确认，其它任意键放弃：").strip().lower()
        if ans not in ("yes", "y"):
            print("已放弃。")
            return 1
    ok, msg = cfgmod.save_databases(path, dbs)
    print(("✓ " if ok else "× ") + msg)
    if not ok:
        return 1
    print("  ★ 后台 Agent 会在一两秒内自动读到新清单（不用重启）；"
          "云端那边还要把这几本登记进商户的 databases，两边都放行才读得到。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="切换本地 Agent 的账套（sql.database）")
    ap.add_argument("--db", help="目标账套名，如 tongda / shiwantongda")
    ap.add_argument("--list", action="store_true", help="列出可切换的账套")
    ap.add_argument("--allow", default="",
                    help="登记**本机允许被读到的账套清单**（逗号分隔，第一个=默认库）。"
                         "一台主机两本账时用；不带这个参数的请求仍只读 sql.database")
    ap.add_argument("--config", help="指定 config.json（默认按 Agent 的优先级找）")
    ap.add_argument("--yes", action="store_true", help="跳过二次确认")
    ap.add_argument("--wait", type=int, default=90, help="等热切换完成的秒数，默认 90")
    args = ap.parse_args()

    path = resolve_config(args.config)
    cfg = cfgmod.load(path)
    current = cfgmod.get_database(cfg)

    if args.allow:
        return apply_allow(path, cfg, args.allow, args.yes)

    if args.list or not args.db:
        rc = show_list(cfg)
        if not args.db:
            return rc
        if rc:
            return rc

    target = str(args.db).strip()
    print("配置：%s" % path)
    ok, why = v3sql.is_switchable(cfg, target)
    if not ok:
        print("× %s" % why)
        return 1

    if target == current:
        print("当前已经是 %s，无需切换。" % target)
        return 0

    if not args.yes and not confirm(target, current):
        print("已放弃。")
        return 1

    pid, alive = dm.read_pid()
    if alive:
        ok, msg = dm.request_db_switch(target, source="cli")
        print(msg)
        if not ok:
            return 1
        print("后台 Agent 在跑（PID %d），等待它在两条请求之间完成切换…" % pid)
        return wait_for_switch(target, args.wait)

    print("后台 Agent 未运行 → 直接改配置文件（下次启动即生效）")
    ok, msg = cfgmod.save_database(path, target)
    print(("✓ " if ok else "× ") + msg)
    if not ok:
        return 1
    cfg2 = cfgmod.load(path)
    try:
        with v3sql.session(cfg2) as conn:
            print("✓ 用新配置实连一次：DB_NAME() = %s" % v3sql.current_database(conn.cursor()))
    except Exception as exc:  # noqa: BLE001
        print("× 新配置连不上：%s" % str(exc)[:200])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
