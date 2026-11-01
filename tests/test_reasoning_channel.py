"""Reasoning 通道测试 —— 思考与答案分离 P2。

运行（项目根目录下）：
    python tests/test_reasoning_channel.py

守的是三条规格不变量（Spec §5.1）：
    ① 思考与答案是两条通道 —— reasoning 不得混进 content
    ② 一次 LLM 调用 = 一个 step_end 事件（落库，含 kind）
    ③ 终止事件只收尾 —— done 不改写正文

另加解析层的两个已知坑（Spec §5.2）：
    · reasoning 字段名不统一（reasoning_content / reasoning），要逐个候选试
    · temperature 硬编码会让不支持该参数的模型 400（events id=1012 就是 kimi-k3）

为什么先写测试：P2 动的是「上游」。渲染层写错了看得见，解析层写错了看不见——
reasoning 被丢掉时界面什么都不变，你只会觉得「这个模型好像不太思考」。这种静默丢失只能靠断言抓。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.capabilities import Capability
from src.config import Config
from src.kernel.plugin import AgentContext
from src.llm_client import LLMCall, LLMClient
from src.plugins.event_store_plugin import EventStorePlugin
from src.plugins.tool_registry import ToolRegistryPlugin
from src.reactor import Reactor
from src.storage import Storage
from src.tools.file_view import FileViewTool

GW = "https://fake-gateway.example.com/v1"


# ---------------------------------------------------------------- 测试替身


class StreamFakeLLM:
    """按脚本返回 LLMCall，并像真流式一样先回调 reasoning、再回调 content。"""

    def __init__(self, script: list[LLMCall]):
        self._script = list(script)
        self.seen_messages: list[list[dict]] = []

    def chat_stream(self, messages, tools=None, on_delta=None, on_reasoning_delta=None, should_stop=None):
        self.seen_messages.append([dict(m) for m in messages])
        item = self._script.pop(0)
        if getattr(item, "reasoning", None) and on_reasoning_delta is not None:
            on_reasoning_delta(item.reasoning)
        if item.text and on_delta is not None:
            on_delta(item.text)
        return item


class Collect:
    """事件收集器（直接挂在 EventBus 上，抓不落库的 delta 事件）。"""

    def __init__(self):
        self.events: list[dict] = []

    def __call__(self, data):
        self.events.append(dict(data or {}))

    def joined(self, field: str = "delta") -> str:
        return "".join(str(e.get(field) or "") for e in self.events)


def build(tmp: Path, script: list[LLMCall]):
    cfg = Config(
        llm_api_key="fake-key",
        db_path=str(tmp / "rc.db"),
        max_steps_per_turn=10,
    )
    storage = Storage(cfg.db_path)
    ctx = AgentContext(config=cfg, storage=storage)
    ctx.plugins.register(ToolRegistryPlugin({"file_view": FileViewTool(max_bytes=1024)}))
    ctx.plugins.register(EventStorePlugin())  # 与 api.py 装配一致：step_end 要落库才有重建价值
    ctx.start()
    llm = StreamFakeLLM(script)
    return storage, ctx, Reactor(llm, ctx), llm


# ---------------------------------------------------------------- Reactor 侧


def test_reasoning_is_separate_channel(tmp: Path):
    """[1/5] reasoning 走自己的事件，不混进 content；step_end 落库并带 kind"""
    print("[1/5] 两条通道分离 + step_end 落库")
    f = tmp / "a.txt"
    f.write_text("内容", encoding="utf-8")
    script = [
        LLMCall(
            text="我先读一下这个文件。",
            reasoning="用户要看文件，先 file_view。",
            tool_calls=[{"id": "c1", "name": "file_view", "arguments": {"path": str(f)}}],
        ),
        LLMCall(text="文件里只有两个字：内容。", reasoning="已拿到内容，可以回答了。", tool_calls=[]),
    ]
    storage, ctx, reactor, _ = build(tmp, script)

    r_collect, c_collect, s_collect = Collect(), Collect(), Collect()
    ctx.events.on("agent/reasoning_delta", r_collect)
    ctx.events.on("agent/content_delta", c_collect)
    ctx.events.on("agent/step_end", s_collect)

    task = storage.create_task("读文件")
    final = reactor.run(task.task_id, task.content)

    # ① 两条通道分离
    assert r_collect.events, "没有 agent/reasoning_delta 事件"
    assert c_collect.events, "没有 agent/content_delta 事件"
    assert r_collect.joined() == "用户要看文件，先 file_view。已拿到内容，可以回答了。"
    assert "file_view" not in c_collect.joined(), "reasoning 混进了 content 通道"
    assert "我先读一下" not in r_collect.joined(), "content 混进了 reasoning 通道"

    # ② step_end 落库（落库才是重建上下文的唯一依据）
    persisted = storage.list_events(task.task_id, types=["agent/step_end"])
    assert len(persisted) == 2, f"step_end 应落库 2 条，实际 {len(persisted)}"
    first = persisted[0]["payload"]
    assert first["kind"] == "process", first["kind"]      # 有 tool_calls → 过程
    assert first["reasoning"] and first["content"], first
    assert first["tool_calls"] and first["tool_calls"][0]["name"] == "file_view", first
    second = persisted[1]["payload"]
    assert second["kind"] == "answer", second["kind"]      # 无 tool_calls → 答案
    assert second["content"] == "文件里只有两个字：内容。", second["content"]

    # ③ 终止事件只收尾：done 不改写正文（result 与 answer 块同文）
    done = storage.list_events(task.task_id, types=["agent/done"])[0]["payload"]
    assert done["result"] == second["content"], done["result"]
    assert final == second["content"]
    storage.close()
    print(f"    ✅ reasoning={len(r_collect.events)}段 / content={len(c_collect.events)}段 / step_end={len(persisted)}条")


def test_model_recorded_per_step(tmp: Path):
    """[2/5] 每个 step 记录 model（.current_model 是全局热切换，会话可能混模型）"""
    print("[2/5] step_end 带 model")
    script = [LLMCall(text="答案。", tool_calls=[])]
    storage, ctx, reactor, _ = build(tmp, script)
    task = storage.create_task("随便")
    reactor.run(task.task_id, task.content)
    s = storage.list_events(task.task_id, types=["agent/step_end"])[0]["payload"]
    assert s.get("model"), f"step_end 缺少 model 字段：{s.keys()}"
    storage.close()
    print(f"    ✅ model={s['model']}")


def test_thought_no_longer_emitted(tmp: Path):
    """[3/5] P3 起 agent/thought 停发（内容由 step_end 承载，双写只是冗余存储）"""
    print("[3/5] agent/thought 已停发")
    script = [LLMCall(text="答案。", reasoning="想了一下。", tool_calls=[])]
    storage, ctx, reactor, _ = build(tmp, script)
    t_collect = Collect()
    ctx.events.on("agent/thought", t_collect)
    task = storage.create_task("随便")
    reactor.run(task.task_id, task.content)
    assert not t_collect.events, f"P3 之后不该再有 agent/thought：{t_collect.events}"
    s = storage.list_events(task.task_id, types=["agent/step_end"])[0]["payload"]
    assert s["content"] == "答案。", s
    storage.close()
    print("    ✅ 不再双写；内容只在 step_end 里")


# ---------------------------------------------------------------- 解析层


class FakeChunk:
    def __init__(self, content=None, reasoning_content=None, reasoning=None, usage=None):
        delta = SimpleNamespace(content=content, tool_calls=None)
        if reasoning_content is not None:
            delta.reasoning_content = reasoning_content
        if reasoning is not None:
            delta.reasoning = reasoning
        self.choices = [SimpleNamespace(delta=delta)]
        self.usage = usage


class FakeOpenAI:
    """记录请求参数，按预设 chunk 序列返回（stream=True 时是迭代器）。"""

    def __init__(self, chunks):
        self.chunks = chunks
        self.captured: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.captured.append(kwargs)
        return iter(self.chunks)


def make_client(tmp: Path, model: str, chunks, caps: dict | None = None) -> tuple[LLMClient, FakeOpenAI]:
    cap_file = tmp / "capabilities.json"
    if caps is None:
        caps = {"gateways": {"fakegw": {"match": "fake-gateway.example.com"}}, "groups": {}, "models": {}}
    cap_file.write_text(json.dumps(caps, ensure_ascii=False), encoding="utf-8")
    cfg = Config(
        llm_api_key="fake-key",
        llm_model=model,
        llm_base_url=GW,
        capabilities_file=str(cap_file),
    )
    client = LLMClient(cfg)
    fake = FakeOpenAI(chunks)
    client._client = fake
    return client, fake


def test_parse_reasoning_content(tmp: Path):
    """[4/5] 解析 reasoning_content，且不混进 text"""
    print("[4/5] 解析 reasoning_content")
    chunks = [
        FakeChunk(reasoning_content="先想一下"),
        FakeChunk(reasoning_content="，再回答。"),
        FakeChunk(content="答案"),
        FakeChunk(content="在此。"),
    ]
    client, _ = make_client(tmp, "qwen3.8-max", chunks)
    got_r: list[str] = []
    got_c: list[str] = []
    call = client.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=got_c.append,
        on_reasoning_delta=got_r.append,
    )
    assert "".join(got_r) == "先想一下，再回答。", got_r
    assert "".join(got_c) == "答案在此。", got_c
    assert call.reasoning == "先想一下，再回答。", call.reasoning
    assert call.text == "答案在此。", call.text
    print(f"    ✅ reasoning={call.reasoning!r} text={call.text!r}")


def test_parse_alt_reasoning_field(tmp: Path):
    """[5/7] 兼容备选字段名 reasoning（字段名不统一是常态）"""
    print("[5/7] 备选字段 reasoning 也能解析")
    chunks = [FakeChunk(reasoning="换个字段名"), FakeChunk(content="照样能读")]
    client, _ = make_client(tmp, "qwen3.8-max", chunks)
    call = client.chat_stream([{"role": "user", "content": "hi"}])
    assert call.reasoning == "换个字段名", call.reasoning
    assert call.text == "照样能读", call.text
    print(f"    ✅ reasoning={call.reasoning!r}")


def test_temperature_by_capability(tmp: Path):
    """[6/7] 不支持 temperature 的模型不下发该参数（kimi-k3 400 的根因）"""
    print("[6/7] temperature 按能力表下发")
    caps = {
        "gateways": {"fakegw": {"match": "fake-gateway.example.com", "supports_temperature": True}},
        "groups": {},
        "models": {"kimi-k3": {"supports_temperature": False}},
    }
    chunks = [FakeChunk(content="ok")]
    client, fake = make_client(tmp, "kimi-k3", chunks, caps)
    client.chat_stream([{"role": "user", "content": "hi"}])
    assert "temperature" not in fake.captured[0], fake.captured[0]

    client2, fake2 = make_client(tmp, "qwen3.8-max", [FakeChunk(content="ok")], caps)
    client2.chat_stream([{"role": "user", "content": "hi"}])
    assert fake2.captured[0].get("temperature") is not None, fake2.captured[0]
    print(f"    ✅ kimi-k3 不带 temperature / qwen 带 {fake2.captured[0].get('temperature')}")


def test_verify_model_uses_real_shape(tmp: Path):
    """[7/7] 试水口径 = 真实调用口径：带 temperature、带 tools，失败能说出原因"""
    print("[7/7] verify_model 用真实口径探测")
    caps = {
        "gateways": {"fakegw": {"match": "fake-gateway.example.com", "supports_temperature": True}},
        "groups": {},
        "models": {"kimi-k3": {"supports_temperature": False}},
    }
    client, fake = make_client(tmp, "qwen3.8-max", [FakeChunk(content="ok")], caps)
    ok, reason = client.verify_model("qwen3.8-max")
    assert ok is True, reason
    sent = fake.captured[-1]
    assert sent.get("temperature") is not None, f"试水没带 temperature：{sent}"
    assert sent.get("tools"), f"试水没带 tools：{sent}"

    # 不支持 temperature 的模型：试水也不带，避免「试水通过、任务 400」
    client2, fake2 = make_client(tmp, "kimi-k3", [FakeChunk(content="ok")], caps)
    ok2, _ = client2.verify_model("kimi-k3")
    assert ok2 is True
    assert "temperature" not in fake2.captured[-1], fake2.captured[-1]
    print("    ✅ 试水带 temperature+tools；不支持者自动省略")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="aa_rc_", ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        test_reasoning_is_separate_channel(tmp)
        test_model_recorded_per_step(tmp)
        test_thought_no_longer_emitted(tmp)
        test_parse_reasoning_content(tmp)
        test_parse_alt_reasoning_field(tmp)
        test_temperature_by_capability(tmp)
        test_verify_model_uses_real_shape(tmp)
    print("\n✅ 全部通过：reasoning 通道分离 + step_end 落库 + 解析/参数按能力表")
    return 0


if __name__ == "__main__":
    sys.exit(main())
