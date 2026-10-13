# -*- coding: utf-8 -*-
"""销售单打印：返回打印报文，或在 Agent 所在 Windows 机器上用默认打印机打印。"""

import os
import socket
import sys
import tempfile
import base64
import zlib
from decimal import Decimal

from . import sales
from . import sql as db


def _money(v):
    return "%.2f" % float(v or 0)


def render_bill_text(bill):
    lines = []
    sep = "-" * 40
    lines.append("销 售 出 库 单")
    lines.append(sep)
    lines.append("单号：" + str(bill.get("bill_no") or ""))
    lines.append("日期：" + str(bill.get("ndate") or "")[:19])
    cust = bill.get("customer") or {}
    lines.append("客户：" + str(cust.get("name") or "") + "（" + str(cust.get("code") or "") + "）")
    lines.append("仓库：" + str(bill.get("warehouse") or ""))
    lines.append(sep)
    total_qty = 0
    total_amt = 0.0
    for i, x in enumerate(bill.get("lines") or [], 1):
        qty = float(x.get("qty") or 0)
        amt = float(x.get("amount") or 0)
        total_qty += qty
        total_amt += amt
        lines.append("%d. %s" % (i, x.get("name") or x.get("hpbh") or ""))
        lines.append("   %s %s x %s = %s" % (
            x.get("hpbh") or "", _fmt(qty), _money(x.get("price")), _money(amt)))
    lines.append(sep)
    lines.append("合计数量：%s    合计金额：%s" % (_fmt(total_qty), _money(total_amt)))
    note = str(bill.get("bz_xs") or "")
    if note:
        lines.append("备注：" + note)
    lines.append(sep)
    return "\n".join(lines)


def _fmt(v):
    f = float(v or 0)
    return str(int(f)) if f == int(f) else ("%.2f" % f)


def _print_to(path, printer, timeout=30):
    """把文件投递到**指定打印机**（Windows：ShellExecuteExW + 动词 `printto`）。

    ★ 为什么不继续用 `os.startfile(path, "print")`：
      那是「默认打印动作」，**永远走系统默认打印机** —— 用户在打印页选了别的机器也没用。
      这正是「打印页的打印机列表只能看、不能选」的另一半根因。
    ★ 为什么用 ctypes 而不是 pywin32：Agent 是 PyInstaller 单文件打包，
      少一个三方依赖少一份打包风险；winspool 枚举打印机也是同样的 ctypes 路子。
    ★ lpParameters **必须是带引号的打印机名**（`"HP Laser MFP"`）——
      不带引号时名字里的空格会被当成参数分隔，打印机会被截断成 "HP"。
    """
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.DWORD),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HANDLE),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),  # union { hIcon, hMonitor }
            ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_FLAG_NO_UI = 0x00000400
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI
    info.lpVerb = "printto"
    info.lpFile = str(path)
    info.lpParameters = '"%s"' % printer
    info.nShow = 0
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        raise OSError("投递到打印机「%s」失败（GetLastError=%s）"
                      % (printer, ctypes.get_last_error()))
    if info.hProcess:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject(info.hProcess, int(timeout) * 1000)
        kernel32.CloseHandle(info.hProcess)


def print_bill(cur, cfg, bill_no, mode="system", copies=1, printer=None, fmt=None, dpi=None):
    """打印单据。

    ★ 2026-09-23（需求）：新增 `printer` / `fmt` 两个可选参数 ——
      - `printer`：**指定打印机名**。非空时用 `printto` 投递到该打印机；
        为空时保持老行为（走系统默认打印机）。名字来自 capabilities.printers.v3Printers。
      - `fmt`：选用的 **V3 报表方案名**（`print.format.list` 的 items[].name）。
        随结果返回并记录为「本次使用的打印格式」。真正按 ReportMachine(.rmf) 定义
        **重排版式**是独立课题（见 skill 里 .rmf 的推进记录），不在本次范围。
    """
    bill = sales.get_bill(cur, bill_no)
    payload = render_bill_text(bill)
    mode = str(mode or "system").lower()
    printer = str(printer or "").strip()
    # ★ 打印机名要过一道脏字符检查（2026-10-12）：它是这样送进 ShellExecuteExW 的 ——
    #   `lpParameters = '"%s"' % printer`，名字里带一个引号或换行就能把那对引号顶破、
    #   参数被拆成两截。正常路径这个名字来自 capabilities 列出的打印机，不会脏；
    #   但 op 的入参是**客户端拼的**，按外部输入处理。
    if any(ch in printer for ch in ('"', "'", "`", "\r", "\n")):
        raise ValueError("打印机名含非法字符，请从列表里选一台，不要手填")
    printer = printer[:128]
    fmt = str(fmt or "").strip()
    result = {"billNo": bill_no, "mode": mode, "payload": payload}
    if printer:
        result["printer"] = printer
    if fmt:
        result["format"] = fmt
    if mode == "layout":
        # ★ V3 1:1 版式 + **静默直投**（GDI）—— 远程打印的落点。
        return print_bill_layout(cur, cfg, bill_no, printer=printer, copies=copies, bill=bill)
    if mode == "image":
        # ★ 出 PNG 供移动端预览（与打印共用同一套绘制代码 ⇒ 所见即所打）。
        #   dpi 留空 ⇒ render_bill_image 按页数自适应（防小程序 setData 撑爆）。
        img = render_bill_image(cur, bill, dpi=dpi)
        result["totalPages"] = img.get("totalPages", 0)
        result["dpi"] = img.get("dpi")
        result["pages"] = [{"page": p["page"], "w": p["w"], "h": p["h"],
                            "png": base64.b64encode(p["png"]).decode("ascii")}
                           for p in (img.get("pages") or [])]
        result["bytes"] = sum(len(p["png"]) for p in (img.get("pages") or []))
        return result
    if mode == "html":
        result["html"] = render_bill_html(cur, bill)
        return result
    if mode == "payload":
        return result
    if not sys.platform.startswith("win"):
        result.update({"dispatched": False, "message": "Agent 非 Windows 环境，仅返回打印报文"})
        return result
    try:
        path = os.path.join(tempfile.gettempdir(), "v3print_%s.txt" % str(bill_no).replace("/", "_"))
        with open(path, "w", encoding="utf-8-sig", newline="\r\n") as fh:
            fh.write(payload)
        # ★ 份数封顶（2026-10-12）：原来是 `range(max(int(copies or 1), 1))` ——
        #   只有下限没有上限，`copies=99999` 就是在这台客户机上连投 99999 次
        #   ShellExecute（每投一次等一次打印机句柄），既占死 print 那道闸，
        #   也是真的会往出纸口堆几十摞纸。上限给 20 份：单据场景一次最多也就一沓。
        n_copies = max(1, min(int(copies or 1), 20))
        for _ in range(n_copies):
            if printer:
                _print_to(path, printer)
            else:
                os.startfile(path, "print")  # noqa: S606  Windows 默认打印动作
        msg = ("已提交到打印机「%s」" % printer) if printer \
            else "已提交默认打印机，请确认打印机与纸张"
        result.update({"dispatched": True, "file": path, "message": msg})
    except Exception as exc:  # noqa: BLE001
        result.update({"dispatched": False, "message": "打印失败：" + str(exc)})
    return result


def print_format(cur, bill_name="销售出货单"):
    """返回 V3 账套里的打印格式配置（Report_BillList / Report_Mapping / Report_FormatSet）。"""
    names = [r[0] for r in db.qall(
        cur, "SELECT BillName FROM Report_BillList GROUP BY BillName ORDER BY MIN(ID)")]
    bill_name = str(bill_name or "销售出货单").strip()
    head = db.q1(
        cur,
        """SELECT BillName, PaperSize, IsPaging, Goods_Type, SelectSql_M, SelectSql_D, SelectSql_S,
                  GoodsRow, M_Height, D_Height
           FROM Report_BillList WHERE BillName=%s""",
        (bill_name,),
    )
    if not head:
        return {"billNames": names, "billName": bill_name, "found": False}
    mappings = db.qall(
        cur,
        """SELECT ID, IsType, DB_ColumnName, Show_ColumnName FROM Report_Mapping
           WHERE BillName=%s ORDER BY ID""",
        (bill_name,),
    )
    layout = db.qall(
        cur,
        """SELECT ID, Ctrl_X, Ctrl_Y, Ctrl_W, Ctrl_Text1, Ctrl_Text2, Ctrl_Size, Ctrl_Type, BarCodeType
           FROM Report_FormatSet WHERE BillName=%s ORDER BY ID""",
        (bill_name,),
    )
    return {
        "billNames": names,
        "billName": bill_name,
        "found": True,
        "paperSize": float(head[1] or 0),
        "isPaging": int(head[2] or 0),
        "goodsType": int(head[3] or 0),
        "selectSqlHeader": head[4],
        "selectSqlDetail": head[5],
        "selectSqlSummary": head[6],
        "goodsRow": int(head[7] or 0),
        "headerHeight": int(head[8] or 0),
        "detailHeight": int(head[9] or 0),
        "mappings": [
            {"id": r[0], "scope": "header" if r[1] == 1 else "detail",
             "column": r[2], "label": r[3]}
            for r in mappings
        ],
        "layout": [
            {"id": r[0], "x": r[1], "y": r[2], "w": r[3], "text": r[4], "text2": r[5],
             "size": r[6], "area": r[7], "barcode": r[8]}
            for r in layout
        ],
    }


FORMAT_DBS = ("AccMaster", "TempUpSpRpt")


def list_named_formats(cur, rpt_type="BillXS", db_name="AccMaster"):
    """列出 V3 报表方案（默认销售出货单 BillXS）。"""
    db_name = db_name if db_name in FORMAT_DBS else "AccMaster"
    rpt_type = str(rpt_type or "BillXS")
    rows = db.qall(
        cur,
        f"""SELECT RptName, Rptdefa, sysrpt, RptCustom, RptVerName, IDENCOL,
                   DATALENGTH(RptMemo2), RptBuildTime, RptBuildName, RptEditTime, RptEditName
            FROM [{db_name}].dbo.HxSysReport WHERE RptType=%s ORDER BY IDENCOL""",
        (rpt_type,),
    )
    return {
        "source": db_name,
        "rptType": rpt_type,
        "count": len(rows),
        "items": [
            {"name": r[0], "isDefault": bool(r[1]), "sysRpt": bool(r[2]),
             "custom": bool(r[3]), "verName": r[4], "idencol": r[5],
             "definitionBytes": int(r[6] or 0),
             "buildTime": str(r[7] or ""), "buildName": r[8],
             "editTime": str(r[9] or ""), "editName": r[10]}
            for r in rows
        ],
    }


def get_named_format(cur, rpt_name, rpt_type="BillXS", db_name="AccMaster",
                     include_definition=False, include_text=False):
    """取某个 V3 打印方案的元数据/定义（ReportMachine 3.0，zlib 压缩）。"""
    db_name = db_name if db_name in FORMAT_DBS else "AccMaster"
    r = db.q1(
        cur,
        f"""SELECT RptName, Rptdefa, sysrpt, RptCustom, RptVerName, IDENCOL,
                   RptMemo, RptMemo2, RptBuildTime, RptBuildName, RptEditTime, RptEditName
            FROM [{db_name}].dbo.HxSysReport WHERE RptType=%s AND RptName=%s""",
        (str(rpt_type or "BillXS"), str(rpt_name or "")),
    )
    if not r:
        return {"found": False, "source": db_name, "rptType": rpt_type, "name": rpt_name}
    blob = bytes(r[7] or r[6] or b"")
    data = b""
    engine = ""
    if blob:
        try:
            data = zlib.decompress(blob[4:]) if blob[4:6] == b"\x78\x9c" else blob
            engine = "ReportMachine 3.0" if "ReportMachine".encode("utf-16le") in data[:120] else "unknown"
        except Exception:  # noqa: BLE001
            data = blob
    result = {
        "found": True, "source": db_name, "rptType": rpt_type, "name": r[0],
        "isDefault": bool(r[1]), "sysRpt": bool(r[2]), "custom": bool(r[3]),
        "verName": r[4], "idencol": r[5], "definitionBytes": len(blob),
        "inflatedBytes": len(data), "engine": engine,
        "buildTime": str(r[8] or ""), "buildName": r[9],
        "editTime": str(r[10] or ""), "editName": r[11],
    }
    if include_definition and data:
        result["definitionBase64"] = base64.b64encode(data).decode("ascii")
    if include_text and data:
        text = []
        cur_s = []
        for i in range(0, len(data) - 1, 2):
            ch = chr(data[i] | (data[i + 1] << 8))
            if ch.isprintable():
                cur_s.append(ch)
            else:
                if len(cur_s) >= 3:
                    text.append("".join(cur_s))
                cur_s = []
        result["texts"] = text[:200]
    return result


def _default_printer():
    if not sys.platform.startswith("win"):
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        winspool = ctypes.WinDLL("winspool.drv")
        size = wintypes.DWORD(0)
        winspool.GetDefaultPrinterW(None, ctypes.byref(size))
        if not size.value:
            return ""
        buf = ctypes.create_unicode_buffer(size.value)
        if winspool.GetDefaultPrinterW(buf, ctypes.byref(size)):
            return buf.value
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _enum_windows_printers():
    """枚举 Windows 系统实际安装的打印机（本地 + 已连接的网络共享打印机）。

    ★ PRINTER_ENUM_LOCAL(0x2) 覆盖本地安装（含 TCP/IP、WSD 网络打印机）；
      PRINTER_ENUM_CONNECTIONS(0x4) 覆盖 \\\\server\\share 映射来的共享打印机。
      两者合起来 = Windows「设备和打印机」面板看到的全部打印机。
    ★ 用 level 4（PRINTER_INFO_4W）轻量枚举，只要名字，不取驱动/端口。
    """
    if not sys.platform.startswith("win"):
        return []
    try:
        import ctypes
        from ctypes import wintypes

        winspool = ctypes.WinDLL("winspool.drv")
        PRINTER_ENUM_LOCAL = 0x2
        PRINTER_ENUM_CONNECTIONS = 0x4

        class PRINTER_INFO_4W(ctypes.Structure):
            _fields_ = [
                ("pPrinterName", wintypes.LPWSTR),
                ("pServerName", wintypes.LPWSTR),
                ("Attributes", wintypes.DWORD),
            ]

        needed = wintypes.DWORD(0)
        returned = wintypes.DWORD(0)
        winspool.EnumPrintersW(PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS, None,
                               4, None, 0, ctypes.byref(needed), ctypes.byref(returned))
        if not needed.value:
            return []
        buf = ctypes.create_string_buffer(needed.value)
        ok = winspool.EnumPrintersW(PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS, None,
                                    4, buf, needed, ctypes.byref(needed), ctypes.byref(returned))
        if not ok:
            return []
        arr = (PRINTER_INFO_4W * returned.value).from_buffer(buf)
        return [p.pPrinterName for p in arr if p.pPrinterName]
    except Exception:  # noqa: BLE001
        return []


def list_printers(cur, cfg=None):
    """列出 Agent 所在电脑的真实打印机（枚举 Windows，不再查 V3 的 HxPrinter 配置表）。

    ★ 2026-09-23 改：原来查 HxPrinter 表（V3 C/S 时代按主机名配的打印机），
      在 B/S + Agent 架构下对不上号 —— 只显示旧机器 WISENB 配的两台虚拟打印机，
      而本机真实装的 HP 激光等根本没被读。改为 winspool.EnumPrinters 枚举本机
      真实打印机（本地 + 网络共享），才是「Agent 在哪台电脑、列哪台电脑的打印机」。
    ★ 返回字段保持 `v3Printers` + `printerName` 不变，前端 toCapabilities 无需改动。
    """
    host = socket.gethostname()
    default = _default_printer()
    names = _enum_windows_printers()
    note = ""
    if not names:
        note = "本机未枚举到 Windows 打印机"
    return {
        "hostName": host,
        "matchedHost": host,
        "note": note,
        "defaultPrinter": default,
        "v3Printers": [
            {"hostName": host, "printerName": n, "copies": 0, "knife": False}
            for n in names
        ],
    }


# ★★★ 2026-09-23 纠正：Report_FormatSet 的 Ctrl_X / Ctrl_Y / Ctrl_W **单位是 96DPI 像素**，
#   不是 0.1mm（旧注释与旧实现都写的 0.1mm，导致渲染全部挤在左上角、标题压住单号）。
#   实测依据（账套 tongda「销售出货单」）：
#     · 整页最右 = max(X+W) = 765px → 765 × 25.4/96 = **202.4mm**，正好落在 A4 宽 210mm 内；
#     · 「往来单位:」W=760px → 201.1mm，刚好占满 A4 版心；
#     · 标题 Y=10px→2.6mm 与 单号 Y=50px→13.2mm 间距 10.6mm，19pt 标题不再与单号重叠；
#     · 各表头行 Y 依次 50/90/130/210px → 13.2/23.8/34.4/55.6mm，行距规整。
#   ⚠ 与 Report_BillList.PaperSize=26.20 对不上（按旧口径 ×10=262mm 明显偏宽）；
#     纸张尺寸改用 A4 210mm，与本账套实际版心一致。
# ============ V3 销售出货单版式引擎（2026-09-23 换代）============
# 依据：V3 自己导出的三份矢量 PDF 定量反推并经 hold-out 验证
#   · 标定样本：销售出货单(折后价) 1 行明细
#   · hold-out：6 行明细、43 行跨 2 页（分页点 32+11）
#   · 实测偏差：竖线 0.00pt（精确）、横线 ≤0.51pt、文字行 ≤0.56px、明细单元格 x ≤0.32pt
#   · 文本 33/62/199 个全部在 V3 PDF 中原样出现（未命中 0）
# 坐标单位：**96DPI 像素**（1/96 英寸 ≈ 0.2646mm），**页顶起算**。
# ⚠ 与 Report_FormatSet 的 0.1mm 口径不同，两者不可混用。
_V3_FONT = 11.04          # 正文字号(pt)
_V3_TITLE_FONT = 15.96    # 标题字号(pt)
_V3_ROW = 24.0            # 行距(px) = 18pt = 6.35mm
_V3_TEXT_DY = 17.0        # 行顶 → 文字基线(px)
_V3_TITLE_Y = 80.32       # 标题文字 y(px)
_V3_DETAIL_TOP0 = 185.0   # 明细第 1 行行顶(px)
_V3_ROWS_PER_PAGE = 32    # 每页明细行数（超出翻页，表头表尾整块重印）
_V3_PAGE_H_PX = 1122.5    # A4 高 297mm = 1122.5px@96
_V3_PX2PT = 0.75
_V3_PX2MM = 25.4 / 96.0
_V3_ASCII_EM = 0.5435     # 11.04pt 宋体下 ASCII 字宽 6.0/11.04（实测）
_V3_ROW_TOP = {"单号": 89.0, "客户": 113.0, "地址": 137.0, "表头": 161.0}
_V3_COL_X_PT = [28.32, 50.04, 113.04, 300.48, 326.76, 391.92, 451.2, 508.2, 565.2]
_V3_TITLE_X_PT = 234.60
_V3_HEAD_X_PT = {"开单日期:": 60.72, "开单日期值": 114.48, "单号行": 402.72,
                 "客户:": 82.92, "客户值": 114.84, "电话:": 361.80,
                 "地址:": 82.92, "传真:": 361.80}
_V3_COL_HDR = [("序", 37.08), ("货品编号", 59.04), ("货品名称", 184.32), ("单位", 302.40),
               ("数量", 348.12), ("单价", 410.40), ("金额", 468.48), ("备注", 525.48)]
# 右对齐列的**右边界**(pt)（实测：右边缘恒定）。数量无 1/3 字样本，由 2 字样本反推。
_V3_RIGHT_X = {"序": 48.6, "数量": 390.04, "单价": 449.5, "金额": 506.5}
_V3_LEFT_X = {"货品编号": 51.84, "货品名称": 114.84, "单位": 308.04}
_DIGITS = "零壹贰叁肆伍陆柒捌玖"
_UNIT4 = ["", "拾", "佰", "仟"]
_BIG = ["", "万", "亿", "万亿"]


def _tw(text, size=_V3_FONT):
    """估算文本宽度(pt)：ASCII 0.5435em、CJK 1em（用于右对齐定位）。"""
    return sum(size * (_V3_ASCII_EM if ord(ch) < 0x2E80 else 1.0) for ch in str(text))


def _grp(ds):
    out, zero, n = "", False, len(ds)
    for i, ch in enumerate(ds):
        v = int(ch)
        pos = n - 1 - i
        if v == 0:
            zero = True
        else:
            if zero and out:
                out += "零"
            out += _DIGITS[v] + _UNIT4[pos]
            zero = False
    return out


def _yuan(y):
    if y == 0:
        return ""
    s, groups = str(y), []
    while s:
        groups.append(s[-4:])
        s = s[:-4]
    parts = []
    for i in range(len(groups) - 1, -1, -1):
        if int(groups[i]) == 0:
            if parts and any(int(x) for x in groups[:i]):
                parts.append("零")
            continue
        parts.append(_grp(groups[i]) + _BIG[i])
    return "".join(parts).rstrip("零")


def _rmb(v):
    """金额大写（与 V3 一致：无角分→「X元整」；有角无分→「X元Y角整」）。"""
    try:
        q = Decimal(str(v)).quantize(Decimal("0.01"))
    except Exception:  # noqa: BLE001
        return ""
    y = int(q)
    jiao = int((q * 10) % 10)
    fen = int((q * 100) % 10)
    s = (_yuan(y) + "元") if y else ""
    if jiao == 0 and fen == 0:
        return (s or "零元") + "整"
    if jiao == 0:
        return s + "零" + _DIGITS[fen] + "分"
    if fen == 0:
        return s + _DIGITS[jiao] + "角整"
    return s + _DIGITS[jiao] + "角" + _DIGITS[fen] + "分"


def _corp_name(cur):
    """抬头 [HxCorpName] = AccMaster.AccountInfo.ACNAME（按当前库的 DATANAME 取）。"""
    try:
        row = db.q1(cur, "SELECT TOP 1 ACNAME FROM [AccMaster].dbo.AccountInfo "
                         "WHERE DATANAME = DB_NAME()")
        if row and row[0]:
            return str(row[0]).strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _zdr_name(cur, code):
    """制单人：库里存代码（如 SYSTEM），V3 印的是 UserInfo.UserName（系统管理员）。"""
    code = str(code or "").strip()
    if not code:
        return ""
    try:
        row = db.q1(cur, "SELECT TOP 1 UserName FROM UserInfo WHERE UserID=%s", (code,))
        if row and row[0]:
            return str(row[0]).strip()
    except Exception:  # noqa: BLE001
        pass
    return code


def _lg(line, *keys):
    for k in keys:
        if line.get(k) not in (None, ""):
            return line[k]
    return ""


def _qty_text(v):
    try:
        f = float(v or 0)
    except Exception:  # noqa: BLE001
        return str(v or "")
    return str(int(f)) if abs(f - round(f)) < 1e-9 else ("%g" % f)


def build_bill_layout(cur, bill, corp=None, rows_per_page=_V3_ROWS_PER_PAGE):
    """按 V3 真实版式生成**分页**版式模型（坐标 px@96，页顶起算）。"""
    lines = bill.get("lines") or []
    n = len(lines)
    cust = (bill.get("customer") or {})
    cust_name = str(cust.get("name") or cust.get("itemmc") or "")
    ndate = str(bill.get("ndate") or "")
    if corp is None:
        corp = _corp_name(cur)
    zdr = _zdr_name(cur, bill.get("zdr") or bill.get("user_insert") or "")
    total_pages = max(1, (n + rows_per_page - 1) // rows_per_page)
    tq = sum(float(_lg(L, "qty", "quantity") or 0) for L in lines)
    ta = sum(float(_lg(L, "amount") or 0) for L in lines)

    pages = []
    for p in range(1, total_pages + 1):
        lo = (p - 1) * rows_per_page
        page_rows = lines[lo:min(lo + rows_per_page, n)]
        m = len(page_rows)
        el = []

        def add(role, text, x_pt, y_px, size=_V3_FONT):
            # ★ x 常量一律以 pt 书写（便于与 V3 PDF 直接对照），此处统一换算成 px，
            #   与页面 API 的 px 口径一致 —— 漏了这一步会让所有 x 缩小 1.33 倍。
            el.append((role, str(text), float(x_pt) / _V3_PX2PT, float(y_px), float(size)))

        add("标题", corp + "销售出货单", _V3_TITLE_X_PT, _V3_TITLE_Y, _V3_TITLE_FONT)
        y_ord = _V3_ROW_TOP["单号"] + _V3_TEXT_DY
        add("单号行", "单号:%s 第%d/共%d页" % (bill.get("bill_no") or "", p, total_pages),
            _V3_HEAD_X_PT["单号行"], y_ord)
        add("开单日期", "开单日期:", _V3_HEAD_X_PT["开单日期:"], y_ord)
        add("开单日期值", ndate, _V3_HEAD_X_PT["开单日期值"], y_ord)
        y_c = _V3_ROW_TOP["客户"] + _V3_TEXT_DY
        add("客户", "客户:", _V3_HEAD_X_PT["客户:"], y_c)
        add("客户值", cust_name, _V3_HEAD_X_PT["客户值"], y_c)
        add("电话", "电话:", _V3_HEAD_X_PT["电话:"], y_c)
        y_a = _V3_ROW_TOP["地址"] + _V3_TEXT_DY
        add("地址", "地址:", _V3_HEAD_X_PT["地址:"], y_a)
        add("传真", "传真:", _V3_HEAD_X_PT["传真:"], y_a)
        y_h = _V3_ROW_TOP["表头"] + _V3_TEXT_DY
        for t, x in _V3_COL_HDR:
            add("表头", t, x, y_h)

        for i, L in enumerate(page_rows, 1):
            y = _V3_DETAIL_TOP0 + _V3_ROW * (i - 1) + _V3_TEXT_DY
            unit = str(_lg(L, "unit", "hpunit", "hp_unit") or "")
            seq_t = str(lo + i)
            code_t = str(_lg(L, "hpbh", "product_id") or "").strip()
            name_t = str(_lg(L, "name", "hpmc", "product_name") or "")
            qty_t = _qty_text(_lg(L, "qty", "quantity")) + unit
            price_t = "%g" % float(_lg(L, "price", "d_dj") or 0)
            amt_t = _money(_lg(L, "amount") or 0)
            add("明细", seq_t, _V3_RIGHT_X["序"] - _tw(seq_t), y)
            add("明细", code_t, _V3_LEFT_X["货品编号"], y)
            add("明细", name_t, _V3_LEFT_X["货品名称"], y)
            add("明细", unit, _V3_LEFT_X["单位"], y)
            add("明细", qty_t, _V3_RIGHT_X["数量"] - _tw(qty_t), y)
            add("明细", price_t, _V3_RIGHT_X["单价"] - _tw(price_t), y)
            add("明细", amt_t, _V3_RIGHT_X["金额"] - _tw(amt_t), y)

        sq = sum(float(_lg(L, "qty", "quantity") or 0) for L in page_rows)
        sa = sum(float(_lg(L, "amount") or 0) for L in page_rows)
        base = _V3_DETAIL_TOP0 + _V3_ROW * m
        y_f = base + _V3_TEXT_DY
        add("页计", "页计:", 82.92, y_f)
        add("页计", _rmb(sa), 114.84, y_f)
        add("页计", _money(sq), 360.2, y_f)
        add("页计", _money(sa), 470.5, y_f)
        y_g = base + _V3_ROW + _V3_TEXT_DY
        add("本单合计", "本单合计:", 60.36, y_g)
        add("本单合计", _rmb(ta), 114.84, y_g)
        add("本单合计", _money(tq), 356.5, y_g)
        add("本单合计", _money(ta), 466.1, y_g)
        y_b = base + _V3_ROW * 2 + _V3_TEXT_DY
        add("备注", "备注:", 82.92, y_b)
        add("备注值", str(bill.get("bz_xs") or ""), 114.84, y_b)
        y_s = base + _V3_ROW * 3 + _V3_TEXT_DY
        add("制单人", "制单人:", 72.0, y_s)
        add("制单人", zdr, 114.48, y_s)
        add("制单人", "经手人:", 268.44, y_s)
        add("制单人", "收货人:", 410.16, y_s)

        # 边框只画「客户行 → 备注行」；签字区无框
        tops = [_V3_ROW_TOP["客户"], _V3_ROW_TOP["地址"], _V3_ROW_TOP["表头"]]
        tops += [_V3_DETAIL_TOP0 + _V3_ROW * i for i in range(m)]
        tops += [_V3_DETAIL_TOP0 + _V3_ROW * (m + k) for k in (0, 1, 2)]
        h_lines = tops + [_V3_DETAIL_TOP0 + _V3_ROW * (m + 3)]
        pages.append({"page": p, "rows": m, "elements": el, "h_lines_px": h_lines,
                      "seq_from": lo + 1, "seq_to": lo + m})
    return {"bill_no": bill.get("bill_no"), "rows": n, "pages": pages,
            "total_pages": total_pages, "corp": corp,
            "totals": {"qty": tq, "amount": ta}, "zdr": zdr}


def render_bill_html(cur, bill, corp=None):
    """按 V3 真实版式渲染可打印 HTML（多页；A4；坐标 96DPI 像素 → mm）。"""
    lay = build_bill_layout(cur, bill, corp=corp)
    out = ["<!DOCTYPE html><html><head><meta charset='utf-8'><title>%s</title>"
           % (bill.get("bill_no") or "销售出货单"),
           "<style>",
           "@page{size:A4;margin:0}",
           "body{margin:0;background:#fff;font-family:'SimSun','宋体',serif}",
           ".pg{position:relative;width:210mm;height:297mm;overflow:hidden;"
           "page-break-after:always}",
           ".t{position:absolute;line-height:1;white-space:nowrap;color:#000}",
           ".b{position:absolute;background:#000}",
           "</style></head><body>"]
    vmin, vmax = min(_V3_COL_X_PT), max(_V3_COL_X_PT)
    for pg in lay["pages"]:
        out.append("<div class='pg'>")
        hl = [h * _V3_PX2PT for h in pg["h_lines_px"]]
        ytop, ybot = min(hl), max(hl)
        for x in _V3_COL_X_PT:
            out.append("<div class='b' style='left:%.3fmm;top:%.3fmm;width:0.2mm;height:%.3fmm'>"
                       "</div>" % (x * 25.4 / 72, ytop * 25.4 / 72, (ybot - ytop) * 25.4 / 72))
        for y in hl:
            out.append("<div class='b' style='left:%.3fmm;top:%.3fmm;width:%.3fmm;height:0.2mm'>"
                       "</div>" % (vmin * 25.4 / 72, y * 25.4 / 72, (vmax - vmin) * 25.4 / 72))
        for role, text, x_px, y_px, size in pg["elements"]:
            if not str(text).strip():
                continue
            bottom = (_V3_PAGE_H_PX - y_px - 0.2 * (size / _V3_PX2PT)) * _V3_PX2MM
            out.append("<div class='t' style='left:%.3fmm;bottom:%.3fmm;font-size:%.2fpt'>%s"
                       "</div>" % (x_px * _V3_PX2MM, bottom, size,
                                   str(text).replace("&", "&amp;").replace("<", "&lt;")))
        out.append("</div>")
    out.append("</body></html>")
    return "".join(out)


# ============================================================================
# V3 版式 · GDI 直绘（静默打印 + 出图）
#
# ★ 为什么走 GDI，而不是「headless 浏览器转 PDF」：
#   1) 客户机不一定装 Chrome/Edge（Win7 只带 IE）—— 浏览器方案在客户现场会失灵；
#   2) 浏览器出 PDF 后还得二次投递到打印机，多一层失败点；
#   3) GDI 是**系统 API**，零外部依赖，且 `CreateDCW("WINSPOOL", 打印机名)` 能
#      直接指定打印机、**全程无对话框** —— 这恰恰是 V3 客户端做不到的点
#      （V3 打印必弹 TPrintDialog，所以要人工点，无法远程）。
#
# 实测（把同一版式打到 Microsoft Print to PDF，再解 PDF 与 V3 原版逐元素对照）：
#   · 坐标误差 ≤ 0.5 逻辑单位 = 0.5px = 0.13mm（GDI 坐标按整数取整所致）
#   · 字号误差 ≤ 0.21pt = 0.07mm（lfHeight 只能给整数）
#   · 线宽用 `PS_SOLID, 0` ⇒ 恒为 1 设备像素，不受逻辑缩放影响
# 均在打印不可见范围内。
# ============================================================================

_GDI_API = None          # 懒加载的 ctypes 句柄集合
_GDI_CACHE = {"fonts": {}}


def _gdi():
    """懒加载 gdi32/user32 并**显式声明 argtypes**。

    ★ 64 位下 HDC / HFONT / HPEN 都是**指针**：不声明 argtypes 时 ctypes 默认按
      int 传参，句柄高位被截断 ⇒ 时好时坏的随机失败（最难查的那类）。
    """
    global _GDI_API
    if _GDI_API is not None:
        return _GDI_API
    import ctypes
    from ctypes import wintypes

    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    HDC = ctypes.c_void_p
    HGDIOBJ = ctypes.c_void_p

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class DOCINFOW(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_int), ("lpszDocName", wintypes.LPCWSTR),
                    ("lpszOutput", wintypes.LPCWSTR), ("lpszDatatype", wintypes.LPCWSTR),
                    ("fwType", wintypes.DWORD)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    gdi32.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
    gdi32.CreateDCW.restype = HDC
    gdi32.DeleteDC.argtypes = [HDC]
    gdi32.CreateCompatibleDC.argtypes = [HDC]
    gdi32.CreateCompatibleDC.restype = HDC
    gdi32.CreateDIBSection.argtypes = [HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), HDC, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = HGDIOBJ
    gdi32.SetMapMode.argtypes = [HDC, ctypes.c_int]
    gdi32.SetWindowExtEx.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.POINTER(SIZE)]
    gdi32.SetViewportExtEx.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.POINTER(SIZE)]
    gdi32.GetDeviceCaps.argtypes = [HDC, ctypes.c_int]
    gdi32.GetDeviceCaps.restype = ctypes.c_int
    gdi32.StartDocW.argtypes = [HDC, ctypes.POINTER(DOCINFOW)]
    gdi32.StartPage.argtypes = [HDC]
    gdi32.EndPage.argtypes = [HDC]
    gdi32.EndDoc.argtypes = [HDC]
    gdi32.AbortDoc.argtypes = [HDC]
    gdi32.CreateFontW.argtypes = ([ctypes.c_int] * 4 + [ctypes.c_int]
                                  + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR])
    gdi32.CreateFontW.restype = HGDIOBJ
    gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.CreatePen.restype = HGDIOBJ
    gdi32.SelectObject.argtypes = [HDC, HGDIOBJ]
    gdi32.SelectObject.restype = HGDIOBJ
    gdi32.DeleteObject.argtypes = [HGDIOBJ]
    gdi32.SetTextAlign.argtypes = [HDC, ctypes.c_uint]
    gdi32.SetTextColor.argtypes = [HDC, wintypes.DWORD]
    gdi32.SetBkMode.argtypes = [HDC, ctypes.c_int]
    gdi32.TextOutW.argtypes = [HDC, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int]
    gdi32.MoveToEx.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.POINTER(POINT)]
    gdi32.LineTo.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
    gdi32.GetTextExtentPoint32W.argtypes = [HDC, wintypes.LPCWSTR, ctypes.c_int,
                                            ctypes.POINTER(SIZE)]
    gdi32.PatBlt.argtypes = [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.DWORD]
    gdi32.PatBlt.restype = wintypes.BOOL

    _GDI_API = {
        "gdi32": gdi32, "SIZE": SIZE, "DOCINFOW": DOCINFOW, "BITMAPINFO": BITMAPINFO,
        "BITMAPINFOHEADER": BITMAPINFOHEADER, "HDC": HDC, "HGDIOBJ": HGDIOBJ,
        # 常量
        "MM_ANISOTROPIC": 8, "LOGPIXELSX": 88, "LOGPIXELSY": 90,
        "TA_BASELINE": 24, "TRANSPARENT": 1, "PS_SOLID": 0, "GB2312_CHARSET": 134,
        "FW_NORMAL": 400, "FW_BOLD": 700, "DIB_RGB_COLORS": 0, "BI_RGB": 0,
    }
    return _GDI_API


def _gdi_font(hdc, size_pt, bold=False):
    """取（并缓存）指定字号的宋体 HFONT。字号 pt → 96dpi 逻辑单位。"""
    api = _gdi()
    gdi32 = api["gdi32"]
    lh = -int(round(float(size_pt) / 72.0 * 96.0))
    key = (lh, bool(bold))
    f = _GDI_CACHE["fonts"].get(key)
    if f:
        return f
    f = gdi32.CreateFontW(lh, 0, 0, 0, api["FW_BOLD"] if bold else api["FW_NORMAL"],
                          0, 0, 0, api["GB2312_CHARSET"], 0, 0, 0, 0, "宋体")
    _GDI_CACHE["fonts"][key] = f
    return f


def _gdi_prepare(hdc, dpi_x, dpi_y):
    """把 DC 的坐标系统一成「96dpi 像素、原点在左上」——与 layout 模型同口径。

    用 MM_ANISOTROPIC + WindowExt/ViewportExt 让 GDI 自己做缩放，
    这样调用方给的坐标**永远是 layout 里那套 px@96**，不必到处乘系数。
    """
    api = _gdi()
    api["gdi32"].SetMapMode(hdc, api["MM_ANISOTROPIC"])
    api["gdi32"].SetWindowExtEx(hdc, 96, 96, None)
    api["gdi32"].SetViewportExtEx(hdc, int(dpi_x), int(dpi_y), None)


def _gdi_draw_page(hdc, pg):
    """把一页版式画到任意 DC（打印机 DC / 内存 DIB DC 通用）。"""
    api = _gdi()
    gdi32 = api["gdi32"]
    hl = pg["h_lines_px"]
    cols_px = [x / _V3_PX2PT for x in _V3_COL_X_PT]      # pt → px@96
    x0, x1 = min(cols_px), max(cols_px)
    y0, y1 = min(hl), max(hl)

    pen = gdi32.CreatePen(api["PS_SOLID"], 0, 0)          # 宽度 0 = 1 设备像素
    old_pen = gdi32.SelectObject(hdc, pen)
    for cx in cols_px:
        gdi32.MoveToEx(hdc, int(round(cx)), int(round(y0)), None)
        gdi32.LineTo(hdc, int(round(cx)), int(round(y1)))
    for cy in hl:
        gdi32.MoveToEx(hdc, int(round(x0)), int(round(cy)), None)
        gdi32.LineTo(hdc, int(round(x1)), int(round(cy)))
    gdi32.SelectObject(hdc, old_pen)
    gdi32.DeleteObject(pen)

    gdi32.SetBkMode(hdc, api["TRANSPARENT"])
    gdi32.SetTextColor(hdc, 0)
    # ★ TA_BASELINE：layout 的 y 就是**文字基线**（与 V3 PDF 实测一致）。
    #   用 TA_TOP 会让整页文字下移一个 ascent（约 1.5mm），行距看着对、整体偏。
    gdi32.SetTextAlign(hdc, api["TA_BASELINE"])
    old_font = None
    cur_key = None
    for role, text, x_px, y_px, size in pg["elements"]:
        s = str(text)
        if not s.strip():
            continue
        lh = -int(round(float(size) / 72.0 * 96.0))
        if lh != cur_key:
            f = _gdi_font(hdc, size)
            if old_font is None:
                old_font = gdi32.SelectObject(hdc, f)
            else:
                gdi32.SelectObject(hdc, f)
            cur_key = lh
        gdi32.TextOutW(hdc, int(round(x_px)), int(round(y_px)), s, len(s))
    if old_font is not None:
        gdi32.SelectObject(hdc, old_font)


def print_bill_layout(cur, cfg=None, bill_no="", printer=None, copies=1, bill=None,
                      _out_file=None):
    """按 V3 版式**静默**打印到指定打印机（GDI 直投，无任何对话框）。

    printer 为空 ⇒ 走系统默认打印机。这是「远程打印」的落点：
    手机下发指令 → Agent 直接进打印队列出纸，店员那台电脑不受打扰。

    `_out_file` **仅供验收脚本**把作业落到文件（配 `Microsoft Print to PDF`）以便逐元素
    比对坐标；生产调用一律不传 ⇒ 正常走打印机端口。不设它的话，用 PDF 虚拟打印机测试
    会弹出「另存为」对话框，自动化会卡死。
    """
    import ctypes
    bill_no = str(bill_no or "").strip()
    if bill is None:
        bill = sales.get_bill(cur, bill_no)
    lay = build_bill_layout(cur, bill)
    result = {"billNo": bill.get("bill_no") or bill_no, "mode": "layout",
              "pages": lay["total_pages"], "rows": lay["rows"]}
    if not sys.platform.startswith("win"):
        result.update({"dispatched": False, "message": "Agent 非 Windows 环境，无法直投打印机"})
        return result
    api = _gdi()
    gdi32 = api["gdi32"]
    printer = str(printer or "").strip() or _default_printer()
    result["printer"] = printer
    copies = max(int(copies or 1), 1)
    hdc = gdi32.CreateDCW("WINSPOOL", printer, None, None)
    if not hdc:
        result.update({"dispatched": False,
                       "message": "打开打印机「%s」失败（GetLastError=%s）"
                                  % (printer, ctypes.get_last_error())})
        return result
    try:
        _gdi_prepare(hdc, gdi32.GetDeviceCaps(hdc, api["LOGPIXELSX"]),
                     gdi32.GetDeviceCaps(hdc, api["LOGPIXELSY"]))
        for _ in range(copies):
            di = api["DOCINFOW"]()
            di.cbSize = ctypes.sizeof(api["DOCINFOW"])
            di.lpszDocName = "销售出货单 %s" % (bill.get("bill_no") or bill_no)
            di.lpszOutput = str(_out_file) if _out_file else None
            if gdi32.StartDocW(hdc, ctypes.byref(di)) <= 0:
                raise OSError("StartDoc 失败")
            try:
                for pg in lay["pages"]:
                    if gdi32.StartPage(hdc) <= 0:
                        raise OSError("StartPage 失败")
                    _gdi_draw_page(hdc, pg)
                    gdi32.EndPage(hdc)
                gdi32.EndDoc(hdc)
            except Exception:
                gdi32.AbortDoc(hdc)
                raise
        result.update({"dispatched": True,
                       "message": "已按 V3 版式提交到打印机「%s」" % printer})
    except Exception as exc:  # noqa: BLE001
        result.update({"dispatched": False, "message": "打印失败：" + str(exc)})
    finally:
        gdi32.DeleteDC(hdc)
    return result


def _png_encode(w, h, rgb):
    """RGB 原始像素 → PNG（纯标准库：zlib + struct，不引第三方依赖）。"""
    import struct
    stride = w * 3
    raw = bytearray()
    for y in range(h):
        raw.append(0)                       # 每行 filter type = 0 (None)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)     # 8bit truecolor
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def render_bill_image(cur, bill, corp=None, dpi=None):
    """按 V3 版式渲染 PNG（内存 DIB + GDI 直绘），返回 {"pages":[{png,w,h}], ...}。

    与 print_bill_layout 共用 _gdi_draw_page ⇒ **预览和出纸是同一套绘制代码**，
    手机上看到的版式就是打出来的版式（不会「预览一个样、打出来另一个样」）。

    `dpi` 传 None（默认）时**按页数自适应** —— 原因见下面注释（setData 上限）。
    """
    import ctypes
    bill = bill or {}
    lay = build_bill_layout(cur, bill, corp=corp)
    if not sys.platform.startswith("win"):
        return {"pages": [], "totalPages": 0, "error": "非 Windows 环境不支持出图"}
    api = _gdi()
    gdi32 = api["gdi32"]
    if not dpi:
        # ★★★ 按页数自适应分辨率（2026-09-23 真机白屏后加）：
        #   预览图的 base64 会**整个进小程序 setData**（单次上限 1MB），
        #   多页大单若仍按 150dpi，base64 会撑爆 setData ⇒ setData 失败 ⇒ 页面白屏、
        #   **一张图都不显示**（而不是逐页失败，极易误判成「接口没数据」）。
        #   实测单页 150dpi ≈ 70KB ⇒ base64 ≈ 94KB（≈1.34 倍），故留足余量：
        #     1 页 150dpi / 2~3 页 130dpi / ≥4 页 110dpi。
        n = len(lay["pages"])
        dpi = 150 if n <= 1 else (130 if n <= 3 else 110)
    dpi = int(dpi)
    w = int(round(210.0 / 25.4 * dpi))
    h = int(round(297.0 / 25.4 * dpi))
    hdc_screen = gdi32.CreateDCW("DISPLAY", None, None, None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    out = []
    try:
        bmi = api["BITMAPINFO"]()
        bmi.bmiHeader.biSize = ctypes.sizeof(api["BITMAPINFOHEADER"])
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = h          # 正数 = bottom-up
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = api["BI_RGB"]
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), api["DIB_RGB_COLORS"],
                                      ctypes.byref(bits), None, 0)
        if not hbmp:
            raise OSError("CreateDIBSection 失败")
        old_bmp = gdi32.SelectObject(hdc_mem, hbmp)
        _gdi_prepare(hdc_mem, dpi, dpi)
        for pg in lay["pages"]:
            # ★ 每页开始前**刷白**：内存 DC 默认是黑底，不刷会得到黑纸白字；
            #   多页时还会把上一页叠在下面。PatBlt(WHITENESS) 是 gdi32 自带，无需画笔。
            gdi32.PatBlt(hdc_mem, 0, 0, w, h, 0x00FF0062)      # WHITENESS
            _gdi_draw_page(hdc_mem, pg)
            buf = ctypes.string_at(bits, w * h * 4)
            mv = memoryview(buf)
            rows = [bytes(mv[(h - 1 - y) * w * 4:(h - y) * w * 4]) for y in range(h)]
            flat = b"".join(rows)
            rgb = bytearray(w * h * 3)
            rgb[0::3] = flat[2::4]
            rgb[1::3] = flat[1::4]
            rgb[2::3] = flat[0::4]
            out.append({"png": _png_encode(w, h, rgb), "w": w, "h": h, "page": pg["page"]})
        gdi32.SelectObject(hdc_mem, old_bmp)
        gdi32.DeleteObject(hbmp)
    finally:
        gdi32.DeleteDC(hdc_mem)
        gdi32.DeleteDC(hdc_screen)
    return {"pages": out, "totalPages": len(out), "dpi": dpi,
            "bill_no": bill.get("bill_no"), "rows": lay["rows"]}
