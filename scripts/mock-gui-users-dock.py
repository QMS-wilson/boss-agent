# -*- coding: utf-8 -*-
"""效果图：把「在线用户」从弹窗搬回主界面（首页）常驻区，形状照当年日志区那块。

★ 为什么用真窗口截，而不是画一张 HTML：这一版的排版、字体、按钮纯色块都是 tkinter
  原生算出来的，画出来的图"好看但不等于做得出来"，量出来的高度/间距也不可信。
★ 表里那几行**不是编的**：直接拿本机 config.json 里的 agent.name/agent.secret 调云端
  `/agent/sessions`（和弹窗走同一条路），拿到几条截几条。云端连不上就明确写"读不到"，
  不摆假数据。

产出：<repo>/实况截图/效果图-agent首页-现状.png 与 -建议.png

用法：<py> scripts/mock-gui-users-dock.py [--rows N]
"""
import argparse
import json
import os
import sys
import time
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import ttk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import agent_gui as AG  # noqa: E402  （模块导入不建窗口，main 在 __main__ 里）

SHOT = os.path.join(ROOT, "实况截图")
COLS = ("state", "who", "kind", "dev", "ip", "login", "idle")
HEADS = (("state", "状态", 72), ("who", "账号", 170), ("kind", "类型", 84),
         ("dev", "设备 / 端", 168), ("ip", "来源 IP", 126),
         ("login", "登录于", 116), ("idle", "最后活跃", 104))
KINDS = {"app": "App 账号", "v3": "V3 操作员", "wechat": "微信"}


def real_rows(cfg, limit):
    """照 agent_gui 里那条现成的路子取真数据；失败就把原因带回去（不编数据）。"""
    a = (cfg or {}).get("agent") or {}
    s = (cfg or {}).get("sql") or {}
    base = str(a.get("relay_url") or "").replace("wss://", "https://").replace("ws://", "http://")
    base = base.split("/ws/")[0].rstrip("/")
    if not base or not a.get("name") or not a.get("secret"):
        return None, "配置里缺 relay_url / name / secret"
    body = {"agent": a.get("name"), "secret": a.get("secret"),
            "database": s.get("database") or ""}
    req = urllib.request.Request(base + "/agent/sessions",
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
        return None, "云端 HTTP %d：%s" % (exc.code, data.get("error") or "")
    except Exception as exc:  # noqa: BLE001
        return None, "连不上云端（%s）" % type(exc).__name__
    if not data.get("ok"):
        return None, str(data.get("error") or "云端返回失败")
    rows = (data.get("sessions") or [])[:limit]
    return {"rows": rows, "stats": data.get("stats") or {}}, None


def grab(root, path):
    """截整窗（含真标题栏）。
    ★ 别拿 `winfo_rootx/width` 当像素坐标：进程声明 DPI 感知之后，tk 的屏幕宽度是物理像素、
      而窗口几何又是另一套，照着截必然右边缺一截（本轮前两版就是这么被切掉的）。
      ⇒ 用 Win32 `GetWindowRect` 拿这个窗口在屏幕上的真实矩形。
    """
    import ctypes
    from ctypes import wintypes
    from PIL import ImageGrab
    root.update_idletasks()
    root.update()
    # ★ 抓之前压一层 topmost：这台机桌面上常开着 IDE / 真的 Agent 控制台，
    #   它们一盖过来，截到的就是别人的窗口（本轮有两张就这么废了）。
    root.attributes("-topmost", True)
    root.lift()
    root.update()
    time.sleep(0.35)
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        raise SystemExit("GetWindowRect 失败，截不了图")
    img = ImageGrab.grab().crop((rect.left, rect.top, rect.right, rect.bottom))
    root.attributes("-topmost", False)
    img.save(path)
    print("  [图] %s（%dx%d）" % (path, rect.right - rect.left, rect.bottom - rect.top))


def tree_metrics(con, ui):
    """行高与列宽都从**字体度量**算出来，不写死数字。

    ★ 为什么要算：ttk.Treeview 的行高来自主题默认值，**不跟着字体放大** ——
      这台机（1.5 倍 DPI + 微软雅黑 10）下默认行高装不下中文行，每行下半截被切，
      看着像"两行重叠"。写死一个 28 也一样会随字号/DPI 漂回去。
    """
    from tkinter import font as tkfont
    f = tkfont.Font(family=ui, size=10)
    st = ttk.Style(con.body)
    before = st.lookup("Treeview", "rowheight")
    ls = int(f.metrics("linespace"))
    rowh = ls + 8                      # 一行字 + 上下各 4px 呼吸
    st.configure("Treeview", rowheight=rowh, font=f, padding=(6, 3))
    st.configure("Treeview.Heading", font=tkfont.Font(family=ui, size=10, weight="bold"),
                 padding=(6, 5))
    return f, rowh, before


def measure_cols(f, values, heads, visible=None):
    """列宽 = 该列最宽内容（连表头一起量）+ 余量。

    ★ 原来这里夹了个 56~300 的硬上限，结果 1704 的窗口明明有余量，
      「账号 / 设备 · 端」还是被截 —— 上限不该拍数字，该由**可视宽**决定：
      量出来的总宽放得下就原样给；放不下才从最宽的那几列开始收，
      并且只给**真的被收过**的列补省略号。
    """
    need, floor = [], []
    for i, (key, text, _fb) in enumerate(heads):
        head = f.measure(text) + 18
        w = head
        for row in values:
            w = max(w, f.measure(str(row[i])) + 18)
        need.append(max(56, w))
        floor.append(max(56, head))          # ★ 收到表头宽就停，别把「已踢出」压成「已…」
    shrunk = [False] * len(need)
    if visible:
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
    return need, shrunk


def build_dock(con, payload, err, tuned=True, visible=None):
    """主界面底部常驻块：标题条 + 表格 + 动作条（形状照日志区当年那块）。"""
    ui = "Microsoft YaHei UI"
    box = tk.Frame(con.body, bg="#ffffff",
                   highlightbackground="#cbd5e1", highlightthickness=1)
    box.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    bar = tk.Frame(box, bg="#f8fafc")
    bar.pack(fill="x")
    tk.Label(bar, text="在线用户", bg="#f8fafc", fg="#0f172a",
             font=(ui, 11, "bold")).pack(side="left", padx=(10, 0), pady=6)
    tk.Label(bar, text="正在使用 App 的登录会话（数据来自云端）", bg="#f8fafc",
             fg="#64748b", font=(ui, 9)).pack(side="left", padx=(8, 0))
    stat = tk.Label(bar, text="—", bg="#f8fafc", fg="#475569", font=(ui, 9))
    stat.pack(side="right", padx=(0, 10))
    tk.Button(bar, text="刷新", command=lambda: None, bg="#ffffff", fg="#334155",
              relief="flat", bd=0, padx=10, pady=2, cursor="hand2",
              font=(ui, 9), highlightbackground="#cbd5e1",
              highlightthickness=1).pack(side="right", padx=8)

    # ---- 先把要显示的字算出来（列宽要拿它量，不能先建表再改）----
    values = []
    if not err:
        for rec in payload["rows"]:
            sub = str(rec.get("sub") or "")
            user = sub.split(":", 1)[1] if ":" in sub else sub
            dev = " · ".join(x for x in (rec.get("brand"), rec.get("model"),
                                         rec.get("client")) if x) or rec.get("platform") or "—"
            values.append((
                "已踢出" if rec.get("revoked") else ("在线" if rec.get("online") else "离线"),
                "%s（%s）" % (rec.get("name") or user, user),
                KINDS.get(rec.get("kind")) or rec.get("kind") or "—",
                dev, rec.get("ip") or "—", AG._ts(rec.get("login_at")),
                AG._idle(rec.get("idle_sec"))))

    rowh = None
    if tuned:
        fnt, rowh, _before = tree_metrics(con, ui)
        widths, shrunk = measure_cols(fnt, values, HEADS, visible=visible)
        # 只给**真的被收过**的列补省略号；没收到上限的列一律原样（原来 300 硬夹
        # 把「账号 / 设备·端」在 1704 的窗口里也截了，那是假截）
        values = [tuple(v[i] if not shrunk[i] or fnt.measure(str(v[i])) <= widths[i] - 12
                        else fit_text(fnt, v[i], widths[i] - 12)
                        for i in range(len(v))) for v in values]
    else:
        widths = [w for _k, _t, w in HEADS]

    tree = ttk.Treeview(box, columns=COLS, show="headings", height=7)
    for (key, text, _fb), width in zip(HEADS, widths):
        tree.heading(key, text=text)
        tree.column(key, width=width, anchor="w", stretch=(key == "who"))
    tree.pack(fill="both", expand=True, padx=8, pady=(6, 0))
    tree.tag_configure("off", foreground="#94a3b8")

    if err:
        stat.configure(text="读不到：" + err, fg="#b91c1c")
    else:
        st = payload["stats"]
        stat.configure(text="共 %s 条 · 在线 %s" % (st.get("total", len(payload["rows"])),
                                                   st.get("online", "—")))
        for v in values:
            tree.insert("", "end", values=v, tags=() if v[0] == "在线" else ("off",))
        if values:
            tree.selection_set(tree.get_children()[0])
    box.rowheight = rowh      # 截图脚本顺手带出来，报告里要说这个数
    box.tree = tree

    acts = tk.Frame(box, bg="#ffffff")
    acts.pack(fill="x", padx=8, pady=8)
    for text, bg in (("踢出这条", "#ea580c"), ("踢出该账号全部", "#ffffff"),
                     ("禁用账号", "#b91c1c")):
        fg = "#334155" if bg == "#ffffff" else "#ffffff"
        b = tk.Button(acts, text=text, command=lambda: None, bg=bg, fg=fg, relief="flat",
                      bd=0, padx=12, pady=5, cursor="hand2",
                      font=(ui, 10, "bold" if bg != "#ffffff" else "normal"),
                      activebackground=bg, activeforeground=fg)
        if bg == "#ffffff":
            b.configure(highlightbackground="#cbd5e1", highlightthickness=1)
        b.pack(side="left", padx=(0, 8))
    tk.Label(acts, text="踢出＝立刻断线（还能再登）；禁用＝以后不能登录",
             fg="#64748b", font=(ui, 9)).pack(side="left", padx=(6, 0))
    return box


def measure_fit(root, con, box):
    """量"这个尺寸到底装不装得下"：操作行最右那颗、表格最后一列，都得在窗口里。
    ★ 不靠肉眼看图判断 —— 数字说了算，换 DPI/换字号时这条判据自己会红。"""
    mb = None
    for f in con.body.winfo_children():
        for c in f.winfo_children():
            if isinstance(c, ttk.Menubutton):
                mb = c
    tree = box.tree
    total = sum(int(tree.column(k, "width")) for k in COLS)
    return {
        "窗口内宽": root.winfo_width(),
        "操作行最右到": (mb.winfo_rootx() + mb.winfo_width() - root.winfo_rootx()) if mb else None,
        "表格列总宽": total,
        "表格可视宽": tree.winfo_width(),
        "行高": box.rowheight,
    }


def hide_users_button(con):
    """搬上首页之后，操作行那颗「在线用户」就是重复入口 ⇒ 撤掉。"""
    n = 0
    for f in con.body.winfo_children():
        for c in f.winfo_children():
            if isinstance(c, tk.Button) and c.cget("text") == "在线用户":
                c.pack_forget()
                n += 1
    return n


def fit_text(f, text, cap):
    """超长值按列宽截断并补省略号 —— 原来夹到 300 是**硬切**，
    看着像数据本身少了几个字（「系统管理员（shiwantongda:SYS」那种）。"""
    s = str(text)
    if f.measure(s) <= cap:
        return s
    while s and f.measure(s + "…") > cap:
        s = s[:-1]
    return (s.rstrip("（( :·-") + "…") if s else "…"


def fix_dbrow(con):
    """账套行：把「允许读 N 本…」与右侧「配置：… Agent 名：…」挪到下面一行小字。
    原来它们同挤一行，长文案一来就和右对齐的标签重叠（1704 那张图上看得见尾巴）。"""
    ui = "Microsoft YaHei UI"
    dbrow = None
    for f in con.body.winfo_children():
        texts = " ".join(str(c.cget("text")) for c in f.winfo_children()
                         if isinstance(c, (tk.Button, tk.Label, ttk.Combobox)) and c.winfo_exists())
        if "账套" in texts and "切换" in texts:
            dbrow = f
    if dbrow is None:
        return None
    moved = []
    for c in dbrow.winfo_children():
        t = str(c.cget("text")) if isinstance(c, tk.Label) else ""
        if t.startswith("当前：") or t.startswith("配置："):
            moved.append(t)
            c.pack_forget()
    sub = tk.Frame(con.body, bg="#ffffff")
    sub.pack(after=dbrow, fill="x", padx=14, pady=(2, 0))   # ★ 紧跟账套行，别掉到页面末尾
    tk.Label(sub, text="   ·   ".join(moved) or "—", bg="#ffffff", fg="#64748b",
             font=(ui, 9), anchor="w", justify="left",
             wraplength=max(400, (con.body.winfo_width() or 1660) - 40)).pack(side="left", fill="x")
    return dbrow


def wrap_yx(panel, visible=1660):
    """燕秀工具行：**一颗都不藏**，改成换第二行。

    ★ 用户拍板（2026-10-13）：不要收进「更多」——原来 11 颗都在明面，
      藏起来等于把功能变少了。工具行需要 2436px 而窗口只 1704，
      所以让它按源码里**已有的分组线**折成两行：顺序、按钮、分组分隔线全保留。
    返回值：(第一行宽, 第二行宽, 折了几颗)
    """
    bar = None
    for c in panel.winfo_children():
        kids = c.winfo_children()
        if kids and any(isinstance(k, ttk.Button) and str(k.cget("text")) == "打开客户文件"
                        for k in kids):
            bar = c
    if bar is None:
        return None
    kids = bar.winfo_children()

    def slot_w(w):
        try:
            sl = w.pack_info()
            padx = sl.get("padx") or 0
            padx = sum(int(x) for x in padx) if isinstance(padx, (tuple, list)) else int(padx)
        except Exception:  # noqa: BLE001
            padx = 0
        return w.winfo_reqwidth() + padx

    cuts = [i for i, k in enumerate(kids) if isinstance(k, ttk.Separator)]
    split, acc = None, 0
    for i, k in enumerate(kids):
        acc += slot_w(k)
        if i in cuts and acc <= visible:
            split = i                      # 取"仍然放得下"的最后一条分隔线
    if split is None:
        return None
    row2 = ttk.Frame(panel, padding=(8, 0, 8, 6))
    row2.pack(after=bar, side=tk.TOP, fill=tk.X)
    moved = 0
    # ★ Tk 不允许把控件 pack 进"不是它父容器"的框里（试过，直接报 can't pack … inside …），
    #   所以第二行是**照原样重建**：文本、顺序、分组分隔线、command 都搬过来。
    for k in kids[split:]:
        if isinstance(k, ttk.Separator):
            ttk.Separator(row2, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
            continue
        if not isinstance(k, ttk.Button):
            continue
        t = str(k.cget("text"))
        side = tk.RIGHT if t == "使用说明" else tk.LEFT
        padx = (6, 0) if t not in ("导入Excel物料档案(备用)",) else 0
        try:
            cmd = k.cget("command")
        except Exception:  # noqa: BLE001
            cmd = None
        ttk.Button(row2, text=t, command=cmd).pack(side=side, padx=padx)
        k.pack_forget()
        k.destroy()
        moved += 1
    panel.update_idletasks()
    panel.update()
    return bar.winfo_reqwidth(), row2.winfo_reqwidth(), moved


def collapse_yx(panel):
    """燕秀工具行：只留主流程两颗，其余按原来那三条分组线收进「更多 ▾」。
    ★ 不重新定义哪些是低频 —— 就照源码里已有的分组（物料库一组、导入导出一组）。"""
    bar = None
    for c in panel.winfo_children():
        kids = c.winfo_children()
        if kids and any(isinstance(k, ttk.Button) and str(k.cget("text")) == "打开客户文件"
                        for k in kids):
            bar = c
    if bar is None:
        return None
    keep = {"打开客户文件", "自动匹配全部", "使用说明"}
    groups, cur = [], []
    for c in bar.winfo_children():
        if isinstance(c, ttk.Separator):
            if cur:
                groups.append(cur)
            cur = []
            continue
        if isinstance(c, ttk.Button):
            t = str(c.cget("text"))
            if t in keep:
                continue
            cur.append((t, c))
    if cur:
        groups.append(cur)
    for _t, c in [x for g in groups for x in g]:
        c.pack_forget()
    for c in bar.winfo_children():
        if isinstance(c, ttk.Separator):
            c.pack_forget()
    mb = ttk.Menubutton(bar, text="更多 ▾", width=9)
    menu = tk.Menu(mb, tearoff=0)
    for i, g in enumerate(groups):
        if i:
            menu.add_separator()
        for t, c in g:
            menu.add_command(label=t, command=lambda cmd=c.cget("command"): cmd())
    mb["menu"] = menu
    mb.pack(side=tk.LEFT, padx=(6, 0))
    return bar, sum(len(g) for g in groups)


def render_yx(root, con, size):
    """真·燕秀面板（同一份源码、embedded=True），收完工具行截一张。"""
    from v3agent.yxparts.ui import YxPartsPanel
    top = tk.Toplevel(root)
    top.title("燕秀零件处理（建议）· " + AG.APP_TITLE)
    top.geometry("%s+18+18" % size)
    db_p, cfg_p = AG.yx_data_paths()
    AG.seed_yx_config(con.cfg_path, cfg_p)
    panel = YxPartsPanel(top, db_p, cfg_p, embedded=True)
    panel.pack(fill="both", expand=True)
    root.update_idletasks()
    root.update()
    bar = panel.winfo_children()[0]
    before = bar.winfo_reqwidth()
    got = wrap_yx(panel, visible=1660)
    root.update_idletasks()
    root.update()
    grab(top, os.path.join(SHOT, "效果图-燕秀页-一起治.png"))
    top.destroy()
    return before, got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=7, help="表里最多截几条（默认 7 行）")
    ap.add_argument("--fixed", action="store_true",
                    help="再截一版「一起治」：账套行长文案挪到第二行 + 燕秀工具行收进「更多」")
    a = ap.parse_args()
    if not os.path.isdir(SHOT):
        os.makedirs(SHOT)

    cfg = {}
    # ★ 先读**在跑的那份**（D:\config.json）：仓库里那份是模板/另一台机，
    #   拿它的 secret 去调云端只会收到 403 bad secret。
    for p in (r"D:\config.json", os.path.join(ROOT, "config.json")):
        try:
            cfg = json.load(open(p, encoding="utf-8"))
            break
        except Exception:  # noqa: BLE001
            continue
    payload, err = real_rows(cfg, a.rows)
    print("真数据：%s" % ("%d 条会话" % len(payload["rows"]) if payload else "读不到 —— " + err))

    AG._init_dpi()
    root = tk.Tk()
    con = AG.Console(root)
    # ★ 把 GUI 的配置指到**在跑的那份**：Console 默认会去读仓库里的 config.json，
    #   那份的 agent.secret 与现网不同 ⇒ 弹窗自己调云端会回「bad secret」，
    #   截出来就是一张空表，白比一场。
    con.cfg = cfg
    con.cfg_path = next((p for p in (r"D:\config.json", os.path.join(ROOT, "config.json"))
                         if os.path.isfile(p)), None)
    # 三张图同一宽度：现状首页 / 现状弹窗 / 建议（首页常驻区），才比得出差别
    root.geometry("1500x760+15+12")
    root.update_idletasks()
    root.update()
    grab(root, os.path.join(SHOT, "效果图-agent首页-现状.png"))

    # 也截一张**现在的弹窗**：同一套 Treeview 代码，行高/列宽的问题两边一样才有可比性
    con.open_users_window()
    root.update_idletasks()
    root.update()
    pop = [w for w in root.winfo_children() if isinstance(w, tk.Toplevel)]
    if pop:
        pop[0].geometry("1180x440+180+120")
        grab(pop[0], os.path.join(SHOT, "效果图-agent首页-现状弹窗.png"))
    con._close_users_window()
    # ★ 关掉之后要等一下并置顶：Windows 关窗有淡出，不等就把主窗截进去的是那张残影
    #   （本轮第一版就这么截出了一张"首页上叠着半个弹窗"的假图）。
    time.sleep(1.0)
    root.lift()
    root.focus_force()
    time.sleep(0.4)

    root.geometry("1500x1010+15+12")
    n = hide_users_button(con)
    box = build_dock(con, payload, err, tuned=False)
    root.update_idletasks()
    root.update()
    grab(root, os.path.join(SHOT, "效果图-agent首页-建议-没调行高.png"))

    box.destroy()
    # 列宽的预算 = 窗口内宽扣掉左右留白与树自身边框（拿 dock 自己的 reqwidth 会算出小得离谱的预算）
    box = build_dock(con, payload, err, tuned=True, visible=root.winfo_width() - 44)
    root.update_idletasks()
    root.update()
    grab(root, os.path.join(SHOT, "效果图-agent首页-建议.png"))
    print("  · 操作行撤掉的重复入口：%d 个（「在线用户」按钮）" % n)
    print("  · 行高从主题默认值改成按字体算：Treeview rowheight = %s px" % box.rowheight)

    # ---- 关键一问：燕秀页那个初始尺寸（1360x800）够不够装下控制台自己的东西 ----
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    W, H = min(1360, sw - 40), min(800, sh - 80)
    root.geometry("%dx%d+15+12" % (W, H))
    root.update_idletasks()
    root.update()
    m = measure_fit(root, con, box)
    fits_btn = m["操作行最右到"] is not None and m["操作行最右到"] <= m["窗口内宽"] - 24
    fits_col = m["表格列总宽"] <= m["表格可视宽"]
    print("  · 按燕秀页尺寸当初始值：%dx%d（屏幕 %dx%d）" % (W, H, sw, sh))
    print("      操作行最右 %s / 可用 %s ⇒ %s"
          % (m["操作行最右到"], m["窗口内宽"] - 24, "装得下" if fits_btn else "★装不下"))
    print("      表格列总宽 %s / 可视 %s ⇒ %s"
          % (m["表格列总宽"], m["表格可视宽"], "装得下" if fits_col else "★装不下（最后一列会被切）"))
    root.lift(); root.focus_force(); time.sleep(0.6)
    grab(root, os.path.join(SHOT, "效果图-agent首页-初始1360x800.png"))

    # ---- 所以初始宽不能抄一个数：量出控制台自己需要多宽，与燕秀页取大 ----
    btns_row = None
    for f in con.body.winfo_children():
        if any(isinstance(c, tk.Button) and "启动后台" in str(c.cget("text"))
               for c in f.winfo_children()):
            btns_row = f
    need_btn = (btns_row.winfo_reqwidth() + 28) if btns_row else 0
    need_col = m["表格列总宽"] + 44
    need = max(need_btn, need_col)
    W2 = max(W, min(sw - 40, need))
    print("  · 控制台自己需要：操作行 %d、表格 %d ⇒ 取 %d；燕秀页 %d ⇒ 初始宽 %d"
          % (need_btn, need_col, need, W, W2))
    root.geometry("%dx%d+15+12" % (W2, H))
    root.update_idletasks()
    root.update()
    m2 = measure_fit(root, con, box)
    ok2 = m2["操作行最右到"] <= m2["窗口内宽"] - 24 and m2["表格列总宽"] <= m2["表格可视宽"]
    print("      复核：操作行最右 %s / 可用 %s，列总宽 %s / 可视 %s ⇒ %s"
          % (m2["操作行最右到"], m2["窗口内宽"] - 24, m2["表格列总宽"],
             m2["表格可视宽"], "都装得下" if ok2 else "★还差"))
    root.lift(); root.focus_force(); time.sleep(0.6)
    grab(root, os.path.join(SHOT, "效果图-agent首页-初始按量取宽.png"))
    print("  · 高度：控制台自然需要 %d px（燕秀页给的初始高 %d）" % (root.winfo_reqheight(), H))

    # ---- 建议的单一初始尺寸 ----
    # 1704 = probe-gui-fit.py 量出来的"燕秀表格 13 列合计 1660 + 余量"（两页里最宽的内容列）
    # 902  = 首页带「在线用户」常驻区时的自然高
    root.geometry("1704x902+8+8")
    root.update_idletasks()
    root.update()
    m3 = measure_fit(root, con, box)
    print("  · 建议初始 1704x902 复核：操作行最右 %s / 可用 %d，列总宽 %s / 可视 %s"
          % (m3["操作行最右到"], m3["窗口内宽"] - 24, m3["表格列总宽"], m3["表格可视宽"]))
    root.lift(); root.focus_force(); time.sleep(0.6)
    grab(root, os.path.join(SHOT, "效果图-agent首页-初始1704x902.png"))

    if "--fixed" in sys.argv:
        # ---- 一起治：账套行长文案不再和右侧标签挤、燕秀工具行收进「更多」----
        box.destroy()
        box = build_dock(con, payload, err, tuned=True, visible=root.winfo_width() - 44)
        fix_dbrow(con)
        root.update_idletasks()
        root.update()
        root.lift(); root.focus_force(); time.sleep(0.5)
        grab(root, os.path.join(SHOT, "效果图-agent首页-一起治.png"))
        before, got = render_yx(root, con, "1704x902")
        if got:
            r1, r2, moved = got
            print("  · 燕秀工具行：折 %d 颗到第二行，第一行 %d + 第二行 %d（原来单行 %d，一颗都没藏）"
                  % (moved, r1, r2, before))
        else:
            print("  · 燕秀工具行：没找到折行点（★说明窗口还是太窄）")
    root.destroy()


if __name__ == "__main__":
    main()
