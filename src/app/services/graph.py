"""知识图谱存储：SQLite 两表 + 递归 CTE。

GraphStore 是抽象边界：将来换图库只改实现，不动调用方。
「建议 → 依据 → 责任人」三跳查询是知识图谱存在的唯一理由（第 8 节）。
"""

from __future__ import annotations  # 延迟注解

import json  # props 解析
from typing import Any  # 结构

from . import retrieval  # 混合检索


def _parse_props(raw: Any) -> dict:
    """解析 props JSON。

    入参：raw 字符串或字典。
    出参：字典。
    失败时的行为：解析失败返回空字典（不阻断查询）。
    """

    if isinstance(raw, dict):  # 已经是字典
        return raw
    try:
        return json.loads(raw or "{}")  # 尝试解析
    except json.JSONDecodeError:  # 脏数据
        return {}


class GraphStore:
    """图谱读写实现（SQLite）。"""

    def __init__(self, conn) -> None:
        """初始化。

        入参：conn 数据库连接。
        出参：实例。
        失败时的行为：不抛异常。
        """

        self.conn = conn  # 保存连接

    def get_node(self, node_id: str) -> dict | None:
        """读取单个节点。

        入参：node_id 节点 ID，如 rule:f44。
        出参：节点字典（props 已解析）或 None。
        失败时的行为：SQL 异常向上抛出。
        """

        row = self.conn.execute("SELECT * FROM graph_nodes WHERE id = ?", (node_id,)).fetchone()  # 主键查询
        if not row:  # 不存在
            return None
        node = dict(row)  # 转字典
        node["props"] = _parse_props(node.get("props"))  # 解析属性
        return node

    def neighbors(self, node_id: str, rel: str | None = None, direction: str = "out") -> list[dict]:
        """查询一跳邻居。

        入参：node_id；rel 可选关系过滤；direction out|in。
        出参：[{rel, direction, node}]。
        失败时的行为：SQL 异常向上抛出。
        """

        if direction == "in":  # 反向：谁指向我
            sql = "SELECT src AS other, rel FROM graph_edges WHERE dst = ?"
            params: list[Any] = [node_id]
        else:  # 正向
            sql = "SELECT dst AS other, rel FROM graph_edges WHERE src = ?"
            params = [node_id]
        if rel:  # 关系过滤
            sql += " AND rel = ?"
            params.append(rel)
        rows = self.conn.execute(sql, params).fetchall()  # 执行
        results: list[dict] = []  # 结果
        for row in rows:  # 补节点信息
            node = self.get_node(row["other"])  # 邻居节点
            if node:  # 只返回存在的节点
                results.append({"rel": row["rel"], "direction": direction, "node": node})
        return results

    def path(self, src: str, depth: int = 2, rels: list[str] | None = None, direction: str = "out") -> dict:
        """多跳路径查询（递归 CTE，一条 SQL）。

        入参：src 起点；depth 最大跳数；rels 允许的关系序列（None 表示不限）；direction out|in。
        出参：{src, depth, hops:[...], paths:[[...]]}。
        失败时的行为：SQL 异常向上抛出；起点不存在时 hops 为空。
        """

        depth = max(1, min(int(depth or 2), 3))  # 最多 3 跳，避免图循环爆炸
        rel_filter = ""  # 关系过滤片段
        rel_params: list[Any] = []
        if rels:  # 指定关系（例如 EVIDENCED_BY,OWNED_BY）
            placeholders = ",".join("?" for _ in rels)  # 占位符
            rel_filter = f" AND e.rel IN ({placeholders})"  # 过滤条件
            rel_params = list(rels)  # 参数
        if direction == "in":  # 反向查询
            join_cond = "e.dst = w.node_id"  # 从终点回溯
            next_col = "e.src"  # 下一跳取 src
        else:  # 正向查询
            join_cond = "e.src = w.node_id"  # 从起点前进
            next_col = "e.dst"  # 下一跳取 dst
        sql = f"""
        WITH RECURSIVE walk(node_id, next_id, rel, depth, path) AS (
          SELECT e.src, e.dst, e.rel, 1, e.src || '>' || e.dst
          FROM graph_edges e
          WHERE {('e.dst' if direction == 'in' else 'e.src')} = ?{rel_filter}
          UNION ALL
          SELECT w.next_id, {next_col}, e.rel, w.depth + 1, w.path || '>' || {next_col}
          FROM graph_edges e JOIN walk w ON {join_cond}
          WHERE w.depth < ?{rel_filter}
        )
        SELECT node_id, next_id, rel, depth, path FROM walk ORDER BY depth, rel
        """
        params = [src, *rel_params, depth, *rel_params]  # 两段 CTE 各需一次参数
        rows = self.conn.execute(sql, params).fetchall()  # 执行递归查询
        hops: list[dict] = []  # 跳列表
        paths: list[list[str]] = []  # 路径集合
        seen_paths: set[str] = set()  # 去重
        for row in rows:  # 逐跳补节点
            src_node = self.get_node(row["node_id"])  # 起点节点
            dst_node = self.get_node(row["next_id"])  # 终点节点
            if not src_node or not dst_node:  # 跳过悬空边
                continue
            hops.append({
                "depth": row["depth"], "rel": row["rel"],
                "src": src_node["id"], "src_name": src_node["name"], "src_kind": src_node["kind"],
                "dst": dst_node["id"], "dst_name": dst_node["name"], "dst_kind": dst_node["kind"],
                "dst_props": dst_node.get("props", {}),
            })
            path_key = row["path"]  # 路径串
            if path_key not in seen_paths:  # 去重
                seen_paths.add(path_key)
                paths.append(path_key.split(">"))  # 转为节点数组
        return {"src": src, "depth": depth, "direction": direction, "hops": hops, "paths": paths}

    def search(self, query: str, top_k: int | None = None) -> dict:
        """混合检索节点。

        入参：query 关键词；top_k 条数。
        出参：retrieval.search 的结果结构。
        失败时的行为：不抛异常（检索降级由 retrieval 处理）。
        """

        return retrieval.search(self.conn, query, top_k)

    def metric_signature(self, metric_code: str) -> dict:
        """查询指标签发状态。

        入参：metric_code 指标编码。
        出参：{definition, signed, signed_by, edges}。
        失败时的行为：SQL 异常向上抛出。
        """

        node_id = f"metric:{metric_code}"  # 约定节点 ID
        definition = self.get_node(node_id)  # 口径节点
        edges = self.neighbors(node_id, direction="out")  # 所有出边
        signed_edges = [e for e in edges if e["rel"] == "SIGNED_BY"]  # 只看签发边
        return {
            "definition": definition,  # 节点本身
            "signed": bool(signed_edges),  # 是否有签发关系
            "signed_by": [e["node"] for e in signed_edges],  # 签发人节点
            "edges": edges,  # 全部出边，便于前端画图
        }
