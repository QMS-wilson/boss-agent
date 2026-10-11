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


def _make_root():
    """建根窗口：有 tkinterdnd2 就用它的 Tk（子控件才能收文件拖拽），否则退回普通 Tk。"""
    if TkinterDnD is not None:
        try:
            return TkinterDnD.Tk()
        except Exception:  # noqa: BLE001
            pass
    return tk.Tk()


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


# ------------------------------------------------------------------ 控制面板

class Console:
    def __init__(self, root, body=None):
        self.root = root
        # body = 真正装控件的容器（标签页里是子 Frame）；root 只做窗口级操作
        self.body = body if body is not None else root
        root.title(APP_TITLE)
        root.geometry("820x600")
        root.minsize(660, 480)
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
        self._build_ui()
        self._load_config()
        self.discover_dbs()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(300, self._tick)
        self.log("控制台已就绪。", "info")
        self.log("提示：关掉本窗口不会影响 Agent —— 它会继续在后台运行。", "meta")

    # ---- UI

    def _build_ui(self):
        top = tk.Frame(self.body, bg="#0f172a")
        top.pack(fill="x")
        tk.Label(top, text=APP_TITLE, bg="#0f172a", fg="white",
                 font=("Microsoft YaHei UI", 13, "bold")).pack(side="left", padx=14, pady=10)
        self.lbl_state = tk.Label(top, text="检测中…", bg="#0f172a", fg="#94a3b8",
                                  font=("Microsoft YaHei UI", 11, "bold"))
        self.lbl_state.pack(side="right", padx=14)

        info = tk.Frame(self.body)
        info.pack(fill="x", padx=12, pady=(10, 4))
        self.lbl_agent = tk.Label(info, text="后台 Agent：检测中…", anchor="w",
                                  font=("Microsoft YaHei UI", 10), fg="#0f172a")
        self.lbl_agent.pack(fill="x")
        self.lbl_cfg = tk.Label(info, text="配置：—", anchor="w", fg="#475569")
        self.lbl_cfg.pack(fill="x")
        self.lbl_conn = tk.Label(info, text="云端：—", anchor="w", fg="#475569")
        self.lbl_conn.pack(fill="x")
        self.lbl_stat = tk.Label(info, text="接收 — / 发送 — / 运行 —", anchor="w", fg="#475569")
        self.lbl_stat.pack(fill="x")

        # ---- 本机配置（首启填表 / 改配置，2026-09-26）----
        # ★ 为什么给它一个常驻槽位而不是弹模态窗：填到一半想去看状态、想点「重新发现」，
        #   模态窗会把人关在里面。面板收起时只是个空槽，不占地方。
        self.cfg_slot = tk.Frame(self.body)
        self.cfg_slot.pack(fill="x", padx=12, pady=(0, 4))
        self._build_cfg_panel()

        # ---- 账套选择（2026-09-24）----
        # ★ 为什么放在 GUI 上：以前换账套要么手改 config.json、要么去点另一个目录的
        #   exe（两份 exe 各带一套库、还共用同一个注册名互相踢），出错时数字看着都
        #   正常但完全是另一套账。这里把选择收敛成一个动作，并且列表来自
        #   sys.databases + 业务表四件套实测，不让手输错库名。
        dbrow = tk.Frame(self.body)
        dbrow.pack(fill="x", padx=12, pady=(0, 6))
        tk.Label(dbrow, text="账套", font=("Microsoft YaHei UI", 10, "bold"),
                 fg="#0f172a").pack(side="left")
        self.cmb_db = ttk.Combobox(dbrow, width=24, state="readonly", values=[])
        self.cmb_db.pack(side="left", padx=6)
        tk.Button(dbrow, text="切换账套", width=10, bg="#7c3aed", fg="white",
                  font=("Microsoft YaHei UI", 10, "bold"),
                  command=self.switch_db).pack(side="left")
        tk.Button(dbrow, text="重新发现", width=10,
                  font=("Microsoft YaHei UI", 10),
                  command=self.discover_dbs).pack(side="left", padx=6)
        self.lbl_db = tk.Label(dbrow, text="当前：—", anchor="w", fg="#475569")
        self.lbl_db.pack(side="left", padx=8)

        btns = tk.Frame(self.body)
        btns.pack(fill="x", padx=12, pady=8)
        self.btn_start = tk.Button(btns, text="启动后台 Agent", width=15, bg="#16a34a", fg="white",
                                   font=("Microsoft YaHei UI", 10, "bold"), command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = tk.Button(btns, text="停止后台 Agent", width=15, bg="#ea580c", fg="white",
                                  font=("Microsoft YaHei UI", 10, "bold"), command=self.stop)
        self.btn_stop.pack(side="left", padx=8)
        tk.Button(btns, text="刷新状态", width=10, command=self.refresh_now,
                  font=("Microsoft YaHei UI", 10)).pack(side="left")
        tk.Button(btns, text="查看日志文件", width=12, command=self.open_log,
                  font=("Microsoft YaHei UI", 10)).pack(side="left", padx=8)
        tk.Button(btns, text="打开配置文件", width=12, command=self.open_cfg,
                  font=("Microsoft YaHei UI", 10)).pack(side="left")
        # ★ 界面里改配置 = 首启那条路（拷 exe 出来的人不该被要求会手写 JSON）
        tk.Button(btns, text="本机配置", width=10, bg="#1d4ed8", fg="white",
                  font=("Microsoft YaHei UI", 10, "bold"),
                  command=lambda: self.show_cfg(True)).pack(side="left", padx=8)
        tk.Button(btns, text="现场自检", width=12, bg="#0f766e", fg="white", command=self.run_selfcheck,
                  font=("Microsoft YaHei UI", 10, "bold")).pack(side="left", padx=8)

        bar = tk.Frame(self.body)
        bar.pack(fill="x", padx=12)
        tk.Label(bar, text="后台 Agent 最近日志（本窗口只是显示，关掉不影响它）",
                 anchor="w", fg="#64748b", font=("Microsoft YaHei UI", 9)).pack(fill="x")

        self.txt = scrolledtext.ScrolledText(self.body, height=18, state="disabled",
                                             font=("Consolas", 10), bg="#f8fafc",
                                             fg="#0f172a", relief="flat")
        self.txt.pack(fill="both", expand=True, padx=12, pady=(2, 12))
        for tag, color in (("info", "#334155"), ("ok", "#15803d"), ("warn", "#b45309"),
                           ("error", "#b91c1c"), ("req", "#1d4ed8"), ("meta", "#64748b")):
            self.txt.tag_config(tag, foreground=color)

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
        tk.Button(row, text="保存并应用", width=13, bg="#16a34a", fg="white",
                  font=("Microsoft YaHei UI", 10, "bold"),
                  command=self.save_cfg).pack(side="left")
        tk.Button(row, text="试连并列账套", width=13,
                  font=("Microsoft YaHei UI", 10),
                  command=self.probe_sql).pack(side="left", padx=6)
        tk.Button(row, text="显示口令", width=10,
                  font=("Microsoft YaHei UI", 10),
                  command=self.toggle_pwd).pack(side="left")
        tk.Button(row, text="收起", width=8, command=lambda: self.show_cfg(False),
                  font=("Microsoft YaHei UI", 10)).pack(side="right")
        self.cfg_panel.pack_forget()

    def show_cfg(self, open_it=True):
        """展开/收起配置面板。填完或配置已经可用时就收起来，别挡着状态行。"""
        if open_it:
            if not self.cfg_panel.winfo_ismapped():
                self.cfg_panel.pack(in_=self.cfg_slot, fill="x")
                self._prefill_cfg()
                self.root.geometry("820x780")
        else:
            self.cfg_panel.pack_forget()
            self.root.geometry("820x600")

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

    def log(self, msg, level="info"):
        self.txt.configure(state="normal")
        self.txt.insert("end", "%s\n" % msg, level)
        self.txt.see("end")
        self.txt.configure(state="disabled")

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
        self._refresh()

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

        if alive:
            self.lbl_state.configure(text="后台运行中", fg="#22c55e")
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self.lbl_agent.configure(
                text="后台 Agent：运行中（PID %d）" % pid, fg="#15803d")
        else:
            self.lbl_state.configure(text="未运行", fg="#94a3b8")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            # 也许有进程但 pid 文件丢了，兜底扫一遍
            # ★ 2026-09-20：这里以前每轮都无条件扫，而 agent_processes()
            #   内部会 spawn 一个 powershell.exe 取命令行 → 黑窗闪烁的元凶。
            #   现在 _iter_processes() 已改成纯 Win32 API（0.012s、零子进程），
            #   扫描本身不再有副作用；但为了不给「幽灵进程」制造假象，
            #   仍然只在**需要时**才提示，并把处置说得更明确。
            leftovers = self.dm.agent_processes(exclude_pids=(os.getpid(),))
            if leftovers:
                self.lbl_agent.configure(
                    text="后台 Agent：发现 %d 个残留进程但无 pid 文件 → 点『停止』清理"
                         % len(leftovers), fg="#b45309")
            else:
                self.lbl_agent.configure(text="后台 Agent：未运行", fg="#64748b")

        if st:
            conn = st.get("connected")
            state = st.get("state") or "?"
            stale = st.get("stale")
            # ★ 进程已死时，"已连接"是临终残影，不能照实显示
            if not alive:
                self.lbl_conn.configure(
                    text="云端：—（后台 Agent 已退出，下面是它退出前的最后记录）", fg="#b45309")
            elif stale:
                self.lbl_conn.configure(
                    text="云端：状态未更新 %ds（可能卡住，可点『刷新状态』）" % st.get("age_sec", 0),
                    fg="#b45309")
            else:
                self.lbl_conn.configure(
                    text="云端：%s" % ("已连接 ✓" if conn else state),
                    fg="#15803d" if conn else "#475569")
            up = st.get("uptime_sec") or 0
            self.lbl_stat.configure(
                text="接收 %s 条 / 发送 %s 条 / 运行 %s   （Agent 版本 %s，更新于 %s）" % (
                    st.get("recv"), st.get("sent"), _dur(up),
                    st.get("version") or "?", st.get("updated_at") or "?"))
            self._sync_log(st.get("logs") or [])
        else:
            if alive:
                self.lbl_conn.configure(text="云端：启动中…（后台进程刚起来，稍候）", fg="#b45309")
            self.lbl_stat.configure(text="接收 — / 发送 — / 运行 —")

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
        """把后台 Agent 的日志增量贴到窗口里。"""
        n = len(logs)
        if n == self._last_log_count:
            return
        fresh = logs[self._last_log_count:] if n > self._last_log_count else logs[-20:]
        self._last_log_count = n
        for item in fresh:
            self.log("[%s] %s" % (item.get("t", ""), item.get("msg", "")),
                     item.get("level", "info"))

    # ---- 动作

    def refresh_now(self):
        self._refresh()
        self.log("已刷新。", "meta")

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

    def on_close(self):
        """★ 关键：关窗口**不停** Agent。"""
        pid, alive = self.dm.read_pid()
        if alive:
            self.log("窗口关闭。后台 Agent（PID %d）继续运行。" % pid, "meta")
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

    def _fit_window():
        """燕秀页列比控制台多：窗口不够大时放大一次（只放大，不缩回去）。"""
        try:
            w, h = root.winfo_width(), root.winfo_height()
            if w < 1150 or h < 660:
                root.geometry("%dx%d" % (max(w, 1150), max(h, 660)))
        except Exception:  # noqa: BLE001
            pass

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
