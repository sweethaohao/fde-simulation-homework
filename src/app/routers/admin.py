"""管理面路由：账号、导入批次、Agent 注册表、兜底规则。"""

from __future__ import annotations  # 延迟注解

from fastapi import APIRouter, Depends, Query  # 路由
from pydantic import BaseModel, Field  # 入参

from ..agents import guardrail  # 兜底规则
from ..agents.registry import registry_payload  # Agent 注册表
from ..db import get_conn, now_iso  # 数据库
from ..deps import ROLE_NAMES, allowed_store_ids, get_current_user, require_action  # 权限
from ..errors import AppError, ErrorCode, ok  # 统一响应
from ..security import hash_password  # 口令哈希
from ..services import metrics as metrics_service  # 就绪度
from ..services.audit import write_audit  # 留痕

router = APIRouter(prefix="/api/admin", tags=["admin"])  # 路由前缀

VALID_ROLES = set(ROLE_NAMES.keys())  # 合法角色
VALID_SCOPES = {"global", "region", "store"}  # 合法数据域类型
SAFE_USER_FIELDS = ("user_id", "username", "email", "display_name", "role", "scope_type", "scope_value", "active", "created_at")  # 返回字段白名单


class CreateUserIn(BaseModel):
    """创建账号入参。"""

    username: str = Field(min_length=3, max_length=64)  # 登录名
    email: str = Field(min_length=3, max_length=128)  # 邮箱
    display_name: str = Field(min_length=1, max_length=64)  # 显示名
    role: str  # 角色
    scope_type: str = "store"  # 数据域类型
    scope_value: str | None = None  # 数据域取值
    password: str = Field(min_length=8, max_length=128)  # 初始口令


class UpdateUserIn(BaseModel):
    """更新账号入参（全部可选）。"""

    email: str | None = Field(default=None, max_length=128)  # 邮箱
    display_name: str | None = Field(default=None, max_length=64)  # 显示名
    role: str | None = None  # 角色
    scope_type: str | None = None  # 数据域类型
    scope_value: str | None = None  # 数据域取值
    active: bool | None = None  # 启停
    password: str | None = Field(default=None, min_length=8, max_length=128)  # 重置口令


def _validate_scope(conn, scope_type: str, scope_value: str | None) -> None:
    """校验数据域配置合法。

    入参：conn；scope_type；scope_value。
    出参：无。
    失败时的行为：非法配置抛 422 VALIDATION_ERROR。
    """

    if scope_type not in VALID_SCOPES:  # 类型必须合法
        raise AppError(ErrorCode.VALIDATION_ERROR, f"scope_type 必须是 {sorted(VALID_SCOPES)}", status=422)
    if scope_type == "global":  # 全局无需取值
        return
    if not scope_value:  # 区域/门店必须给值
        raise AppError(ErrorCode.VALIDATION_ERROR, "区域/门店数据域必须提供 scope_value", status=422)
    if scope_type == "store" and not conn.execute("SELECT 1 FROM stores WHERE store_id = ?", (scope_value,)).fetchone():  # 门店存在
        raise AppError(ErrorCode.VALIDATION_ERROR, "门店不存在", status=422)
    if scope_type == "region" and not conn.execute("SELECT 1 FROM stores WHERE region = ? LIMIT 1", (scope_value,)).fetchone():  # 区域存在
        raise AppError(ErrorCode.VALIDATION_ERROR, "区域不存在", status=422)


@router.get("/users")
def list_users(user: dict = Depends(get_current_user)) -> dict:
    """账号列表。

    入参：user 管理员。
    出参：统一响应，含账号数组（不含口令哈希）。
    失败时的行为：非管理员 403。
    """

    require_action(user, "users.manage")  # 动作权限
    with get_conn() as conn:  # 独立连接
        rows = conn.execute("SELECT * FROM users ORDER BY user_id").fetchall()  # 全量账号
        items = [{k: r[k] for k in SAFE_USER_FIELDS} for r in rows]  # 白名单字段
        for item in items:  # 补中文角色名
            item["role_name"] = ROLE_NAMES.get(item["role"], item["role"])
    return ok({"items": items})  # 统一成功


@router.post("/users")
def create_user(payload: CreateUserIn, user: dict = Depends(get_current_user)) -> dict:
    """创建账号。

    入参：payload；user 管理员。
    出参：统一响应，含新账号。
    失败时的行为：角色/数据域非法 422；用户名重复 409。
    """

    require_action(user, "users.manage")  # 动作权限
    if payload.role not in VALID_ROLES:  # 角色白名单
        raise AppError(ErrorCode.VALIDATION_ERROR, "角色不合法", status=422)
    with get_conn() as conn:  # 独立连接
        _validate_scope(conn, payload.scope_type, payload.scope_value)  # 数据域
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (payload.username,)).fetchone():  # 唯一性
            raise AppError(ErrorCode.VALIDATION_ERROR, "用户名已存在", status=409)
        user_id = f"U-{payload.username}"  # 约定 ID
        conn.execute(
            """
            INSERT INTO users(user_id,username,email,display_name,role,scope_type,scope_value,
              password_hash,active,created_at,is_synthetic)
            VALUES (?,?,?,?,?,?,?,?,1,?,1)
            """,
            (user_id, payload.username, payload.email, payload.display_name, payload.role,
             payload.scope_type, payload.scope_value if payload.scope_type != "global" else None,
             hash_password(payload.password), now_iso()),
        )  # 插入账号
        write_audit(conn, actor=user, action="users.create", resource="user", resource_id=user_id,
                    decision="allow", detail={"role": payload.role, "scope_type": payload.scope_type})  # 留痕
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()  # 回读
        data = {k: row[k] for k in SAFE_USER_FIELDS}  # 白名单
    return ok({"user": data})  # 统一成功


@router.patch("/users/{user_id}")
def update_user(user_id: str, payload: UpdateUserIn, user: dict = Depends(get_current_user)) -> dict:
    """更新账号。

    入参：user_id；payload 可选字段；user 管理员。
    出参：统一响应，含更新后账号。
    失败时的行为：不能停用自己；非法配置 422；不存在 404。
    """

    require_action(user, "users.manage")  # 动作权限
    with get_conn() as conn:  # 独立连接
        target = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()  # 目标账号
        if not target:  # 不存在
            raise AppError(ErrorCode.NOT_FOUND, "账号不存在", status=404)
        if user_id == user["user_id"] and payload.active is False:  # 防止把自己锁在门外
            raise AppError(ErrorCode.VALIDATION_ERROR, "不能停用当前登录账号", status=422)
        if payload.role and payload.role not in VALID_ROLES:  # 角色白名单
            raise AppError(ErrorCode.VALIDATION_ERROR, "角色不合法", status=422)
        scope_type = payload.scope_type or target["scope_type"]  # 未传则沿用
        scope_value = payload.scope_value if payload.scope_value is not None else target["scope_value"]  # 未传则沿用
        if payload.scope_type or payload.scope_value:  # 只有改了数据域才校验
            _validate_scope(conn, scope_type, scope_value)
        updates: list[str] = []  # 动态更新列
        params: list = []  # 参数
        for column, value in (
            ("email", payload.email), ("display_name", payload.display_name), ("role", payload.role),
            ("scope_type", payload.scope_type), ("scope_value", payload.scope_value),
            ("active", None if payload.active is None else int(payload.active)),
        ):
            if value is not None:  # 只更新显式传入字段
                updates.append(f"{column} = ?")
                params.append(value)
        if payload.password:  # 重置口令单独处理
            updates.append("password_hash = ?")
            params.append(hash_password(payload.password))
        if not updates:  # 无变更
            raise AppError(ErrorCode.VALIDATION_ERROR, "没有可更新的字段", status=422)
        params.append(user_id)  # WHERE 参数
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE user_id = ?", params)  # 执行更新
        write_audit(conn, actor=user, action="users.update", resource="user", resource_id=user_id,
                    decision="allow", detail={"fields": [u.split(" =")[0] for u in updates]})  # 留痕（不含口令）
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()  # 回读
        data = {k: row[k] for k in SAFE_USER_FIELDS}  # 白名单
    return ok({"user": data})  # 统一成功


@router.get("/import")
def import_status(user: dict = Depends(get_current_user)) -> dict:
    """导入批次与就绪度。

    入参：user。
    出参：统一响应，含 CSV 导入记录与就绪度。
    失败时的行为：动作/数据域不足 403。
    """

    require_action(user, "readiness.view")  # 只读
    with get_conn() as conn:  # 独立连接
        imports = [dict(r) for r in conn.execute("SELECT * FROM data_imports ORDER BY import_id").fetchall()]  # 导入批次
        stores = allowed_store_ids(conn, user)  # 数据域
        ready = metrics_service.readiness(conn, stores)  # 就绪度
    return ok({"imports": imports, "readiness": ready, "note": "外部数据只经 CSV 导入（scripts/init_db.py），不回写源系统（R3）"})  # 统一成功


@router.get("/agents")
def agents(user: dict = Depends(get_current_user)) -> dict:
    """Agent 与工具注册表。

    入参：user。
    出参：统一响应，含 Harness 落点说明。
    失败时的行为：非允许角色 403。
    """

    require_action(user, "agents.view")  # 动作权限
    return ok(registry_payload())  # 静态注册表


@router.get("/guardrail/rules")
def guardrail_rules(user: dict = Depends(get_current_user), enabled_only: bool = Query(default=False)) -> dict:
    """兜底规则清单。

    入参：user；enabled_only 兼容参数。
    出参：统一响应，含五类规则与人工确认时限。
    失败时的行为：非允许角色 403。
    """

    require_action(user, "guardrail.view")  # 动作权限
    payload = guardrail.rules_payload()  # 规则清单
    if enabled_only:  # 当前实现全部启用
        payload["rules"] = [r for r in payload["rules"] if True]
    return ok(payload)  # 统一成功
