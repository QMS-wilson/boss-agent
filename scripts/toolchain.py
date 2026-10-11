# -*- coding: utf-8 -*-
"""工具链路径解析（本仓 scripts/ 共用，三个仓内容一致）。

★★ 为什么要有这个文件（2026-10-10）：
   托管运行时**被升级过一次**（node `22.22.2-3` → `22.22.2-6`），
   而脚本里写死 `.../node/versions/22.22.2-3/node.exe` 的地方直接
   `FileNotFoundError`。报错堆栈指向 `subprocess`，看起来像"测试失败/
   构建失败"，实际是**工具链路径过期** —— 排查方向会被带偏很久。

   同一个坑撞了至少两次（deploy.py 2026-10-07、test-adapters.py 2026-10-10），
   所以这次不再各写一份，收敛到这里。

解析顺序（第一个存在的就用）：
   ① 环境变量 `NODE` / `PY313`（临时指定用，也是 CI 的入口）
   ② PATH 上能找到的 `node`
   ③ 托管目录里**版本号最大**的那个 node
   ④ 系统 Python（`Python313`）的固定安装位 —— 它是用户真装的、不随托管升级漂移
"""
import os
import re
from shutil import which

# 托管目录：WorkBuddy 的隔离运行时
_NODE_BASE = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries",
                          "node", "versions")
_PY_BASE = os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries",
                        "python", "versions")


def _ver_key(s):
    """把 '22.22.2-6' 这类串转成可比较的元组。

    ★ 纯字符串比较会错：'22.22.2-10' < '22.22.2-6'（'1' < '6'）。
      所以按 `.` 和 `-` 切开、数字段转 int，非数字段给 0（排最后）。
    """
    return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", s)]


def _newest_in(base, exe_name):
    try:
        dirs = [d for d in os.listdir(base)
                if os.path.isdir(os.path.join(base, d))]
    except OSError:
        return ""
    for d in sorted(dirs, key=_ver_key, reverse=True):
        exe = os.path.join(base, d, exe_name)
        if os.path.exists(exe):
            return exe
    return ""


def resolve_node():
    """返回可用的 node 绝对路径；全找不到时抛 SystemExit（带怎么修）。"""
    env = os.environ.get("NODE")
    if env and os.path.exists(env):
        return env
    on_path = which("node")
    if on_path:
        return on_path
    got = _newest_in(_NODE_BASE, "node.exe") or _newest_in(_NODE_BASE, "node")
    if got:
        return got
    raise SystemExit(
        "找不到 node：设 NODE 环境变量，或确认 %s 下有可用版本" % _NODE_BASE)


def resolve_python313():
    """返回一个**系统装的** Python 3.13（不是托管 venv）。

    ★ 为什么优先系统 Python：本仓的验收脚本大量用标准库起子进程、跑 Agent，
      托管 venv 依赖不全（历史上缺 xlrd/openpyxl 就让源码 Agent 起不来）。
      系统那个是用户真装的，稳定。
    """
    env = os.environ.get("PY313")
    if env and os.path.exists(env):
        return env
    cands = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python",
                     "Python313", "python.exe"),
        r"C:\Python313\python.exe",
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    # 退路：本进程自己（若恰好就是 3.13）
    import sys
    if sys.version_info[:2] == (3, 13):
        return sys.executable
    got = _newest_in(_PY_BASE, "python.exe")
    if got:
        return got
    raise SystemExit("找不到 Python 3.13：设 PY313 环境变量")


if __name__ == "__main__":
    print("node   =", resolve_node())
    print("py313  =", resolve_python313())
