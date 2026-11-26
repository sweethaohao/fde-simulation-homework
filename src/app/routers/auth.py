"""认证路由：登录、当前用户、登出。"""

from __future__ import annotations  # 延迟注解

from fastapi import APIRouter, Depends, Request  # 路由与依赖
from pydantic import BaseModel, Field  # 入参校验

from ..db import get_conn, row_to_dict  # 数据库
from ..deps import ROLE_ACTIONS, ROLE_NAMES, get_current_user, scope_payload  # 权限
from ..errors import AppError, ErrorCode, ok  # 统一响应
from ..security import create_token, verify_password  # 认证
from ..services.audit import write_audit  # 留痕

router = APIRouter(prefix="/api/auth", tags=["auth"])  # 路由前缀


class LoginIn(BaseModel):
    """登录入参。"""

    username: str = Field(min_length=1, max_length=64)  # 用户名
    password: str = Field(min_length=1, max_length=128)  # 口令


@router.post("/login")
def login(payload: LoginIn, request: Request) -> dict:
    """账号密码登录。

    入参：payload 用户名口令；request 用于取 IP。
    出参：统一响应，data 含 token、用户信息与数据域。
    失败时的行为：账号密码错误返回 401 AUTH_INVALID，并写审计。
    """

    ip = request.client.host if request.client else None  # 来源 IP
    with get_conn() as conn:  # 独立连接
        row = conn.execute("SELECT * FROM users WHERE username = ?", (payload.username,)).fetchone()  # 查用户
        user = row_to_dict(row)  # 转字典
        if not user or not user["active"] or not verify_password(payload.password, user["password_hash"]):  # 三类失败合并
            write_audit(conn, actor=None, action="auth.login", resource="user", resource_id=payload.username,
                        decision="deny", error_code=ErrorCode.AUTH_INVALID.value,
                        detail={"username": payload.username}, ip=ip)  # 失败留痕（不含口令）
            conn.commit()  # 先提交审计再抛错，避免事务回滚把失败留痕一起丢掉
            raise AppError(ErrorCode.AUTH_INVALID, "用户名或口令错误", status=401)
        token = create_token(user)  # 签发 token
        write_audit(conn, actor=user, action="auth.login", resource="user", resource_id=user["user_id"],
                    decision="allow", detail={"role": user["role"], "scope": user["scope_type"]}, ip=ip)  # 成功留痕
        scope = scope_payload(conn, user)  # 数据域描述
        safe_user = {k: user[k] for k in ("user_id", "username", "email", "display_name", "role", "scope_type", "scope_value")}  # 不含口令哈希
        safe_user["role_name"] = ROLE_NAMES.get(user["role"], user["role"])  # 中文角色名
        return ok({"token": token, "user": safe_user, "scope": scope, "synthetic": True})  # 统一成功


@router.get("/me")
def me(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """当前登录用户。

    入参：request；user 依赖注入。
    出参：统一响应，含用户、数据域与可执行动作。
    失败时的行为：未登录由依赖抛 401。
    """

    with get_conn() as conn:  # 独立连接
        scope = scope_payload(conn, user)  # 数据域
    safe_user = {k: user[k] for k in ("user_id", "username", "email", "display_name", "role", "scope_type", "scope_value")}  # 去掉口令
    safe_user["role_name"] = ROLE_NAMES.get(user["role"], user["role"])  # 中文角色名
    actions = sorted(a for a, roles in ROLE_ACTIONS.items() if user["role"] in roles)  # 可执行动作
    return ok({"user": safe_user, "scope": scope, "actions": actions, "request_id": getattr(request.state, "request_id", "")})


@router.post("/logout")
def logout(user: dict = Depends(get_current_user)) -> dict:
    """登出（无状态 token，仅留痕）。

    入参：user。
    出参：统一响应。
    失败时的行为：未登录由依赖抛 401。
    """

    with get_conn() as conn:  # 独立连接
        write_audit(conn, actor=user, action="auth.logout", resource="user", resource_id=user["user_id"],
                    decision="allow", detail={})  # 登出留痕
    return ok({"logged_out": True})  # 前端清除本地 token
