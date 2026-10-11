# -*- coding: utf-8 -*-
"""Agent 运行器：控制台模式（本地调试）与 WebSocket 模式（接云端中转）。"""

import json
import logging
import os
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from decimal import Decimal

# ★ 只把要用的函数引进来，不 `from . import actor`：dispatch 的形参本来就叫 actor，
#   模块名会被形参遮掉 —— 第一版就是这么把 `'str' object has no attribute 'use'`
#   送到客户机上的（登录页直接报这一句，2026-09-29 换包后暴露）。
from .actor import use as actor_scope
from . import config as cfgmod
from . import ops, sql as db
from .__init__ import __version__
from .yxparts.errors import BillConflict
from .yxparts.errors import YxError

log = logging.getLogger("v3agent")


def _needs_convert(obj, _depth=0):
    """快速探测：这棵结构里有没有需要转换的类型（Decimal / datetime）。

    ★ 动机（2026-09-22 实测）：`_jsonable` 原先**无条件**递归重建整棵结果树，
      哪怕数据全是 str/int（前端绝大多数列表就是这样）也要走一遍
      dict/list 解包 —— 大结果集（如 XSMX 全表 95959 行）实测取数 1955ms 里
      有相当一部分耗在这里。
    ★ 语义保证：本函数只**读**不**写**，且只在**确定干净**时让调用方跳过转换。
      一旦发现 Decimal/datetime（或未知的自定义对象）立即返回 True ⇒ 回退到
      完整转换路径，行为与改造前完全一致。
    ★ 深度上限：正常结果集不会很深；给 32 层上限，超限保守返回 True（宁可多跑
      一遍转换，也不能漏转）。
    """
    if _depth > 32:
        return True
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return False
    if isinstance(obj, (Decimal, datetime)):
        return True
    if isinstance(obj, dict):
        for v in obj.values():
            if _needs_convert(v, _depth + 1):
                return True
        return False
    if isinstance(obj, (list, tuple)):
        for v in obj:
            if _needs_convert(v, _depth + 1):
                return True
        return False
    # 其他类型（bytes、自定义对象…）一律保守返回 True
    return True


def _jsonable(obj):
    """把 Decimal/datetime 等转为 JSON 可序列化类型，避免回包崩溃。"""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def to_jsonable(data):
    """对外统一入口：能跳过就跳过，跳不过才做完整转换。

    ★ 干净的树直接原样返回（零拷贝、零重建），不干净才调 `_jsonable`。
    """
    if _needs_convert(data):
        return _jsonable(data)
    return data


class LocalAgent:
    """op 执行器。

    ★ 账套可热切换（2026-09-24）：每个 op 都在这里**自己开一条 pymssql 连接**
      （`db.session(self.cfg)` 读的是当时的 `cfg["sql"]["database"]`），
      所以只要把"换库"限制在两条 op 之间发生，就不可能出现单头落在 A 库、
      明细落在 B 库这种撕裂结果 —— 建单含 commit 全在同一个 op 内，天然原子。
      `_inflight` 就是为此而存在的闸门。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._guard = threading.Lock()
        self._inflight = 0
        # ★ 请求级路由（Step 2）留下的痕迹：这台机器上这个进程实际服务过哪几本账。
        #   给 status 帧的 served_databases 用 —— 云端后台要能看见"一家两账套真的在两本之间跑"，
        #   而不是只有一个 database_verified（那只是当前默认库）。
        self._served = {}

    # ---- 账套切换闸门 ----

    @property
    def inflight(self):
        return self._inflight

    def database(self):
        return ((self.cfg or {}).get("sql") or {}).get("database") or ""

    def served_databases(self):
        """按最近服务时间排序的库名（默认库即使没被点名也算服务过）。"""
        cur = self.database()
        out = sorted(self._served, key=lambda k: -self._served[k])
        if cur and cur not in out:
            out.insert(0, cur)
        return out

    def _mark_served(self, database):
        with self._guard:
            self._served[str(database or "").strip()] = time.time()

    def cfg_for_database(self, database):
        """返回一份只把 sql.database 换成目标库的 cfg **副本**。

        ★ 绝不就地改 self.cfg：那份是热切哨兵与状态上报读的对象，
          按请求改它等于把"这一条读哪本"升级成进程级长期状态 ——
          下一条不带库名的请求就会读到不该读的账。
        """
        import copy
        cfg = copy.deepcopy(self.cfg)
        cfg.setdefault("sql", {})["database"] = str(database or "").strip()
        return cfg

    def pick_database(self, requested):
        """裁决这一条请求落在哪本账。返回 (库名, cfg, 错误响应或 None)。

        三道门，缺一就可能串账：
          1) 没点名 → 默认库（行为与 Step 2 之前**完全一致**，不带库名的老客户端零影响）
          2) 点名了但不在**本地清单**（config.sql.databases）→ DATABASE_NOT_ALLOWED
             ★ 一台主机可能装着两家的库，只信云端那份的话，A 家请求写个 B 家库名就能读到 B 家
          3) 点名了且本地允许，但那本库不是财鲸V3 账套（缺业务表）→ DATABASE_NOT_ACCOUNT_SET
             （这挡的是"清单登记错了/客户又装了个空库"，宁可拒绝也不要返回一堆空数字）
        """
        name = str(requested or "").strip()
        if not name or name == self.database():
            self._mark_served(self.database())
            return self.database(), self.cfg, None
        if not cfgmod.database_allowed(self.cfg, name):
            return "", None, {"ok": False, "error": {
                "code": "DATABASE_NOT_ALLOWED",
                "message": "本机不允许读账套「%s」。这台机器被允许读：%s。"
                           "要加就在客户机上跑 scripts\\switch-db.py --allow <库名>"
                           % (name, "、".join(cfgmod.allowed_databases(self.cfg)) or "(空)")}}
        return name, self.cfg_for_database(name), None

    def try_set_database(self, database):
        """没有在途请求时才换库。返回 (ok, 旧库名或拒绝原因)。"""
        with self._guard:
            if self._inflight:
                return False, "有 %d 条请求在途，等它们跑完再切" % self._inflight
            sql = self.cfg.setdefault("sql", {})
            old = sql.get("database") or ""
            if old == database:
                return True, old
            sql["database"] = database
            return True, old

    def _account_set_cache(self):
        if not hasattr(self, "_acct_ok"):
            self._acct_ok = set()
        return self._acct_ok

    def _verify_routed_db(self, cur, target):
        """按请求路由到**非默认库**时，实测一下它是不是财鲸V3 账套。

        ★ 只在路由时查、且查过就记住：默认路径一条 SQL 都不多问，
          行为与 Step 2 之前逐字节一致（这条项目的老规矩）。
        ★ 为什么必须查：清单登记错一个字母、或客户又装了个没初始化的空库时，
          不查就会返回一堆空列表/零数字，页面看着"这家还没开张"，
          而真实原因是连错了库 —— 那是最难查的一类故障。
        """
        cache = self._account_set_cache()
        if target in cache:
            return None
        got, missing = db.assert_account_set(cur)
        if missing:
            return {"ok": False, "error": {
                "code": "DATABASE_NOT_ACCOUNT_SET",
                "message": "账套「%s」(实测=%s) 缺业务表：%s —— 它不是财鲸V3 的账套，"
                           "不要把这种库登记进 sql.databases" % (target, got, "、".join(missing))}}
        cache.add(target)
        return None

    def dispatch(self, op_name, params, database=None, actor_code=""):
        """执行白名单 op，统一异常转错误响应。

        `database` = Step 2 的**请求级账套路由**：一条请求可以点名读哪本账，
        与"进程当前默认库"无关。不带这个参数时行为与改造前完全一致。
        ★ 它不改 config、不碰热切哨兵 —— 见 pick_database 的注释。

        `actor_code` = 这条请求的操作员代码（V3 的 UserID），建单/审核/作废那几列就写它。
        只认云端中转帧带下来的值（帧上的键仍叫 `actor`），业务 params 里的一律不作数
        —— 见 v3agent/actor.py。形参**故意不叫 actor**：那会遮住模块名。
        """
        handler = ops.OPS.get(op_name)
        if handler is None:
            return {"ok": False, "error": {"code": "UNKNOWN_OP", "message": f"未知操作：{op_name}"}}
        target, cfg, denied = self.pick_database(database)
        if denied:
            return denied
        with self._guard:
            self._inflight += 1
        try:
            try:
                with db.session(cfg) as conn:
                    cur = conn.cursor()
                    if target != self.database():
                        bad = self._verify_routed_db(cur, target)
                        if bad:
                            return bad
                    with actor_scope(actor_code):
                        data = handler(cur, cfg, params or {})
                    try:
                        conn.commit()
                    except Exception as exc:  # noqa: BLE001
                        # 只读 op 可能没有开启事务，pymssql 会抛 3902；不影响结果
                        if "no corresponding BEGIN TRANSACTION" not in str(exc):
                            raise
            except Exception as exc:  # noqa: BLE001
                # 库连不上时给专门的错误码：运维要能区分"这家的 Agent 坏了"
                # 和"这本账不存在/没权限"，后者改的是清单不是客户机
                msg = str(exc)
                if target != self.database() and any(
                        k in msg for k in ("Cannot open database", "4060", "Login failed",
                                           "20002", "Adaptive Server")):
                    return {"ok": False, "error": {
                        "code": "DATABASE_NOT_FOUND",
                        "message": "连不上账套「%s」：%s" % (target, msg[:200])}}
                raise
            self._mark_served(target)
            resp = {"ok": True, "data": data}
            # ★ 只有**真的按请求路由到非默认库**时才在信封上回 database：
            #   不带库名时响应结构与改造前逐字节一致，老前端与老云端不会因为多一个键变化。
            if target != self.database():
                resp["database"] = target
            return resp
        except YxError as exc:
            # 燕秀的业务错误：带 YX_ 错误码与定位信息（App 按码给不同引导），
            # 不是内部故障，所以不打 exception 日志，避免把用户操作记成系统错误。
            return {"ok": False, "error": exc.to_error()}
        except BillConflict as exc:
            # ★ 改单乐观锁冲突（2026-10-11）：也不是内部故障，是"你的内容过期了"。
            #   App 据此给"退出去重新打开"的引导，所以要与 BAD_REQUEST 区分开。
            return {"ok": False, "error": exc.to_error()}
        except LookupError as exc:
            return {"ok": False, "error": {"code": "NOT_FOUND", "message": str(exc)}}
        except ValueError as exc:
            return {"ok": False, "error": {"code": "BAD_REQUEST", "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001
            log.exception("op %s failed", op_name)
            return {"ok": False, "error": {"code": "INTERNAL", "message": str(exc)}}
        finally:
            with self._guard:
                self._inflight -= 1

    def handle_request(self, req):
        req_id = req.get("id") or req.get("request_id") or ""
        op_name = str(req.get("op") or req.get("operation") or "").strip()
        # 账套可以在信封上给，也可以在 params 里给（调试台手填 JSON 时更顺手）；
        # 两处都认，但**信封优先** —— params 是业务参数，不该被路由语义污染。
        wanted = str(req.get("database") or (req.get("params") or {}).get("database") or "").strip()
        # ★ actor 与 database 相反：**只认信封，绝不从 params 认**。
        #   业务体是客户端拼的，谁都能在里面塞一个 "actor":"101" 把单挂到别人名下；
        #   账套名塞错了最多是读哪本，制单人塞错了是**责任记到错的人头上**，不能同一个口子。
        who = str(req.get("actor") or "").strip()
        resp = to_jsonable(self.dispatch(op_name, req.get("params") or {},
                                         database=wanted, actor_code=who))
        resp.update({"id": req_id, "op": op_name})
        return resp


def status_payload(agent, database="", verified=None, uptime_sec=0):
    """状态帧的唯一构造处（源码模式 run_ws 与生产常驻 agent_daemon 共用）。

    ★ 与 hello_payload 同一个理由：本项目有两套 WS 实现，字段各写一份就会漂移，
      症状是"只有 exe 上报了放行清单、源码模式下后台看不见"这种极难查的分叉
      （verify-install-id.py 对握手帧已经有这条断言，状态帧现在一并纳入）。
    ★ `verified=None` 表示"这一侧没有实测值"（源码模式不去假装连过库），
      与"实测值是空串"是同一件事实，都不该编造。
    """
    cfg = getattr(agent, "cfg", None) or {}
    return {
        "type": "status",
        "database": str(database or (cfg.get("sql") or {}).get("database") or ""),
        "database_verified": "" if verified is None else str(verified or ""),
        # 客户机自己放行的清单 + 实际服务过的账套（一家两本账时云端要看差集）
        "allowed_databases": cfgmod.allowed_databases(cfg),
        "served_databases": agent.served_databases() if hasattr(agent, "served_databases") else [],
        "uptime_sec": int(uptime_sec or 0),
    }


def hello_payload(cfg):
    """握手帧的唯一构造处。

    ★ 为什么抽这一个函数：本项目有两套 WebSocket 实现（v3agent/agent.py 源码模式
      与 agent_daemon.py 生产常驻），握手字段以前各写一份 —— 云端一旦按某个字段
      做判断（比如装机指纹），漏改一边就等于让另一边永久走旧逻辑，且症状是
      "只有源码模式能用/只有 exe 能用"这种极难查的分叉。握手只此一处拼。
    """
    import socket
    a = cfg.get("agent") or {}
    return {
        "type": "hello",
        "agent": a.get("name") or "",
        "secret": a.get("secret") or "",
        "version": __version__,
        "ops": sorted(ops.OPS),
        "install_id": str(a.get("install_id") or "").strip(),
        "hostname": socket.gethostname(),
        # ★ B3：配置里的账套名。**实测值不在这里** —— 握手时可能还没连过库，
        #   在这里连一次就等于把阻塞放进握手。实测账套由随后第一帧 status 报上去
        #   （agent_daemon 每 30 秒 + 账套热切后立刻报）。
        "database": str((cfg.get("sql") or {}).get("database") or "").strip(),
        # ★ Step 2：把自己被允许读哪几本账一起报上去。云端那份（merchants[码].databases）
        #   是平台登记，这一份是客户机实际放行的 —— 两边不一致就是
        #   "后台说能读两本、客户机只放行一本"，装机现场最容易撞，所以要在后台看得见。
        "allowed_databases": cfgmod.allowed_databases(cfg),
        # 后台调试台的说明与示例参数。只在握手发一次（约 8KB）——
        # 动作清单本身就是握手时自报的，说明跟着它一起走才不会与清单漂移。
        "op_docs": ops.catalog(),
    }


def run_console(agent):
    """stdin 每行一个 JSON 请求，stdout 每行一个 JSON 响应。"""
    for line in sys.stdin:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"ok": False, "error": {"message": "JSON 解析失败"}}, ensure_ascii=False))
            continue
        print(json.dumps(agent.handle_request(req), ensure_ascii=False), flush=True)


def run_ws(agent, url, name, secret, reconnect_sec, heartbeat_sec):
    """连接云端中转。请求/响应 JSON；断线自动重连；心跳保活。"""
    stage = os.path.join(tempfile.gettempdir(), "v3stage.txt")
    try:
        with open(stage, "a", encoding="utf-8") as fh:
            fh.write("%s ws-enter\n" % time.time())
    except Exception:
        pass
    try:
        import asyncio
        import websockets
    except ImportError as exc:
        raise SystemExit("WebSocket 模式需要 websockets 库：py -m pip install -r requirements.txt") from exc
    try:
        with open(stage, "a", encoding="utf-8") as fh:
            fh.write("%s ws-imports-ok\n" % time.time())
    except Exception:
        pass

    async def _once(ws):
        await ws.send(json.dumps(hello_payload(agent.cfg), ensure_ascii=False))
        # 源码模式也报一帧状态：账套名给配置值，实测值这里留空（不假装测过）。
        # 生产常驻（agent_daemon）才会把 SELECT DB_NAME() 的真值报上去。
        await ws.send(json.dumps(status_payload(agent, database=str(agent.database()),
                                                  verified=None), ensure_ascii=False))
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=heartbeat_sec * 2)
            except asyncio.TimeoutError:
                await ws.send(json.dumps({"type": "ping"}))
                continue
            if not msg:
                continue
            try:
                req = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if req.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))
                continue
            if req.get("type") == "request":
                resp = agent.handle_request(req)
                await ws.send(json.dumps({"type": "response", **resp}, ensure_ascii=False))

    async def _loop():
        while True:
            try:
                try:
                    with open(stage, "a", encoding="utf-8") as fh:
                        fh.write("%s connecting %s\n" % (time.time(), url))
                except Exception:
                    pass
                async with websockets.connect(url) as ws:
                    try:
                        with open(stage, "a", encoding="utf-8") as fh:
                            fh.write("%s connected\n" % time.time())
                    except Exception:
                        pass
                    log.info("已连接云端中转：%s", url)
                    await _once(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                text = str(exc)
                if "4002" in text:
                    # 名字已被另一台机器占用：再快也抢不进来，只会把日志刷满。
                    # 改成慢重连，并明确告诉人要去看 /admin 的「接管」。
                    log.warning("接入被拒(4002)：Agent 名 %s 已被另一台安装占用，60 秒后重试；"
                                "确认是本机换硬件/重装的话，请到云端后台点『接管』",
                                (agent.cfg.get("agent") or {}).get("name"))
                    await asyncio.sleep(60)
                    continue
                log.warning("连接断开(%s)，%.0f 秒后重连", exc, reconnect_sec)
            await asyncio.sleep(reconnect_sec + (sum((name or "").encode("utf-8")) % 16))

    asyncio.run(_loop())


def run_http(agent, base_url, name, secret, poll_sec=5, version=None, op_list=None):
    """HTTP 轮询模式：短连接 POST，公网链路不稳时比 WebSocket 可靠。
    base_url 例如 http://149.88.75.125:8000（自动拼接 /agent/poll、/agent/result）。
    """
    base = base_url.rstrip("/")
    # 直连，不经过环境代理（代理会造成 502/超时）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(path, payload, timeout=15):
        req = urllib.request.Request(
            base + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    body = {
        "agent": name,
        "secret": secret,
        "version": version or __version__,
        "ops": op_list or sorted(ops.OPS),
        # 与 WS 通道同一套指纹，否则走 HTTP 的机器就绕过了同名准入
        "install_id": hello_payload(agent.cfg)["install_id"],
        "hostname": hello_payload(agent.cfg)["hostname"],
        "database": hello_payload(agent.cfg)["database"],
        # ★ HTTP 通道必须带与 WS 通道一样的能力信息：
        #   少带 `allowed_databases`，云端就会把这台机器判成"不支持选账套的老包"，
        #   而它的现象是"只有配了 http 传输的那几家点不动第二本账"。
        "allowed_databases": hello_payload(agent.cfg)["allowed_databases"],
    }
    # 说明与示例只在**第一次**轮询时带上（约 8KB）：这条通道每几秒就轮询一次，
    # 每次都带就是把同一份文档反复推上网络。
    body["op_docs"] = hello_payload(agent.cfg)["op_docs"]
    while True:
        try:
            # 服务过的账套每次轮询现算（这条通道没有 30 秒状态帧，
            # 不带上云端就只能看见"默认那本"，两本账在这条传输上变成盲区）
            body["served_databases"] = agent.served_databases()
            data = post("/agent/poll", body)
            body.pop("op_docs", None)      # 只在第一帧带上，之后不重复推
            for req in data.get("requests") or []:
                resp = agent.handle_request(req)
                post("/agent/result", {"agent": name, "secret": secret, "response": resp})
            time.sleep(poll_sec)
        except Exception as exc:  # noqa: BLE001
            log.warning("HTTP 轮询异常：%s，%s 秒后重试", exc, poll_sec)
            time.sleep(poll_sec)
