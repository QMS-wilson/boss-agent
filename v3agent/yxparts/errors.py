# -*- coding: utf-8 -*-
"""燕秀相关 op 的业务错误。

为什么单独立一个异常类型：Agent 的 dispatch 只认 LookupError / ValueError →
NOT_FOUND / BAD_REQUEST，其余一律 INTERNAL。手机端要按错误码给不同引导
（"没识别到表头" 要去核对文件，"物料库是空的" 要去维护品名），
所以需要一个能带 code + 定位信息的异常，由 dispatch 统一翻译成
`{"ok": false, "error": {"code": ..., "message": ..., "detail": ...}}`。

错误码约定：一律 `YX_` 前缀，message 用中文、可直接展示给用户。
"""
from __future__ import annotations


class YxError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = str(code or "YX_ERROR")
        self.message = str(message or "")
        self.detail = detail or {}

    def to_error(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class BillConflict(Exception):
    """改单乐观锁冲突：这张单在你打开之后被别人改过了（2026-10-11）。

    为什么单独立类型而不是复用 ValueError：App 要按错误码给**不同引导** ——
    普通的 BAD_REQUEST 是"你填错了"，这个是"你的内容过期了，退出去重新打开"，
    两者在界面上的处置完全不同。dispatch 里本类翻译成 `BILL_CONFLICT`。

    `detail` 会带 `current_revision`（当前库内版本）与 `updated_at`，
    前端可以据此决定是提示刷新还是直接静默重拉。
    """

    code = "BILL_CONFLICT"

    def __init__(self, message, current_revision="", updated_at=""):
        super().__init__(message)
        self.message = str(message or "")
        self.current_revision = str(current_revision or "")
        self.updated_at = str(updated_at or "")

    def to_error(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "detail": {
                "current_revision": self.current_revision,
                "updated_at": self.updated_at,
            },
        }
