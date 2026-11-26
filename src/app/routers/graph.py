"""图谱路由：路径查询（≤3 跳）与混合检索。"""

from __future__ import annotations  # 延迟注解

from fastapi import APIRouter, Depends, Query, Request  # 路由

from ..agents import trace as trace_agent  # 溯源
from ..db import get_conn  # 数据库
from ..deps import get_current_user, require_action  # 权限
from ..errors import AppError, ErrorCode, ok  # 统一响应
from ..services.audit import write_audit  # 敏感读取留痕

router = APIRouter(prefix="/api/graph", tags=["graph"])  # 路由前缀


@router.get("/path")
def graph_path(
    request: Request,
    src: str = Query(min_length=1, description="起点，如 suggestion:ORD-2026-0001"),
    depth: int = Query(default=2, ge=1, description="跳数，最多 3"),
    rel: str | None = Query(default=None, description="单关系过滤，如 OWNED_BY"),
    rels: str | None = Query(default=None, description="多关系过滤，逗号分隔"),
    direction: str = Query(default="out", pattern="^(out|in)$"),
    user: dict = Depends(get_current_user),
) -> dict:
    """多跳路径查询。

    入参：src 起点；depth 跳数；rel/rels 关系；direction 方向；user。
    出参：统一响应，含 hops 与 paths。
    失败时的行为：depth>3 返回 422 VALIDATION_ERROR。
    """

    require_action(user, "graph.view")  # 动作权限
    if depth > 3:  # 第 8 节硬约束
        raise AppError(ErrorCode.VALIDATION_ERROR, "最多 3 跳", status=422)
    rel_list: list[str] | None = None  # 关系列表
    if rels:  # 多关系优先
        rel_list = [r.strip() for r in rels.split(",") if r.strip()]
    elif rel:  # 单关系
        rel_list = [rel]
    with get_conn() as conn:  # 独立连接
        data = trace_agent.path(conn, src, depth=depth, rels=rel_list, direction=direction)  # 查询
        write_audit(conn, actor=user, action="graph.path", resource="graph", resource_id=src,
                    decision="allow", detail={"depth": depth, "rels": rel_list}, ip=request.client.host if request.client else None)  # 留痕
    return ok(data)  # 统一成功


@router.get("/search")
def graph_search(
    q: str = Query(min_length=1, max_length=200),
    top_k: int = Query(default=6, ge=1, le=20),
    user: dict = Depends(get_current_user),
) -> dict:
    """混合检索（向量 + FTS5）。

    入参：q 查询；top_k 条数；user。
    出参：统一响应，含 mode/degraded/items。
    失败时的行为：不抛异常；模型不可用时自动降级。
    """

    require_action(user, "graph.view")  # 动作权限
    with get_conn() as conn:  # 独立连接
        data = trace_agent.search(conn, q, top_k)  # 检索
    return ok(data)  # 统一成功
