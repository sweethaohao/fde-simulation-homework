"""审计写入、字段白名单过滤与导出。

R4：写入前过白名单，password/token/secret/phone/id_card 一律丢弃。
R6：留存 90 天，到期归档需 loss_manager 确认，不自动续期。
"""

from __future__ import annotations  # 延迟注解

import csv  # 导出 CSV
import io  # 内存缓冲
import json  # detail 序列化
import uuid  # 生成 request_id
from contextvars import ContextVar  # 请求级上下文
from typing import Any  # 任意结构

from ..config import settings  # 读取白名单配置
from ..db import now_iso, row_to_dict, rows_to_dicts  # 时间与行转换

# 请求级 request_id：中间件写入，审计/日志读取，避免层层传参
_request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def new_request_id() -> str:
    """生成 request_id。

    入参：无。
    出参：短 UUID 字符串。
    失败时的行为：不抛异常。
    """

    return uuid.uuid4().hex[:16]  # 16 位足够单机演示追踪


def set_request_id(value: str) -> None:
    """设置当前请求 ID。

    入参：value 请求 ID。
    出参：无。
    失败时的行为：不抛异常。
    """

    _request_id_var.set(value)  # 写入上下文


def get_request_id() -> str:
    """读取当前请求 ID。

    入参：无。
    出参：请求 ID，未设置时返回空串。
    失败时的行为：不抛异常。
    """

    return _request_id_var.get()  # 读取上下文


def _is_sensitive(key: str) -> bool:
    """判断字段名是否命中敏感词。

    入参：key 字段名。
    出参：True 表示必须丢弃或打码。
    失败时的行为：不抛异常。
    """

    lowered = key.lower()  # 统一小写比较
    return any(word in lowered for word in settings.audit.redact_fields)  # 子串命中即敏感


def redact(value: Any) -> Any:
    """递归过滤敏感字段。

    入参：value 任意 JSON 兼容结构（dict/list/标量）。
    出参：过滤后的新结构，敏感字段值替换为 `***`。
    失败时的行为：不抛异常；非 JSON 对象转为字符串。
    """

    if isinstance(value, dict):  # 字典逐键处理
        cleaned: dict[str, Any] = {}  # 新字典，避免修改调用方数据
        for key, item in value.items():  # 遍历所有键
            if _is_sensitive(str(key)):  # 命中白名单外的敏感键
                cleaned[key] = "***"  # 只保留占位，不落原值
            else:
                cleaned[key] = redact(item)  # 递归处理嵌套结构
        return cleaned  # 返回脱敏结果
    if isinstance(value, list):  # 列表逐项处理
        return [redact(item) for item in value]  # 保留顺序
    if isinstance(value, (str, int, float, bool)) or value is None:  # 基础标量原样返回
        return value  # JSON 可直接序列化
    return str(value)  # 其余类型降级为字符串，避免序列化失败


def write_audit(
    conn,
    *,
    actor: dict | None,
    action: str,
    resource: str = "",
    resource_id: str = "",
    decision: str = "allow",
    error_code: str | None = None,
    detail: dict | None = None,
    scope: str | None = None,
    ip: str | None = None,
) -> int:
    """写一条审计日志。

    入参：conn 连接；actor 当前用户（可为空）；action 动作；resource/resource_id 资源；
          decision allow|deny|stop|sign|reject 等；error_code 错误码；detail 附加详情；
          scope 数据域；ip 来源地址。
    出参：审计自增 ID。
    失败时的行为：数据库异常向上抛出，由全局处理器兜底（审计失败不可静默）。
    """

    safe_detail = redact(detail or {})  # R4：先脱敏再落库
    cur = conn.execute(
        """
        INSERT INTO audit_logs
          (request_id, actor_id, actor_name, role, action, resource, resource_id,
           decision, error_code, scope, detail, ip, created_at, is_synthetic)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
        """,
        (
            get_request_id(),  # 请求链路 ID
            actor.get("user_id") if actor else None,  # 操作人 ID
            actor.get("display_name") if actor else "anonymous",  # 操作人显示名
            actor.get("role") if actor else None,  # 角色
            action,  # 动作
            resource,  # 资源类型
            resource_id,  # 资源 ID
            decision,  # 判定结果
            error_code,  # 错误码（如有）
            scope,  # 数据域
            json.dumps(safe_detail, ensure_ascii=False),  # 脱敏后的详情
            ip,  # 来源 IP
            now_iso(),  # 时间
        ),
    )
    return int(cur.lastrowid)  # 返回审计 ID


def list_audit_logs(
    conn,
    *,
    limit: int = 100,
    actor: str | None = None,
    action: str | None = None,
    decision: str | None = None,
    resource_id: str | None = None,
) -> list[dict]:
    """查询审计日志。

    入参：conn；limit 条数；actor 用户名；action 动作；decision 判定；resource_id 资源。
    出参：日志字典列表（新到旧）。
    失败时的行为：SQL 异常向上抛出。
    """

    clauses: list[str] = []  # WHERE 片段
    params: list[Any] = []  # 对应参数
    if actor:  # 按操作人过滤
        clauses.append("actor_name = ?")  # 用户名精确匹配
        params.append(actor)  # 参数入列
    if action:  # 按动作过滤
        clauses.append("action = ?")  # 动作精确匹配
        params.append(action)  # 参数入列
    if decision:  # 按判定过滤
        clauses.append("decision = ?")  # 判定精确匹配
        params.append(decision)  # 参数入列
    if resource_id:  # 按资源过滤
        clauses.append("resource_id = ?")  # 资源精确匹配
        params.append(resource_id)  # 参数入列
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""  # 组装条件
    sql = f"SELECT * FROM audit_logs{where} ORDER BY audit_id DESC LIMIT ?"  # 只取最近的记录
    rows = conn.execute(sql, (*params, max(1, min(limit, 1000)))).fetchall()  # 限制最大 1000 条
    return rows_to_dicts(rows)  # 转字典


def get_retention_status(conn) -> dict:
    """计算审计留存状态。

    入参：conn 连接。
    出参：留存天数、最早记录、剩余天数、归档要求。
    失败时的行为：SQL 异常向上抛出。
    """

    days = settings.audit.retention_days  # 90 天来自配置
    first = conn.execute("SELECT MIN(created_at) AS oldest, COUNT(*) AS total FROM audit_logs").fetchone()  # 最早时间与总数
    oldest = first["oldest"] if first else None  # 可能还没有日志
    remaining = days  # 没有日志时按满额展示
    if oldest:  # 有日志才计算剩余
        from datetime import datetime  # 局部导入避免顶部拥挤
        from ..db import TZ  # 业务时区

        created = datetime.fromisoformat(oldest)  # 解析 ISO 时间
        elapsed_days = (datetime.now(TZ) - created).days  # 已留存天数
        remaining = max(0, days - elapsed_days)  # 剩余天数
    return {
        "retention_days": days,  # 配置的留存上限
        "oldest_log_at": oldest,  # 最早一条日志时间
        "total": int(first["total"]) if first else 0,  # 日志总数
        "remaining_days": remaining,  # 距离到期天数
        "archive_requires": "loss_manager",  # R6：到期归档需商品与损耗经理确认
        "auto_renew": False,  # 不自动续期
    }


def export_audit_csv(conn, limit: int = 1000) -> str:
    """导出审计 CSV。

    入参：conn；limit 最多导出条数。
    出参：CSV 字符串（含表头）。
    失败时的行为：SQL 异常向上抛出。
    """

    rows = conn.execute("SELECT * FROM audit_logs ORDER BY audit_id DESC LIMIT ?", (limit,)).fetchall()  # 倒序导出
    buffer = io.StringIO()  # 内存缓冲，避免落临时文件
    fieldnames = [  # 固定列顺序，便于复核
        "audit_id", "created_at", "request_id", "actor_name", "role", "action",
        "resource", "resource_id", "decision", "error_code", "scope", "ip", "detail",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")  # 忽略额外列
    writer.writeheader()  # 写表头
    for row in rows:  # 逐行写入
        writer.writerow({k: row[k] for k in fieldnames})  # 只导出固定列
    return buffer.getvalue()  # 返回 CSV 文本


def get_audit_by_resource(conn, resource_id: str) -> list[dict]:
    """按资源查审计轨迹。

    入参：conn；resource_id 资源 ID（例如单据号）。
    出参：该资源相关日志列表。
    失败时的行为：SQL 异常向上抛出。
    """

    rows = conn.execute(
        "SELECT * FROM audit_logs WHERE resource_id = ? ORDER BY audit_id ASC", (resource_id,)
    ).fetchall()  # 按时间正序，便于重建
    return rows_to_dicts(rows)  # 转字典


__all__ = [  # 明确对外接口
    "new_request_id", "set_request_id", "get_request_id", "redact", "write_audit",
    "list_audit_logs", "get_retention_status", "export_audit_csv", "get_audit_by_resource",
    "row_to_dict",
]
