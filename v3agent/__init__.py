# -*- coding: utf-8 -*-
"""V3 本地 Agent：小程序云端链路中运行在 V3 SQL Server 所在机器的白名单执行端。

★ 版本从 VERSION.json 来（build_gui.py 打包时生成并塞进包里），
  以前这里是硬编码 `__version__ = "0.1.0"` —— 于是**每次改完重打包，版本号一个字都不动**，
  云端日志与 /admin 里根本看不出哪家跑的是哪一版（Phase 4 就是为此存在）。
  读不到（源码直跑/漏打包）时回 `0.0.0+unknown`：宁可显眼地报"不知道"，
  也不要再假称自己是某个正式版本。
"""
import json
import os
import sys

_FALLBACK = "0.0.0+unknown"


def _candidates():
    here = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        # ★ exe 同目录那份优先：deploy-gui.py 会把带 exe_md5 的完整版本信息放这儿，
        #   运维不换包也能覆盖（与 config.json 的优先级规则保持一致）。
        yield os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                           "VERSION.json")
        yield os.path.join(here, "VERSION.json")                  # 打进包时与 v3agent 同级
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            yield os.path.join(mei, "VERSION.json")               # _MEIPASS 根
    else:
        yield os.path.join(here, "VERSION.json")
        yield os.path.join(os.path.dirname(here), "VERSION.json")  # 源码：项目根


def _source_semver():
    """源码直跑（没有 VERSION.json）时退回读仓库根的 VERSION 一行号。

    拿不到就 `0.0.0+unknown` —— 但也不冒充 0.1.0：那个号以前写死在代码里，
    改了代码重打包它也不动，等于版本信息是假的。
    """
    try:
        v = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "VERSION"), encoding="utf-8").read().strip()
        return v if v[:1].isdigit() else ""
    except OSError:
        return ""


def _read_version_file():
    for p in _candidates():
        try:
            if not os.path.isfile(p):
                continue
            d = json.load(open(p, encoding="utf-8"))
            semver = str(d.get("semver") or "").strip()
            build = str(d.get("build_id") or "").strip()
            if not semver:
                continue
            return (semver + ("+" + build[:12] if build else ""), build,
                    str(d.get("built_at") or ""), str(d.get("exe_md5") or ""))
        except Exception:  # noqa: BLE001 半个文件/编码问题都不该让 Agent 起不来
            continue
    src = _source_semver()
    if src:
        return src + "+source", "", "", ""
    return _FALLBACK, "", "", ""


__version__, BUILD_ID, BUILT_AT, EXE_MD5 = _read_version_file()


def version_info():
    """给握手帧与状态文件用（hello 只带 __version__，其余留本地排查）。"""
    return {"version": __version__, "build_id": BUILD_ID,
            "built_at": BUILT_AT, "exe_md5": EXE_MD5}
