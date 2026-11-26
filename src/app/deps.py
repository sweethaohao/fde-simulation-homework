"""认证依赖与三层权限校验：角色 → 数据域 → 动作。

三层全过才放行；任一层不过返回 403 并写入审计。
admin 的「查看全部」只是放宽数据域，不放宽动作：admin 调签核接口必须 FORBIDDEN_ACTION。
"""

from __future__ import annotations  # 延迟注解

from typing import Any  # 类型

from fastapi import Request  # 取请求头

from .db import get_conn, row_to_dict  # 数据库
from .errors import AppError, ErrorCode  # 统一错误
from .security import decode_token  # token 解析
from .services.audit import write_audit  # 越权留痕

# 动作 → 允许的角色（第 3 节权限表的可执行版本）
ROLE_ACTIONS: dict[str, set[str]] = {
    # 查看类：admin 全局可见，其余按数据域裁剪
    "dashboard.view": {"admin", "ops_director", "loss_manager", "planner", "store_manager", "logistics", "data_engineer"},
    "metrics.view": {"admin", "ops_director", "loss_manager", "planner", "store_manager", "logistics", "data_engineer"},
    "graph.view": {"admin", "ops_director", "loss_manager", "planner", "store_manager", "logistics", "data_engineer"},
    "readiness.view": {"admin", "ops_director", "loss_manager", "planner", "store_manager", "logistics", "data_engineer"},
    "guardrail.view": {"admin", "ops_director", "loss_manager", "planner", "logistics"},
    # 业务动线：生成建议只能计划员；提交/修改同理
    "suggestion.generate": {"planner"},
    "order.view": {"admin", "ops_director", "loss_manager", "planner", "store_manager", "logistics"},
    "order.submit": {"planner"},
    "order.update": {"planner"},
    "order.sign": {"ops_director"},  # 签核权归运营总监；admin 不在其中（刻意）
    "order.reject": {"ops_director", "loss_manager"},  # 风险否决权归商品与损耗经理
    "order.rebuild": {"admin", "ops_director", "loss_manager", "planner", "store_manager", "logistics"},
    # 管理面
    "audit.view": {"admin", "loss_manager"},
    "audit.export": {"admin"},
    "users.manage": {"admin"},
    "agents.view": {"admin", "ops_director", "loss_manager", "data_engineer"},
}

# 角色中文名，前端与审计共用
ROLE_NAMES: dict[str, str] = {
    "admin": "系统管理员",
    "ops_director": "运营总监",
    "loss_manager": "商品与损耗经理",
    "planner": "补货计划员",
    "store_manager": "门店店长",
    "logistics": "区域物流经理",
    "data_engineer": "数据工程师",
}


def _bearer(request: Request) -> str:
    """从请求头取 Bearer token。

    入参：request FastAPI 请求。
    出参：token 字符串。
    失败时的行为：缺失或格式错误抛 AppError(AUTH_REQUIRED, 401)。
    """

    header = request.headers.get("Authorization", "")  # 读取授权头
    if not header.startswith("Bearer "):  # 只接受 Bearer 方案
        raise AppError(ErrorCode.AUTH_REQUIRED, "请先登录", status=401)  # 未登录
    return header[7:].strip()  # 去掉前缀


def get_current_user(request: Request) -> dict:
    """解析当前用户（FastAPI 依赖）。

    入参：request 请求。
    出参：用户字典（含角色与数据域）。
    失败时的行为：token 无效 401；用户被停用 403；用户不存在 401。
    """

    token = _bearer(request)  # 取 token
    claims = decode_token(token)  # 校验签名与有效期
    with get_conn() as conn:  # 每次请求独立连接
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (claims.get("sub"),)).fetchone()  # 以库为准，避免 token 被改造
        user = row_to_dict(row)  # 转字典
    if not user:  # token 指向已删除用户
        raise AppError(ErrorCode.AUTH_INVALID, "用户不存在", status=401)  # 视为无效登录
    if not user["active"]:  # 被停用账号
        raise AppError(ErrorCode.FORBIDDEN_ACTION, "账号已停用", status=403)  # 明确拒绝
    request.state.user = user  # 缓存到请求上下文，便于审计取用
    return user  # 返回用户


def require_action(user: dict, action: str, detail: dict | None = None) -> None:
    """校验动作权限（三层中的第三层）。

    入参：user 当前用户；action 动作名；detail 附加到审计的说明。
    出参：无。
    失败时的行为：无权限时写审计并以 403 FORBIDDEN_ACTION 抛出。
    """

    allowed = ROLE_ACTIONS.get(action, set())  # 未声明的动作默认拒绝（fail closed）
    if user["role"] in allowed:  # 角色允许即通过
        return
    with get_conn() as conn:  # 越权必须留痕
        write_audit(
            conn,
            actor=user,
            action=action,
            resource="action",
            resource_id=action,
            decision="deny",
            error_code=ErrorCode.FORBIDDEN_ACTION.value,
            detail=detail or {},
            scope=user.get("scope_type"),  # 记录当时数据域
        )
    if user["role"] == "admin" and action == "order.sign":  # 验收点：管理员尝试签核被拒
        raise AppError(
            ErrorCode.FORBIDDEN_ACTION,
            "管理员只能查看，不能签核业务单据（保留人在环）",
            status=403,
            data={"hint": "审计已记录：管理员尝试签核被拒"},  # 明示留痕
        )
    raise AppError(ErrorCode.FORBIDDEN_ACTION, f"角色 {user['role']} 不得执行 {action}", status=403)  # 通用拒绝


def allowed_store_ids(conn, user: dict) -> list[str]:
    """计算用户可见门店集合。

    入参：conn 连接；user 当前用户。
    出参：门店 ID 列表（按 ID 排序）。
    失败时的行为：SQL 异常向上抛出。
    """

    if user["scope_type"] == "global":  # 全局域：全部门店
        rows = conn.execute("SELECT store_id FROM stores ORDER BY store_id").fetchall()  # 全部门店
        return [r["store_id"] for r in rows]  # 返回列表
    if user["scope_type"] == "region":  # 区域域：按区域过滤
        rows = conn.execute("SELECT store_id FROM stores WHERE region = ? ORDER BY store_id", (user["scope_value"],)).fetchall()  # 区域内门店
        return [r["store_id"] for r in rows]  # 返回列表
    if user["scope_type"] == "store":  # 门店域：仅本店
        return [user["scope_value"]] if user["scope_value"] else []  # 空配置视为不可见任何门店
    return []  # 未知域一律拒绝，避免默认放大权限


def require_scope(conn, user: dict, store_id: str | None) -> None:
    """校验数据域（三层中的第二层）。

    入参：conn 连接；user 当前用户；store_id 目标门店（可为空表示聚合）。
    出参：无。
    失败时的行为：越域时写审计并以 403 FORBIDDEN_SCOPE 抛出。
    """

    allowed = set(allowed_store_ids(conn, user))  # 可见门店集合
    if store_id is None:  # 聚合查询：有任一可见门店即可
        if allowed:
            return
    elif store_id in allowed:  # 目标门店可见
        return
    write_audit(
        conn,
        actor=user,
        action="scope.check",
        resource="store",
        resource_id=str(store_id),
        decision="deny",
        error_code=ErrorCode.FORBIDDEN_SCOPE.value,
        detail={"allowed": sorted(allowed), "requested": store_id},  # 便于复核越域范围
        scope=user.get("scope_type"),  # 当前数据域
    )
    conn.commit()  # 先落审计再拒绝；否则调用方事务回滚会把越域留痕丢掉
    raise AppError(ErrorCode.FORBIDDEN_SCOPE, "该数据不在你的数据域内", status=403)  # 拒绝


def scope_payload(conn, user: dict) -> dict[str, Any]:
    """构造数据域描述（返回给前端）。

    入参：conn；user。
    出参：{type, value, stores, role_name}。
    失败时的行为：SQL 异常向上抛出。
    """

    stores = allowed_store_ids(conn, user)  # 可见门店
    names = {r["store_id"]: r["name"] for r in conn.execute("SELECT store_id, name FROM stores").fetchall()}  # 门店名映射
    return {
        "type": user["scope_type"],  # global / region / store
        "value": user["scope_value"],  # 具体取值
        "stores": [{"store_id": sid, "name": names.get(sid, sid)} for sid in stores],  # 带名称的门店列表
        "role_name": ROLE_NAMES.get(user["role"], user["role"]),  # 中文角色名
    }
