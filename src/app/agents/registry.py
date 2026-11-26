"""Agent 与工具注册表 + 调用留痕（Harness 落点）。

Harness 在本项目里只是「编排治理层」：注册表、留痕、健康就绪，不引入任何 SDK 依赖。
"""

from __future__ import annotations  # 延迟注解

import json  # detail 序列化
from typing import Any  # 结构

from ..db import get_conn, now_iso  # 留痕入库
from ..services.audit import get_request_id  # 请求链路

AGENTS: list[dict[str, Any]] = [
    {
        "name": "diagnose", "label": "诊断 Agent", "file": "agents/diagnose.py",
        "purpose": "把缺货拆成「后仓有货未上架 / 真正无货 / 已签收未上架」三类",
        "tools": ["metrics.query", "inventory.snapshot", "graph.search"],
        "roles": ["ops_director", "loss_manager", "planner", "store_manager", "logistics", "data_engineer", "admin"],
        "llm": "optional",  # 统计可先确定性算出，LLM 只做摘要
    },
    {
        "name": "suggest", "label": "建议 Agent", "file": "agents/suggest.py",
        "purpose": "在兜底之后给出建议量、依据与置信说明；模型不可用时规则兜底并标注 degraded",
        "tools": ["graph.search", "inventory.snapshot", "promo.version", "replenishment.persist"],
        "roles": ["planner"],
        "llm": "required-optional",  # 需要 LLM 增强，但允许降级
    },
    {
        "name": "trace", "label": "溯源 Agent", "file": "agents/trace.py",
        "purpose": "三跳图谱查询：建议 → 规则/证据 → 责任人",
        "tools": ["graph.path", "graph.search"],
        "roles": ["admin", "ops_director", "loss_manager", "planner", "store_manager", "logistics", "data_engineer"],
        "llm": "none",  # 纯确定性查询，不调模型
    },
    {
        "name": "guardrail", "label": "兜底 Agent", "file": "agents/guardrail.py",
        "purpose": "f44 五类硬拦截：命中即停，不返回建议量",
        "tools": ["inventory.snapshot", "promo.version", "metrics.query", "audit.write"],
        "roles": ["planner"],
        "llm": "none",  # 必须是确定性代码，禁止模型判定
    },
]

TOOLS: list[dict[str, Any]] = [
    {"name": "metrics.query", "label": "指标查询", "kind": "read", "scope": "按数据域",
     "capabilities": ["metrics.view"], "note": "未签发口径不出数（R10）"},
    {"name": "graph.path", "label": "图谱路径", "kind": "read", "scope": "全局只读",
     "capabilities": ["graph.view"], "note": "递归 CTE，最多 3 跳"},
    {"name": "graph.search", "label": "混合检索", "kind": "read", "scope": "全局只读",
     "capabilities": ["graph.view"], "note": "向量 + FTS5，可降级"},
    {"name": "inventory.snapshot", "label": "库存快照", "kind": "read", "scope": "按数据域",
     "capabilities": ["dashboard.view"], "note": "只读快照，不回写"},
    {"name": "promo.version", "label": "促销版本", "kind": "read", "scope": "全局只读",
     "capabilities": ["graph.view"], "note": "以下发版本为准"},
    {"name": "replenishment.persist", "label": "写入待签核单据", "kind": "write", "scope": "本区域/本店",
     "capabilities": ["suggestion.generate"], "note": "只写 PENDING_SIGN，不存在下单路径（R1）"},
    {"name": "audit.write", "label": "审计写入", "kind": "write", "scope": "全局",
     "capabilities": ["audit.view"], "note": "写前过敏感字段白名单（R4）"},
]


def registry_payload() -> dict:
    """返回注册表快照。

    入参：无。
    出参：{agents, tools, harness:{registry, tracing, health}}。
    失败时的行为：不抛异常。
    """

    return {
        "agents": AGENTS,  # 四个 Agent
        "tools": TOOLS,  # 七个工具
        "harness": {
            "registry": "src/app/agents/registry.py",  # 注册表落点
            "tracing": "agent_runs 表：输入摘要、耗时、token、状态",  # 留痕落点
            "health": "/api/health 与 /api/ready",  # 就绪落点
            "sdk_imported": False,  # 未 import 任何 harness 包
        },
    }


def record_run(
    kind: str,
    *,
    agent: str | None = None,
    tool: str | None = None,
    input_summary: str = "",
    output_summary: str = "",
    duration_ms: int = 0,
    tokens: int = 0,
    status: str = "ok",
    detail: dict | None = None,
) -> None:
    """记录一次 Agent/工具调用。

    入参：kind(llm|tool)；agent/tool 名称；输入输出摘要；耗时；token；状态；详情。
    出参：无。
    失败时的行为：落库异常向上抛出（留痕失败不应静默，但调用方可选择忽略）。
    """

    with get_conn() as conn:  # 独立连接，避免与业务事务耦合
        conn.execute(
            """
            INSERT INTO agent_runs(request_id, kind, agent, tool, input_summary, output_summary,
              duration_ms, tokens, status, detail, created_at, is_synthetic)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
            """,
            (
                get_request_id(), kind, agent, tool,
                input_summary[:300], output_summary[:300], duration_ms, tokens, status,
                json.dumps(detail or {}, ensure_ascii=False)[:2000], now_iso(),
            ),
        )
