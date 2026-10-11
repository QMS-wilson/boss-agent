# -*- coding: utf-8 -*-
"""建单幂等 —— 销售/采购共用的「一次提交只落一次库」机制。

★★ 为什么要有这个模块（2026-09-22 第九轮需求）：
   用户报障：「在真机测试中，发现新增单据时，多次点击保存，会导致多次提交
   产生重复单据」。前端加了 `saving` 锁只能挡**连点**；挡不住这一类：
     请求已经到 Agent、单据已经插库，但**响应在回程丢失/超时** →
     用户看到失败提示 → 再点一次 → 库里两张单。
   ⇒ 由前端为「一次提交」生成 `client_token`，Agent 按它去重：
     同一 token 只插一次单，重复到达时直接把上次那张单回给他。

★ 为什么抽成独立模块（第十轮）：采购建单有**同样的暴露面**，而
   「销售一份、采购一份」的实现必然漂移（一边改了另一边忘）。
   共用一份是唯一稳的做法。

★ 为什么用「前端 token」而不是「按内容判重」（同供应商/客户+同时间+同行）：
   内容判重在业务上是**合法**的（同一客户同一时刻确实可能真的下两张一样的单）。
   误判的后果是「**少建一张单**」—— 用户不会去数单子，比重复单更难发现。
   只有前端明确声明「这是同一次提交的重试」时才去重，才不会误伤正常业务。

★ 为什么只放内存、不落库：客户的生产库不该为我们的幂等建表；
   TTL 30 分钟 + 上限 1000 条足以覆盖「手抖重试」的全部场景。
   Agent 进程重启会丢 —— 此时前端会随页面重载生成新 token，影响可忽略。

★★ 并发安全（同一 token 的两个请求**同时**到达）：
   两个线程都会判「没建过」⇒ 各插一张单 ⇒ 幂等白做。
   所以采用「**先原子认领占位、后回填单号**」三步：
     begin()  → 'go'     本次负责建单（占位已就绪）
              → 'replay' 同一 token 已建过，直接回上次那张单
              → 撞到「正在建」的占位则短轮询等对方出结果（建单 ~100ms）
     finish() 建单成功、**commit 之后**回填单号
     abort()  建单失败时释放占位
   ★ 三条铁律，缺一就会出现难查的故障：
     · finish 必须在 commit **之后** —— 回滚了却标记成功 ⇒ 用户重试反而不建单；
     · abort 必须真的执行 —— 否则用户重试被自己的占位挡住（最需要重试的时刻）；
     · 轮询必须**出锁再 sleep** —— 持锁睡会死锁。
"""

import threading
import time

TTL_SEC = 1800        # 幂等记录有效期（30 分钟）
STALE_SEC = 120       # 占位（正在建单）多久算僵死，可被抢占重做
MAX_ENTRIES = 1000    # 条数上限，防内存无界增长

_STATE = {}           # {(账套, token): {"bill_no","ts"}}；bill_no=None 表示"正在建"
_LOCK = threading.Lock()


def _key(database, token):
    """幂等键必须带**账套**。

    ★ 为什么（2026-09-26 Step 2）：一台主机两本账、请求级选库之后，
      同一个前端 token 可能先打到 A 账再打到 B 账（用户在两本之间来回切、
      或离线草稿被重放到另一本）。只按 token 去重的话，第二本会直接"复用"
      第一本的单号 —— 那是**另一本账里的单**，比重复单严重得多。
    ★ database 传空（单账套客户与所有老调用点）⇒ 键是 `("", token)`，与改造前等价。
    """
    return (str(database or "").strip(), str(token or "").strip())


def db_of(cfg):
    """这条请求实际落在哪本账。

    handler 收到的是**路由之后的 cfg 副本**（见 agent.LocalAgent.cfg_for_database），
    所以 `cfg.sql.database` 就是答案本身，不需要再问连接。
    """
    return str(((cfg or {}).get("sql") or {}).get("database") or "").strip()


def _gc(now):
    """惰性清理：过期项 + 超上限时淘汰最旧的（调用方必须已持有 _LOCK）。"""
    for k in [k for k, v in _STATE.items() if now - v["ts"] > TTL_SEC]:
        _STATE.pop(k, None)
    while len(_STATE) >= MAX_ENTRIES and _STATE:
        _STATE.pop(next(iter(_STATE)), None)


def begin(token, database=""):
    """插入前原子认领 token（在**同一本账**的命名空间内）。

    返回 (state, bill_no)：
      ('go',     None)    → 本次是第一次，可以建单（占位已就绪）
      ('replay', billNo)  → 同一 token 在这本账已经建过，直接回这张单
    超时（对方建单卡住）抛 RuntimeError —— 让前端看到「正在处理中」而不是重复建单。
    """
    token = str(token or "").strip()
    if not token:
        return "go", None
    key = _key(database, token)
    deadline = time.time() + 5.0
    while True:
        with _LOCK:
            now = time.time()
            rec = _STATE.get(key)
            if rec is None:
                _STATE[key] = {"bill_no": None, "ts": now}
                _gc(now)
                return "go", None
            if rec.get("bill_no"):
                if now - rec["ts"] <= TTL_SEC:
                    return "replay", rec["bill_no"]
                _STATE.pop(key, None)         # 过期记录，按新提交处理
                continue
            if now - rec["ts"] > STALE_SEC:
                # 占位僵死（上一个请求中途崩了）→ 抢过来重做
                _STATE[key] = {"bill_no": None, "ts": now}
                return "go", None
            if now >= deadline:
                raise RuntimeError("同一笔提交正在处理中，请稍候再试")
        time.sleep(0.05)                      # ★ 必须出锁再睡，否则死锁


def finish(token, bill_no, database=""):
    """建单成功后回填单号（此后同 token + 同账套的请求一律 replay）。"""
    token = str(token or "").strip()
    if not token:
        return
    with _LOCK:
        _STATE[_key(database, token)] = {"bill_no": bill_no, "ts": time.time()}


def abort(token, database=""):
    """建单失败时释放占位 —— 否则用户重试会一直等到「正在处理中」超时。"""
    token = str(token or "").strip()
    if not token:
        return
    with _LOCK:
        rec = _STATE.get(_key(database, token))
        if rec and not rec.get("bill_no"):
            _STATE.pop(_key(database, token), None)


def reset():
    """仅测试用：清空幂等表。"""
    with _LOCK:
        _STATE.clear()


def peek(token, database=""):
    """仅测试/诊断用：看某 token 在当前这本账里的记录。"""
    with _LOCK:
        rec = _STATE.get(_key(database, token))
        return dict(rec) if rec else None
