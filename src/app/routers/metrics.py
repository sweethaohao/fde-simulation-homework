"""指标路由：按口径返回数字，未签发/数据不足返回业务级拦截。"""

from __future__ import annotations  # 延迟注解

from fastapi import APIRouter, Depends, Query  # 路由
from fastapi.responses import JSONResponse  # 业务拦截统一 200

from ..agents import trace as trace_agent  # 口径溯源
from ..db import get_conn  # 数据库
from ..deps import allowed_store_ids, get_current_user, require_action, require_scope  # 权限
from ..errors import AppError, ErrorCode, fail, ok  # 统一响应
from ..services import metrics as metrics_service  # 指标服务

router = APIRouter(prefix="/api/metrics", tags=["metrics"])  # 路由前缀


@router.get("/{code}")
def get_metric(code: str, store_id: str | None = Query(default=None), user: dict = Depends(get_current_user)):
    """查询单指标。

    入参：code 指标编码；store_id 可选门店；user。
    出参：已签发且数据就绪时 200 ok；未签发/数据不足时 200 ok=false（业务拦截）。
    失败时的行为：门店越域 403；指标不存在 404。
    """

    require_action(user, "metrics.view")  # 动作权限
    with get_conn() as conn:  # 独立连接
        if store_id:  # 指定门店时校验数据域
            require_scope(conn, user, store_id)
            stores = [store_id]
        else:
            stores = allowed_store_ids(conn, user)  # 聚合数据域
        if not stores:  # 无可见门店
            raise AppError(ErrorCode.FORBIDDEN_SCOPE, "没有可见门店", status=403)
        payload = metrics_service.metric_payload(conn, code, stores)  # 计算
    if payload.get("status") == "blocked":  # R10 / 数据不足
        code_value = payload.get("reason_code", "METRIC_BLOCKED")  # 区分两类拦截
        err = ErrorCode.METRIC_BLOCKED if code_value == "METRIC_BLOCKED" else ErrorCode.DATA_NOT_READY  # 映射错误码
        return JSONResponse(status_code=200, content=fail(err, payload.get("reason", "指标不可用"), payload))
    return ok(payload)  # 正常出数


@router.get("/{code}/trace")
def trace_metric(code: str, depth: int = Query(default=3, ge=1, le=3), user: dict = Depends(get_current_user)) -> dict:
    """指标口径溯源。

    入参：code 指标编码；depth 跳数；user。
    出参：统一响应，含签发关系与路径。
    失败时的行为：指标节点不存在时返回空路径（不报错）。
    """

    require_action(user, "metrics.view")  # 动作权限
    with get_conn() as conn:  # 独立连接
        data = trace_agent.metric_trace(conn, code, depth)  # 图谱溯源
    return ok(data)  # 统一成功
