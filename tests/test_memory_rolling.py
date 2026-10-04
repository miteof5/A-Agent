"""二级滚动摘要测试（S5.x）：摘要条数上限、最老 2 条合并、渲染去重、重试退避、失败兜底。

场景：
- 摘要上限 5 条（AA_MEMORY_SUMMARY_MAX_COUNT=5）
- 超过上限 → 先把最老 2 条摘要合并成 1 条粗摘要（覆盖并集），再压新 chunk
- 合并/压缩 LLM 调用失败 → 指数退避重试（测试中 retry_delay=0 不阻塞）；重试耗尽 → 本次放弃返回 None，绝不抛异常
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Config
from src.memory import _covered_turns, _effective_summaries, _summaries, build_memory_block, maybe_compact
from src.rebuild import rebuild_messages
from src.storage import Storage
from tests.test_memory import FakeLLM, build_task  # 复用事件构造与基础 FakeLLM


class FlakyLLM(FakeLLM):
    """前 fail_times 次 summarize 抛异常，之后正常（模拟网络抖动/额度问题）。"""

    def __init__(self, fail_times: int = 0):
        super().__init__()
        self.fail_times = fail_times
        self.attempts = 0

    def summarize(self, history_text: str, max_chars: int = 400):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError(f"模拟 LLM 失败（第 {self.attempts} 次尝试）")
        return super().summarize(history_text, max_chars)


# 上限 5、重试 2 次、退避 0s（测试不阻塞）
CFG2 = Config(
    memory_full_turns=5,
    memory_summary_chunk=10,
    memory_summary_chars=400,
    memory_summary_max_count=5,
    memory_summary_retries=2,
    memory_summary_retry_delay=0,
)


def _db_summaries(storage, tid):
    return [e for e in storage.list_events(tid) if e["type"] == "memory/summary"]


def test_second_level_cap_and_merge(tmp: Path):
    """[1/3] 75 轮长会话：摘要条数恒 ≤5、覆盖 0~69 完整、渲染只显示最粗摘要"""
    print("[1/3] 75 轮 → 二级合并：摘要 ≤5 条、覆盖完整、渲染去重")
    storage, tid = build_task(tmp, 75)  # turn0~74
    llm = FakeLLM()
    # 连续续聊触发压缩（每次 rebuild 压一批最老未覆盖轮次）
    for _ in range(20):
        rebuild_messages(storage, {}, tid, CFG2, llm)
    summaries = _db_summaries(storage, tid)
    # 物理条数随 append-only 增长，恒 ≤上限 的是"有效摘要数"（未被更大范围覆盖）
    effective = _effective_summaries(storage.list_events(tid))
    assert len(effective) <= CFG2.memory_summary_max_count, [s["payload"] for s in summaries]
    # 覆盖范围完整（0~69 全覆盖；窗口内 70~74 不压）
    covered = _covered_turns(storage.list_events(tid))
    assert covered == set(range(70)), covered
    # 渲染去重：渲染段数 == 有效摘要数（旧细摘要不重复出现）
    block = build_memory_block(storage.list_events(tid), CFG2) or ""
    rendered = [s for s in summaries if f"[第{s['payload']['turn_start'] + 1}~{s['payload']['turn_end'] + 1}轮摘要]" in block]
    assert len(rendered) == len(effective), (rendered, effective)
    # 粗摘要已覆盖的最老细摘要文本不再渲染
    assert "LLM摘要-1" not in block, block
    assert "[第1~10轮摘要]" not in block, block
    storage.close()
    print(f"    ✅ 有效摘要 {len(effective)} 条 ≤5；覆盖 0~69；渲染 {len(rendered)} 段无重复细摘要")


def test_merge_retry_success(tmp: Path):
    """[2/3] 合并/压缩调用失败 1 次后重试成功：maybe_compact 正常返回，不抛异常"""
    print("[2/3] LLM 失败 1 次 → 重试成功")
    storage, tid = build_task(tmp, 75)
    llm = FlakyLLM(fail_times=1)
    payload = maybe_compact(storage, llm, tid, CFG2)
    assert payload is not None, "重试后应成功"
    assert llm.attempts == 2, llm.attempts  # 1 次失败 + 1 次成功
    storage.close()
    print(f"    ✅ 失败 1 次后重试成功（attempts={llm.attempts}），payload.turn_start={payload['turn_start']}")


def test_compact_all_fail_graceful(tmp: Path):
    """[3/3] 重试耗尽全部失败：maybe_compact 返回 None 不抛异常（续聊绝不 500）"""
    print("[3/3] 重试耗尽 → 放弃本次压缩，不抛异常")
    storage, tid = build_task(tmp, 75)
    llm = FlakyLLM(fail_times=99)
    try:
        payload = maybe_compact(storage, llm, tid, CFG2)
    except Exception as e:
        raise AssertionError(f"压缩失败不应抛出：{e}")
    assert payload is None, payload
    # 摘要条数不变（本次未落库），下次续聊再试
    assert len(_db_summaries(storage, tid)) == 0
    storage.close()
    print("    ✅ 重试 3 次（attempts=3）全败 → 返回 None，无异常，未落库")


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="aa_mem2_", ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        test_second_level_cap_and_merge(tmp)
        test_merge_retry_success(tmp)
        test_compact_all_fail_graceful(tmp)
    print("\n✅ 全部通过：二级滚动摘要（上限/合并/去重/重试/失败兜底）可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
