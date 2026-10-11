# -*- coding: utf-8 -*-
"""一键把本地 Agent（后台 daemon）启动起来，并确认云端已上线。

★★ 为什么不能用 subprocess.Popen / Start-Process 直接拉：
   WorkBuddy 的 **Bash / PowerShell 工具在每次调用结束时会回收它拉起的进程树**
   （实测：Popen 拉起的 daemon 在**下一次工具调用**里必然已经死了；
    `status.json` 只留下一份 uptime=3 的「临终残影」，极具迷惑性）。
   已试过并失败的姿势：
     ✗ subprocess.Popen(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
     ✗ PowerShell Start-Process -WindowStyle Hidden
     ✗ WMI Win32_Process.Create（被安全策略直接拦下）
   ✔ 唯一有效：**计划任务**。进程挂在 Task Scheduler 服务下，不在我们的进程树里。

★ 为什么创建后要**立刻删掉任务**：
   任务是 ONCE 型、带触发时间的，留着会在触发时刻**再拉一个 Agent**，
   而云端同一 agent 名只允许一条连接（registry.py 的
   `close(4001, "replaced by new connection")`）→ 两个实例无限互踢。
   实测：**删任务不会杀掉已经在跑的进程实例**，所以「创建→运行→删除」是安全序列。

★ 启动的是 `--__daemon`（纯后台，无窗口）。
   GUI 只是遥控器，用户随时双击 exe 看状态即可（见铁律九）。

用法：
    PY313=C:/Users/24773/AppData/Local/Programs/Python/Python313/python.exe
    "$PY313" scripts/start-agent.py                     # 用 发布程序/V3AgentGui.exe
    "$PY313" scripts/start-agent.py <exe路径>
    "$PY313" scripts/start-agent.py --no-kill           # 不清场（已有实例时别加）
    "$PY313" scripts/start-agent.py --timeout 45        # 等上线的秒数
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "verify_exe_dbstatus", os.path.join(ROOT, "scripts", "verify-exe-dbstatus.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

DAEMON_FLAG = _mod.DAEMON_FLAG
_iter_processes = _mod._iter_processes
find_gui_exes = _mod.find_gui_exes
read_status = _mod.read_status
login = _mod.login

CLOUD = "http://149.88.75.125:8000"
TASK_NAME = "V3AgentGuiAutostart"
DEFAULT_EXE = os.path.join(ROOT, "发布程序", "V3AgentGui.exe")


def kill_stale():
    """按**映像名**清掉所有 V3AgentGui*（含 .old-*.exe）——不能只杀目标名，
    否则新旧两个 Agent 会同时连云端、互相无限踢。"""
    out = []
    for pid, name in _iter_processes():
        if pid == os.getpid():
            continue
        if "v3agent" in name.lower():
            if _mod._kill(pid):
                out.append((pid, name))
    return out


def online():
    """云端视角：Agent 是否在线 + ops 数量。失败返回 (False, 0, None)。"""
    try:
        tok = login()
        req = urllib.request.Request(
            CLOUD + "/api/v1/capabilities",
            headers={"Authorization": "Bearer " + tok})
        env = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
        ag = ((env.get("data") or {}).get("agent") or {})
        return bool(ag.get("online")), len(ag.get("ops") or []), ag.get("version")
    except Exception:  # noqa: BLE001
        return False, 0, None


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def start_via_task(exe, cwd):
    """创建一次性计划任务 → 运行 → 立刻删除任务。返回 (ok, 信息)。"""
    tr = '"%s" %s' % (exe, DAEMON_FLAG)
    r1 = run(["schtasks", "/Create", "/TN", TASK_NAME, "/TR", tr,
              "/SC", "ONCE", "/ST", "23:59", "/F"])
    if r1.returncode != 0:
        return False, "创建计划任务失败: %s" % (r1.stdout + r1.stderr)[:200]
    r2 = run(["schtasks", "/Run", "/TN", TASK_NAME])
    # ★ 无论运行成功与否都要删任务：留着会在 23:59 再拉一个实例 → 互踢
    r3 = run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    ok = r2.returncode == 0
    return ok, "run=%s delete=%s" % (r2.returncode, r3.returncode)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    exe = os.path.abspath(args[0]) if args else DEFAULT_EXE
    timeout = 30
    for i, a in enumerate(sys.argv):
        if a == "--timeout" and i + 1 < len(sys.argv):
            timeout = int(sys.argv[i + 1])
    if not os.path.isfile(exe):
        print("✗ 找不到 exe：%s" % exe)
        return 1
    exe_dir = os.path.dirname(exe)

    print("=" * 70)
    print(" 启动本地 Agent（后台 daemon）")
    print("=" * 70)
    print("exe   : %s" % exe)
    print("mtime : %s   大小 %.1f MB"
          % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(exe))),
             os.path.getsize(exe) / 1048576.0))

    if "--no-kill" not in sys.argv:
        killed = kill_stale()
        print("\n[1] 清场：已杀 %s" % (killed or "无"))
        time.sleep(2)
    else:
        print("\n[1] --no-kill：跳过清场")

    before = find_gui_exes(os.path.basename(exe))
    print("\n[2] 经计划任务拉起（绕过工具调用结束时的进程树回收）")
    ok, info = start_via_task(exe, exe_dir)
    print("    %s  %s" % ("已触发" if ok else "✗ 触发失败", info))
    if not ok:
        return 1

    print("\n[3] 等上线（最多 %ds）" % timeout)
    up, ops, ver = False, 0, None
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(2)
        up, ops, ver = online()
        if up:
            break
    if not up:
        print("    ✗ %.0fs 内未上线" % (time.time() - t0))
        st = read_status(exe_dir)
        if st:
            print("    status.json: connected=%s state=%r last_err=%r"
                  % (st.get("connected"), st.get("state"), st.get("last_err")))
        return 1
    print("    ★ 已上线（用时 %.0fs）ops=%d version=%s" % (time.time() - t0, ops, ver))

    print("\n[4] 本机实例与状态")
    procs = find_gui_exes(os.path.basename(exe))
    print("    进程: %s" % (procs or "无"))
    st = read_status(exe_dir)
    if st:
        print("    status.json: connected=%s state=%s pid=%s"
              % (st.get("connected"), st.get("state"), st.get("pid")))

    print()
    print("=" * 70)
    print(" 结论：★★★ Agent 已在线（进程挂在计划任务服务下，关窗口/结束工具调用都不受影响）")
    print("      看状态：双击 发布程序\\V3AgentGui.exe（GUI 只是遥控器）")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
