"""FastAPI 应用入口：统一响应、异常兜底、静态页面与路由装配。"""

from __future__ import annotations  # 延迟注解

import logging  # 运行日志（不含敏感数据）
import time  # 运行时长

from fastapi import FastAPI, Request  # 应用与请求
from fastapi.exceptions import RequestValidationError  # 入参校验异常
from fastapi.responses import JSONResponse  # 统一错误响应
from fastapi.staticfiles import StaticFiles  # 单页前端
from starlette.exceptions import HTTPException as StarletteHTTPException  # 路由级 HTTP 异常

from .config import settings  # 配置
from .db import get_conn, init_schema  # 建表
from .errors import AppError, ErrorCode, fail, ok  # 统一响应
from .routers import admin as admin_router  # 管理面
from .routers import audit as audit_router  # 审计
from .routers import auth as auth_router  # 认证
from .routers import dashboard as dashboard_router  # 看板
from .routers import graph as graph_router  # 图谱
from .routers import health as health_router  # 健康
from .routers import metrics as metrics_router  # 指标
from .routers import replenishment as replenishment_router  # 补货
from .security import get_jwt_secret  # 启动即校验密钥
from .services.audit import new_request_id, set_request_id  # 请求链路

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")  # 标准输出日志
logger = logging.getLogger("app")  # 应用日志器


def create_app() -> FastAPI:
    """创建应用实例。

    入参：无（配置来自 settings / 环境变量）。
    出参：FastAPI 实例。
    失败时的行为：缺少 APP_JWT_SECRET 时抛 RuntimeError，进程启动失败；数据库不可建表时抛异常。
    """

    get_jwt_secret()  # R4：没有密钥不允许启动，禁止硬编码兜底
    with get_conn() as conn:  # 启动时确保表结构存在
        init_schema(conn)  # 幂等建表

    app = FastAPI(
        title=settings.app.name, version="0.1.0", docs_url="/api/docs", redoc_url=None,
        openapi_url="/api/openapi.json",  # 文档放在 /api 下，避免与单页冲突
    )
    app.state.started_at = time.time()  # 供健康检查读取

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """请求级上下文中间件。

        入参：request；call_next 下游。
        出参：响应（附加 X-Request-ID）。
        失败时的行为：异常交由全局处理器；中间件不吞异常。
        """

        rid = request.headers.get("X-Request-ID") or new_request_id()  # 支持外部传入
        request.state.request_id = rid  # 挂到请求对象
        set_request_id(rid)  # 写入 contextvar，供审计读取
        started = time.perf_counter()  # 计时
        response = await call_next(request)  # 继续处理
        response.headers["X-Request-ID"] = rid  # 回传链路 ID
        response.headers["X-Response-Time-Ms"] = str(int((time.perf_counter() - started) * 1000))  # 耗时
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        """业务异常 → 统一响应。

        入参：request；exc 业务异常。
        出参：JSONResponse（HTTP 状态取自异常）。
        失败时的行为：不抛异常。
        """

        return JSONResponse(status_code=exc.status, content=fail(exc.code, exc.msg, exc.data))  # 统一失败信封

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        """入参校验异常 → 统一响应（不回显输入值）。

        入参：request；exc 校验异常。
        出参：422 JSONResponse。
        失败时的行为：不抛异常。
        """

        details = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]  # 去掉 input，避免回显口令
        return JSONResponse(status_code=422, content=fail(ErrorCode.VALIDATION_ERROR, "入参校验失败", {"errors": details}))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """路由级 HTTP 异常 → 统一响应。

        入参：request；exc HTTP 异常。
        出参：JSONResponse。
        失败时的行为：不抛异常。
        """

        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.VALIDATION_ERROR  # 404 明确语义
        return JSONResponse(status_code=exc.status_code, content=fail(code, str(exc.detail), None))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """未预期异常 → 500，不泄漏堆栈。

        入参：request；exc 异常。
        出参：500 JSONResponse。
        失败时的行为：不抛异常。
        """

        logger.exception("unhandled error path=%s rid=%s", request.url.path, getattr(request.state, "request_id", ""))  # 服务端留完整堆栈
        return JSONResponse(status_code=500, content=fail(ErrorCode.INTERNAL, "服务内部错误，请稍后重试", None))

    for router in (  # 顺序不影响匹配，前缀各自独立
        auth_router.router, health_router.router, dashboard_router.router, metrics_router.router,
        graph_router.router, replenishment_router.router, admin_router.router, audit_router.router,
    ):
        app.include_router(router)  # 装配

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"])
    def api_not_found(rest: str) -> JSONResponse:
        """未注册的 /api 路径统一返回 JSON 404。

        入参：rest 剩余路径。
        出参：404 统一响应。
        失败时的行为：不抛异常。
        """

        return JSONResponse(status_code=404, content=fail(ErrorCode.NOT_FOUND, f"接口不存在：/api/{rest}", None))

    if settings.web_dir.exists():  # 前端目录存在才挂载，避免开发期启动失败
        app.mount("/", StaticFiles(directory=str(settings.web_dir), html=True), name="web")  # 单页入口

    @app.on_event("startup")
    async def on_startup() -> None:
        """启动日志。

        入参：无。
        出参：无。
        失败时的行为：不抛异常。
        """

        logger.info("app started env=%s db=%s synthetic=true", settings.app.env, settings.db.path)  # 不含任何密钥

    return app  # 返回实例


app = create_app()  # uvicorn src.app.main:app 使用
