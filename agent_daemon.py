# -*- coding: utf-8 -*-
"""Agent 后台进程 —— 真正的常驻者。GUI 只是它的控制面板。

设计动机
--------
用户要求：**关掉窗口不能影响 Agent 的在线状态，进程不该被放在前台。**

所以所有权反过来：

    AgentDaemon.exe（后台，`--__daemon` 模式启动）
        ├─ 跑 WebSocket，连云端
        ├─ 写 logs\\agent.pid        （谁在跑，PID）
        ├─ 写 logs\\agent.status.json（在线状态、计数、最近日志，给 GUI 读）
        └─ 追加 logs\\agent.log      （落盘日志，GUI 关掉也能事后查）

    V3AgentGui.exe（GUI，可随时开关）
        ├─ 只读 status.json 显示状态 → 不持有连接
        ├─ 点「启动」→ spawn 一个 AgentDaemon
        ├─ 点「停止」→ 按 PID 关掉 AgentDaemon
        └─ 关窗口 → 什么都不做，AgentDaemon 继续跑 ✓

这样「关窗口」和「Agent 是否在线」彻底解耦。
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

PID_FILE = "agent.pid"
STATUS_FILE = "agent.status.json"
LOG_FILE = "agent.log"
DAEMON_FLAG = "--__daemon"
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW：
# 子进程彻底脱离父进程（关窗口不影响在线），且万一带控制台也不闪黑窗。
# 安装器和 GUI 起 daemon 都必须用这一组，别再各写一遍数字。
SPAWN_FLAGS = 0x00000008 | 0x00000200 | 0x08000000
# 账套切换指令文件：GUI / scripts/switch-db.py 原子写这个文件，
# 后台 Agent 每 ≤1s 看一次，在两条 op 之间换库（见 v3agent/agent.py 的闸门）。
DBCMD_FILE = "agent.dbcmd.json"
DB_SWITCH_POLL_SEC = 1.0
# ★ B3：实测账套上报周期（秒）。云端后台要能回答"这家现在实际在读哪套账"，
#   而这个值只有本机连库才测得出来（SELECT DB_NAME()）。
STATUS_REPORT_SEC = 30.0


# ------------------------------------------------------------------ 路径

def project_dir():
    """项目根目录 —— 「跟随文件自己所在的位置」，不写死盘符。

    优先级：环境变量 → 本文件所在目录（源码模式）→ exe 所在目录（打包模式）。
    这样把整个目录搬到 D:\\重构 也能直接跑，不需要改代码。
    """
    env = os.environ.get("V3AGENT_HOME")
    if env and os.path.isdir(env):
        return env
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.abspath(sys.executable))


def runtime_dir():
    """日志/状态文件的落盘目录。

    AgentDaemon 是独立进程，没有"exe 同目录"概念（除非打包），
    所以统一落在项目根的 logs —— 与既有 agent.log 保持一致。
    """
    env = os.environ.get("V3AGENT_RUNTIME_DIR")
    if env:
        return env
    for d in (project_dir(), os.path.dirname(os.path.abspath(sys.executable))):
        if d and os.path.isdir(d):
            p = os.path.join(d, "logs")
            os.makedirs(p, exist_ok=True)
            return p
    p = os.path.join(os.path.expanduser("~"), ".v3agent")
    os.makedirs(p, exist_ok=True)
    return p


def pid_path():
    return os.path.join(runtime_dir(), PID_FILE)


def status_path():
    return os.path.join(runtime_dir(), STATUS_FILE)


def log_path():
    return os.path.join(runtime_dir(), LOG_FILE)


def db_cmd_path():
    return os.path.join(runtime_dir(), DBCMD_FILE)


# ================================================================== 账套（sql.database）切换
#
# ★ 为什么要走"文件哨兵"而不是让 GUI 直接改配置：
#   GUI 只是遥控器、不持连接（这是 2026-09-19 定下的所有权模型）。真正在跑 op 的
#   是后台进程，改库必须通知它、并由它挑"没有在途请求"的时刻换，否则一张销售单
#   可能单头落 A 库、明细落 B 库。写完哨兵后由 Agent 自己落盘 config.json，
#   顺带保证"配置 = 运行实况"，不会出现 GUI 改了配置但 Agent 还跑老库的假象。
#
# ★ 为什么要二次确认 + 实测回填：本项目最容易出的事故就是「名字说 A、连的是 B」
#   （内置 DEFAULTS 的 agent.name=shiwantongda 而 sql.database=tongda），
#   所以切换完成与否一律以 SELECT DB_NAME() 的实测值为准，不以配置字符串为准。

def request_db_switch(database, source="cli"):
    """下发一次账套切换请求。返回 (ok, 说明)。真正换库由后台 Agent 执行。"""
    database = str(database or "").strip()
    if not database:
        return False, "账套名不能为空"
    os.makedirs(runtime_dir(), exist_ok=True)
    payload = {
        "database": database,
        "source": source,
        "nonce": "%d-%s" % (int(time.time() * 1000), os.getpid()),
        "ts": time.time(),
        "requested_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = db_cmd_path() + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, db_cmd_path())        # 原子替换，Agent 不会读到半个文件
        return True, "已下发切换请求 → %s（来源 %s）" % (database, source)
    except Exception as exc:  # noqa: BLE001
        return False, "写入切换请求失败：%s" % exc


def read_db_cmd():
    try:
        with open(db_cmd_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def clear_db_cmd(nonce):
    """只删自己处理过的那条请求：期间若又有新请求下达，不能误删。"""
    try:
        cur = read_db_cmd()
        if cur and cur.get("nonce") != nonce:
            return False
        os.remove(db_cmd_path())
        return True
    except Exception:  # noqa: BLE001
        return False


def perform_db_switch(agent, sw, cfg, config_path, state):
    """处理一条切换请求。返回 done / pending / none。

    ★ 同步函数：放到快线程池里跑，别在事件循环里连数据库。
    """
    cmd = read_db_cmd()
    if not cmd:
        state.pop("waiting", None)
        return "none"
    target = str(cmd.get("database") or "").strip()
    nonce = cmd.get("nonce")
    if not target:
        clear_db_cmd(nonce)
        return "none"
    # ★ 过期指令直接丢掉：哨兵文件是持久的，不能让一条几小时前下达、当时没人处理的
    #   请求在下次启动时把客户机静默切到另一套账上。
    ts = float(cmd.get("ts") or 0)
    if ts and time.time() - ts > 600:
        sw.add_log("忽略过期的账套切换请求 → %s（下达于 %s，已超过 10 分钟）"
                   % (target, cmd.get("requested_at") or "?"), "warn")
        sw.flush()
        clear_db_cmd(nonce)
        return "none"

    from v3agent import config as cfgmod
    from v3agent import sql as v3sql

    current = agent.database()
    if target == current:
        # 已经是这个库：把配置也对齐（可能上次落盘失败），然后收掉请求
        ok, msg = cfgmod.save_database(config_path, target)
        sw.db_switch = "已在 %s" % target
        sw.database = target
        # ★ 这里**不**改 database_verified：本分支没去实连，写个"已验证"是假证据。
        #   保留上一次真实测得的值即可（GUI 用它和配置对比，对不上会报）。
        sw.add_log("账套已是 %s，无需切换（配置：%s）" % (target, "已对齐" if ok else msg),
                   "ok" if ok else "warn")
        sw.flush()
        clear_db_cmd(nonce)
        state.pop("waiting", None)
        return "done"

    # ---- 在途闸门：有 op 正在跑就等，绝不在半张单中间换库 ----
    if agent.inflight:
        now = time.time()
        waited = now - state.get("waiting", now)
        if "waiting" not in state:
            state["waiting"] = now
            sw.add_log("收到账套切换请求 %s → %s，但有 %d 条请求在途，等它们跑完"
                       % (current, target, agent.inflight), "warn")
        elif waited >= 10:
            state["waiting"] = now
            sw.add_log("仍在等待在途请求（%d 条）…账套切换 %s → %s 暂缓"
                       % (agent.inflight, current, target), "warn")
        sw.db_switch = "等待在途请求后切换 → %s" % target
        sw.flush()
        return "pending"
    state.pop("waiting", None)

    # ---- 准入校验：目标库必须真是账套（业务表四件套齐）----
    ok, why = v3sql.is_switchable(cfg, target)
    if not ok:
        sw.db_switch = "已拒绝：%s" % why
        sw.add_log("★ 账套切换被拒绝：%s" % why, "error")
        sw.flush()
        clear_db_cmd(nonce)
        return "done"

    # ---- 换库（只在两条 op 之间发生）----
    applied, old = agent.try_set_database(target)
    if not applied:
        # 极窄竞态：闸门判定后又有新 op 进来 —— 下轮再来
        sw.db_switch = "等待在途请求后切换 → %s" % target
        sw.flush()
        return "pending"

    # ---- 落盘：让 config.json 跟上实况，别让下次重启回到旧库 ----
    saved, save_msg = cfgmod.save_database(config_path, target)

    # ---- 实测确认：配置字符串不算数，DB_NAME() 才算数 ----
    verified = ""
    try:
        with v3sql.session(agent.cfg) as conn:
            verified = v3sql.current_database(conn.cursor())
    except Exception as exc:  # noqa: BLE001
        verified = ""
        sw.add_log("★ 切库后探活失败：%s" % str(exc)[:160], "error")

    sw.database = target
    sw.database_verified = verified
    if verified == target:
        sw.db_switch = "已切换 %s → %s" % (old, target)
        sw.add_log("✓ 账套已切换：%s → %s（实测 DB_NAME()=%s）%s"
                   % (old, target, verified, "" if saved else "★ 但未落盘，重启会回到 %s" % old),
                   "ok")
    else:
        sw.db_switch = "切换结果异常：期望 %s 实测 %s" % (target, verified or "无")
        sw.add_log("★★ 账套切换结果异常：目标 %s，实测 DB_NAME()=%s（old=%s）"
                   % (target, verified or "(连不上)", old), "error")
    if not saved:
        sw.add_log("配置未写入（当前只改了内存，重启后回到 %s）：%s" % (old, save_msg), "warn")
    sw.flush()
    clear_db_cmd(nonce)
    return "done"


# ------------------------------------------------------------------ 单实例互斥
#
# ★★★ 为什么 PID 文件不够（2026-09-22 实测踩坑）：
#   pid 文件落在 `runtime_dir()`，只能挡住**同一个工作目录**下的重复启动。
#   而现实中完全可能同时存在多份 Agent（不同目录 / 旧 exe 没退干净 / 用户
#   手动双击了两次），它们各写各的 pid 文件、互不知情，却会在云端抢**同一个
#   agent.name 的注册位** → 互相顶号（4001）→ 连接反复重建。
#   症状极具迷惑性：接口忽快忽慢（实测 120s 超时与 11s 抖动交替）、Agent 日志
#   里看不到自己发出的请求、status.json 的 recv/sent 计数对不上。
#   ⇒ 再补一道**按 agent.name 的全局锁**（放系统临时目录，跨工作目录生效）。

# ★★★ 2026-09-22 回归事故：上面这版锁**不够**，实测被穿透。两个硬缺陷：
#
#   缺陷一（TOCTOU 竞态）：`read_global_lock` 判「无锁」→ `write_global_lock` 写锁，
#     两步之间没有任何原子性。两个实例相隔几十毫秒启动时，**都会读到「无锁」**，
#     都往下走，都写锁、都上线 ⇒ 云端互相顶号。实测日志：
#       [10:54:08] 后台 Agent 启动 pid=23344  → ✓ 已连接
#       [10:54:39] 后台 Agent 启动 pid=27916  → ✓ 已连接   ← 31 秒后仍能顶号
#     两者都连上了，说明锁从头到尾没生效。
#
#   缺陷二（pid 复用误判）：`pid_alive(pid)` 只问「这个 pid 号是否有进程」，不问
#     「是不是 Agent」。pid 被系统复用（或旧进程刚退、新进程接管同号）时会误判为
#     持有者存活 / 已死，两个方向都出错。
#
#   ⇒ 重写为**原子创建 + 指纹校验**：
#     · 用 `os.open(..., O_CREAT|O_EXCL)` 原子占位：只有一个人能成功。
#     · 锁内容写 JSON：pid + 启动时刻 + 可执行文件路径（指纹）。
#     · 判存活 = pid 活着 **且** 该 pid 的可执行文件与我们同一份（防复用）。
#
#   ★ 为什么 O_EXCL 能解决 TOCTOU：NTFS 上 `CreateFile` 带 `CREATE_NEW` 是
#     原子操作，两个并发调用**必然只有一个**能创建成功，另一个收到 EEXIST。

def _global_lock_path(agent_name):
    import hashlib
    import tempfile
    h = hashlib.sha1((agent_name or "default").encode("utf-8")).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), "v3agent-%s.lock" % h)


def _self_fingerprint():
    """本进程指纹：可执行文件绝对路径（打包后=exe 路径，开发期=python.exe）。"""
    try:
        return os.path.normcase(os.path.abspath(sys.executable or ""))
    except Exception:  # noqa: BLE001
        return ""


def _proc_fingerprint(pid):
    """查某个 pid 的可执行文件路径。查不到返回 ""（保守：不认为是自己人）。

    ★ 用 tasklist 而非 psutil：exe 体积敏感（见 count_local_instances 注释）。
      tasklist 的 CSV 输出里没有 exe 路径，所以用 wmic —— 它是 Windows 自带。
      wmic 若被裁剪/拦截（部分精简系统），退化为「按 pid_alive 判」，
      此时仍然要求 pid 号一致，安全性不低于旧版。
    """
    try:
        flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW
        out = subprocess.run(
            ["wmic", "process", "where", "ProcessId=%d" % int(pid),
             "get", "ExecutablePath", "/value"],
            capture_output=True, text=True, timeout=8, creationflags=flags,
        )
        if out.returncode != 0:
            return ""
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.lower().startswith("executablepath="):
                v = line.split("=", 1)[1].strip()
                return os.path.normcase(os.path.abspath(v)) if v else ""
        return ""
    except Exception:  # noqa: BLE001
        return ""


def read_global_lock(agent_name):
    """读全局锁：返回 (pid, 是否真的要阻塞我们)。

    ★ 判定「持有者仍有效」= pid 存活 **且**（指纹一致 或 指纹查不到时退化为
      「agent.name 相同」）。指纹不一致 ⇒ pid 被复用/是别的程序 ⇒ 锁是陈旧的，
      可以安全接管。
    """
    p = _global_lock_path(agent_name)
    if not os.path.isfile(p):
        return 0, False
    pid = 0
    try:
        with open(p, encoding="utf-8") as fh:
            raw = fh.read().strip()
        # 兼容旧格式（纯 pid 数字）与新格式（JSON）
        if raw.startswith("{"):
            d = json.loads(raw)
            pid = int(d.get("pid") or 0)
            holder_fp = os.path.normcase(str(d.get("exe") or ""))
        else:
            pid = int(raw or 0)
            holder_fp = ""
    except Exception:  # noqa: BLE001
        pid, holder_fp = 0, ""

    # ★★ 关键分支：锁是**自己**持有的 ⇒ 返回「已持有，不阻塞」，且**绝不能删**。
    #   （2026-09-22 自检抓出的 bug：旧写法跳过了这个分支，直接落到下面的
    #    "陈旧锁清理" 把**自己的锁删了** ⇒ 锁彻底失效，第二个实例照样能进。）
    if pid and pid == os.getpid():
        return pid, False

    # ★★ 2026-09-23 修复：判活从 `pid_alive(pid)` 换成 `_agent_process_alive(pid)`。
    #   只用 OpenProcess 判活会被 **PID 复用** 穿透：旧 Agent 被强杀后 PID 被
    #   Windows 分配给无关进程 → OpenProcess 成功 → 陈旧锁被当成「已有同名
    #   Agent 在线」→ 新 Agent（尤其是部署新 exe 后重启的）永远退出码 5。
    #   （实测：锁里 pid=29856 的旧 Agent 09:46 已死，09:59 重启时 29856 已被
    #     复用，deploy 后连续 3 次 start-agent 全部「重复实例」退出。）
    #   _agent_process_alive = pid 存活 **且** 映像名像 Agent，复用进程直接判死。
    if pid and _agent_process_alive(pid):
        me = _self_fingerprint()
        live_fp = _proc_fingerprint(pid)
        # 指纹双方都有 ⇒ 必须一致才算「真的是我们自己人」。
        # 指纹缺失（wmic 不可用）⇒ 只看 pid 存活，保持旧行为（不放松）。
        if holder_fp and me and holder_fp != me:
            # 锁里的指纹与自己的 exe 不同 ⇒ 不是同一份 Agent（换目录/换版本）
            # ★ 这里**也**要阻塞：不同副本抢同一个 agent.name 同样会顶号。
            return pid, True
        if live_fp and me and live_fp != me:
            return pid, True
        return pid, True

    # 持有者已死 / pid 号归零 ⇒ 陈旧锁，清掉
    try:
        os.remove(p)
    except Exception:  # noqa: BLE001
        pass
    return 0, False


def write_global_lock(agent_name, force=False):
    """原子抢占全局锁。返回 (ok, msg)。

    ★ force=True 用于「已确认可以接管」的场景（例如踢掉旧实例之后补写）。
    ★ 非 force 时**绝不覆盖**已有锁 —— 覆盖就回到旧的竞态去了。
    """
    p = _global_lock_path(agent_name)
    payload = json.dumps({
        "pid": os.getpid(),
        "exe": _self_fingerprint(),
        "name": agent_name,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "boot": time.time(),
    }, ensure_ascii=False)
    if not force:
        try:
            # O_CREAT|O_EXCL|O_WRONLY：原子创建，已存在则抛 FileExistsError
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            return True, "已获取全局锁"
        except FileExistsError:
            return False, "锁已存在（另一实例持有）"
        except Exception as exc:  # noqa: BLE001
            return False, "写锁失败：%s" % exc
    try:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(payload)
        return True, "已强制获取全局锁"
    except Exception as exc:  # noqa: BLE001
        return False, "强制写锁失败：%s" % exc


def release_global_lock(agent_name):
    """释放全局锁（只释放自己持有的那把）。"""
    p = _global_lock_path(agent_name)
    try:
        if not os.path.isfile(p):
            return
        with open(p, encoding="utf-8") as fh:
            raw = fh.read().strip()
        pid = int(json.loads(raw).get("pid") or 0) if raw.startswith("{") \
            else int(raw or 0)
        if pid == os.getpid():
            os.remove(p)
    except Exception:  # noqa: BLE001
        pass


def count_local_instances():
    """统计本机有几个 Agent 后台进程（按进程名，含自己）。

    ★ 用 tasklist 枚举而非 psutil：exe 打包体积敏感，psutil 会显著增大体积，
      而 tasklist 是 Windows 自带、零依赖。
    ★ 必须容错：tasklist 被安全软件拦截时返回非零码，此时返回 -1 表示「未知」，
      不能当成 0（否则会误判为无冲突而放行）。
    """
    try:
        flags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq V3AgentGui.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=8,
            creationflags=flags,
        )
        if out.returncode != 0:
            return -1
        n = 0
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if not line or "INFO:" in line or "信息:" in line:
                continue
            if "V3AgentGui.exe" in line:
                n += 1
        return n
    except Exception:  # noqa: BLE001
        return -1


# ------------------------------------------------------------------ 进程工具

def _k32():
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)


def pid_alive(pid):
    """进程是否活着。Windows 无 os.kill(pid,0) 语义，用 OpenProcess。"""
    if not pid or pid <= 0:
        return False
    try:
        h = _k32().OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        _k32().CloseHandle(h)
        return True
    except Exception:  # noqa: BLE001
        return False


def terminate(pid):
    if not pid or pid <= 0:
        return False
    try:
        k = _k32()
        h = k.OpenProcess(0x0001, False, int(pid))  # PROCESS_TERMINATE
        if not h:
            return False
        try:
            return bool(k.TerminateProcess(h, 9))
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return False


def read_pid():
    """读 pid 文件；若进程已死则顺手清掉。返回 (pid, 是否存活)。

    ★★ 2026-09-23 增强：pid_alive 之外再校验**进程映像名**——
      Windows 会复用 PID，旧 Agent 死后它的 PID 可能被分配给完全无关的进程，
      仅凭 OpenProcess 判活会误报「Agent 已在运行」→ 新 Agent 永远起不来
      （2026-09-23 实测：强杀 Agent 后 pid 文件残留，重启恰好踩中复用窗口）。
      只有「进程活着 且 映像名像 Agent」才算真活着。
    """
    p = pid_path()
    if not os.path.isfile(p):
        return 0, False
    try:
        pid = int(open(p, encoding="utf-8").read().strip() or 0)
    except Exception:  # noqa: BLE001
        return 0, False
    if pid and _agent_process_alive(pid):
        return pid, True
    try:
        os.remove(p)
    except Exception:  # noqa: BLE001
        pass
    return 0, False


def _agent_process_alive(pid):
    """进程活着且映像名像 Agent（V3AgentGui*/agent_gui/agent_daemon/python）。

    ★ 复用 _iter_processes()（Toolhelp32 快照）查映像名：一次枚举全表，
      比逐个取命令行便宜得多；找不到该 pid = 进程已死。
    """
    if not pid_alive(pid):
        return False
    try:
        for p, name, _cmd in _iter_processes():
            if int(p) == int(pid):
                low = (name or "").lower()
                return ("v3agent" in low or "agent_gui" in low
                        or "agent_daemon" in low or low.startswith("python"))
    except Exception:  # noqa: BLE001
        # 枚举失败时退回保守结论：进程活着（保持旧行为，避免误杀真实例）
        return True
    return False


def read_status():
    """读 status.json。超过 30 秒没更新就标 stale（防止读到死进程的残影）。"""
    p = status_path()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:  # noqa: BLE001
        return None
    age = time.time() - float(d.get("updated_ts") or 0)
    d["age_sec"] = int(age)
    d["stale"] = age > 30
    return d


# ------------------------------------------------------- 谁在跑 Agent

def _cmdline_is_daemon(cmd, exe):
    """判断一个进程是不是"后台 Agent"。

    ★ 极易误伤：GUI 自己也是 `python agent_gui.py` 或 `V3AgentGui.exe`，
      不能只看 exe 名。判据必须是：
        - 命令行里明确带 `--__daemon`，或
        - 命令行是 `python -m v3agent.main ... --ws`（旧的手工启动方式）
      **绝不能**只因为 exe 名叫 V3AgentGui.exe 就认定它是 Agent。
    """
    low = (cmd or "").lower()
    name = (exe or "").lower()
    if DAEMON_FLAG in low:
        return True
    if "python" in name and "v3agent.main" in low and "--ws" in low:
        return True
    return False


def _image_path(pid):
    """进程的完整 exe 路径（QueryFullProcessImageNameW）。取不到返回 ""。"""
    import ctypes
    try:
        k = _k32()
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(4096)
            size = ctypes.c_ulong(len(buf))
            if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value or ""
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return ""


def scope_of(cmd, exe, pid=None):
    """一个 Agent 进程属于"哪一份安装"。返回 normcase 绝对目录，判不出返回 ""。

    ★★ 为什么必须有这个（2026-09-26）：一台机器上可能跑两份安装 ——
      一个老板两个账套、或两家共用一台主机。以前 `agent_processes()` 只看命令行里
      有没有 `--__daemon`，于是 GUI/安装器的"清理残留"会把**别人家**的 Agent 一起停掉，
      症状是"我给 B 装完，A 家突然整片 502"。

      注意 `_iter_processes()` 给的 `exe` 只是**映像名**（Toolhelp32 快照），不是路径，
      所以归属按这三层判，判不出就返回 ""：
        1) 命令行里 `--config <路径>` 的所在目录（首选，安装器与 GUI 都会带）
        2) 命令行里第一个 `.py` 脚本的所在目录（源码模式 `python agent_gui.py --__daemon`）
        3) 打包版的完整映像路径所在目录（`python.exe` 不算 —— 否则同一台开发机上
           两个不同目录的源码 Agent 会因为"都是那个 python"而互杀）
      ★ 返回 "" 的进程**不会**被按 scope 过滤后的清理动作碰：宁可不杀，也不误杀别人家。
    """
    m = re.search(r'--config\s+"?([^"\s]+)', cmd or "")
    if m:
        return os.path.normcase(os.path.abspath(os.path.dirname(os.path.abspath(m.group(1)))))
    for tok in re.findall(r'"([^"]+\.py)"|(\S+\.py)', cmd or ""):
        one = (tok[0] or tok[1]).strip()
        if one:
            return os.path.normcase(os.path.abspath(os.path.dirname(one)))
    if pid:
        path = _image_path(pid)
        if path and "python" not in os.path.normcase(os.path.basename(path)):
            return os.path.normcase(os.path.abspath(os.path.dirname(path)))
    return ""


def agent_processes(exclude_pids=(), scope=None):
    """列出本机"后台 Agent 进程"：[(pid, 说明)]。

    两个来源合并：
      1) 我们自己的 pid 文件 —— 最权威（pid 文件按 runtime 目录分，天然只属于这一份安装）
      2) 扫命令行里带 `--__daemon` / `python -m v3agent.main --ws` 的进程

    `scope` = 某个安装目录：只返回属于那一份的进程（判不出归属的一律不动）。
    不传 = 返回全部，只用于**看**（监控、报告），绝不能拿去做停杀。
    """
    me = os.getpid()
    excl = set(exclude_pids) | {me}
    want = os.path.normcase(os.path.abspath(scope)) if scope else ""
    found = {}

    pid, alive = read_pid()
    if alive and pid not in excl:
        found[pid] = "AgentDaemon（pid 文件）"

    for p, exe, cmd in _iter_processes():
        if p in excl or not _cmdline_is_daemon(cmd, exe):
            continue
        if want and scope_of(cmd, exe, p) != want:
            continue
        found[p] = "%s（后台 Agent）" % (exe or "?")
    return sorted(found.items())


def other_installs(scope):
    """本机**不属于**这一份安装的 Agent 进程：[(pid, 说明, 归属目录)]。

    只给界面看（"这台机器上还跑着别家"），绝不交给停杀动作。
    """
    want = os.path.normcase(os.path.abspath(scope)) if scope else ""
    out = []
    for p, exe, cmd in _iter_processes():
        if not _cmdline_is_daemon(cmd, exe):
            continue
        sc = scope_of(cmd, exe, p)
        if want and sc == want:
            continue
        out.append((p, "%s（后台 Agent）" % (exe or "?"), sc or "(归属不明)"))
    return sorted(out)


def _cmdline_of(pid):
    """取某个进程的完整命令行 —— 纯 Win32 API，**零子进程、零窗口**。

    ★★★ 为什么不用 WMI / wmic / powershell：
      它们都是**控制台子系统**程序（wmic.exe / powershell.exe / cmd.exe），
      一被 spawn 就会由 conhost 分配一个控制台窗口 → 用户看到黑窗闪烁。
      在没有 `creationflags=CREATE_NO_WINDOW` 的情况下必然闪。

    实现：NtQueryInformationProcess(ProcessBasicInformation) 拿 PEB 地址，
    再 ReadProcessMemory 读 `RTL_USER_PROCESS_PARAMETERS.CommandLine`（UNICODE_STRING）。
    x64 下 PEB 偏移 0x20 是 ProcessParameters。
    失败（权限不足/64 位读 32 位等）一律返回 ""，调用方按「无命令行」处理 ——
    只会让该进程**不被判定为后台 Agent**，不会误杀，安全。
    """
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010
    STATUS_INFO_LENGTH_MISMATCH = 0xC0000004

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT),
                    ("MaximumLength", wintypes.USHORT),
                    ("Buffer", ctypes.c_void_p)]

    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [("Reserved1", ctypes.c_void_p),
                    ("PebBaseAddress", ctypes.c_void_p),
                    ("Reserved2", ctypes.c_void_p * 2),
                    ("UniqueProcessId", ctypes.c_void_p),
                    ("Reserved3", ctypes.c_void_p)]

    h = k.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, int(pid))
    if not h:
        # 退一步：只要查询权限（有些进程拒绝 VM_READ）
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
    try:
        pbi = PROCESS_BASIC_INFORMATION()
        ret = ctypes.c_ulong(0)
        st = ntdll.NtQueryInformationProcess(
            ctypes.c_void_p(h), 0, ctypes.byref(pbi),
            ctypes.sizeof(pbi), ctypes.byref(ret))
        if st == STATUS_INFO_LENGTH_MISMATCH:
            return ""
        if st != 0 or not pbi.PebBaseAddress:
            return ""

        # PEB -> ProcessParameters
        pp = ctypes.c_void_p()
        if not k.ReadProcessMemory(ctypes.c_void_p(h),
                                   ctypes.c_void_p(pbi.PebBaseAddress + 0x20),
                                   ctypes.byref(pp), ctypes.sizeof(pp), None):
            return ""
        if not pp.value:
            return ""

        # RTL_USER_PROCESS_PARAMETERS -> CommandLine (偏移 0x70)
        us = UNICODE_STRING()
        if not k.ReadProcessMemory(ctypes.c_void_p(h),
                                   ctypes.c_void_p(pp.value + 0x70),
                                   ctypes.byref(us), ctypes.sizeof(us), None):
            return ""
        if not us.Buffer or not us.Length:
            return ""
        buf = ctypes.create_string_buffer(int(us.Length) + 2)
        if not k.ReadProcessMemory(ctypes.c_void_p(h), ctypes.c_void_p(us.Buffer),
                                   buf, int(us.Length), None):
            return ""
        return buf.raw[:int(us.Length)].decode("utf-16-le", "replace")
    except Exception:  # noqa: BLE001
        return ""
    finally:
        try:
            k.CloseHandle(h)
        except Exception:  # noqa: BLE001
            pass


def _iter_processes():
    """(pid, exe, cmdline)。exe 走 Toolhelp32，命令行走原生 API（带编码兜底）。

    ★★★ 2026-09-20 重写：**不再调用外部控制台程序取命令行**。
      旧实现是 `subprocess.run(["powershell", ...])`，两个致命后果：
        1. **每次调用都闪一个黑窗**（控制台子系统程序被调用即创建 conhost）。
           GUI 的 `_refresh()` 在「未运行」分支里每 1.5 秒调一次
           → 用户看到的就是「一堆 cmd/powershell 窗口反复弹、关了又弹」。
           实测单次耗时 0.85s，等于**一分钟弹约 70 次**。
        2. 拖慢一次刷新近 1 秒，GUI 明显卡顿。
      现在改用 `_cmdline_of()`（NtQueryInformationProcess + 读 PEB），
      **纯 API 调用、零子进程、零窗口、微秒级**。
    """
    import ctypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)

    class PE32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_char * 260),
        ]

    rows = []
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    if snap != -1:
        e = PE32()
        e.dwSize = ctypes.sizeof(PE32)
        ok = k.Process32First(snap, ctypes.byref(e))
        while ok:
            rows.append((int(e.th32ProcessID), e.szExeFile.decode("gbk", "replace")))
            ok = k.Process32Next(snap, ctypes.byref(e))
        k.CloseHandle(snap)

    out = []
    for pid, name in rows:
        # 只在「可能是 Agent」的进程上取命令行 —— 取命令行要 OpenProcess，
        # 对系统进程会失败且更慢；过滤后候选通常只有 0~3 个。
        cmd = ""
        if "python" in name.lower() or "v3agent" in name.lower():
            cmd = _cmdline_of(pid)
        out.append((pid, name, cmd))
    return out


def own_scope():
    """当前这一份 Agent 自己的安装目录（打包版 = exe 同目录）。

    给所有清理/停止动作当 scope：只有同目录的才算"我家残留"，
    别人家的 Agent 一律不动 —— 判据见 scope_of()。
    """
    if getattr(sys, "frozen", False):
        return os.path.normcase(os.path.abspath(os.path.dirname(os.path.abspath(sys.executable))))
    return os.path.normcase(os.path.abspath(project_dir()))


def kill_process_tree(pid):
    """连子进程一起停。返回 (是否成功, 说明)。

    ★★ PyInstaller onefile 的 bootloader PID 和真正跑 Python 的子进程不是同一个，
      TerminateProcess 只杀一个会留下「看着停了、云端还连着一条 WS」的假象
      （2026-09-26 装第二家时实测踩到）。所以停 Agent 一律走 taskkill /F /T /PID，
      terminate() 只作为拿不到 taskkill 时的兜底。
    """
    if not pid or pid <= 0:
        return False, "无效 PID"
    try:
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            return True, "PID %d（含子进程）" % pid
    except Exception:  # noqa: BLE001
        pass
    if terminate(pid):
        return True, "PID %d（仅本体，子进程可能还在）" % pid
    return False, "PID %d 停不掉" % pid


def kick_duplicates(keep_pid=None, exclude_pids=(), scope=None):
    """确保"这一份安装"只留一个 Agent：把除 keep_pid 以外的同目录实例关掉。

    为什么必须有这个：云端每个 agent.name 只允许一条 WS 连接，
    多实例会互相 4001 踢线，结果谁都不稳。这是本项目最大的坑。

    ★ scope 默认取 own_scope()：同一台机器上跑两份安装（一家两个账套 / 两家共用主机）
      已经是既定场景，不带 scope 的清理会把**别家**的 Agent 停掉。
      确实要全机清理（开发机收尾）时显式传 scope=""。
    """
    killed = []
    for p, desc in agent_processes(exclude_pids=exclude_pids, scope=own_scope() if scope is None else scope):
        if keep_pid and p == keep_pid:
            continue
        ok, why = kill_process_tree(p)
        if ok:
            killed.append((p, "%s：%s" % (desc, why)))
    return killed


# ------------------------------------------------------- 启动 / 停止

def _spawn_target():
    """该用什么命令起一个后台 Agent。

    ★ 注意：必须走 `agent_gui.py --__daemon`，因为只有它有 __main__ 入口。
      直接跑 `python agent_daemon.py` 的话，本文件在 import 时不执行任何东西
      （本文件末尾虽有 __main__ 兜底，但主入口统一放在 agent_gui.py 更清晰）。

    开发期：`python.exe agent_gui.py --__daemon`
    打包后：`V3AgentGui.exe --__daemon`（自带 --noconsole，天然无窗口）
    """
    gui_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_gui.py")
    if getattr(sys, "frozen", False):
        return [sys.executable, DAEMON_FLAG]
    if os.path.isfile(gui_py):
        return [sys.executable, gui_py, DAEMON_FLAG]
    return [sys.executable, os.path.abspath(__file__), DAEMON_FLAG]


def start_daemon(cfg_path=None, clean_first=True, scope=None):
    """起一个后台 Agent。返回 (ok, msg, pid)。

    clean_first=True：先踢掉**这一份安装**的旧实例，保证只有一条云端连接。
    scope 缺省时用 cfg_path 所在目录 —— 那份 config 就是"我家"的定义，
    没有 cfg_path 才退回 exe 同目录（own_scope）。
    """
    pid, alive = read_pid()
    if alive:
        return True, "Agent 已在后台运行（PID %d）" % pid, pid

    if scope is None:
        scope = (os.path.normcase(os.path.abspath(os.path.dirname(os.path.abspath(cfg_path))))
                 if cfg_path else own_scope())
    if clean_first:
        for p, desc in kick_duplicates(scope=scope):
            pass  # 调用方可以从日志看到

    cmd = _spawn_target()
    if cfg_path:
        cmd += ["--config", cfg_path]

    # DETACHED_PROCESS(0x8) + CREATE_NEW_PROCESS_GROUP(0x200)：
    # 让子进程完全脱离父进程，父进程（GUI）退出/关窗口都不影响它。
    # ★ 再加 CREATE_NO_WINDOW(0x08000000) 双保险：万一将来有人把这里的
    #   目标从 `--noconsole` 的 exe 换成带控制台的解释器，也不会闪黑窗。
    flags = 0x00000008 | 0x00000200 | 0x08000000
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
    except Exception:  # noqa: BLE001
        si = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            creationflags=flags,
            startupinfo=si,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        return False, "启动后台进程失败：%s" % exc, 0

    # 等它把 pid 文件写出来（最多 8 秒）
    for _ in range(16):
        time.sleep(0.5)
        p2, alive2 = read_pid()
        if alive2:
            return True, "后台 Agent 已启动（PID %d）" % p2, p2
    return bool(pid_alive(proc.pid)), "已拉起进程 PID %d，但没等到 pid 文件" % proc.pid, proc.pid


def stop_daemon(scope=None):
    """停掉后台 Agent。返回 (ok, msg)。

    ★ 只停"这一份安装"的：同机跑两份时，不带 scope 的停止会把别家一起干掉。
    """
    stopped = []
    scope = scope if scope is not None else own_scope()
    pid, alive = read_pid()
    if alive:
        ok, why = kill_process_tree(pid)
        if ok:
            stopped.append(why)
    for p, desc in agent_processes(scope=scope):
        ok, why = kill_process_tree(p)
        if ok:
            stopped.append(why)
        if os.path.isfile(pid_path()):
            try:
                os.remove(pid_path())
            except Exception:  # noqa: BLE001
                pass
    if stopped:
        return True, "已停止：" + "、".join(sorted(set(stopped)))
    return False, "这一份安装没有正在运行的后台 Agent（同机别家的目录不会被碰）"


# =============================================================== 后台主体

class StatusWriter:
    """把运行状态定期写进 status.json，供 GUI / 外部读取。"""

    def __init__(self, path):
        self.path = path
        self.state = "启动中"
        self.connected = False
        self.recv = 0
        self.sent = 0
        self.started_at = time.time()
        self.agent_name = ""
        self.relay_url = ""
        self.version = ""
        self.database = ""
        self.database_verified = ""
        self.db_switch = ""
        self.logs = []
        self.last_err = ""

    def add_log(self, msg, level="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        line = "[%s] %s" % (ts, msg)
        self.logs.append({"t": ts, "msg": msg, "level": level})
        if len(self.logs) > 300:
            self.logs = self.logs[-300:]
        # 同步落盘，GUI 关掉后也能查
        try:
            with open(log_path(), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass

    def flush(self):
        data = {
            "state": self.state,
            "connected": self.connected,
            "recv": self.recv,
            "sent": self.sent,
            "uptime_sec": int(time.time() - self.started_at),
            "agent_name": self.agent_name,
            "relay_url": self.relay_url,
            "version": self.version,
            "database": self.database,
            "database_verified": self.database_verified,
            "db_switch": self.db_switch,
            "pid": os.getpid(),
            "logs": self.logs[-80:],
            "last_err": self.last_err,
            "updated_ts": time.time(),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)   # 原子替换，GUI 不会读到半个文件
        except Exception:  # noqa: BLE001
            pass


def run_daemon(config_path=None, runtime_dir_override=None):
    """后台 Agent 主体。由 `--__daemon` 进入，永不创建窗口。"""
    import asyncio
    import logging

    if runtime_dir_override:
        os.environ["V3AGENT_RUNTIME_DIR"] = runtime_dir_override

    rt = runtime_dir()
    os.makedirs(rt, exist_ok=True)

    # 单实例：已经有活着的就退出（避免又搞出互踢）
    old_pid, alive = read_pid()
    if alive and old_pid != os.getpid():
        # ★★ 2026-09-23 修复：PyInstaller GUI（--windowed）模式下
        #   sys.stdout / sys.stderr 都是 **None**，直接 write 会
        #   AttributeError（实测：Agent 启动即崩、弹窗 Unhandled exception
        #   in script，表现为永远「未上线」——正是用户截图的那个错误框）。
        #   后台模式本就没有控制台，退出原因必须落盘日志才有意义。
        try:
            sys.stderr.write("已有 Agent 在运行（PID %d），本次退出。\n" % old_pid)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.makedirs(rt, exist_ok=True)
            with open(os.path.join(rt, LOG_FILE), "a", encoding="utf-8") as fh:
                fh.write("[%s] 单实例拦截：已有 Agent 在运行（PID %d），本次退出\n"
                         % (time.strftime("%H:%M:%S"), old_pid))
        except Exception:  # noqa: BLE001
            pass
        return 2

    # 写 pid
    try:
        with open(pid_path(), "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except Exception:  # noqa: BLE001
        pass

    # 把自己挂到父进程之外（双保险；spawn 时已用 DETACHED_PROCESS）
    try:
        import ctypes
        ctypes.WinDLL("kernel32").FreeConsole()
    except Exception:  # noqa: BLE001
        pass

    # 日志：只写文件，不输出 stdout（后台没有控制台）
    logging.basicConfig(
        level=logging.INFO,
        handlers=[logging.FileHandler(log_path(), encoding="utf-8")],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    sw = StatusWriter(status_path())

    # 找出 v3agent 包
    import agent_gui as gui   # 复用 ensure_import_path / find_config
    gui.ensure_import_path()

    try:
        from v3agent import __version__, ops
        from v3agent.agent import LocalAgent, hello_payload, status_payload
        from v3agent import config as cfgmod
        import websockets
    except Exception as exc:  # noqa: BLE001
        sw.state = "依赖缺失"
        sw.last_err = str(exc)
        sw.add_log("加载失败：%s" % exc, "error")
        sw.flush()
        return 3

    path = config_path or gui.find_config()
    if not path:
        sw.state = "配置缺失"
        sw.add_log("找不到 config.json：双击 V3AgentGui.exe 打开控制台，它会在这台机器上"
                   "建出一份并在『本机配置』里让你填商户码/密钥/账套/SQL 凭据；"
                   "或用安装器装。★ 包里不内置商户身份，没有配置就不上线 —— "
                   "后台服务不会自己建配置（那等于凭空造一个不知道属于哪家的实例）。", "error")
        sw.flush()
        return 4

    try:
        cfg = cfgmod.load(path)
    except cfgmod.ConfigError as exc:
        # ★ P1-4：配置缺身份键就不再上线。以前这里会静默兜成
        #   agent.name=shiwantongda + sql.database=tongda，结果是「用 A 家的名字读
        #   B 家的账」，页面看着一切正常。宁可报错也不猜。
        sw.state = "配置不可用"
        sw.last_err = str(exc)
        sw.add_log("配置不可用，拒绝上线：%s" % exc, "error")
        sw.flush()
        return 5

    a = cfg["agent"]
    # ★ 装机指纹：本机唯一、首次生成后长期复用。云端靠它区分"重连"与"另一台机器
    #   拿同一份密钥上线"，后者会被拒(close 4002)而不是把在线那家顶掉。
    a["install_id"] = cfgmod.ensure_install_id(cfg, path)
    sw.agent_name = a.get("name", "")
    sw.relay_url = a.get("relay_url", "")
    sw.version = __version__
    sw.add_log("后台 Agent 启动 pid=%d" % os.getpid())
    sw.add_log("配置：%s" % path)
    sw.add_log("云端：%s" % sw.relay_url)
    sw.add_log("可执行操作：%d 个" % len(ops.OPS))
    sw.database = cfgmod.get_database(cfg)
    sw.add_log("账套（配置）：%s" % sw.database)

    # ★★ 上线前先用 DB_NAME() 实测一次库名（2026-09-25 打包验收时发现缺这一步）：
    #   没有这一步，"实测库"字段在第一次切换之前一直是空的，于是"配置写 A、
    #   实际连 B"这种本项目最容易出的事故，偏偏在最该被发现的上线时刻发现不了。
    try:
        from v3agent import sql as v3sql
        with v3sql.session(cfg) as _conn:
            sw.database_verified = v3sql.current_database(_conn.cursor())
    except Exception as exc:  # noqa: BLE001
        sw.database_verified = ""
        sw.add_log("★ 启动实测库名失败：%s —— 下面显示的账套只是配置值，未经数据库确认"
                   % str(exc)[:120], "warn")
    if sw.database_verified and sw.database_verified != sw.database:
        sw.add_log("★★ 配置写 %s，但实测 DB_NAME()=%s —— 不一致，先确认在连哪套账"
                   % (sw.database, sw.database_verified), "error")
    elif sw.database_verified:
        sw.add_log("实测账套 DB_NAME() = %s（与配置一致）" % sw.database_verified, "ok")
    sw.flush()

    # ★★★ 第二道单实例闸门：按 agent.name 的全局锁（跨工作目录生效）。
    #   上面 pid 文件只挡同目录；这里挡「不同目录的多份副本抢同一个注册名」。
    #   不让它们同时上线，否则云端互相顶号 → 假慢 / 假崩溃。
    #
    #   ★★ 2026-09-22 事故修正：旧版「读锁 → 判活 → 写锁」有 TOCTOU 竞态，
    #      实测被穿透（两个实例相隔 31 秒都成功上线，云端互相顶号）。
    #      现改为「踢重复 → 原子抢锁」，并区分两种失败：
    #        · 抢锁失败（别人已持有）→ 退出码 5，不打扰对方
    #        · 抢锁成功但本机仍有别的 Agent 进程 → 记日志但**不接管**，
    #          交给下面的 kick_duplicates 做定向清理
    holder_pid, holder_alive = read_global_lock(sw.agent_name)
    if holder_alive and holder_pid != os.getpid():
        # 再确认一次：可能刚才是陈旧锁（read_global_lock 已顺手清掉）。
        # 仍然存在 ⇒ 真的有同名实例在线，本进程退出，避免互相顶号。
        sw.state = "重复实例"
        sw.last_err = "已有同名 Agent 在线（PID %d）" % holder_pid
        sw.add_log(
            "★ 已有同名 Agent 在线（agent=%s, PID %d），本次退出，避免互相顶号。"
            % (sw.agent_name, holder_pid), "error")
        sw.flush()
        return 5

    # 本机进程数自检：>1 说明还有别的副本在跑（哪怕它锁的是别的名字）。
    n_inst = count_local_instances()
    if n_inst > 1:
        sw.add_log(
            "⚠ 本机检测到 %d 个 Agent 进程，可能有旧副本未退干净；将尝试定向清理。"
            % n_inst, "warn")
    elif n_inst < 0:
        sw.add_log("（进程枚举不可用，跳过本机实例数自检）", "warn")

    # ★ 主动清理：把所有「别的」Agent 进程踢掉（排除自己）。
    #   旧版只是 warn 一句就放行 ⇒ 抢注册名继续存在。这里真踢。
    #   keep_pid=自己，exclude_pids=自己，避免自杀（历史上踩过 no-selfkill 的坑）。
    me = os.getpid()
    for p, desc in agent_processes(exclude_pids=(me,)):
        if p == me:
            continue
        if terminate(p):
            sw.add_log("已清理重复 Agent 实例 PID %d（%s）" % (p, desc), "warn")
    # 再数一次，确认清干净了（给自己一点时间退出）
    for _ in range(10):
        if count_local_instances() <= 1:
            break
        time.sleep(0.3)

    # ★ 原子抢锁：O_CREAT|O_EXCL，并发下必然只有一个成功。
    ok, msg = write_global_lock(sw.agent_name)
    if not ok:
        # 极端情况：踢完旧实例后又被第三方抢先占锁（例如用户同时连点）。
        sw.state = "重复实例"
        sw.last_err = msg
        sw.add_log("★ 抢占全局锁失败：%s，本次退出。" % msg, "error")
        sw.flush()
        return 5
    sw.add_log("✓ 已获取全局锁（agent=%s, pid=%d）" % (sw.agent_name, me))
    sw.flush()

    agent = LocalAgent(cfg)
    hb = int(a.get("heartbeat_sec", 25))
    rc = int(a.get("reconnect_sec", 5))
    stop_file = os.path.join(rt, "agent.stop")

    # ★★★ 分级并发（2026-09-22 治本第二轮）：
    #
    #   背景（实测石锤）：改造前是 `max_workers=1` + 单个队列 + 单个 worker ⇒
    #   所有 op 严格排队。实测：慢 op（product-ranking）跑 3.45s 时，同刻插入的
    #   `system.ping`（纯内存、本该 <1ms）**也等到 3.46s 才返回** ⇒ 完全阻塞。
    #   这就是用户反馈的「报表查询卡住的时候切其他页面也无法加载内容」的根因。
    #
    #   为什么不能简单把 max_workers 调大？因为报表类重查询同时跑 4 个会一起
    #   压本地 SQL Server（历史上曾有跑 48~89s 的记录）。
    #
    #   ⇒ 分级：**快池（多数请求）** + **重池（报表类，限并发）**。
    #     · 快池 max_workers=8：列表/明细/单头/capabilities/ping —— 永不排队。
    #       （DB 实测：轻查询并发 8 路墙钟 18→23ms，无退化，故敢开 8。）
    #     · 重池 max_workers=2：6 个报表类 op —— 限流，不压垮本地库。
    #     DB 承载实测（probe-db-concurrency.py）：轻查询并发 8 路墙钟 18→23ms
    #     无退化；重查询并发 4 路仍线性 ⇒ 两个池的规模都安全。
    #
    #   心跳（system.ping / ping-pong）走**事件循环内联处理**，根本不进池，
    #   所以哪怕两个池都占满，Agent 依然能正常应答心跳、不被云端踢。
    from concurrent.futures import ThreadPoolExecutor
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

    fast_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="v3fast")
    heavy_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="v3heavy")
    sw.add_log("并发模型：快池×8 + 重池×2（重 op 限并发）")

    async def main_loop():
        attempt = 0
        last_flush = 0.0
        while True:
            # 支持"优雅停止"：GUI 写一个 stop 文件即可
            if os.path.isfile(stop_file):
                sw.state = "已停止"
                sw.connected = False
                sw.add_log("收到停止信号，退出。")
                sw.flush()
                try:
                    os.remove(stop_file)
                except Exception:  # noqa: BLE001
                    pass
                return
            try:
                sw.state = "连接中…"
                sw.flush()
                async with websockets.connect(a["relay_url"]) as ws:
                    await ws.send(json.dumps(hello_payload(cfg), ensure_ascii=False))
                    await asyncio.sleep(1.5)   # 撑过 1.5 秒没被关 = 登录成功
                    attempt = 0
                    sw.connected = True
                    sw.state = "已连接"
                    sw.add_log("✓ 已连接云端中转", "ok")
                    sw.flush()
                    last_ping = time.time()
                    # ★★★ 治本（2026-09-22）：op 执行必须放到**线程池**，
                    #   否则 `agent.handle_request()` 里的同步 SQL（pymssql 是阻塞的）
                    #   会卡死整个 asyncio 事件循环 → 收不到也发不出 WS ping/pong
                    #   → 云端判定 keepalive 超时发 1011 → Agent 假死 + 前端 502。
                    #   实测：product-ranking 在真库可跑 48~89s，足够饿死心跳。
                    #
                    #   第二轮（分级并发）：**两个池 + 两个队列 + 两个 worker**。
                    #   快请求（列表/明细/ping 等）进 fast_q ⇒ 永不被报表堵住；
                    #   报表类重 op 进 heavy_q ⇒ 最多 2 个并发，不压垮本地库。
                    #   这就是「报表卡住时其他页面照常加载」的保证。
                    #
                    #   ★ 关键：worker **自己**把响应发回 ws，接收循环绝不 await 结果。
                    #   若接收循环 `await fut`，整条协程会挂起到 op 结束，
                    #   期间到不了 `await ws.recv()` ⇒ 心跳依然被饿死（实测验证过）。
                    #   ⇒ 收发彻底解耦：接收循环只管收 + 发心跳，发出结果由 worker 负责。
                    fast_q = asyncio.Queue()
                    heavy_q = asyncio.Queue()
                    loop = asyncio.get_running_loop()
                    send_lock = asyncio.Lock()   # 防止两个 worker 与心跳同时写 ws

                    async def worker(q, pool, tag):
                        while True:
                            req = await q.get()
                            op = req.get("op") or req.get("operation") or "?"
                            t0 = time.time()
                            # ★ 按请求路由到非默认库时把这本写进日志行：
                            #   两本账共用一个进程，事后只看"跑了什么 op"查不出"读的是哪本"。
                            want_db = str(req.get("database") or "").strip()
                            db_tag = (" 账套=%s" % want_db) if want_db else ""
                            sw.add_log("→ 请求 %s [%s]%s" % (op, tag, db_tag), "req")
                            try:
                                # 在线程池里跑同步 SQL，不阻塞事件循环
                                resp = await loop.run_in_executor(
                                    pool, agent.handle_request, req)
                            except Exception as exc:  # noqa: BLE001
                                resp = {"ok": False,
                                        "error": {"code": "INTERNAL", "message": str(exc)}}
                            cost = (time.time() - t0) * 1000
                            if resp.get("ok"):
                                d = resp.get("data")
                                n = d.get("total") if isinstance(d, dict) else None
                                extra = (" total=%s" % n) if n is not None else ""
                                sw.add_log("← 完成 %s  %.0fms%s" % (op, cost, extra), "ok")
                            else:
                                err = (resp.get("error") or {}).get("message", "")
                                sw.last_err = err
                                sw.add_log("← 失败 %s  %.0fms  %s" % (op, cost, err), "error")
                            # worker 直接回包（不经过接收循环的 await），
                            # 这样即使 op 跑几十秒，接收循环仍在正常收帧/发心跳。
                            try:
                                async with send_lock:
                                    await ws.send(json.dumps(
                                        {"type": "response", **resp},
                                        ensure_ascii=False))
                                sw.sent += 1
                                sw.flush()
                            except Exception as exc:  # noqa: BLE001
                                sw.add_log("回包失败 %s：%s" % (op, str(exc)[:120]), "warn")

                    worker_tasks = [
                        asyncio.create_task(worker(fast_q, fast_pool, "快")),
                        asyncio.create_task(worker(heavy_q, heavy_pool, "重")),
                    ]

                    # 账套热切换：≤1s 看一次哨兵文件，真活丢给快线程池干
                    # （is_switchable / DB_NAME() 都要连库，绝不能在事件循环里同步做）。
                    db_state = {}
                    last_dbchk = 0.0
                    # 允许清单（config.sql.databases）的磁盘修改时间：
                    # 装机的人用 switch-db.py --allow 登记第二本之后，
                    # 不该还得重启 Agent —— 症状会是"清单加了但仍读不到那本"，
                    # 而第一反应往往去查云端放行没有。
                    last_cfg_mtime = 0.0
                    # ★ B3：实测账套要定期、且**热切之后立刻**报给云端。
                    #   只靠 hello 不够 —— 握手时可能还没连过库，而热切之后若不立刻报，
                    #   后台最长 30 秒里显示的还是上一套账，那正好是最需要看准的时刻。
                    last_status = 0.0
                    need_status = True

                    async def send_status():
                        nonlocal last_status, need_status
                        # 状态帧只此一处拼（v3agent.agent.status_payload）：
                        # 两套 WS 实现各写一份的话，"只有 exe 上报了放行清单"这种分叉最难查。
                        payload = status_payload(
                            agent, database=sw.database or "",
                            verified=sw.database_verified or "",
                            uptime_sec=int(time.time() - sw.started_at))
                        try:
                            async with send_lock:
                                await ws.send(json.dumps(payload, ensure_ascii=False))
                        except Exception:  # noqa: BLE001 连接正在换，下一轮再报
                            return
                        last_status = time.time()
                        need_status = False

                    async def pump_db_switch():
                        nonlocal need_status
                        try:
                            await loop.run_in_executor(
                                fast_pool, perform_db_switch,
                                agent, sw, cfg, path, db_state)
                            need_status = True
                        except Exception as exc:  # noqa: BLE001
                            sw.add_log("账套切换执行异常：%s" % str(exc)[:160], "error")
                            sw.flush()

                    try:
                        while True:
                            now = time.time()
                            if now - last_dbchk >= DB_SWITCH_POLL_SEC:
                                last_dbchk = now
                                # 清单同步只在 config.json 真的被改过时做一次，且丢进线程池
                                # （getmtime 很便宜，读盘+解析不放到事件循环里）
                                try:
                                    mt = os.path.getmtime(path)
                                except OSError:
                                    mt = 0.0
                                if mt > last_cfg_mtime:
                                    last_cfg_mtime = mt
                                    chg, allowed = await loop.run_in_executor(
                                        fast_pool, cfgmod.refresh_databases, cfg, path)
                                    if chg:
                                        sw.add_log("本机允许读的账套清单已同步：%s"
                                                   % "、".join(allowed), "ok")
                                        need_status = True
                                if os.path.isfile(db_cmd_path()):
                                    await pump_db_switch()
                            if need_status or now - last_status >= STATUS_REPORT_SEC:
                                await send_status()
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                            except asyncio.TimeoutError:
                                now = time.time()
                                if now - last_ping >= hb:
                                    async with send_lock:
                                        await ws.send(json.dumps({"type": "ping"}))
                                    last_ping = now
                                if now - last_flush >= 2:
                                    sw.flush()
                                    last_flush = now
                                continue
                            if not msg:
                                continue
                            sw.recv += 1
                            try:
                                req = json.loads(msg)
                            except json.JSONDecodeError:
                                continue
                            t = req.get("type")
                            if t == "ping":
                                async with send_lock:
                                    await ws.send(json.dumps({"type": "pong"}))
                                continue
                            if t == "request":
                                # 只投递，**不 await**：接收循环立刻回去收帧/发心跳。
                                # 结果由 worker 直接回包（见 worker）。
                                # ★ 按 op 名路由：报表类进重队列（限并发），
                                #   其余进快队列（永不被报表堵住）。
                                op_name = str(req.get("op") or req.get("operation") or "")
                                if op_name in HEAVY_OPS:
                                    await heavy_q.put(req)
                                else:
                                    await fast_q.put(req)
                    finally:
                        for wt in worker_tasks:
                            wt.cancel()
                        for wt in worker_tasks:
                            try:
                                await wt
                            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                                pass
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                text = str(exc)
                sw.connected = False
                if "4002" in text:
                    # ★ 名字被另一台安装占用：抢不进来。快速重连只会刷满日志、
                    #   还会让运营者误以为"网络问题"。改成 60 秒一次并说清下一步。
                    sw.state = "名称被占用"
                    sw.last_err = "另一台机器已用同名上线（install_id 不同）"
                    sw.add_log("★ 接入被拒(4002)：%s 已被另一台安装占用。"
                               "如果确实是本机换硬盘/重装系统，请到云端后台点『接管』后再试；"
                               "如果不是，说明有第二份 Agent 在跑，请找出并停掉它。"
                               "（60 秒后自动重试）" % a["name"], "error")
                    sw.flush()
                    await asyncio.sleep(60)
                    continue
                if "4001" in text:
                    # ★★ 4001 在这套中转里有**两种完全不同的意思**：
                    #   · 云端 `server.py` 用 4001 + reason="bad secret" 拒密钥不对的连接；
                    #   · 云端 `registry.py` 用 4001 + reason="replaced by new connection" 顶旧连接。
                    #   以前这里一律写"被另一条连接顶掉 —— 本机还有别的 Agent"，
                    #   于是**密钥被重置过的机器被指成了"有第二份 Agent"**：
                    #   2026-09-26 现网就这么把唯一真商户挡在门外 50 分钟，
                    #   排查方向一路被带偏（去翻进程、翻云端注册表，其实只差一个密钥）。
                    #   ⇒ 一律带上云端给的 reason，两种情况的处置写清楚。
                    if "secret" in text.lower():
                        sw.state = "密钥不符"
                        sw.last_err = "云端不认本机 agent.secret（多半是后台重置过密钥）"
                        sw.add_log("★ 接入被拒(4001 bad secret)：本机 `agent.secret` 与云端登记的不一致。"
                                   "去云端后台『商户』页重新取该商户的 Agent 密钥，"
                                   "改 config.json 的 agent.secret 后重启 Agent。"
                                   "★ 这不是网络问题，也不是有第二份 Agent。", "error")
                        sw.flush()
                        # 密钥错重连一百次也不会对：慢下来，别刷满日志。
                        await asyncio.sleep(60)
                        continue
                    sw.state = "被顶号"
                    sw.add_log("被另一条连接顶掉(4001) —— 同一个 agent.name 还有另一条连接在抢"
                               "（常见：把装好的目录/exe+config 拷到了第二台机器）。%s"
                               % (text[:120] or ""), "error")
                else:
                    sw.state = "重连中"
                    sw.add_log("连接断开：%s" % text[:140], "warn")
                sw.flush()
                # ★ 重连加抖动（Phase 5）：云端重启会让所有商户同时掉线，
                #   固定 5 秒意味着它们每一轮都撞在同一时刻（重连风暴 + 云端重启后
                #   的第一秒被打满）。按商户码错开 0-15 秒；用 str 的 byte 和而不是 hash()，
                #   因为 CPython 的 str hash 每个进程加盐，换进程就错开了。
                await asyncio.sleep(rc + (sum(sw.agent_name.encode("utf-8")) % 16))

    try:
        asyncio.run(main_loop())
    except Exception as exc:  # noqa: BLE001
        sw.state = "异常退出"
        sw.add_log("运行异常：%s" % exc, "error")
        sw.flush()
    finally:
        for _pool in (fast_pool, heavy_pool):
            try:
                _pool.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
        release_global_lock(sw.agent_name)
        # 只有"这个 pid 文件是自己的"才删，避免误删新进程的
        try:
            if os.path.isfile(pid_path()):
                with open(pid_path(), encoding="utf-8") as fh:
                    mine = int(fh.read().strip() or 0)
                if mine == os.getpid():
                    os.remove(pid_path())
        except Exception:  # noqa: BLE001
            pass
        try:
            if os.path.isfile(status_path()):
                st = read_status()
                if st and st.get("pid") == os.getpid():
                    os.remove(status_path())
        except Exception:  # noqa: BLE001
            pass
    return 0


def _main():
    """独立入口：`python agent_daemon.py --__daemon [--config X]`。

    agent_gui.py 里也有同名分支，两者等价 —— 这里保留是为了
    在调试时可以直接单独跑后台，不用绕 GUI。
    """
    ensure_paths()
    cfg = None
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            cfg = argv[i + 1]
    sys.exit(run_daemon(cfg))


def ensure_paths():
    """把项目根和本文件目录都塞进 sys.path，保证能 import v3agent。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, project_dir(), getattr(sys, "_MEIPASS", "")):
        if d and os.path.isdir(d) and d not in sys.path:
            sys.path.insert(0, d)


if __name__ == "__main__":
    _main()
