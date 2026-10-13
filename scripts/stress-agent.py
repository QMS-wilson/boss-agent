# -*- coding: utf-8 -*-
"""Agent 并发压力测试（复刻生产 agent_daemon 的双池 + 队列 + 心跳模型）。

★ 为什么不用现成的 probe-concurrent-*.py：
  那些脚本直接调 `agent.dispatch()` —— 测的是**DB 吞吐**，
  绕过了真正会出问题的那一层：**asyncio 事件循环 + 两个线程池 + 队列**。
  生产上「报表卡住时列表还转不转」「心跳会不会被饿死」「排队排多久」
  都取决于这一层，必须如实复刻才有意义。

★ 本脚本复刻的正是 agent_daemon.run_daemon 里的模型：
    · fast_pool = ThreadPoolExecutor(max_workers=8)
    · heavy_pool = ThreadPoolExecutor(max_workers=2)
    · HEAVY_OPS 进重队列，其余进快队列
    · 每个池一个 worker，worker 自己回包（接收循环绝不 await 结果）
    · 心跳（type=ping）在事件循环内联响应，**不进池**

用法：
   PY313 scripts/stress-agent.py                    # 默认场景
   PY313 scripts/stress-agent.py --scenario all
   PY313 scripts/stress-agent.py --scenario burst --n 20
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# ★ 与 agent_daemon 逐字同源 —— 改那边就要改这边，否则测的不是生产行为
HEAVY_OPS = {
    "report.product.ranking",
    "report.customer.ranking",
    "report.profit.summary",
    "report.customer.product.detail",
    "report.customer.statement",
    "report.receivables.summary",
    "report.purchase.summary",
    "report.purchase.trend",
}
FAST_WORKERS = 8
HEAVY_WORKERS = 2


def pct(vals, p):
    if not vals:
        return 0
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


class Pool:
    """复刻生产 Agent 的并发执行体：两个池 + 两个队列 + worker 独立回包。"""

    def __init__(self, agent):
        self.agent = agent
        self.fast_pool = ThreadPoolExecutor(max_workers=FAST_WORKERS,
                                            thread_name_prefix="v3fast")
        self.heavy_pool = ThreadPoolExecutor(max_workers=HEAVY_WORKERS,
                                             thread_name_prefix="v3heavy")
        # 统计
        self.stats = {"sent": 0, "done": 0, "failed": 0}
        self.ping_ms = []          # 心跳往返（标称 = 本地内联，应恒 ≈0）
        self.results = {}          # reid -> (ms, ok, err)
        self.queue_wait = {}       # reid -> 排队等待 ms
        self._submit_t = {}
        self._done_evt = asyncio.Event()
        # 队列深度采样
        self.max_fast_q = 0
        self.max_heavy_q = 0

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.fast_q = asyncio.Queue()
        self.heavy_q = asyncio.Queue()
        self.tasks = [
            asyncio.create_task(self._worker(self.fast_q, self.fast_pool, "fast")),
            asyncio.create_task(self._worker(self.heavy_q, self.heavy_pool, "heavy")),
        ]

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self.fast_pool.shutdown(wait=False)
        self.heavy_pool.shutdown(wait=False)

    async def _worker(self, q, pool, tag):
        while True:
            reid, req = await q.get()
            self.queue_wait[reid] = (time.time() - self._submit_t[reid]) * 1000
            t0 = time.time()
            try:
                resp = await self.loop.run_in_executor(
                    pool, self.agent.handle_request, req)
            except Exception as exc:  # noqa: BLE001
                resp = {"ok": False, "error": {"code": "INTERNAL", "message": str(exc)}}
            ms = (time.time() - t0) * 1000
            ok = bool(resp.get("ok"))
            self.results[reid] = (ms, ok,
                                  "" if ok else (resp.get("error") or {}).get("message", "")[:120])
            self.stats["done"] += 1
            if not ok:
                self.stats["failed"] += 1
            if self.stats["done"] >= self.stats["sent"]:
                self._done_evt.set()

    async def submit(self, reid, op, params):
        req = {"type": "request", "id": reid, "op": op, "params": params}
        self.stats["sent"] += 1
        self._submit_t[reid] = time.time()
        if op in HEAVY_OPS:
            self.max_heavy_q = max(self.max_heavy_q, self.heavy_q.qsize() + 1)
            await self.heavy_q.put((reid, req))
        else:
            self.max_fast_q = max(self.max_fast_q, self.fast_q.qsize() + 1)
            await self.fast_q.put((reid, req))

    async def wait_all(self, timeout):
        try:
            await asyncio.wait_for(self._done_evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return self.stats["done"] >= self.stats["sent"]


async def heartbeat_watchdog(pool, stop_evt, interval=0.5):
    """模拟云端每 interval 秒发一次 ping，测「事件循环有没有被饿死」。

    ★ 这是本项目最容易出的事故：同步 SQL 卡住事件循环 ⇒ 收不到/发不出 ping
      ⇒ 云端判 keepalive 超时发 1011 ⇒ Agent 假死 + 前端 502。
      只要事件循环健康，这个循环的抖动就应该是个位数 ms。
    """
    jitters = []
    t_prev = time.time()
    while not stop_evt.is_set():
        await asyncio.sleep(interval)
        now = time.time()
        jitters.append((now - t_prev - interval) * 1000)
        t_prev = now
    return jitters


def summarize(label, pool, wall, extra=""):
    res = pool.results
    lat = [v[0] for v in res.values()]
    fails = [v for v in res.values() if not v[1]]
    print("  ┌─ %s" % label)
    print("  │  请求数 %d  墙钟 %dms  失败 %d" % (len(res), wall, len(fails)))
    if lat:
        print("  │  延迟 ms：min=%d  p50=%d  p95=%d  max=%d"
              % (min(lat), pct(lat, 50), pct(lat, 95), max(lat)))
    qw = [v for v in pool.queue_wait.values()]
    if qw:
        print("  │  排队 ms：p50=%d  p95=%d  max=%d"
              % (pct(qw, 50), pct(qw, 95), max(qw)))
    if extra:
        print("  │  %s" % extra)
    if fails:
        for v in fails[:3]:
            print("  │  ✗ %s" % v[2])
    print("  └─")


# ---------------------------------------------------------------------------

SCENARIOS = {}


def scenario(fn):
    SCENARIOS[fn.__name__.replace("sc_", "")] = fn
    return fn


@scenario
async def sc_mixed(agent, n):
    """混合负载：模拟 4~5 个 App 同时用（列表/详情/报表/建单参数查询穿插）。

    这是**最贴近真实**的场景：老板在开单、另一个人在翻列表、
    第三个人在点报表 —— 三者的 op 类型完全不同。
    """
    pool = Pool(agent)
    await pool.start()
    stop = asyncio.Event()
    hb_task = asyncio.create_task(heartbeat_watchdog(pool, stop))

    RANGE = {"date_from": "2025-10-01", "date_to": "2026-10-11"}
    # 每个 App 一轮做这些事（op 名取自 `ops.OPS` 真身，别凭印象写）
    seq = [
        ("bill.list", {"limit": 20}),
        ("customer.search", {"limit": 20}),
        ("product.search", {"limit": 20}),
        ("report.product.ranking", dict(RANGE, limit=20)),
        ("report.receivables.summary", {}),
        ("bill.list", {"state": "pending", "limit": 20}),
        ("system.dashboard", {}),
        ("report.sales.summary", dict(RANGE)),
        ("product.salesHistory", {"hpbh": "12345", "limit": 10}),
    ]
    reid = 0
    tasks = []
    t0 = time.time()
    for app in range(n):
        for step in range(len(seq)):
            op, params = seq[step]
            tasks.append(pool.submit("m%d-%d" % (app, step), op, dict(params)))
            reid += 1
            await asyncio.sleep(0.02)   # 轻微错开，模拟真人点击节奏
    await asyncio.gather(*tasks)
    ok = await pool.wait_all(timeout=300)
    wall = int((time.time() - t0) * 1000)
    stop.set()
    jit = await hb_task
    await pool.stop()

    summarize("混合负载  N=%d 个 App × %d 步（共 %d 请求）" % (n, len(seq), reid),
              pool, wall,
              "队列峰值 快=%d 重=%d" % (pool.max_fast_q, pool.max_heavy_q))
    print("     心跳抖动 ms：p50=%d p95=%d max=%d  （>500ms 说明事件循环被饿死）"
          % (pct(jit, 50), pct(jit, 95), max(jit)))
    if not ok:
        print("     ⚠ 有请求超时未完成")
    return {"wall": wall, "hb_max": max(jit), "stats": dict(pool.stats),
            "lat_p95": pct([v[0] for v in pool.results.values()], 95)}


@scenario
async def sc_heavy_storm(agent, n):
    """报表风暴：N 个人同时打重 op —— 验证重池限流是否真的保护了本地库，
    以及快 op 是否**完全不受影响**（这是双池设计的核心承诺）。
    """
    pool = Pool(agent)
    await pool.start()
    stop = asyncio.Event()
    hb_task = asyncio.create_task(heartbeat_watchdog(pool, stop))

    RANGE = {"date_from": "2025-10-01", "date_to": "2026-10-11"}
    tasks = []
    for i in range(n):
        tasks.append(pool.submit("h%d" % i, "report.product.ranking",
                                 dict(RANGE, limit=20)))
    await asyncio.gather(*tasks)

    # 报表洪峰期间，穿插 3 个快 op —— 它们应该几乎不被拖延
    await asyncio.sleep(0.3)
    t_fast = time.time()
    fast_tasks = []
    for i in range(3):
        fast_tasks.append(pool.submit("f%d" % i, "bill.list", {"limit": 20}))
    await asyncio.gather(*fast_tasks)
    # 等快 op 完成
    while any(("f%d" % i) not in pool.results for i in range(3)):
        await asyncio.sleep(0.05)
    fast_ms = (time.time() - t_fast) * 1000

    ok = await pool.wait_all(timeout=600)
    stop.set()
    jit = await hb_task
    await pool.stop()

    summarize("报表风暴  重 op × %d（快池穿插 3 条轻 op）" % n, pool,
              int(sum(v[0] for v in pool.results.values())),
              "队列峰值 快=%d 重=%d" % (pool.max_fast_q, pool.max_heavy_q))
    print("     ★ 报表洪峰中 3 条轻 op 的墙钟 = %dms（应 ≈ 单发耗时，不被报表拖住）"
          % fast_ms)
    print("     心跳抖动 ms：p95=%d max=%d" % (pct(jit, 95), max(jit)))
    if not ok:
        print("     ⚠ 有请求超时未完成")
    return {"fast_during_storm_ms": fast_ms, "hb_max": max(jit)}


@scenario
async def sc_burst(agent, n):
    """瞬时脉冲：N 条相同轻 op 同时打进来（模拟多人同时下拉刷新/切页）。

    用来找**队列 + 线程池**这一层的拐点：N 超过快池 8 之后，
    多出来的人排队，延迟怎么涨。
    """
    pool = Pool(agent)
    await pool.start()
    stop = asyncio.Event()
    hb_task = asyncio.create_task(heartbeat_watchdog(pool, stop))

    t0 = time.time()
    tasks = [pool.submit("b%d" % i, "bill.list", {"limit": 20}) for i in range(n)]
    await asyncio.gather(*tasks)
    ok = await pool.wait_all(timeout=300)
    wall = int((time.time() - t0) * 1000)
    stop.set()
    jit = await hb_task
    await pool.stop()

    summarize("瞬时脉冲  bill.list × %d（同一时刻）" % n, pool, wall,
              "队列峰值 快=%d" % pool.max_fast_q)
    print("     心跳抖动 ms：p95=%d max=%d" % (pct(jit, 95), max(jit)))
    if not ok:
        print("     ⚠ 有请求超时未完成")
    return {"wall": wall, "hb_max": max(jit),
            "lat_p95": pct([v[0] for v in pool.results.values()], 95)}


@scenario
async def sc_mixed_read_write(agent, n):
    """读写混合：一半人建单（走 applock 取号，持锁几百 ms），一半人查询。

    ★ 验证 2026-10-11 引入的 applock 取号在**并发查询同时发生**时
      会不会互相影响（取号锁按账套+单据类型命名，理论上只挡同类建单）。
    """
    pool = Pool(agent)
    await pool.start()
    stop = asyncio.Event()
    hb_task = asyncio.create_task(heartbeat_watchdog(pool, stop))

    # 先拿两个客户编号
    r = agent.dispatch("customer.search", {"limit": 2})
    custs = [c["code"] for c in ((r.get("data") or {}).get("items") or [])][:2] or ["K00001"]

    tasks = []
    for i in range(n):
        if i % 2 == 0:
            # 查询类
            tasks.append(pool.submit("r%d" % i, "bill.list", {"limit": 20}))
        else:
            # 建单类（会取号，持 applock）
            tasks.append(pool.submit("w%d" % i, "salesbill.create", {
                "customer": custs[i % len(custs)],
                # ★ 单位必须是货品真实单位，否则 Agent 直接拒绝（"没有这个单位"）
                "lines": [{"hpbh": "12345", "unit": "卷", "qty": 1, "price": 10, "remark": ""}],
                "ndate": "2026-10-12",
                "note": "stress-test",
            }))
    await asyncio.gather(*tasks)
    ok = await pool.wait_all(timeout=300)
    stop.set()
    jit = await hb_task
    await pool.stop()

    summarize("读写混合  查询 %d + 建单 %d" % ((n + 1) // 2, n // 2), pool,
              int(sum(v[0] for v in pool.results.values())))
    print("     心跳抖动 ms：p95=%d max=%d" % (pct(jit, 95), max(jit)))
    if not ok:
        print("     ⚠ 有请求超时未完成")
    return {"hb_max": max(jit), "stats": dict(pool.stats)}


async def main_async(args):
    from v3agent.agent import LocalAgent
    cfg_path = args.config or os.path.join(ROOT, "config.json")
    c = json.loads(open(cfg_path, encoding="utf-8").read())
    agent = LocalAgent(c)

    print("=" * 80)
    print("  Agent 并发压力测试    账套=%s   快池=%d 重池=%d"
          % (c["sql"]["database"], FAST_WORKERS, HEAVY_WORKERS))
    print("  配置=%s" % cfg_path)
    print("=" * 80)

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    out = {}
    for nm in names:
        if nm not in SCENARIOS:
            print("未知场景：%s（可选 %s）" % (nm, " / ".join(SCENARIOS)))
            return 2
        print()
        out[nm] = await SCENARIOS[nm](agent, args.n)
    print("\n" + "=" * 80)
    print("  汇总")
    print("=" * 80)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="mixed",
                    help="mixed / heavy_storm / burst / mixed_read_write / all")
    ap.add_argument("--n", type=int, default=5, help="并发规模")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
