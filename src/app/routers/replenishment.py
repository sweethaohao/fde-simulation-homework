"""补货路由：建议 → 待签核 → 签核/驳回 → 重建。"""

from __future__ import annotations  # 延迟注解

from fastapi import APIRouter, Depends, Query  # 路由
from fastapi.responses import JSONResponse  # 业务拦截 200
from pydantic import BaseModel, Field  # 入参校验

from ..agents import diagnose as diagnose_agent  # 诊断
from ..db import get_conn  # 数据库
from ..deps import allowed_store_ids, get_current_user, require_action, require_scope  # 权限
from ..errors import AppError, ErrorCode, fail, ok  # 统一响应
from ..services import replenishment as service  # 业务服务
from ..services.audit import write_audit  # 留痕

router = APIRouter(prefix="/api/replenishment", tags=["replenishment"])  # 路由前缀


class SuggestIn(BaseModel):
    """生成建议入参。"""

    store_id: str = Field(min_length=1, max_length=16)  # 门店
    sku_id: str = Field(min_length=1, max_length=16)  # 商品
    biz_date: str | None = Field(default=None, max_length=10)  # 业务日（缺省取该店最新）


class UpdateIn(BaseModel):
    """修改建议量入参。"""

    final_qty: int = Field(ge=0, le=10000)  # 新数量


class RejectIn(BaseModel):
    """驳回入参。"""

    reason: str = Field(min_length=2, max_length=200)  # 必填原因


def _check_order_scope(conn, user: dict, order: dict) -> None:
    """校验单据所属门店在数据域内。

    入参：conn；user；order 单据。
    出参：无。
    失败时的行为：越域抛 403 FORBIDDEN_SCOPE。
    """

    if user["role"] == "store_manager" and order["store_id"] != user["scope_value"]:  # 店长只能看本店
        write_audit(conn, actor=user, action="order.view", resource="order", resource_id=order["order_id"],
                    decision="deny", error_code=ErrorCode.FORBIDDEN_SCOPE.value, detail={"order_store": order["store_id"]})  # 越域留痕
        conn.commit()  # 先提交审计再抛错，避免调用方事务回滚
        raise AppError(ErrorCode.FORBIDDEN_SCOPE, "该单据不属于你的门店", status=403)
    require_scope(conn, user, order["store_id"])  # 其余角色按数据域


@router.get("/options")
def options(user: dict = Depends(get_current_user)) -> dict:
    """下拉选项。

    入参：user。
    出参：统一响应，含门店、商品、日期与兜底样例。
    失败时的行为：无可见门店时返回空列表。
    """

    require_action(user, "order.view")  # 动作权限
    with get_conn() as conn:  # 独立连接
        stores = allowed_store_ids(conn, user)  # 数据域
        return ok(service.options(conn, stores))  # 返回选项


@router.get("/diagnose")
def diagnose(store_id: str = Query(min_length=1), days: int = Query(default=7, ge=1, le=14),
            user: dict = Depends(get_current_user)) -> dict:
    """缺货三因诊断。

    入参：store_id；days 窗口；user。
    出参：统一响应，含三类计数与摘要。
    失败时的行为：越域 403。
    """

    require_action(user, "dashboard.view")  # 动作权限
    with get_conn() as conn:  # 独立连接
        require_scope(conn, user, store_id)  # 数据域
        data = diagnose_agent.run(conn, store_id, days)  # 诊断
    return ok(data)  # 统一成功


@router.post("/suggest")
def suggest(payload: SuggestIn, user: dict = Depends(get_current_user)):
    """生成补货建议。

    入参：payload 门店/商品/可选日期；user 计划员。
    出参：未命中兜底时 200 ok；命中兜底时 200 ok=false，data.suggestion 为 null。
    失败时的行为：越域 403；资料缺失 404/422。
    """

    require_action(user, "suggestion.generate")  # 只有计划员可生成
    with get_conn() as conn:  # 独立连接
        require_scope(conn, user, payload.store_id)  # 数据域
        data = service.suggest_order(conn, user, payload.store_id, payload.sku_id, payload.biz_date)  # 主链路
    if data.get("stopped"):  # 兜底：HTTP 200 + ok=false，data 保留停止详情
        return JSONResponse(
            status_code=200,
            content=fail(ErrorCode.GUARDRAIL_STOPPED, data["reason"], data),
        )
    return ok(data)  # 正常建议


@router.get("/orders")
def list_orders(
    status: str | None = Query(default=None),
    user: dict = Depends(get_current_user),
) -> dict:
    """单据列表。

    入参：status 可选状态；user。
    出参：统一响应，含单据数组（按数据域裁剪）。
    失败时的行为：无可见门店返回空数组。
    """

    require_action(user, "order.view")  # 动作权限
    with get_conn() as conn:  # 独立连接
        stores = allowed_store_ids(conn, user)  # 数据域
        return ok({"items": service.list_orders(conn, stores, status), "stores": stores})  # 返回列表


@router.get("/orders/{order_id}")
def get_order(order_id: str, user: dict = Depends(get_current_user)) -> dict:
    """单据详情。

    入参：order_id；user。
    出参：统一响应，含单据。
    失败时的行为：不存在 404；越域 403。
    """

    require_action(user, "order.view")  # 动作权限
    with get_conn() as conn:  # 独立连接
        order = service.get_order(conn, order_id)  # 取单据
        _check_order_scope(conn, user, order)  # 数据域
    return ok({"order": order})  # 统一成功


@router.post("/orders/{order_id}/submit")
def submit(order_id: str, user: dict = Depends(get_current_user)) -> dict:
    """提交待签核。

    入参：order_id；user 计划员。
    出参：统一响应，含更新后单据。
    失败时的行为：状态不符 422；越域 403。
    """

    require_action(user, "order.submit")  # 动作权限
    with get_conn() as conn:  # 独立连接
        order = service.get_order(conn, order_id)  # 取单据
        _check_order_scope(conn, user, order)  # 数据域
        updated = service.submit_order(conn, user, order_id)  # 推进状态
    return ok({"order": updated})  # 统一成功


@router.patch("/orders/{order_id}")
def update(order_id: str, payload: UpdateIn, user: dict = Depends(get_current_user)) -> dict:
    """修改建议量（仅草稿）。

    入参：order_id；payload 新数量；user。
    出参：统一响应，含更新后单据。
    失败时的行为：非草稿 422；越域 403。
    """

    require_action(user, "order.update")  # 动作权限
    with get_conn() as conn:  # 独立连接
        order = service.get_order(conn, order_id)  # 取单据
        _check_order_scope(conn, user, order)  # 数据域
        updated = service.update_qty(conn, user, order_id, payload.final_qty)  # 更新
    return ok({"order": updated})  # 统一成功


@router.post("/orders/{order_id}/sign")
def sign(order_id: str, user: dict = Depends(get_current_user)) -> dict:
    """签核单据。

    入参：order_id；user 运营总监。
    出参：统一响应，含签核后单据。
    失败时的行为：admin 尝试签核 403 FORBIDDEN_ACTION；已签 409；自签 403；状态不符 422。
    """

    require_action(user, "order.sign")  # admin 会在这里被拒并留痕
    with get_conn() as conn:  # 独立连接
        order = service.get_order(conn, order_id)  # 取单据
        _check_order_scope(conn, user, order)  # 数据域
        updated = service.sign_order(conn, user, order_id)  # 签核（服务内写审计）
    return ok({"order": updated})  # 统一成功


@router.post("/orders/{order_id}/reject")
def reject(order_id: str, payload: RejectIn, user: dict = Depends(get_current_user)) -> dict:
    """驳回单据。

    入参：order_id；payload 原因；user。
    出参：统一响应，含驳回后单据。
    失败时的行为：缺原因 422；已签 409；越域 403。
    """

    require_action(user, "order.reject")  # 动作权限
    with get_conn() as conn:  # 独立连接
        order = service.get_order(conn, order_id)  # 取单据
        _check_order_scope(conn, user, order)  # 数据域
        updated = service.reject_order(conn, user, order_id, payload.reason)  # 驳回
    return ok({"order": updated})  # 统一成功


@router.get("/orders/{order_id}/rebuild")
def rebuild(order_id: str, user: dict = Depends(get_current_user)) -> dict:
    """重建单据处置现场。

    入参：order_id；user。
    出参：统一响应，含快照、版本、兜底、事件、审计与时间线。
    失败时的行为：不存在 404；越域 403。
    """

    require_action(user, "order.rebuild")  # 动作权限
    with get_conn() as conn:  # 独立连接
        order = service.get_order(conn, order_id)  # 取单据
        _check_order_scope(conn, user, order)  # 数据域
        data = service.rebuild(conn, order_id)  # 重建
        write_audit(conn, actor=user, action="order.rebuild", resource="order", resource_id=order_id,
                    decision="allow", detail={"status": order["status"]})  # 敏感读取留痕
    return ok(data)  # 统一成功
