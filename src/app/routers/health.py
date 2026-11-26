"""健康与就绪：进程存活 + 数据库 + 本地模型。"""

from __future__ import annotations  # 延迟注解

import time  # 运行时长

from fastapi import APIRouter  # 路由
from fastapi.responses import JSONResponse  # 503 需要自定义状态码

from ..config import settings  # 应用信息
from ..db import get_conn  # 数据库探测
from ..errors import ErrorCode, fail, ok  # 统一响应
from ..llm import ollama  # 模型探测

router = APIRouter(prefix="/api", tags=["health"])  # 路由前缀
STARTED_AT = time.time()  # 进程启动时间


@router.get("/health")
def health() -> dict:
    """存活检查。

    入参：无。
    出参：统一响应，data 含状态、运行时长与合成数据标识。
    失败时的行为：不抛异常。
    """

    return ok({
        "status": "ok", "app": settings.app.name, "env": settings.app.env,
        "uptime_seconds": int(time.time() - STARTED_AT), "synthetic": True,
    })


@router.get("/ready")
def ready():
    """就绪检查：数据库与本地模型。

    入参：无。
    出参：全部就绪时 200；任一失败时 503。
    失败时的行为：不抛异常，返回 503 与失败项。
    """

    checks: dict[str, dict] = {}  # 各项探测结果
    db_ok = False  # 数据库状态
    try:
        with get_conn() as conn:  # 连接并执行最小查询
            conn.execute("SELECT 1").fetchone()  # 探活
        db_ok = True  # 成功
        checks["db"] = {"ok": True, "detail": "sqlite ok"}  # 记录
    except Exception as exc:  # 数据库异常
        checks["db"] = {"ok": False, "detail": str(exc)[:200]}  # 记录原因
    llm = ollama.health()  # 模型探测
    checks["llm"] = {"ok": llm["ok"], "detail": llm["detail"], "models": llm["models"]}  # 记录
    all_ok = db_ok and llm["ok"]  # 两者都就绪才算 ready
    payload = {"status": "ready" if all_ok else "not_ready", "checks": checks, "synthetic": True}  # 汇总
    if not all_ok:  # 未就绪返回 503（部署脚本依赖此语义）
        return JSONResponse(status_code=503, content=fail(ErrorCode.DATA_NOT_READY, "依赖未就绪", payload))
    return ok(payload)
