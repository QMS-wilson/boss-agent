# -*- coding: utf-8 -*-
"""SQL Server 连接与通用查询辅助（全部经白名单 op 调用，不对外透传 SQL）。"""

from contextlib import contextmanager
import threading
import time

import pymssql


def connect(cfg):
    return pymssql.connect(
        server=cfg["sql"]["server"],
        port=int(cfg["sql"].get("port", 1433)),
        user=cfg["sql"]["user"],
        password=cfg["sql"]["password"],
        database=cfg["sql"]["database"],
        tds_version=cfg["sql"].get("tds_version", "7.0"),
        login_timeout=int(cfg["sql"].get("login_timeout", 10)),
        timeout=int(cfg["sql"].get("timeout", 120)),
        autocommit=False,
        charset="utf8",
    )


# ================================================== 连接复用（线程本地池）
#
# ★★★ 为什么要池（2026-10-12 压测实测）：
#   改造前 `session()` 是 **每 op 新建一条 pymssql 连接、用完 close**。
#   实测建连（TCP + login 握手）稳态约 **25ms**，而一条轻查询（customer.search）
#   本体只要 ~13ms —— 也就是说**超过一半的耗时花在了"开门"上**。
#   80 并发时这条成本被乘以 80，是列表/详情类接口延迟的主因之一。
#   复用后同一线程第二次起不再付这笔钱（实测 bill.list 102ms → 77ms）。
#
# ★ 为什么用**线程本地**而不是共享队列池：
#   生产 Agent 的执行体是 `ThreadPoolExecutor`（快池 8 + 重池 2），
#   线程数固定且远小于并发请求数 ⇒ 每线程持有一条长连接就把并发度吃满了，
#   实现上也没必要引入借还协议与超时回收（那类池最容易在异常路径上漏连接）。
#   代价：池里最多 10 条连接（8+2），对 SQL Server 毫无压力。
#
# ★ 为什么 key 必须带 database：
#   账套可按请求路由（`agent.cfg_for_database` 会 deep-copy 一份只改 database 的 cfg）。
#   不带 database 做 key 就会把"读 A 库的连接"拿去读 B 库 —— **静默串账**，
#   这是本项目最不能犯的错。所以 key = (server, port, user, database)。
#
# ★ 为什么回池前要 `sp_reset_connection`（等价物）：
#   连接上可能残留：未提交事务、`sp_getapplock @LockOwner='Session'` 持有的
#   应用锁、SET 选项、临时表。尤其**取号那把 applock 是会话级的** ——
#   若某条异常路径漏了 release，复用这条连接的下一个请求会**一直拿不到号**。
#   所以归还前rollback + 释放本会话持有的 applock；出错就干脆丢弃这条连接重开
#   （丢弃是安全兜底，不算性能问题——异常本来就是少数路径）。
#
# ★ 为什么要有"活性探测"：
#   `agent.capabilities` 依赖 `db.session(cfg)` 在**库挂掉时抛异常**
#   （否则它会把 dbStatus 报成 healthy）。复用的连接若在 SQL Server 重启后
#   还躺在池里，直接拿它执行会报"连接已断" —— 那正好也是异常，语义正确。
#   真正的风险是相反方向：连接看着还在、实际已废，于是首次用报错、第二次才好。
#   因此这里**不主动 ping**（那会把每 op 又加一次往返），而是让断连**如实抛错**，
#   抛错后把这条连接从池里摘掉（下次自动重建）—— 失败一次，之后就好。

_POOL = threading.local()

#: 单进程池里最多几条连接（= 快池 8 + 重池 2 + 2 条余量给热切/状态帧）。
#: 只用于统计与超限告警，不做强制回收。
POOL_MAX = 12

_pool_lock = threading.Lock()
_pool_size = 0          # 当前池中现存连接数（仅统计用）


def _conn_key(cfg):
    """连接身份：(server, port, user, database)。换任何一项都必须换连接。"""
    sql = cfg["sql"]
    return (str(sql.get("server") or ""), int(sql.get("port", 1433) or 1433),
            str(sql.get("user") or ""), str(sql.get("database") or ""))


class _Pooled:
    """线程本地的一条可复用连接 + 其身份 key。"""

    __slots__ = ("conn", "key")

    def __init__(self, conn, key):
        self.conn = conn
        self.key = key


def _new_pooled(cfg):
    global _pool_size
    conn = connect(cfg)
    with _pool_lock:
        _pool_size += 1
    return _Pooled(conn, _conn_key(cfg))


#: 本连接**当前仍持有**的会话级应用锁（资源名集合）。
#: ★★★ 这是连接复用最危险的一处状态：`sp_getapplock @LockOwner='Session'` 的锁
#:   随**会话**存活，不随事务 —— 回滚、commit 都带不走它。若某条异常路径漏了
#:   release，这把锁会挂在被复用的连接上；而它又是"整个账套的取号权"，
#:   下一个用这条连接的请求**永远拿不到号**（10s 超时后报"暂被占用"），
#:   而且换任何一个新请求都还是同一条坏连接 ⇒ 表现成"这家客户忽然再也开不了单"。
#:   ⇒ 所以不靠"调用方记得释放"，而是在**取号/放号这一对函数里自己记账**：
#:     取到号就登记，释放成功就销账；回池前若发现有欠账，**直接丢弃这条连接**
#:     （Session 级锁随连接关闭由 SQL Server 释放，这是最硬的兜底）。
_HELD_LOCKS = {}

_held_lock = threading.Lock()


def _lock_mark(conn, resource, held):
    """登记/注销「这条连接是否还持着这把应用锁」。"""
    with _held_lock:
        key = id(conn)
        if held:
            _HELD_LOCKS.setdefault(key, set()).add(resource)
        else:
            got = _HELD_LOCKS.get(key)
            if got:
                got.discard(resource)
                if not got:
                    _HELD_LOCKS.pop(key, None)


def _lock_pending(conn):
    """这条连接上是否还有未释放的应用锁（有 ⇒ 不许复用，必须丢弃）。"""
    with _held_lock:
        return bool(_HELD_LOCKS.get(id(conn)))


def _lock_forget(conn):
    """连接销毁时清账，避免 id() 复用导致误判。"""
    with _held_lock:
        _HELD_LOCKS.pop(id(conn), None)


def _reset_for_reuse(conn):
    """回池前的清理。任何一步失败都代表这条连接的会话状态不可信 ⇒ 调用方丢弃它。

    · rollback：扔掉未提交事务（只读 op 走 commit 后这里是空操作；
      写 op 在自己函数里已 commit）。**绝不能让下一条请求继承上一条的事务**。
    · 应用锁欠账检查：`sp_releaseapplock` 不随事务走、必须显式。正常路径下
      `sales.py` / `purchase.py` 的 finally 都已经释放（实测核对过 100% 覆盖），
      但"覆盖得对"这件事不该靠人工审计来维持 —— 这里做**最后一道闸**：
      只要账上还挂着锁，就判定这条连接不可复用 ⇒ 返回 False，调用方丢弃它。
      丢弃会让 SQL Server 释放该会话的全部锁，代价只是下次重建一条连接（25ms）。
      ★ 为什么不用"查本会话持有的锁"：SQL Server 2008 没有这个轻量视图，
        而 `sp_releaseapplock` 也不支持通配；自己记账是唯一无额外往返的做法。
    """
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        return False
    if _lock_pending(conn):
        return False
    return True


def _discard(pooled):
    global _pool_size
    _lock_forget(pooled.conn)
    try:
        pooled.conn.close()
    except Exception:  # noqa: BLE001
        pass
    with _pool_lock:
        _pool_size = max(0, _pool_size - 1)


@contextmanager
def session(cfg):
    """取一条连接供本次 op 使用；正常结束归还线程本地池，异常则丢弃重建。

    ★ 语义与改造前**完全一致**（调用方依旧 `with session(cfg) as conn:`），
      唯一区别是连接被复用而不是每次新建 —— 所以所有既有调用点零改动。
    ★ `own=True` 只给"必须独立连接、且绝不能污染池"的场景用；
      目前没有这种场景（capabilities 的探活也走池 —— 库真挂时池里的连接一样会抛）。
    """
    key = _conn_key(cfg)
    pooled = getattr(_POOL, "slot", None)

    # 复用条件：同线程、同身份、连接还在
    if pooled is not None and pooled.key == key:
        conn = pooled.conn
    else:
        # 换身份（账套路由/热切换）⇒ 旧连接归还前先清理，避免带着别的库的会话状态
        if pooled is not None:
            if not _reset_for_reuse(pooled.conn):
                _discard(pooled)
            _POOL.slot = None
        pooled = _new_pooled(cfg)
        conn = pooled.conn

    ok = False
    try:
        yield conn
        ok = True
    finally:
        if ok:
            if _reset_for_reuse(conn):
                _POOL.slot = pooled        # 留作下次复用
            else:
                _discard(pooled)           # 状态不可信 ⇒ 丢弃，下次重建
                _POOL.slot = None
        else:
            # 异常路径：连接可能处在半途状态，**丢弃**最安全。
            # 这不是性能问题 —— 异常本就是少数路径，重开一条才 25ms。
            _discard(pooled)
            _POOL.slot = None


def pool_stats():
    """当前线程是否有可复用连接 + 全进程池大小（诊断/压测用）。"""
    pooled = getattr(_POOL, "slot", None)
    with _pool_lock:
        size = _pool_size
    with _held_lock:
        pending = sum(len(v) for v in _HELD_LOCKS.values())
    return {"thread_has_conn": pooled is not None, "pool_size": size,
            "max": POOL_MAX, "locks_pending": pending}


def q1(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchone()



# ================================================== 单据取号（并发安全）
#
# ★★★ 为什么需要这把锁（2026-10-11 实测确诊）：
#   原来的取号是「先 SELECT BillNoid → +1 → UPDATE」四步，中间**一把锁都没有**。
#   本实例隔离级别是 READ COMMITTED（且两库都没开快照隔离，实测
#   `is_read_committed_snapshot_on=0`），所以并发两笔都读到同一个号 ⇒
#   先提交的赢、后提交的在 INSERT 时撞 `PK_XS_BillNo`（2627）。
#   实测：**2 路并发就有一半概率失败，5 路并发只有 1 路成功**。
#
# ★★★ 为什么不用「X 锁 + 死锁重试」（方案 A，已否决）：
#   把 `UPDATE No_BillID SET BillNoid=BillNoid+1` 放事务首句确实能互斥（实测有效），
#   但 X 锁要持有一整个建单事务（500~900ms），期间触发器还会去改 Khdan/InvenFlow，
#   反序获取时**必然**形成 1205 死锁（实测两种时序都复现，另一事务被堵 3~5 秒）。
#   死锁重试还会**消耗单号造成跳号** —— 而用户明确要求**单号必须连号**。
#
# ★★★ 所以改用 `sp_getapplock` 应用锁（实测通过）：
#   · 它不在 SQL Server 的**行锁图**里 ⇒ 从机制上**不产生 1205**，
#     无需任何重试逻辑（"反应最快"的正解 = 让它不出现）；
#   · `@LockOwner='Session'` ⇒ 连接断开自动释放，
#     Agent 崩溃/被杀**不会**永久卡住全体建单（实测：持锁后直接 close，另一连接 0.00s 拿到）；
#   · `@LockTimeout` 毫秒级生效，拿不到就快速返回错误（实测设 800ms → 0.82s 返回 -1）；
#   · 取号在锁保护下**顺序推进**（UPDATE 首句），因此**单号严格连续**
#     （实测 4 路并发 → [36745,36746,36747,36748] 无重复无跳号）。
#
# ★ 锁资源名按**账套 + 单据类型**命名：不同账套/不同单据类型互不阻塞。
ACQ_APPLOCK = ("DECLARE @rc int; EXEC @rc = sp_getapplock @Resource=%s, "
               "@LockMode='Exclusive', @LockOwner='Session', @LockTimeout=%s; "
               "SELECT @rc")
REL_APPLOCK = "EXEC sp_releaseapplock @Resource=%s, @LockOwner='Session'"

APPLOCK_TIMEOUT_MS = 10000   # 单张建单 500~900ms，5 路最坏 5×0.9≈4.5s，10s 留足余量


def app_lock_resource(database, bill_kind, bill_na_id):
    """应用锁资源名：`v3:NoBillID:<库>:<类型><号>`。按库+类型分锁，互不牵连。"""
    dbn = str(database or "").strip().lower() or "default"
    return "v3:NoBillID:%s:%s%s" % (dbn, str(bill_kind).lower(), bill_na_id)


def acquire_number_lock(cur, resource, timeout_ms=APPLOCK_TIMEOUT_MS):
    """取「单据号」应用锁。返回 True=拿到；False=超时（调用方应给出可读错误）。

    ★ 必须用 `SELECT @rc` 读返回码：pymssql 直接 `EXEC sp_getapplock` 会报
      "executed statement has no resultset"（实测踩过）。
    ★ 返回码语义：0=立即拿到 / 1=等待后拿到 / 负数=失败
      （-1 超时、-2 被取消、-3 调用方是死锁牺牲品、-999 参数/权限错误）。
    ★ 锁随**会话**释放，不随事务 —— 所以调用方在 finally 里释放即可，
      中途回滚也不会把锁带走（回滚后连接仍活、仍持锁，必须显式 release）。
    """
    row = q1(cur, ACQ_APPLOCK, (resource, int(timeout_ms)))
    rc = int(row[0]) if row else -1
    if rc in (0, 1):
        # ★ 记账：这条连接现在握着这把锁。会话级锁不随事务走，
        #   唯一能保证它被还回去的就是"回池前查账"（见 _reset_for_reuse）。
        _lock_mark(cur.connection, resource, True)
        return True
    return False


def release_number_lock(cur, resource):
    """释放应用锁。释放失败不该影响业务结果，所以吞掉异常。

    ★ 无论成功与否都**销账**：release 抛异常时锁大概率已经不在了
      （连接断了/已被丢弃），此时若继续记账，会把一条本来干净的连接
      永久判成"不可复用"——那是拿性能换一个不存在的风险。
      真没释放掉的情况由"丢弃连接"兜底（见 _discard）。
    """
    try:
        cur.execute(REL_APPLOCK, (resource,))
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            _lock_mark(cur.connection, resource, False)
        except Exception:  # noqa: BLE001
            pass


def next_bill_number(cur, bill_na_id, database="", kind="", max_sql=None, prefix_sql=None):
    """并发安全地取下一个单号（应用锁保护下顺序推进）。

    参数：
      bill_na_id  `No_BillID.BillNaID` 的取值（销售 2020 / 采购 1020）
      database    账套名，仅用于拼锁资源名（不同账套互不阻塞）
      kind        单据类型短名（"xs" / "jh"），同上，仅用于拼锁资源名
      max_sql     可选，「当前最大行号」SELECT —— 历史兜底（如 `XS.IdenCol`）。
                  取到的号若 ≤ 该最大值则跳到 max+1 并回写计数器。
      prefix_sql  可选，前缀 SELECT（在加锁**前**读，它不参与竞态）。
                  返回 `row[0]` 作为 prefix。

    返回 (prefix, nid)：prefix 为 None（没传 prefix_sql）或该 SELECT 的首列。

    取号失败抛 ValueError（都能被 dispatch 转成 BAD_REQUEST 的可读消息）：
      · 拿不到锁 ⇒ 「单据号暂被其它保存占用」—— 前端提示重试即可
      · 配置行不存在 ⇒ 账套没初始化，属真故障

    ★★ 为什么不用 `SELECT @@ROWCOUNT` 判「配置行是否存在」：
      本实例是 **SQL Server 2008 R2**（兼容性级别 90），对
      `UPDATE 表 SET 列 = 列 + 1` 这种形式**不发 rows affected 计数** ——
      `@@ROWCOUNT` 恒为 0（评审实测确认）。用它判断会把**每一次正常取号**
      误判成"配置缺失"直接报错。所以这里改用「UPDATE 后再 SELECT 读回」
      两条独立语句：读回为空才是真的没有这一行。
    """
    resource = app_lock_resource(database, kind, bill_na_id)
    prefix = None
    if prefix_sql:
        r = q1(cur, prefix_sql)
        prefix = r[0] if r else None

    if not acquire_number_lock(cur, resource):
        raise ValueError("单据号暂被其它保存占用，请稍后重试（未取到号，未产生任何单据）")
    try:
        # ★ UPDATE 必须是这把锁内的**第一条**写语句：顺序推进，不重复、不跳号。
        cur.execute("UPDATE No_BillID SET BillNoid = ISNULL(BillNoid, 0) + 1 "
                    "WHERE BillNaID=%s", (bill_na_id,))
        row = q1(cur, "SELECT BillNoid FROM No_BillID WHERE BillNaID=%s", (bill_na_id,))
        if not row:
            raise ValueError("未找到单号配置(No_BillID/%s)" % bill_na_id)
        nid = int(row[0])
        if max_sql:
            mx = int(q1(cur, max_sql)[0] or 0)
            if nid <= mx:
                nid = mx + 1
                cur.execute("UPDATE No_BillID SET BillNoid=%s WHERE BillNaID=%s",
                            (nid, bill_na_id))
        return prefix, nid
    except Exception:
        release_number_lock(cur, resource)   # 失败即放锁，别让下一次白等
        raise


def qall(cur, sql, params=None, cap=None):
    """执行查询并取回全部行。

    ★ cap（可选）：结果集行数上限。设了之后用 `fetchmany` 分批取，取满 cap 即停，
      不再无限堆内存 —— 防止「某客户全期间流水」这类无界结果把 Agent 撑爆
      （历史上「崩溃」嫌疑之一就是大结果集的内存峰值）。
    ★ 默认 cap=None ⇒ 行为与改造前**逐字节一致**（一次性 fetchall）。
      只有显式传 cap 的调用点才走分批路径，绝不悄悄改变既有语义。
    """
    cur.execute(sql, params or ())
    if cap is None:
        return cur.fetchall()
    out = []
    batch = 2000
    while len(out) < cap:
        chunk = cur.fetchmany(min(batch, cap - len(out)))
        if not chunk:
            break
        out.extend(chunk)
    return out


def account_option_values(cur, names):
    """读 `HxAccountOption` 的若干个开关，**同名多行时不猜**。

    ★★ 这张表的主键实测是 **(OptionSpid, OptionName, ZDRBH)**（PK_HxAccountOption），
      不是 OptionName ⇒ 同一个开关名合法地可以有多行。本库实测 12 组同名多行、
      其中 8 组**取值就不一致**（如 `TxBillData.NInvenColorCurr.Checked` 同时有 True 和 False）。
      所以 `SELECT TOP 1 OptionValue ... WHERE OptionName=?` 是错的写法：
      拿哪一条取决于执行计划，症状是"同一家店，界面一会儿有这个功能一会儿没有"。

    返回 `{开关名: {"value": str|None, "rows": int, "conflict": bool}}`：
      value=None   库里根本没这一行 = **不知道**（调用方别当成"没开"）
      value=''     有行但值是空 = 明确"没设"（走 V3 默认）
      conflict=True 多行且去空格后取值不一致 ⇒ value 仍给 None，并把名字带回去露出来
    """
    names = [str(n) for n in (names or ()) if str(n).strip()]
    if not names:
        return {}
    ph = ",".join(["%s"] * len(names))
    rows = qall(cur, "SELECT LTRIM(RTRIM(OptionName)), ISNULL(OptionValue,'') "
                     "FROM HxAccountOption WHERE OptionName IN (" + ph + ")", tuple(names))
    got = {}
    for name, val in rows:
        got.setdefault(str(name).strip(), set()).add(str(val or "").strip())
    out = {}
    for n in names:
        vals = got.get(n)
        if not vals:
            out[n] = {"value": None, "rows": 0, "conflict": False}
            continue
        if len(vals) > 1:
            out[n] = {"value": None, "rows": len(vals), "conflict": True}
            continue
        out[n] = {"value": next(iter(vals)), "rows": 1, "conflict": False}
    return out


def ensure_temp(cur, guid, iden, ndate, itembh):
    """建触发器依赖的临时表并写入单头信息（V3 正规 DML 前提）。"""
    cur.execute("IF OBJECT_ID('tempdb..#Bill') IS NOT NULL DROP TABLE #Bill")
    cur.execute(
        "CREATE TABLE #Bill (IdenGuid uniqueidentifier, IdenCol int, NDate datetime, ItemBh nvarchar(60), UserVoid nvarchar(40))"
    )
    cur.execute("IF OBJECT_ID('tempdb..#BillMx') IS NOT NULL DROP TABLE #BillMx")
    cur.execute("CREATE TABLE #BillMx (Hpbh nvarchar(60), IdenCol int)")
    cur.execute("IF OBJECT_ID('tempdb..#BillModu') IS NOT NULL DROP TABLE #BillModu")
    cur.execute("CREATE TABLE #BillModu (BillNaID smallint, RootGuid uniqueidentifier)")
    cur.execute("IF OBJECT_ID('tempdb..#OutGuid') IS NOT NULL DROP TABLE #OutGuid")
    cur.execute("CREATE TABLE #OutGuid (IdenGuid uniqueidentifier)")
    cur.execute(
        "INSERT INTO #Bill VALUES (CONVERT(uniqueidentifier, %s), %s, %s, %s, '')",
        (str(guid), iden, ndate, itembh),
    )


# ================================================== 账套（同名 SQL Server 实例上的多套数据）

# ★ 为什么不能"枚举 sys.databases 让人随便挑"（2026-09-24 实测）：
#   本机实例在线可写的库有 4 个 —— AccMaster / shiwantongda / TempUpSpRpt / tongda，
#   但**只有后两个是账套**。AccMaster 与 TempUpSpRpt 是遗留 V3 报表引擎自己的库
#   （printing.py 就是靠 [AccMaster].dbo.HxSysReport 取打印格式的）。
#   把 sql.database 切到那种库上 = 整个 Agent 落到一个没有业务表的库里，
#   所有 op 一起挂，而且云端只会显示"接口报错"，看不出是切错了库。
#   ⇒ 判定用「业务表四件套齐不齐」，不用名字猜、也不用"非系统库"这种弱条件。
SYSTEM_DBS = ("master", "tempdb", "model", "msdb",
              "ReportServer", "ReportServerTempDB")
ACCOUNT_SET_TABLES = ("XS", "Hpdan", "Khdan", "No_BillID")


def current_database(cur):
    """实测当前连接落在哪个库（配置写了什么 ≠ 真的连上了什么）。"""
    cur.execute("SELECT DB_NAME()")
    row = cur.fetchone()
    return (row[0] if row and row[0] else "")


def assert_account_set(cur):
    """在**当前连接的库**里实测：库名 + 业务表四件套。返回 (库名, 缺失表列表)。

    ★ 与 account_sets() 的区别：那个要连 master 枚举整台实例（GUI 下拉与命令行用），
      这个只问"我脚下这个库是不是财鲸V3 的账套"，一条请求顺带就查完了 ——
      请求级账套路由（Step 2）需要的是后者，每次 op 去枚举全实例会把自己拖死。
    ★ 表名走 OBJECT_ID(%s) 绑定参数，不拼 SQL。
    """
    got = current_database(cur)
    missing = []
    for tbl in ACCOUNT_SET_TABLES:
        cur.execute("SELECT OBJECT_ID(%s)", (u"dbo.%s" % tbl,))
        row = cur.fetchone()
        if not (row and row[0]):
            missing.append(tbl)
    return got, missing


def account_sets(cfg, timeout_sec=30):
    """枚举本实例上可切换的账套。

    返回 {"databases":[{"name","isAccountSet","missing","active"}],
          "current": <配置里的库>, "error": None|str}

    ★ 只做只读元数据查询（连 master + OBJECT_ID），不改任何东西；
    ★ 库名一律走绑定参数（OBJECT_ID(%s)），不做字符串拼接 —— 本函数会被
      GUI 与命令行同时调用，不能被一个奇怪的库名带出注入面。
    """
    s = cfg.get("sql") or {}
    out = {"databases": [], "current": s.get("database") or "", "error": None}
    conn = None
    try:
        conn = pymssql.connect(
            server=s.get("server", "127.0.0.1"),
            port=int(s.get("port", 1433)),
            user=s.get("user"),
            password=s.get("password"),
            database="master",
            tds_version=s.get("tds_version", "7.0"),
            login_timeout=int(s.get("login_timeout", 10)),
            timeout=timeout_sec,
            autocommit=True,
            charset="utf8",
        )
        cur = conn.cursor()
        cur.execute(
            """SELECT name FROM sys.databases
               WHERE DATABASEPROPERTYEX(name, 'Status') = 'ONLINE'
                 AND DATABASEPROPERTYEX(name, 'Updateability') = 'READ_WRITE'
               ORDER BY name"""
        )
        names = [r[0] for r in cur.fetchall() or []]
        for name in names:
            if name in SYSTEM_DBS:
                continue
            missing = []
            for tbl in ACCOUNT_SET_TABLES:
                cur.execute("SELECT OBJECT_ID(%s)", (u"%s.dbo.%s" % (name, tbl),))
                row = cur.fetchone()
                if not (row and row[0]):
                    missing.append(tbl)
            out["databases"].append({
                "name": name,
                "isAccountSet": not missing,
                "missing": missing,
                "active": name == out["current"],
            })
        # 能用的排前面，同名稳定
        out["databases"].sort(key=lambda d: (not d["isAccountSet"], d["name"]))
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return out


def is_switchable(cfg, database):
    """切换前的准入校验：该库必须是账套（四件套齐）。返回 (ok, 说明)。"""
    info = account_sets(cfg)
    if info["error"]:
        return False, "查询账套列表失败：%s" % info["error"]
    for d in info["databases"]:
        if d["name"] == database:
            if d["isAccountSet"]:
                return True, "OK"
            return False, "库 %s 缺少财鲸V3业务表（%s），不是账套，不能切过去" % (
                database, "、".join(d["missing"]))
    return False, "本实例上没有名为 %s 的在线可写库" % database
