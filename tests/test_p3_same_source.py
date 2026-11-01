"""P3「同源」测试 —— 思考与答案分离 Spec §5.6 / §8 P3。

运行（项目根目录下）：
    python tests/test_p3_same_source.py

守的是 P3 的四件事，它们**必须同批上线**，否则中间态就是 400：

    ① 开关：thinking_param 真的下发了（P2 只描述、没启用）
    ② 轮内回传（R1）：同一 turn 内带 tools 的 assistant 消息要把 reasoning 原样带回
    ③ 同源（重建）：rebuild 认 step_end，不再认 thought；done 补 turn 后不得产生第二条相同 assistant
    ④ 跨轮回传（R2/R3）：默认丢弃；标了 must_echo_cross_turn 且开了 thinking 才带

为什么 ③ 必须先于「给 done 补 turn」验证：
done 一旦有了 turn，`list_events` 的 min_turn 过滤就不再把它当 NULL 滤掉，
rebuild 会同时拿到 thought 与 done → 上下文里立刻出现两条同文 assistant。
这条测试就是防止那个回归。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.capabilities import needs_echo, resolve
from src.config import Config
from src.memory import _render_turns
from src.rebuild import rebuild_messages
from src.storage import Storage

GW = "https://fake-gateway.example.com/v1"


# ---------------------------------------------------------------- 测试替身


class FakeChunk:
    def __init__(self, content=None, reasoning_content=None, usage=None):
        delta = SimpleNamespace(content=content, tool_calls=None)
        if reasoning_content is not None:
            delta.reasoning_content = reasoning_content
        self.choices = [SimpleNamespace(delta=delta)]
        self.usage = usage


class FakeOpenAI:
    def __init__(self, chunks):
        self.chunks = chunks
        self.captured: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.captured.append(kwargs)
        return iter(self.chunks)


def make_client(tmp: Path, model: str, chunks, caps: dict, *, thinking: bool = True):
    from src.llm_client import LLMClient

    cap_file = tmp / f"caps_{model}.json"
    cap_file.write_text(json.dumps(caps, ensure_ascii=False), encoding="utf-8")
    cfg = Config(
        llm_api_key="fake-key",
        llm_model=model,
        llm_base_url=GW,
        capabilities_file=str(cap_file),
        thinking_enabled=thinking,
    )
    client = LLMClient(cfg)
    fake = FakeOpenAI(chunks)
    client._client = fake
    return client, fake


def seed_new_task(storage: Storage, *, with_reasoning: bool = True, model: str = "qwen3.8-max") -> str:
    """按 P3 之后的事件形态造一个两轮任务：step_end 取代 thought。"""
    task = storage.create_task("把 a.txt 读一下再总结")
    tid = task.task_id
    reasoning = "用户要看文件，先 file_view。" if with_reasoning else ""
    storage.create_event_next(tid, "agent/user", {"turn": 0, "content": "把 a.txt 读一下再总结"})
    storage.create_event_next(
        tid,
        "agent/step_end",
        {
            "turn": 0, "step": 0, "model": model, "kind": "process",
            "content": "我先读一下。", "reasoning": reasoning,
            "tool_calls": [{"id": "c1", "name": "file_view", "arguments": {"path": "a.txt"}}],
            "finish_reason": "tool_calls",
        },
    )
    storage.create_event_next(tid, "tools/call", {
        "turn": 0, "step": 0, "name": "file_view", "arguments": {"path": "a.txt"}, "tool_call_id": "c1",
    })
    storage.create_event_next(tid, "tools/result", {
        "turn": 0, "step": 0, "ok": True, "content": "内容", "content_summary": "内容",
        "error": None, "is_denied": False, "metadata": {}, "tool_call_id": "c1",
    })
    storage.create_event_next(
        tid,
        "agent/step_end",
        {
            "turn": 0, "step": 1, "model": model, "kind": "answer",
            "content": "文件里只有两个字：内容。", "reasoning": "拿到了，可以回答。",
            "tool_calls": [], "finish_reason": "stop",
        },
    )
    storage.create_event_next(tid, "agent/done", {
        "turn": 0, "step": 1, "result": "文件里只有两个字：内容。", "question": "把 a.txt 读一下再总结",
    })
    return tid


def seed_legacy_task(storage: Storage) -> str:
    """P3 之前的事件形态：thought 有 turn，done 没有 turn（所以能被 min_turn 滤掉）。"""
    task = storage.create_task("旧任务")
    tid = task.task_id
    storage.create_event_next(tid, "agent/user", {"turn": 0, "content": "旧任务"})
    storage.create_event_next(tid, "agent/thought", {"turn": 0, "step": 0, "text": "旧答案。"})
    storage.create_event_next(tid, "agent/done", {"result": "旧答案。"})
    return tid


def assistant_msgs(msgs: list[dict]) -> list[dict]:
    return [m for m in msgs if m["role"] == "assistant"]


CAPS_SAME_GW = {
    "gateways": {
        "fakegw": {
            "match": "fake-gateway.example.com",
            "thinking_param": {"enable_thinking": True},
            "echo_reasoning_with_tools": True,
            "must_echo_cross_turn": False,
            "supports_temperature": True,
        }
    },
    "groups": {},
    "models": {},
}


# ---------------------------------------------------------------- ① 开关


def test_thinking_param_sent(tmp: Path):
    """[1/6] thinking_param 真的下发；总开关关掉就不发"""
    print("[1/6] thinking_param 下发")
    client, fake = make_client(tmp, "qwen3.8-max", [FakeChunk(content="ok")], CAPS_SAME_GW)
    client.chat_stream([{"role": "user", "content": "hi"}])
    sent = fake.captured[0]
    assert sent.get("extra_body") == {"enable_thinking": True}, f"thinking 未下发：{sent}"

    client2, fake2 = make_client(tmp, "qwen3.8-max", [FakeChunk(content="ok")], CAPS_SAME_GW, thinking=False)
    client2.chat_stream([{"role": "user", "content": "hi"}])
    assert "extra_body" not in fake2.captured[0], f"总开关关了还在发：{fake2.captured[0]}"
    print("    ✅ 开=下发 extra_body / 关=不下发")


def test_thinking_not_in_probe(tmp: Path):
    """[2/6] 试水不带 thinking（部分模型非流式不支持思考，带了会让切换被误拒）"""
    print("[2/6] 试水口径不含 thinking")
    client, fake = make_client(tmp, "qwen3.8-max", [FakeChunk(content="ok")], CAPS_SAME_GW)
    ok, reason = client.verify_model("qwen3.8-max")
    assert ok, reason
    assert "extra_body" not in fake.captured[-1], fake.captured[-1]
    print("    ✅ 试水不带 extra_body")


# ---------------------------------------------------------------- ② 轮内回传


def test_echo_reasoning_within_turn(tmp: Path):
    """[3/6] 同一 turn 内，带 tools 的 assistant 消息必须原样带回 reasoning（R1）"""
    print("[3/6] 轮内回传 reasoning（R1）")
    from src.kernel.plugin import AgentContext
    from src.llm_client import LLMCall
    from src.plugins.event_store_plugin import EventStorePlugin
    from src.plugins.tool_registry import ToolRegistryPlugin
    from src.reactor import Reactor
    from src.tools.file_view import FileViewTool

    f = tmp / "in.txt"
    f.write_text("内容", encoding="utf-8")

    class StreamFakeLLM:
        """带 capability()，模拟真 LLMClient 的能力查询。"""

        def __init__(self, script, cap):
            self._script = list(script)
            self._cap = cap
            self.seen_messages: list[list[dict]] = []
            self.model = "qwen3.8-max"

        def capability(self, model=None):
            return self._cap

        def chat_stream(self, messages, tools=None, on_delta=None, on_reasoning_delta=None, should_stop=None):
            self.seen_messages.append([dict(m) for m in messages])
            item = self._script.pop(0)
            if item.text and on_delta:
                on_delta(item.text)
            if item.reasoning and on_reasoning_delta:
                on_reasoning_delta(item.reasoning)
            return item

    cap = resolve("qwen3.8-max", GW, table=CAPS_SAME_GW)
    assert cap.echo_reasoning_with_tools is True
    script = [
        LLMCall(text="我先读一下。", reasoning="先 file_view。", reasoning_field="reasoning_content",
                tool_calls=[{"id": "c1", "name": "file_view", "arguments": {"path": str(f)}}]),
        LLMCall(text="读到了。", reasoning="可以回答。", reasoning_field="reasoning_content", tool_calls=[]),
    ]
    cfg = Config(llm_api_key="fake-key", db_path=str(tmp / "echo.db"), max_steps_per_turn=5)
    storage = Storage(cfg.db_path)
    ctx = AgentContext(config=cfg, storage=storage)
    ctx.plugins.register(ToolRegistryPlugin({"file_view": FileViewTool(max_bytes=1024)}))
    ctx.plugins.register(EventStorePlugin())
    ctx.start()
    llm = StreamFakeLLM(script, cap)
    reactor = Reactor(llm, ctx)
    task = storage.create_task("读文件")
    reactor.run(task.task_id, task.content)

    second = llm.seen_messages[1]
    assistants = [m for m in second if m["role"] == "assistant"]
    assert assistants, "第二次调用没带 assistant 消息"
    first_assistant = assistants[0]
    assert first_assistant.get("reasoning_content") == "先 file_view。", first_assistant
    assert first_assistant.get("tool_calls"), first_assistant
    storage.close()
    print("    ✅ assistant.tool_calls 同一条消息里带回了 reasoning_content")


def test_no_echo_when_capability_says_no(tmp: Path):
    """[4/6] echo_reasoning_with_tools=false → 绝不带（R5，带了同样可能 400）"""
    print("[4/6] 能力表禁止时不回传")
    caps = json.loads(json.dumps(CAPS_SAME_GW))
    caps["gateways"]["fakegw"]["echo_reasoning_with_tools"] = False
    cap = resolve("qw", GW, table=caps)
    assert cap.echo_reasoning_with_tools is False

    from src.reactor import _assistant_message

    call = SimpleNamespace(text="说话", reasoning="思考", reasoning_field="reasoning_content",
                           tool_calls=[{"id": "c1", "name": "file_view", "arguments": {}}])
    msg = _assistant_message(call, cap)
    assert "reasoning_content" not in msg, msg
    print("    ✅ 不带 reasoning 字段")


# ---------------------------------------------------------------- ③ 同源


def test_rebuild_reads_step_end(tmp: Path):
    """[5/6] rebuild 认 step_end：答案只出现一次，done 不再产生第二条"""
    print("[5/6] rebuild 读 step_end，done 不重复")
    cfg = Config(llm_api_key="k", db_path=str(tmp / "rb.db"), llm_base_url=GW,
                 capabilities_file=str(tmp / "cap.json"), thinking_enabled=False)
    Path(cfg.capabilities_file).write_text(json.dumps(CAPS_SAME_GW), encoding="utf-8")
    storage = Storage(cfg.db_path)
    tid = seed_new_task(storage)

    msgs = rebuild_messages(storage, {}, tid, cfg, llm=None)
    assistants = assistant_msgs(msgs)
    texts = [m.get("content") for m in assistants]

    assert texts.count("文件里只有两个字：内容。") == 1, f"答案重复：{texts}"
    assert "我先读一下。" in texts, texts
    # 跨轮默认丢弃 reasoning（D4=A）
    assert not any("reasoning_content" in m for m in assistants), [list(m) for m in assistants]
    # tool 消息必须还在
    assert [m for m in msgs if m["role"] == "tool"], "tool 消息丢了"
    storage.close()
    print(f"    ✅ assistant={texts}")


def test_rebuild_legacy_thought(tmp: Path):
    """[6/6] 旧任务仍能被重建（写新读旧：没有 step_end 就退回 thought）"""
    print("[6/6] 旧任务走 thought 降级")
    cfg = Config(llm_api_key="k", db_path=str(tmp / "leg.db"), llm_base_url=GW,
                 capabilities_file=str(tmp / "cap2.json"), thinking_enabled=False)
    Path(cfg.capabilities_file).write_text(json.dumps(CAPS_SAME_GW), encoding="utf-8")
    storage = Storage(cfg.db_path)
    tid = seed_legacy_task(storage)
    msgs = rebuild_messages(storage, {}, tid, cfg, llm=None)
    texts = [m.get("content") for m in assistant_msgs(msgs)]
    assert texts.count("旧答案。") == 1, f"旧任务重复：{texts}"
    storage.close()
    print(f"    ✅ 旧任务 assistant={texts}")


def test_cross_turn_echo_when_required(tmp: Path):
    """[7/9] must_echo_cross_turn=when_thinking 且开了 thinking → 跨轮也回传（R3）"""
    print("[7/9] 跨轮回传（R3）")
    caps = json.loads(json.dumps(CAPS_SAME_GW))
    caps["gateways"]["fakegw"]["must_echo_cross_turn"] = "when_thinking"
    cfg = Config(llm_api_key="k", db_path=str(tmp / "x.db"), llm_base_url=GW,
                 capabilities_file=str(tmp / "capx.json"), thinking_enabled=True)
    Path(cfg.capabilities_file).write_text(json.dumps(caps), encoding="utf-8")
    storage = Storage(cfg.db_path)
    tid = seed_new_task(storage)
    msgs = rebuild_messages(storage, {}, tid, cfg, llm=None)
    with_reasoning = [m for m in assistant_msgs(msgs) if m.get("reasoning_content")]
    assert with_reasoning, [list(m) for m in assistant_msgs(msgs)]
    assert with_reasoning[0]["reasoning_content"] == "用户要看文件，先 file_view。", with_reasoning[0]

    # 关掉 thinking → 同样的配置不再回传（三态的意义）
    cfg2 = Config(llm_api_key="k", db_path=str(tmp / "x2.db"), llm_base_url=GW,
                  capabilities_file=str(tmp / "capx.json"), thinking_enabled=False)
    msgs2 = rebuild_messages(storage, {}, tid, cfg2, llm=None)
    assert not any("reasoning_content" in m for m in assistant_msgs(msgs2)), \
        [list(m) for m in assistant_msgs(msgs2)]
    cap_on = resolve("qwen3.8-max", GW, table=caps)
    assert needs_echo(cap_on, thinking_enabled=True) is True
    assert needs_echo(cap_on, thinking_enabled=False) is False
    storage.close()
    print("    ✅ 开则回传 / 关则丢弃")


# ---------------------------------------------------------------- ④ 压缩摘要


def test_memory_render_uses_step_end(tmp: Path):
    """[8/9] 压缩输入读 step_end，且不与 thought 重复计一次"""
    print("[8/9] 摘要渲染读 step_end")
    storage = Storage(str(tmp / "mem.db"))
    tid = seed_new_task(storage)
    events = storage.list_events(tid)
    text = _render_turns(events, {0})
    assert "我先读一下。" in text, text
    assert text.count("文件里只有两个字：内容。") == 1, text
    assert "思考:" in text or "结论:" in text, text
    storage.close()
    print(f"    ✅ 渲染 {len(text)} 字，答案出现 1 次")


def test_done_carries_turn(tmp: Path):
    """[9/9] done 补 turn/step（否则 rebuild/memory 靠 NULL 巧合）"""
    print("[9/9] done 带 turn/step")
    from src.kernel.plugin import AgentContext
    from src.llm_client import LLMCall
    from src.plugins.event_store_plugin import EventStorePlugin
    from src.reactor import Reactor

    class OneShot:
        model = "qwen3.8-max"

        def chat_stream(self, messages, tools=None, on_delta=None, on_reasoning_delta=None, should_stop=None):
            if on_delta:
                on_delta("答案。")
            return LLMCall(text="答案。", tool_calls=[])

    cfg = Config(llm_api_key="k", db_path=str(tmp / "done.db"), max_steps_per_turn=3)
    storage = Storage(cfg.db_path)
    ctx = AgentContext(config=cfg, storage=storage)
    ctx.plugins.register(EventStorePlugin())
    ctx.start()
    reactor = Reactor(OneShot(), ctx)
    task = storage.create_task("随便")
    reactor.run(task.task_id, task.content)
    done = storage.list_events(task.task_id, types=["agent/done"])[0]["payload"]
    assert done.get("turn") is not None, f"done 缺 turn：{done.keys()}"
    assert done.get("step") is not None, f"done 缺 step：{done.keys()}"

    # 补了 turn 之后重建仍只有一条 assistant（这是 P3 最容易被忽略的连带项）
    msgs = rebuild_messages(storage, {}, task.task_id, Config(
        llm_api_key="k", db_path=cfg.db_path, llm_base_url=GW,
        capabilities_file=str(tmp / "cap3.json"), thinking_enabled=False), llm=None)
    texts = [m.get("content") for m in assistant_msgs(msgs)]
    assert texts.count("答案。") == 1, f"done 有了 turn 就重复了：{texts}"
    storage.close()
    print(f"    ✅ done.turn={done['turn']} done.step={done['step']}，重建仍只 1 条")


def test_thinking_rejection_self_heals(tmp: Path):
    """[10/10] 思考参数被网关拒绝 → 去掉参数重试一次，并记入能力表（R7）"""
    print("[10/10] 思考参数被拒自愈（R7）")
    from src.llm_client import LLMClient

    class RejectingOpenAI(FakeOpenAI):
        """第一次带 extra_body 就抛 400，模拟网关不认这个参数名。"""

        def _create(self, **kwargs):
            self.captured.append(kwargs)
            if "extra_body" in kwargs:
                raise RuntimeError("Error code: 400 - enable_thinking is not supported by this model")
            return iter([FakeChunk(content="照样能跑")])

    cap_file = tmp / "cap_selfheal.json"
    cap_file.write_text(json.dumps(CAPS_SAME_GW), encoding="utf-8")
    cfg = Config(llm_api_key="k", llm_model="qwen3.8-max", llm_base_url=GW,
                 capabilities_file=str(cap_file), thinking_enabled=True)
    client = LLMClient(cfg)
    fake = RejectingOpenAI([])
    client._client = fake

    call = client.chat_stream([{"role": "user", "content": "hi"}])
    assert call.text == "照样能跑", call.text
    assert "extra_body" in fake.captured[0], "首次请求应当带思考参数（否则测不到自愈）"
    assert "extra_body" not in fake.captured[1], "重试时没有去掉思考参数"

    probe = tmp / "capabilities.probe.json"
    assert probe.exists(), "自愈结论没写回能力表"
    saved = json.loads(probe.read_text(encoding="utf-8"))["probes"]["qwen3.8-max"]
    assert saved.get("thinking_param") is None, saved

    # 写回后同一进程内不再下发（缓存已更新）
    client.chat_stream([{"role": "user", "content": "hi"}])
    assert "extra_body" not in fake.captured[-1], fake.captured[-1]
    print("    ✅ 首次带参数被拒 → 去掉重试 → 结论落盘 → 下次不再发")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="aa_p3_", ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        test_thinking_param_sent(tmp)
        test_thinking_not_in_probe(tmp)
        test_echo_reasoning_within_turn(tmp)
        test_no_echo_when_capability_says_no(tmp)
        test_rebuild_reads_step_end(tmp)
        test_rebuild_legacy_thought(tmp)
        test_cross_turn_echo_when_required(tmp)
        test_memory_render_uses_step_end(tmp)
        test_done_carries_turn(tmp)
        test_thinking_rejection_self_heals(tmp)
    print("\n✅ 全部通过：开关下发 + 轮内回传 + 重建同源 + 跨轮三态 + done 补 turn + 参数自愈")
    return 0


if __name__ == "__main__":
    sys.exit(main())
