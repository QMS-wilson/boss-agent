# -*- coding: utf-8 -*-
"""CRM 本地 Agent 一键管理台（安装/启动/停止/自启，tkinter）。

打包：PyInstaller --onefile --noconsole --uac-admin，
并把 **V3AgentGui.exe** 以 --add-binary 打进本程序；运行时从 sys._MEIPASS 取出安装。

★★ 装的必须是 V3AgentGui.exe，不是 V3Agent.exe（scripts/build_agent.py 那条线 2026-09-20
   之后就没再出过包，dist 里只有 V3AgentGui.exe；照旧名装 = 一键安装必然失败）
★ 计划任务必须带 `--__daemon --config <绝对路径>`：裸跑 V3AgentGui.exe 开的是 tkinter 面板，
   不连云端；而不给 --config 时 P1-4 之后连 config 都找不到（ONSTART 的工作目录不是安装目录），
   结果是"任务注册成功、开机后集体掉线"。
"""

import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent_daemon as dm  # noqa: E402  DAEMON_FLAG / pid / status / 进程枚举的唯一来源

TASK_NAME = "V3LocalAgent"
CREATE_NO_WINDOW = 0x08000000
AGENT_EXE = "V3AgentGui.exe"

# 商户码规则：与 cloudapi/insights.py:22、cloudapi/admin.py:247、
# 进销存-uniapp/src/utils/merchant.js:17、deploy.py 的 nginx 正则**同一套**。
# 放宽任何一处，出事都在第二家身上（前缀进 nginx、进令牌 claims、进存储键）。
CODE_RE = re.compile(r"^[a-z][a-z0-9-]{1,29}$")
CODE_RESERVED = ("", "h5", "web", "index", "static", "assets", "admin", "api", "ws", "logs")


def _frozen_base():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _agent_exe_source():
    p = os.path.join(_frozen_base(), AGENT_EXE)
    if os.path.isfile(p):
        return p
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist", AGENT_EXE)


def _run_cmd(cmd, timeout=60):
    try:
        # ★ 必须 CREATE_NO_WINDOW：安装器是 --noconsole 的，但 schtasks/taskkill/tasklist
        #   都是控制台程序，不加这个在客户机上就是"点一下黑窗乱闪"（verify-exe-no-popups 的旧课）。
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


class SetupApp:
    def __init__(self, root):
        self.root = root
        root.title("CRM 本地 Agent 一键管理台")
        root.geometry("620x560")
        self.vars = {}
        fields = [
            ("dir", "安装目录", r"C:\V3Agent"),
            ("relay", "云端地址 (wss://域名/ws/agent)", "ws://149.88.75.125:8000/ws/agent"),
            # ★ 名称/密钥/库名一律**不再预填任何一家商户的真实值**。
            #   原来默认值是 Agent 名称 shiwantongda、数据库 tongda ——
            #   给第二家装机时只要没改这两个，就会拿第一家的身份上线：
            #   云端同名只留一条连接，第一家当场被顶下线（实测 3 分钟重连 34 次）。
            #   留空 = 装机时必须显式填，填错也只会错在"这一台"，不会串到别家。
            ("name", "Agent 名称（=商户码，小写字母开头）", ""),
            ("secret", "Agent 密钥（云端后台生成后粘贴）", ""),
            ("sql_server", "SQL 服务器", "127.0.0.1"),
            ("sql_port", "SQL 端口", "1433"),
            ("sql_db", "数据库（该商户的账套名）", ""),
            ("sql_user", "SQL 账号", "sa"),
            ("sql_pwd", "SQL 密码", ""),
        ]
        pad = 12
        for i, (key, label, default) in enumerate(fields):
            tk.Label(root, text=label, anchor="w").grid(row=i, column=0, sticky="w", padx=pad, pady=3)
            show = "*" if key in ("secret", "sql_pwd") else None
            var = tk.StringVar(value=default)
            ent = tk.Entry(root, textvariable=var, show=show or "", width=46)
            ent.grid(row=i, column=1, sticky="we", padx=pad, pady=3)
            self.vars[key] = var
        btns = tk.Frame(root)
        btns.grid(row=len(fields), column=0, columnspan=2, pady=10)
        tk.Button(btns, text="一键安装并启动", command=self.install, bg="#2563eb", fg="white").pack(side="left", padx=6)
        tk.Button(btns, text="启动 Agent", command=self.start_agent, bg="#16a34a", fg="white").pack(side="left", padx=6)
        tk.Button(btns, text="停止 Agent", command=self.stop_agent, bg="#ea580c", fg="white").pack(side="left", padx=6)
        tk.Button(btns, text="只保存配置", command=self.save_only).pack(side="left", padx=6)
        tk.Button(btns, text="查状态", command=self.status).pack(side="left", padx=6)
        tk.Button(btns, text="现场自检", command=self.run_selfcheck, bg="#0f766e", fg="white").pack(side="left", padx=6)
        tk.Button(btns, text="卸载", command=self.uninstall, bg="#dc2626", fg="white").pack(side="left", padx=6)
        self.log = scrolledtext.ScrolledText(root, height=12, state="disabled", font=("Consolas", 10))
        self.log.grid(row=len(fields) + 1, column=0, columnspan=2, padx=pad, pady=8, sticky="nsew")
        root.grid_rowconfigure(len(fields) + 1, weight=1)
        root.grid_columnconfigure(1, weight=1)
        self.prefill_from_installed()
        self.append("填好上方信息后点「一键安装并启动」；已安装时可点「启动 Agent / 停止 Agent」。\n")

    def prefill_from_installed(self):
        for d in (self.vars["dir"].get().strip() or r"C:\V3Agent", os.path.dirname(os.path.abspath(__file__))):
            cfg_path = os.path.join(d, "config.json")
            if not os.path.isfile(cfg_path):
                continue
            try:
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh)
                self.vars["dir"].set(d)
                self.vars["name"].set(str(cfg.get("agent", {}).get("name") or self.vars["name"].get()))
                # ★ 不再把已装的 secret / SQL 密码回读到输入框：
                #   这台机器可能是上一家的，明文回填等于顺手把别人的凭据
                #   展示给下一个装机的人（而且看起来像"默认值"被人误用到别家）。
                relay = str(cfg.get("agent", {}).get("relay_url") or self.vars["relay"].get())
                self.vars["relay"].set(relay)
                sql = cfg.get("sql", {})
                self.vars["sql_server"].set(str(sql.get("server") or self.vars["sql_server"].get()))
                self.vars["sql_port"].set(str(sql.get("port") or self.vars["sql_port"].get()))
                self.vars["sql_db"].set(str(sql.get("database") or self.vars["sql_db"].get()))
                self.vars["sql_user"].set(str(sql.get("user") or self.vars["sql_user"].get()))
                self.append("检测到已有安装（%s），已带入名称与连接参数；"
                            "密钥与 SQL 密码需重新粘贴。" % d)
                break
            except Exception:  # noqa: BLE001
                continue

    def append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.root.update()

    def config(self, install_dir):
        return {
            "sql": {
                "server": self.vars["sql_server"].get().strip(),
                "port": int(self.vars["sql_port"].get().strip() or 1433),
                "user": self.vars["sql_user"].get().strip(),
                "password": self.vars["sql_pwd"].get(),
                "database": self.vars["sql_db"].get().strip(),
                "tds_version": "7.0",
                "login_timeout": 10,
                "timeout": 120,
            },
            "agent": {
                "name": self.vars["name"].get().strip(),
                "secret": self.vars["secret"].get().strip(),
                "relay_url": self.vars["relay"].get().strip(),
                "reconnect_sec": 5,
                "heartbeat_sec": 25,
            },
            "price": {"default_price_column": "D_SJ1", "recent_price_field": "D_PriceTax"},
            "log_level": "INFO",
        }

    def write_files(self):
        d = self.vars["dir"].get().strip() or r"C:\V3Agent"
        os.makedirs(d, exist_ok=True)
        cfg = self.config(d)
        name = str(cfg["agent"]["name"] or "").strip()
        # 名称就是商户码：格式收紧，避免"石湾同达分店"这种带空格/中文的名字
        # 进了云端注册表以后没法在 X-Store、备份目录、计划任务名里安全使用
        if not CODE_RE.match(name or ""):
            raise ValueError("Agent 名称要用商户码：小写字母开头、2-30 位、只用小写字母/数字/-"
                             "（例如 shiwantongda、fs-tongda）")
        if name in CODE_RESERVED:
            raise ValueError("商户码 %s 是访问路径的保留字，不能当商户码（会把 %s/ 这个入口遮掉）"
                             % (name, name))
        if not cfg["agent"]["secret"]:
            raise ValueError("Agent 密钥不能为空（请在云端后台『Agent』页生成后粘贴）")
        if len(cfg["agent"]["secret"]) < 8:
            raise ValueError("Agent 密钥至少 8 位")
        if not str(cfg["sql"]["database"] or "").strip():
            raise ValueError("数据库（该商户的账套名）不能为空 —— 留空会连到错误的账套")
        # ★ SQL 凭据从"跟着默认值走"改成"装机必须填"：
        #   v3agent/config.py 的 sa/123 兜底同时被删掉并列入 REQUIRED，
        #   所以这里留空 = Agent 起来就拒绝上线，必须在安装器上问清楚。
        if not str(cfg["sql"]["user"] or "").strip():
            raise ValueError("SQL 账号不能为空（本机 SQL Server 的登录名，通常 sa）")
        if not str(cfg["sql"]["password"] or ""):
            raise ValueError("SQL 密码不能为空 —— 不再兜底 sa/123，留空 Agent 会拒绝启动")
        if not cfg["agent"]["relay_url"].startswith("ws"):
            raise ValueError("云端地址需要 wss:// 或 ws:// 开头")
        with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        src = _agent_exe_source()
        if not os.path.isfile(src):
            raise FileNotFoundError("找不到内置的 %s：安装包出错了，或 dist 里没有这份 exe" % AGENT_EXE)
        shutil.copy2(src, os.path.join(d, AGENT_EXE))
        # 上一代安装器写的文件名留着会让"到底哪份在跑"再次变成谜题
        for stale in ("V3Agent.exe",):
            p = os.path.join(d, stale)
            if os.path.isfile(p):
                try:
                    os.remove(p)
                    self.append("已删除旧版残留 %s（现在跑的是 %s）" % (stale, AGENT_EXE))
                except OSError as exc:
                    self.append("★ 旧版 %s 删不掉：%s —— 请手工删，别让它和新的混淆" % (stale, exc))
        self.append(f"已写入 {d}\\config.json 和 {AGENT_EXE}（商户码 {name}，账套 {cfg['sql']['database']}）")
        return d

    def _task_action(self):
        """开机自启要跑的那条命令 —— 必须带 --__daemon 和 config 的绝对路径。

        ONSTART 任务的工作目录不是安装目录，裸 exe 会因为找不到 config.json 而拒绝启动
        （P1-4 起 config.py 不再兜 agent.name / sql.database）。
        """
        d, exe = self._agent_exe()
        return '"%s" %s --config "%s"' % (exe, dm.DAEMON_FLAG, os.path.join(d, "config.json"))

    def _runtime_is(self, d):
        """把 agent_daemon 的 runtime 目录指到本次安装目录。

        源码模式跑安装器时 runtime_dir() 会落到仓库根 logs，读到的 pid/status
        就不是这台机器这个目录的；打包模式下 exe 同目录本来就对，设了也一致。
        """
        os.environ["V3AGENT_RUNTIME_DIR"] = os.path.join(d, "logs")

    def _read_status(self):
        d, _ = self._agent_exe()
        self._runtime_is(d)
        try:
            p = dm.status_path()
            if not os.path.isfile(p):
                return None
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return None

    def report_running(self, wait_sec=30):
        """装机现场唯一有效的判据：实测账套 + 是否连上云端，不是"任务注册成功"。

        2026-09-26 00:38 那起事故（agent_name=A 而 database_verified=B，页面数字看着全正常）
        在这里会被直接读出来，所以每次安装/启动后都要走这一遍。
        """
        d, _ = self._agent_exe()
        self._runtime_is(d)
        want_db = ""
        try:
            with open(os.path.join(d, "config.json"), encoding="utf-8") as fh:
                want_db = str(json.load(fh).get("sql", {}).get("database") or "")
        except Exception:  # noqa: BLE001
            pass
        st = None
        deadline = time.time() + max(0, wait_sec)
        while True:
            st = self._read_status()
            if st and (st.get("database_verified") or st.get("connected")):
                break
            if time.time() >= deadline:
                break
            time.sleep(2)
        if not st:
            self.append("★ 没读到 %s\\logs\\agent.status.json —— Agent 没起来。"
                        "先看 %s\\logs\\agent.log 的最后几行。" % (d, d))
            return False
        got_db = str(st.get("database_verified") or "")
        ver = str(st.get("version") or "")
        online = bool(st.get("connected"))
        self.append("实测账套：%s    配置账套：%s    版本：%s    云端：%s" % (
            got_db or "(无)", want_db or "(无)", ver or "(无)",
            "已连接" if online else "未连接"))
        bad = []
        if not got_db:
            bad.append("没有实测账套 —— 连不上 SQL Server 或库名写错"
                       "（要看本机有哪些账套：在安装目录跑 python scripts\\switch-db.py --list）")
        elif want_db and got_db != want_db:
            bad.append("实测与配置不一致 —— 这正是「用 A 家的名字读 B 家的账」，客户看到的数字来自另一套账")
        if not online:
            bad.append("没连上云端 —— 查商户码/密钥/云端地址，以及这台机器能不能出网")
        for b in bad:
            self.append("✗ " + b)
        if not bad:
            self.append("✓ 可以交付：现在这一台读的就是「%s」这套账，且已连上云端。" % got_db)
        return not bad

    @staticmethod
    def task_name(cfg):
        # 计划任务名带商户码：以前是常量 V3LocalAgent，
        #   一台机器上装第二家（或重装到别的目录）会互相覆盖开机自启项
        return "V3Agent-%s" % (cfg["agent"]["name"] or "default")

    def task_names(self):
        """本商户的任务名 + 历史常量名（老安装要还能被停掉/删掉）。"""
        names = []
        try:
            names.append(self.task_name(self.config(self.vars["dir"].get().strip() or r"C:\V3Agent")))
        except Exception:  # noqa: BLE001
            pass
        names.append(TASK_NAME)
        out = []
        for n in names:
            if n and n not in out:
                out.append(n)
        return out

    def _scope(self):
        """本机这一份安装的"地盘"= 安装目录。

        ★ 同一台机器上装两家（或一个老板两个账套各起一份）时，
          清理残留只能清自己目录里的，绝不能把别人那份停掉 ——
          见 agent_daemon.agent_processes 的 scope 参数。
        """
        return os.path.normcase(os.path.abspath(self.vars["dir"].get().strip() or r"C:\V3Agent"))

    def install(self):
        try:
            d = self.write_files()
            self._runtime_is(d)
            tn = self.task_names()[0]
            rc, out = _run_cmd(["schtasks", "/Create", "/F", "/TN", tn,
                                "/TR", self._task_action(), "/SC", "ONSTART",
                                "/RU", "SYSTEM", "/RL", "HIGHEST"])
            self.append("注册开机自启（任务名 %s）：" % tn + ("成功" if rc == 0 else "失败 " + out.strip()))
            if rc != 0:
                self.append("★ 开机自启没注册上，这台机器重启后 Agent 不会自己起来，"
                            "要么解决权限（需要管理员），要么先手工点「启动 Agent」。")
            rc2, out2 = _run_cmd(["schtasks", "/Run", "/TN", tn])
            self.append("启动 Agent：" + ("已启动" if rc2 == 0 else "失败 " + out2.strip()))
            self.append("日志在 " + os.path.join(d, "logs", "agent.log"))
            self.report_running()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("安装失败", str(exc))
            self.append("失败：" + str(exc))

    def save_only(self):
        try:
            d = self.write_files()
            self._runtime_is(d)
            self.append("配置已保存（未安装开机自启）。可点「启动 Agent」，"
                        "或直接跑：%s %s --config \"%s\""
                        % (os.path.join(d, AGENT_EXE), dm.DAEMON_FLAG, os.path.join(d, "config.json")))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))

    def _agent_exe(self):
        d = self.vars["dir"].get().strip() or r"C:\V3Agent"
        return d, os.path.join(d, AGENT_EXE)

    def start_agent(self):
        try:
            d, exe = self._agent_exe()
            cfg = os.path.join(d, "config.json")
            if not os.path.isfile(exe) or not os.path.isfile(cfg):
                messagebox.showinfo("提示", "安装目录缺少 %s/config.json，请先「一键安装并启动」或「只保存配置」。" % AGENT_EXE)
                return
            self._runtime_is(d)
            os.makedirs(os.path.join(d, "logs"), exist_ok=True)
            killed = dm.kick_duplicates(scope=self._scope())
            if killed:
                self.append("启动前清掉本目录的残留 Agent %d 个（云端同名只留一条连接，必须清）" % len(killed))
            # ★ 必须带 --__daemon 和 --config 绝对路径：裸跑这份 exe 开的是 tkinter 面板，不连云端。
            #   pid 文件交给 daemon 自己写（它写的是真正跑 Python 的那个 PID）。
            proc = subprocess.Popen([exe, dm.DAEMON_FLAG, "--config", cfg], cwd=d,
                                    creationflags=dm.SPAWN_FLAGS)
            self.append("Agent 已拉起 (PID %d)，日志见 %s\\logs\\agent.log" % (proc.pid, d))
            self.report_running()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("启动失败", str(exc))
            self.append("启动失败：" + str(exc))

    def stop_agent(self, silent=False):
        try:
            d, _ = self._agent_exe()
            self._runtime_is(d)
            ok, msg = dm.stop_daemon(scope=self._scope())
            ended = []
            for tn in self.task_names():
                rc, _out = _run_cmd(["schtasks", "/End", "/TN", tn])
                if rc == 0:
                    ended.append(tn)
            if ended:
                msg += "；已终止计划任务 " + "、".join(ended)
            if not ok and not ended:
                msg += "（其他目录/别家的 Agent 不在停用范围内，故意不动）"
            self.append(msg)
            if not silent:
                messagebox.showinfo("停止", msg)
        except Exception as exc:  # noqa: BLE001
            if not silent:
                messagebox.showerror("停止失败", str(exc))
            self.append("停止失败：" + str(exc))

    def status(self):
        d, exe = self._agent_exe()
        self._runtime_is(d)
        pid, alive = dm.read_pid()
        mine = [p for p, _ in dm.agent_processes(scope=self._scope())]
        self.append("进程：PID %s（%s）" % (pid or "无", "在运行" if alive or mine else "未运行"))
        for tn in self.task_names():
            rc, _out = _run_cmd(["schtasks", "/Query", "/TN", tn, "/V", "/FO", "LIST"])
            if rc == 0:
                self.append("开机自启任务：已注册（任务名 %s，命令：%s）\n" % (tn, self._task_action()))
                break
        else:
            self.append("开机自启任务：未注册\n")
        # 状态里必须看得见"这是哪家、连的是哪套账"——
        # 本项目最难查的故障就是名字与账套不一致而界面上一切正常。
        # ★ 这里读的是**磁盘上那份 config.json**，不是输入框：
        #   输入框可能还留着上一家的参数，拿它报状态就等于报一个不存在的安装。
        try:
            with open(os.path.join(d, "config.json"), encoding="utf-8") as fh:
                c = json.load(fh)
            self.append("商户码：%s    账套(配置写的)：%s    云端：%s" % (
                (c.get("agent") or {}).get("name") or "(空)",
                (c.get("sql") or {}).get("database") or "(空)",
                (c.get("agent") or {}).get("relay_url") or "(空)"))
        except Exception as exc:  # noqa: BLE001
            self.append("读取 %s\\config.json 失败：%s" % (d, exc))
        self.report_running(wait_sec=0)
        others = dm.agent_processes()
        foreign = [x for x in others if x[0] not in mine]
        if foreign:
            self.append("★ 本机还跑着**别的目录**的 Agent（不会互相停掉）：" +
                        "、".join("PID %d %s" % (p, desc) for p, desc in foreign))
        self.append("安装目录：" + d)
        self.append("Agent 日志：" + os.path.join(d, "logs", "agent.log"))

    def run_selfcheck(self):
        """现场自检：跑**装好的那份 exe**，把结果文件显示出来（远程排障就发这个文件）。

        ★ 用安装目录里的 exe 跑，而不是用安装器自己跑：
          测的必须是将来真在客户机上常驻的那个东西，否则"自检通过"没有意义。
        """
        try:
            d, exe = self._agent_exe()
            cfg = os.path.join(d, "config.json")
            if not os.path.isfile(exe) or not os.path.isfile(cfg):
                messagebox.showinfo("提示", "先「一键安装并启动」，自检要跑安装目录里的那份 exe。")
                return
            logs = os.path.join(d, "logs")
            before = set(glob.glob(os.path.join(logs, "selfcheck-*.txt")))
            self.append("开始现场自检（只读：不写业务数据、不改配置）…")
            self.root.update()
            rc, out = _run_cmd([exe, "--selfcheck", "--config", cfg], timeout=200)
            fresh = sorted(set(glob.glob(os.path.join(logs, "selfcheck-*.txt"))) - before)
            if fresh:
                text = io.open(fresh[-1], encoding="utf-8").read()
                for line in text.splitlines():
                    self.append(line)
                self.append("结果文件：" + fresh[-1])
                self.append("★ 远程支持时把这一个文件发回来就够，不用截图、也不用登进对方电脑。")
            else:
                self.append("✗ 没拿到自检文件（退出码 %s）：%s" % (rc, (out or "")[:300]))
                self.append("   多半是这份 exe 太旧（不认识 --selfcheck）—— 用新安装包重装一次。")
            self.report_running(wait_sec=0)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("自检失败", str(exc))
            self.append("自检失败：" + str(exc))

    def uninstall(self):
        names = self.task_names()
        if not messagebox.askyesno("确认", "停止并删除开机自启任务：%s？" % "、".join(names)):
            return
        for tn in names:
            _run_cmd(["schtasks", "/End", "/TN", tn])
            rc, out = _run_cmd(["schtasks", "/Delete", "/TN", tn, "/F"])
            if rc == 0:
                self.append("已删除任务 " + tn)
        if messagebox.askyesno("确认", "是否同时删除安装目录（含 config.json 与日志）？"):
            d = self.vars["dir"].get().strip() or r"C:\V3Agent"
            try:
                shutil.rmtree(d, ignore_errors=True)
                self.append(f"已删除 {d}")
            except Exception as exc:  # noqa: BLE001
                self.append("删除失败：" + str(exc))


def main():
    root = tk.Tk()
    SetupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
