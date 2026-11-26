"""溯源 Agent：三跳图谱查询与检索的薄封装，统一留痕。"""

from __future__ import annotations  # 延迟注解

from . import registry  # 留痕
from ..services.graph import GraphStore  # 图谱实现


def path(conn, src: str, depth: int = 2, rels: list[str] | None = None, direction: str = "out") -> dict:
    """执行路径查询。

    入参：conn；src 起点；depth 跳数；rels 关系过滤；direction 方向。
    出参：GraphStore.path 的结构，附带 src_node。
    失败时的行为：SQL 异常向上抛出。
    """

    store = GraphStore(conn)  # 实例化
    result = store.path(src, depth=depth, rels=rels, direction=direction)  # 递归查询
    result["src_node"] = store.get_node(src)  # 起点详情
    registry.record_run(
        "tool", agent="trace", tool="graph.path",
        input_summary=f"{src} depth={depth} rels={rels}",
        output_summary=f"{len(result['hops'])} 跳", status="ok",
    )  # 留痕
    return result


def search(conn, query: str, top_k: int | None = None) -> dict:
    """执行混合检索。

    入参：conn；query；top_k。
    出参：retrieval.search 结构。
    失败时的行为：不抛异常（检索内部降级）。
    """

    result = GraphStore(conn).search(query, top_k)  # 检索
    registry.record_run(
        "tool", agent="trace", tool="graph.search", input_summary=query,
        output_summary=f"{len(result['items'])} 条 / {result['mode']}",
        status="ok" if not result["degraded"] else "degraded",
    )  # 留痕
    return result


def metric_trace(conn, metric_code: str, depth: int = 3) -> dict:
    """指标口径溯源：口径 → 签发人 → 依据。

    入参：conn；metric_code；depth 跳数。
    出参：{signature, path}。
    失败时的行为：SQL 异常向上抛出。
    """

    store = GraphStore(conn)  # 图谱
    signature = store.metric_signature(metric_code)  # 签发状态
    path = store.path(f"metric:{metric_code}", depth=depth)  # 路径
    registry.record_run(
        "tool", agent="trace", tool="graph.path", input_summary=f"metric:{metric_code}",
        output_summary=f"signed={signature['signed']}", status="ok",
    )  # 留痕
    return {"signature": signature, "path": path}
