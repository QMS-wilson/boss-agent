# -*- coding: utf-8 -*-
"""配置加载：JSON 文件（默认 config.json，可用 V3_AGENT_CONFIG 覆盖）。"""

import json
import os
import re
import shutil


DEFAULTS = {
    "sql": {
        "server": "127.0.0.1",
        "port": 1433,
        # ★ user/password 从 2026-09-26 起**不再有默认值**（P1-6 收尾）。
        #   原来这里兜 sa/123：装机的人没填也能起来，连到本机 SQL Server 上
        #   任何一个用默认口令的库 —— 而"哪家在读哪套账"全靠 config 里的 database，
        #   凭据兜底等于把"连错了也能跑"这条路留着。
        "tds_version": "7.0",
        "login_timeout": 10,
        "timeout": 120,
    },
    "agent": {
        "secret": "change-me",
        "relay_url": "ws://127.0.0.1:8000/ws/agent",
        "transport": "ws",  # ws=WebSocket 长连接；http=HTTP 轮询（公网链路不稳时用）
        "reconnect_sec": 5,
        "heartbeat_sec": 25,
    },
    "price": {
        # 无最近销价时的默认价格列（本账套 98% 货品价格存在 D_SJ1）
        "default_price_column": "D_SJ1",
        "recent_price_field": "D_PriceTax",
    },
    "log_level": "INFO",
}

# ★★ 身份与凭据**故意没有默认值**：
#   原来这里写着 agent.name=shiwantongda、sql.database=tongda，而两者本来就不同名：
#     · 配置文件少写 database ⇒ 静默连到 tongda ⇒ 「用 A 家的名字读 B 家的账」，
#       页面数字看着全正常，客户和运营都看不出错（2026-09-25 实测踩过，在线 daemon 就这样跑了 2 小时）；
#     · 装机照抄 name 默认值 ⇒ 第二家上线把第一家顶下线（云端同名只留一条连接）。
#   所以宁可启动即失败并说清该填什么，也不能猜。
REQUIRED = (("agent", "name"), ("sql", "database"), ("sql", "user"), ("sql", "password"))

HINT = ("agent.name = 商户码（云端按它认租户，一家一个，不能撞别人家）；"
        "sql.database = 这个商户的账套库名（V3 装在哪套账就读哪套）；"
        "sql.user / sql.password = 本机 SQL Server 的登录凭据（不再兜 sa/123，必须显式写）。"
        "可以用 scripts/switch-db.py --list 看本机有哪些账套")


class ConfigError(Exception):
    """配置缺失/不可用 —— 调用方应当把它显示出来并拒绝上线，不要兜底。"""


# ---------------------------------------------------- 能不能上线：一份判据，三处共用
#
# ★ 为什么这些规则住在这里而不是安装器里：安装器（setup_gui）与 GUI 面板问的是
#   同一批字段，规则抄两份就一定会漂（本项目反复栽在"名字说 A、连的是 B"这类
#   两个真源上）。GUI 首启填表、安装器装机、daemon 上线前自查，都调这一个函数。
#   安装器那份内联校验由 verify-installer.py 逐条比对这里，防它偷偷漂移。

CODE_RE = re.compile(r"^[a-z][a-z0-9-]{1,29}$")


def code_rules():
    """商户码规则：**与云端 `cloudapi/admin.py`、H5 `utils/merchant.js` 同一套**。

    不一致的后果很具体：后台能开出 `fs-tongda` 这样的码，客户机装不上（或反过来）。
    留这个函数名是给门用的（`verify-installer.py` 逐条比对四处规则文本）。
    """
    return CODE_RE


# 这些词不能当商户码：它们会遮掉入口或静态资源的路径前缀
CODE_RESERVED = ("", "h5", "web", "index", "static", "assets",
                 "admin", "api", "ws", "logs")
SECRET_MIN_LEN = 8


def blank_keys_for_first_run():
    """首启种子里必须抹空的键 —— **身份与凭据一个都不预填**。

    ★ 模板里 `sql.user` 写着 `sa`：那是"本机 SQL Server 登录名"这种环境常量，
      预填了等于又回到"没填也能跑"。这里连它一起抹掉，宁可让人多敲一次。
    """
    return ("agent.name", "agent.secret", "sql.database", "sql.user", "sql.password")


def first_run_config(template=None):
    """由 config.template.json 造一份"能打开界面、但不能上线"的种子配置。"""
    import copy
    cfg = copy.deepcopy(template) if isinstance(template, dict) else {}
    cfg.setdefault("sql", {})
    cfg.setdefault("agent", {})
    for dotted in blank_keys_for_first_run():
        sec, key = dotted.split(".")
        cfg[sec][key] = ""
    cfg.pop("databases", None)
    cfg["sql"].pop("databases", None)
    # 安装指纹是"这台机器"的身份，必须由 daemon 现生成，不能从模板带过来
    cfg["agent"].pop("install_id", None)
    cfg["_首启"] = ("这份是 GUI 自动创建的空配置：在本机填完（或点界面上的『本机配置』）"
                    "才能上线。缺哪一项，启动时会点名告诉你，不会带着默认值偷偷连上云端。")
    return cfg


def validate_identity(cfg):
    """返回"这条配置不许上线"的人话清单（空列表 = 可以上线）。

    ★ 只做**格式与完整性**判断，不连库、不联网 —— 真能不能读要等 daemon 起来实测
      （`database_verified`）。这里挡的是"一看就会出事"的那几类：
      商户码格式错（顶掉别家/路径被遮）、密钥没粘贴（一直 401）、
      账套留空（连到别的账）、SQL 凭据留空（不再兜 sa/123）。
    """
    problems = []
    a = (cfg or {}).get("agent") or {}
    s = (cfg or {}).get("sql") or {}
    name = str(a.get("name") or "").strip()
    if not name:
        problems.append("商户码（agent.name）没填：一家一个，由平台方开通时给出")
    elif not code_rules().match(name):
        problems.append("商户码「%s」格式不对：小写字母开头、2-30 位、只用小写字母/数字/-"
                        "（例如 shiwantongda、fs-tongda）" % name)
    elif name in CODE_RESERVED:
        problems.append("商户码「%s」是访问路径的保留字，不能当商户码（会把 /%s/ 这个入口遮掉）"
                        % (name, name))
    secret = str(a.get("secret") or "").strip()
    if not secret:
        problems.append("Agent 密钥（agent.secret）没填：在云端后台『商户』页开通时显示一次")
    elif secret == "change-me":
        # DEFAULTS 里那个占位值不算凭据。★ 话术要同时覆盖"文件里没写"这一种：
        #   没写的人会被 DEFAULTS 补成 change-me，说"你粘错了"会让人一头雾水。
        problems.append("Agent 密钥没填（现在还是占位值 change-me）：把平台方给的那串粘进来")
    elif len(secret) < SECRET_MIN_LEN:
        problems.append("Agent 密钥至少 %d 位（现在 %d 位）—— 多半是没粘贴全"
                        % (SECRET_MIN_LEN, len(secret)))
    relay = str(a.get("relay_url") or "").strip()
    if not relay.startswith("ws"):
        problems.append("云端地址（agent.relay_url）要 ws:// 或 wss:// 开头，现在是「%s」" % relay)
    if not str(s.get("database") or "").strip():
        problems.append("账套库名（sql.database）没填：填错不会报错，但客户看到的钱是另一套账的")
    if not str(s.get("user") or "").strip():
        problems.append("SQL 账号（sql.user）没填：本机 SQL Server 的登录名（Agent 不再兜任何默认凭据）")
    if not str(s.get("password") or ""):
        problems.append("SQL 密码（sql.password）没填 —— 不再兜底 sa/123，留空 Agent 会拒绝启动")
    # ★ 端口单独处理：`int(x or 1433)` 会把 0 当成"没写"放过（0 是 falsy），
    #   而 0 恰恰是最该被拦下来的写法 —— 它会让 Agent 连到 1433 之外的某个地方或失败。
    if "port" in s and not str(s.get("port") or "").strip():
        problems.append("SQL 端口（sql.port）空着：要么写 1433，要么把这个键删掉让它走默认")
    else:
        try:
            port = int(s.get("port", 1433))
            if not 0 < port < 65536:
                raise ValueError
        except Exception:  # noqa: BLE001
            problems.append("SQL 端口（sql.port）要是 1-65535 的数字，现在是「%s」" % s.get("port"))
    for d in parse_databases(s.get("databases")):
        if "," in d or "\n" in d or len(d) > 128:
            problems.append("放行账套名「%s」不合法（不能带分隔符，单个不超过 128 位）" % d)
    return problems



def load(path=None):
    path = path or os.environ.get("V3_AGENT_CONFIG") or "config.json"
    ap = os.path.abspath(path)
    cfg = json.loads(json.dumps(DEFAULTS))
    if not os.path.isfile(ap):
        raise ConfigError(
            "找不到配置文件：%s\n  双击 V3AgentGui.exe 打开控制台，它会在界面上让你填并自动建好；"
          "  装机走安装器（setup_gui）。手动建就把 config.template.json 复制成 config.json 填写 %s"
            % (ap, HINT))
    with open(ap, "r", encoding="utf-8") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConfigError("配置文件不是合法 JSON：%s（%s）" % (ap, exc))
    if not isinstance(data, dict):
        raise ConfigError("配置文件顶层必须是对象：%s" % ap)
    _split_multi_database(data)
    for section in ("sql", "agent", "price"):
        if isinstance(data.get(section), dict):
            cfg[section].update(data[section])
    cfg.update({k: v for k, v in data.items() if k not in ("sql", "agent", "price")})
    missing = ["%s.%s" % (s, k) for s, k in REQUIRED
               if not str(((cfg.get(s) or {}).get(k)) or "").strip()]
    if missing:
        raise ConfigError("配置 %s 缺少必填项：%s\n  %s"
                          % (ap, "、".join(missing), HINT))
    cfg["_meta"] = {"config_path": ap}
    return cfg


# -------------------------------------------------------------- 账套（sql.database）

def load_any(path=None):
    """和 `load()` 一样合并 DEFAULTS，但**不因缺必填项而抛**，返回 (cfg, 问题列表)。

    ★ 为什么需要它：`V3AgentGui.exe` 是 --noconsole 的，print 对方看不见。
      配置没填全的时候恰恰最需要一份能发回来的报告，而以前 `load()` 在这里抛错，
      自检直接退出、**连 logs\\selfcheck-*.txt 都不写** ⇒ 技术员点了「现场自检」
      等于没点，远程支持什么也拿不到。现在照样出报告，第一条就是"缺什么、去哪儿填"。
    """
    path = path or os.environ.get("V3_AGENT_CONFIG") or "config.json"
    ap = os.path.abspath(path)
    cfg = json.loads(json.dumps(DEFAULTS))
    problems = []
    if not os.path.isfile(ap):
        problems.append("没有配置文件 %s：双击 V3AgentGui.exe，在『本机配置』里填完保存" % ap)
        return cfg, problems
    try:
        with open(ap, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as exc:
        problems.append("配置文件不是合法 JSON：%s（%s）" % (ap, exc))
        return cfg, problems
    if not isinstance(data, dict):
        problems.append("配置文件顶层必须是对象：%s" % ap)
        return cfg, problems
    _split_multi_database(data)
    for section in ("sql", "agent", "price"):
        if isinstance(data.get(section), dict):
            cfg[section].update(data[section])
    cfg.update({k: v for k, v in data.items() if k not in ("sql", "agent", "price")})
    # ★ 问题清单只看**文件里真写了什么**，不看 DEFAULTS 补出来的那一份：
    #   否则"文件里没写 secret"会被 DEFAULTS 的占位值 change-me 遮成一句莫名其妙的话，
    #   而真实情况就是"这个文件还是空的"。cfg 该补的照补（自检要能往下跑），
    #   判据用 raw —— 告诉人的永远是"你那份文件缺什么"。
    problems = validate_identity(data)
    cfg["_meta"] = {"config_path": ap}
    return cfg, problems


def get_database(cfg):
    return ((cfg or {}).get("sql") or {}).get("database") or ""


# -------------------------------------------------------------- 装机指纹（install_id）

def install_id(cfg):
    return str(((cfg or {}).get("agent") or {}).get("install_id") or "").strip()


def ensure_install_id(cfg, path=None):
    """首次运行生成一个装机指纹并落盘，之后一直复用。

    ★ 为什么必须有它：云端只允许同名一条连接，而"客户机上多装了一份 Agent"
      是本项目最难的故障（无限互踢、接口时好时坏）。有了每台唯一的指纹，
      云端才能把"这台机器网络抖了下重连"和"另一台机器拿着同一份密钥上线"
      分开处理 —— 后者会被拒(close 4002)，而不是把在线那家顶掉。
    ★ 落盘失败不阻断启动：没指纹只是退化成旧行为（新连接顶旧连接），
      不能因为写不进 config.json 就不让客户的 Agent 上线。
    """
    if install_id(cfg):
        return cfg["agent"]["install_id"]
    import uuid
    new = uuid.uuid4().hex[:12]
    cfg.setdefault("agent", {})["install_id"] = new
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data.setdefault("agent", {})["install_id"] = new
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            pass
    return new


def set_database(cfg, database):
    """改内存里的目标库（就地改，供 Agent 热切换用）。返回改前的库名。"""
    old = get_database(cfg)
    cfg.setdefault("sql", {})["database"] = database
    return old


# 配置文件备份只留一份滚动副本：切库是高频调试动作，
# 留一堆 config.json.bak-db-<时间戳> 会把发布目录淹掉。
CONFIG_BACKUP = "config.json.bak-last-db-switch"


def save_database(path, database):
    """把 sql.database 落盘（原子替换 + 滚动备份），保留文件里其它键不动。

    ★ 为什么"读回来改一个键再写回去"而不是整份重写：
      config.json 里除了 database 还有 secret / 密码 / 取价列，
      用 DEFAULTS 重建会把客户机上的密钥抹成 change-me（= Agent 立刻掉线）。
    """
    import tempfile

    if not path or not os.path.isfile(path):
        return False, "找不到配置文件：%s" % (path or "(空)")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return False, "配置文件解析失败，未做任何修改：%s" % exc
    if not isinstance(data, dict):
        return False, "配置文件格式不是对象，未做任何修改"

    old = ((data.get("sql") or {}) or {}).get("database") or ""
    sql = data.get("sql")
    if not isinstance(sql, dict):
        sql = {}
        data["sql"] = sql
    if sql.get("database") == database:
        return True, "配置已是 %s，无需改动" % database
    sql["database"] = database

    try:
        shutil.copy2(path, os.path.join(os.path.dirname(path) or ".", CONFIG_BACKUP))
    except Exception:  # noqa: BLE001
        pass                      # 备份失败不能挡住切库本身
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp",
                               dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)     # 原子替换：不会留下半个 config.json
    except Exception as exc:  # noqa: BLE001
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass
        return False, "写入配置失败（当前仍用 %s）：%s" % (old, exc)
    return True, "已落盘 %s：%s → %s" % (path, old or "(无)", database)


# ============================================================ 本机被允许读哪几本账
#
# ★ 为什么本地也要有一份清单（`sql.databases`）：
#   云端那份（`merchants[码].databases`）回答的是"这位老板能进哪几家/哪几本"，
#   而一台主机上**可能装着两家的库**。如果只信请求里带来的库名，
#   A 家的一条请求写个 B 家的库名，这个进程就会照着去连 B 家的账 ——
#   越界发生在"同一个进程能看见的库"这一层，本地必须也挡一次。
# ★ 缺省值是**收紧**的：没写 databases 就等于只允许 `sql.database` 那一本。
#   所以老 config 的行为一个字节都没变，不会因为升级忽然放开整台机器。
#   要两本账一起服务，得显式登记（`scripts/switch-db.py --allow`）。

def parse_databases(value):
    """list / 逗号 / 换行 三种写法都收（技术员从 V3 里抄出来常是后者两种）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        import re
        parts = re.split(r"[,，、;；\n\r\t ]+", str(value))
    out = []
    for p in parts:
        name = str(p or "").strip().strip("'\"")
        if name and name not in out:
            out.append(name)
    return out


def _split_multi_database(data):
    """把误写进 `sql.database` 的多值拆开（2026-10-09 现场踩过）。

    ★ 症状特别误导：`sql.database` 是**单值**字段（多本账要写 `sql.databases`）。
      一旦写成 "tongda,shiwantongda"：
        · 默认库变成一个不存在的库名 → 启动时 DB_NAME() 实测直接失败（18456 登录失败）；
        · 拒绝横幅打印"本机不允许读账套「shiwantongda」。这台机器被允许读：
          tongda,shiwantongda" —— 那串其实**就是同一个畸形值**（清单里只有它一项），
          看着像权限没放行，于是人会一直去查云端/清单，查不出所以然。
      ⇒ 读取时直接拆开：第一个当默认库，其余并入放行清单（两者都是单值语义）。
      只认 `sql` 段，其它键一概不动。返回 (是否改过, 原名, 新默认名)。
    """
    import re

    sql = (data or {}).get("sql") if isinstance(data, dict) else None
    if not isinstance(sql, dict):
        return False, "", ""
    raw = str(sql.get("database") or "").strip()
    parts = [p.strip() for p in re.split(r"[,，、;；\n\r\t ]+", raw) if p.strip()]
    if len(parts) <= 1:
        return False, raw, raw
    sql["database"] = parts[0]
    extra = []
    for d in list(parts[1:]) + list(sql.get("databases") or []):
        name = str(d or "").strip()
        if name and name != parts[0] and name not in extra:
            extra.append(name)
    sql["databases"] = extra
    return True, raw, parts[0]


def allowed_databases(cfg):
    """这个进程被允许服务的账套清单。

    ★★ 唯一一条默认库规则：**默认库恒等于 `sql.database`，并排在清单第一位**。
      清单本身只回答"允许被读到哪几本"，不回答"默认是哪本" ——
      否则就会出现"清单第一项说 A、不带库名的请求实际读 B"这种两个真源，
      而这正是本项目反复踩的那类坑（名字说 A 家、连的是 B 家账）。
      所以热切换之后清单第一位会自动变成新库。
    """
    s = (cfg or {}).get("sql") or {}
    cur = str(s.get("database") or "").strip()
    dbs = [d for d in parse_databases(s.get("databases")) if d != cur]
    if cur:
        dbs.insert(0, cur)
    return dbs


def default_database(cfg):
    return (allowed_databases(cfg) or [""])[0]


def database_allowed(cfg, name):
    name = str(name or "").strip()
    return bool(name) and name in allowed_databases(cfg)


def refresh_databases(cfg, path, mtime=None):
    """把磁盘上那份 `sql.databases` 同步进内存 cfg，**其余键一概不动**。

    ★ 为什么要有：装机现场用 `switch-db.py --allow` 登记第二本账之后，
      跑着的 daemon 手里还是启动时那份 cfg ⇒ 症状是"清单加了还是读不到那本"，
      而第一反应往往会去查云端有没有放行。所以后台每轮询一次哨兵就顺手同步这一个键。
    ★ 只认 databases 一个键是刻意的：`sql.database`（当前在读）归热切管，
      secret/密码归装机管 —— 从磁盘反向覆盖它们会做出"热切被配置文件改回去"这类怪事。
    ★ 传 mtime 可以先按修改时间跳过读盘（轮询场景用）。返回 (是否变化, 当前清单)。
    """
    try:
        if not path or not os.path.isfile(path):
            return False, allowed_databases(cfg)
        if mtime is not None:
            try:
                if float(os.path.getmtime(path)) <= float(mtime):
                    return False, allowed_databases(cfg)
            except Exception:  # noqa: BLE001
                pass
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        disk = parse_databases(((data or {}).get("sql") or {}).get("databases"))
    except Exception:  # noqa: BLE001  读不到就保持现状，绝不让轮询把服务带崩
        return False, allowed_databases(cfg)
    sql = cfg.setdefault("sql", {})
    if disk and sql.get("databases") != disk:
        sql["databases"] = disk
        return True, allowed_databases(cfg)
    return False, allowed_databases(cfg)


def save_databases(path, databases):
    """只改 `sql.databases` 一个键（原子写 + 滚动备份，与 save_database 同一路数）。"""
    import tempfile

    if not path or not os.path.isfile(path):
        return False, "找不到配置文件：%s" % (path or "(空)")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return False, "配置文件解析失败，未做任何修改：%s" % exc
    if not isinstance(data, dict):
        return False, "配置文件格式不是对象，未做任何修改"
    sql = data.get("sql")
    if not isinstance(sql, dict):
        sql = {}
        data["sql"] = sql
    dbs = parse_databases(databases)
    if not dbs:
        return False, "清单不能清空 —— 至少要有当前那本账（%s）" % (sql.get("database") or "(空)")
    cur = str(sql.get("database") or "").strip()
    # 落盘也按"当前在读的那本排第一"，让文件内容与 allowed_databases() 读出来的一致
    if cur in dbs:
        dbs = [cur] + [d for d in dbs if d != cur]
    elif cur:
        dbs = [cur] + dbs
    if sql.get("databases") == dbs:
        return True, "清单已是 %s，无需改动" % "、".join(dbs)
    sql["databases"] = dbs
    try:
        shutil.copy2(path, os.path.join(os.path.dirname(path) or ".", CONFIG_BACKUP))
    except Exception:  # noqa: BLE001
        pass
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp",
                               dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass
        return False, "写入配置失败（当前清单未变）：%s" % exc
    return True, "已落盘 %s：允许读 %s（默认 %s）" % (path, "、".join(dbs), dbs[0])
