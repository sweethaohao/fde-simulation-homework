"""把规则、证据、角色、环节、指标灌入知识图谱（幂等）。

f 编号原样入库，不新增、不改写；材料未记录的标 unverified 并写明向谁确认。
同时为「建议」建立 EVIDENCED_BY / OWNED_BY 边，支撑三跳溯源。
"""

from __future__ import annotations  # 延迟注解

import argparse  # 命令行
import json  # props
import sys  # 导入路径
from pathlib import Path  # 路径

ROOT = Path(__file__).resolve().parents[1]  # 仓库根
if str(ROOT) not in sys.path:  # 允许直接运行
    sys.path.insert(0, str(ROOT))

from src.app.db import connect, init_schema, now_iso  # noqa: E402  # 连接与建表
from src.app.llm import ollama  # noqa: E402  # 嵌入（可失败）
from src.app.services import retrieval  # noqa: E402  # FTS 索引文本

ROLES = {  # 七个角色节点
    "admin": ("系统管理员", "admin"),
    "ops_director": ("顾明岚（运营总监）", "ops_director"),
    "loss_manager": ("傅景（商品与损耗经理）", "loss_manager"),
    "planner": ("唐映秋（补货计划员）", "planner"),
    "store_manager": ("林亦舟（门店店长）", "store_manager"),
    "logistics": ("叶书（区域物流经理）", "logistics"),
    "data_engineer": ("秦芮（数据工程师）", "data_engineer"),
}

F_NODES = [  # (编号, 类型, 摘要, 责任人角色, 状态, 备注)
    ("f00", "signal", "目标：爆款缺货率 12%→5% 以下、库存周转 48→35 天；「对上面交的账」", "ops_director", "verified", "顾明岚目标"),
    ("f01", "rule", "缺货标识维护责任未落地，需秦芮确认数据可得性", "ops_director", "verified", "责任未落定"),
    ("f02", "rule", "试点 8 店、50 个常温 SKU、6 周；缺货减少不能靠全面增库存", "ops_director", "verified", ""),
    ("f03", "rule", "预算 16 万元封顶，含接口、培训、复核、维护四项成本", "ops_director", "verified", "封顶数不是可花数"),
    ("f04", "rule", "立项由运营总监牵头，商品与损耗经理有风险否决权，区域物流参与交接验收", "ops_director", "verified", "决策结构"),
    ("f05", "rule", "首期覆盖 8 店 50 SKU，先历史回放再并行，不直接关掉人工流程", "ops_director", "verified", ""),
    ("f10", "evidence", "货在后仓未上架，系统仍算有库存；消费者看到空货架", "store_manager", "verified", "有货不可售"),
    ("f11", "evidence", "临时促销变价使预测失效，节假日客流差异大", "store_manager", "verified", ""),
    ("f12", "signal", "每天盘 20 个重点 SKU，账实差异平均 8%", "store_manager", "verified", "需分清有货与可售"),
    ("f13", "evidence", "发现缺货无时点记录，靠人目视", "store_manager", "verified", "t0 不可得"),
    ("f14", "rule", "群聊/个人表补说明再回系统补录，关键原因无结构化字段", "store_manager", "verified", ""),
    ("f15", "rule", "每条辅助结果须显示原记录；异常先由计划员核对，未确认不覆盖原记录", "store_manager", "verified", ""),
    ("f20", "rule", "最小起订量、整箱规格、仓库分配上限导致压回，一边缺一边压", "planner", "verified", ""),
    ("f21", "rule", "促销版本由运营下发，门店改价不总是回传", "planner", "verified", ""),
    ("f22", "rule", "跨店调拨需区域经理批准；自动采购不能直接上线", "planner", "verified", "区域经理未受访"),
    ("f23", "rule", "发送消息不等于责任已转移；交接须带版本与未解决事项", "planner", "verified", ""),
    ("f24", "rule", "规则由业务负责人维护，争议记录使用版本并由商品与损耗经理确认", "planner", "verified", ""),
    ("f25", "rule", "异常分派须写明处理人、截止时间、退回原因；超时交运营总监", "planner", "verified", ""),
    ("f30", "evidence", "POS 按日汇总、WMS 实时；商品编码存在新旧码与套装码", "data_engineer", "verified", "粒度不一致"),
    ("f31", "evidence", "销量为零可能是缺货截断，不一定没人买", "data_engineer", "verified", ""),
    ("f32", "rule", "供应商下单接口尚未审批，首期只导历史 CSV", "data_engineer", "verified", "R3/R9"),
    ("f33", "rule", "销售库存、促销版本、整箱规格、配送日历须校验主编号/时间/来源/版本；缺字段进人工修复队列", "data_engineer", "verified", ""),
    ("f34", "rule", "每天两次刷新；超过一个周期标注待刷新，历史快照不得伪装当前", "data_engineer", "verified", ""),
    ("f35", "rule", "接口失败继续人工流程，保留只读快照；恢复后按业务编号去重补齐", "data_engineer", "verified", ""),
    ("f42", "rule", "验收同看缺货、周转、报废，促销期间单列误差；反向恶化不认", "loss_manager", "verified", ""),
    ("f43", "rule", "评价记录暂留 90 天，到期由商品与损耗经理确认删除或归档", "loss_manager", "verified", "R6"),
    ("f44", "rule", "兜底五类：临时促销、缺货截断、可售与账面不一致、依据缺失、关键系统不可用 → 停止自动建议，标明原因、接手人、人工确认时间", "loss_manager", "verified", "硬拦截"),
    ("f45", "rule", "审核日志留存六类要素可重建；不记口令与非必要个人信息", "loss_manager", "verified", "R4/R5"),
    ("f51", "signal", "约 30% 延迟补货源于配送排队，不是预测问题", "logistics", "verified", "先核查截单时间"),
    ("f52", "rule", "不改供应商合同；须给门店看预计到货与本次未满足数量", "logistics", "verified", ""),
    ("f53", "rule", "三指标分母/周期/排除规则由业务负责人固定；不能用消息发送量代替完成量", "logistics", "verified", "R10 来源"),
    ("f54", "rule", "下游反馈须关联原业务编号与资料版本；叶书受理、计划员核实", "logistics", "verified", ""),
    ("f55", "rule", "每周复盘，改进负责人由运营总监指定，复测达标才更新规则", "ops_director", "verified", ""),
    ("零售-032", "rule", "兜底触发率与日志完整率是否一票否决待定", "loss_manager", "unverified", "向傅景确认"),
    ("零售-033", "rule", "审批矩阵与代签规则待定", "loss_manager", "unverified", "向傅景、顾明岚确认"),
    ("零售-034", "evidence", "下游反馈缺编号/版本即退回重问，全程不留痕", "logistics", "verified", ""),
]

STEPS = [  # 11 个处理环节（现状流程，不是目标态）
    ("1", "货架空了（门店目视发现）", "门店", "无系统记录、无时间戳（f13）"),
    ("2", "门店报数", "门店", "群聊/电话/系统三渠道（f14）"),
    ("3", "总部接收与汇总", "总部", "POS 日汇总 + WMS 实时，粒度不同（f30）"),
    ("4", "建议量计算", "总部", "兜底必须在此步之前（f44）"),
    ("5", "整箱取整", "计划", "按整箱规格向上取整（f20）"),
    ("6", "仓容校验/压回", "仓库", "超上限压回或拆批（f20）"),
    ("7", "跨店调拨审批", "计划", "区域经理批准（f22，未受访）"),
    ("8", "仓库分配", "仓库", "门店看不到分配依据（f01）"),
    ("9", "配送排队与截单", "配送", "约 30% 延迟源于排队（f51）"),
    ("10", "签收交接", "物流", "唯一可信交接时间戳（f52）"),
    ("11", "上架可售", "门店", "全程无记录，有货不可售发生处（f10）"),
]

STEP_APPLIES = {  # 规则/证据影响哪些环节
    "f00": ["4"], "f01": ["2", "8"], "f02": ["4"], "f03": ["4"], "f04": ["4"], "f05": ["4"],
    "f10": ["3", "11"], "f11": ["4"], "f12": ["2"], "f13": ["1"], "f14": ["2", "10"], "f15": ["10"],
    "f20": ["5", "6"], "f21": ["4"], "f22": ["7"], "f23": ["10"], "f24": ["4"], "f25": ["10"],
    "f30": ["3"], "f31": ["3", "4"], "f32": ["8"], "f33": ["3"], "f34": ["3"], "f35": ["3"],
    "f42": ["4"], "f43": ["10"], "f44": ["4"], "f45": ["10"], "f51": ["9"], "f52": ["10"],
    "f53": ["4"], "f54": ["10"], "f55": ["11"], "零售-032": ["4"], "零售-033": ["7"], "零售-034": ["10"],
}

METRIC_NODES = [  # 指标口径节点（SIGNED_BY 表达签发关系）
    ("stockout_rate", "缺货率", "傅景", True, "两种分母：当日截面 12% / 时段加权 21%"),
    ("account_diff_rate", "账实差异率", "傅景", True, "真差异 / 编码伪差异拆分"),
    ("turnover_days", "库存周转天数", "顾明岚", False, "含/不含在途差 6 天以上，未签发"),
    ("waste_rate", "报废率", "傅景", False, "6 周窗口过短，未签发"),
    ("shelf_restock_hours", "上架时效", "叶书", True, "数据不足：shelved 无时间戳"),
]

SIGNER_ROLE = {"傅景": "loss_manager", "顾明岚": "ops_director", "叶书": "logistics"}  # 签发人姓名 → 角色 ID

DOCS = [  # 来源文档
    ("doc:discovery-report", "业务需求发现报告（f00–f55 访谈台账）"),
    ("doc:process-flow", "现状流程图与环境路径（6 泳道 × 11 步）"),
    ("doc:four-source", "四来源收敛与证据核对"),
    ("doc:build-prompt", "MVP 施工图（硬约束）"),
]


def node_id(code: str, kind: str) -> str:
    """按类型生成节点 ID。

    入参：code 编号；kind 类型。
    出参：`kind:code`。
    失败时的行为：不抛异常。
    """

    return f"{kind}:{code}"  # 与第 8 节约定一致


def seed(db_path: str | Path = ROOT / "data" / "mvp.db") -> dict:
    """灌入图谱（幂等）。

    入参：db_path 数据库路径。
    出参：节点/边/向量数量统计。
    失败时的行为：SQL 异常向上抛出；嵌入失败仅跳过向量，不影响图谱。
    """

    conn = connect(db_path)  # 打开连接
    try:
        init_schema(conn)  # 确保表存在
        for table in ("graph_fts", "graph_embeddings", "graph_edges", "graph_nodes"):  # 清理旧图
            conn.execute(f"DELETE FROM {table}")  # 幂等重建
        created = now_iso()  # 统一时间
        nodes: list[tuple] = []  # 节点行
        edges: list[tuple] = []  # 边行

        for role_code, (name, official) in ROLES.items():  # 角色节点
            nodes.append((f"role:{official}", "role", name, json.dumps({"role_code": official}, ensure_ascii=False), created, 1))
        for doc_id, name in DOCS:  # 文档节点
            nodes.append((doc_id, "doc", name, json.dumps({"doc": doc_id}, ensure_ascii=False), created, 1))
        for code, kind, summary, owner, status, note in F_NODES:  # 规则/证据/信号节点
            nid = node_id(code, kind)  # 节点 ID
            props = {"f_code": code, "summary": summary, "status": status, "owner_role": owner, "note": note}
            nodes.append((nid, kind, f"{code} · {summary[:60]}", json.dumps(props, ensure_ascii=False), created, 1))
            edges.append((nid, f"role:{owner}", "OWNED_BY", json.dumps({"why": "责任落人，不落部门"}, ensure_ascii=False)))  # 责任人边
            for step_no in STEP_APPLIES.get(code, []):  # 影响环节
                edges.append((nid, f"step:st{int(step_no):02d}", "APPLIES_TO", "{}"))
            if status == "unverified":  # 未核实条目显式标注
                edges.append((nid, "role:loss_manager", "APPLIES_TO", json.dumps({"note": note}, ensure_ascii=False)))
            edges.append((nid, "doc:discovery-report", "EVIDENCED_BY", json.dumps({"source": "访谈台账"}, ensure_ascii=False)))
        for no, name, lane, note in STEPS:  # 流程环节
            nodes.append((f"step:st{int(no):02d}", "step", f"{no}. {name}", json.dumps({"lane": lane, "note": note}, ensure_ascii=False), created, 1))
        for idx in range(1, len(STEPS)):  # 环节先后
            edges.append((f"step:st{idx:02d}", f"step:st{idx + 1:02d}", "PRECEDES", "{}"))
        for code, name, signer, signed, note in METRIC_NODES:  # 指标节点
            mid = f"metric:{code}"  # 指标节点 ID
            signer_role = SIGNER_ROLE[signer]  # 签发人姓名 → 角色 ID
            props = {"metric_code": code, "signed": signed, "signed_by": signer if signed else None,
                     "required_signer": signer, "note": note}
            nodes.append((mid, "metric", f"指标 · {name}", json.dumps(props, ensure_ascii=False), created, 1))
            edges.append((mid, f"role:{signer_role}", "OWNED_BY", "{}"))  # 口径归属人
            if signed:  # 已签发才有 SIGNED_BY 边
                edges.append((mid, f"role:{signer_role}", "SIGNED_BY", json.dumps({"signed_at": created}, ensure_ascii=False)))
        edges = [e for e in edges if "{" not in e[1]]  # 防御：移除任何未替换的占位边
        edges.append(("metric:stockout_rate", "rule:f53", "EVIDENCED_BY", "{}"))
        edges.append(("metric:account_diff_rate", "evidence:f30", "EVIDENCED_BY", "{}"))
        edges.append(("metric:turnover_days", "signal:f00", "EVIDENCED_BY", "{}"))
        edges.append(("metric:waste_rate", "rule:f42", "EVIDENCED_BY", "{}"))
        edges.append(("metric:shelf_restock_hours", "evidence:f10", "EVIDENCED_BY", "{}"))
        edges.append(("rule:f44", "signal:stop", "TRIGGERS", json.dumps({"action": "停止自动建议"}, ensure_ascii=False)))
        nodes.append(("signal:stop", "signal", "兜底触发即停（不返回建议量）", json.dumps({"summary": "命中 f44 任一类别即短路"}, ensure_ascii=False), created, 1))
        edges.append(("signal:stop", "role:loss_manager", "OWNED_BY", "{}"))

        # 补货单 → 依据 → 责任人（三跳溯源的起点）
        orders = conn.execute(
            "SELECT order_id, biz_no, store_id, sku_id, status FROM replenishment_orders"
        ).fetchall()  # 全部单据
        for order in orders:  # 每单建一个建议节点
            sid = f"suggestion:{order['order_id']}"  # 建议节点 ID
            ev_code = "f44" if order["status"] == "STOPPED" else "f20"  # 兜底单指向 f44，其余指向整箱/仓容
            nodes.append((sid, "suggestion", f"建议 {order['biz_no']} · {order['store_id']}/{order['sku_id']}",
                          json.dumps({"order_id": order["order_id"], "status": order["status"]}, ensure_ascii=False), created, 1))
            edges.append((sid, f"rule:{ev_code}", "EVIDENCED_BY", json.dumps({"f_code": ev_code}, ensure_ascii=False)))
        # 去重边（PRIMARY KEY src,dst,rel）
        unique_edges = list({(a, b, c): (a, b, c, d) for a, b, c, d in edges}.values())
        conn.executemany("INSERT OR REPLACE INTO graph_nodes(id,kind,name,props,created_at,is_synthetic) VALUES (?,?,?,?,?,?)", nodes)  # 写节点
        conn.executemany("INSERT OR REPLACE INTO graph_edges(src,dst,rel,props) VALUES (?,?,?,?)", unique_edges)  # 写边

        # FTS5：全量节点（含建议）按中文二元组建索引
        fts_rows = []  # 索引行
        for nid, kind, name, props, _, _ in nodes:  # 复用节点数据
            fts_rows.append((nid, retrieval.index_text({"id": nid, "name": name, "props": props})))  # 生成 token 串
        conn.executemany("INSERT INTO graph_fts(node_id, text) VALUES (?,?)", fts_rows)  # 写入 FTS

        # 向量：只嵌入非 suggestion 节点（建议数量大，关键词足够）
        vector_count = 0  # 成功嵌入数
        embed_targets = [n for n in nodes if n[1] != "suggestion"]  # 跳过建议
        try:
            for i in range(0, len(embed_targets), 16):  # 分批 16 条
                batch = embed_targets[i:i + 16]  # 当前批
                texts = [f"{n[2]} {n[3]}" for n in batch]  # 名称 + 属性作为嵌入文本
                vectors = ollama.embed(texts)  # 调用嵌入模型
                conn.executemany(
                    "INSERT OR REPLACE INTO graph_embeddings(node_id,vector,model,created_at) VALUES (?,?,?,?)",
                    [(n[0], json.dumps(v), "nomic-embed-text", created) for n, v in zip(batch, vectors)],
                )
                vector_count += len(vectors)  # 累计
        except ollama.LlmUnavailable as exc:  # 模型不可用 → 只留 FTS
            print(f"[WARN] 嵌入不可用，降级 FTS5：{exc}")
        conn.commit()  # 提交
        return {"nodes": len(nodes), "edges": len(unique_edges), "fts": len(fts_rows), "vectors": vector_count}  # 统计
    finally:
        conn.close()  # 关闭


def main() -> None:
    """命令行入口。

    入参：--db 数据库路径。
    出参：无（打印统计）。
    失败时的行为：异常向上抛出。
    """

    parser = argparse.ArgumentParser(description="灌入知识图谱")  # 参数解析
    parser.add_argument("--db", default=str(ROOT / "data" / "mvp.db"))  # 默认运行库
    args = parser.parse_args()  # 解析
    print(json.dumps(seed(args.db), ensure_ascii=False, indent=2))  # 打印统计


if __name__ == "__main__":
    main()
