"""建议 Agent：LLM 出建议 + 依据，服务端做确定性校验；失败降级规则基线。

模型只能建议，不能下单、不能承诺到货；任何异常都不阻断主链路（degraded=true）。
"""

from __future__ import annotations  # 延迟注解

import json  # 解析模型输出
import re  # 提取 JSON

from . import registry  # 留痕
from ..config import settings  # 上限参数
from ..llm import ollama, prompts  # 模型与提示词

F_CODE_RE = re.compile(r"^f\d{2}$")  # 允许引用的 f 编号形态


def _extract_json(content: str) -> dict:
    """从模型输出中提取 JSON 对象。

    入参：content 模型原文。
    出参：解析后的字典。
    失败时的行为：无法解析时抛 ValueError（调用方降级）。
    """

    try:
        data = json.loads(content)  # 正常情况直接解析
        if isinstance(data, dict):  # 必须是对象
            return data
    except json.JSONDecodeError:  # 容忍模型加前后缀
        match = re.search(r"\{.*\}", content, re.S)  # 抓第一个大括号块
        if match:
            data = json.loads(match.group(0))  # 二次解析
            if isinstance(data, dict):  # 仍要求对象
                return data
    raise ValueError("模型输出不是 JSON 对象")


def _validate(data: dict, allowed_codes: set[str]) -> dict:
    """校验并规范化模型建议。

    入参：data 模型输出；allowed_codes 允许引用的 f 编号集合。
    出参：规范化后的建议。
    失败时的行为：关键字段非法时抛 ValueError。
    """

    qty = data.get("suggestion_qty")  # 建议量
    if isinstance(qty, bool) or not isinstance(qty, (int, float)):  # 拒绝布尔/字符串
        raise ValueError("suggestion_qty 非数值")
    qty = int(round(qty))  # 四舍五入
    if qty < 0 or qty > 600:  # 业务上限
        raise ValueError("suggestion_qty 越界")
    rationale = str(data.get("rationale") or "").strip()  # 依据
    if not rationale:  # 必须有依据
        raise ValueError("缺少 rationale")
    try:
        confidence = float(data.get("confidence", 0.5))  # 置信度
    except (TypeError, ValueError):  # 非法数值
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))  # 截断到 [0,1]
    refs_raw = data.get("evidence_refs") or []  # 引用
    refs = [str(r) for r in refs_raw if isinstance(r, str) and F_CODE_RE.match(str(r))] if isinstance(refs_raw, list) else []  # 只留合法 f 编号
    refs = [r for r in refs if r in allowed_codes]  # 过滤未提供的依据，防止编造
    risks_raw = data.get("risks") or []  # 风险
    risks = [str(r)[:120] for r in risks_raw][:5] if isinstance(risks_raw, list) else []  # 限制条数与长度
    return {
        "suggestion_qty": qty, "rationale": rationale[:300], "confidence": round(confidence, 2),
        "evidence_refs": refs[:6], "risks": risks,
    }


def run(ctx: dict, fragments: list[dict]) -> dict:
    """生成补货建议。

    入参：ctx 单据上下文（含 rule_baseline_qty）；fragments 检索片段。
    出参：{suggestion_qty, rationale, confidence, evidence_refs, risks, source, degraded, ...}。
    失败时的行为：模型不可用/输出非法时返回规则基线并置 degraded=true。
    """

    allowed = {f.get("f_code") for f in fragments if f.get("f_code")}  # 可引用的依据集合
    messages = [
        {"role": "system", "content": prompts.SUGGEST_SYSTEM},  # 硬约束系统提示
        prompts.build_suggest_user(ctx, fragments, settings.llm.max_context_chars),  # 上下文与片段
    ]
    try:
        reply = ollama.chat(messages, format_json=True)  # 调用本地模型
    except ollama.LlmUnavailable as exc:  # 模型不可用 → 规则兜底
        registry.record_run(
            "llm", agent="suggest", input_summary=f"{ctx.get('store_id')}/{ctx.get('sku_id')}",
            output_summary="LLM 不可用，降级规则基线", status="degraded", detail={"error": str(exc)},
        )
        return _degraded(ctx, reason=f"本地模型不可用：{str(exc)[:80]}")  # 降级返回
    try:
        parsed = _extract_json(reply["content"])  # 解析
        advice = _validate(parsed, allowed)  # 校验
    except (ValueError, json.JSONDecodeError) as exc:  # 输出非法 → 规则兜底
        registry.record_run(
            "llm", agent="suggest", input_summary=f"{ctx.get('store_id')}/{ctx.get('sku_id')}",
            output_summary="模型输出非法，降级规则基线", duration_ms=reply["duration_ms"],
            tokens=reply["tokens"], status="degraded", detail={"error": str(exc), "raw": reply["content"][:500]},
        )
        return _degraded(ctx, reason=f"模型输出不可解析：{str(exc)[:80]}", tokens=reply["tokens"], duration_ms=reply["duration_ms"])  # 降级
    registry.record_run(
        "llm", agent="suggest", input_summary=f"{ctx.get('store_id')}/{ctx.get('sku_id')}",
        output_summary=f"建议 {advice['suggestion_qty']} 件，置信 {advice['confidence']}",
        duration_ms=reply["duration_ms"], tokens=reply["tokens"], status="ok",
        detail={"model": reply["model"], "format": "json"},
    )
    return {
        **advice, "source": "llm", "degraded": False,
        "model": reply["model"], "tokens": reply["tokens"], "duration_ms": reply["duration_ms"],
    }


def _degraded(ctx: dict, reason: str, tokens: int = 0, duration_ms: int = 0) -> dict:
    """构造降级建议。

    入参：ctx 上下文；reason 降级原因；tokens/duration_ms 调用统计。
    出参：规则基线建议，degraded=true。
    失败时的行为：不抛异常。
    """

    baseline = int(ctx.get("rule_baseline_qty") or 0)  # 规则基线
    return {
        "suggestion_qty": baseline,
        "rationale": f"规则基线：近 7 日销量、覆盖天数、可用库存与整箱规格计算；{reason}",
        "confidence": 0.55,
        "evidence_refs": ["f20", "f53"],
        "risks": ["未经过模型复核，需计划员确认", "促销与缺货截断已由兜底排除，但仍有执行波动"],
        "source": "degraded", "degraded": True, "model": None,
        "tokens": tokens, "duration_ms": duration_ms, "degrade_reason": reason,
    }
