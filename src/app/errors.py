"""统一错误码与统一响应构造。

所有 /api/* 返回 `{ok, data, error}` 结构；错误码只使用本文件枚举，禁止散落字符串。
"""

from __future__ import annotations  # 延迟类型注解，兼容 3.13 语法

import enum  # 错误码用枚举，避免拼写漂移
from typing import Any  # data 可能是任意 JSON 结构


class ErrorCode(str, enum.Enum):
    """全局错误码枚举（与 BUILD_PROMPT 第 11 节一致，不得改变语义）。"""

    AUTH_REQUIRED = "AUTH_REQUIRED"                        # 未携带或格式错误的 token
    AUTH_INVALID = "AUTH_INVALID"                          # 账号密码错误 / token 无效
    FORBIDDEN_SCOPE = "FORBIDDEN_SCOPE"                    # 越过数据域（店长看他店）
    FORBIDDEN_ACTION = "FORBIDDEN_ACTION"                  # 角色不允许该动作（admin 签核）
    VALIDATION_ERROR = "VALIDATION_ERROR"                  # 入参校验失败
    NOT_FOUND = "NOT_FOUND"                                # 资源不存在
    CONFLICT_ALREADY_SIGNED = "CONFLICT_ALREADY_SIGNED"    # 单据已签核，不能重复签
    SELF_SIGN_FORBIDDEN = "SELF_SIGN_FORBIDDEN"            # 不允许自己签自己提交的单
    GUARDRAIL_STOPPED = "GUARDRAIL_STOPPED"                # 命中 f44 兜底，停止自动建议
    METRIC_BLOCKED = "METRIC_BLOCKED"                      # R10：指标口径未签发，不出数
    DATA_NOT_READY = "DATA_NOT_READY"                      # 数据不足 / 依赖未就绪
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"                    # 本地模型不可用（可降级）
    IMPORT_FAILED = "IMPORT_FAILED"                        # CSV 导入失败
    INTERNAL = "INTERNAL"                                  # 未预期异常


class AppError(Exception):
    """业务异常：由全局异常处理器转成统一响应，不向前端泄漏堆栈。

    入参：code 错误码；msg 面向人的说明；status HTTP 状态码；data 附加结构化信息。
    出参：异常对象本身。
    失败时的行为：不吞异常；未捕获时由 main.py 兜底为 500 INTERNAL。
    """

    def __init__(self, code: ErrorCode, msg: str, status: int = 400, data: dict[str, Any] | None = None) -> None:
        super().__init__(msg)  # 保留原始消息供日志使用
        self.code = code  # 枚举错误码
        self.msg = msg  # 面向人的说明
        self.status = status  # HTTP 状态码
        self.data = data or {}  # 附加数据（例如兜底详情）


def ok(data: Any) -> dict[str, Any]:
    """成功响应。

    入参：data 业务数据。
    出参：`{ok:true, data, error:null}`。
    失败时的行为：不抛异常。
    """

    return {"ok": True, "data": data, "error": None}  # 统一成功信封


def fail(code: ErrorCode, msg: str, data: Any = None) -> dict[str, Any]:
    """失败响应。

    入参：code 错误码枚举；msg 说明；data 附加结构。
    出参：`{ok:false, data, error:{code,msg}}`。
    失败时的行为：不抛异常；HTTP 状态码由调用方决定。
    """

    return {"ok": False, "data": data, "error": {"code": code.value, "msg": msg}}  # 统一失败信封
