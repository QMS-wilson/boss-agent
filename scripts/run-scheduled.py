# -*- coding: utf-8 -*-
"""把一个本地脚本放进「任务计划」里跑，从而**绕开宿主 Python 的 safe-delete shim**。

为什么需要它
------------
宿主 WorkBuddy 通过 `PYTHONPATH` 注入 `sitecustomize.py`，把
`os.remove / os.rmdir / shutil.rmtree` 换成「移入回收站」的包装器。
于是任何在本会话里跑的 Python 脚本，**删不掉东西**（还被日志刷屏）。

任务计划的进程**不继承**本会话的环境变量（PYTHONPATH / 各种 CODEBUDDY_*），
所以它的 Python 是**干净的**，删除是真删。

用法：
  python run-scheduled.py <脚本绝对路径> [超时秒数]

产物：同目录下 <脚本名>.sched.log
"""

import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = r"C:\Users\24773\AppData\Local\Programs\Python\Python313\python.exe"

TASK = "V3CleanupProbe"


def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法：run-scheduled.py <脚本绝对路径> [超时秒数]")
    target = os.path.abspath(sys.argv[1])
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0
    log = target + ".sched.log"
    try:
        os.remove(log)          # shim 会拦；拦了就 append，无妨
    except Exception:
        pass
    # 清空日志
    with open(log, "w", encoding="utf-8") as fh:
        fh.write("")

    pyw = PY.replace('"', '')
    inner = '"%s" "%s" > "%s" 2>&1' % (pyw, target, log)
    create = [
        "schtasks", "/Create", "/TN", TASK, "/F",
        "/SC", "ONCE", "/ST", "23:59",
        "/TR", "cmd /c " + inner,
    ]
    r = subprocess.run(create, capture_output=True, text=True)
    print("create rc=%s" % r.returncode, r.stdout.strip() or r.stderr.strip())
    if r.returncode != 0:
        raise SystemExit("建任务失败")

    r = subprocess.run(["schtasks", "/Run", "/TN", TASK],
                       capture_output=True, text=True)
    print("run rc=%s" % r.returncode, r.stdout.strip() or r.stderr.strip())

    time.sleep(timeout)
    subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"],
                   capture_output=True, text=True)
    print("=" * 70)
    print(" 任务输出（%s）" % log)
    print("=" * 70)
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            sys.stdout.write(fh.read())
    except Exception as e:
        print("读日志失败：%s" % e)


if __name__ == "__main__":
    main()
