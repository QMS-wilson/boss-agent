# -*- coding: utf-8 -*-
"""打包 V3 本地 Agent 图形界面（V3AgentGui.exe）。

要点：
  --onefile --noconsole  → 双击出窗口，没有黑框
  --add-data config.template.json → 只带**空身份模板**（P1-4：真身份不许进包，见下方注释）
  --add-data VERSION.json  → 版本戳（semver+源码哈希），hello.version 从此有区分度
  --hidden-import        → tkinter/websockets/pymssql 有些子模块 PyInstaller 抓不全
  --paths D:\\v3-wechat   → 让 v3agent 包能被静态分析到
  --runtime-tmpdir       → ★ 解包目录改到固定位置，不再随机生成 _MEIxxxxxx
  --runtime-hook         → ★ 启动时回收历史遗留的解包目录

产物：D:\\v3-wechat\\dist\\V3AgentGui.exe
"""

import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根（跟随脚本位置，可整体搬迁）
NAME = "V3AgentGui"
# ★★ 图标必须在这里给，不能只改 V3AgentGui.spec（2026-10-12 踩到的坑）：
#   本脚本走的是 `python -m PyInstaller <入口.py> --参数…` 这条**命令行**路，
#   PyInstaller 在命令行模式下**会忽略仓库里那份 .spec** ——
#   所以只在 spec 里写 icon= 等于没写（日志里那句 "Copying icon to EXE"
#   拷的是 PyInstaller 自带的默认图标，不是我们的）。
#   同一个 ico 还要 --add-data 进包：exe 自身的图标是编译期嵌的，
#   而**窗口左上角**那个要运行时 iconbitmap 才认（见 agent_gui.app_icon_path）。
ICON = os.path.join(ROOT, "assets", "icons", "v3agent.ico")


def pick_python():
    """挑一个**装了 PyInstaller** 的解释器。

    ★ 踩坑：脚本原来直接 `PY = sys.executable`。如果用一个没装 PyInstaller 的
      解释器去跑本脚本（例如 WorkBuddy 托管的那个精简 Python），
      就会 `No module named PyInstaller` 直接失败。
      这里按顺序探测：当前解释器 → 显式指定 → 系统 Python。
      可用环境变量 V3_BUILD_PYTHON 强制指定。
    """
    import subprocess as _sp

    def has_pyi(p):
        try:
            r = _sp.run([p, "-c", "import PyInstaller,sys;"
                                  "sys.stdout.write(PyInstaller.__version__)"],
                        capture_output=True, timeout=60)
            return r.returncode == 0
        except Exception:
            return False

    cands = []
    env = os.environ.get("V3_BUILD_PYTHON")
    if env:
        cands.append(env)
    cands.append(sys.executable)
    for p in (r"C:\Users\24773\AppData\Local\Programs\Python\Python313\python.exe",
              r"C:\Python313\python.exe"):
        cands.append(p)
    try:
        r = _sp.run(["py", "-3", "-c",
                     "import PyInstaller,sys;sys.stdout.write(sys.executable)"],
                    capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            cands.append(r.stdout.strip())
    except Exception:
        pass

    seen = set()
    for p in cands:
        if not p or p in seen:
            continue
        seen.add(p)
        if os.path.isfile(p) and has_pyi(p):
            return p
    raise SystemExit(
        "找不到装了 PyInstaller 的 Python。\n"
        "请用 `pip install pyinstaller` 装好后，重跑本脚本，\n"
        "或设环境变量 V3_BUILD_PYTHON 指向正确的 python.exe。")


PY = pick_python()

# ★★★ 关于解包目录（--runtime-tmpdir）：**绝对不要设**（2026-09-20 最终结论）
#
#   起因：一开始担心 PyInstaller 默认解包到 %TEMP%\_MEIxxxxxx「名字随机、
#         退出删失败时每份都留下」，于是改设成固定私有目录
#         %LOCALAPPDATA%\V3Agent\runtime —— 想法没错，但**代价惨重**。
#
#   ★★ 实测（同一份 exe，只改这一个参数，三次连跑一致）：
#
#     | 解包位置                                          | 启动耗时 |
#     |--------------------------------------------------|---------|
#     | --runtime-tmpdir %LOCALAPPDATA%\V3Agent\runtime  | 101.3 s |
#     | --runtime-tmpdir %USERPROFILE%\V3Agent-rt        |  99.3 s |
#     | **不设（默认，原地 %TEMP%）**                     | **1.8 s** |
#
#     **相差 56 倍。** 与包体大小、hidden-import、hook 全都无关 ——
#     把同一个 13.9 MB 大包的解包位置换回默认 %TEMP%，立刻从 101 s 变 1.8 s。
#     （详见 scripts/test-rtt-location-speed.py；三次连跑都是 ~99 s，
#       所以**不是**杀软冷启动，是 bootloader 的路径相关行为。）
#
#   这个 101 秒直接造成两个用户可见故障：
#     ① 用户以为程序卡死（启动要等近两分钟）；
#     ② 解包目录存活近 100 秒 → 退出时「被占用/来不及删」概率大增
#        → **这正是弹「Failed to remove temporary directory」的根因**。
#
#   ⇒ 结论：让 bootloader 用它自己的默认目录。残留改由 rthook 在下次启动时，
#     按**内容特征**（顶层含 pymssql/ + websockets/）在 %TEMP% 里认领回收
#     —— %TEMP% 是共享目录，只能按内容认领，绝不能「见 _MEI* 就删」。
#
#   历史踩坑留档（供后续参考，均已不再是当前方案）：
#     【坑一】相对路径相对的是 **CWD** 不是 exe 目录 → 双击时目录散落各处。
#     【坑二】%VAR% 在 Windows 上确实会被展开（POSIX 不会），所以绝对路径能用。
#     两者都「能用」，但都逃不过上面的 56 倍减速 —— 所以整个方案作废。

HIDDEN = [
    "v3agent", "v3agent.main", "v3agent.agent", "v3agent.ops",
    "v3agent.config", "v3agent.sql",
    "agent_daemon", "agent_gui",
    "websockets", "websockets.asyncio", "websockets.asyncio.client",
    "websockets.legacy", "websockets.legacy.client",
    "pymssql", "_mssql",
    "tkinter", "tkinter.scrolledtext", "tkinter.messagebox", "tkinter.ttk",
]


def run(args, **kw):
    print(">>", " ".join(args))
    r = subprocess.run(args, cwd=ROOT, **kw)
    if r.returncode != 0:
        raise SystemExit("命令失败，返回码 %s" % r.returncode)


# ---------------- 版本戳（Phase 4，2026-09-26） ----------------
VERSION_FILE = os.path.join(ROOT, "VERSION")
PKG_INFO_DIR = os.path.join(ROOT, "build", "_pkginfo")


def read_semver():
    """semver 的唯一来源是仓库根的 VERSION 文件（一行三段号）。

    ★ 为什么不让它写在代码里：`__version__` 以前硬编码在 v3agent/__init__.py，
      于是改了代码重打包，版本号一个字都不动，云端与 /admin 完全看不出
      哪家跑的是哪一版 —— 灰度与回滚都没有抓手。
    """
    try:
        v = open(VERSION_FILE, encoding="utf-8").read().strip()
    except OSError:
        raise SystemExit("缺少 %s（写一行三段号，例如 0.2.0）" % VERSION_FILE)
    if not re.match(r"^\d+\.\d+\.\d+([-.+].*)?$", v):
        raise SystemExit("VERSION 内容不合法：%r" % v)
    return v


def content_build_id():
    """构建号 = Agent 源码树内容的 sha256 前 12 位（跟 H5 的 build-id 同一套思路）。

    内容不变 → 号不变（重复打包不会伪装成"新版本"）；
    内容变了 → 号必变，且从云端日志就能认出哪台机器还没换包。
    """
    import hashlib
    paths = []
    for base, _dirs, names in os.walk(os.path.join(ROOT, "v3agent")):
        for n in names:
            if n.endswith(".py"):
                paths.append(os.path.join(base, n))
    for n in ("agent_daemon.py", "agent_gui.py", "VERSION"):
        paths.append(os.path.join(ROOT, n))
    h = hashlib.sha256()
    for p in sorted(paths, key=lambda x: os.path.relpath(x, ROOT).replace("\\", "/")):
        if not os.path.isfile(p):
            continue
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        h.update(("%s\x00" % rel).encode())
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()[:12]


def write_version_json(dirpath, semver, build_id, exe_md5="", built_at=None):
    os.makedirs(dirpath, exist_ok=True)
    body = {"semver": semver, "build_id": build_id,
            "built_at": built_at or time.strftime("%Y-%m-%d %H:%M:%S"),
            "exe_md5": exe_md5, "built_by": os.environ.get("USERNAME") or ""}
    with open(os.path.join(dirpath, "VERSION.json"), "w", encoding="utf-8",
              newline="") as fh:
        json.dump(body, fh, ensure_ascii=False, indent=2)
    return body


def md5_file(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    # ★★ 构建期踩坑（2026-09-20，卡了多次）：
    #   宿主环境有「批量删除需二次确认」保护（阈值 50 个 *条目*），而
    #   PyInstaller 内部把**归档/压缩包**也按内含文件数计数：
    #     · `build\NAME\`             → 703 个条目 → 拦截
    #     · `dist\NAME.exe`（onefile）→ 704 个条目 → 拦截
    #     · `build\NAME\base_library.zip` → 3554 个条目 → 拦截
    #   于是 `--clean`、`rmtree`、`os.remove` 全部返回码 1，构建中断。
    #   ★ 解法：一律**改名，不删除**（rename 不进入删除统计）。
    #     build 目录 → build/NAME.bak-<时间戳>（下次构建前若已存在同名再顺延）
    #     dist 产物  → dist/NAME.prev-<时间戳>.exe
    import datetime
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    for sub in ("build", "dist"):
        d = os.path.join(ROOT, sub)
        os.makedirs(d, exist_ok=True)

    # ★ 图标缺了直接停：否则会打出一份"默认图标"的 exe，装到客户机上才发现，
    #   还得整套重来一遍（打包本身不便宜）。
    if not os.path.isfile(ICON):
        raise SystemExit("找不到图标：%s" % ICON)

    # 1) 挪走 build 缓存目录
    cache = os.path.join(ROOT, "build", NAME)
    if os.path.isdir(cache):
        bak = os.path.join(ROOT, "build", "%s.bak-%s" % (NAME, stamp))
        try:
            os.rename(cache, bak)
            print("build 缓存已改名保留：%s" % os.path.basename(bak))
        except OSError as exc:
            print("build 缓存改名失败（PyInstaller 可能报错）：%s" % exc)

    # 2) 挪走旧产物
    exe_target = os.path.join(ROOT, "dist", NAME + ".exe")
    if os.path.isfile(exe_target):
        bak = os.path.join(ROOT, "dist", "%s.prev-%s.exe" % (NAME, stamp))
        try:
            os.rename(exe_target, bak)
            print("旧产物已改名保留：%s" % os.path.basename(bak))
        except OSError as exc:
            print("旧产物改名失败（将原地覆盖）：%s" % exc)

    semver = read_semver()
    build_id = content_build_id()
    built_at = time.strftime("%Y-%m-%d %H:%M:%S")
    # 包内那份带不了 exe_md5（要先有 exe 才能算），所以 exe 同目录另放一份带 md5 的：
    # v3agent/__init__.py 优先读同目录那份，运维不换包也能覆盖版本信息。
    write_version_json(PKG_INFO_DIR, semver, build_id, "", built_at)
    print("版本戳：semver=%s build_id=%s built_at=%s" % (semver, build_id, built_at))

    args = [
        PY, "-m", "PyInstaller", "--noconfirm", "--onefile", "--noconsole",
        "--name", NAME,
        "--paths", ROOT,
        # ★★ exe 自身的图标（桌面/任务栏/资源管理器）。缺了它，装到客户机上
        #   就是 PyInstaller 那个默认图标 —— 用户会认不出这是哪个程序。
        "--icon", ICON,
        # ★ 同一个 ico 也放进包：运行时要给**窗口左上角**设图标
        #   （exe 图标是编译期嵌的，不会自动成为窗口图标）。
        "--add-data", "%s;assets/icons" % ICON,
        # ★★ 打进包里的是**空身份模板**，不是本地那份 config.json（P1-4，2026-09-26）。
        #   原来 `--add-data config.json;.` 会把开发者机器上的真身份
        #   （agent.name=shiwantongda、sql.database=tongda、真 secret）嵌进 _MEIPASS，
        #   于是"从哪个目录双击 exe"决定服务哪套账，而且每次重新打包都把 tongda 带回去。
        #   换成模板后：exe 同目录没有 config.json ⇒ 直接拒绝启动并说清该填什么。
        "--add-data", "%s;." % os.path.join(ROOT, "config.template.json"),
        # ★ 版本戳一起进包：hello.version 才有区分度（Phase 4）
        "--add-data", "%s;." % os.path.join(PKG_INFO_DIR, "VERSION.json"),
        # ★★★ 这里**故意不传 --runtime-tmpdir**。
        #   实测：显式指定解包目录会让启动从 1.8 s 变成 101 s（慢 56 倍），
        #   并直接引发「Failed to remove temporary directory」弹窗。
        #   详见本文件上方 RUNTIME_TMPDIR 段的完整说明。
        # ★ 启动时回收历史遗留解包目录（在默认 %TEMP% 里按内容特征认领）
        "--runtime-hook", os.path.join(ROOT, "scripts", "rthook_reap_mei.py"),
    ]
    for h in HIDDEN:
        args += ["--hidden-import", h]
    args.append(os.path.join(ROOT, "agent_gui.py"))
    run(args)

    exe = os.path.join(ROOT, "dist", NAME + ".exe")
    assert os.path.isfile(exe), "未生成 %s" % exe
    exe_md5 = md5_file(exe)
    meta = write_version_json(os.path.join(ROOT, "dist"), semver, build_id, exe_md5, built_at)
    print("\n产物：%s  (%.1f MB)" % (exe, os.path.getsize(exe) / 1048576.0))
    print("  exe md5 %s" % exe_md5)
    print("  dist/VERSION.json %s" % json.dumps(meta, ensure_ascii=False)[:150])
    print("  ★ 部署时 deploy-gui.py 会把 exe 与这份 VERSION.json 一起放进发布目录；")
    print("    云端 /admin 看到的就是 `%s+%s`，不再是一律 0.1.0。" % (semver, build_id))
    print()
    print("用法：")
    print("  双击                  → 打开控制台窗口（只是遥控器，可随时关掉）")
    print("  控制台里点『启动』      → 拉起独立的后台 Agent 进程")
    print("                     ★ 关掉控制台窗口，后台 Agent 继续运行")
    print("  控制台里点『停止』      → 关掉后台 Agent")
    print("  --__daemon            → 纯后台模式（由控制台自动调用）")


if __name__ == "__main__":
    main()
