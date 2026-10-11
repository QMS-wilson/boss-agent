# -*- coding: utf-8 -*-
"""线上验收脚本用的应用账号（**不落仓库**）。

★ 2026-09-26：同一个应用账号口令此前被复制进 50 个脚本（含 40 多个线上门），
  任何一个文件被贴进群/问答/备份都是一次泄露。口令取值顺序：
  环境变量 JXC_APP_PWD → ~/.ssh/jxc_app_pw → 都没有就报清楚怎么补。
  轮换口令时只改那一处文件，脚本一行都不用动。
"""
import os

ACCOUNT = os.environ.get("JXC_APP_ACCOUNT", "system")


def app_password():
    pw = (os.environ.get("JXC_APP_PWD") or "").strip()
    if not pw:
        p = os.path.expanduser("~/.ssh/jxc_app_pw")
        try:
            pw = open(p, encoding="utf-8").read().strip()
        except OSError:
            pw = ""
    if not pw:
        raise SystemExit("缺应用账号口令：设 JXC_APP_PWD 环境变量，或写 %s"
                         % os.path.expanduser("~/.ssh/jxc_app_pw"))
    return pw


APP_PASSWORD = app_password()


# ---------------- V3 操作员（provider=v3 那条登录路） ----------------
# 2026-09-29 加：单据的制单人现在取的是**令牌里的 V3 操作员**，要验这条链路就得真用
# V3 身份登录一次。口令同样**不落仓库**：JXC_V3_PWD → ~/.ssh/jxc_v3_pw。
# ★ 与 app_password() 不同，这里取不到就返回空串、**不 SystemExit** ——
#   应用账号那条腿仍然要能独立跑，V3 那条腿缺口令就跳过并说明原因。
V3_USER = os.environ.get("JXC_V3_UID", "101").strip()


def v3_password():
    pw = (os.environ.get("JXC_V3_PWD") or "").strip()
    if not pw:
        p = os.path.expanduser("~/.ssh/jxc_v3_pw")
        try:
            pw = open(p, encoding="utf-8").read().strip()
        except OSError:
            pw = ""
    return pw
