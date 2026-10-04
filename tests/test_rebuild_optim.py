"""rebuild 优化测试（A 定向读取 + B 写入时物化 Q+A）。

A：storage 定向查询（types/min_turn/max_turn/turn_range/all_turns/last_user_content）+
    (task_id, turn) 表达式索引——rebuild 不再全量拉历史
B：agent/done 写入时预存 question → _qa_of_turn 直接命中（不依赖 user 事件遍历）；
   老数据无预存 → 兜底现算
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Config
from src.memory import _qa_of_turn
from src.storage import Storage
from tests.test_memory import build_task


def test_storage_directed_queries(tmp: Path):
    """[1/4] 定向查询：types / min_turn / max_turn / turn_range / all_turns / last_user_content"""
    print("[1/4] storage 定向查询（A：不再全量拉历史）")
    storage, tid = build_task(tmp, 12)  # turn0~11
    # 摘要事件定向取
    storage.create_event_next(tid, "memory/summary", {"turn_start": 0, "turn_end": 9, "text": "s"})
    summaries = storage.list_events(tid, types=["memory/summary"])
    assert len(summaries) == 1 and summaries[0]["type"] == "memory/summary"
    # 窗口轮定向取（min_turn）——非 summary 事件按 turn 过滤，summary 无 turn 自动排除
    window = storage.list_events(tid, min_turn=10)
    assert window and all(e["payload"].get("turn", -1) >= 10 for e in window)
    assert all(e["type"] != "memory/summary" for e in window)
    # max_turn 配合
    batch = storage.list_events(tid, min_turn=0, max_turn=9)
    assert batch and all(0 <= e["payload"].get("turn", -1) <= 9 for e in batch)
    # 聚合
    assert storage.turn_range(tid) == (0, 11)
    assert storage.all_turns(tid) == list(range(12))
    assert storage.last_user_content(tid) == "问题11"
    # 表达式索引已建
    idx = storage._fetchone(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_events_task_turn'"
    )
    assert idx is not None, "缺 (task_id, turn) 表达式索引"
    storage.close()
    print("    ✅ types/min_turn/max_turn 过滤 + 聚合 + 索引 全部正确")


def test_qa_prestored_question(tmp: Path):
    """[2/4] B：done 预存 question → _qa_of_turn 直接命中（无 user 事件也能取到）"""
    print("[2/4] 预存 Q+A：done.question 直接命中")
    storage, tid = build_task(tmp, 2)
    # 模拟新数据：done 落库时已带 question（reactor emit 行为），集合里故意不放 user
    events = [
        {"type": "agent/done", "payload": {"turn": 1, "result": "结论1", "question": "问题1"}},
    ]
    q, r = _qa_of_turn(events, 1)
    assert (q, r) == ("问题1", "结论1"), (q, r)
    storage.close()
    print("    ✅ 直接命中（q=问题1 r=结论1），不依赖 user 事件")


def test_qa_fallback_old_data(tmp: Path):
    """[3/4] 老数据无预存 → 兜底现算（user/done 遍历）"""
    print("[3/4] 老数据兜底：无 question 现算")
    storage, tid = build_task(tmp, 3)
    events = storage.list_events(tid, types=["agent/user", "agent/done", "agent/error"])
    q, r = _qa_of_turn(events, 1)
    assert q == "问题1" and r == "结论1", (q, r)
    storage.close()
    print("    ✅ 兜底现算正确（问题1 → 结论1）")


def test_rebuild_with_directed_events(tmp: Path):
    """[4/4] rebuild 定向子集 + 预存 Q+A：摘要块与完整窗口不变（回归）"""
    print("[4/4] rebuild 定向读取后行为不变（Q+A 区块正确）")
    from src.rebuild import rebuild_messages

    storage, tid = build_task(tmp, 17)  # turn0~16：LLM摘要(0~9) + turn10~11 Q+A + turn12~16 完整
    llm = _FakeLLM()
    messages = rebuild_messages(storage, {}, tid, CFG, llm)
    block = " ".join(m["content"] for m in messages if m["role"] == "system")
    assert "LLM摘要-1" in block, block
    assert "[第11轮] 用户: 问题10 → 结果: 结论10" in block, block  # turn10 Q+A
    assert "[第12轮] 用户: 问题11 → 结果: 结论11" in block, block  # turn11 Q+A
    users = [m["content"] for m in messages if m["role"] == "user"]
    assert users == [f"问题{t}" for t in range(12, 17)], users  # 窗口 12~16 完整
    storage.close()
    print("    ✅ 摘要块 + turn10~11 Q+A + 窗口 12~16 完整，行为与全量读一致")


class _FakeLLM:
    def __init__(self):
        self.model = "fake"

    def summarize(self, history_text: str, max_chars: int = 400):
        self._calls = getattr(self, "_calls", 0) + 1
        return f"LLM摘要-{self._calls}", {"prompt_tokens": 100, "completion_tokens": 10}


CFG = Config(memory_full_turns=5, memory_summary_chunk=10, memory_summary_chars=400)


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="aa_reopt_", ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        test_storage_directed_queries(tmp)
        test_qa_prestored_question(tmp)
        test_qa_fallback_old_data(tmp)
        test_rebuild_with_directed_events(tmp)
    print("\n✅ 全部通过：rebuild 优化（A 定向读取 + B 预存 Q+A）可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
