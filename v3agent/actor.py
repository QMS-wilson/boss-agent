"""请求级「这是谁」—— 单据审计列（zdrbh / UserInsert / UserUpdate / UserVoid）的取值来源。

★ 身份**只从云端中转帧上的 `actor` 字段进来**（见 agent.handle_request）：
    · 云端从令牌 claims 里取 `v3uid`，只有财鲸 V3 操作员登录的会话才带；
    · 客户端自己在业务 params 里写的 actor 一律不认 —— 否则谁都能把单挂到别人名下。

★ 没有身份时回落 `FALLBACK`（= V3 操作员表里真存在的 `SYSDEV`），**不是**自造一个代码：
    打印时 printing._zdr_name 会拿这个值去 `UserInfo` 换姓名，查不到就会把裸代码印在单上。
    历史上这里写死 `SYSTEM`，所以每张手机开的单制单人都是"系统管理员"，看不出是谁开的。

★ 为什么用 threading.local 而不是 contextvar：`dispatch()` 到 handler 是**同步**一条道走完
    （handler 里没有 await），要么同线程内不会交错，要么本来就在各自的线程里 —— 两种情况都对。
    设值只包住 handler 那一段，出块即还原，不留到下一条请求。
"""
import contextlib
import threading

FALLBACK = "SYSDEV"

_local = threading.local()


def current():
    """当前请求的制单人代码；没注入身份时给兜底值。"""
    return getattr(_local, "actor", "") or FALLBACK


@contextlib.contextmanager
def use(code):
    prev = getattr(_local, "actor", "")
    _local.actor = str(code or "").strip()
    try:
        yield _local.actor or FALLBACK
    finally:
        _local.actor = prev
