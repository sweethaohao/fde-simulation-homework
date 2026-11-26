"""向量检索 + FTS5 降级。

nomic-embed-text 可用时做 768 维余弦相似度；不可用时降级为 FTS5（中文按二元组切分），
不报错、不阻断主链路（第 1 节检索约束 + 第 5 节 fallback 配置）。
"""

from __future__ import annotations  # 延迟注解

import json  # 向量与 props
import math  # 余弦相似度
import re  # 中文切分

from ..config import settings  # top_k 等参数
from ..llm import ollama  # 嵌入调用

CJK_RE = re.compile(r"[\u4e00-\u9fff]+")  # 连续中文片段
WORD_RE = re.compile(r"[a-zA-Z0-9_\-\.]+")  # 英文/编号片段


def cjk_tokens(text: str) -> list[str]:
    """把文本切成可检索 token。

    入参：text 原始文本。
    出参：token 列表（中文二元组 + 英文/编号词，均小写）。
    失败时的行为：不抛异常。
    """

    tokens: list[str] = []  # 结果集合
    for run in CJK_RE.findall(text or ""):  # 只处理中文片段
        if len(run) == 1:  # 单字直接入列
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))  # 二元组覆盖常见短语
    tokens.extend(w.lower() for w in WORD_RE.findall(text or ""))  # 英文词与 f 编号
    return list(dict.fromkeys(tokens))  # 去重且保持顺序


def index_text(node: dict) -> str:
    """构造节点索引文本。

    入参：node 含 id/name/props 的字典。
    出参：空格分隔的 token 串（供 FTS5 入库）。
    失败时的行为：props 非 JSON 时按字符串处理。
    """

    props = node.get("props") or {}  # 可能是字符串
    if isinstance(props, str):  # 兼容直接传 JSON 字符串
        try:
            props = json.loads(props)  # 尝试解析
        except json.JSONDecodeError:  # 解析失败就原样使用
            props = {"raw": props}
    raw = " ".join(str(v) for v in props.values() if isinstance(v, (str, int, float)))  # 抽取可检索字段
    text = f"{node.get('id', '')} {node.get('name', '')} {raw}"  # 拼接
    return " ".join(cjk_tokens(text))  # 转 token 串


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。

    入参：a、b 等长向量。
    出参：[-1,1] 浮点。
    失败时的行为：维度不一致返回 0（不抛异常，避免影响检索）。
    """

    if not a or not b or len(a) != len(b):  # 防御性检查
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))  # 点积
    na = math.sqrt(sum(x * x for x in a))  # 模长
    nb = math.sqrt(sum(y * y for y in b))  # 模长
    return dot / (na * nb) if na and nb else 0.0  # 避免除零


def _fts_search(conn, query: str, top_k: int) -> list[dict]:
    """FTS5 关键词检索。

    入参：conn；query 查询串；top_k 条数。
    出参：[{node_id, kind, name, score, source}]。
    失败时的行为：FTS 语法异常时返回空列表（降级不报错）。
    """

    tokens = cjk_tokens(query)[:16]  # 限制 token 数，避免超长 MATCH
    if not tokens:  # 空查询
        return []
    match = " OR ".join('"' + t.replace('"', '') + '"' for t in tokens)  # 引号包裹，降低语法风险
    try:
        rows = conn.execute(
            """
            SELECT node_id, bm25(graph_fts) AS score
            FROM graph_fts WHERE graph_fts MATCH ? ORDER BY score LIMIT ?
            """,
            (match, top_k),
        ).fetchall()  # bm25 分数越小越相关
    except Exception:  # FTS 语法/表缺失时静默降级
        return []
    results: list[dict] = []  # 结果集
    for row in rows:  # 逐行补充节点信息
        node = conn.execute("SELECT id, kind, name, props FROM graph_nodes WHERE id = ?", (row["node_id"],)).fetchone()  # 取节点
        if not node:  # 索引残留
            continue
        results.append({
            "node_id": node["id"], "kind": node["kind"], "name": node["name"],
            "score": round(1.0 / (1.0 + abs(float(row["score"] or 0))), 4),  # 归一化为 0–1
            "source": "fts5",  # 来源标记
        })
    return results


def _vector_search(conn, query: str, top_k: int) -> tuple[list[dict], str]:
    """向量检索。

    入参：conn；query；top_k。
    出参：([结果], 状态) 状态为 `vector` 或 `fts5`。
    失败时的行为：嵌入不可用时返回空列表和 fts5 状态（不抛异常）。
    """

    try:
        qvec = ollama.embed([query])[0]  # 查询向量
    except ollama.LlmUnavailable:  # 模型未就绪 → 降级
        return [], settings.retrieval.fallback
    rows = conn.execute("SELECT node_id, vector FROM graph_embeddings").fetchall()  # 全部节点向量（规模小）
    scored: list[tuple[float, str]] = []  # (分数, 节点)
    for row in rows:  # 逐个算相似度
        try:
            vec = json.loads(row["vector"])  # 还原向量
        except json.JSONDecodeError:  # 脏数据跳过
            continue
        scored.append((_cosine(qvec, vec), row["node_id"]))  # 记录分数
    scored.sort(reverse=True)  # 高分在前
    results: list[dict] = []  # 结果集
    for score, node_id in scored[:top_k]:  # 取前 k
        node = conn.execute("SELECT id, kind, name FROM graph_nodes WHERE id = ?", (node_id,)).fetchone()  # 补节点
        if node and score > 0:  # 过滤无效
            results.append({"node_id": node["id"], "kind": node["kind"], "name": node["name"],
                            "score": round(score, 4), "source": "vector"})
    return results, "vector"


def search(conn, query: str, top_k: int | None = None) -> dict:
    """混合检索（向量优先，FTS5 合并）。

    入参：conn；query 查询串；top_k 条数（默认配置值）。
    出参：{mode, degraded, items:[...]}。
    失败时的行为：不抛异常；任一通道失败都尽量返回另一通道结果。
    """

    k = top_k or settings.retrieval.top_k  # 返回条数
    vector_items, mode = _vector_search(conn, query, k)  # 向量通道
    fts_items = _fts_search(conn, query, k)  # 关键词通道
    merged: dict[str, dict] = {}  # 按节点去重
    for item in vector_items + fts_items:  # 先向量后关键词
        current = merged.get(item["node_id"])  # 已存在则取更高分
        if current is None or item["score"] > current["score"]:
            merged[item["node_id"]] = item
    items = sorted(merged.values(), key=lambda x: x["score"], reverse=True)[:k]  # 排序截断
    for item in items:  # 补充摘要，供提示词与前端使用
        node = conn.execute("SELECT props FROM graph_nodes WHERE id = ?", (item["node_id"],)).fetchone()  # 取属性
        try:
            props = json.loads(node["props"]) if node else {}  # 解析
        except json.JSONDecodeError:  # 容错
            props = {}
        item["snippet"] = str(props.get("summary") or props.get("note") or item["name"])[:160]  # 摘要
        item["f_code"] = props.get("f_code") or (item["node_id"].split(":")[-1] if item["node_id"].startswith(("rule:", "evidence:", "signal:")) else None)  # f 编号
    return {
        "mode": "hybrid" if mode == "vector" else "fts5",  # 实际模式
        "degraded": mode != "vector",  # 是否降级
        "items": items,  # 结果
    }
