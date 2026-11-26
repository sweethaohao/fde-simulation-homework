"""所有提示词模板集中在此，便于审计与版本化（f33/f34）。

约束：模型只能给出「建议」，不得下单、不得承诺到货、不得编造 f 编号。
"""

from __future__ import annotations  # 延迟注解

SUGGEST_SYSTEM = """你是「拾光连锁」门店补货建议助手，服务于补货计划员。
只输出一个 JSON 对象，不要输出 Markdown 或解释文字。字段：
- suggestion_qty: 整数，单位「件」，必须在 0 到 600 之间
- rationale: 不超过 120 个汉字，说明建议量如何得出
- confidence: 0 到 1 之间的小数
- evidence_refs: 字符串数组，只能引用用户消息中给出的 f 编号（例如 "f20"）
- risks: 字符串数组，列出仍未消除的不确定性

硬性规则：
1. 你只能给出建议，不能下单、不能承诺到货时间、不能修改规则或口径。
2. 不要编造 f 编号；没有依据就写空数组。
3. 涉及临时促销、缺货截断、可售与账面不一致、依据缺失、关键系统不可用时，系统已经拦截，
   你不需要也不允许绕过这些兜底。
4. 建议量必须考虑整箱规格、最小起订量与仓容上限，但这些最终由服务端确定性校验。"""


DIAGNOSE_SYSTEM = """你是门店缺货诊断助手。只输出一个 JSON 对象：
{"summary": "不超过 80 字", "causes": [{"key": "not_on_shelf|no_stock|replenished_not_shelved", "label": "中文", "count": 整数, "note": "不超过 40 字"}], "confidence": 0~1}
数字只能来自用户给出的统计，不要自行补充或估算。"""


def build_suggest_user(context: dict, fragments: list[dict], max_chars: int) -> dict:
    """构造建议请求的用户消息。

    入参：context 单据上下文；fragments 检索片段；max_chars 片段总字符上限。
    出参：{"role":"user","content":...}。
    失败时的行为：不抛异常；超限片段会被截断，保证总长可控。
    """

    lines: list[str] = []  # 逐行拼接提示词
    lines.append("【门店与商品】")
    lines.append(f"门店：{context.get('store_id')} {context.get('store_name', '')}")
    lines.append(f"商品：{context.get('sku_id')} {context.get('sku_name', '')}（{context.get('abc_class', '')}）")
    lines.append(f"业务日期：{context.get('biz_date')}")
    lines.append("【库存与销量】")
    lines.append(
        f"账面库存 {context.get('book_stock')}，货架可售 {context.get('shelf_available')}，"
        f"后仓 {context.get('backroom_stock')}，在途 {context.get('in_transit')}"
    )
    lines.append(f"近 7 日销量合计：{context.get('sales_7d')}（其中缺货天数 {context.get('stockout_days_7d')}）")
    lines.append("【约束与版本】")
    lines.append(f"整箱规格：{context.get('case_size')} 件/箱；最小起订：{context.get('moq_cases')} 箱")
    lines.append(f"仓容上限：{context.get('warehouse_cap')} 件；促销版本：{context.get('promo_version')}")
    lines.append(f"规则版本：{context.get('rule_version')}；配送日历版本：{context.get('delivery_calendar_version')}")
    lines.append(f"服务端规则基线建议量：{context.get('rule_baseline_qty')} 件")
    lines.append("【可引用依据片段】")
    used = 0  # 已使用字符数
    for item in fragments:  # 逐片段追加
        text = f"[{item.get('f_code') or item.get('node_id')}] {item.get('name')}：{item.get('snippet', '')}"
        if used + len(text) > max_chars:  # 超过总字符上限就停
            break
        lines.append(text)  # 追加
        used += len(text)  # 累计
    lines.append("【任务】给出建议量、依据与置信说明，只输出 JSON。")
    return {"role": "user", "content": "\n".join(lines)}


def build_diagnose_user(context: dict) -> dict:
    """构造诊断请求的用户消息。

    入参：context 聚合统计。
    出参：{"role":"user","content":...}。
    失败时的行为：不抛异常。
    """

    return {
        "role": "user",
        "content": (
            f"门店 {context.get('store_id')} 近 {context.get('days')} 天缺货 SKU {context.get('stockout_skus')} 个，"
            f"其中「后仓有货但货架为 0」{context.get('not_on_shelf')} 个，「账面为 0」{context.get('no_stock')} 个，"
            f"「已签收但缺上架记录」{context.get('replenished_not_shelved')} 个。"
            "请按这三类拆分并给出简要诊断，只输出 JSON。"
        ),
    }
