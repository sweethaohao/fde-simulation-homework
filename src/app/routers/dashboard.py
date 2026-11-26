"""看板路由：一次返回数据域内的关键数字、兜底命中与就绪度。"""

from __future__ import annotations  # 延迟注解

from fastapi import APIRouter, Depends  # 路由与依赖

from ..db import get_conn  # 数据库
from ..deps import allowed_store_ids, get_current_user, require_action, require_scope, scope_payload  # 权限
from ..errors import ok  # 统一响应
from ..services import metrics as metrics_service  # 指标服务

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])  # 路由前缀


@router.get("/summary")
def summary(user: dict = Depends(get_current_user)) -> dict:
    """看板汇总。

    入参：user 当前用户。
    出参：统一响应，data 含缺货两种分母、账实差异、兜底命中、单据状态、就绪度。
    失败时的行为：越域由 require_scope 抛 403；SQL 异常由全局处理器兜底。
    """

    require_action(user, "dashboard.view")  # 第三层：动作
    with get_conn() as conn:  # 独立连接
        stores = allowed_store_ids(conn, user)  # 第一层：数据域
        require_scope(conn, user, None)  # 第二层：至少有一个门店可见
        data = metrics_service.dashboard_summary(conn, stores)  # 指标聚合
        data["scope"] = scope_payload(conn, user)  # 前端展示数据域
        data["banner"] = "本系统全部数据为合成数据，仅用于演示；不接真实钱款、订单与个人信息"  # 常驻横幅
        return ok(data)  # 统一成功
