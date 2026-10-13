# -*- coding: utf-8 -*-
"""V3 本地 Agent 图形界面（tkinter）—— 只是控制面板，不是 Agent 本体。

★ 核心设计（2026-09-19 按用户要求重构）

    用户原话：「启动之后应要可以关掉窗口而不影响 agent 的在线状态的，
               不必把这个进程放在前台」

所以所有权变了：
    - **AgentDaemon 是独立的后台进程**，它才是连云端的那个人
    - **本 GUI 只是遥控器**：读状态、点启动/停止
    - **关窗口 = 关遥控器，Agent 照跑** ✓
    - 下次再打开窗口，能看到它还在线、跑了多久、收发了多少

    ┌──────────────────┐         ┌──────────────────────┐
    │ V3AgentGui.exe   │ 读状态  │ AgentDaemon (后台)    │
    │  （控制面板）      │ ──────> │  --__daemon 模式      │
    │  可随时关掉       │         │  连云端、处理请求      │
    │                  │ 写 stop │  status.json          │
    │                  │ <────── │  agent.pid / agent.log│
    └──────────────────┘         └──────────────────────┘

启动方式
    V3AgentGui.exe              打开控制面板
    V3AgentGui.exe --__daemon   以纯后台模式运行（由控制面板调用）
    V3AgentGui.exe --gui         同上，显式打开面板
"""

import json
import os
import re
import sys
import time
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

try:  # 拖拽支持（燕秀标签页要用）。缺库时降级成普通窗口，不影响 Agent 本体。
    from tkinterdnd2 import TkinterDnD
except Exception:  # noqa: BLE001
    TkinterDnD = None

APP_TITLE = "V3 本地 Agent 控制台"

# ---- 窗口尺寸的唯一出处（2026-10-13；依据 scripts/probe-gui-fit.py 量的数）----
WIN_W, WIN_H = 1704, 960          # 初始：两页需求取大（含首页常驻区的动作条）
WIN_MIN_W, WIN_MIN_H = 1150, 660  # 地板：也是 minsize，也是"旧记录比这小就忽略"


def _init_dpi():
    """声明 DPI 感知：高分屏（125%/150%）下窗口与控件不再发虚。

    ★ 必须赶在建根窗口**之前**调用 —— Tk 一旦按系统缩放建过窗口，再声明也晚了。
    ★ 与 v3agent/yxparts/ui.py 的 _init_dpi() 用同一套写法：那个模块里本来就有，
      只有控制台页缺 —— 结果是同一个程序里燕秀页清晰、控制台页发虚。
    """
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass          # 老系统没有 shcore.dll，静默跳过


def app_icon_path():
    """窗口图标 .ico 的路径。

    ★ 为什么还要单独找一次：PyInstaller 打包时 exe 自己的图标是**编译期嵌进去的**，
      但 Tk 窗口默认仍显示 Tcl/Tk 那个图标 —— 必须在运行时显式 iconbitmap 一次。
      onefile 下资源在 sys._MEIPASS，开发态在 assets/icons/。
    """
    for d in (getattr(sys, "_MEIPASS", "") or "", base_dir(),
              os.path.join(base_dir(), "assets", "icons")):
        if not d:
            continue
        for n in ("v3agent.ico", "v3agent_A.ico"):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return None


def _set_window_icon(root):
    p = app_icon_path()
    if not p:
        return
    try:
        root.iconbitmap(default=p)
    except Exception:  # noqa: BLE001
        try:
            root.iconbitmap(p)
        except Exception:  # noqa: BLE001
            pass


def _default_geometry(root):
    """窗口初始尺寸 —— 一个数说了算，切页不再变。

    ★ 原来这里挂着**四个**互相不服的尺寸（本函数 1020x830、恢复地板 900x640、
      切到燕秀页时 `_fit_window` 兜底 1150x660、燕秀独立窗 1360x800），
      表现就是"每切一次页、每重开一次，窗口大小都在变"。
    ★ 1704 x 902 是**量出来的**（`scripts/probe-gui-fit.py`，两页各自需求取大）：
        首页 操作行需 1542 / 账套行需 1637 / 带「在线用户」常驻区自然高 902
        燕秀页 表格 13 列需 1704
      改这两页的控件之后必须重跑那支门，否则加的按钮又会把内容挤出去。
    ★ 小屏幕（1366x768 的笔记本）夹到屏内：宁可表格横向滚，也不让窗口超出桌面。
    """
    try:
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
    except Exception:  # noqa: BLE001
        return "%dx%d" % (WIN_W, WIN_H)
    return "%dx%d" % (max(WIN_MIN_W, min(WIN_W, sw - 40)),
                      max(WIN_MIN_H, min(WIN_H, sh - 80)))


def _make_root():
    """建根窗口：有 tkinterdnd2 就用它的 Tk（子控件才能收文件拖拽），否则退回普通 Tk。"""
    root = None
    if TkinterDnD is not None:
        try:
            root = TkinterDnD.Tk()
        except Exception:  # noqa: BLE001
            root = None
    if root is None:
        root = tk.Tk()
    _set_window_icon(root)
    return root


# ------------------------------------------------------------------ 路径

def base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_config():
    for d in (base_dir(), getattr(sys, "_MEIPASS", ""), os.getcwd(),
              os.path.dirname(os.path.abspath(__file__))):
        if not d:
            continue
        p = os.path.join(d, "config.json")
        if os.path.isfile(p):
            return p
    return None


def ensure_import_path():
    for d in (base_dir(), getattr(sys, "_MEIPASS", ""), os.path.dirname(os.path.abspath(__file__))):
        if d and os.path.isdir(os.path.join(d, "v3agent")) and d not in sys.path:
            sys.path.insert(0, d)


def seed_config():
    """★ 首启自动在本目录建一份 config.json（2026-09-26 按用户要求改的）。

    改之前的行为：把 exe 单独拷出来双击 → 红字「找不到 config.json，请复制模板或
    用安装器装」。用户的意思是「这种应该自创建，然后配置直接在 gui 界面填入应用」。

    ★ 自创建的这份**不含任何商户身份与凭据**（`agent.name`/`agent.secret`/
      `sql.database`/`sql.user`/`sql.password` 全空，见 `config.first_run_config`），
      所以它开得了界面、上不了线 —— P1-4 那条红线一点没松：
      带着默认值悄悄连上云端、用 A 家的名字读 B 家的账，才是这个项目最贵的故障。
    ★ 只在**本目录没有** config.json 时建，绝不定覆盖已有配置（那台机器可能正在跑）。

    返回 (路径或 None, 失败原因)。
    """
    target = os.path.join(base_dir(), "config.json")
    if os.path.isfile(target):
        return target, ""
    tpl = None
    for d in (base_dir(), getattr(sys, "_MEIPASS", "")):
        p = os.path.join(d or "", "config.template.json")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    tpl = json.load(fh)
                break
            except Exception:  # noqa: BLE001
                tpl = None
    ensure_import_path()
    try:
        from v3agent import config as cfgmod
        cfg = cfgmod.first_run_config(tpl)
    except Exception as exc:  # noqa: BLE001
        return None, "读配置模板失败：%s" % exc
    try:
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except Exception as exc:  # noqa: BLE001
        # Program Files 之类只读目录会走到这里 —— 说清下一步，别只报个 winerror
        return None, ("建不出 %s：%s\n  这个目录不可写。把 V3AgentGui.exe 拷到一个可写目录"
                      "（例如 C:\\V3Agent）再双击，或用 --config 指到能写的地方。"
                      % (target, exc))
    return target, ""


def runtime_dir():
    return _dm().runtime_dir()


def _dm():
    """拿到 agent_daemon 模块（延迟导入，避免循环依赖）。"""
    ensure_import_path()
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import agent_daemon
    return agent_daemon


# ================================================================ 状态翻译（2026-10-12）
#
# ★★ 本次重构的核心。
#
# 后台 Agent 早就把"结论"写进 status.json 了：
#   * state      —— 13 种明确取值，由 daemon 判定（agent_daemon.py L1107-1518 逐个赋值）
#   * last_err   —— daemon 写好的**人话原因**（如"云端不认本机 agent.secret（多半是后台重置过密钥）"）
#   * logs       —— 带 level 的最近 80 条，能数出"同一件事重复了多少次"
#
# 而旧界面只判 read_pid()（进程在不在）：进程活着就画绿色「后台运行中」，
# 于是"一条请求都处理不了"的机器顶栏照样是绿的 —— 界面给出了最乐观的错误结论。
# 下面这几张表只做"翻译"，不重新判断、不猜。

# level: ok 正常 / warn 正在自动恢复 / err 要人动手 / stop 进程不在
STATE_LEVEL = {
    "启动中": ("ok", "正在启动"),
    "连接中…": ("ok", "正在连接"),
    "已连接": ("ok", "已连云端"),
    "重连中": ("warn", "正在重连"),
    "被顶号": ("warn", "被顶号"),
    "已停止": ("stop", "未运行"),
    "重复实例": ("err", "重复实例"),
    "名称被占用": ("err", "名称被占用"),
    "密钥不符": ("err", "密钥不符"),
    "配置缺失": ("err", "配置缺失"),
    "配置不可用": ("err", "配置不可用"),
    "依赖缺失": ("err", "依赖缺失"),
    "异常退出": ("err", "异常退出"),
}

# 每个"要人动手"的状态对应一条提示：结论（title）+ 处置（desc）。
# ★ "acts" 是**历史遗留**：诊断条已经不放按钮了（动作统一收在操作行），
#   它不参与当前渲染，留着是为了以后 C-2「在线用户」弹窗复用这套动作键。
STATE_DIAG = {
    "密钥不符": {
        "title": "云端不认本机的 Agent 密钥",
        "desc": "去云端后台『商户』页重新取这个商户的 Agent 密钥，填进『本机配置』的"
                "『Agent 密钥』一栏，保存后重新启动 Agent。",
        "acts": ["cfg", "log"],
    },
    "名称被占用": {
        "title": "这个商户码已经被另一台机器占用",
        "desc": "同一时刻一个商户码只允许一条连接。先确认该跑的是哪台机器："
                "如果不是本机，在『本机配置』里把商户码改成本机对应的那一个。",
        "acts": ["cfg", "log"],
    },
    "重复实例": {
        "title": "本机有多个 Agent 在抢同一个商户码",
        "desc": "多个实例会互相顶掉、谁都连不稳。点『停止全部 Agent』把本安装目录"
                "清干净，再重新启动。",
        "acts": ["stopall", "log"],
    },
    "被顶号": {
        "title": "同一个商户码有两条连接在抢",
        "desc": "点『停止全部 Agent』清掉本安装目录的残留，再重新启动；"
                "如果本机确实只有一个实例，就去另一台机器上找。",
        "acts": ["stopall", "log"],
    },
    "配置缺失": {
        "title": "配置里缺必填项，Agent 起不来",
        "desc": "在『本机配置』里补齐标出的那几项，保存后再启动。",
        "acts": ["cfg", "log"],
    },
    "配置不可用": {
        "title": "配置文件读不了或内容不对",
        "desc": "打开配置文件核一下 JSON 是否完整（有没有多逗号、少引号）；"
                "改完保存再启动。",
        "acts": ["cfgfile", "cfg", "log"],
    },
    "依赖缺失": {
        "title": "这台机器缺运行库",
        "desc": "把依据行里的原始报错发给平台方 —— 这一类重装 exe 通常解决不了。",
        "acts": ["log"],
    },
    "异常退出": {
        "title": "Agent 上一次是异常退出的",
        "desc": "打开日志看退出前最后几条，再点『启动』。",
        "acts": ["log"],
    },
}

# 状态带左侧那句话
HEADLINE = {"ok": "服务正常", "warn": "正在自动恢复",
            "err": "服务已中断", "stop": "未运行"}
HEADLINE_FG = {"ok": "#15803d", "warn": "#b45309",
               "err": "#b91c1c", "stop": "#64748b"}
DOT_FG = {"ok": "#22c55e", "warn": "#f59e0b", "err": "#ef4444", "stop": "#94a3b8"}
# 顶栏药丸徽章的 (底色, 字色)
BADGE_BG = {"ok": ("#14532d", "#bbf7d0"), "warn": ("#78350f", "#fde68a"),
            "err": ("#7f1d1d", "#fecaca"), "stop": ("#334155", "#e2e8f0")}


# ------------------------------------------------------------------ 控制面板

class Console:
    def __init__(self, root, body=None):
        self.root = root
        # body = 真正装控件的容器（标签页里是子 Frame）；root 只做窗口级操作
        self.body = body if body is not None else root
        root.title(APP_TITLE)
        # ★ 尺寸由屏幕算（不再写死 820x600）：新界面内容更多，600 高会截断
        root.geometry(_default_geometry(root))
        root.minsize(WIN_MIN_W, WIN_MIN_H)
        self.dm = _dm()
        self.cfg_path = None
        self.cfg = None
        self._db_scan = None          # 发现线程句柄
        self._db_box = []             # 线程回传结果（避免跨线程碰 tkinter 控件）
        self._db_names = []
        self._db_note = ""
        self._last_log_count = 0
        # 本机配置面板的状态（首启填表用）
        self.cfg_vars = {}
        self.cfg_prefill = {}
        self.cfg_ready = False
        self.show_pwd = False
        self._sql_box = []
        self._sql_scan = None
        self._sql_tip = ""
        # ---- 日志缓冲（2026-10-12：加了筛选/搜索后，日志不再直写控件）----
        self._log_buf = []            # [(hh:mm:ss 或 daemon 的 t, msg, level)]
        self._log_after = None        # after_idle 句柄（把同一批写入合并成一次重绘）
        self._log_dirty = False
        self._log_filter = "all"
        self._log_paused = tk.BooleanVar(value=False)
        self._log_kw = tk.StringVar()
        self._log_chips = {}          # {"all"/"info"/...: 标签控件}（在弹窗里）
        # ---- 日志改弹窗（2026-10-12）：主界面不再有日志区 ----
        self.log_win = None           # 日志弹窗（Toplevel）；没开就是 None
        self.txt = None               # 文本区在弹窗里，主界面没有它
        self.btn_log = None           # 「查看日志」按钮（要更新错误计数角标）
        # ---- 在线用户弹窗（2026-10-12，C-2）----
        self.users_box = None
        self.users_tree = None
        # 换账套之后要通知燕秀面板丢掉物料库（面板是 main() 里延迟建的，只能挂回调）
        self.on_db_switched = None

        self.users_stat = None
        self._users_rows = []
        # ---- 诊断条（不放按钮；正常态整块不占位）----
        self.diag = None
        self._badge_level = "stop"
        self._probe_after = None      # 「重新探测」结果回退定时器
        self._setup_style()
        self._build_ui()
        self._restore_geometry()
        self._load_config()
        self.discover_dbs()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(300, self._tick)
        self.log("控制台已就绪。", "info")
        self.log("提示：关掉本窗口不会影响 Agent —— 它会继续在后台运行。", "meta")

    # ---- 样式（2026-10-12）

    def _setup_style(self):
        """统一控件观感。

        ★ 必须先 theme_use("clam")：vista / default 主题下 ttk 控件的 background
          **根本不生效**（原生主题交给系统绘制），想自己配色只能换 clam。
          这也是"按钮换成 ttk 之后还是灰的"的根因。
        ★ Style 是全进程共享的 —— 燕秀页（v3agent/yxparts/ui.py）的 Treeview /
          Combobox 会跟着一起变，改完必须两页都看一遍。
        """
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:  # noqa: BLE001
            pass
        ui = "Microsoft YaHei UI"
        st.configure("TButton", font=(ui, 10), padding=(10, 5),
                     background="#ffffff", foreground="#334155",
                     bordercolor="#cbd5e1", borderwidth=1, relief="solid",
                     focusthickness=0, focuscolor="#ffffff")
        st.map("TButton",
               background=[("disabled", "#f1f5f9"), ("pressed", "#e2e8f0"),
                           ("active", "#f8fafc")],
               foreground=[("disabled", "#94a3b8")],
               bordercolor=[("disabled", "#e2e8f0")])
        for name, bg, bd in (("Primary", "#16a34a", "#15803d"),   # 启动
                             ("Stop", "#ea580c", "#c2410c"),      # 停止
                             ("Accent", "#1d4ed8", "#1e40af"),    # 本机配置
                             ("Danger", "#b91c1c", "#991b1b"),    # 停止全部 / 危险
                             ("Teal", "#0f766e", "#115e59")):     # 现场自检
            st.configure(name + ".TButton", font=(ui, 10, "bold"), padding=(13, 6),
                         background=bg, foreground="#ffffff",
                         bordercolor=bd, borderwidth=0, focuscolor=bg)
            st.map(name + ".TButton",
                   background=[("disabled", "#e8e8e8"), ("pressed", bd), ("active", bg)],
                   foreground=[("disabled", "#9a9a9a")])
        st.configure("TEntry", padding=(5, 4), fieldbackground="#ffffff")
        st.configure("TCombobox", padding=(6, 3))
        st.configure("TCheckbutton", font=(ui, 9), background="#ffffff",
                     foreground="#334155")
        st.map("TCheckbutton", background=[("active", "#ffffff")])

        # ---- Treeview：行高必须按字体度量算（2026-10-13）----
        # ★ 症状：在线用户那张表每行下半截被切，两行看就是一团重叠。
        #   根因是 ttk.Treeview 的 rowheight 来自主题默认值，**不跟着你设的字体放大**，
        #   而这台机是 微软雅黑 10 + 1.5 倍 DPI（量出来 linespace=35，默认行高装不下）。
        #   ⇒ 写死一个数（比如 28）也一样会随字号/DPI 漂回去，所以按度量算。
        # ★ Style 是全进程共享的：这一改同时作用到燕秀页的表格 —— 那张表的行
        #   本来也被切，一起变好；改完两页都要看（scripts/check-gui-fit.py 会量）。
        try:
            from tkinter import font as _tkfont
            from v3agent.ui_metrics import tree_rowheight
            _tf = _tkfont.Font(family=ui, size=10)
            st.configure("Treeview", rowheight=tree_rowheight(self.root),
                         font=_tf, padding=(6, 3))
            st.configure("Treeview.Heading",
                         font=_tkfont.Font(family=ui, size=10, weight="bold"),
                         padding=(6, 5))
            self.tree_font = _tf
        except Exception:  # noqa: BLE001  拿不到字体度量就用主题默认，不影响功能
            self.tree_font = None

    # ---- 窗口尺寸记忆
    #
    # ★ 为什么单独存一个文件、不写进 config.json：config.json 是**运行配置**
    #   （商户身份 / 凭据 / 账套），把界面尺寸写进去会污染它，还会被
    #   self._merged_cfg() 当成"用户改过的字段"回写 —— 那是真事故。

    def _ui_state_path(self):
        return os.path.join(runtime_dir(), "ui_state.json")

    # 恢复尺寸时的下限：小于它就忽略旧记录（加了新控件之后小窗口会截断）
    MIN_W, MIN_H = WIN_MIN_W, WIN_MIN_H   # ★ 与 minsize 同一个数，别再各写一份

    def _restore_geometry(self):
        try:
            with open(self._ui_state_path(), "r", encoding="utf-8") as fh:
                g = str((json.load(fh) or {}).get("geometry") or "").strip()
        except Exception:  # noqa: BLE001
            return False
        m = re.match(r"^(\d+)x(\d+)([+-]-?\d+[+-]-?\d+)?$", g)
        if not m:
            return False
        if int(m.group(1)) < self.MIN_W or int(m.group(2)) < self.MIN_H:
            return False          # 旧记录太小 ⇒ 忽略它，用屏幕算出来的默认尺寸
        try:
            self.root.geometry(g)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _save_geometry(self):
        try:
            p = self._ui_state_path()
            data = {}
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as fh:
                        data = json.load(fh) or {}
                except Exception:  # noqa: BLE001
                    data = {}
            data["geometry"] = self.root.geometry()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass

    # ---- UI

    def _build_ui(self):
        # ---------- 顶栏：品牌 + 状态药丸 ----------
        top = tk.Frame(self.body, bg="#0f172a")
        top.pack(fill="x")
        tk.Label(top, text=APP_TITLE, bg="#0f172a", fg="white",
                 font=("Microsoft YaHei UI", 14, "bold")).pack(side="left", padx=16, pady=11)
        self.lbl_ver = tk.Label(top, text="", bg="#0f172a", fg="#64748b",
                                font=("Microsoft YaHei UI", 9))
        self.lbl_ver.pack(side="right", padx=(0, 14))
        self.badge = tk.Frame(top, bg="#334155")
        self.badge.pack(side="right", padx=(0, 6), pady=10)
        self.lbl_dot = tk.Label(self.badge, text="●", bg="#334155", fg="#94a3b8",
                                font=("Microsoft YaHei UI", 10))
        self.lbl_dot.pack(side="left", padx=(10, 4), pady=3)
        self.lbl_state = tk.Label(self.badge, text="检测中…", bg="#334155", fg="#e2e8f0",
                                  font=("Microsoft YaHei UI", 10, "bold"))
        self.lbl_state.pack(side="left", padx=(0, 11), pady=3)

        # ---------- 提示 / 诊断条（只两行；不需要时整块不出现）----------
        self._build_diag()

        # ---------- 状态行 ----------
        self._build_strip()

        # ---- 本机配置（首启填表 / 改配置，2026-09-26）----
        # ★ 为什么给它一个常驻槽位而不是弹模态窗：填到一半想去看状态、想点「重新发现」，
        #   模态窗会把人关在里面。面板收起时只是个空槽，不占地方。
        self.cfg_slot = tk.Frame(self.body)
        self.cfg_slot.pack(fill="x", padx=14, pady=(2, 0))
        self._build_cfg_panel()

        # ---------- 账套（2026-09-24）----------
        self._build_dbrow()

        # ---------- 操作（日志改弹窗了，不在这里占位置）----------
        self._build_btns()

    def _build_diag(self):
        """提示条 —— 一行结论 + 指向；故障时多一行依据。**不放按钮**。

        ★ 为什么去掉按钮：原来那四个（启动 / 本机配置 / 查看日志 / 停止全部）
          与下面操作行和「更多」菜单里的**完全重复**，等于让用户在两处找同一个功能。
          现在这里只负责"说清发生了什么、该去点哪里"，动作统一收在操作行。

        ★ 也不新写诊断逻辑：只读 daemon 已经算好的 status.json
          （state 13 种取值 + last_err 人话原因 + logs 重复次数）。
        """
        ui = "Microsoft YaHei UI"
        self.diag = tk.Frame(self.body, bg="#eff6ff",
                             highlightbackground="#93c5fd", highlightthickness=1)
        self.diag_inner = tk.Frame(self.diag, bg="#eff6ff")
        self.diag_inner.pack(fill="x", padx=12, pady=7)

        line = tk.Frame(self.diag_inner, bg="#eff6ff")
        line.pack(fill="x")
        self.diag_ic = tk.Label(line, text="i", bg="#dbeafe", fg="#1e40af",
                                font=(ui, 10, "bold"), width=2)
        self.diag_ic.pack(side="left", padx=(0, 9))
        self.diag_title = tk.Label(line, text="", bg="#eff6ff", fg="#1e40af",
                                   font=(ui, 11, "bold"), anchor="w")
        self.diag_title.pack(side="left")
        self.diag_desc = tk.Label(line, text="", bg="#eff6ff", fg="#1e3a8a",
                                  font=(ui, 9), anchor="w", justify="left",
                                  wraplength=700)
        self.diag_desc.pack(side="left", padx=(9, 0))
        self.diag_ev = tk.Label(self.diag_inner, text="", bg="#dbeafe", fg="#1d4ed8",
                                font=("Consolas", 9), anchor="w", justify="left",
                                wraplength=780, padx=8, pady=3)
        # 依据行刻意不 pack —— 由 _render_diag() 决定要不要出现

    def _build_strip(self):
        """状态行 —— 就一行：左边一句结论，右边一串事实。

        ★ 原来是四行散文本 + 四个指标块（要 90px 高）。压成一行之后，
          日志区才拿得到足够高度，不会再有"显示不全"。
        """
        ui = "Microsoft YaHei UI"
        strip = tk.Frame(self.body, bg="#ffffff")
        strip.pack(fill="x", padx=14, pady=(10, 0))
        self.strip = strip
        self.lbl_headline = tk.Label(strip, text="检测中…", bg="#ffffff", fg="#0f172a",
                                     font=(ui, 15, "bold"), anchor="w")
        self.lbl_headline.pack(side="left")
        self.lbl_facts = tk.Label(strip, text="", bg="#ffffff", fg="#64748b",
                                  font=(ui, 10), anchor="w")
        self.lbl_facts.pack(side="left", padx=(14, 0))

    def _build_dbrow(self):
        """账套选择（2026-09-24）。

        ★ 为什么放在 GUI 上：以前换账套要么手改 config.json、要么去点另一个目录的
          exe（两份 exe 各带一套库、还共用同一个注册名互相踢），出错时数字看着都
          正常但完全是另一套账。这里把选择收敛成一个动作，列表来自
          sys.databases + 业务表四件套实测，不让手输错库名。
        """
        ui = "Microsoft YaHei UI"
        dbrow = tk.Frame(self.body)
        dbrow.pack(fill="x", padx=14, pady=(9, 0))
        tk.Label(dbrow, text="账套", font=(ui, 10, "bold"),
                 fg="#0f172a").pack(side="left")
        self.cmb_db = ttk.Combobox(dbrow, width=16, state="readonly", values=[])
        self.cmb_db.pack(side="left", padx=6)
        for text, cmd, bg in (("切换", self.switch_db, "#7c3aed"),
                              ("重新发现", self.discover_dbs, "#ffffff")):
            fg = "#334155" if bg == "#ffffff" else "#ffffff"
            b = tk.Button(dbrow, text=text, command=cmd, bg=bg, fg=fg,
                          relief="flat", bd=0, width=8, padx=4, pady=3, cursor="hand2",
                          font=(ui, 9, "bold" if bg != "#ffffff" else "normal"),
                          activebackground=bg, activeforeground=fg)
            if bg == "#ffffff":
                b.configure(highlightbackground="#cbd5e1", highlightthickness=1,
                            activebackground="#eff6ff")
            b.pack(side="left", padx=(0, 5))
        # ★ 这两个标签原来一个 pack 左边、一个 pack 右边挤在同一行 ——
        #   「允许读 3 本：shiwantongda、tongda、geshi」这种长文案一来，就和右侧
        #   「配置：<路径> Agent 名：…」直接重叠（2026-10-13 截图才看得见）。
        #   次要信息降到下面一行小字，并按窗口宽度回行，不跟主操作抢同一行。
        # ★ 必须**直接建在这个框里**：Tk 不允许把控件 pack 进不是它父容器的框
        #   （先建在 dbrow 再 `pack(in_=db_sub)` 会直接 TclError，本轮撞过）。
        self.db_sub = tk.Frame(self.body, bg="#ffffff")
        self.db_sub.pack(fill="x", padx=14, pady=(2, 0))
        self.lbl_db = tk.Label(self.db_sub, text="当前：—", anchor="w", fg="#64748b",
                               font=(ui, 9), justify="left")
        self.lbl_db.pack(side="top", anchor="w")
        self.lbl_cfg = tk.Label(self.db_sub, text="配置：—", anchor="w", fg="#94a3b8",
                                font=(ui, 9), justify="left")
        self.lbl_cfg.pack(side="top", anchor="w")
        self._db_sub_bind_width()
        self.body.bind("<Configure>", lambda e: self._db_sub_bind_width(e.width), add="+")

    def _db_sub_bind_width(self, width=None):
        """让第二行小字跟着窗口宽度回行（不回行就会顶出右边界，看着像被裁）。"""
        for lbl in (getattr(self, "lbl_db", None), getattr(self, "lbl_cfg", None)):
            if lbl is not None:
                try:
                    lbl.configure(wraplength=max(400, int(width or 0) - 40))
                except Exception:  # noqa: BLE001
                    pass

    def _build_btns(self):
        """操作行：常用功能平铺 ＋「更多 ▾」收纳低频功能。

        ★ 为什么这次用菜单：要整洁就得取舍 —— 常用的一眼可见，
          低频（看日志文件 / 开配置文件 / 停全部实例）收进菜单，不占版面。
        ★ 刻意**保留**"启动/停止 用 disabled 联动"这套机制（见 _refresh）——
          它本来就是对的。若改成"隐藏另一个按钮"，_refresh 必须同步改，
          否则 configure 到已销毁的控件 ⇒ 启停按钮直接失效。
        """
        ui = "Microsoft YaHei UI"
        btns = tk.Frame(self.body)
        btns.pack(fill="x", padx=14, pady=10)

        def mk(text, cmd, bg, bold=False):
            """扁平按钮。★ 用 tk.Button 而不是 ttk.Button ——
            clam 主题下的 ttk 按钮是灰扁方块，观感反而比原生更朴素；
            tk.Button 配 relief="flat" 才能做出纯色扁平块。"""
            fg = "#334155" if bg == "#ffffff" else "#ffffff"
            b = tk.Button(btns, text=text, command=cmd, bg=bg, fg=fg,
                          relief="flat", bd=0, padx=15, pady=7, cursor="hand2",
                          font=(ui, 10, "bold" if bold else "normal"),
                          activebackground=bg, activeforeground=fg,
                          disabledforeground="#9aa4b2")
            if bg == "#ffffff":
                b.configure(highlightbackground="#cbd5e1", highlightthickness=1,
                            activebackground="#eff6ff")
            b.pack(side="left", padx=(0, 8))
            return b

        # ★ 主操作：实心大按钮。仍由 _refresh 设 state（既有 disabled 联动不变）
        self.btn_start = mk("启动后台 Agent", self.start, "#16a34a", bold=True)
        self.btn_stop = mk("停止后台 Agent", self.stop, "#ea580c", bold=True)
        tk.Frame(btns, width=1, bg="#cbd5e1").pack(side="left", fill="y", padx=4)
        mk("重新探测", self.probe_now, "#ffffff")
        mk("本机配置", lambda: self.show_cfg(True), "#1d4ed8", bold=True)
        mk("现场自检", self.run_selfcheck, "#0f766e", bold=True)
        # ★ 日志搬进弹窗了，这里只留一个入口；按钮文字带错误计数角标
        self.btn_log = mk("查看日志", self.open_log_window, "#ffffff")
        # ★「在线用户」不再是按钮：它已经搬进首页常驻区（见 _build_users_panel），
        #   留着就是同一个功能两处入口。
        self._build_users_panel()

        mb = ttk.Menubutton(btns, text="更多 ▾", width=9)
        menu = tk.Menu(mb, tearoff=0)
        menu.add_command(label="打开日志文件（原始 txt）", command=self.open_log)
        menu.add_command(label="打开配置文件", command=self.open_cfg)
        menu.add_separator()
        menu.add_command(label="停止全部 Agent（清残留）", command=self.stop_all)
        mb["menu"] = menu
        mb.pack(side="left", padx=(0, 8))

    # ---- 日志弹窗（2026-10-12：从主界面搬出来）
    #
    # ★ 为什么搬：日志区要 300px 高，主界面装不下 —— 那正是"显示不全"的根源。
    #   搬走之后主界面只剩四行，而且**日志和操作可以同时看着**（边点启动边看）。
    # ★ 非模态（不加 grab_set）：加了 grab 就变成"必须先关日志窗才能点主界面"，
    #   正好把"边看边操作"这个唯一的好处毁掉。

    def open_log_window(self):
        """打开日志窗口；已经开着就把它提到前面（不重复开第二个）。"""
        if self.log_win is not None:
            try:
                if self.log_win.winfo_exists():
                    self.log_win.lift()
                    self.log_win.focus_set()
                    return
            except Exception:  # noqa: BLE001
                pass
            self.log_win = None

        ui = "Microsoft YaHei UI"
        win = tk.Toplevel(self.root)
        self.log_win = win
        win.title("后台 Agent 日志 · " + APP_TITLE)
        win.geometry("900x520")
        win.minsize(640, 300)
        _set_window_icon(win)

        bar = tk.Frame(win)
        bar.pack(fill="x", padx=12, pady=(10, 0))
        for key, text in (("all", "全部"), ("info", "信息"), ("req", "请求"),
                          ("warn", "警告"), ("error", "错误")):
            chip = tk.Label(bar, text=text, fg="#334155", bg="#ffffff",
                            font=(ui, 9), padx=9, pady=2, cursor="hand2",
                            highlightbackground="#cbd5e1", highlightthickness=1)
            chip.pack(side="left", padx=(0, 5))
            chip.bind("<Button-1>", lambda _e, k=key: self.set_log_filter(k))
            self._log_chips[key] = chip
        ttk.Checkbutton(bar, text="暂停", variable=self._log_paused,
                        command=self._render_log).pack(side="right")
        ttk.Button(bar, text="清空", width=5,
                   command=self.clear_log).pack(side="right", padx=6)
        ent = ttk.Entry(bar, textvariable=self._log_kw, width=20)
        ent.pack(side="right")
        ent.bind("<KeyRelease>", lambda _e: self._render_log())
        tk.Label(bar, text="搜索", fg="#64748b",
                 font=(ui, 9)).pack(side="right", padx=(0, 4))

        self.txt = scrolledtext.ScrolledText(win, state="disabled",
                                             font=("Consolas", 10), bg="#ffffff",
                                             fg="#0f172a", relief="flat",
                                             highlightbackground="#e2e8f0",
                                             highlightthickness=1)
        self.txt.pack(fill="both", expand=True, padx=12, pady=(6, 12))
        for tag, color in (("info", "#334155"), ("ok", "#15803d"), ("warn", "#b45309"),
                           ("error", "#b91c1c"), ("req", "#1d4ed8"), ("meta", "#64748b")):
            self.txt.tag_config(tag, foreground=color)
        win.protocol("WM_DELETE_WINDOW", self._close_log_window)
        self.set_log_filter(self._log_filter)     # 重绘 + 同步 chip 选中态

    def _close_log_window(self):
        """关掉弹窗 —— ★ 只是不看了：缓冲照收，主界面按钮上的角标继续跳。"""
        win, self.log_win = self.log_win, None
        self.txt = None
        self._log_chips = {}
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                pass

    # ---- 在线用户弹窗（2026-10-12 · C-2）
    #
    # ★ 数据来自云端（/agent/sessions）：登录会话的所有权在云端 ——
    #   本地只是把它取过来显示、并把"踢出"这个动作转达回去。
    # ★ 用**现有的** agent.secret 调，不为这个功能新发一套密钥
    #   （少一套密钥 = 少一处会在换机器时忘记改的东西）。
    # ★ 三个动作分开给：踢这条 / 踢该账号全部 / 禁用账号 ——
    #   合成一个必然出现"我只想踢他下线，结果把人账号禁了"。

    def _cloud_base(self):
        """云端 http 基地址（由 relay_url 换算）。"""
        st = self.dm.read_status() or {}
        url = str(st.get("relay_url") or "").strip() or str(
            ((self.cfg or {}).get("agent") or {}).get("relay_url") or "")
        if not url:
            return ""
        base = url.replace("wss://", "https://").replace("ws://", "http://")
        return base.split("/ws/")[0].rstrip("/")

    def _cloud_post(self, path, payload=None, timeout=8):
        """带 agent 身份调云端；返回 (ok, 数据 或 失败原因)。"""
        import urllib.error
        import urllib.request
        base = self._cloud_base()
        if not base:
            return False, "云端地址未知（配置里没有 agent.relay_url）"
        a = (self.cfg or {}).get("agent") or {}
        body = dict(payload or {})
        body["agent"] = a.get("name") or ""
        body["secret"] = a.get("secret") or ""
        if not body["agent"] or not body["secret"]:
            return False, "配置里缺 agent.name 或 agent.secret"
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                data = {}
            return False, str(data.get("error") or ("HTTP %d" % exc.code))
        except Exception as exc:  # noqa: BLE001
            return False, "连不上云端（%s）" % type(exc).__name__
        if not data.get("ok"):
            return False, str(data.get("error") or "云端返回失败")
        return True, data

    # ---- 在线用户（2026-10-13：从弹窗搬进主界面，做成常驻区）----
    #
    # ★ 为什么常驻、不要弹窗：这一页回答的是"谁在用我这本账"，是开机就该看见的状态。
    #   藏在弹窗里 ⇒ 每次先点按钮、弹窗和主窗抢焦点、一关信息就没了。
    #   形状照当年日志区那块：标题条 + 表格 + 动作条。
    # ★ 数据来自云端（/agent/sessions）：登录会话的所有权在云端 ——
    #   本地只是把它取过来显示、并把"踢出"这个动作转达回去。
    # ★ 三个动作分开给：踢这条 / 踢该账号全部 / 禁用账号 ——
    #   合成一个必然出现"我只想踢他下线，结果把人账号禁了"。

    USERS_HEADS = (("state", "状态"), ("who", "账号"), ("kind", "类型"),
                   ("db", "账套"), ("dev", "设备 / 端"), ("ip", "来源 IP"),
                   ("login", "登录于"), ("idle", "最后活跃"))

    def _build_users_panel(self):
        ui = "Microsoft YaHei UI"
        box = tk.Frame(self.body, bg="#ffffff",
                       highlightbackground="#cbd5e1", highlightthickness=1)
        box.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.users_box = box

        bar = tk.Frame(box, bg="#f8fafc")
        bar.pack(fill="x")
        tk.Label(bar, text="在线用户", bg="#f8fafc", fg="#0f172a",
                 font=(ui, 11, "bold")).pack(side="left", padx=(10, 0), pady=6)
        tk.Label(bar, text="正在使用 App 的 V3 操作员", bg="#f8fafc",
                 fg="#64748b", font=(ui, 9)).pack(side="left", padx=(8, 0))
        self.users_stat = tk.Label(bar, text="—", bg="#f8fafc", fg="#475569", font=(ui, 9))
        self.users_stat.pack(side="right", padx=(0, 10))
        ttk.Button(bar, text="刷新", width=7,
                   command=self._reload_users).pack(side="right", padx=8)

        cols = [k for k, _t in self.USERS_HEADS]
        self.users_tree = ttk.Treeview(box, columns=cols, show="headings", height=7)
        for key, text in self.USERS_HEADS:
            self.users_tree.heading(key, text=text)
            self.users_tree.column(key, width=90, anchor="w", stretch=(key == "who"))
        self.users_tree.pack(fill="both", expand=True, padx=8, pady=(6, 0))
        self.users_tree.tag_configure("off", foreground="#94a3b8")

        acts = tk.Frame(box, bg="#ffffff")
        acts.pack(fill="x", padx=8, pady=8)

        def abtn(text, cmd, bg="#ffffff"):
            fg = "#334155" if bg == "#ffffff" else "#ffffff"
            b = tk.Button(acts, text=text, command=cmd, bg=bg, fg=fg, relief="flat",
                          bd=0, padx=12, pady=5, cursor="hand2",
                          font=(ui, 10, "bold" if bg != "#ffffff" else "normal"),
                          activebackground=bg, activeforeground=fg)
            if bg == "#ffffff":
                b.configure(highlightbackground="#cbd5e1", highlightthickness=1)
            b.pack(side="left", padx=(0, 8))
            return b

        abtn("踢出这条", self._kick_selected, "#ea580c")
        abtn("踢出该账号全部", self._kick_user)
        abtn("禁用账号", self._disable_user, "#b91c1c")
        tk.Label(acts, text="踢出＝立刻断线（还能再登）；禁用＝以后不能登录",
                 fg="#64748b", font=(ui, 9)).pack(side="left", padx=(6, 0))
        # 等窗口真的摆开之后再取数：列宽要按可视宽算，早了就没有那个宽度
        self.root.after(250, self._reload_users)

    def _fit_users_columns(self, values):
        """列宽按**这一屏真数据**量：放得下就一个都不截，放不下才从最宽的列收，
        且只给真被收过的列补省略号。
        ★ 原来这里写死 72/170/84/168/126/116/104，「设备 · 端」和「最后活跃」
          在 1704 的窗口里都被切；夹个 300 上限更是把放得下的列也截了。
        """
        f = self.tree_font
        tree = self.users_tree
        if f is None or tree is None:
            return
        # 还没布局时 winfo_width() 是 1 ⇒ 那种情况下**不收缩**，按内容原样给宽，
        # 否则首屏会把每一列都压成省略号（本轮就在这红过一次）
        visible = tree.winfo_width() - 8 if tree.winfo_width() > 200 else None
        need, floor = [], []
        for i, (_k, text) in enumerate(self.USERS_HEADS):
            head = f.measure(text) + 18
            w = head
            for row in values:
                w = max(w, f.measure(str(row[i])) + 18)
            need.append(max(56, w))
            floor.append(max(56, head))          # 收到表头宽就停，别把「已踢出」压成「已…」
        shrunk = [False] * len(need)
        total = sum(need)
        while total > visible:
            cands = [i for i in range(len(need)) if need[i] > floor[i] + 8]
            if not cands:
                break
            i = max(cands, key=lambda k: need[k])
            cut = min(need[i] - floor[i], max(8, (total - visible) // 3))
            need[i] -= cut
            shrunk[i] = True
            total = sum(need)
        out = []
        for row in values:
            cells = []
            for i, v in enumerate(row):
                t = str(v)
                if shrunk[i] and f.measure(t) > need[i] - 12:
                    while t and f.measure(t + "…") > need[i] - 12:
                        t = t[:-1]
                    t = t.rstrip("（( :·-")
                    cells.append((t + "…") if t else "…")
                    continue
                cells.append(t)
            out.append(tuple(cells))
        for i, (key, _t) in enumerate(self.USERS_HEADS):
            tree.column(key, width=need[i], anchor="w", stretch=(key == "who"))
        return out

    @staticmethod
    def _users_values(rows):
        """会话记录 → 表格那 7 列的字（口径与三端页面共用的一份保持一致）。"""
        out = []
        for rec in rows:
            sub = str(rec.get("sub") or "")
            user = sub.split(":", 1)[1] if ":" in sub else sub
            kinds = {"app": "App 账号", "v3": "V3 操作员", "wechat": "微信"}
            kind = kinds.get(rec.get("kind")) or rec.get("kind") or "—"
            # 设备：品牌·型号·端 拼一行（服务端解析好的，三处页面共用一份口径）
            dev = " · ".join(x for x in (rec.get("brand"), rec.get("model"),
                                         rec.get("client")) if x) or rec.get("platform") or "—"
            out.append((
                "已踢出" if rec.get("revoked") else ("在线" if rec.get("online") else "离线"),
                "%s（%s）" % (rec.get("name") or user, user),
                kind,
                # ★ 账套（2026-10-14）：每本账有自己的操作员集合，不显示就答不出
                #   "这个操作员此刻在哪本账上"。空串 = 该商户没登记账套清单。
                rec.get("database") or "默认",
                dev, rec.get("ip") or "—", _ts(rec.get("login_at")),
                _idle(rec.get("idle_sec"))))
        return out

    def _reload_users(self):
        if self.users_tree is None:
            return
        # ★ 只要 V3 操作员（kind=v3），**不拉平台账号**（2026-10-14）。
        #   本地 Agent 服务的是"一家商户的一本账"，而平台账号（应用账号）属于
        #   云端那一层、还可能跨商户 —— 露在客户机上既没用，又是一处泄露面。
        #   要管平台账号，去云端后台。
        ok, data = self._cloud_post("/agent/sessions", {"kind": "v3"})
        for iid in self.users_tree.get_children():
            self.users_tree.delete(iid)
        if not ok:
            self.users_stat.configure(text="读不到：%s" % data, fg="#b91c1c")
            self._users_rows = []
            return
        rows = data.get("sessions") or []
        st = data.get("stats") or {}
        self._users_rows = rows
        self.users_stat.configure(
            text="共 %s 条 · 在线 %s" % (st.get("total", len(rows)), st.get("online", "—")),
            fg="#475569")
        for i, (vals, rec) in enumerate(zip(self._fit_users_columns(self._users_values(rows))
                                            or self._users_values(rows), rows)):
            self.users_tree.insert("", "end", iid=str(i), values=vals,
                                   tags=() if rec.get("online") else ("off",))

    def _selected_user(self):
        sel = self.users_tree.selection() if self.users_tree else ()
        if not sel:
            messagebox.showinfo(APP_TITLE, "先在上面选中一行。")
            return None
        try:
            return self._users_rows[int(sel[0])]
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _user_name(rec):
        sub = str((rec or {}).get("sub") or "")
        return sub.split(":", 1)[1] if ":" in sub else sub

    def _kick_selected(self):
        rec = self._selected_user()
        if not rec:
            return
        user = self._user_name(rec)
        if not messagebox.askyesno(
                APP_TITLE,
                "踢出这一条会话？\n\n    %s（%s）\n\n"
                "只断这一台设备；该账号在其他设备上的登录不受影响。"
                % (rec.get("name") or user, rec.get("ip") or "—")):
            return
        ok, data = self._cloud_post("/agent/sessions/revoke", {"jti": rec.get("jti")})
        self.log("踢出会话 %s：%s" % (user, "成功" if ok else data), "ok" if ok else "warn")
        if not ok:
            messagebox.showerror(APP_TITLE, str(data))
        self._reload_users()

    def _kick_user(self):
        rec = self._selected_user()
        if not rec:
            return
        user = self._user_name(rec)
        if not messagebox.askyesno(
                APP_TITLE,
                "踢出「%s」的**全部**会话？\n\n"
                "他所有设备会立刻掉线，需要重新登录。\n"
                "账号本身不受影响 —— 之后还能正常登录。\n\n"
                "只想断这一台设备的话，请用「踢出这条」。" % user):
            return
        # ★ 传 subject（完整主体）而不是 username：
        #   V3 操作员的主体是 `v3:<商户>:<编号>`，不是 `account:<编号>` ——
        #   拿 username 拼的话，对 V3 操作员会 404「账号不存在」，
        #   而用户看到的只是"踢不动"（2026-10-14 修）。
        ok, data = self._cloud_post("/agent/sessions/revoke", {"subject": rec.get("sub")})
        if ok:
            self.log("已踢出「%s」全部会话（清 %s 条）" % (user, data.get("revoked")), "ok")
        else:
            self.log("踢出「%s」失败：%s" % (user, data), "warn")
            messagebox.showerror(APP_TITLE, str(data))
        self._reload_users()

    def _disable_user(self):
        rec = self._selected_user()
        if not rec:
            return
        user = self._user_name(rec)
        # ★ V3 操作员**没有**"禁用"这个概念，别给一个点了必然失败的按钮：
        #   他的账号在客户机的财鲸 V3 里（UserInfo 表），云端管不着。
        if str(rec.get("kind") or "") == "v3":
            messagebox.showinfo(
                APP_TITLE,
                "「%s」是财鲸 V3 的操作员，不能在云端禁用。\n\n"
                "要停用这个人：请在财鲸 V3 的「操作员」里停用他；\n"
                "只想让他现在下线：用「踢出该账号全部」。" % user)
            return
        if not messagebox.askyesno(
                APP_TITLE,
                "禁用账号「%s」？\n\n"
                "禁用 = 以后不能再登录，同时现有会话立刻失效。\n"
                "如果只想让他下线、之后还能登，请用「踢出」。\n\n"
                "账号不会被删除，之后可以在云端后台重新启用。" % user):
            return
        ok, data = self._cloud_post("/agent/accounts/disable",
                                    {"username": user, "disabled": True})
        if ok:
            self.log("已禁用账号「%s」，清掉 %s 条会话" % (user, data.get("dropped")), "ok")
        else:
            self.log("禁用「%s」失败：%s" % (user, data), "warn")
            messagebox.showerror(APP_TITLE, str(data))
        self._reload_users()

    # ---- 本机配置（首启填表）

    CFG_FIELDS = (
        ("商户码", "agent.name", "",
         "云端开通时给的那一个，一家一个。填成别家会把那家的在线连接顶掉"),
        ("Agent 密钥", "agent.secret", "pwd",
         "后台『商户』页开通时显示一次，当场粘贴进来"),
        ("云端地址", "agent.relay_url", "",
         "固定值 ws://149.88.75.125:8000/ws/agent"),
        ("SQL 地址", "sql.server", "",
         "财鲸V3 的 SQL Server 在哪台机器；本机就写 127.0.0.1"),
        ("SQL 端口", "sql.port", "",
         "默认 1433"),
        ("账套库名", "sql.database", "",
         "★ 这一家读哪套账。不确定就点下面『试连并列账套』看本机有哪几本"),
        ("SQL 账号", "sql.user", "",
         "本机 SQL Server 的登录名（通常 sa）—— 已不再有默认值，必须填"),
        ("SQL 密码", "sql.password", "pwd",
         "同上。留空 Agent 会拒绝启动，不会带着 sa/123 偷偷上线"),
        ("放行账套（可选）", "sql.databases", "",
         "一台主机两本账时才填：逗号分隔，= 这台机器允许被读到哪几本"),
    )

    def _build_cfg_panel(self):
        box = tk.LabelFrame(self.cfg_slot, text=" 本机配置 ", padx=10, pady=6)
        self.cfg_panel = box
        grid = tk.Frame(box)
        grid.pack(fill="x")
        for i, (label, dotted, kind, tip) in enumerate(self.CFG_FIELDS):
            tk.Label(grid, text=label, width=15, anchor="w",
                     font=("Microsoft YaHei UI", 10, "bold"), fg="#0f172a").grid(
                row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar()
            entry = tk.Entry(grid, textvariable=var, width=40,
                             show="●" if kind == "pwd" else "",
                             font=("Microsoft YaHei UI", 10))
            entry.grid(row=i, column=1, sticky="we", padx=(4, 8))
            tk.Label(grid, text=tip, anchor="w", fg="#64748b",
                     font=("Microsoft YaHei UI", 9), wraplength=330,
                     justify="left").grid(row=i, column=2, sticky="w")
            self.cfg_vars[dotted] = (var, entry, kind)
        row = tk.Frame(box)
        row.pack(fill="x", pady=(6, 0))

        def cbtn(text, cmd, bg, w, side="left", pad=(0, 6)):
            fg = "#334155" if bg == "#ffffff" else "#ffffff"
            b = tk.Button(row, text=text, command=cmd, bg=bg, fg=fg,
                          relief="flat", bd=0, width=w, padx=6, pady=4, cursor="hand2",
                          font=("Microsoft YaHei UI", 10,
                                "bold" if bg != "#ffffff" else "normal"),
                          activebackground=bg, activeforeground=fg,
                          disabledforeground="#9aa4b2")
            if bg == "#ffffff":
                b.configure(highlightbackground="#cbd5e1", highlightthickness=1,
                            activebackground="#eff6ff")
            b.pack(side=side, padx=pad)
            return b

        cbtn("保存并应用", self.save_cfg, "#16a34a", 13)
        cbtn("试连并列账套", self.probe_sql, "#ffffff", 13)
        cbtn("显示口令", self.toggle_pwd, "#ffffff", 10)
        cbtn("收起", lambda: self.show_cfg(False), "#ffffff", 8,
             side="right", pad=(0, 0))
        self.cfg_panel.pack_forget()

    def show_cfg(self, open_it=True):
        """展开/收起配置面板。填完或配置已经可用时就收起来，别挡着状态行。

        ★ 去掉两处 geometry 硬改（2026-10-12）。原来展开写死 820x780、
          收起写死 820x600 —— 会把用户自己拉好的窗口尺寸顶掉，收起时又拉回 600。
          现在只 pack / pack_forget，尺寸交给窗口自己伸；
          用户调过的尺寸由 runtime/ui_state.json 记住并恢复。
        """
        if open_it:
            if not self.cfg_panel.winfo_ismapped():
                self.cfg_panel.pack(in_=self.cfg_slot, fill="x")
                self._prefill_cfg()
        else:
            self.cfg_panel.pack_forget()

    def _read_disk_cfg(self):
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _dig(d, dotted):
        cur = d
        for part in dotted.split("."):
            if not isinstance(cur, dict):
                return ""
            cur = cur.get(part)
        return "" if cur is None else str(cur)

    @staticmethod
    def _put(d, dotted, value):
        parts = dotted.split(".")
        cur = d
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

    def _prefill_cfg(self):
        """把磁盘上那份 config 摊进表单，并记住"打开时长什么样"。

        ★ 记住 prefill 是为了下一步：只有**真的被改过**的字段才写回文件。
          热切换是后台进程自己落盘的，要是保存时把表单里那本（打开面板时的旧值）
          整体写回去，就会把刚切过去的账套又改回来 —— 又是一个"看着正常其实读错账"。
        """
        raw = self._read_disk_cfg()
        for dotted, (var, _entry, _kind) in self.cfg_vars.items():
            val = self._dig(raw, dotted)
            var.set(val)
            self.cfg_prefill[dotted] = val

    def _form_changes(self):
        """返回 {dotted: 新值}，只包含用户真改过的字段。"""
        out = {}
        for dotted, (var, _entry, _kind) in self.cfg_vars.items():
            val = str(var.get()).strip()
            if val != str(self.cfg_prefill.get(dotted, "")):
                out[dotted] = val
        return out

    def _merged_cfg(self, changes):
        """磁盘那份 + 表单改动 —— 这是**要写进文件**的那份。

        ★ 故意不并 `DEFAULTS`：内置默认值不能被当成"显式写过的值"落盘，
          否则缺 `agent.relay_url` 会被悄悄补成 `ws://127.0.0.1:8000`（本机没有中转），
          而 P1-4 挣来的那条"缺什么就点名报错"又变回"带着默认值上线"。
        """
        merged = json.loads(json.dumps(self._read_disk_cfg()))
        for dotted, val in changes.items():
            self._put(merged, dotted, val)
        return merged

    def _probe_cfg(self, changes):
        """给『试连并列账套』用的完整视图：DEFAULTS 打底 + 磁盘 + 表单。

        只在内存里存在、不落盘 —— `sql.account_sets()` 要 `tds_version/login_timeout/timeout`
        这些键才连得动，而这些本来就该由 DEFAULTS 补（读配置时也是这么补的）。
        """
        ensure_import_path()
        from v3agent import config as cfgmod
        merged = json.loads(json.dumps(cfgmod.DEFAULTS))
        disk = self._read_disk_cfg()
        for section in ("sql", "agent", "price"):
            if isinstance(disk.get(section), dict):
                merged[section].update(disk[section])
        merged.update({k: v for k, v in disk.items() if k not in ("sql", "agent", "price")})
        for dotted, val in changes.items():
            self._put(merged, dotted, val)
        return merged

    def save_cfg(self):
        changes = self._form_changes()
        if not changes:
            self.log("表单没有改动，未写文件。", "meta")
            return
        ensure_import_path()
        from v3agent import config as cfgmod
        merged = self._merged_cfg(changes)
        merged.pop("_meta", None)
        try:
            with open(self.cfg_path, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
        except Exception as exc:  # noqa: BLE001
            self.log("写 config 失败：%s" % exc, "error")
            messagebox.showerror(APP_TITLE, "写不进去：%s\n\n%s" % (self.cfg_path, exc))
            return
        self.log("已写 %s（改了 %s）" % (self.cfg_path, "、".join(sorted(changes))), "ok")
        problems = cfgmod.validate_identity(merged)
        if problems:
            self.log("★ 还不能上线，缺这些：", "warn")
            for p in problems:
                self.log("    · " + p, "warn")
            self.log("补完再点『保存并应用』。这些项不填，Agent 会拒绝启动（不会带着默认值上线）。", "meta")
            self._load_config(keep_open=True)
            return
        alive = self.dm.read_pid()[1]
        self._load_config(keep_open=False)
        self.show_cfg(False)
        self.discover_dbs()
        if alive:
            self.log("配置已保存，但后台 Agent 用的还是启动时那一份 —— "
                     "点『停止』再点『启动』才吃到新配置。", "warn")
        else:
            self.log("配置齐了，点『启动后台 Agent』即可上线。", "ok")

    def toggle_pwd(self):
        self.show_pwd = not self.show_pwd
        for _label, dotted, kind, _tip in self.CFG_FIELDS:
            if kind == "pwd":
                self.cfg_vars[dotted][1].configure(show="" if self.show_pwd else "●")

    def probe_sql(self):
        """用**表单里这组凭据**试连本机 SQL Server，并列出真正的财鲸V3 账套。

        ★ 为什么要这个按钮：装机最常见的两个错是"库名抄错一个字母"和
          "SQL 账号连不上"，而这两个错在今天都要等到 Agent 起来才暴露。
          只读（sys.databases + 四张业务表存在性），不写任何数据。
        ★ 不落盘、不改配置：它拿的是"表单当前内容 + 磁盘那份"合出来的内存副本。
        """
        if self._sql_scan is not None and self._sql_scan.is_alive():
            return
        merged = self._probe_cfg(self._form_changes())
        self._sql_box.clear()
        box = self._sql_box
        self.log("试连 %s:%s（只读，不改配置）…" % (
            ((merged.get("sql") or {}).get("server")), ((merged.get("sql") or {}).get("port"))), "meta")

        def _scan():
            try:
                ensure_import_path()
                from v3agent import sql as v3sql
                box.append(v3sql.account_sets(merged))
            except Exception as exc:  # noqa: BLE001
                box.append({"error": str(exc)[:200], "databases": []})

        self._sql_scan = threading.Thread(target=_scan, daemon=True)
        self._sql_scan.start()
        self._collect_sql()

    def _collect_sql(self):
        """取回试连结果（tkinter 控件不能在子线程里碰，所以由 UI 线程轮这个盒子）。"""
        if not self._sql_box:
            self.root.after(200, self._collect_sql)
            return
        info = self._sql_box.pop(0)
        dbs = info.get("databases") or []
        names = [d.get("name") for d in dbs if d.get("isAccountSet")]
        skipped = [d.get("name") for d in dbs if not d.get("isAccountSet")]
        if info.get("error"):
            self.log("✗ 连不上：%s" % info["error"], "error")
            self.log("  → 逐项核：SQL 地址/端口是否在跑、账号口令、TCP/IP 协议有没有启用"
                     "（SQL 配置管理器里）。这条不通过，装了也上线不了。", "warn")
            return
        self.log("✓ 连上了。本机是财鲸V3 账套的库：%s" % ("、".join(names) or "（一个都没有）"), "ok")
        if skipped:
            self.log("  已排除（缺 XS/Hpdan/Khdan/No_BillID，不是账套）：%s" % "、".join(skipped), "meta")
        var = self.cfg_vars.get("sql.database", (None,))[0]
        if var is not None and len(names) == 1 and not str(var.get()).strip():
            var.set(names[0])
            self.log("  只有一本账套，已替你填进『账套库名』—— 确认一下再点『保存并应用』。", "meta")
        elif names:
            self.log("  有几本：自己挑一个填进『账套库名』。"
                     "★ 填错不会报错，但客户看到的钱是另一套账的。", "warn")
        else:
            self.log("  没找到账套库 ⇒ 这台机器的财鲸V3 还没装好或账号没权限读，先让客户方补齐。", "warn")

    # ---- 日志
    #
    # ★ 2026-10-12 改造：从"直写 ScrolledText"改成"内存缓冲 + 按筛选重绘"。
    #   原因：加了级别筛选与搜索之后，插入一条就得判断它是否命中当前筛选，
    #   还要维护 chip 上的计数 —— 统一走"缓冲 → 重绘"最简单，也不会数错。
    #   缓冲上限 1000 行，防止长时间运行把内存吃掉。

    LOG_MAX = 1000
    LOG_FILTERS = {
        "all": None,
        "info": ("info", "ok", "meta"),
        "req": ("req",),
        "warn": ("warn",),
        "error": ("error",),
    }

    def _append_log(self, ts, msg, level="info"):
        """只入缓冲并登记一次重绘 —— 同一批写入由 after_idle 合并成一次。"""
        self._log_buf.append((ts or time.strftime("%H:%M:%S"), str(msg), level))
        if len(self._log_buf) > self.LOG_MAX:
            del self._log_buf[:len(self._log_buf) - self.LOG_MAX]
        self._log_dirty = True
        if self._log_after is None:
            try:
                self._log_after = self.root.after_idle(self._flush_log)
            except Exception:  # noqa: BLE001
                self._flush_log()

    def log(self, msg, level="info"):
        """对外沿用旧签名，内部改走缓冲。"""
        self._append_log(time.strftime("%H:%M:%S"), msg, level)

    def _flush_log(self):
        self._log_after = None
        if not self._log_dirty:
            return
        self._log_dirty = False
        self._update_log_badge()
        if not self._log_paused.get():
            self._render_log()

    def set_log_filter(self, key):
        self._log_filter = key if key in self.LOG_FILTERS else "all"
        for k, chip in self._log_chips.items():
            on = (k == self._log_filter)
            chip.configure(bg="#0f172a" if on else "#ffffff",
                           fg="#ffffff" if on else "#334155",
                           highlightbackground="#0f172a" if on else "#cbd5e1",
                           text=self._chip_text(k))
        self._render_log()

    def _chip_text(self, key):
        base = {"all": "全部", "info": "信息", "req": "请求",
                "warn": "警告", "error": "错误"}[key]
        allow = self.LOG_FILTERS[key]
        if allow is None:
            n = len(self._log_buf)
        else:
            n = sum(1 for _t, _m, lv in self._log_buf if lv in allow)
        # ★ 错误数是排障时最该一眼看到的，始终带出来
        if key in ("all", "error") and n:
            return "%s %d" % (base, n)
        return base

    def _update_log_badge(self):
        """更新「查看日志」按钮上的错误计数；弹窗开着时顺带同步 chips。

        ★ 日志藏进弹窗之后，"有没有出错"只能靠这个角标 ——
          所以哪怕弹窗没开，也要照常更新。
        """
        errs = sum(1 for _t, _m, lv in self._log_buf if lv == "error")
        if self.btn_log is not None:
            try:
                self.btn_log.configure(text=("查看日志 %d" % errs) if errs else "查看日志")
            except Exception:  # noqa: BLE001
                pass
        for k, chip in self._log_chips.items():
            chip.configure(text=self._chip_text(k))

    def _render_log(self):
        if self.txt is None:
            return                 # 弹窗没开：不用渲染，缓冲照收
        allow = self.LOG_FILTERS.get(self._log_filter)
        kw = (self._log_kw.get() or "").strip().lower()
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        shown = 0
        for ts, msg, level in self._log_buf:
            if allow is not None and level not in allow:
                continue
            if kw and kw not in msg.lower():
                continue
            self.txt.insert("end", "%s  %s\n" % (ts, msg), level)
            shown += 1
        if not shown:
            self.txt.insert("end", "（当前筛选下没有日志）\n", "meta")
        self.txt.configure(state="disabled")
        if not self._log_paused.get():
            self.txt.see("end")

    def clear_log(self):
        self._log_buf.clear()
        self._log_dirty = True
        self._flush_log()

    # ---- 配置

    def _load_config(self, keep_open=False):
        """读配置；读不出就按"待填写"处理（自创建 + 打开面板），而不是罢工。

        ★ 改这件事的原因（2026-09-26 用户提出）：把 `V3AgentGui.exe` 单独拷到别的机器
          双击，原来只会红字一句"去找模板或用安装器"。现在它自己建出配置文件、
          把该填的几项摊在界面上，填完点『保存并应用』。
        ★ 红线一点没动：daemon 那边照旧 `config.load()` 缺必填项就抛错拒绝上线，
          GUI 只是把"缺什么、去哪儿填"说清楚 —— 不预填任何身份或凭据。
        """
        self.cfg = None
        self.cfg_ready = False
        self.cfg_path = find_config()
        if not self.cfg_path:
            self.cfg_path, why = seed_config()
            if not self.cfg_path:
                self.lbl_cfg.configure(text="配置：本目录建不出 config.json", fg="#b91c1c")
                self.log(why, "error")
                return
            self.log("本目录没有 config.json，已自动建出一份空的：%s" % self.cfg_path, "meta")
            self.log("★ 下面『本机配置』填完点『保存并应用』。不填也能开窗口，但 Agent 会"
                     "拒绝启动 —— 包里不内置商户身份，这是故意的。", "warn")
        ensure_import_path()
        from v3agent import config as cfgmod
        try:
            cfg = cfgmod.load(self.cfg_path)
        except Exception as exc:  # noqa: BLE001
            # 分两种：还差几项（待填写，黄）与这文件本身坏了（故障，红）。
            problems = []
            try:
                problems = cfgmod.validate_identity(self._merged_cfg({}))
            except Exception:  # noqa: BLE001
                problems = []
            if problems:
                self.lbl_cfg.configure(text="配置：还差 %d 项没填（后台 Agent 不会启动）"
                                             % len(problems), fg="#b45309")
                self.log("配置还差这些：%s" % "；".join(problems), "warn")
                self.show_cfg(True)
                return
            # ★ P1-4：以前这种配置会"带着内置默认值"照样上线，现在直接拒绝并说清缺什么。
            self.lbl_cfg.configure(text="配置：%s" % str(exc).split("\n")[0], fg="#b91c1c")
            self.log("配置不可用（后台 Agent 不会启动）：%s" % exc, "error")
            self.show_cfg(True)
            return
        a = cfg["agent"]
        self.cfg = cfg
        self.cfg_ready = True
        self.lbl_cfg.configure(
            text="配置：%s   Agent 名：%s" % (self.cfg_path, a.get("name")), fg="#475569")
        if not keep_open:
            self.show_cfg(False)

    # ---- 账套发现与切换 ----

    def discover_dbs(self):
        """后台扫一遍本实例上可切的账套。

        ★ 为什么丢给线程：account_sets() 要连 master 并对每个库探 4 次 OBJECT_ID，
          本机 0.1~0.3s，但库多或 SQL Server 正忙时会到秒级 —— 直接在 UI 线程做
          会把窗口冻住（这个窗口还有 3 秒一次的状态轮询）。
        """
        if not self.cfg:
            self.lbl_db.configure(text="当前：先点『本机配置』填完并保存，再发现账套", fg="#b45309")
            return
        if self._db_scan is not None and self._db_scan.is_alive():
            return
        self._db_box.clear()
        box = self._db_box
        cfg_snapshot = json.loads(json.dumps(self.cfg))
        self._db_scan = threading.Thread(target=self._scan_dbs, args=(cfg_snapshot, box),
                                         daemon=True)
        self._db_scan.start()
        self.lbl_db.configure(text="账套发现中…", fg="#475569")

    @staticmethod
    def _scan_dbs(cfg, box):
        try:
            ensure_import_path()
            from v3agent import sql as v3sql
            box.append(v3sql.account_sets(cfg))
        except Exception as exc:  # noqa: BLE001
            box.append({"error": str(exc)[:180], "databases": []})

    def _collect_dbs(self):
        """把发现线程的结果取回 UI 线程（tkinter 控件不能在子线程里碰）。"""
        if not self._db_box:
            return
        info = self._db_box.pop(0)
        dbs = info.get("databases") or []
        names = [d.get("name") for d in dbs if d.get("isAccountSet")]
        skipped = [d.get("name") for d in dbs if not d.get("isAccountSet")]
        if info.get("error"):
            self._db_note = "发现失败：%s" % info["error"]
        if not names and self.cfg:
            names = [((self.cfg.get("sql") or {}).get("database") or "")]
        self._db_names = [n for n in names if n]
        self.cmb_db.configure(values=self._db_names)
        if skipped:
            self.log("已排除非账套库（缺 XS/Hpdan/Khdan/No_BillID）：%s" % "、".join(skipped), "meta")
        if self._db_names:
            cur = self._current_db()
            if cur in self._db_names:
                self.cmb_db.set(cur)
            self.log("可切换账套：%s" % "、".join(self._db_names), "meta")

    def _current_db(self, st=None):
        """当前账套：以 Agent 实测值为准，其次配置值。"""
        st = st if st is not None else (self.dm.read_status() or {})
        return (st.get("database_verified") or st.get("database")
                or ((self.cfg or {}).get("sql") or {}).get("database") or "")

    def switch_db(self):
        target = str(self.cmb_db.get() or "").strip()
        if not target:
            messagebox.showwarning(
                APP_TITLE, "先在下拉里选一个账套。\n列表为空就点『重新发现』。")
            return
        cur = self._current_db()
        if target == cur:
            messagebox.showinfo(APP_TITLE, "当前已经是 %s，无需切换。" % target)
            return
        pid, alive = self.dm.read_pid()
        if not messagebox.askyesno(
                APP_TITLE,
                "确认把本地 Agent 从账套\n\n    %s\n\n切到\n\n    %s ？\n\n"
                "★ 切完之后，手机 / 小程序上看到的客户、商品、单据、报表\n"
                "  全部会变成 %s 这套账的数据。\n"
                "  两个账套的客户与单号可能长得很像，数字看起来都「正常」。\n\n"
                "在途请求会先跑完（建单不会跨库撕裂）。" % (cur or "(未知)", target, target)):
            self.log("已取消切换账套。", "meta")
            return
        ok = True
        if alive:
            ok, msg = self.dm.request_db_switch(target, source="gui")
            self.log(msg, "ok" if ok else "error")
            if not ok:
                messagebox.showerror(APP_TITLE, msg)
        else:
            # 后台没跑：直接改配置，不留过期哨兵（否则下次启动会被一条几小时前的请求切走）
            ensure_import_path()
            from v3agent import config as cfgmod
            ok, msg = cfgmod.save_database(self.cfg_path, target)
            self.log(msg, "ok" if ok else "error")
            if ok:
                cfgmod.set_database(self.cfg, target)
                self.cmb_db.set(target)
            else:
                messagebox.showerror(APP_TITLE, msg)
        if ok:
            self._after_db_switch(cur, target)
        self._refresh()

    def _after_db_switch(self, old_db, new_db):
        """换账套成功之后：让燕秀面板丢掉旧账的物料库。

        ★ 不丢掉会发生什么：候选列表还是按 A 账的 HPKhType 给编号，人点下去写进 B 账 ——
          那个编号在 B 账里要么不存在、要么根本是另一种东西，
          而且界面上一切都"正常"，只有对账时才看得出来。
        """
        try:
            if self.on_db_switched:
                self.on_db_switched(old_db, new_db)
        except Exception as exc:  # noqa: BLE001
            self.log("通知燕秀面板重置物料库失败：%s" % exc, "warn")

    def open_cfg(self):
        if not self.cfg_path:
            messagebox.showwarning(APP_TITLE, "没找到 config.json")
            return
        try:
            os.startfile(self.cfg_path)  # noqa: S606
        except Exception:  # noqa: BLE001
            messagebox.showinfo(APP_TITLE, "配置路径：\n%s" % self.cfg_path)

    def open_log(self):
        p = self.dm.log_path()
        try:
            os.startfile(p)  # noqa: S606
        except Exception:  # noqa: BLE001
            messagebox.showinfo(APP_TITLE, "日志路径：\n%s" % p)

    # ---- 状态翻译（★ 本次重构的核心）
    #
    # 后台 Agent 早把结论写在 status.json 里了，这边只做"翻译"，不重新判断：
    #   _badge_state()  → 顶栏徽章
    #   _diag_content() → 诊断卡（正常 / 未运行时返回 None，整块不渲染）

    def _badge_state(self, st, alive):
        """返回 (level, 徽章文案)。

        ★ 用 daemon 写的 state 判，**不是"进程在不在"**。
          旧逻辑只看 read_pid()：进程活着就画绿色「后台运行中」，
          于是"一条请求都处理不了"的机器顶栏照样是绿的 —— 界面给出了最乐观的错结论。
        """
        if not alive:
            return "stop", "未运行"
        st = st or {}
        if st.get("stale"):
            return "warn", "状态未更新 %ds" % int(st.get("age_sec") or 0)
        if st.get("connected"):
            return "ok", "已连云端"
        state = st.get("state") or ""
        if not state:
            return "warn", "状态未知"
        return STATE_LEVEL.get(state, ("warn", state))

    def _diag_content(self, st, alive):
        """提示条内容；不需要提示时返回 None（整块不出现）。

        ★ 只给"结论 + 该去点哪里"，**不放按钮** —— 动作统一在操作行。
        """
        if not alive:
            # 未启动也给引导（原来是"什么都不显示"，用户打开界面时反而最空）
            return {
                "level": "info",
                "title": "未启动",
                "desc": "点右侧「启动后台 Agent」即可上线；上线后这个窗口可以随时关掉。",
                "ev": "",
            }
        st = st or {}
        if st.get("stale"):
            return {
                "level": "warn",
                "title": "状态超过 %d 秒没更新" % int(st.get("age_sec") or 0),
                "desc": "正常情况每 2 秒写一次。先点「重新探测」看看；仍不动就「停止」再「启动」。",
                "ev": "state=%s　·　age=%ss　·　connected=%s" % (
                    st.get("state") or "?", st.get("age_sec"), st.get("connected")),
            }
        if st.get("connected"):
            return None            # 正常态：整块不出现，不占高度
        state = st.get("state") or ""
        logs = st.get("logs") or []

        # 被顶号 / 重连中：偶发一次不吵，反复出现才提示
        if state in ("被顶号", "重连中"):
            n = self._count_log(logs, "顶掉")
            if n < 3:
                return None
            hint = self._find_log(logs, "个 Agent 进程")
            desc = ("同一商户码只允许一条连接。点「更多 → 停止全部 Agent」清掉本安装目录的"
                    "残留再重新启动；若本机确实只有一个实例，就去另一台机器上找。")
            if hint:
                desc = hint + " " + desc
            return {
                "level": "warn",
                "title": "同一个商户码有两条连接在抢（已 %d 次）" % n,
                "desc": desc,
                "ev": "state=%s　·　日志里「顶掉」%d 次　·　最后一条 %s" % (
                    state, n, self._last_log_time(logs)),
            }

        spec = STATE_DIAG.get(state)
        if not spec:
            return None            # 未知状态不猜
        return {
            "level": spec.get("level", "err"),
            "title": spec["title"],
            "desc": spec["desc"],
            "ev": self._diag_evidence(st, state),
        }

    def _diag_evidence(self, st, state):
        """依据行：把 daemon 写的东西原样摆出来，便于判断、也便于转述给技术员。"""
        bits = ["state=%s" % state]
        err = str(st.get("last_err") or "").strip()
        if err:
            bits.append("last_err=%s" % err[:180])
        db = st.get("database_verified") or st.get("database") or ""
        if db:
            bits.append("实测账套=%s" % db)
        return "　·　".join(bits)

    def _render_diag(self, content):
        """渲染诊断卡；content 为 None 时 pack_forget（不占高度）。

        三档配色：info 浅蓝（给引导）/ warn 黄（正在自动恢复）/ err 红（要人动手）
        """
        if content is None:
            if self.diag.winfo_manager():
                self.diag.pack_forget()
            return
        lvl = content.get("level", "err")
        if lvl == "warn":
            bg, bd, fg_t, fg_d, ic_bg, fg_e, ev_bg, mark = (
                "#fffbeb", "#fcd34d", "#92400e", "#78350f",
                "#fef3c7", "#92400e", "#fffdf5", "!")
        elif lvl == "info":
            bg, bd, fg_t, fg_d, ic_bg, fg_e, ev_bg, mark = (
                "#eff6ff", "#93c5fd", "#1e40af", "#1e3a8a",
                "#dbeafe", "#1d4ed8", "#f8fbff", "i")
        else:
            bg, bd, fg_t, fg_d, ic_bg, fg_e, ev_bg, mark = (
                "#fef2f2", "#fca5a5", "#991b1b", "#7f1d1d",
                "#fee2e2", "#9f1239", "#fff5f5", "!")
        self.diag.configure(bg=bg, highlightbackground=bd)
        self.diag_inner.configure(bg=bg)
        self.diag_ic.configure(bg=ic_bg, fg=fg_t, text=mark)
        self.diag_title.configure(bg=bg, fg=fg_t, text=content.get("title") or "")
        self.diag_desc.configure(bg=bg, fg=fg_d, text=content.get("desc") or "")
        ev = content.get("ev") or ""
        if ev:
            self.diag_ev.configure(bg=ev_bg, fg=fg_e, text=ev)
            if not self.diag_ev.winfo_manager():
                self.diag_ev.pack(fill="x", pady=(5, 0), padx=(31, 0))
        elif self.diag_ev.winfo_manager():
            self.diag_ev.pack_forget()     # 没有依据就别留一个空框
        if not self.diag.winfo_manager():
            self.diag.pack(fill="x", padx=14, pady=(10, 0), before=self.strip)

    @staticmethod
    def _count_log(logs, keyword):
        return sum(1 for it in logs if keyword in (it.get("msg") or ""))

    @staticmethod
    def _find_log(logs, keyword):
        for it in reversed(logs):
            m = it.get("msg") or ""
            if keyword in m:
                return m
        return ""

    @staticmethod
    def _last_log_time(logs):
        return (logs[-1].get("t") if logs else "") or "—"

    # ---- 定时轮询（GUI 只读，不持有任何连接）

    def _tick(self):
        try:
            self._collect_dbs()
            self._refresh()
        except Exception as exc:  # noqa: BLE001
            self.log("刷新状态异常：%s" % exc, "warn")
        # ★ 2026-09-20：轮询间隔 1.5s → 3s。
        #   起因是「未运行」分支每次都调 agent_processes()，而它以前内部会
        #   spawn 一个 powershell.exe（控制台程序）去查命令行
        #   → 1.5 秒闪一个黑窗，用户看到「一堆 cmd/powershell 反复弹」。
        #   现在那条路径已换成纯 Win32 API（0.012s、零子进程），闪窗问题根除；
        #   把间隔放宽到 3s 只是进一步降低无谓唤醒 —— 状态本来也只需要
        #   「秒级」新鲜度，用户要立刻刷新可以点『刷新状态』。
        self.root.after(3000, self._tick)

    def _refresh(self):
        pid, alive = self.dm.read_pid()
        st = self.dm.read_status()
        s = st or {}

        # ---------- 顶栏药丸：以 daemon 的 state 为准，不再只看进程在不在 ----------
        level, badge = self._badge_state(st, alive)
        self._badge_level = level
        dot = DOT_FG.get(level, "#94a3b8")
        pill_bg, pill_fg = BADGE_BG.get(level, ("#334155", "#e2e8f0"))
        self.badge.configure(bg=pill_bg)
        self.lbl_dot.configure(fg=dot, bg=pill_bg)
        self.lbl_state.configure(text=badge, fg=pill_fg, bg=pill_bg)
        if s.get("version"):
            self.lbl_ver.configure(text="v%s" % s["version"])

        # ---------- 启停按钮可用性（★ 这套 disabled 联动是既有正确实现，别改坏）----------
        self.btn_start.configure(state="normal" if not alive else "disabled")
        self.btn_stop.configure(state="disabled" if not alive else "normal")

        # ---------- 状态行 ----------
        self.lbl_headline.configure(text=HEADLINE.get(level, "运行中"),
                                    fg=HEADLINE_FG.get(level, "#0f172a"))
        bits = []
        if alive:
            bits.append("PID %d" % pid)
            bits.append("已运行 %s" % _dur(s.get("uptime_sec")))
            if level == "err":
                bits.append("进程还在，但当前不提供服务")
        else:
            # 也许有进程但 pid 文件丢了，兜底扫一遍
            # ★ 2026-09-20：agent_processes() 已改成纯 Win32 API（0.012s、零子进程），
            #   不再闪黑窗；但为了不给「幽灵进程」制造假象，仍然只在需要时才提示。
            leftovers = self.dm.agent_processes(exclude_pids=(os.getpid(),))
            if leftovers:
                bits.append("发现 %d 个残留进程 → 点『停止』清理" % len(leftovers))
        db = s.get("database_verified") or s.get("database") or ""
        if db:
            bits.append("账套 %s" % db)
        if alive and s.get("recv") is not None:
            bits.append("接收 %s · 发送 %s" % (s.get("recv"), s.get("sent")))
        self.lbl_facts.configure(text="　·　".join(bits))

        # ---------- 提示 / 诊断条（正常态整块不出现）----------
        self._render_diag(self._diag_content(st, alive))

        if st:
            self._sync_log(st.get("logs") or [])

        self._update_db_line(st if alive else {}, alive)

    def _update_db_line(self, st, alive):
        """账套那一行 + 窗口标题：把"现在到底连的哪套账"摆到最显眼的位置。"""
        cfgdb = ((self.cfg or {}).get("sql") or {}).get("database") or ""
        db = self._current_db(st)
        verified = (st or {}).get("database_verified") or ""
        claimed = (st or {}).get("database") or ""      # Agent 自报的"我以为我在哪"
        parts = ["当前：%s" % (db or "—")]
        warn = False
        # ★ 一台主机两本账时，"能读到哪几本"是这台机器自己的清单（不是云端那份）。
        #   不显示出来的话，现场只会看到"下拉里明明有第二本，手机上却读不到"。
        try:
            ensure_import_path()
            from v3agent import config as _cfgmod
            allowed = _cfgmod.allowed_databases(self.cfg or {})
        except Exception:  # noqa: BLE001
            allowed = []
        if len(allowed) > 1:
            parts.append("允许读 %d 本：%s" % (len(allowed), "、".join(allowed)))
        if not db:
            parts.append("★ 不知道连的是哪套账：没有可用配置，且后台 Agent 未上报实测库")
            warn = True
        if verified and cfgdb and verified != cfgdb:
            parts.append("★ 实测库=%s 但配置写的是 %s（配置未生效？）" % (verified, cfgdb))
            warn = True
        if verified and claimed and verified != claimed:
            # 同一条状态里"自报"和"实测"打架 = 切库没落地，必须吵
            parts.append("★ Agent 说在 %s，实测 DB_NAME()=%s" % (claimed, verified))
            warn = True
        pending = (st or {}).get("db_switch") or ""
        if pending:
            parts.append(pending)
        if not alive:
            parts.append("（后台 Agent 未运行）")
        self.lbl_db.configure(text="   ".join(parts),
                              fg="#b91c1c" if warn else "#475569")
        title = APP_TITLE + ("　·　账套 %s" % db if db else "")
        if self.root.title() != title:
            self.root.title(title)

    def _sync_log(self, logs):
        """把后台 Agent 的日志增量贴进缓冲。

        ★ 契约不能破：self._last_log_count 是 daemon `logs` 列表的**长度水位**。
          daemon 那边内存上限 300 条、写进 status.json 的只有最后 80 条
          （agent_daemon.py L1005 / L1028），所以一旦水位超过 80 就必须靠
          logs[-20:] 兜底 —— 这段逻辑原本就是这样，这里只是把"写控件"换成"写缓冲"。
        """
        n = len(logs)
        if n == self._last_log_count:
            return
        fresh = logs[self._last_log_count:] if n > self._last_log_count else logs[-20:]
        self._last_log_count = n
        for item in fresh:
            self._append_log(item.get("t") or "", item.get("msg") or "",
                             item.get("level", "info"))

    # ---- 动作

    # ---- 重新探测（2026-10-12 取代原来的「刷新状态」）
    #
    # ★ 为什么取代：原按钮只是手动跑一次 _refresh()，而 _tick() 每 3 秒自动跑一次
    #   —— 不 ping 云端、不查库、不重连，点了没反应，反而像坏了。
    # ★ 现在真探三件事：云端通不通 / 账套读不读得到 / 进程在不在。
    # ★ 两条刻意的界限：
    #     ① 不写 status.json —— 那份文件由 daemon 每 2 秒覆盖（agent_daemon.py L1429），
    #        GUI 写进去会被立刻盖掉，还可能和 daemon 抢文件。结果只活在界面和日志里。
    #     ② 不重启、不重连 —— 探测只回答"现在行不行"，不改变任何状态。真要重连，
    #        用户该点「停止」再「启动」；那是明确动作，不该被一个探测按钮偷偷做掉。

    def probe_now(self):
        if self._probe_after is not None:
            try:
                self.root.after_cancel(self._probe_after)
            except Exception:  # noqa: BLE001
                pass
            self._probe_after = None

        self.log("—" * 50, "meta")
        self.log("开始重新探测…", "meta")
        pid, alive = self.dm.read_pid()
        cloud = self._probe_cloud()
        db = self._probe_database()
        bits = ["云端 %s" % cloud,
                "账套 %s" % db,
                "进程 %s" % (("PID %d ✓" % pid) if alive else "未运行")]
        good = cloud.startswith("✓") and db.startswith("✓")
        self.log("重新探测：" + "　".join(bits), "ok" if good else "warn")
        if not good:
            self.log("（探测只回答『现在行不行』，不会自动重连；"
                     "要重连请点「停止」再「启动」）", "meta")
        self.lbl_facts.configure(text="重新探测：　" + "　·　".join(bits))
        self._probe_after = self.root.after(8000, self._probe_revert)

    def _probe_revert(self):
        """8 秒后把状态行还回常规事实串。"""
        self._probe_after = None
        self._refresh()

    def _probe_cloud(self):
        """探云端：GET /api/v1/health（把 relay_url 的 ws:// 换成 http://）。

        只读、超时 3 秒。返回值以 ✓ / ✗ 开头，调用方据此判断。
        """
        import urllib.request
        st = self.dm.read_status() or {}
        url = str(st.get("relay_url") or "").strip() or str(
            ((self.cfg or {}).get("agent") or {}).get("relay_url") or "")
        if not url:
            return "地址未知"
        base = url.replace("wss://", "https://").replace("ws://", "http://")
        api = base.split("/ws/")[0].rstrip("/") + "/api/v1/health"
        t0 = time.time()
        try:
            with urllib.request.urlopen(api, timeout=3) as resp:
                resp.read(4096)
            return "✓ %dms" % int((time.time() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001
            return "✗ %s" % type(exc).__name__

    def _probe_database(self):
        """探账套：用配置里的凭据连一次，跑 SELECT DB_NAME() 看实测是哪一本。

        ★ 为什么要主动连：界面平时显示的是 daemon 上报的 database_verified，
          那是**被动**的（等它下次 flush）；这里主动连一次，立刻知道。
        ★ 只读、短连接 —— session() 用完会把连接还回池。
        """
        if not self.cfg:
            return "配置未就绪"
        try:
            ensure_import_path()
            from v3agent import sql as v3sql
        except Exception as exc:  # noqa: BLE001
            return "✗ 加载失败（%s）" % type(exc).__name__
        try:
            with v3sql.session(self.cfg) as conn:
                name = v3sql.current_database(conn.cursor())
        except KeyError as exc:      # 配置缺字段（正规装机不会，开发态会）
            return "✗ 配置不完整（缺 %s）" % exc
        except Exception as exc:  # noqa: BLE001
            return "✗ %s" % type(exc).__name__
        want = str(((self.cfg or {}).get("sql") or {}).get("database") or "")
        if name and want and name != want:
            return "✗ 实测 %s（配置写的是 %s）" % (name, want)
        return "✓ %s" % (name or "未知")

    def _scope(self):
        """本窗口这一份安装的地盘 = config.json 所在目录。

        ★ 一台机器上可能有几份安装（一个老板两个账套 / 两家共用主机），
          清理残留与「停止」都只能碰自己这一份，越界会把别家打下线。
        """
        if not self.cfg_path:
            return self.dm.own_scope()
        return os.path.normcase(os.path.abspath(os.path.dirname(os.path.abspath(self.cfg_path))))

    def run_selfcheck(self):
        """现场自检：现场装机和远程排障都靠这一次点击（结果同时写进 logs）。

        ★ 每次重读磁盘上的 config.json，不用窗口里那份内存 cfg：
          账套热切是后台进程自己落盘的，内存那份可能还是上一本账。
        """
        self.log("—" * 60, "meta")
        if not self.cfg_path:
            self._load_config()
        if not self.cfg_path:
            messagebox.showerror(APP_TITLE, "没有 config.json，无法自检。")
            return
        try:
            ensure_import_path()
            from v3agent import config as cfgmod
            from v3agent import selfcheck as sc
            # ★ 用 load_any 而不是 load：配置没填全时也要出一份报告。
            #   以前这里 load 抛错就直接返回，技术员点完自检什么文件都没有 ——
            #   而"没填全"恰恰是他最该拿到"缺哪几项"的时候。
            cfg, problems = cfgmod.load_any(self.cfg_path)
        except Exception as exc:  # noqa: BLE001
            self.log("自检没跑起来：%s" % exc, "error")
            self.show_cfg(True)
            return
        if problems:
            self.log("配置还没填全，自检照样跑（第一条会点名缺什么）：", "warn")
            for pr in problems:
                self.log("    · " + pr, "warn")
            self.log("  → 点『本机配置』补齐并保存，再自检一次。", "meta")
        self.log("开始现场自检（只读：不写业务数据、不改配置）…", "meta")
        try:
            res = sc.run(cfg, self.cfg_path)
        except Exception as exc:  # noqa: BLE001
            self.log("自检异常（把这条原文发给平台方）：%s" % exc, "error")
            return
        for it in res["items"]:
            self.log("%s %s：%s" % ("✓" if it["ok"] else "✗", it["label"], it["detail"]),
                     "ok" if it["ok"] else "error")
            if it.get("next_step"):
                self.log("    → " + it["next_step"], "warn" if not it["ok"] else "meta")
        self.log("自检结论：%s" % ("全部通过，可以交付" if res["ok"] else "有未通过项，按上面的处置再来一次"),
                 "ok" if res["ok"] else "error")
        self.log("结果已写：%s" % (res.get("written") or "(写文件失败，请截图上面几行)"), "meta")

    def start(self):
        if self.dm.read_pid()[1]:
            messagebox.showinfo(APP_TITLE, "后台 Agent 已经在运行了。")
            return
        if not self.cfg_path:
            self._load_config()
        if not self.cfg_ready:
            # ★ 这里不放行是刻意的：让 daemon 起来再自己拒绝，只会让人以为"点了启动没反应"
            self.show_cfg(True)
            problems = []
            try:
                ensure_import_path()
                from v3agent import config as cfgmod
                problems = cfgmod.validate_identity(self._merged_cfg({}))
            except Exception:  # noqa: BLE001
                pass
            self.log("不能启动：配置还没填全。" + ("缺 → " + "；".join(problems) if problems else ""),
                     "warn")
            messagebox.showwarning(
                APP_TITLE,
                "配置还没填全，Agent 不会上线。\n\n"
                + "\n".join("· " + p for p in problems[:6] or ["见窗口下方『本机配置』"])
                + "\n\n填完点『保存并应用』，再点『启动』。")
            return

        scope = self._scope()
        self.log("—" * 60, "meta")
        killed = self.dm.kick_duplicates(exclude_pids=(os.getpid(),), scope=scope)
        if killed:
            self.log("启动前清理了本安装目录的 %d 个残留 Agent（避免抢云端连接）：" % len(killed), "warn")
            for p, desc in killed:
                self.log("    PID %-7d %s" % (p, desc), "warn")
        else:
            self.log("本安装目录没有残留。", "info")
        others = self.dm.other_installs(scope)
        if others:
            self.log("★ 本机还跑着**别的目录**的 Agent，本次不动它们："
                     + "、".join("PID %d @ %s" % (p, where or "归属不明") for p, _d, where in others), "meta")

        ok, msg, pid = self.dm.start_daemon(self.cfg_path, clean_first=False, scope=scope)
        self.log(msg, "ok" if ok else "error")
        if ok:
            self.log("★ 现在可以关掉本窗口了 —— Agent 会继续在后台跑。", "ok")
        else:
            messagebox.showerror(APP_TITLE, msg)
        self._refresh()

    def stop(self):
        ok, msg = self.dm.stop_daemon(scope=self._scope())
        self.log(msg, "warn" if ok else "info")
        others = self.dm.other_installs(self._scope())
        if others:
            self.log("本机另有 %d 个不同目录的 Agent 仍在运行（不是本窗口的，故意没停）" % len(others), "meta")
        self._last_log_count = 0
        self._refresh()

    def stop_all(self):
        """停止**本安装目录**下的全部 Agent 实例（诊断卡上「停止全部 Agent」用）。

        ★ 这不是"清理垃圾"：dm.kick_duplicates() 的 keep_pid 默认 None，
          会把本安装目录下**每一个** Agent 进程都杀掉 —— 包括正在服务的那一个。
          所以必须二次确认，并把"停完之后要点启动"说清楚。
          （scope 限定在本安装目录，同机别家的 Agent 不会被碰。）
        """
        if not messagebox.askyesno(
                APP_TITLE,
                "将停止本安装目录下的**全部** Agent 进程，\n"
                "包括正在运行的那一个。\n\n"
                "适用场景：同一个商户码有多个实例在互相顶号。\n"
                "停完之后需要点『启动后台 Agent』重新接通。\n\n"
                "确定继续？"):
            self.log("已取消「停止全部 Agent」。", "meta")
            return
        scope = self._scope()
        killed = self.dm.kick_duplicates(exclude_pids=(os.getpid(),), scope=scope)
        if killed:
            self.log("已停止本安装目录下的 %d 个 Agent：" % len(killed), "warn")
            for p, desc in killed:
                self.log("    PID %-7d %s" % (p, desc), "warn")
            self.log("★ 现在点『启动后台 Agent』重新接通。", "ok")
        else:
            self.log("本安装目录没有找到正在运行的 Agent 进程。", "meta")
        others = self.dm.other_installs(scope)
        if others:
            self.log("本机另有 %d 个**不同目录**的 Agent 仍在运行（不是本窗口的，故意没停）"
                     % len(others), "meta")
        self._last_log_count = 0
        self._refresh()

    def on_close(self):
        """★ 关键：关窗口**不停** Agent。"""
        pid, alive = self.dm.read_pid()
        if alive:
            self.log("窗口关闭。后台 Agent（PID %d）继续运行。" % pid, "meta")
        self._save_geometry()      # 记住用户拉出来的窗口尺寸/位置
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass


def _dur(sec):
    sec = int(sec or 0)
    if sec < 60:
        return "%d 秒" % sec
    if sec < 3600:
        return "%d 分 %d 秒" % (sec // 60, sec % 60)
    return "%d 时 %d 分" % (sec // 3600, (sec % 3600) // 60)


def _ts(v):
    """云端给的是 unix 秒，转成本地时间的「月-日 时:分」。"""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(v)))
    except Exception:  # noqa: BLE001
        return "—"


def _idle(sec):
    """「多久没动静了」—— 会话表里 last_seen 与现在的差。"""
    sec = int(sec or 0)
    if sec < 60:
        return "%d 秒前" % sec
    if sec < 3600:
        return "%d 分钟前" % (sec // 60)
    if sec < 86400:
        return "%d 小时前" % (sec // 3600)
    return "%d 天前" % (sec // 86400)


# ------------------------------------------------------------------ 燕秀标签页

def yx_data_paths():
    """燕秀标签页用的两个文件：匹配记忆库 + 它自己的配置。

    ★ 刻意不用 Agent 的 config.json：燕秀那套「连接设置」是**整文件覆盖**写法
      （save_config 直接把 {"sqlserver":…, "hpdan":…} 写回去），两者共用一份
      文件会把 Agent 的 agent/relay 等字段抹掉。所以给它独立一份 yxconfig.json，
      首次使用时从 Agent 配置里抄一遍连接信息。
    """
    d = runtime_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return os.path.join(d, "yx_matchlib.db"), os.path.join(d, "yxconfig.json")


def seed_yx_config(agent_cfg_path, yx_cfg_path):
    """把 Agent 的 sql 连接信息抄成燕秀的配置（只在 yxconfig.json 不存在时做一次）。"""
    if os.path.isfile(yx_cfg_path):
        return
    src = agent_cfg_path or find_config()
    sql = {}
    if src and os.path.isfile(src):
        try:
            with open(src, encoding="utf-8") as fh:
                sql = (json.load(fh) or {}).get("sql") or {}
        except Exception:  # noqa: BLE001
            sql = {}
    data = {
        "sqlserver": {
            "server": sql.get("server", ""),
            "port": int(sql.get("port", 1433) or 1433),
            "database": sql.get("database", ""),
            "user": sql.get("user", ""),
            "password": sql.get("password", ""),
            "windows_auth": False,
            "tds_version": str(sql.get("tds_version", "7.0")),
            "table": "HPKhType",
            "field_match": "hpmckh",
            "field_spec": "GgxhKh",
            "field_spec2": "HpbhKh",
            "field_code": "hpbh",
            "field_unit": "HpUnitName",
        },
        # 客户类别编号按现网数据取 801-（旧环境是 21-），可在界面「连接设置」里改
        "hpdan": {"table": "hpdan", "name_field": "hpmc", "code_field": "hpbh",
                  "unit_field": "hpunit", "spec_field": "Ggxh", "lbtype_kh": "801-"},
    }
    try:
        with open(yx_cfg_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def startup_files(argv):
    """挑出被拖到 exe 图标上的文件（.xls/.xlsx），忽略 --config 这类开关。

    ★ 这就是「把客户单拖到图标上自动解析」的入口：Windows 会把拖上来的文件
      作为命令行参数传给 exe，与 `零件清单转换.exe` 原来的行为一致。
    """
    out, skip_next = [], False
    for a in list(argv)[1:]:
        if skip_next:
            skip_next = False
            continue
        if a == "--config":
            skip_next = True
            continue
        if a.startswith("-"):
            continue
        if a.lower().endswith((".xls", ".xlsx")) and os.path.isfile(a):
            out.append(a)
    return out


def main():
    # ★ DPI 必须在建根窗口**之前**声明，否则高分屏上整窗发虚。
    #   放在 main() 最前面：daemon 模式本来就没有窗口，声明它对进程无害。
    _init_dpi()

    # --__daemon 走纯后台，绝不建窗口
    if "--__daemon" in sys.argv:
        ensure_import_path()
        dm = _dm()
        cfg = None
        for i, a in enumerate(sys.argv):
            if a == "--config" and i + 1 < len(sys.argv):
                cfg = sys.argv[i + 1]
        sys.exit(dm.run_daemon(cfg))

    # --selfcheck：现场/远程排障用的只读自检。**这份 exe 是 --noconsole 的，
    #   print 对方看不见**，所以结果一律落 logs\selfcheck-<时间>.txt，
    #   由安装器的「现场自检」按钮或人工把文件发回来。
    if "--selfcheck" in sys.argv:
        ensure_import_path()
        cfg_path = None
        for i, a in enumerate(sys.argv):
            if a == "--config" and i + 1 < len(sys.argv):
                cfg_path = sys.argv[i + 1]
        cfg_path = cfg_path or find_config() or os.path.join(base_dir(), "config.json")
        # ★ 最后那个兜底是刻意的：连"在哪儿"都没定，报告就没地方写。
        #   指向 exe 同目录之后，即使那份配置根本不存在，也照样会写出一份
        #   「缺哪几项」的 selfcheck-*.txt —— 拷出去的人点自检才不会毫无反应。
        from v3agent import config as cfgmod
        from v3agent import selfcheck as sc
        cfg, _problems = cfgmod.load_any(cfg_path)   # 缺必填项也不抛，照样出报告
        try:
            res = sc.run(cfg, cfg_path)
        except Exception as exc:  # noqa: BLE001
            print("自检起不来：%s" % exc)
            sys.exit(3)
        # 控制台是 GBK，✓ 会直接抛 UnicodeEncodeError 把退出码带成 1；
        # --noconsole 的包里 sys.stdout 可能是 None，所以一律先判再打。
        try:
            if sys.stdout is not None:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                print(res["text"])
        except Exception:  # noqa: BLE001
            pass
        sys.exit(0 if res["ok"] else 1)

    root = _make_root()
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)

    tab_console = tk.Frame(nb)
    nb.add(tab_console, text="  运行状态  ")
    console = Console(root, tab_console)

    tab_yx = tk.Frame(nb)
    nb.add(tab_yx, text="  燕秀零件处理  ")
    yx_box = {"panel": None}

    def yx_panel():
        """燕秀面板：延迟创建（不用它就不加载 openpyxl/xlrd，也不碰记忆库）。"""
        if yx_box["panel"] is None:
            from v3agent.yxparts.ui import YxPartsPanel
            db_p, cfg_p = yx_data_paths()
            seed_yx_config(console.cfg_path, cfg_p)
            panel = YxPartsPanel(tab_yx, db_p, cfg_p, embedded=True)
            panel.pack(fill="both", expand=True)
            yx_box["panel"] = panel
        return yx_box["panel"]

    def _on_db_switched(old_db, new_db):
        """换账套 → 燕秀面板丢掉旧账的物料库（面板还没建过就不用管，建起来自然是新账）。"""
        panel = yx_box["panel"]
        if panel is None:
            return
        stale = panel.forget_library(old_db, new_db)
        console.log("燕秀页物料库已清空（%s → %s）%s"
                    % (old_db or "?", new_db,
                      "；之前匹配的 %d 行需要重新匹配" % stale if stale else ""), "warn")

    console.on_db_switched = _on_db_switched

    def _fit_window():
        """**不再改窗口尺寸**（2026-10-13）。

        原来第一次切到燕秀页时会把窗口撑到 ≥1150x660 —— 这就是"每切一次页大小都在变"
        的那一下。现在初始尺寸按两页真正需求一次算够（WIN_W/WIN_H，probe-gui-fit.py 量的），
        切页只切内容。函数留着是为了看清这段历史，别再往回加 geometry。
        """
        return

    files = startup_files(sys.argv)
    if files:
        # 拖到图标上启动：直接切到燕秀页并解析（用户看到的就是"拖进来就有结果"）
        nb.select(tab_yx)
        def _open_dropped():
            yx_panel()
            _fit_window()
            yx_box["panel"].open_paths(files)
        root.after(200, _open_dropped)
    else:
        def _on_tab(_event=None):
            try:
                if nb.select() == str(tab_yx):
                    yx_panel()
                    _fit_window()
            except Exception:  # noqa: BLE001
                pass
        nb.bind("<<NotebookTabChanged>>", _on_tab)

    root.mainloop()


if __name__ == "__main__":
    main()
