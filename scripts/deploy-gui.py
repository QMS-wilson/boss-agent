#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把修好的 dist\\V3AgentGui.exe 部署到 发布程序\\（旧版改名保留，可 --rollback）。

★★ 两条与"多商户 + 白天也在用"直接相关的规矩（2026-09-26 加）：
  1) **只杀目标目录里跑着的进程**。以前是无条件
     `taskkill /F /IM V3AgentGui.exe /T` —— 一台机器上装了多家（或开发者同时开着
     dist 那份）时，它会把**别家正在服务的 Agent 连同你开着的窗口一起关掉**。
     现在按 ExecutablePath 前缀筛，只杀要覆盖的那份；筛不到就直接覆盖，
     覆盖失败（文件被占用）就明确报错让你去关，而不是背地里杀进程。
  2) exe 与 **VERSION.json** 一起放。云端 /admin 靠这个版本号认"哪家还没换包"，
     只换 exe 不放版本信息的话，hello.version 会退回 0.0.0+unknown。

用法：
    python scripts\\deploy-gui.py                  # 部署 dist 产物
    python scripts\\deploy-gui.py --list            # 看目录里有哪些版本可回滚
    python scripts\\deploy-gui.py --rollback 20260926-005147   # 用 .old-<stamp>.exe 退回
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "dist", "V3AgentGui.exe")
DST_DIR = os.path.join(ROOT, "发布程序")
DST = os.path.join(DST_DIR, "V3AgentGui.exe")
NAME = "V3AgentGui.exe"


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def running_under(dirpath):
    """列出 ExecutablePath 落在 dirpath 下的 V3AgentGui 进程 PID（含 onefile 父子两进程）。

    ★ 必须按**镜像路径**筛而不是按镜像名：同名不同目录 = 不同商户/不同构建。
    """
    ps = ("Get-CimInstance Win32_Process -Filter \"name='%s'\" | "
          "Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like '%s*' } | "
          "ForEach-Object { $_.ProcessId }" % (NAME, dirpath.replace("'", "")))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        print("! 查进程列表失败（%s）—— 不敢乱杀，直接尝试覆盖" % exc)
        return []
    return [x.strip() for x in (r.stdout or "").splitlines() if x.strip().isdigit()]


def kill_pids(pids):
    """onefile 的 bootloader PID ≠ 真正跑 Python 的子进程 PID，必须 /T 连带子进程。"""
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
    if pids:
        time.sleep(2)


def copy_in(src_exe, stamp_note):
    meta = os.path.join(os.path.dirname(os.path.abspath(src_exe)), "VERSION.json")
    shutil.copy2(src_exe, DST)
    if os.path.isfile(meta):
        shutil.copy2(meta, os.path.join(DST_DIR, "VERSION.json"))
        body = json.load(open(meta, encoding="utf-8"))
        print("  版本信息：%s  build_id=%s  built_at=%s"
              % (body.get("semver"), str(body.get("build_id"))[:12], body.get("built_at")))
    else:
        print("  ! 没找到 %s —— exe 会自报 0.0.0+unknown，/admin 里看不出是哪一版" % meta)
    print("已部署：%s  %s" % (DST, stamp_note))
    print("  大小 %.1f MB  md5 %s" % (os.path.getsize(DST) / 1048576.0, md5(DST)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rollback", metavar="STAMP", help="用 V3AgentGui.old-<STAMP>.exe 退回旧版")
    ap.add_argument("--list", action="store_true", help="列出可回滚的旧版")
    ap.add_argument("--restart", action="store_true",
                    help="部署后重新拉起后台 daemon（会断 3~10 秒，请在无人用时用）")
    args = ap.parse_args()

    olds = sorted([n for n in os.listdir(DST_DIR) if n.startswith("V3AgentGui.old-")],
                  reverse=True) if os.path.isdir(DST_DIR) else []
    if args.list:
        print("发布目录：%s" % DST_DIR)
        print("  当前   %s  md5 %s" % (NAME, md5(DST) if os.path.isfile(DST) else "(无)"))
        for n in olds:
            p = os.path.join(DST_DIR, n)
            print("  可回滚 %s  %.1f MB  md5 %s" % (n, os.path.getsize(p) / 1048576.0, md5(p)))
        return 0

    if args.rollback:
        want = "V3AgentGui.old-%s.exe" % args.rollback
        if want not in olds:
            print("✗ 没有 %s。可回滚：%s" % (want, [o.replace("V3AgentGui.old-", "").replace(".exe", "") for o in olds]))
            return 1
        src = os.path.join(DST_DIR, want)
    else:
        if not os.path.isfile(SRC):
            print("未找到 %s（先跑 build_gui.py）" % SRC)
            return 1
        src = SRC

    os.makedirs(DST_DIR, exist_ok=True)
    # ★ 要停的永远是**被覆盖的那个镜像**（发布目录里那份），不是 dist 那份。
    #   第一版这里写的是 `os.path.dirname(SRC)`（= dist），后果是发布目录里正在跑的
    #   daemon 根本没被停：rename 照样成功（Windows 允许改名运行中的 exe），于是
    #   "部署成功"的日志与真实运行的进程不是一回事 —— 新包躺在目录里，线上还在跑旧包。
    #   这类"部署了但没生效"最费时间，所以这里宁可多打一行现状。
    pids = running_under(DST_DIR)
    if args.rollback:
        pids = running_under(DST_DIR)          # 回滚要停的是发布目录里正在跑的那个
    if pids:
        print("目标目录在跑的进程：%s → 先停掉（其它目录的实例一律不动）" % ",".join(pids))
        kill_pids(pids)

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if os.path.isfile(DST) and not args.rollback:
        bak = os.path.join(DST_DIR, "V3AgentGui.old-%s.exe" % stamp)
        os.rename(DST, bak)
        print("旧产物已改名保留：%s（要退回：deploy-gui.py --rollback %s）" % (os.path.basename(bak), stamp))

    try:
        copy_in(src, "（回滚自 %s）" % args.rollback if args.rollback else "")
    except PermissionError as exc:
        print("✗ 覆盖被占用：%s" % exc)
        print("  说明还有目标目录的实例在跑，请先在控制台点『停止』或 taskkill 对应 PID 后重试。")
        print("  本脚本刻意不做「杀不到就乱杀」——那会连带关掉别家正在服务的 Agent。")
        return 2

    same = md5(src) == md5(DST)
    print("  源 md5 %s  目标 md5 %s  一致=%s" % (md5(src), md5(DST), same))
    if args.restart and same:
        cfg = os.path.join(DST_DIR, "config.json")
        subprocess.Popen(["powershell", "-NoProfile", "-Command",
                          "Start-Process -FilePath '%s' -ArgumentList '--__daemon','--config','%s' "
                          "-WorkingDirectory '%s'" % (DST, cfg, DST_DIR)])
        print("  已重新拉起后台 daemon（约 5~20s 后重连云端，请核对 database_verified）")
    return 0 if same else 2


if __name__ == "__main__":
    sys.exit(main())
