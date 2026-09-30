"""RepeatGuard 防死循环测试（S5.1）：FakeLLM 模拟死循环，无需真实 API Key。

运行（项目根目录下）：
    python tests/test_repeat_guard.py

覆盖：
1. 同工具同参数死循环：soft 纠偏 2 次 → hard 拦截 → 任务 failed（不烧完步数）
2. 模型收到纠偏后换方法：soft 有效 → 任务正常 done（不误杀）
3. 通道 B（结果相同）：不同参数但结果相同 → 纠偏文本附加到工具结果，任务仍 done
4. 阈值可配置（soft/hard 由 Config 传入）

注：Reactor 当前签名是 Reactor(llm, ctx)（S2.4 微内核化后），
测试直接构造 AgentContext + 插件，不走后台线程（单线程即任务，thread-local 隔离天然成立）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Config
from src.kernel.plugin import AgentContext
from src.llm_client import LLMCall
from src.models import TaskState
from src.plugins.repeat_guard_plugin import RepeatGuardPlugin
from src.plugins.tool_registry import ToolRegistryPlugin
from src.reactor import Reactor
from src.storage import Storage
from src.tools.file_view import FileViewTool


class RecordingFakeLLM:
    """按脚本顺序返回预设响应，并记录每次收到的 messages（断言纠偏文本是否喂回）。"""

    def __init__(self, script: list[LLMCall]):
        self._script = list(script)
        self.calls = 0
        self.messages_seen: list[list[dict]] = []

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMCall:
        self.calls += 1
        self.messages_seen.append([dict(m) for m in messages])
        return self._script.pop(0)


def build(tmp: Path, script: list[LLMCall], soft: int = 3, hard: int = 3):
    """构造 微内核（工具+RepeatGuard）+ Reactor。返回 (storage, reactor, llm)。"""
    config = Config(
        llm_api_key="fake-key",
        db_path=str(tmp / "rg.db"),
        max_steps_per_turn=20,
        repeat_soft_limit=soft,
        repeat_hard_limit=hard,
    )
    storage = Storage(config.db_path)
    ctx = AgentContext(config=config, storage=storage)
    ctx.plugins.register(ToolRegistryPlugin({"file_view": FileViewTool(max_bytes=4096)}))
    ctx.plugins.register(RepeatGuardPlugin(soft_limit=soft, hard_limit=hard))
    ctx.start()
    llm = RecordingFakeLLM(script)
    reactor = Reactor(llm, ctx)
    return storage, reactor, llm


def same_call(name: str, args: dict) -> LLMCall:
    return LLMCall(text="继续尝试。", tool_calls=[{"id": name, "name": "file_view", "arguments": args}])


def test_same_call_loop(tmp: Path):
    """[1/4] 同工具同参数死循环 → soft×2 → hard → failed"""
    print("[1/4] 同工具同参数死循环")
    f = tmp / "loop.txt"
    f.write_text("内容 X", encoding="utf-8")
    args = {"path": str(f)}
    script = [same_call(f"c{i}", args) for i in range(1, 6)]  # 5 次完全相同调用
    storage, reactor, llm = build(tmp, script)

    task = storage.create_task("重复读同一个文件")
    result = reactor.run(task.task_id, task.content)

    t = storage.get_task(task.task_id)
    assert t is not None and t.state == TaskState.FAILED, t
    assert "重复死循环" in (t.error or ""), t.error

    steps = storage.list_steps(task.task_id)
    # 1/2 步真实执行（repeats=1,2）→ 3/4 步 soft 拦截（repeats=3,4）→ 第 5 次 hard 终止（不落 steps）
    assert len(steps) == 4, steps
    assert steps[0]["status"] == "succeeded" and steps[0]["tool_name"] == "file_view"
    assert steps[1]["status"] == "succeeded"
    assert steps[2]["status"] == "failed" and "[RepeatGuard]" in (steps[2]["tool_result"] or {}).get("error", ""), steps[2]
    assert steps[3]["status"] == "failed" and "[RepeatGuard]" in (steps[3]["tool_result"] or {}).get("error", ""), steps[3]

    # 纠偏文本确实喂回了 LLM（messages 里出现过）
    all_msgs = [m["content"] for seen in llm.messages_seen for m in seen if m.get("role") == "tool"]
    assert any("[RepeatGuard]" in (c or "") for c in all_msgs), "纠偏文本未进入 messages"

    storage.close()
    print(f"    ✅ failed / error={t.error!r} / 步骤数={len(steps)} / 纠偏已喂回 LLM")


def test_soft_corrects(tmp: Path):
    """[2/4] 模型收到纠偏后换方法 → 正常 done（soft 不误杀）"""
    print("[2/4] soft 纠偏后模型换方法")
    fa = tmp / "a.txt"
    fa.write_text("AAA", encoding="utf-8")
    fb = tmp / "b.txt"
    fb.write_text("BBB", encoding="utf-8")
    script = [
        same_call("c1", {"path": str(fa)}),
        same_call("c2", {"path": str(fa)}),   # repeats=2
        same_call("c3", {"path": str(fa)}),   # repeats=3 → soft 拦截
        same_call("c4", {"path": str(fb)}),   # 换参数 → 重置，执行成功
        LLMCall(text="换了个文件读到了不同内容，任务完成。", tool_calls=[]),
    ]
    storage, reactor, llm = build(tmp, script)

    task = storage.create_task("读文件")
    result = reactor.run(task.task_id, task.content)

    t = storage.get_task(task.task_id)
    assert t is not None and t.state == TaskState.DONE, t
    steps = storage.list_steps(task.task_id)
    assert len(steps) == 5, steps  # 4 次工具调用 + 1 条最终答案
    # 换参数后的第 4 步应正常成功、无 RepeatGuard 标记
    assert steps[3]["status"] == "succeeded", steps[3]
    tr = steps[3]["tool_result"] or {}
    assert "[RepeatGuard]" not in (tr.get("error") or ""), tr
    storage.close()
    print(f"    ✅ done / 换方法后正常执行 / 未误杀")


def test_same_result_channel(tmp: Path):
    """[3/4] 通道 B：不同参数但结果相同 → 附加纠偏，任务仍 done"""
    print("[3/4] 同结果通道（参数不同）")
    same_content = "完全相同的输出"
    for n in ("x1.txt", "x2.txt", "x3.txt"):
        (tmp / n).write_text(same_content, encoding="utf-8")
    script = [
        same_call("c1", {"path": str(tmp / "x1.txt")}),  # result hash X（result_repeats=1）
        same_call("c2", {"path": str(tmp / "x2.txt")}),  # 参数不同→通道A放行；结果同→result_repeats=2
        same_call("c3", {"path": str(tmp / "x3.txt")}),  # result_repeats=3 → soft 附加纠偏
        LLMCall(text="三个文件内容相同，任务完成。", tool_calls=[]),
    ]
    storage, reactor, llm = build(tmp, script)

    task = storage.create_task("读三个文件")
    result = reactor.run(task.task_id, task.content)

    t = storage.get_task(task.task_id)
    assert t is not None and t.state == TaskState.DONE, t
    steps = storage.list_steps(task.task_id)
    assert len(steps) == 4, steps
    # 第 3 次调用（第三个文件）应带纠偏文本
    tr = steps[2]["tool_result"] or {}
    assert "[RepeatGuard]" in (tr.get("error") or ""), tr
    assert tr.get("ok") is True, tr  # 工具本身执行成功，只是附加提示
    storage.close()
    print("    ✅ done / 第 3 次结果附加纠偏文本 / 未中断任务")


def test_configurable_threshold(tmp: Path):
    """[4/4] 阈值可配置：soft=2/hard=2 → 更快触发"""
    print("[4/4] 阈值配置生效")
    f = tmp / "t.txt"
    f.write_text("内容", encoding="utf-8")
    args = {"path": str(f)}
    script = [
        same_call("c1", args),  # repeats=1 放行
        same_call("c2", args),  # repeats=2 → soft（soft_hits=1）
        same_call("c3", args),  # repeats=3 → soft（soft_hits=2 >= hard=2）→ hard
    ]
    storage, reactor, _ = build(tmp, script, soft=2, hard=2)

    task = storage.create_task("重复读文件（阈值2）")
    reactor.run(task.task_id, task.content)

    t = storage.get_task(task.task_id)
    assert t is not None and t.state == TaskState.FAILED, t
    assert "重复死循环" in (t.error or ""), t.error
    storage.close()
    print("    ✅ soft=2/hard=2 生效（2 次即触发）")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="aa_rg_", ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        test_same_call_loop(tmp)
        test_soft_corrects(tmp)
        test_same_result_channel(tmp)
        test_configurable_threshold(tmp)
    print("\n✅ 全部通过：RepeatGuard 软检测 + hard 兜底 + 阈值配置 可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
