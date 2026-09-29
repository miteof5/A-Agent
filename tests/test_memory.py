"""滚动记忆压缩测试：三层记忆时序推演（无需真实 Key，FakeLLM 模拟总结）。

用户拍板方案：
- 完整窗口：最近 5 轮（AA_MEMORY_FULL_TURNS=5）
- 逐轮 Q+A：窗口外轮次规则提取（用户提问+结论），攒满 10 条（CHUNK）触发 LLM 总结
- LLM 总结：取最老 10 轮**完整原文**总结，存 events 表 memory/summary 事件
- 重建结构：system prompt + 摘要块（第 2、3 层，1 条 system 消息）+ 最近 5 轮完整

时序断言：
1. 6 轮历史 → 重建 = turn1 Q+A 摘要 + turn1~5 完整（不触发总结）
2. 15 轮历史 + llm → 触发一次总结（turn0~9 原文）→ 重建 = LLM摘要(0~9) + turn10~15 完整
3. 16 轮完成 → 重建 = LLM摘要(0~9) + turn10 Q+A + turn11~15 完整
4. 26 轮完成 → 触发第二批总结（turn10~19）→ 重建 = 两个 LLM 摘要 + turn20 Q+A + turn21~25 完整
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Config
from src.memory import _all_turns, _covered_turns, maybe_compact
from src.rebuild import rebuild_messages
from src.storage import Storage


class FakeLLM:
    """模拟 LLMClient：summarize 记录调用历史，返回固定摘要。"""

    def __init__(self):
        self.model = "fake-model"
        self.summarize_calls: list[str] = []

    def summarize(self, history_text: str, max_chars: int = 400):
        self.summarize_calls.append(history_text)
        return f"LLM摘要-{len(self.summarize_calls)}", {
            "prompt_tokens": 1000 * len(history_text),  # 示意：原文越长输入越多
            "completion_tokens": 50,
        }

    def chat(self, messages, tools=None):
        from src.llm_client import LLMCall

        return LLMCall(text="ok")

    def chat_stream(self, messages, tools=None, on_delta=None, should_stop=None):
        from src.llm_client import LLMCall

        return LLMCall(text="ok")


CFG = Config(memory_full_turns=5, memory_summary_chunk=10, memory_summary_chars=400)


def build_task(tmp: Path, n_turns: int, storage=None):
    """构造一个含 n 轮完整事件（user+thought+done）的任务。返回 (storage, task_id)。"""
    if storage is None:
        storage = Storage(str(tmp / "mem.db"))
    task = storage.create_task("多轮任务")
    for t in range(n_turns):
        storage.create_event_next(task.task_id, "agent/user", {"turn": t, "content": f"问题{t}"})
        storage.create_event_next(task.task_id, "agent/thought", {"turn": t, "text": f"思考{t}"})
        storage.create_event_next(task.task_id, "agent/done", {"turn": t, "result": f"结论{t}"})
    return storage, task.task_id


def _summary_block(messages: list[dict]) -> str | None:
    """取摘要块（第二条 system 消息）。"""
    systems = [m["content"] for m in messages if m["role"] == "system"]
    return systems[1] if len(systems) > 1 else None


def _user_msgs(messages: list[dict]) -> list[str]:
    return [m["content"] for m in messages if m["role"] == "user"]


def test_window_6_turns(tmp: Path):
    """[1/6] 6 轮历史：turn0 变 Q+A 进摘要块，窗口内 turn1~5 完整；不触发总结"""
    print("[1/6] 6 轮历史 → 摘要块含 turn0 Q+A + turn1~5 完整")
    storage, tid = build_task(tmp, 6)
    messages = rebuild_messages(storage, {}, tid, CFG)  # 无 llm：不压缩
    block = _summary_block(messages)
    assert block and "[第1轮] 用户: 问题0 → 结果: 结论0" in block, block
    assert _user_msgs(messages) == [f"问题{t}" for t in range(1, 6)], _user_msgs(messages)
    storage.close()
    print(f"    ✅ 摘要块含 turn0 Q+A；完整窗口=问题1~5")


def test_no_compact_under_chunk(tmp: Path):
    """[2/6] 未攒满 10 条不触发总结"""
    print("[2/6] 6 轮 + llm：不触发总结")
    storage, tid = build_task(tmp, 6)
    llm = FakeLLM()
    rebuild_messages(storage, {}, tid, CFG, llm)
    assert llm.summarize_calls == [], llm.summarize_calls
    storage.close()
    print("    ✅ summarize 未被调用")


def test_compact_at_15(tmp: Path):
    """[3/6] 15 轮 + llm：触发一次总结（最老 10 轮原文），落库 memory/summary"""
    print("[3/6] 15 轮 → 触发总结 turn0~9 原文")
    storage, tid = build_task(tmp, 15)
    llm = FakeLLM()
    messages = rebuild_messages(storage, {}, tid, CFG, llm)
    assert len(llm.summarize_calls) == 1, llm.summarize_calls
    # 总结原料 = turn0~9 的完整原文
    raw = llm.summarize_calls[0]
    assert "问题0" in raw and "问题9" in raw and "结论9" in raw, raw[:200]
    # 落库
    events = storage.list_events(tid)
    summaries = [e for e in events if e["type"] == "memory/summary"]
    assert len(summaries) == 1, summaries
    assert summaries[0]["payload"]["turn_start"] == 0 and summaries[0]["payload"]["turn_end"] == 9
    # 重建：摘要块 = LLM摘要段（turn0~9 不再逐轮 Q+A）+ 完整窗口 turn10~14
    block = _summary_block(messages)
    assert block and "LLM摘要-1" in block, block
    assert "[第1轮] 用户" not in block, block  # 已覆盖轮次不再逐轮 Q+A
    assert _user_msgs(messages) == [f"问题{t}" for t in range(10, 15)], _user_msgs(messages)
    storage.close()
    print(f"    ✅ 1 次总结（{raw[:40]!r}…）；摘要块=LLM段；完整窗口=问题10~14")


def test_layer2_qa_after_compact(tmp: Path):
    """[4/6] 压缩后第 16 轮完成：turn10 被挤出 → 逐轮 Q+A + LLM摘要 + turn11~15 完整"""
    print("[4/6] 第 17 轮：LLM摘要(0~9) + turn10 Q+A + turn11~15 完整")
    storage, tid = build_task(tmp, 15)
    llm = FakeLLM()
    rebuild_messages(storage, {}, tid, CFG, llm)  # 触发压缩 turn0~9
    # 第 16 轮执行完（turn15）
    storage.create_event_next(tid, "agent/user", {"turn": 15, "content": "问题15"})
    storage.create_event_next(tid, "agent/thought", {"turn": 15, "text": "思考15"})
    storage.create_event_next(tid, "agent/done", {"turn": 15, "result": "结论15"})
    messages = rebuild_messages(storage, {}, tid, CFG)  # 不再触发（candidates 只有 turn10 < 10）
    block = _summary_block(messages)
    assert block, block
    assert "LLM摘要-1" in block
    assert "[第11轮] 用户: 问题10 → 结果: 结论10" in block, block  # 逐轮 Q+A
    assert _user_msgs(messages) == [f"问题{t}" for t in range(11, 16)], _user_msgs(messages)
    storage.close()
    print(f"    ✅ 摘要块 = LLM摘要 + turn10 Q+A；完整窗口=问题11~15")


def test_two_summaries_at_27(tmp: Path):
    """[5/6] 第 27 轮：两个 LLM 摘要段 + turn20 Q+A + turn21~25 完整"""
    print("[5/6] 第 27 轮：LLM(0~9) + LLM(10~19) + turn20 Q+A + turn21~25 完整")
    storage, tid = build_task(tmp, 26)  # 第 26 轮完成 = turn0~25
    llm = FakeLLM()
    messages = rebuild_messages(storage, {}, tid, CFG, llm)  # 第 26 轮完成时触发第一批？不——
    # 第一批：candidates=turn0~19（20 个）→ 只压最老 10 个 turn0~9
    assert len(llm.summarize_calls) == 1
    events = storage.list_events(tid)
    assert len([e for e in events if e["type"] == "memory/summary"]) == 1
    # 再触发一次（模拟下一次续聊）：第二批 turn10~19
    messages = rebuild_messages(storage, {}, tid, CFG, llm)
    assert len(llm.summarize_calls) == 2, llm.summarize_calls
    summaries = [e for e in storage.list_events(tid) if e["type"] == "memory/summary"]
    assert len(summaries) == 2
    assert (summaries[0]["payload"]["turn_start"], summaries[0]["payload"]["turn_end"]) == (0, 9)
    assert (summaries[1]["payload"]["turn_start"], summaries[1]["payload"]["turn_end"]) == (10, 19)
    block = _summary_block(messages)
    assert block and "LLM摘要-1" in block and "LLM摘要-2" in block, block
    assert "[第21轮] 用户: 问题20 → 结果: 结论20" in block, block  # turn20 逐轮 Q+A
    assert _user_msgs(messages) == [f"问题{t}" for t in range(21, 26)], _user_msgs(messages)
    storage.close()
    print(f"    ✅ 两个 LLM 摘要段 + turn20 Q+A；完整窗口=问题21~25")


def test_helpers(tmp: Path):
    """[6/6] 工具函数：_all_turns / _covered_turns"""
    print("[6/6] memory 工具函数")
    storage, tid = build_task(tmp, 12)
    storage.create_event_next(tid, "memory/summary", {"turn_start": 0, "turn_end": 9, "text": "s"})
    events = storage.list_events(tid)
    assert _all_turns(events) == list(range(12)), _all_turns(events)
    assert _covered_turns(events) == set(range(10)), _covered_turns(events)
    storage.close()
    print("    ✅ 轮次提取与覆盖集合正确")


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="aa_mem_", ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        test_window_6_turns(tmp)
        test_no_compact_under_chunk(tmp)
        test_compact_at_15(tmp)
        test_layer2_qa_after_compact(tmp)
        test_two_summaries_at_27(tmp)
        test_helpers(tmp)
    print("\n✅ 全部通过：滚动记忆分层/压缩触发/时序推演 可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
