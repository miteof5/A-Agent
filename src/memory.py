"""短期记忆压缩层（滚动摘要）：三层记忆 → 摘要块文本 + 压缩触发。

用户拍板的分层方案（窗口=最近 N 轮完整原文，默认 5）：
- 第 1 层：最近 N 轮完整原文（细节最全，直接进 messages）
- 第 2 层：逐轮 Q+A 摘要区（规则提取"用户提问+最终结论"，零成本，攒满 chunk 条触发合并）
- 第 3 层：LLM 段落摘要区（每 chunk=10 轮一条，从 events 全量原文总结，存 events 表
  `memory/summary` 事件，重启不丢）

压缩触发时机：续聊时（上一轮完成、下一轮开始前）检查"未覆盖轮次" ≥ chunk →
取最老 chunk 轮的**完整原文**调 LLM 总结 → 存 memory/summary → 之后这些轮次只以
第 3 层摘要形式进入上下文，不再重复上传原文。

events 表始终全量保留原文——压缩只影响"上传什么"，不删任何数据。
"""

from __future__ import annotations

import json
import logging

from .config import Config

logger = logging.getLogger(__name__)

SUMMARY_EVENT = "memory/summary"


# ---- 工具：从 events 里取"未被摘要覆盖"的轮次信息 ----

def _summaries(events: list[dict]) -> list[dict]:
    """按 seq 顺序返回 memory/summary 事件（payload 已含 turn_start/turn_end/text）。"""
    return [e for e in events if e["type"] == SUMMARY_EVENT]


def _covered_turns(events: list[dict]) -> set[int]:
    """已被 LLM 摘要覆盖的轮次集合（这些轮不再生成逐轮 Q+A、也不进完整窗口）。"""
    covered: set[int] = set()
    for e in _summaries(events):
        p = e["payload"]
        covered.update(range(int(p["turn_start"]), int(p["turn_end"]) + 1))
    return covered


def _all_turns(events: list[dict]) -> list[int]:
    """非摘要事件的轮次（升序去重）。"""
    turns = {
        e["payload"].get("turn")
        for e in events
        if e["type"] != SUMMARY_EVENT and e["payload"].get("turn") is not None
    }
    return sorted(turns)


def _window_start(events: list[dict], config: Config) -> int:
    """完整窗口起点：最近 memory_full_turns 轮完整保留。"""
    turns = _all_turns(events)
    if not turns:
        return 0
    return max(turns[0], turns[-1] - config.memory_full_turns + 1)


# ---- 第 2 层：逐轮 Q+A（规则提取，零 LLM 成本）----

def _qa_of_turn(events: list[dict], turn: int) -> tuple[str | None, str | None]:
    question = result = None
    for e in events:
        p = e["payload"]
        if p.get("turn") != turn:
            continue
        if e["type"] == "agent/user" and question is None:
            question = p.get("content")
        elif e["type"] == "agent/done" and result is None:
            result = p.get("result")
        elif e["type"] == "agent/error" and result is None:
            result = "出错：" + str(p.get("message") or "")
    return question, result


# ---- 第 3 层原料：把一批轮次的完整原文渲染成紧凑文本（喂给 LLM 总结）----

def _render_turns(events: list[dict], turns: set[int]) -> str:
    lines: list[str] = []
    cur: int | None = None
    for e in events:
        p = e["payload"]
        turn = p.get("turn")
        if turn is None or e["type"] == SUMMARY_EVENT or turn not in turns:
            continue
        if turn != cur:
            cur = turn
            lines.append(f"[第{turn + 1}轮]")
        t = e["type"]
        if t == "agent/user":
            lines.append(f"  用户: {p.get('content') or ''}")
        elif t == "agent/thought":
            lines.append(f"  思考: {p.get('text') or ''}")
        elif t == "tools/call":
            args = json.dumps(p.get("arguments") or {}, ensure_ascii=False)
            lines.append(f"  调用: {p.get('name')}({args})")
        elif t == "tools/result":
            lines.append(f"  结果: ok={p.get('ok')} 内容={(p.get('content') or '')[:120]}")
        elif t == "agent/done":
            lines.append(f"  结论: {p.get('result') or ''}")
        elif t == "agent/error":
            lines.append(f"  错误: {p.get('message') or ''}")
    return "\n".join(lines)


# ---- 压缩触发（续聊入口调用）----

def maybe_compact(storage, llm, task_id: str, config: Config) -> dict | None:
    """检查未覆盖轮次是否攒满 chunk 个：是 → 取最老 chunk 轮完整原文 LLM 总结并落库。

    返回压缩结果 payload（含 turn_start/turn_end/text/input_tokens/output_tokens），
    未触发返回 None。
    """
    if not hasattr(llm, "summarize"):
        return None
    events = storage.list_events(task_id)
    if not events:
        return None
    covered = _covered_turns(events)
    turns = _all_turns(events)
    if not turns:
        return None
    full_start = _window_start(events, config)
    candidates = [t for t in turns if t < full_start and t not in covered]
    if len(candidates) < config.memory_summary_chunk:
        return None
    batch = candidates[: config.memory_summary_chunk]
    history_text = _render_turns(events, set(batch))
    summary, usage = llm.summarize(history_text, max_chars=config.memory_summary_chars)
    payload = {
        "turn_start": batch[0],
        "turn_end": batch[-1],
        "text": summary,
        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
    }
    storage.create_event_next(task_id, SUMMARY_EVENT, payload)
    logger.info(
        "记忆压缩 task_id=%s turns=%d-%d input_tokens=%d output_tokens=%d",
        task_id, batch[0], batch[-1], payload["input_tokens"], payload["output_tokens"],
    )
    return payload


# ---- 摘要块文本（第 2、3 层 → 1 条 system 消息）----

def build_memory_block(events: list[dict], config: Config) -> str | None:
    """组装摘要块：LLM 段落摘要 + 逐轮 Q+A，打包成一段 system 消息文本。

    规则：完整窗口（最近 N 轮）与已覆盖轮次之外的轮次 → 逐轮 Q+A；
    已覆盖轮次 → 只出现一次 LLM 摘要（不重复逐轮）。
    """
    turns = _all_turns(events)
    if not turns:
        return None
    full_start = _window_start(events, config)
    covered = _covered_turns(events)
    parts: list[str] = []
    # 第 3 层：LLM 段落摘要（按轮次顺序，每条一段）
    for e in _summaries(events):
        p = e["payload"]
        parts.append(f"[第{p['turn_start'] + 1}~{p['turn_end'] + 1}轮摘要] {p['text']}")
    # 第 2 层：逐轮 Q+A（窗口外且未被覆盖）
    for t in turns:
        if t >= full_start or t in covered:
            continue
        q, r = _qa_of_turn(events, t)
        if q is not None or r is not None:
            parts.append(f"[第{t + 1}轮] 用户: {q} → 结果: {r}")
    if not parts:
        return None
    return "以下是更早轮次的摘要（历史背景，不是当前对话）：\n" + "\n".join(parts)
