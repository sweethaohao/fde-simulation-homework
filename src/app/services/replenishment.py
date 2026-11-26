"""补货主链路：兜底 → 建议 → 待签核 → 签核/驳回 → 留痕与重建。

R1：只到待签核与人工签核，不存在下单接口。
R5：重建包含当次快照、规则/促销/箱规/配送日历版本、兜底原因、建议依据、提交与签核记录。
"""

from __future__ import annotations  # 延迟注解

import json  # JSON 字段
from datetime import date, timedelta  # 日期窗口

from ..agents import guardrail, suggest, trace  # 三个 Agent
from ..config import settings  # 计算参数
from ..db import now_iso, row_to_dict  # 时间与行转换
from ..errors import AppError, ErrorCode  # 统一错误
from . import metrics  # 最新业务日
from .audit import write_audit  # 审计


def _as_dict(value) -> dict:
    """安全解析 JSON 字段。

    入参：value 字符串/字典/None。
    出参：字典。
    失败时的行为：解析失败返回空字典。
    """

    if isinstance(value, dict):  # 已是字典
        return value
    try:
        return json.loads(value or "{}")  # 解析
    except json.JSONDecodeError:  # 脏数据
        return {}


def _new_ids(conn) -> tuple[str, str]:
    """生成单据号与业务编号。

    入参：conn。
    出参：(order_id, biz_no)。
    失败时的行为：SQL 异常向上抛出。
    """

    today = date.today().strftime("%Y%m%d")  # 当日日期
    seq = int(conn.execute("SELECT COUNT(*) AS c FROM replenishment_orders").fetchone()["c"]) + 1  # 递增序号
    while True:  # 避免与种子/并发单号冲突
        order_id = f"ORD-{today}-{seq:04d}"  # 单据 ID
        biz_no = f"SG-{today}-{seq:04d}"  # 业务编号
        exists = conn.execute("SELECT 1 FROM replenishment_orders WHERE order_id = ? OR biz_no = ?", (order_id, biz_no)).fetchone()  # 查重
        if not exists:  # 唯一才返回
            return order_id, biz_no
        seq += 1  # 冲突则顺延


def _link_suggestion(conn, order_id: str, ev_code: str) -> None:
    """为单据建立图谱溯源边。

    入参：conn；order_id；ev_code 依据 f 编号。
    出参：无。
    失败时的行为：SQL 异常向上抛出（图谱与业务同库，同一个事务）。
    """

    sid = f"suggestion:{order_id}"  # 建议节点
    conn.execute(
        "INSERT OR REPLACE INTO graph_nodes(id,kind,name,props,created_at,is_synthetic) VALUES (?,?,?,?,?,1)",
        (sid, "suggestion", f"建议 {order_id}", json.dumps({"order_id": order_id}, ensure_ascii=False), now_iso()),
    )  # 写节点
    conn.execute(
        "INSERT OR REPLACE INTO graph_edges(src,dst,rel,props) VALUES (?,?,?,?)",
        (sid, f"rule:{ev_code}", "EVIDENCED_BY", json.dumps({"f_code": ev_code}, ensure_ascii=False)),
    )  # 建议 → 依据
    conn.execute(
        "INSERT INTO graph_fts(node_id, text) SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM graph_fts WHERE node_id = ?)",
        (sid, "建议 补货 单据 兜底", sid),
    )  # 关键词索引（向量不覆盖建议节点）


def _load_context(conn, store_id: str, sku_id: str, biz_date: str) -> dict:
    """加载建议所需上下文。

    入参：conn；store_id；sku_id；biz_date。
    出参：上下文与原始行。
    失败时的行为：门店/商品不存在抛 AppError(NOT_FOUND)；SQL 异常向上抛出。
    """

    store = row_to_dict(conn.execute("SELECT * FROM stores WHERE store_id = ?", (store_id,)).fetchone())  # 门店
    sku = row_to_dict(conn.execute("SELECT * FROM skus WHERE sku_id = ?", (sku_id,)).fetchone())  # 商品
    if not store or not sku:  # 基础资料必须存在
        raise AppError(ErrorCode.NOT_FOUND, "门店或商品不存在", status=404)
    snap = row_to_dict(conn.execute(
        "SELECT * FROM daily_snapshots WHERE store_id = ? AND sku_id = ? AND biz_date = ?",
        (store_id, sku_id, biz_date),
    ).fetchone())  # 当日快照（可能为空）
    start = (date.fromisoformat(biz_date) - timedelta(days=6)).isoformat()  # 近 7 日
    recent = conn.execute(
        """
        SELECT COUNT(*) AS days, COALESCE(SUM(sales_qty),0) AS sales,
               SUM(CASE WHEN shelf_available = 0 THEN 1 ELSE 0 END) AS out_days,
               COALESCE(AVG(CASE WHEN shelf_available > 0 THEN sales_qty END),0) AS avg_sales
        FROM daily_snapshots WHERE store_id = ? AND sku_id = ? AND biz_date BETWEEN ? AND ?
        """,
        (store_id, sku_id, start, biz_date),
    ).fetchone()  # 近期统计
    promo = conn.execute(
        """
        SELECT version, status FROM promotions
        WHERE sku_id = ? AND (store_id IS NULL OR store_id = ?) AND start_date <= ? AND end_date >= ?
        ORDER BY start_date DESC LIMIT 1
        """,
        (sku_id, store_id, biz_date, biz_date),
    ).fetchone()  # 促销版本
    case_size = sku.get("case_size") or 0  # 整箱规格
    moq_units = int(sku.get("moq_cases") or 1) * max(1, case_size)  # 起订量（件）
    available = 0  # 可用库存
    if snap:  # 有快照才算可用
        available = int(snap["shelf_available"] or 0) + int(snap["backroom_stock"] or 0) + int(snap["in_transit"] or 0)
    avg_sales = float(recent["avg_sales"] or 0)  # 有货日平均销量
    cover = settings.replenishment.cover_days + settings.replenishment.lead_time_days  # 覆盖 + 前置
    need = avg_sales * cover * (1 + settings.replenishment.safety_ratio) - available  # 规则基线
    baseline = max(0, int(round(need)))  # 非负取整
    versions = {
        "promo_version": promo["version"] if promo else "无促销",
        "case_spec_version": "CASE-2026-09-v1",
        "delivery_calendar_version": store.get("delivery_calendar_version"),
        "rule_version": "RULE-2026-09-v1",
    }
    ctx = {
        "store_id": store_id, "store_name": store["name"], "sku_id": sku_id, "sku_name": sku["name"],
        "abc_class": sku["abc_class"], "biz_date": biz_date,
        "book_stock": snap["book_stock"] if snap else None, "shelf_available": snap["shelf_available"] if snap else None,
        "backroom_stock": snap["backroom_stock"] if snap else None, "in_transit": snap["in_transit"] if snap else None,
        "sales_7d": int(recent["sales"] or 0), "stockout_days_7d": int(recent["out_days"] or 0),
        "case_size": case_size or None, "moq_cases": int(sku.get("moq_cases") or 1), "moq_units": moq_units,
        "warehouse_cap": min(int(sku.get("warehouse_cap") or 240), settings.replenishment.warehouse_cap_units),
        "promo_version": versions["promo_version"], "rule_version": versions["rule_version"],
        "delivery_calendar_version": versions["delivery_calendar_version"],
        "rule_baseline_qty": baseline,
    }
    return {"ctx": ctx, "snapshot": snap, "sku": sku, "store": store, "versions": versions}


def suggest_order(conn, user: dict, store_id: str, sku_id: str, biz_date: str | None = None) -> dict:
    """生成补货建议（或兜底停止）。

    入参：conn；user 计划员；store_id；sku_id；biz_date 可选（默认该店最新业务日）。
    出参：{stopped, order_id, ...}。
    失败时的行为：数据缺失/门店商品不存在抛 AppError；兜底命中不抛异常，返回 stopped。
    """

    latest = metrics.latest_biz_date(conn, [store_id])  # 该店最新有数据的日期
    target_date = biz_date or latest  # 允许指定日期（用于演示系统不可用）
    if not target_date:  # 完全无数据
        raise AppError(ErrorCode.DATA_NOT_READY, "该门店没有任何库存快照", status=422)
    guard = guardrail.evaluate(conn, store_id, sku_id, target_date)  # 兜底先行
    loaded = _load_context(conn, store_id, sku_id, target_date)  # 上下文
    ctx, snapshot, sku = loaded["ctx"], loaded["snapshot"], loaded["sku"]
    versions = loaded["versions"]
    if guard["stopped"]:  # 命中兜底：只落 stopped 单，不返回建议量
        order_id, biz_no = _new_ids(conn)  # 生成单号
        primary = guard["primary"]  # 主原因
        conn.execute(
            """
            INSERT INTO replenishment_orders(order_id,biz_no,store_id,sku_id,biz_date,status,
              suggestion_json,snapshot_json,versions_json,guardrail_json,degraded,reason,
              owner_role,owner_name,manual_confirm_at,created_by,created_at,is_synthetic)
            VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,1)
            """,
            (
                order_id, biz_no, store_id, sku_id, target_date, "STOPPED", None,
                json.dumps(snapshot or {}, ensure_ascii=False), json.dumps(versions, ensure_ascii=False),
                json.dumps({"stopped": True, "hits": guard["hits"], "f_code": "f44"}, ensure_ascii=False),
                primary["reason"], primary["owner_role"], primary["owner_name"],
                guard["handoff"]["manual_confirm_at"], user["user_id"], now_iso(),
            ),
        )  # 写 stopped 单
        _link_suggestion(conn, order_id, "f44")  # 溯源边指向 f44
        audit_id = write_audit(
            conn, actor=user, action="suggestion.generate", resource="order", resource_id=order_id,
            decision="stop", error_code=ErrorCode.GUARDRAIL_STOPPED.value,
            detail={"category": primary["category"], "reason": primary["reason"], "f_code": "f44"},
        )  # 兜底留痕
        return {
            "stopped": True, "order_id": order_id, "biz_no": biz_no, "status": "STOPPED",
            "reason": primary["reason"], "hits": guard["hits"], "handoff": guard["handoff"],
            "suggestion": None, "snapshot": snapshot, "versions": versions, "audit_id": audit_id,
            "synthetic": True,
        }

    search_result = trace.search(conn, f"{sku['name']} 整箱 仓容 补货 缺货 促销", settings.retrieval.top_k)  # 检索依据片段
    advice = suggest.run(ctx, search_result["items"])  # LLM 建议（可降级）
    case_size = ctx["case_size"] or 12  # 兜底箱规（正常已被 G4 拦截）
    raw = int(advice["suggestion_qty"])  # 模型/规则建议量
    rounded = ((raw + case_size - 1) // case_size) * case_size if raw > 0 else 0  # 按整箱向上取整
    if 0 < rounded < ctx["moq_units"]:  # 最小起订约束
        rounded = ctx["moq_units"]
    cap = ctx["warehouse_cap"]  # 仓容上限
    final = min(rounded, cap)  # 仓容压回
    capped_units = max(0, rounded - cap)  # 压回数量
    if final and final < ctx["moq_units"]:  # 起订量与仓容冲突时以仓容为准，并在原因中说明
        note = "起订量超过仓容上限，按仓容上限拆批"
    else:
        note = None
    suggestion_json = {
        **advice, "raw_qty": raw, "case_size": case_size, "rounded_qty": rounded,
        "capacity_capped_qty": capped_units, "final_qty": final, "cap": cap,
        "retrieval": {"mode": search_result["mode"], "degraded": search_result["degraded"],
                      "refs": [i.get("f_code") for i in search_result["items"] if i.get("f_code")]},
    }
    order_id, biz_no = _new_ids(conn)  # 单号
    conn.execute(
        """
        INSERT INTO replenishment_orders(order_id,biz_no,store_id,sku_id,biz_date,status,
          suggestion_qty,before_round_qty,rounded_qty,capacity_capped_qty,final_qty,
          suggestion_json,snapshot_json,versions_json,guardrail_json,degraded,reason,
          created_by,created_at,is_synthetic)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,1)
        """,
        (
            order_id, biz_no, store_id, sku_id, target_date, "DRAFT", raw, int(ctx["rule_baseline_qty"]),
            rounded, capped_units, final, json.dumps(suggestion_json, ensure_ascii=False),
            json.dumps(snapshot or {}, ensure_ascii=False), json.dumps(versions, ensure_ascii=False),
            1 if advice["degraded"] else 0, note, user["user_id"], now_iso(),
        ),
    )  # 落草稿单
    _link_suggestion(conn, order_id, "f20")  # 建议依据：整箱/仓容
    audit_id = write_audit(
        conn, actor=user, action="suggestion.generate", resource="order", resource_id=order_id,
        decision="allow", detail={"degraded": advice["degraded"], "source": advice["source"],
                                  "final_qty": final, "case_size": case_size},
    )  # 建议留痕
    return {
        "stopped": False, "order_id": order_id, "biz_no": biz_no, "status": "DRAFT",
        "suggestion": {k: advice[k] for k in ("suggestion_qty", "rationale", "confidence", "evidence_refs", "risks")},
        "source": advice["source"], "degraded": advice["degraded"],
        "degrade_reason": advice.get("degrade_reason"),
        "rule_baseline_qty": ctx["rule_baseline_qty"],
        "rounding": {"before_round": raw, "case_size": case_size, "rounded": rounded,
                     "moq_units": ctx["moq_units"], "capacity_capped": capped_units, "final": final, "cap": cap,
                     "note": note},
        "snapshot": snapshot, "versions": versions, "guardrail": {"stopped": False, "hits": []},
        "retrieval": {"mode": search_result["mode"], "degraded": search_result["degraded"],
                      "items": search_result["items"]},
        "audit_id": audit_id, "synthetic": True,
    }


def list_orders(conn, store_ids: list[str], status: str | None = None, order_id: str | None = None) -> list[dict]:
    """查询单据列表。

    入参：conn；store_ids 数据域；status 可选状态；order_id 可选单据。
    出参：单据列表（新到旧，最多 200）。
    失败时的行为：SQL 异常向上抛出。
    """

    if not store_ids:  # 空数据域
        return []
    ph = ",".join("?" for _ in store_ids)  # 占位符
    sql = f"SELECT * FROM replenishment_orders WHERE store_id IN ({ph})"  # 基础条件
    params: list = list(store_ids)  # 参数
    if status:  # 状态过滤
        sql += " AND status = ?"
        params.append(status)
    if order_id:  # 单据过滤
        sql += " AND order_id = ?"
        params.append(order_id)
    sql += " ORDER BY created_at DESC LIMIT 200"  # 限制返回量
    return [dict(r) for r in conn.execute(sql, params).fetchall()]  # 转字典


def get_order(conn, order_id: str) -> dict:
    """取单张单据。

    入参：conn；order_id。
    出参：单据字典。
    失败时的行为：不存在抛 AppError(NOT_FOUND, 404)。
    """

    row = conn.execute("SELECT * FROM replenishment_orders WHERE order_id = ?", (order_id,)).fetchone()  # 主键查询
    if not row:  # 不存在
        raise AppError(ErrorCode.NOT_FOUND, "单据不存在", status=404)
    return dict(row)  # 返回字典


def submit_order(conn, user: dict, order_id: str) -> dict:
    """提交待签核。

    入参：conn；user 计划员；order_id。
    出参：更新后的单据。
    失败时的行为：状态不允许抛 AppError(VALIDATION_ERROR, 422)。
    """

    order = get_order(conn, order_id)  # 取单据
    if order["status"] != "DRAFT":  # 只有草稿可提交
        raise AppError(ErrorCode.VALIDATION_ERROR, f"单据状态为 {order['status']}，不能提交", status=422)
    conn.execute("UPDATE replenishment_orders SET status='PENDING_SIGN', submitted_at=? WHERE order_id=?", (now_iso(), order_id))  # 状态推进
    write_audit(conn, actor=user, action="order.submit", resource="order", resource_id=order_id,
                decision="allow", detail={"from": "DRAFT", "to": "PENDING_SIGN"})  # 留痕
    return get_order(conn, order_id)  # 返回最新


def update_qty(conn, user: dict, order_id: str, final_qty: int) -> dict:
    """计划员修改建议量。

    入参：conn；user；order_id；final_qty 新数量。
    出参：更新后的单据。
    失败时的行为：非草稿或数量非法抛 AppError。
    """

    order = get_order(conn, order_id)  # 取单据
    if order["status"] != "DRAFT":  # 仅草稿可改
        raise AppError(ErrorCode.VALIDATION_ERROR, "只有草稿可修改建议量", status=422)
    if final_qty < 0 or final_qty > 10000:  # 基本校验
        raise AppError(ErrorCode.VALIDATION_ERROR, "建议量超出允许范围", status=422)
    conn.execute("UPDATE replenishment_orders SET final_qty=?, reason=? WHERE order_id=?",
                 (final_qty, "计划员人工调整", order_id))  # 更新数量
    write_audit(conn, actor=user, action="order.update", resource="order", resource_id=order_id,
                decision="allow", detail={"old_final_qty": order["final_qty"], "new_final_qty": final_qty})  # 留痕
    return get_order(conn, order_id)  # 返回最新


def sign_order(conn, user: dict, order_id: str) -> dict:
    """签核单据。

    入参：conn；user 运营总监；order_id。
    出参：更新后的单据。
    失败时的行为：已签核抛 CONFLICT_ALREADY_SIGNED；自签抛 SELF_SIGN_FORBIDDEN；状态不符抛 VALIDATION_ERROR。
    """

    order = get_order(conn, order_id)  # 取单据
    if order["status"] == "SIGNED":  # 幂等冲突
        raise AppError(ErrorCode.CONFLICT_ALREADY_SIGNED, "单据已签核，不能重复签", status=409)
    if order["status"] != "PENDING_SIGN":  # 只有待签核可签
        raise AppError(ErrorCode.VALIDATION_ERROR, f"状态为 {order['status']}，不能签核", status=422)
    if order.get("created_by") == user["user_id"]:  # 自己不能签自己的单
        raise AppError(ErrorCode.SELF_SIGN_FORBIDDEN, "不能签核自己提交的单据", status=403)
    now = now_iso()  # 签核时间
    conn.execute("UPDATE replenishment_orders SET status='SIGNED', signed_by=?, signed_at=? WHERE order_id=?",
                 (user["username"], now, order_id))  # 更新状态
    write_audit(conn, actor=user, action="order.sign", resource="order", resource_id=order_id,
                decision="sign", detail={"final_qty": order["final_qty"], "versions": _as_dict(order["versions_json"]),
                                         "signed_at": now})  # 签核留痕
    return get_order(conn, order_id)  # 返回最新


def reject_order(conn, user: dict, order_id: str, reason: str) -> dict:
    """驳回单据。

    入参：conn；user；order_id；reason 原因。
    出参：更新后的单据。
    失败时的行为：已签核抛 CONFLICT；缺原因抛 VALIDATION_ERROR。
    """

    order = get_order(conn, order_id)  # 取单据
    if order["status"] == "SIGNED":  # 已签核不可驳回
        raise AppError(ErrorCode.CONFLICT_ALREADY_SIGNED, "已签核单据不能驳回", status=409)
    if not reason or len(reason.strip()) < 2:  # 必须给原因
        raise AppError(ErrorCode.VALIDATION_ERROR, "驳回必须填写原因", status=422)
    conn.execute("UPDATE replenishment_orders SET status='REJECTED', reject_reason=?, signed_by=?, signed_at=? WHERE order_id=?",
                 (reason.strip(), user["username"], now_iso(), order_id))  # 记录驳回
    write_audit(conn, actor=user, action="order.reject", resource="order", resource_id=order_id,
                decision="reject", detail={"reason": reason.strip()})  # 留痕
    return get_order(conn, order_id)  # 返回最新


def rebuild(conn, order_id: str) -> dict:
    """重建单据处置现场（R5）。

    入参：conn；order_id。
    出参：{order, snapshot, versions, suggestion, guardrail, events, audit_trail, timeline}。
    失败时的行为：单据不存在抛 AppError(NOT_FOUND)。
    """

    order = get_order(conn, order_id)  # 单据
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE order_id = ? ORDER BY COALESCE(occurred_at, '9999') ASC", (order_id,)
    ).fetchall()]  # 直接关联事件
    audit_rows = conn.execute(
        "SELECT audit_id, request_id, actor_name, role, action, decision, error_code, detail, created_at "
        "FROM audit_logs WHERE resource_id = ? ORDER BY audit_id ASC", (order_id,)
    ).fetchall()  # 审计轨迹
    audit_trail = []  # 解析后的审计
    for row in audit_rows:  # 逐条解析 detail
        item = dict(row)  # 复制
        item["detail"] = _as_dict(item["detail"])  # JSON 解析
        audit_trail.append(item)  # 加入
    timeline = [  # 关键节点时间线
        {"at": order["created_at"], "event": "创建/生成建议", "actor": order["created_by"]},
    ]
    if order.get("submitted_at"):  # 提交节点
        timeline.append({"at": order["submitted_at"], "event": "提交待签核", "actor": order["created_by"]})
    if order["status"] == "STOPPED":  # 兜底节点
        timeline.append({"at": order["created_at"], "event": "命中兜底，停止自动建议",
                         "actor": order.get("owner_name"), "reason": order.get("reason")})
    if order["status"] == "SIGNED":  # 签核节点
        timeline.append({"at": order["signed_at"], "event": "签核通过", "actor": order["signed_by"]})
    if order["status"] == "REJECTED":  # 驳回节点
        timeline.append({"at": order["signed_at"], "event": "驳回", "actor": order["signed_by"],
                         "reason": order.get("reject_reason")})
    for ev in events:  # 事件流节点
        if ev.get("occurred_at"):  # 只展示有时间戳的
            timeline.append({"at": ev["occurred_at"], "event": ev["event_type"], "actor": ev.get("channel")})
    timeline.sort(key=lambda x: x["at"] or "")  # 按时间排序
    guardrail_data = _as_dict(order.get("guardrail_json"))  # 兜底详情
    return {
        "order": order,  # 单据原样
        "snapshot": _as_dict(order.get("snapshot_json")),  # 当次快照
        "versions": _as_dict(order.get("versions_json")),  # 四类版本
        "suggestion": _as_dict(order.get("suggestion_json")) or None,  # 建议依据
        "guardrail": guardrail_data or None,  # 兜底原因
        "events": events,  # 原始事件（含空时间戳）
        "audit_trail": audit_trail,  # 审计留痕
        "timeline": timeline,  # 合并时间线
        "synthetic": True,
    }


def options(conn, store_ids: list[str]) -> dict:
    """下拉选项（前端使用）。

    入参：conn；store_ids 数据域。
    出参：{stores, skus, dates, demo_cases}。
    失败时的行为：SQL 异常向上抛出。
    """

    if not store_ids:  # 空域
        return {"stores": [], "skus": [], "dates": [], "demo_cases": []}
    ph = ",".join("?" for _ in store_ids)  # 占位符
    stores = [dict(r) for r in conn.execute(
        f"SELECT store_id, name FROM stores WHERE store_id IN ({ph}) ORDER BY store_id", store_ids
    ).fetchall()]  # 门店
    skus = [dict(r) for r in conn.execute(
        "SELECT sku_id, name, case_size FROM skus ORDER BY sku_id"
    ).fetchall()]  # 商品
    dates = [r["d"] for r in conn.execute(
        f"SELECT DISTINCT biz_date AS d FROM daily_snapshots WHERE store_id IN ({ph}) ORDER BY biz_date DESC", store_ids
    ).fetchall()]  # 可选业务日
    return {
        "stores": stores, "skus": skus, "dates": dates,
        "demo_cases": [  # 确定性兜底样例，DEMO.md 与前端共用
            {"label": "临时促销", "store_id": "S01", "sku_id": "K001", "hint": "最后一天命中 G1"},
            {"label": "缺货截断", "store_id": "S02", "sku_id": "K002", "hint": "近 7 天销量 0 且缺货"},
            {"label": "可售/账面不一致", "store_id": "S03", "sku_id": "K003", "hint": "最后一天命中 G3"},
            {"label": "依据缺失（整箱规格）", "store_id": "S04", "sku_id": "K049", "hint": "case_size 为空"},
            {"label": "依据缺失（配送日历）", "store_id": "S08", "sku_id": "K005", "hint": "门店版本为空"},
            {"label": "关键系统不可用", "store_id": "S07", "sku_id": "K006", "hint": "最后 3 天无快照"},
        ],
    }
