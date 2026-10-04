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
import time

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

def _call_with_retry(fn, config: Config):
    """压缩 LLM 调用重试：指数退避（2s→4s，共重试 2 次，最大等待 6s），重试耗尽后抛异常（由调用方兜底）。

    同步等待只在真失败时发生（正常一次成功零延迟）；重试次数/退避基数可配置。
    """
    delay = config.memory_summary_retry_delay
    for attempt in range(config.memory_summary_retries + 1):
        try:
            return fn()
        except Exception:
            if attempt >= config.memory_summary_retries:
                raise
            logger.warning(
                "记忆压缩 LLM 调用失败，%.0fs 后重试（第 %d/%d 次）",
                delay, attempt + 1, config.memory_summary_retries,
            )
            time.sleep(delay)
            delay *= 2


def _effective_summaries(events: list[dict]) -> list[dict]:
    """未被更大范围覆盖的摘要（有效摘要，按 seq 升序）。

    二级合并后旧细摘要被粗摘要吞并（覆盖范围包含），物理上仍在 events 表（append-only），
    但逻辑上已失效——计数/合并/渲染一律只看有效摘要，避免重复合并旧细摘要。
    """
    summaries = _summaries(events)
    kept: list[dict] = []
    for s in summaries:
        p = s["payload"]
        rng = (p["turn_start"], p["turn_end"])
        superseded = any(
            o["payload"]["turn_start"] <= rng[0] and o["payload"]["turn_end"] >= rng[1]
            for o in summaries
            if o is not s
        )
        if not superseded:
            kept.append(s)
    # 按覆盖范围起始排序：合并取"最老有效摘要"、渲染按轮次顺序，都与创建顺序（seq）无关
    kept.sort(key=lambda s: s["payload"]["turn_start"])
    return kept


def _merge_oldest_summaries(storage, llm, task_id: str, events: list[dict], config: Config) -> dict | None:
    """二级压缩：把最老 2 条**有效**摘要合并成 1 条粗摘要（覆盖两者轮次并集），腾出摘要位置。

    - 合并同样是 LLM 调用（带重试），输入为 2 段摘要文本，输出仍 ≤ max_chars 字
    - 合并后新建 1 条 memory/summary 事件（覆盖 [a, d]）；旧细摘要不删（events append-only），
      渲染时按"覆盖范围去重"跳过被更大范围包含的旧摘要
    - 合并失败（重试耗尽）返回 None——调用方放弃本次压缩，下次续聊再试
    """
    kept = _effective_summaries(events)
    if len(kept) < 2:
        return None
    old1, old2 = kept[0], kept[1]  # 有效摘要按 seq 升序，最老两条
    p1, p2 = old1["payload"], old2["payload"]
    combined_text = (
        f"[第{p1['turn_start'] + 1}~{p1['turn_end'] + 1}轮摘要] {p1['text']}\n"
        f"[第{p2['turn_start'] + 1}~{p2['turn_end'] + 1}轮摘要] {p2['text']}"
    )
    summary, usage = _call_with_retry(
        lambda: llm.summarize(combined_text, max_chars=config.memory_summary_chars),
        config,
    )
    payload = {
        "turn_start": p1["turn_start"],
        "turn_end": p2["turn_end"],
        "text": summary,
        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        "merged": True,  # 二级摘要标记（日志/审计用；渲染去重按覆盖范围判断，不依赖本标记）
    }
    storage.create_event_next(task_id, SUMMARY_EVENT, payload)
    logger.info(
        "记忆摘要合并 task_id=%s turns=%d-%d（粗摘要，源 2 条，input_tokens=%d output_tokens=%d）",
        task_id, payload["turn_start"], payload["turn_end"],
        payload["input_tokens"], payload["output_tokens"],
    )
    return payload


def maybe_compact(storage, llm, task_id: str, config: Config) -> dict | None:
    """检查未覆盖轮次是否攒满 chunk 个：是 → 取最老 chunk 轮完整原文 LLM 总结并落库。

    二级压缩（摘要条数上限）：摘要已达 memory_summary_max_count 条 → 先把最老 2 条
    合并成粗摘要腾位置，再压新 chunk → 摘要条数恒 ≤ 上限。

    返回压缩结果 payload（含 turn_start/turn_end/text/input_tokens/output_tokens），
    未触发返回 None。任何失败（含重试耗尽）→ 记日志返回 None，绝不影响续聊主流程。
    """
    if not hasattr(llm, "summarize"):
        return None
    try:
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
        # 二级压缩：有效摘要条数已达上限 → 先合并最老 2 条（腾位置）；合并失败则本次放弃
        if len(_effective_summaries(events)) >= config.memory_summary_max_count:
            if _merge_oldest_summaries(storage, llm, task_id, events, config) is None:
                logger.warning("记忆摘要合并失败 task_id=%s（跳过本次压缩，下次再试）", task_id)
                return None
            events = storage.list_events(task_id)  # 刷新（已新增粗摘要）
        batch = candidates[: config.memory_summary_chunk]
        history_text = _render_turns(events, set(batch))
        summary, usage = _call_with_retry(
            lambda: llm.summarize(history_text, max_chars=config.memory_summary_chars),
            config,
        )
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
    except Exception:
        logger.exception("记忆压缩失败 task_id=%s（跳过，不影响续聊）", task_id)
        return None


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
    # 第 3 层：LLM 段落摘要（按轮次顺序，每条一段；只渲染有效摘要——二级合并后旧细摘要被粗摘要覆盖跳过）
    for s in _effective_summaries(events):
        p = s["payload"]
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
