# -*- coding: utf-8 -*-
r"""出「V3Agent一键安装.exe」—— 现场装机用的那一份。

用法：
    py -3.13 scripts\build_gui.py          # 先出 Agent 本体 dist\V3AgentGui.exe
    py -3.13 scripts\build_installer.py    # 再把它嵌进安装器

★★ 为什么改名重写（2026-09-26）：
   原来这条线是 scripts/build_agent.py —— 它从 agent_entry.py 打一份 `V3Agent.exe`，
   再让安装器嵌那份。而线上真正在跑的从来是 `V3AgentGui.exe --__daemon`，
   agent_entry.py 停在 2026-09-20、dist 里也没有 V3Agent.exe ⇒
   **安装器一旦真跑「一键安装」就是 FileNotFoundError**，装第三家时才会暴露。
   现在只嵌 dist\V3AgentGui.exe 这一份，出包名 = 安装名 = 运行名，同一个来源。
   旧文件留在 scripts/build_agent.py.retired-20260926。

★ 出完必须验 TOC：安装器里真的带着 exe、且 agent_daemon 被一起打了进去
  （安装器现在要复用它的 DAEMON_FLAG / 进程归属判据，漏了就是运行期 ImportError）。
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "V3Agent一键安装"
AGENT_EXE = "V3AgentGui.exe"          # 必须与 setup_gui/setup_app.py 的 AGENT_EXE 一致
DIST = os.path.join(ROOT, "dist")


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(args):
    print(">>", " ".join(args))
    r = subprocess.run(args, cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit("PyInstaller 退出码 %d" % r.returncode)


def main():
    agent_exe = os.path.join(DIST, AGENT_EXE)
    if not os.path.isfile(agent_exe):
        raise SystemExit("缺少 %s\n  先跑：py -3.13 scripts\\build_gui.py" % agent_exe)

    # 出包名与安装名必须同源：安装器里写的是 AGENT_EXE，这里打包的也必须是它
    sys.path.insert(0, os.path.join(ROOT, "setup_gui"))
    import setup_app
    if setup_app.AGENT_EXE != AGENT_EXE:
        raise SystemExit("安装器要装的是 %s，本脚本嵌的是 %s —— 两边漂移"
                         % (setup_app.AGENT_EXE, AGENT_EXE))

    ver = {}
    vj = os.path.join(DIST, "VERSION.json")
    if os.path.isfile(vj):
        ver = json.load(io.open(vj, encoding="utf-8"))
    print("被嵌入的 Agent：%s  md5=%s  mtime=%s" % (
        agent_exe, md5(agent_exe), time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(agent_exe)))))
    print("版本戳：%s" % json.dumps(ver, ensure_ascii=False)[:200])
    if not ver:
        print("★ dist 里没有 VERSION.json —— 这份包在云端看不出版本号（build_gui 才会生成它）")

    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--noconsole",
         "--uac-admin", "--name", NAME, "--paths", ROOT,
         "--add-binary", "%s;." % agent_exe,
         os.path.join(ROOT, "setup_gui", "setup_app.py")])

    out = os.path.join(DIST, NAME + ".exe")
    if not os.path.isfile(out):
        raise SystemExit("没产出 %s" % out)

    # ---- 自检：装进去的东西对不对（不靠"看起来成功了"）----
    #   注意分层：onefile 的 CArchive 顶层只列 二进制资源 + PYZ.pyz，
    #   Python 模块在 PYZ 里面 —— 拿顶层 TOC 去找 agent_daemon 永远是 False。
    from PyInstaller.archive.readers import CArchiveReader
    arc = CArchiveReader(out)
    top = list(arc.toc)
    has_agent = any(k.replace("\\", "/").endswith(AGENT_EXE) for k in top)
    pyz_names = []
    for k in top:
        if k.lower().endswith(".pyz"):
            try:
                pyz_names = list(arc.open_embedded_archive(k).toc)
            except Exception as exc:  # noqa: BLE001
                print("  （读 %s 失败：%s）" % (k, exc))
    has_daemon = "agent_daemon" in pyz_names
    print("\nCArchive 顶层 %d 条、PYZ 模块 %d 个；%s 已嵌入=%s；agent_daemon 已打入=%s"
          % (len(top), len(pyz_names), AGENT_EXE, has_agent, has_daemon))
    if not has_agent:
        raise SystemExit("✗ 安装器里没有 %s，现场点「一键安装」会报找不到内置 exe" % AGENT_EXE)
    if not has_daemon:
        raise SystemExit("✗ 安装器没打进 agent_daemon（它要复用 DAEMON_FLAG 与进程归属判据）")

    # ---- 打包版首启探针：拿**打出来的那份 exe** 真跑一次 ----
    #   这次改的是"没有配置时双击会发生什么"，而源码级门看不见打包：
    #   config.template.json 有没有随包、load_any/validate_identity 有没有进 PYZ，
    #   只有跑这份 exe 才知道。故意把 --config 指到一个不存在的目录：
    #   真包里这一步也必须留下一份 selfcheck 报告，否则拷机的人就是"点了没反应"。
    import shutil
    import tempfile
    tdir = tempfile.mkdtemp(prefix="pkg-firstrun-")
    try:
        pexe = os.path.join(tdir, AGENT_EXE)
        shutil.copy2(agent_exe, pexe)
        try:
            subprocess.run([pexe, "--selfcheck", "--config",
                            os.path.join(tdir, "nope", "config.json")],
                           timeout=120, shell=False)
        except subprocess.TimeoutExpired:
            # ★ 超时要单独报：这份 exe 是 --noconsole 的，脚本里抛未捕获异常会弹
            #   系统错误框并一直等人点 —— 现象就是"探针卡死"。上一版就是这么撞掉的
            #   （`load_any(cfg_path=cfg)` 写错参数名，TypeError → 弹窗 → 240s 超时）。
            raise SystemExit("✗ 打包版 --selfcheck 120s 没退出：多半是它弹了对话框或在等输入。"
                             "拷机的人遇到的是同一种死法，先修这条再出包。")
        found = []
        for dname, _dn, fnames in os.walk(tdir):
            for fn in fnames:
                if fn.startswith("selfcheck-") and fn.endswith(".txt"):
                    found.append(os.path.join(dname, fn))
        body = ""
        if found:
            body = io.open(sorted(found)[-1], encoding="utf-8").read()
        probe_ok = ("商户码" in body) and ("本机配置" in body)
        print("\n打包版首启探针：%s（报告 %d 份；内容里点到缺的商户码与『本机配置』=%s）"
              % ("✓" if probe_ok else "✗", len(found), probe_ok))
        if not probe_ok:
            raise SystemExit("✗ 打出来的 exe 在没配置时连一份自检报告都不写 —— "
                             "模板或 load_any 没随包，拷机的人点开是死的")
    finally:
        shutil.rmtree(tdir, ignore_errors=True)

    print("\n产物：%s" % out)
    print("  md5=%s  大小=%.1f MB" % (md5(out), os.path.getsize(out) / 1048576.0))
    print("  嵌的 Agent md5=%s  版本=%s" % (md5(agent_exe), ver.get("semver", "?")))
    print("\n装机现场按这个顺序：")
    print("  1) 双击本 exe（会自动请求管理员）→ 填 商户码/密钥/账套/SQL 账号密码 → 「一键安装并启动」")
    print("  2) 日志必须出现「✓ 可以交付」那行（实测账套=配置账套 且 已连上云端）")
    print("  3) 云端后台『商户』页该行的「实测账套」应当等于这一家的账套名")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
