# -*- coding: utf-8 -*-
"""燕秀零件处理界面（Tkinter + 拖拽）。

本文件同时服务两种用法：
  * **嵌进 Agent 控制台**：``YxPartsPanel(master_frame, ...)`` 是一个普通 Frame，
    放进标签页即可（Agent 的根窗口已换成 TkinterDnD.Tk，拖拽可用）；
  * **独立桌面版**：``launch(db_path, config_path)`` 自己建根窗口，行为与
    原来的「零件清单转换.exe」一致。

移植时相对原版只动了三处：包内相对导入、窗口级调用（标题/尺寸）只在独立模式下执行、
拖拽注册容错（根窗口不是 TkinterDnD.Tk 时跳过，不报错）。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tkinterdnd2 import DND_FILES, TkinterDnD

from .config import load_config, save_config
from .core import Matcher, PartRow, norm_text, parse_file
from .core import parse_import_rows
from .exporter import export_excel
from .sqlsource import (
    fetch_hpdan_lookup,
    fetch_hpdan_sys_map,
    fetch_items,
    insert_customer_items,
    test_connection,
)
from .storage import Storage


def _init_dpi():
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _login_failed_hint(exc_text: str) -> str:
    if "18456" in exc_text or "登录失败" in exc_text:
        return (
            "\n补充：报错 18456 = 当前 Windows 用户没有该数据库的登录权限。\n"
            "解决办法：在数据库里为该用户创建 Windows 登录名，或改用 SQL 账号登录（取消勾选“Windows 认证”）。"
        )
    return ""


def _exc_text(exc) -> str:
    """把 pymssql 报错里的中文消息还原出来。"""
    args = getattr(exc, "args", None)
    if isinstance(args, (tuple, list)) and len(args) >= 2 and isinstance(args[1], bytes):
        try:
            return f"服务器错误 {args[0]}：{args[1].decode('utf-8', errors='replace')}"
        except Exception:
            pass
    return str(exc)


def _perm_hint(exc_text: str) -> str:
    low = exc_text.lower()
    if "permission" in low or "denied" in low:
        return (
            "\n补充：这是数据库权限问题——当前账号没有写入 HPKhType 的权限。\n"
            "请在数据库里执行（替换成你的账号）：GRANT INSERT ON dbo.HPKhType TO [你的账号];"
        )
    return ""


COLUMNS = (
    ("seq", "序号", 55),
    ("status", "状态", 85),
    ("merge", "客户产品名称", 270),
    ("sys_name", "系统产品名称", 230),
    ("unit", "单位", 60),
    ("qty", "数量", 60),
    ("remark", "备注", 150),
    ("mold", "模号", 130),
    ("name", "零件名称", 130),
    ("spec", "采购规格", 150),
    ("actual", "实际尺寸", 110),
    ("material", "材料", 70),
    ("source", "匹配来源", 160),
)


class YxPartsPanel(ttk.Frame):
    """燕秀零件处理面板。既可独立开窗，也可嵌进 Agent 控制台的标签页。"""

    def __init__(self, master, db_path, config_path, startup_files=None, embedded=True):
        super().__init__(master)
        self.embedded = embedded
        if not embedded:
            top = self.winfo_toplevel()
            top.title("客户零件清单 → ERP 导入转换工具")
            top.geometry("1360x800")
            top.minsize(1150, 660)
        try:
            self.option_add("*Font", ("Microsoft YaHei UI", 10))
        except Exception:
            pass
        self._setup_style()

        self.db_path = db_path
        self.config_path = config_path
        self.config = load_config(config_path)
        self.storage = Storage(db_path)
        self.matcher = Matcher(self.storage)
        self.rows: list[PartRow] = []

        self._build_toolbar()
        self._build_table()
        self._build_detail()
        self._build_status()
        self._refresh_status()

        # 拖拽支持：根窗口必须是 TkinterDnD.Tk（Agent 控制台已经是）；否则跳过
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

        # 启动时带入的文件（拖到 exe 图标上会自动作为参数传入）
        if startup_files:
            self.after(300, lambda: self.open_paths(startup_files))

    # ---------------- 界面搭建 ----------------
    def _setup_style(self):
        """行高按微软雅黑字号调大，避免文字被裁掉。"""
        style = ttk.Style(self)
        try:
            style.configure("Treeview", rowheight=30)
            style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10), padding=(4, 4))
            style.configure("TButton", padding=(6, 3))
            style.configure("TEntry", padding=(3, 3))
        except Exception:
            pass

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        ttk.Button(bar, text="打开客户文件", command=self.open_files).pack(side=tk.LEFT)
        ttk.Button(bar, text="自动匹配全部", command=self.auto_match_all).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Button(bar, text="连接SQL Server物料库", command=self.connect_sql).pack(side=tk.LEFT)
        ttk.Button(bar, text="连接设置", command=self.open_settings).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="物料库预览", command=self.open_item_preview).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="导入燕秀品名", command=self.open_import_dialog).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Button(bar, text="导入Excel物料档案(备用)", command=self.import_erp).pack(side=tk.LEFT)
        ttk.Button(bar, text="导出导入文件", command=self.export).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="匹配记忆库", command=self.open_memory).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="清空列表", command=self.clear_rows).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="使用说明", command=self.show_help).pack(side=tk.RIGHT)

    def _build_table(self):
        wrap = ttk.Frame(self, padding=(8, 0, 8, 4))
        wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in COLUMNS], show="headings")
        for key, text, width in COLUMNS:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=tk.W, stretch=(key in ("merge", "name", "spec", "actual", "source")))
        self.tree.tag_configure("unmatched", foreground="#c62828")
        self.tree.tag_configure("matched", foreground="#1b5e20")
        self.tree.tag_configure("manual", foreground="#0b4da2")
        self.tree.tag_configure("odd", background="#f7f7f7")
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

    def _build_detail(self):
        self.detail = tk.Text(self, height=4, state=tk.DISABLED, wrap=tk.WORD)
        self.detail.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 4))

    def _build_status(self):
        self.status_var = tk.StringVar()
        ttk.Label(self, textvariable=self.status_var, anchor=tk.W).pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=3)

    # ---------------- 数据操作 ----------------
    def open_files(self):
        paths = filedialog.askopenfilenames(
            title="选择客户发来的零件清单（也可以直接把文件拖进窗口）",
            filetypes=[("Excel 文件", "*.xls *.xlsx"), ("所有文件", "*.*")],
        )
        if paths:
            self.open_paths(paths)

    def open_paths(self, paths):
        total = 0
        skipped = 0
        for p in paths:
            p = str(p)
            if not p.lower().endswith((".xls", ".xlsx")):
                skipped += 1
                continue
            try:
                rows = parse_file(p)
            except Exception as exc:
                messagebox.showerror("解析失败", f"{p}\n{exc}")
                continue
            if not rows:
                skipped += 1
            total += len(rows)
            self._add_rows(rows)
        msg = f"共识别出 {total} 行零件。"
        if skipped:
            msg += f"\n{skipped} 个文件不是 Excel 或没有标准表头（零件名称/采购规格），已跳过。"
        messagebox.showinfo("打开完成", msg)

    def _on_drop(self, event):
        files = self.tk.splitlist(event.data)
        if files:
            self.open_paths(files)

    def _add_rows(self, new_rows: list[PartRow]):
        start = len(self.rows)
        self.rows.extend(new_rows)
        for row in new_rows:
            self.matcher.auto_match(row)
        self._refresh(start)
        self._refresh_status()

    def auto_match_all(self):
        if not self.rows:
            messagebox.showinfo("提示", "请先打开客户文件。")
            return
        for row in self.rows:
            self.matcher.auto_match(row)
        self._refresh()
        self._refresh_status()
        matched = sum(1 for r in self.rows if r.erp_code)
        messagebox.showinfo("自动匹配", f"已重新匹配，{matched} 行找到 hpbh。")

    # ---------------- SQL Server ----------------
    def open_settings(self):
        SettingsDialog(self, self.config_path, lambda cfg: setattr(self, "config", cfg))

    def connect_sql(self):
        cfg = self.config.get("sqlserver", {})
        if not cfg.get("server"):
            messagebox.showinfo("提示", "请先填写 SQL Server 连接设置。")
            self.open_settings()
            return
        try:
            rows = fetch_items(cfg)
        except Exception as exc:
            hint = _login_failed_hint(str(exc))
            messagebox.showerror(
                "连接失败",
                f"无法读取 HPKhType：\n{_exc_text(exc)}\n{hint}\n\n排查建议：\n"
                "1. 服务器地址是否填对（IP 或 主机名）？\n"
                "2. 在数据库本机运行软件时，请填 localhost 或 127.0.0.1，不要填公网 IP。\n"
                "3. SQL Server 是命名实例（如 主机名\\实例名）时，1433 端口可能不通，需填实例名或实际端口。\n"
                "4. 在数据库电脑上确认：SQL Server 服务已启动、TCP/IP 协议已启用（SQL Server 配置管理器→网络配置→协议→TCP/IP 已启用）。\n"
                "5. 防火墙是否放行 1433 端口？\n"
                "6. SSMS 里是否勾选“允许远程连接到此服务器”？\n"
                "7. 账号是否有 SELECT 权限？",
            )
            return
        sys_map, missing_sys = fetch_hpdan_sys_map(self.config)
        self.matcher.load_hpdan(rows, sys_map)
        if self.rows:
            for row in self.rows:
                self.matcher.auto_match(row)
            self._refresh()
        self._refresh_status()
        msg = f"已加载 {len(rows)} 条货品资料，并重新匹配当前列表。"
        if missing_sys:
            msg += (
                f"\n\n注意：hpdan 表里没找到字段 {', '.join(missing_sys)}，"
                "系统产品名/规格将为空，请在连接设置里核对字段名。"
            )
        messagebox.showinfo("连接成功", msg)

    def open_item_preview(self):
        ItemPreviewDialog(self, self.matcher.items)

    def open_import_dialog(self):
        ImportDialog(self)

    # ---------------- Excel 备用数据源 ----------------
    def import_erp(self):
        path = filedialog.askopenfilename(
            title="选择 ERP 导出的物料档案（备用数据源）",
            filetypes=[("Excel 文件", "*.xls *.xlsx"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            added = self.storage.import_erp_items(path)
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        self.matcher.load_from_storage()
        if self.rows:
            for row in self.rows:
                self.matcher.auto_match(row)
            self._refresh()
        self._refresh_status()
        messagebox.showinfo(
            "导入完成",
            f"新增 {added} 条物料。\n要求表头包含：编码/编号、名称（规格、单位、材料可选）。\n"
            "注意：正式使用时请用“连接SQL Server物料库”，Excel 导入只是备用。",
        )

    # ---------------- 导出 ----------------
    def export(self):
        if not self.rows:
            messagebox.showinfo("提示", "请先打开客户文件。")
            return
        matched = sum(1 for r in self.rows if r.erp_code)
        if matched == 0:
            messagebox.showinfo("提示", "当前没有已匹配的行，请先连接物料库并完成匹配。")
            return
        path = filedialog.asksaveasfilename(
            title="保存 ERP 导入文件",
            defaultextension=".xls",
            initialfile="ERP导入数据.xls",
            filetypes=[("Excel97-2003 工作簿", "*.xls")],
        )
        if not path:
            return
        try:
            m, u = export_excel(self.rows, path)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        msg = f"已导出到：\n{path}\n\n导入数据 {m} 行，未匹配 {u} 行（在“未匹配”工作表）。"
        if u:
            msg += "\n\n未匹配的行不会进入“导入数据”，请先双击补全 hpbh 后再导出。"
        messagebox.showinfo("导出完成", msg)

    def clear_rows(self):
        if not self.rows:
            return
        if messagebox.askyesno("清空", "确定清空当前列表？匹配记忆库不会被删除。"):
            self.rows.clear()
            self.tree.delete(*self.tree.get_children())
            self._refresh_status()

    # ---------------- 表格刷新 ----------------
    def _row_tag(self, row: PartRow) -> str:
        if row.status == "未匹配":
            return "unmatched"
        if row.status == "手动匹配":
            return "manual"
        return "matched"

    def _refresh(self, start: int = 0):
        if start == 0:
            self.tree.delete(*self.tree.get_children())
        for i in range(start, len(self.rows)):
            row = self.rows[i]
            qty = int(row.qty) if row.qty == int(row.qty) else row.qty
            tags = [self._row_tag(row)]
            if i % 2:
                tags.append("odd")
            self.tree.insert(
                "",
                tk.END,
                iid=str(i),
                values=(
                    row.seq,
                    row.status,
                    row.merge,
                    row.sys_name,
                    row.erp_unit,
                    qty,
                    row.remark,
                    row.mold_no,
                    row.name,
                    row.spec,
                    row.actual,
                    row.material,
                    row.source,
                ),
                tags=tags,
            )

    def _refresh_status(self):
        total = len(self.rows)
        matched = sum(1 for r in self.rows if r.erp_code)
        self.status_var.set(
            f"共 {total} 行 ｜ 已匹配 {matched} 行 ｜ 未匹配 {total - matched} 行 ｜ "
            f"物料库：{self.matcher.source_label} ｜ 匹配记忆 {len(self.storage.all_memory())} 条"
        )

    def _update_row(self, index: int):
        row = self.rows[index]
        qty = int(row.qty) if row.qty == int(row.qty) else row.qty
        tags = [self._row_tag(row)]
        if index % 2:
            tags.append("odd")
        self.tree.item(
            str(index),
            values=(
                row.seq,
                row.status,
                row.merge,
                row.sys_name,
                row.erp_unit,
                qty,
                row.remark,
                row.mold_no,
                row.name,
                row.spec,
                row.actual,
                row.material,
                row.source,
            ),
            tags=tags,
        )
        self._refresh_status()

    # ---------------- 事件 ----------------
    def on_double_click(self, event):
        iid = self.tree.focus()
        if not iid:
            return
        index = int(iid)
        MatchDialog(self, self.rows[index], self.matcher, self._apply_manual)

    def on_select(self, event):
        iid = self.tree.focus()
        if not iid:
            return
        row = self.rows[int(iid)]
        lines = [
            f"客户产品名称：{row.merge} ｜ 模号 {row.mold_no} ｜ 序号 {row.seq} ｜ 来源 {row.source_file}（{row.sheet}）",
            f"零件名称：{row.name} ｜ 采购规格：{row.spec} ｜ 实际尺寸：{row.actual} ｜ 材料：{row.material} ｜ 数量：{row.qty}",
            f"热处理：{row.heat} ｜ 厂家：{row.vendor} ｜ 客户零件编号：{row.part_no_orig or '（空）'}",
            f"备注：{row.remark or '（空）'}",
        ]
        self.detail.config(state=tk.NORMAL)
        self.detail.delete("1.0", tk.END)
        self.detail.insert(tk.END, "\n".join(lines))
        self.detail.config(state=tk.DISABLED)

    def _apply_manual(self, row: PartRow, code: str, unit: str, remember: bool):
        row.erp_code = code
        row.erp_unit = unit
        self.matcher.fill_sys(row)
        row.status = "手动匹配"
        row.source = "人工确认"
        if remember:
            if row.merge_a_n:
                self.storage.save_memory(row.merge_a_n, row.merge, code, unit)
            # 键B同样要求“实际尺寸”非空才记，避免记忆里出现只有名称的键
            if row.merge_b_n and row.merge_b_n != row.merge_a_n and norm_text(row.actual):
                self.storage.save_memory(row.merge_b_n, row.merge_b, code, unit)
        index = self.rows.index(row)
        self._update_row(index)

    # ---------------- 其他窗口 ----------------
    def open_memory(self):
        MemoryDialog(self, self.storage)

    @staticmethod
    def show_help():
        messagebox.showinfo(
            "使用说明",
            "1. 打开客户发来的零件清单：点“打开客户文件”，或直接把文件拖进窗口，也可以拖到 exe 图标上。\n\n"
            "2. 点“连接设置”填好 SQL Server 信息（服务器/库名/账号或Windows认证），再点“连接SQL Server物料库”，"
            "程序读取 HPKhType，先按“零件名称+采购规格”匹配 Hpmckh+GgxhKh，"
            "匹配不上再按“零件名称+实际尺寸”匹配 Hpmckh+HpbhKh，取回 hpbh 和单位。\n\n"
            "3. 未匹配的行双击：按相似度推荐候选，选中或手填 hpbh 后点“确定并记住”，下次同样写法自动匹配。\n\n"
            "4. 点“导出导入文件”生成 Excel97-2003 (.xls)，列：hpbh | HpUnitName | sl | dj | bz_xsmx，"
            "dj 暂时留空；未匹配的行单独列在“未匹配”工作表。\n\n"
            "5. “导入Excel物料档案(备用)”仅在暂时连不上数据库时用于测试。",
        )


class SettingsDialog(tk.Toplevel):
    def __init__(self, master: App, config_path, on_saved):
        super().__init__(master)
        self.config_path = config_path
        self.on_saved = on_saved
        self.title("SQL Server 连接设置")
        self.geometry("560x620")
        self.minsize(500, 420)
        self.resizable(True, True)
        self.transient(master)
        self.grab_set()

        # 底部按钮固定在窗口下方
        btns = ttk.Frame(self, padding=12)
        btns.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(btns, text="测试连接", command=self.test).pack(side=tk.LEFT)
        ttk.Button(btns, text="保存", command=self.save).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btns, text="取消", command=self.destroy).pack(side=tk.RIGHT)

        # 中间内容区可滚动、可拉伸
        outer = ttk.Frame(self)
        outer.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(outer, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        cfg = master.config.get("sqlserver", {})
        self.vars = {}

        fields = [
            ("server", "服务器（IP或主机名）", cfg.get("server", "")),
            ("port", "端口", cfg.get("port", 1433)),
            ("database", "数据库", cfg.get("database", "")),
            ("user", "用户名", cfg.get("user", "")),
            ("password", "密码", cfg.get("password", "")),
            ("tds_version", "TDS协议版本", cfg.get("tds_version", "7.0")),
            ("table", "表名", cfg.get("table", "HPKhType")),
            ("field_match", "匹配字段(hpmckh)", cfg.get("field_match", "hpmckh")),
            ("field_spec", "规格字段A(GgxhKh)", cfg.get("field_spec", "GgxhKh")),
            ("field_spec2", "规格字段B(HpbhKh)", cfg.get("field_spec2", "HpbhKh")),
            ("field_code", "编号字段(hpbh)", cfg.get("field_code", "hpbh")),
            ("field_unit", "单位字段(HpUnitName)", cfg.get("field_unit", "HpUnitName")),
        ]
        body = ttk.Frame(self.canvas, padding=12)
        win_id = self.canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(win_id, width=e.width))
        for i, (key, label, val) in enumerate(fields):
            ttk.Label(body, text=label).grid(row=i, column=0, sticky=tk.W, pady=3)
            var = tk.StringVar(value=str(val))
            self.vars[key] = var
            entry = ttk.Entry(body, textvariable=var, width=36)
            entry.grid(row=i, column=1, sticky=tk.EW, pady=3)
        self.win_auth = tk.BooleanVar(value=bool(cfg.get("windows_auth", True)))
        ttk.Checkbutton(body, text="Windows 认证（当前用户登录）", variable=self.win_auth).grid(
            row=len(fields), column=0, columnspan=2, sticky=tk.W, pady=6
        )

        # hpdan 货品对照表（导入客户品名用）
        hp = master.config.get("hpdan", {})
        self.hpdan_vars = {}
        sep_row = len(fields) + 2
        ttk.Label(body, text="—— 货品对照表 hpdan（“导入客户品名”时取编号/单位） ——").grid(
            row=sep_row, column=0, columnspan=2, sticky=tk.W, pady=(10, 2)
        )
        hp_fields = [
            ("hp_table", "hpdan 表名", hp.get("table", "hpdan")),
            ("hp_name", "货品名称字段", hp.get("name_field", "hpmc")),
            ("hp_code", "货品编号字段", hp.get("code_field", "hpbh")),
            ("hp_unit", "货品单位字段", hp.get("unit_field", "hpunit")),
            ("hp_spec", "系统规格字段(Ggxh)", hp.get("spec_field", "Ggxh")),
            ("hp_lbtype", "客户类别编号(LbType_Kh)", hp.get("lbtype_kh", "21-")),
        ]
        for i, (key, label, val) in enumerate(hp_fields):
            r = sep_row + 1 + i
            ttk.Label(body, text=label).grid(row=r, column=0, sticky=tk.W, pady=3)
            var = tk.StringVar(value=str(val))
            self.hpdan_vars[key] = var
            ttk.Entry(body, textvariable=var, width=36).grid(row=r, column=1, sticky=tk.EW, pady=3)
        body.columnconfigure(1, weight=1)

        # 鼠标滚轮滚动（Windows）
        self.bind("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _collect(self) -> dict:
        try:
            port = int(self.vars["port"].get().strip() or 1433)
        except ValueError:
            port = 1433
        return {
            "server": self.vars["server"].get().strip(),
            "port": port,
            "database": self.vars["database"].get().strip(),
            "user": self.vars["user"].get().strip(),
            "password": self.vars["password"].get(),
            "windows_auth": bool(self.win_auth.get()),
            "tds_version": self.vars["tds_version"].get().strip() or "7.0",
            "table": self.vars["table"].get().strip() or "HPKhType",
            "field_match": self.vars["field_match"].get().strip() or "hpmckh",
            "field_spec": self.vars["field_spec"].get().strip() or "GgxhKh",
            "field_spec2": self.vars["field_spec2"].get().strip() or "HpbhKh",
            "field_code": self.vars["field_code"].get().strip() or "hpbh",
            "field_unit": self.vars["field_unit"].get().strip() or "HpUnitName",
        }

    def test(self):
        cfg = self._collect()
        if not cfg["server"]:
            messagebox.showwarning("提示", "请先填写“服务器”地址（IP 或 主机名），数据库不在本机。", parent=self)
            return
        ok, msg = test_connection(cfg)
        if ok:
            messagebox.showinfo("测试连接", msg, parent=self)
        else:
            hint = _login_failed_hint(msg)
            messagebox.showerror(
                "测试连接失败",
                f"{msg}\n{hint}\n\n排查建议：\n"
                "1. 服务器地址是否填对（IP 或 主机名）？\n"
                "2. 在数据库本机运行软件时，请填 localhost 或 127.0.0.1，不要填公网 IP。\n"
                "3. SQL Server 是命名实例（如 主机名\\实例名）时，1433 端口可能不通，需填实例名或实际端口。\n"
                "4. 在数据库电脑上确认：SQL Server 服务已启动、TCP/IP 协议已启用（SQL Server 配置管理器→网络配置→协议→TCP/IP 已启用）。\n"
                "5. 防火墙是否放行 1433 端口？\n"
                "6. SSMS 里是否勾选“允许远程连接到此服务器”？\n"
                "7. 如果用 SQL 账号登录，数据库需启用混合身份验证；Windows 认证则勾选“Windows 认证”。",
                parent=self,
            )

    def save(self):
        hp = {
            "table": self.hpdan_vars["hp_table"].get().strip() or "hpdan",
            "name_field": self.hpdan_vars["hp_name"].get().strip() or "hpmc",
            "code_field": self.hpdan_vars["hp_code"].get().strip() or "hpbh",
            "unit_field": self.hpdan_vars["hp_unit"].get().strip() or "hpunit",
            "spec_field": self.hpdan_vars["hp_spec"].get().strip() or "Ggxh",
            "lbtype_kh": self.hpdan_vars["hp_lbtype"].get().strip() or "21-",
        }
        data = {"sqlserver": self._collect(), "hpdan": hp}
        save_config(self.config_path, data)
        self.on_saved(data)
        messagebox.showinfo("已保存", "连接设置已保存。", parent=self)
        self.destroy()


class MatchDialog(tk.Toplevel):
    def __init__(self, master: App, row: PartRow, matcher: Matcher, on_confirm):
        super().__init__(master)
        self.row = row
        self.matcher = matcher
        self.on_confirm = on_confirm
        self.title(f"匹配：{row.merge}")
        self.geometry("1020x560")
        self.transient(master)
        self.grab_set()

        info = (
            f"客户产品名称：{row.merge}\n"
            f"材料：{row.material} ｜ 数量：{row.qty} ｜ 热处理：{row.heat} ｜ 厂家：{row.vendor}\n"
            f"备注：{row.remark or '（空）'} ｜ 模号：{row.mold_no} ｜ 来源：{row.source_file}"
        )
        ttk.Label(self, text=info, justify=tk.LEFT, wraplength=880).pack(anchor=tk.W, padx=10, pady=(10, 6))

        manual = ttk.Frame(self)
        manual.pack(fill=tk.X, padx=10, pady=4)
        ttk.Label(manual, text="hpbh（货品编号）：").pack(side=tk.LEFT)
        self.code_var = tk.StringVar()
        ttk.Entry(manual, textvariable=self.code_var, width=16).pack(side=tk.LEFT)
        ttk.Label(manual, text="单位：").pack(side=tk.LEFT, padx=(10, 0))
        self.unit_var = tk.StringVar()
        ttk.Entry(manual, textvariable=self.unit_var, width=10).pack(side=tk.LEFT)
        ttk.Button(manual, text="使用输入值", command=self.use_input).pack(side=tk.LEFT, padx=8)

        ttk.Label(self, text="候选物料（双击选择，或在下面输入关键字过滤）：").pack(anchor=tk.W, padx=10, pady=(8, 2))
        search_bar = ttk.Frame(self)
        search_bar.pack(fill=tk.X, padx=10)
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        ttk.Entry(search_bar, textvariable=self.search_var).pack(fill=tk.X)

        wrap = ttk.Frame(self)
        wrap.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        cols = (
            ("code", "hpbh", 90),
            ("name", "名称(hpmckh)", 170),
            ("hpmc", "Hpmc(系统产品名)", 190),
            ("speca", "规格A(GgxhKh)", 150),
            ("specb", "规格B(HpbhKh)", 130),
            ("unit", "单位", 60),
            ("score", "相似度", 70),
        )
        self.cand = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings", height=12)
        for key, text, width in cols:
            self.cand.heading(key, text=text)
            self.cand.column(key, width=width, anchor=tk.W)
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.cand.yview)
        self.cand.configure(yscrollcommand=vsb.set)
        self.cand.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        self.cand.bind("<Double-1>", self.pick_candidate)

        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(btns, text="确定并记住", command=lambda: self._confirm(True)).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btns, text="仅本次匹配", command=lambda: self._confirm(False)).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btns, text="取消", command=self.destroy).pack(side=tk.RIGHT)

        self._candidates = []
        self._load_candidates()

    def _load_candidates(self):
        self._candidates = self.matcher.suggest(self.row)
        self._apply_filter()

    def _apply_filter(self):
        self.cand.delete(*self.cand.get_children())
        kw = norm_text(self.search_var.get())
        shown = 0
        for score, it in self._candidates:
            if kw:
                sys_name = self._sys_name(it)
                blob = (
                    (it.get("name_n") or "")
                    + (it.get("spec_a_n") or "")
                    + (it.get("spec_b_n") or "")
                    + (it.get("code") or "")
                    + norm_text(sys_name)
                )
                if kw not in blob:
                    continue
            self.cand.insert(
                "",
                tk.END,
                values=(
                    it.get("code") or "",
                    it.get("name") or "",
                    self._sys_name(it),
                    it.get("spec_a") or "",
                    it.get("spec_b") or "",
                    it.get("unit") or "",
                    f"{score * 100:.0f}%",
                ),
            )
            shown += 1
        if shown == 0:
            self.cand.insert("", tk.END, values=("", "（没有候选，请检查物料库或手动输入 hpbh）", "", "", "", "", ""))

    def _sys_name(self, it: dict) -> str:
        """候选对应的系统产品名：优先取 hpdan 的 hpmc，其次物料自带名称。"""
        code = (it.get("code") or "").strip()
        info = self.matcher.sys_map.get(code) if code else None
        if info and info.get("sys_name"):
            return info["sys_name"]
        return it.get("sys_name") or ""

    def pick_candidate(self, event):
        iid = self.cand.focus()
        if not iid:
            return
        vals = self.cand.item(iid, "values")
        if vals and vals[0] not in ("", None):
            self.code_var.set(vals[0])
            self.unit_var.set(vals[5])

    def use_input(self):
        code = self.code_var.get().strip()
        if not code:
            messagebox.showwarning("提示", "请先填写 hpbh。", parent=self)
            return
        self._confirm(True)

    def _confirm(self, remember: bool):
        code = self.code_var.get().strip()
        if not code:
            messagebox.showwarning("提示", "请选择候选或填写 hpbh。", parent=self)
            return
        self.on_confirm(self.row, code, self.unit_var.get().strip(), remember)
        self.destroy()


class ItemPreviewDialog(tk.Toplevel):
    """查看从 HPKhType 读出来的物料（用于核对字段内容）。"""

    def __init__(self, master: App, items: list[dict]):
        super().__init__(master)
        self.title("物料库预览（HPKhType）")
        self.geometry("1100x600")
        self.minsize(800, 400)
        self.transient(master)

        ttk.Label(
            self,
            text=f"共 {len(items)} 条。核对 hpmckh / GgxhKh / HpbhKh 的实际内容，以及生成的匹配键。",
            wraplength=1060,
        ).pack(anchor=tk.W, padx=10, pady=(10, 4))

        wrap = ttk.Frame(self)
        wrap.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        cols = (
            ("code", "hpbh", 90),
            ("name", "hpmckh", 240),
            ("speca", "GgxhKh", 180),
            ("specb", "HpbhKh", 150),
            ("unit", "HpUnitName", 70),
            ("keya", "匹配键A(名称+规格A)", 260),
            ("keyb", "匹配键B(名称+规格B)", 260),
        )
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings")
        for key, text, width in cols:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=tk.W, stretch=(key in ("name", "speca", "keya", "keyb")))
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        for i, it in enumerate(items[:1000]):
            self.tree.insert(
                "",
                tk.END,
                iid=str(i),
                values=(
                    it.get("code") or "",
                    it.get("name") or "",
                    it.get("spec_a") or "",
                    it.get("spec_b") or "",
                    it.get("unit") or "",
                    it.get("key_a") or "",
                    it.get("key_b") or "",
                ),
            )

        ttk.Button(self, text="关闭", command=self.destroy).pack(anchor=tk.E, padx=10, pady=8)


class ImportDialog(tk.Toplevel):
    """导入客户品名：Excel → 查 hpdan 取编号/单位 → 写入 HPKhType。"""

    def __init__(self, master: App):
        super().__init__(master)
        self.master_app = master
        self.config = master.config
        self.title("导入燕秀品名（写入 HPKhType）")
        self.geometry("1120x640")
        self.minsize(920, 500)
        self.transient(master)

        self.rows: list[dict] = []
        self.statuses: list[str] = []

        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.pack(fill=tk.X)
        ttk.Button(top, text="选择Excel", command=self.pick_file).pack(side=tk.LEFT)
        ttk.Button(top, text="读取货品对照(hpdan)", command=self.load_lookup).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Label(top, text="客户类别编号(LbType_Kh)：").pack(side=tk.LEFT, padx=(16, 0))
        self.lbtype_var = tk.StringVar(value=str(self.config.get("hpdan", {}).get("lbtype_kh", "21-")))
        ttk.Entry(top, textvariable=self.lbtype_var, width=8).pack(side=tk.LEFT)
        ttk.Label(top, text="表格格式：货品名称 | 客户品名 | 采购规格 | 实际尺寸", foreground="#555555").pack(
            side=tk.LEFT, padx=16
        )

        wrap = ttk.Frame(self)
        wrap.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        cols = (
            ("goods", "货品名称", 150),
            ("cust", "客户品名", 210),
            ("spec", "采购规格", 170),
            ("actual", "实际尺寸", 110),
            ("code", "Hpbh", 90),
            ("unit", "单位", 60),
            ("status", "状态", 150),
        )
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings")
        for key, text, width in cols:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=tk.W, stretch=(key in ("goods", "cust", "spec", "status")))
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        btns = ttk.Frame(self, padding=10)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="开始导入", command=self.do_import).pack(side=tk.RIGHT)
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side=tk.RIGHT, padx=(6, 0))

    def pick_file(self):
        path = filedialog.askopenfilename(
            title="选择燕秀品名导入表",
            filetypes=[("Excel 文件", "*.xls *.xlsx"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            rows = parse_import_rows(path)
        except Exception as exc:
            messagebox.showerror("解析失败", str(exc), parent=self)
            return
        if not rows:
            messagebox.showwarning(
                "未识别到数据",
                "没有找到含“货品名称、客户品名”表头的数据，请确认表格格式：\n货品名称 | 客户品名 | 采购规格 | 实际尺寸",
                parent=self,
            )
            return
        self.rows = rows
        for row in self.rows:
            row.setdefault("code", "")
            row.setdefault("unit", "")
        self.statuses = ["待读取货品对照"] * len(rows)
        self._refresh()
        messagebox.showinfo("已读取", f"共识别 {len(rows)} 行。", parent=self)

    def load_lookup(self):
        if not self.rows:
            messagebox.showinfo("提示", "请先选择 Excel。", parent=self)
            return
        try:
            lookup = fetch_hpdan_lookup(self.config)
        except Exception as exc:
            hint = _login_failed_hint(str(exc))
            messagebox.showerror("读取 hpdan 失败", f"{_exc_text(exc)}\n{hint}", parent=self)
            return
        found = 0
        for row in self.rows:
            hit = lookup.get(norm_text(row["goods_name"]))
            if hit:
                row["code"] = hit["code"]
                row["unit"] = hit["unit"]
                found += 1
            else:
                row["code"] = ""
                row["unit"] = ""
        self.statuses = ["可导入" if row["code"] else "货品名称未找到" for row in self.rows]
        self._refresh()
        messagebox.showinfo(
            "对照完成",
            f"共 {len(self.rows)} 行：{found} 行找到货品编号，{len(self.rows) - found} 行未找到。",
            parent=self,
        )

    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(self.rows):
            status = self.statuses[i] if i < len(self.statuses) else ""
            self.tree.insert(
                "",
                tk.END,
                iid=str(i),
                values=(
                    row["goods_name"],
                    row["cust_name"],
                    row["spec"],
                    row["actual"],
                    row.get("code", ""),
                    row.get("unit", ""),
                    status,
                ),
            )

    def do_import(self):
        if not self.rows:
            messagebox.showinfo("提示", "请先选择 Excel。", parent=self)
            return
        ready = [r for r in self.rows if r.get("code") and r.get("cust_name")]
        if not ready:
            messagebox.showwarning(
                "没有可导入的行",
                "请先点“读取货品对照(hpdan)”，并且客户品名不能为空。",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "确认导入",
            f"将向 HPKhType 表插入 {len(ready)} 行数据（货品名称未找到的行不会导入）。\n\n确定继续？",
            parent=self,
        ):
            return
        try:
            result = insert_customer_items(self.config, ready, self.lbtype_var.get().strip())
        except Exception as exc:
            perm_hint = _perm_hint(_exc_text(exc))
            messagebox.showerror(
                "写入失败",
                f"{_exc_text(exc)}\n{perm_hint}\n\n本次操作已回滚，未写入任何数据。",
                parent=self,
            )
            return

        # 按行回填状态
        by_key = {}
        for o in result.get("outcomes", []):
            by_key[(o.get("goods_name"), o.get("cust_name"), o.get("spec"), o.get("actual"))] = o.get("status", "")
        for i, row in enumerate(self.rows):
            st = by_key.get((row["goods_name"], row["cust_name"], row["spec"], row["actual"]))
            if st:
                self.statuses[i] = st
        self._refresh()

        msg = (
            f"导入完成：成功插入 {result['inserted']} 行，"
            f"重复跳过 {result['skipped_dup'] + result.get('skipped_pk', 0)} 行"
            f"（其中主键冲突 {result.get('skipped_pk', 0)} 行）。"
        )
        if result["failed"]:
            msg += f"\n失败 {len(result['failed'])} 行（在列表中标记为“失败”）。"
        messagebox.showinfo("导入结果", msg, parent=self)


class MemoryDialog(tk.Toplevel):
    def __init__(self, master: App, storage: Storage):
        super().__init__(master)
        self.storage = storage
        self.title("匹配记忆库")
        self.geometry("980x460")
        self.transient(master)

        ttk.Label(
            self,
            text="这里保存“客户产品名称 → hpbh”的对应关系。重复出现的写法会自动匹配；可删除错误条目。",
            wraplength=940,
        ).pack(anchor=tk.W, padx=10, pady=(10, 4))

        wrap = ttk.Frame(self)
        wrap.pack(fill=tk.BOTH, expand=True, padx=10)
        cols = (
            ("name", "客户产品名称", 360),
            ("code", "hpbh", 100),
            ("unit", "单位", 60),
            ("cnt", "次数", 55),
            ("last", "最近使用", 130),
        )
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings")
        for key, text, width in cols:
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=tk.W)
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)

        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, padx=10, pady=8)
        ttk.Button(btns, text="删除选中", command=self.delete_selected).pack(side=tk.LEFT)
        ttk.Button(btns, text="清空全部", command=self.clear_all).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side=tk.RIGHT)

        self._reload()

    def _reload(self):
        self.tree.delete(*self.tree.get_children())
        for m in self.storage.all_memory():
            self.tree.insert(
                "",
                tk.END,
                iid=str(m["id"]),
                values=(m["customer_name"], m["code"], m["unit"], m["match_count"], m["last_used"]),
            )

    def delete_selected(self):
        iid = self.tree.focus()
        if not iid:
            return
        self.storage.delete_memory(int(iid))
        self._reload()

    def clear_all(self):
        if messagebox.askyesno("清空", "确定清空全部匹配记忆？", parent=self):
            self.storage.clear_memory()
            self._reload()


def launch(db_path, config_path, startup_files=None):
    _init_dpi()
    root = TkinterDnD.Tk()
    panel = YxPartsPanel(root, db_path, config_path, startup_files, embedded=False)
    panel.pack(fill="both", expand=True)
    root.mainloop()


# 兼容旧引用（原版类名叫 App）
App = YxPartsPanel
