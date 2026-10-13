# -*- coding: utf-8 -*-
"""界面度量的一处出处 —— 控制台与燕秀页共用，别再各写一个数字。

★ 为什么单独一个文件：`ttk.Treeview` 的行高来自主题默认值，**不跟着字体放大**，
  所以每个用表格的界面都得自己调。原来控制台按 linespace 算（43），
  燕秀页写死 30（当年也是为"文字被裁"打的补丁，但 30 仍小于一行雅黑 10 的 35）
  ⇒ 谁后建谁生效，燕秀页一 `_setup_style` 就把行高压回 30，两张表又一起切字。
  Style 是全进程共享的，这种"两处各写一个数"必然漂。
"""

UI_FONT = "Microsoft YaHei UI"
UI_SIZE = 10
ROW_PAD = 8          # 一行字上下各留 4px 呼吸


def tree_rowheight(widget, family=UI_FONT, size=UI_SIZE, pad=ROW_PAD):
    """按**字体度量**算 Treeview 行高；拿不到字体就退回一个安全值。"""
    try:
        from tkinter import font as tkfont
        return int(tkfont.Font(family=family, size=size, root=widget).metrics("linespace")) + pad
    except Exception:  # noqa: BLE001
        return 26
