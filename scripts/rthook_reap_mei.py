# -*- coding: utf-8 -*-
r"""PyInstaller 运行时钩子 —— 在冻结程序**最开头**执行（早于任何业务 import）。

职责（只有一件）：**回收本程序历史遗留的解包目录**。

★ 为什么需要它
---------------
PyInstaller onefile 的 bootloader 把程序解包到 %TEMP%\_MEIxxxxxx，进程退出时删掉它。
但**退出时删除并不可靠**：
  - 该目录里的 `.pyd` / `.dll` 可能仍处于镜像映射状态，内核没释放
  - 杀软 / EDR 的 minifilter 可能正扫描这些文件
删除失败 → bootloader 弹「Failed to remove temporary directory」对话框。
实测（2026-09-20）：曾累积 88 个 `_MEI*` 残留，合计约 1.6 GB。

★ 为什么在「启动时」回收而不是退出时强删
---------------------------------------
退出时强删要「等内核释放」，只能 sleep+retry，既拖慢退出又引入时序 bug。
启动时回收则天然安全：上一次进程肯定已彻底退出，那些目录的锁早没了。

★★★ 2026-09-20 重大修正 —— 别再设 `--runtime-tmpdir`（付了极大代价）
-------------------------------------------------------------------
历史误判：以为「解包到固定私有目录」更好（名字不随机、便于回收），
于是在 `build_gui.py` 里设了 `--runtime-tmpdir %LOCALAPPDATA%\V3Agent\runtime`。

**实测结果（同一份 exe，只改这一个参数）：**

| 解包位置 | 启动耗时 |
|---|---|
| `--runtime-tmpdir %LOCALAPPDATA%\V3Agent\runtime` | **101.3 s** |
| `--runtime-tmpdir %USERPROFILE%\V3Agent-rt` | **99.3 s** |
| **不设（默认、原地 %TEMP%）** | **1.8 s** ✅ |

**相差 56 倍。** PyInstaller onefile 的 bootloader 只有在解包到
**自己的默认临时目录**时才走快速路径；给任何其它绝对路径都会退化成极慢的
逐文件写入例程。（三次连跑都是 ~99 s，不是杀软冷启动。）

这个 101 秒直接导致了两个用户可见故障：
  ① 用户以为程序卡死；
  ② 解包目录存活 100 秒 → 退出时「来不及删/被占用」的概率大增
     → **正是弹「Failed to remove temporary directory」的根因**。

⇒ **结论：绝不要再设 `--runtime-tmpdir`。** 让 bootloader 用它自己的默认目录。
   本 hook 相应改为在 **默认 %TEMP%** 里按「内容特征」认领本程序的残留。

★ 安全红线（踩过坑，务必遵守）
----------------------------
  1. **绝不删自己**：必须跳过 `sys._MEIPASS`，否则程序把自己抽掉
     （当年"幽灵 Agent"就是这么来的：配置指向了一个被删掉的 `_MEI` 目录）。
  2. %TEMP% 是**共享**目录，绝不能「看到 _MEI* 就删」——
     实测有 7 个 `_MEI*` 属于**别的程序**（含 numpy/lxml/PIL，本项目没这些依赖）。
     ⇒ 必须用**内容特征**认领：本程序解包后顶层必然同时出现
       `pymssql/` 与 `websockets/`（这两个依赖的组合足以区分，已实测验证）。
  3. 全程 try/except + **总时间预算**，任何异常都不能阻断启动。

★ 为什么要有「总时间预算」
------------------------
旧版 `_rmtree_retry(tries=8, delay=0.5)` 是「每个目录最坏 4 秒」。
被杀进程映射过的树，删除可能**不是抛异常，而是极慢**（`try/except` 抓不到），
残留一多，启动就被拖到 76 秒（实测）。现在：
  · 单目录最多试 2 次、每次间隔 0.2 s
  · **整体硬预算 2.0 s**，超了就放弃剩余目录（下次启动再收）
  · 回收永远只是锦上添花，绝不阻塞启动

★ 诊断开关
---------
`V3_RTHOOK_TRACE=1` → 无条件写「我跑了」及扫描明细到 startup.log。
"""

import os
import shutil
import sys

# ★ 诊断：无条件留痕（用来判定 hook 到底有没有被 exe 执行）
TRACE = os.environ.get("V3_RTHOOK_TRACE") == "1"

# ---- 本程序的内容特征（用于在共享 %TEMP% 里认领自己的残留）--------------
# 实测依据（2026-09-20）：本程序顶层条目含这些；一个只用 json/csv/sqlite3
# 的无关 PyInstaller 程序则不含。三者同时成立才认领，误判率极低。
OWN_MARKERS = ("pymssql", "websockets")

# ---- 回收预算（必须很小，这是启动路径）--------------------------------
TOTAL_BUDGET = 2.0      # 秒，整体上限
PER_TRIES = 2           # 单目录最多尝试次数
PER_DELAY = 0.2         # 秒，单目录重试间隔

# ★★★ 新目录保护（2026-09-20 23:40 修复「collections.abc 崩溃」）----------
#   事故：用户双击后弹「Failed to execute script 'pyi_rth_inspect':
#         No module named 'collections.abc'」+「Failed to obtain traceback」。
#   根因：startup.log 显示 23:38:23 **同一秒有两个进程启动**（双击 + 杀软沙箱
#         预运行等并发场景），互相把对方**刚解包的活目录**当残留回收。
#         PermissionError 那两次是运气好（对方已锁文件，rename 失败）；
#         只要一次 rename 赶在对方建锁之前成功，对方运行时即被整体抽走，
#         下一个 rthook import 时文件已消失 → 就是上述报错
#         （连 traceback.pyc 都没了，所以「Failed to obtain traceback」）。
#   修复：**创建时间 < 10 分钟的目录一律跳过**。
#         活进程的解包目录必然是刚建的；真正的历史残留必然是旧的。
#         一刀切消灭「删掉活进程运行时」的整类竞态，成本为零。
MIN_AGE_SECONDS = 600   # 10 分钟内的 _MEI 目录绝不回收


def _trace(msg):
    if TRACE:
        _log("【trace】" + msg)


def _runtime_root():
    """要扫描的根目录 = `sys._MEIPASS` 的父目录（即 bootloader 的临时根）。

    回到默认解包位置后，父目录就是 %TEMP%（形如 C:\\Users\\x\\AppData\\Local\\Temp）。
    ★ 用 `_MEIPASS` 反推而不是读 %TEMP% 环境变量：更贴近真实落点，
      即便将来 bootloader 换了目录也不会扫错地方。
    返回 (root, mine)。
    """
    try:
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            ab = os.path.abspath(mei)
            return os.path.dirname(ab), ab
    except Exception:
        pass
    # 兜底：用 %TEMP%
    for k in ("TEMP", "TMP"):
        v = os.environ.get(k)
        if v:
            return v, ""
    return "", ""


def _looks_ours(path):
    """内容特征判据：这个 `_MEI*` 目录是不是**本程序**留下的？

    %TEMP% 是共享目录 → 只能按内容认领，不能按名字。
    实测：本程序顶层必然同时有 `pymssql/` 与 `websockets/`（hidden-import 带进来的），
    一个无关的 PyInstaller 程序不会同时具备这两个。
    """
    try:
        if not os.path.isdir(path):
            return False
        names = set(os.listdir(path))
        return all(m in names for m in OWN_MARKERS)
    except Exception:
        return False


def _rmtree_once(path):
    """删一个目录：**先改名挪走，再删改名的那个**。

    ★★★ 关键（2026-09-20 踩了至少 5 次）：绝不直接 `rmtree` 原名。
    对「刚被一个**被杀死的**进程映射过」的 `_MEI` 目录直接 rmtree，
    会让**整个进程当场 exit 1 / SIGTERM 死掉**（无 traceback，try/except 抓不住）——
    因为树里的 `.pyd`/`.dll` 仍在镜像映射状态，删除踩到 Windows 加载器竞态。

    解法：① 先对整棵目录 `os.rename`。改名只动目录项、不碰文件内容，
             不触发映射竞态；被占用时正常抛 PermissionError。
          ② 改名成功 = 这棵树已不被任何进程关联，此时 rmtree 才安全。
    """
    tmp = "%s.reef-%d" % (path, os.getpid())
    try:
        os.rename(path, tmp)
    except Exception:
        return False, "rename-failed"
    try:
        shutil.rmtree(tmp)
        return True, None
    except Exception as exc:
        # 删除失败也别把树留在怪名字上，尽力还原
        try:
            os.rename(tmp, path)
        except Exception:
            pass
        return False, type(exc).__name__


def _size_of(path):
    sz = 0
    try:
        for r, _, fs in os.walk(path):
            for f in fs:
                try:
                    sz += os.path.getsize(os.path.join(r, f))
                except Exception:
                    pass
    except Exception:
        pass
    return sz


def _reap():
    """扫掉根目录里**属于本程序**的遗留解包目录。返回 (删除数, 失败数, 释放字节)。"""
    import time as _t

    root, mine = _runtime_root()
    _trace("_runtime_root -> root=%r mine=%r" % (root, mine))
    if not root or not os.path.isdir(root):
        _trace("root 不存在，跳过")
        return 0, 0, 0

    mine_l = os.path.normcase(os.path.abspath(mine)) if mine else ""
    try:
        names = sorted(os.listdir(root))
    except Exception as exc:
        _trace("listdir 失败: %s" % exc)
        return 0, 0, 0
    _trace("扫到 %d 项（只认领含 %s 的 _MEI*）"
           % (len(names), "+".join(OWN_MARKERS)))

    deadline = _t.perf_counter() + TOTAL_BUDGET
    now = _t.time()
    done = failed = 0
    freed = 0
    skipped_foreign = 0
    skipped_self = 0
    skipped_recent = 0

    for n in names:
        if not n.lower().startswith("_mei"):
            continue
        full = os.path.abspath(os.path.join(root, n))
        # ★★★ 绝不删自己
        if mine_l and os.path.normcase(full) == mine_l:
            skipped_self += 1
            continue
        if not os.path.isdir(full):
            continue
        # ★★ 共享目录：只能认领自己的
        if not _looks_ours(full):
            skipped_foreign += 1
            continue
        # ★★★ 新目录保护：创建时间 < 10 分钟的一律跳过。
        #   并发启动的第二个实例（杀软沙箱预运行 / 用户连点两个实例）会把
        #   第一个实例**刚解包的活目录**当残留。活目录的 mtime 必然是几秒前，
        #   而真正需要回收的残留必然是旧目录 —— 用 mtime 一刀切开。
        try:
            age = now - os.path.getmtime(full)
        except Exception:
            continue
        if age < MIN_AGE_SECONDS:
            skipped_recent += 1
            _trace("跳过新目录 %s（%.0f 秒前）" % (n, age))
            continue
        if _t.perf_counter() >= deadline:
            _trace("预算耗尽，剩余目录留待下次启动")
            break

        sz = _size_of(full)
        ok = False
        for i in range(PER_TRIES):
            ok, err = _rmtree_once(full)
            if ok:
                break
            if i < PER_TRIES - 1:
                _t.sleep(PER_DELAY)
            if _t.perf_counter() >= deadline:
                break
        if ok:
            done += 1
            freed += sz
        else:
            failed += 1
            _log("  · 回收失败 %s：%s" % (n, err))

    _trace("跳过自己 %d 个、跳过非本程序 %d 个、跳过新目录 %d 个"
           % (skipped_self, skipped_foreign, skipped_recent))
    return done, failed, freed


def _log(msg):
    """尽力写一行日志到 %LOCALAPPDATA%\\V3Agent\\startup.log（拿不到就静默）。"""
    try:
        la = os.environ.get("LOCALAPPDATA")
        if not la:
            return
        d = os.path.join(la, "V3Agent")
        os.makedirs(d, exist_ok=True)
        import datetime
        line = "%s %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        with open(os.path.join(d, "startup.log"), "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


try:
    _done, _failed, _freed = _reap()
    if _done or _failed or TRACE:
        _log("启动回收：清除遗留解包目录 %d 个（失败 %d 个，释放 %.1f MB），当前 _MEIPASS=%s"
             % (_done, _failed, _freed / 1048576.0, getattr(sys, "_MEIPASS", "")))
except Exception:
    # 回收只是「锦上添花」，绝不能让它挡住程序启动
    pass
