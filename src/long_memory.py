"""S5 长期记忆：跨会话稳定事实（用户画像/项目事实/用户目标）。

与短期记忆（memory.py，会话内）的分工：
- 短期记忆：某次任务的"工作现场"，events 表全量保留，滚动压缩控制上传量
- 长期记忆：用户的"稳定世界"（跨会话不拆），memory 表存储，任务开始时注入

写入（提炼）：任务完成后异步触发——价值过滤（规则，零成本）→ LLM 提炼
  （user+result+已有记忆摘要 → add/update/skip 候选，一次调用同时完成去重判断）
  → 落库（UNIQUE 兜底去重 / update 合并旧条目 / skip 丢弃）。
  提炼失败/超时只记日志，绝不影响主流程。

读取（注入）：新任务/续聊开始时，按活跃度取 top N 条拼成 system 块注入。
"""

from __future__ import annotations

import logging
import re

from .config import Config

logger = logging.getLogger(__name__)

# 注入块通用格式（机制层）；标题词与分类语义来自 config.long_memory_schema（主题层可插拔）
MIN_INPUT_CHARS = 6     # 价值过滤：用户输入至少这么多字（"你好/谢谢/在吗"这类寒暄跳过）
MIN_RESULT_CHARS = 20   # 价值过滤：未调用工具时，结果至少这么多字（"晴天/好的"这类一句话问答跳过）
MAX_QUERY_TERMS = 8     # 检索注入：单次最多使用的 FTS5 检索词数
QUERY_TERM_MIN = 3      # FTS5 trigram 下限：短于 3 字符的词无索引可查


def extract_query_terms(user_input: str) -> list[str]:
    """从任务输入提取 FTS5 检索词（trigram 需 ≥3 字符）。

    ① 英文/数字标识符（模型名、端口、路径、文件名、URL——长期记忆里最常出现的高信号词）
    ② 连续中文段（≥3 字，整段作为子串 token；匹配偏弱，聊胜于无）
    去重保序，最多 MAX_QUERY_TERMS 个。
    """
    text = user_input or ""
    terms: list[str] = []
    terms += re.findall(r"[A-Za-z0-9][A-Za-z0-9._\-/\\]{2,}", text)
    terms += re.findall(r"[\u4e00-\u9fff]{3,}", text)
    seen, out = set(), []
    for t in terms:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= MAX_QUERY_TERMS:
            break
    return out


def has_extraction_value(user_input: str, result: str, called_tool: bool) -> bool:
    """价值过滤（宁漏勿错）：只有明显无价值的任务才跳过提炼。

    规则（零 LLM 成本，基于任务元数据）：
    ① 用户输入太短（闲聊寒暄）→ 跳过
    ② 既没调用工具、结果又很短（一句话问答）→ 跳过
    其余一律提炼（宁可多花一次调用，不可漏掉有价值记忆）。
    """
    if len((user_input or "").strip()) < MIN_INPUT_CHARS:
        return False
    if not called_tool and len((result or "").strip()) < MIN_RESULT_CHARS:
        return False
    return True


def _latest_user_input(storage, task_id: str) -> str:
    """取该任务最近一轮的 user 输入（续聊多轮取最后一轮）。"""
    for e in reversed(storage.list_events(task_id)):
        if e["type"] == "agent/user":
            return e["payload"].get("content") or ""
    return ""


def extract_and_store(storage, llm, task_id: str, config: Config) -> list[dict] | None:
    """完整提炼流程：价值过滤 → LLM 提炼（含去重判断）→ 落库。

    在后台线程调用（不阻塞主流程）。任何异常只记日志返回 None。
    """
    try:
        task = storage.get_task(task_id)
        if task is None or getattr(task, "state", None) != "done":
            return None
        events = storage.list_events(task_id)
        user_input = _latest_user_input(storage, task_id)
        result = getattr(task, "result", "") or ""
        called_tool = any(e["type"] == "tools/call" for e in events)
        if not has_extraction_value(user_input, result, called_tool):
            logger.info("长期记忆：跳过提炼 task_id=%s（无提炼价值）", task_id)
            return None
        existing = storage.list_memory(limit=20)  # 最近 20 条做去重参照
        existing_brief = [
            {"id": m["id"], "category": m["category"], "content": m["content"]}
            for m in existing
        ]
        schema = config.long_memory_schema or {}
        candidates, usage = llm.extract_memories(
            user_input,
            result,
            existing_brief,
            schema=schema,
            max_items=int(schema.get("max_items") or 3),
            max_chars=config.long_memory_max_chars,
        )
        applied = 0
        for c in candidates or []:
            action = c.get("action")
            try:
                # keywords 规范化：LLM 可能返回 list 或 str，统一为空格分隔字符串
                kw = c.get("keywords") or ""
                if isinstance(kw, list):
                    kw = " ".join(str(x) for x in kw)
                if action == "add":
                    rid = storage.create_memory(
                        c.get("category") or "",
                        c.get("content") or "",
                        keywords=kw,
                        source="auto",
                    )
                    if rid:
                        applied += 1
                elif action == "update" and c.get("target_id"):
                    if storage.update_memory(
                        int(c["target_id"]),
                        content=c.get("content"),
                        keywords=kw if kw else None,
                    ):
                        applied += 1
                # skip：丢弃，不落库
            except Exception:
                logger.exception("长期记忆落库失败 task_id=%s candidate=%r", task_id, c)
        logger.info(
            "长期记忆提炼 task_id=%s candidates=%d applied=%d input_tokens=%s output_tokens=%s",
            task_id, len(candidates or []), applied,
            usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"),
        )
        return candidates
    except Exception:
        logger.exception("长期记忆提炼失败 task_id=%s", task_id)
        return None


def retrieve_memories_for_task(storage, config: Config, user_input: str = "") -> list[dict]:
    """检索器接口（阶段一：FTS5 关键词 + 活跃度兜底）。

    - 有检索词且命中 → bm25 相关优先的命中结果
    - 命中不足注入上限 → 活跃度补足（排除已命中条目）
    - 无信号词 / 零命中 → 退回活跃度 top N（行为不低于旧版）

    阶段二换向量检索时保持本签名不变（storage 层换实现即可）。
    """
    limit = config.long_memory_inject_limit
    terms = extract_query_terms(user_input)
    hits = storage.search_memories(terms, limit=limit) if terms else []
    if len(hits) < limit:
        seen = {m["id"] for m in hits}
        for m in storage.retrieve_memories(limit=limit * 2):
            if len(hits) >= limit:
                break
            if m["id"] not in seen:
                hits.append(m)
    return hits[:limit]


def build_long_memory_block(storage, config: Config, user_input: str = "") -> str | None:
    """构建注入块：检索器取出相关记忆 → 一段 system 文本。

    标题/引导语取自 config.long_memory_schema（主题层可插拔），条目格式为机制层通用格式。
    同时刷新被注入条目的 last_used_at（活跃度排序依据）。
    无记忆 → None（不注入，零开销）。
    """
    rows = retrieve_memories_for_task(storage, config, user_input)
    if not rows:
        return None
    schema = config.long_memory_schema or {}
    name = (schema.get("name") or "长期记忆").strip()
    hint = (schema.get("header_hint") or "").strip()
    header = f"【{name}】（{hint}）：" if hint else f"【{name}】："
    parts = [header]
    for m in rows:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        content = content[: config.long_memory_max_chars]
        parts.append(f"- [{m['category']}] {content}")
        storage.touch_memory(m["id"])
    if len(parts) == 1:
        return None
    return "\n".join(parts)
