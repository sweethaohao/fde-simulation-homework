"""Ollama 客户端：chat / embed，强制 think=false。

qwen3.5:9b 是思考模型，不带 `"think": false` 时 message.content 会为空；
生成失败不抛出到业务层，由建议 Agent 降级为规则兜底并标注 degraded。
"""

from __future__ import annotations  # 延迟注解

import time  # 计时
from typing import Any  # 返回结构

import httpx  # 仅允许在这个文件出现 httpx（R9）

from ..config import settings  # 模型地址/参数


class LlmUnavailable(RuntimeError):
    """本地模型不可用（服务未启动、超时、返回空内容）。"""


def _options() -> dict[str, Any]:
    """组装 Ollama 参数。

    入参：无。
    出参：options 字典（温度、上下文、生成上限）。
    失败时的行为：不抛异常。
    """

    return {
        "temperature": settings.llm.temperature,  # 低温度，减少编造
        "num_ctx": settings.llm.num_ctx,  # 上下文窗口
        "num_predict": settings.llm.num_predict,  # 生成长度上限
    }


def chat(messages: list[dict], format_json: bool = True) -> dict:
    """调用本地对话模型。

    入参：messages OpenAI 风格消息；format_json 是否强制 JSON 输出。
    出参：{content, thinking, tokens, duration_ms, model}。
    失败时的行为：连接失败/超时/空内容抛 LlmUnavailable（调用方负责降级）。
    """

    started = time.perf_counter()  # 开始计时
    payload: dict[str, Any] = {
        "model": settings.llm.chat_model,  # 模型名来自配置
        "messages": messages,  # 提示词
        "stream": False,  # 一次性返回，便于记录 token
        "think": False,  # 硬约束：必须 false，否则 content 为空
        "options": _options(),  # 采样参数
    }
    if format_json:  # 建议 Agent 需要可直接解析的 JSON
        payload["format"] = "json"  # Ollama JSON 模式
    try:
        with httpx.Client(timeout=settings.llm.timeout_seconds) as client:  # 每次调用独立连接
            resp = client.post(f"{settings.llm.base_url}/api/chat", json=payload)  # 调用本地服务
            resp.raise_for_status()  # 非 2xx 视为失败
            body = resp.json()  # 解析响应
    except (httpx.HTTPError, ValueError) as exc:  # 网络/解析异常统一降级
        raise LlmUnavailable(str(exc)) from exc
    message = body.get("message") or {}  # 兼容缺字段
    content = (message.get("content") or "").strip()  # 正文
    thinking = (message.get("thinking") or "").strip()  # 思考内容（仅记录，不参与解析）
    if not content:  # think=false 下不应为空
        raise LlmUnavailable("模型返回空 content（请检查 think=false 与模型状态）")
    duration_ms = int((time.perf_counter() - started) * 1000)  # 耗时
    tokens = int(body.get("prompt_eval_count") or 0) + int(body.get("eval_count") or 0)  # 输入+输出 token
    return {
        "content": content, "thinking": thinking, "tokens": tokens,
        "duration_ms": duration_ms, "model": body.get("model") or settings.llm.chat_model,
    }


def embed(texts: list[str]) -> list[list[float]]:
    """调用嵌入模型。

    入参：texts 文本列表。
    出参：与输入等长的向量列表（768 维）。
    失败时的行为：连接失败/维度不符抛 LlmUnavailable（检索层降级 FTS5）。
    """

    if not texts:  # 空输入直接返回
        return []
    payload = {"model": settings.llm.embed_model, "input": texts}  # 嵌入请求
    try:
        with httpx.Client(timeout=settings.llm.timeout_seconds) as client:  # 独立连接
            resp = client.post(f"{settings.llm.base_url}/api/embed", json=payload)  # 调用嵌入接口
            resp.raise_for_status()  # 非 2xx 失败
            body = resp.json()  # 解析
    except (httpx.HTTPError, ValueError) as exc:  # 网络异常
        raise LlmUnavailable(str(exc)) from exc
    vectors = body.get("embeddings") or []  # 兼容不同返回结构
    if len(vectors) != len(texts):  # 数量必须匹配
        raise LlmUnavailable("嵌入返回数量与输入不一致")
    for vec in vectors:  # 校验维度
        if len(vec) != settings.retrieval.vector_dim:  # 维度不符会导致相似度失真
            raise LlmUnavailable(f"嵌入维度 {len(vec)} 与配置 {settings.retrieval.vector_dim} 不一致")
    return vectors  # 返回向量


def health() -> dict:
    """探测 Ollama 是否可达。

    入参：无。
    出参：{ok, detail, models}。
    失败时的行为：不抛异常，返回 ok=false 与原因。
    """

    try:
        with httpx.Client(timeout=3) as client:  # 健康检查要快
            resp = client.get(f"{settings.llm.base_url}/api/tags")  # 模型列表
            resp.raise_for_status()  # 非 2xx 视为不可用
            models = [m.get("name") for m in resp.json().get("models", [])]  # 已装模型
    except (httpx.HTTPError, ValueError) as exc:  # 探测失败
        return {"ok": False, "detail": str(exc), "models": []}  # 返回原因
    has_chat = any(settings.llm.chat_model in (m or "") for m in models)  # 生成模型是否就绪
    has_embed = any(settings.llm.embed_model in (m or "") for m in models)  # 嵌入模型是否就绪
    return {
        "ok": has_chat and has_embed,  # 两个模型都在才算就绪
        "detail": "" if has_chat and has_embed else "缺少生成或嵌入模型",
        "models": models,  # 便于 ready 接口展示
    }
