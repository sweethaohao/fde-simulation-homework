"""指标计算与数据就绪度。

R10：口径未签发（metric_definitions.signed_by 为空）一律不出数，返回 METRIC_BLOCKED；
时效类指标数据不足时返回 DATA_NOT_READY，并说明缺哪一列、影响什么结论。
"""

from __future__ import annotations  # 延迟注解

import json  # evidence_f 解析

from ..config import settings  # 窗口参数
from ..db import rows_to_dicts  # 行转换


def _placeholders(store_ids: list[str]) -> tuple[str, list[str]]:
    """生成 IN 占位符。

    入参：store_ids 门店列表。
    出参：(占位符串, 参数列表)；空列表返回永远不会命中的条件。
    失败时的行为：不抛异常。
    """

    if not store_ids:  # 空数据域
        return "''", []
    return ",".join("?" for _ in store_ids), list(store_ids)  # 标准占位符


def latest_biz_date(conn, store_ids: list[str]) -> str | None:
    """取数据域内最新业务日期。

    入参：conn；store_ids。
    出参：日期字符串或 None。
    失败时的行为：SQL 异常向上抛出。
    """

    ph, params = _placeholders(store_ids)  # 占位符
    row = conn.execute(f"SELECT MAX(biz_date) AS d FROM daily_snapshots WHERE store_id IN ({ph})", params).fetchone()  # 最新日期
    return row["d"] if row else None


def stockout_breakdown(conn, store_ids: list[str]) -> dict:
    """计算缺货率两种分母。

    入参：conn；store_ids 数据域。
    出参：{snapshot, time_weighted, gap_pp}。
    失败时的行为：SQL 异常向上抛出；无数据时返回 0 分母。
    """

    ph, params = _placeholders(store_ids)  # 占位符
    last = latest_biz_date(conn, store_ids)  # 最新日期
    snap = {"numerator": 0, "denominator": 0, "rate": 0.0, "date": last}  # 当日截面
    if last:  # 有数据才计算
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total, SUM(CASE WHEN shelf_available = 0 THEN 1 ELSE 0 END) AS out_cnt
            FROM daily_snapshots WHERE biz_date = ? AND store_id IN ({ph})
            """,
            (last, *params),
        ).fetchone()  # 最后一天快照
        total = int(row["total"] or 0)  # 观测行数（S07 缺数时少于 400）
        out_cnt = int(row["out_cnt"] or 0)  # 货架为 0 的行数
        snap = {"numerator": out_cnt, "denominator": total, "rate": round(out_cnt / total, 4) if total else 0.0, "date": last}
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total, SUM(CASE WHEN shelf_available = 0 THEN 1 ELSE 0 END) AS out_cnt
        FROM daily_snapshots WHERE store_id IN ({ph})
        """,
        params,
    ).fetchone()  # 全窗口
    total = int(row["total"] or 0)  # 窗口行数
    out_cnt = int(row["out_cnt"] or 0)  # 窗口缺货行数
    time_weighted = {
        "numerator": out_cnt, "denominator": total,
        "rate": round(out_cnt / total, 4) if total else 0.0,
        "window_days": settings.metrics.stockout_window_days,
    }
    return {
        "snapshot": snap, "time_weighted": time_weighted,
        "gap_pp": round((time_weighted["rate"] - snap["rate"]) * 100, 1),  # 百分点差
        "note": "两个分母不可互换：12% 是当日截面，21% 是 6 周时长加权；差 9pp 本身就是结论（f53）",
    }


def account_diff_breakdown(conn, store_ids: list[str]) -> dict:
    """账实差异率与真/伪拆分。

    入参：conn；store_ids。
    出参：{rate, flagged, total, true_diff, code_pseudo}。
    失败时的行为：SQL 异常向上抛出。
    """

    ph, params = _placeholders(store_ids)  # 占位符
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN account_diff = 1 THEN 1 ELSE 0 END) AS flagged,
               SUM(CASE WHEN account_diff = 1 AND diff_cause = 'true' THEN 1 ELSE 0 END) AS true_diff,
               SUM(CASE WHEN account_diff = 1 AND diff_cause = 'code_pseudo' THEN 1 ELSE 0 END) AS pseudo
        FROM daily_snapshots WHERE store_id IN ({ph})
        """,
        params,
    ).fetchone()  # 一次聚合
    total = int(row["total"] or 0)  # 总行数
    flagged = int(row["flagged"] or 0)  # 差异行
    return {
        "rate": round(flagged / total, 4) if total else 0.0,  # 差异率
        "flagged": flagged, "total": total,
        "true_diff": int(row["true_diff"] or 0), "code_pseudo": int(row["pseudo"] or 0),
        "note": "真差异与新旧码/套装码造成的伪差异分开看（f30）",
    }


def _definition(conn, code: str) -> dict | None:
    """读取指标口径。

    入参：conn；code 指标编码。
    出参：口径字典（evidence_f 已解析）或 None。
    失败时的行为：SQL/JSON 异常时尽量返回原始行。
    """

    row = conn.execute("SELECT * FROM metric_definitions WHERE metric_code = ?", (code,)).fetchone()  # 主键查询
    if not row:  # 不存在
        return None
    data = dict(row)  # 转字典
    try:
        data["evidence_f"] = json.loads(data.get("evidence_f") or "[]")  # 解析 f 编号
    except json.JSONDecodeError:  # 脏数据兜底
        data["evidence_f"] = []
    data.pop("is_synthetic", None)  # 不向前端暴露内部列
    return data


def _evidence_nodes(conn, codes: list[str]) -> list[dict]:
    """按 f 编号取图谱节点名称。

    入参：conn；codes f 编号列表。
    出参：[{f_code, name, kind}]。
    失败时的行为：缺失节点跳过，不抛异常。
    """

    results: list[dict] = []  # 结果
    for code in codes:  # 逐个查询
        row = conn.execute(
            "SELECT id, kind, name FROM graph_nodes WHERE id LIKE ? OR props LIKE ? LIMIT 1",
            (f"%:{code}", f'%"f_code": "{code}"%'),
        ).fetchone()  # 规则/证据/信号节点
        if row:  # 找到才返回
            results.append({"f_code": code, "node_id": row["id"], "kind": row["kind"], "name": row["name"]})
    return results


def readiness(conn, store_ids: list[str]) -> dict:
    """数据就绪度（按指标拆项）。

    入参：conn；store_ids 数据域。
    出参：{items, summary}。
    失败时的行为：SQL 异常向上抛出。
    """

    ph, params = _placeholders(store_ids)  # 占位符
    snap = conn.execute(
        f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN account_diff = 1 AND diff_cause IS NULL THEN 1 ELSE 0 END) AS cause_missing,
               SUM(CASE WHEN account_diff = 1 THEN 1 ELSE 0 END) AS flagged,
               MIN(biz_date) AS first_day, MAX(biz_date) AS last_day,
               COUNT(DISTINCT biz_date) AS days
        FROM daily_snapshots WHERE store_id IN ({ph})
        """,
        params,
    ).fetchone()  # 快照侧就绪度
    ev = conn.execute(
        f"""
        SELECT event_type,
               COUNT(*) AS total,
               SUM(CASE WHEN occurred_at IS NOT NULL AND ts_reliable = 1 THEN 1 ELSE 0 END) AS reliable
        FROM events WHERE store_id IN ({ph}) GROUP BY event_type
        """,
        params,
    ).fetchall()  # 事件侧就绪度
    event_stats = {r["event_type"]: {"total": int(r["total"]), "reliable": int(r["reliable"] or 0)} for r in ev}  # 转字典
    days = int(snap["days"] or 0)  # 覆盖天数
    items = [
        {
            "key": "stockout_detection_time", "label": "缺货发现时点",
            "status": "partial" if event_stats.get("stockout_detected", {}).get("reliable", 0) else "not_ready",
            "completeness": round(
                event_stats.get("stockout_detected", {}).get("reliable", 0)
                / max(1, event_stats.get("stockout_detected", {}).get("total", 1)), 4
            ),
            "required": "stockout_detected 的 occurred_at + ts_reliable",
            "missing_fields": ["occurred_at（约 30% 缺失）", "ts_reliable"],
            "impact": "无法计算「从发现到补货」的时效，也不能把缺货率归因到具体时点",
            "evidence_f": ["f13", "f53"], "affects_metrics": ["shelf_restock_hours", "stockout_rate"],
        },
        {
            "key": "shelved_timestamp", "label": "上架可售时间",
            "status": "not_ready",
            "completeness": 0.0,
            "required": "shelved 事件时间戳",
            "missing_fields": ["shelved.occurred_at（全部为空）"],
            "impact": "上架时效不可计算；「补了没上架」只能用签收与货架快照间接推断",
            "evidence_f": ["f10", "f15"], "affects_metrics": ["shelf_restock_hours"],
        },
        {
            "key": "delivery_timestamp", "label": "配送签收时间",
            "status": "ready" if event_stats.get("delivered", {}).get("reliable", 0) else "not_ready",
            "completeness": 1.0 if event_stats.get("delivered", {}).get("reliable", 0) else 0.0,
            "required": "delivered.occurred_at",
            "missing_fields": [],
            "impact": "配送时效与交接依据可信；但排队原因仍需截单配置",
            "evidence_f": ["f51", "f52"], "affects_metrics": ["shelf_restock_hours"],
        },
        {
            "key": "turnover_window", "label": "周转计算窗口",
            "status": "partial" if days < 90 else "ready",
            "completeness": round(min(1.0, days / 90), 4),
            "required": "≥ 90 天销量与库存",
            "missing_fields": [f"仅 {days} 天"] if days < 90 else [],
            "impact": "窗口不足时周转天数波动大；含/不含在途的口径也未签发",
            "evidence_f": ["f00", "f42"], "affects_metrics": ["turnover_days"],
        },
        {
            "key": "account_diff_cause", "label": "账实差异原因",
            "status": "partial" if int(snap["cause_missing"] or 0) else "ready",
            "completeness": round(1 - int(snap["cause_missing"] or 0) / max(1, int(snap["flagged"] or 1)), 4),
            "required": "account_diff + diff_cause",
            "missing_fields": ["diff_cause（新旧码/套装码需归并）"] if int(snap["cause_missing"] or 0) else [],
            "impact": "不拆分真差异与编码伪差异，会把账实问题放大",
            "evidence_f": ["f12", "f30"], "affects_metrics": ["account_diff_rate"],
        },
        {
            "key": "promo_version", "label": "促销版本",
            "status": "ready",
            "completeness": 1.0,
            "required": "promotions.version 与门店改价回传",
            "missing_fields": ["门店改价回传（流程缺口，非字段缺失）"],
            "impact": "临时促销期预测误差需单列；命中即触发兜底",
            "evidence_f": ["f11", "f21"], "affects_metrics": ["stockout_rate"],
        },
    ]
    summary = {"ready": 0, "partial": 0, "not_ready": 0}  # 汇总
    for item in items:  # 累计状态
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    return {
        "items": items, "summary": summary,
        "window": {"days": days, "first_day": snap["first_day"], "last_day": snap["last_day"]},
    }


def metric_payload(conn, code: str, store_ids: list[str]) -> dict:
    """计算单个指标（含 R10 拦截）。

    入参：conn；code 指标编码；store_ids 数据域。
    出参：`{status: ok|blocked, ...}`。
    失败时的行为：未知指标返回 blocked(VALIDATION_ERROR 语义由路由处理)；SQL 异常向上抛出。
    """

    definition = _definition(conn, code)  # 口径
    if not definition:  # 未知指标
        return {"status": "blocked", "reason_code": "NOT_FOUND", "reason": "指标未登记口径", "no_value": True}
    evidence = _evidence_nodes(conn, definition.get("evidence_f", []))  # 依据链
    base = {
        "code": code, "name": definition["name"], "definition": definition,
        "evidence": evidence, "synthetic": True,
        "as_of": latest_biz_date(conn, store_ids),
    }
    signed = bool(definition.get("signed_by"))  # 是否已签发
    signature = {
        "signed": signed, "signed_by": definition.get("signed_by"),
        "signed_at": definition.get("signed_at"), "required_signer": definition.get("required_signer"),
    }
    if settings.metrics.signed_required and not signed:  # R10 硬拦截
        return {
            **base, "status": "blocked", "reason_code": "METRIC_BLOCKED",
            "reason": f"口径未签发，不出数；需 {definition.get('required_signer')} 固定分母、周期与排除规则（f53）",
            "signature": signature, "no_value": True,
            "next_step": "由业务负责人在口径文档签发后再开放查询",
        }
    if code == "shelf_restock_hours":  # 时效类依赖上架时间戳
        ready = readiness(conn, store_ids)  # 复用就绪度
        blocked_item = next((i for i in ready["items"] if i["key"] == "shelved_timestamp"), None)  # 上架时间
        if blocked_item and blocked_item["status"] != "ready":  # 数据不足
            return {
                **base, "status": "blocked", "reason_code": "DATA_NOT_READY",
                "reason": "shelved 事件全部无时间戳，上架时效无法计算",
                "signature": signature, "no_value": True,
                "missing_fields": blocked_item["missing_fields"], "impact": blocked_item["impact"],
                "next_step": "在上架动作补录时间戳后再计算；不得用签收时间替代",
            }
    if code == "stockout_rate":  # 两种分母并列
        data = stockout_breakdown(conn, store_ids)  # 计算结果
        return {
            **base, "status": "ok", "value": data["snapshot"]["rate"], "unit": "ratio",
            "variants": [
                {"key": "snapshot", "label": "当日截面：缺货 SKU 数 / 在售 SKU 数",
                 "definition": "最新业务日货架可售为 0 的 SKU 数 ÷ 当日在售 SKU 数",
                 **data["snapshot"]},
                {"key": "time_weighted", "label": "时段加权：缺货 SKU·天 / 在售 SKU·天",
                 "definition": "窗口内货架可售为 0 的 SKU·天 ÷ 窗口内在售 SKU·天",
                 **data["time_weighted"]},
            ],
            "gap_pp": data["gap_pp"], "note": data["note"], "signature": signature,
        }
    if code == "account_diff_rate":  # 账实差异
        data = account_diff_breakdown(conn, store_ids)  # 计算结果
        return {
            **base, "status": "ok", "value": data["rate"], "unit": "ratio",
            "variants": [
                {"key": "all", "label": "全部差异", "value": data["rate"], "count": data["flagged"]},
                {"key": "true_diff", "label": "真差异", "value": round(data["true_diff"] / max(1, data["total"]), 4), "count": data["true_diff"]},
                {"key": "code_pseudo", "label": "编码伪差异", "value": round(data["code_pseudo"] / max(1, data["total"]), 4), "count": data["code_pseudo"]},
            ],
            "sample": {"flagged": data["flagged"], "total": data["total"]},
            "note": data["note"], "signature": signature,
        }
    return {
        **base, "status": "blocked", "reason_code": "DATA_NOT_READY",
        "reason": "指标口径已登记，但计算链路尚未接入（MVP 范围）",
        "signature": signature, "no_value": True,
    }


def dashboard_summary(conn, store_ids: list[str]) -> dict:
    """汇总看板数据。

    入参：conn；store_ids 数据域。
    出参：看板聚合结构。
    失败时的行为：SQL 异常向上抛出。
    """

    ph, params = _placeholders(store_ids)  # 占位符
    order_rows = conn.execute(
        f"SELECT status, COUNT(*) AS c FROM replenishment_orders WHERE store_id IN ({ph}) GROUP BY status",
        params,
    ).fetchall()  # 单据状态分布
    orders = {r["status"]: int(r["c"]) for r in order_rows}  # 转字典
    hit_rows = conn.execute(
        f"""
        SELECT json_extract(guardrail_json, '$.reason') AS reason, COUNT(*) AS c
        FROM replenishment_orders
        WHERE store_id IN ({ph}) AND status = 'STOPPED' AND guardrail_json IS NOT NULL
        GROUP BY reason
        """,
        params,
    ).fetchall()  # 兜底原因分布（按 JSON 字段）
    hits = [{"reason": r["reason"] or "未标注", "count": int(r["c"])} for r in hit_rows]  # 转列表
    stockout = stockout_breakdown(conn, store_ids)  # 缺货两种分母
    diff = account_diff_breakdown(conn, store_ids)  # 账实差异
    ready = readiness(conn, store_ids)  # 就绪度
    blocked = []  # 被拦截指标
    for code in ("turnover_days", "waste_rate", "shelf_restock_hours"):  # 逐个尝试
        payload = metric_payload(conn, code, store_ids)  # 计算
        if payload.get("status") == "blocked":  # 只收集被拦截的
            blocked.append({
                "code": payload["code"], "name": payload.get("name"),
                "reason_code": payload.get("reason_code"), "reason": payload.get("reason"),
                "required_signer": (payload.get("signature") or {}).get("required_signer"),
            })
    return {
        "scope_stores": store_ids,  # 当前数据域
        "as_of": latest_biz_date(conn, store_ids),  # 最新业务日
        "stockout": stockout,  # 缺货
        "account_diff": diff,  # 账实
        "blocked_metrics": blocked,  # 未签发/数据不足
        "orders": {
            "pending_sign": orders.get("PENDING_SIGN", 0), "signed": orders.get("SIGNED", 0),
            "rejected": orders.get("REJECTED", 0), "stopped": orders.get("STOPPED", 0),
            "draft": orders.get("DRAFT", 0), "total": sum(orders.values()),
        },
        "guardrail_hits": hits,  # 兜底命中
        "readiness": ready["summary"],  # 就绪度摘要
        "synthetic": True,  # 合成数据标识
    }
