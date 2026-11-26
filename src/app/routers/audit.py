"""审计路由：查询、导出、留存状态。"""

from __future__ import annotations  # 延迟注解

from datetime import datetime  # 导出文件名

from fastapi import APIRouter, Depends, Query  # 路由
from fastapi.responses import Response  # CSV 响应

from ..db import get_conn  # 数据库
from ..deps import get_current_user, require_action  # 权限
from ..errors import ok  # 统一响应
from ..services.audit import export_audit_csv, get_retention_status, list_audit_logs, write_audit  # 审计

router = APIRouter(prefix="/api/audit", tags=["audit"])  # 路由前缀


@router.get("/logs")
def logs(
    limit: int = Query(default=100, ge=1, le=1000),
    actor: str | None = Query(default=None),
    action: str | None = Query(default=None),
    decision: str | None = Query(default=None),
    resource_id: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> dict:
    """审计日志查询。

    入参：过滤条件与条数；user。
    出参：统一响应，含日志数组。
    失败时的行为：无权限 403。
    """

    require_action(user, "audit.view")  # admin / loss_manager
    with get_conn() as conn:  # 独立连接
        items = list_audit_logs(conn, limit=limit, actor=actor, action=action, decision=decision, resource_id=resource_id)  # 查询
        write_audit(conn, actor=user, action="audit.logs", resource="audit", decision="allow",
                    detail={"limit": limit})  # 查阅留痕
    return ok({"items": items, "count": len(items)})  # 统一成功


@router.get("/export")
def export(user: dict = Depends(get_current_user)) -> Response:
    """导出审计 CSV。

    入参：user 管理员。
    出参：text/csv 附件。
    失败时的行为：非管理员 403。
    """

    require_action(user, "audit.export")  # 仅管理员
    with get_conn() as conn:  # 独立连接
        csv_text = export_audit_csv(conn, limit=1000)  # 导出
        write_audit(conn, actor=user, action="audit.export", resource="audit", decision="allow",
                    detail={"rows": csv_text.count("\n") - 1})  # 导出留痕
    filename = f"audit-{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"  # 文件名
    return Response(content=csv_text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})  # 返回附件


@router.get("/retention")
def retention(user: dict = Depends(get_current_user)) -> dict:
    """审计留存状态。

    入参：user。
    出参：统一响应，含留存天数、最早记录、剩余天数与归档要求。
    失败时的行为：无权限 403。
    """

    require_action(user, "audit.view")  # admin / loss_manager
    with get_conn() as conn:  # 独立连接
        data = get_retention_status(conn)  # 计算
    return ok(data)  # 统一成功
