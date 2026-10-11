# -*- coding: utf-8 -*-
"""一行命令看后台 Agent 状态（不开窗口）。

读 logs\\agent.pid 和 logs\\agent.status.json，并顺带查一下云端是否在线。
"""

import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根（跟随脚本位置，可整体搬迁）
sys.path.insert(0, ROOT)
import agent_daemon as dm  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import livecreds  # noqa: E402


def dur(sec):
    sec = int(sec or 0)
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分 %d 秒" % (sec // 60, sec % 60)
    return "%d 时 %d 分" % (sec // 3600, (sec % 3600) // 60)


def cloud_online():
    """从云端确认 Agent 是否在线（需要登录）。失败就返回 None。"""
    try:
        req = urllib.request.Request(
            "http://149.88.75.125:8000/api/v1/auth/login",
            data=json.dumps({"provider": "password", "account": livecreds.ACCOUNT,
                             "password": livecreds.APP_PASSWORD}).encode(),
            headers={"Content-Type": "application/json"})
        tok = json.loads(urllib.request.urlopen(req, timeout=12).read())["data"]["accessToken"]
        req2 = urllib.request.Request("http://149.88.75.125:8000/api/v1/agents",
                                      headers={"Authorization": "Bearer " + tok})
        d = json.loads(urllib.request.urlopen(req2, timeout=12).read())
        for a in d.get("agents", []):
            if a.get("name") == "shiwantongda":
                return bool(a.get("online"))
    except Exception:  # noqa: BLE001
        return None
    return False


def main():
    pid, alive = dm.read_pid()
    st = dm.read_status()

    print("  进程状态 : %s" % ("★ 运行中  PID %d" % pid if alive else "未运行"))
    if not alive:
        left = dm.agent_processes(exclude_pids=(os.getpid(),))
        if left:
            print("           但发现 %d 个残留进程：%s" % (len(left), left))

    if st:
        age = st.get("age_sec", 0)
        fresh = age <= 30
        # ★ 进程已死时，状态文件里那句"已连接"是临终残影，不能照实显示，否则误导
        if not alive:
            conn_text = "（已退出，下面是它退出前的最后状态）"
        elif st.get("connected"):
            conn_text = "已连接 ✓"
        else:
            conn_text = st.get("state") or "?"
        print("  云端连接 : %s %s" % (conn_text, "（状态新鲜）" if (fresh and alive) else ""))
        print("  运行时长 : %s" % dur(st.get("uptime_sec")))
        print("  收发计数 : 收 %s 条 / 发 %s 条" % (st.get("recv"), st.get("sent")))
        print("  Agent 名 : %s  版本 %s" % (st.get("agent_name"), st.get("version")))
        print("  最后更新 : %s" % st.get("updated_at"))
        if st.get("last_err"):
            print("  最近错误 : %s" % st["last_err"])
        logs = st.get("logs") or []
        if logs:
            print("\n  最近 5 条日志：")
            for item in logs[-5:]:
                print("    [%s] %s" % (item.get("t"), item.get("msg")))
    else:
        print("  云端连接 : （无状态文件，Agent 尚未运行过）")

    print()
    on = cloud_online()
    if on is None:
        print("  云端确认 : 查询失败（网络或账号问题），以上面本地状态为准")
    else:
        print("  云端确认 : %s" % ("Agent 在线 ✓" if on else "Agent 不在线"))

    print()
    print("  日志文件 : %s" % dm.log_path())
    print()
    print("  提示：关掉任何窗口都不会影响上面这个后台状态。")
    print("        想停止请开控制台点『停止后台 Agent』，或运行")
    print("        python scripts\\kill-all-agents.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
