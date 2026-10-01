"""S5 长期记忆测试：价值过滤 / 提炼落库 / 去重更新 / 注入块（无需真实 Key）。

覆盖：
1. 价值过滤：闲聊跳过、无工具短结果跳过、调过工具/长结果通过
2. 提炼落库：FakeLLM add → memory 表新增；重复 add → UNIQUE 拒绝不新增
3. update 合并：FakeLLM update → 旧条目内容被更新
4. extract_and_store 全流程：done 任务 → 过滤 → 提炼 → 落库
5. 注入块：有记忆 → 构建文本并刷新活跃度；无记忆 → None
6. 活跃度排序：最近用过的最先注入
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Config
from src.long_memory import (
    build_long_memory_block,
    extract_and_store,
    extract_query_terms,
    has_extraction_value,
    retrieve_memories_for_task,
)
from src.models import TaskState
from src.storage import Storage


class FakeLLM:
    """模拟 LLMClient：extract_memories 返回预设候选。"""

    def __init__(self, candidates=None):
        self.model = "fake-model"
        self.candidates = candidates or []
        self.calls = 0
        self.last_schema = None

    def extract_memories(self, user_input, result, existing, schema=None, max_items=3, max_chars=100):
        self.calls += 1
        self.last_schema = schema
        return self.candidates, {"prompt_tokens": 300, "completion_tokens": 60}


CFG = Config(long_memory_inject_limit=5, long_memory_max_chars=100)


def _done_task(storage, content, result, n_events=3, with_tool=False):
    """构造一个 DONE 任务：user 事件 + 可选 tools/call + done 事件。"""
    task = storage.create_task(content)
    storage.create_event_next(task.task_id, "agent/user", {"turn": 0, "content": content})
    if with_tool:
        storage.create_event_next(task.task_id, "tools/call", {"turn": 0, "step": 0, "name": "shell_run", "arguments": {"command": "ls"}})
    storage.create_event_next(task.task_id, "agent/done", {"turn": 0, "result": result})
    storage.update_task(task.task_id, state=TaskState.DONE, result=result)
    return task


def test_value_filter():
    ok = []
    # ① 闲聊/一句话 → 跳过
    assert not has_extraction_value("你好", "你好", False), "短输入应跳过"
    # ② 无工具 + 短结果 → 跳过
    assert not has_extraction_value("今天天气如何", "晴天", False), "无工具短结果应跳过"
    # ③ 调过工具 → 通过（即使结果短）
    assert has_extraction_value("帮我看看桌面上有什么文件", "有 3 个", True), "调过工具应提炼"
    # ④ 长结果 → 通过（即使无工具）
    assert has_extraction_value("介绍一下这个项目", "项目分为几个模块，使用 FastAPI 和 SQLite 实现……", False), "长结果应提炼"
    ok.append("价值过滤：4 条规则全对")
    print(f"[1/6] {ok[0]}")


def test_extract_add_and_dedup():
    with tempfile.TemporaryDirectory() as td:
        st = Storage(str(Path(td) / "m.db"))
        task = _done_task(st, "请帮我记住用户偏好：以后都用中文交流，回复要简洁", "好的，已记住", with_tool=True)
        llm = FakeLLM([{"action": "add", "category": "user_profile", "content": "用户偏好中文交流，回复简洁", "keywords": "中文 简洁"}])
        cands = extract_and_store(st, llm, task.task_id, CFG)
        rows = st.list_memory()
        assert len(rows) == 1 and rows[0]["category"] == "user_profile", "应新增 1 条"
        assert rows[0]["content"] == "用户偏好中文交流，回复简洁", "content 应一致"
        # 重复 add → UNIQUE 拒绝，不新增
        rid = st.create_memory("user_profile", "用户偏好中文交流，回复简洁", "中文 简洁")
        assert rid is None, "UNIQUE 重复应返回 None"
        assert len(st.list_memory()) == 1, "重复不应新增"
        st.close()  # Windows：释放 db 文件锁，允许临时目录清理
        print("[2/6] 提炼落库 + UNIQUE 去重 通过")


def test_extract_update():
    with tempfile.TemporaryDirectory() as td:
        st = Storage(str(Path(td) / "m.db"))
        mid = st.create_memory("project_fact", "ActionAgent 端口 8000", "端口")
        assert mid, "seed 记忆应创建"
        task = _done_task(st, "我把服务端口改成了 9000，请更新记录", "已更新", with_tool=True)
        llm = FakeLLM([{"action": "update", "target_id": mid, "content": "ActionAgent 端口 9000", "keywords": "端口 9000"}])
        extract_and_store(st, llm, task.task_id, CFG)
        rows = st.list_memory()
        assert len(rows) == 1, "update 不应新增条目"
        assert rows[0]["content"] == "ActionAgent 端口 9000", "内容应被更新"
        st.close()
        print("[3/6] update 合并旧条目 通过")


def test_extract_skip_and_filter():
    with tempfile.TemporaryDirectory() as td:
        st = Storage(str(Path(td) / "m.db"))
        # 闲聊任务 → 过滤，不调 LLM
        t1 = _done_task(st, "你好", "你好呀")
        llm = FakeLLM()
        extract_and_store(st, llm, t1.task_id, CFG)
        assert llm.calls == 0, "闲聊不应触发提炼调用"
        assert len(st.list_memory()) == 0, "闲聊不应落库"
        # skip 候选 → 不落库
        t2 = _done_task(st, "这个任务有点长没有价值信息可以提炼的，我随便说说", "明白，已处理完毕", with_tool=True)
        llm2 = FakeLLM([{"action": "skip", "reason": "无跨会话价值"}])
        extract_and_store(st, llm2, t2.task_id, CFG)
        assert len(st.list_memory()) == 0, "skip 不应落库"
        st.close()
        print("[4/6] 价值过滤 + skip 候选 通过")


def test_inject_block_and_touch():
    with tempfile.TemporaryDirectory() as td:
        st = Storage(str(Path(td) / "m.db"))
        assert build_long_memory_block(st, CFG) is None, "无记忆应返回 None"
        m1 = st.create_memory("user_profile", "用户偏好中文交流", "中文")
        m2 = st.create_memory("project_fact", "ActionAgent 端口 8000", "端口")
        blk = build_long_memory_block(st, CFG)
        assert blk and "用户偏好中文交流" in blk and "端口 8000" in blk, "注入块应含两条记忆"
        assert "【长期记忆】" in blk, "注入块应有标题"
        # touch：注入后 last_used_at 已刷新
        row = st.get_memory(m1)
        assert row["last_used_at"], "注入后应刷新活跃度"
        st.close()
        print("[5/6] 注入块构建 + 活跃度刷新 通过")


def test_retrieve_order():
    with tempfile.TemporaryDirectory() as td:
        st = Storage(str(Path(td) / "m.db"))
        a = st.create_memory("project_fact", "事实A 不常用", "A")
        b = st.create_memory("project_fact", "事实B 最近用过", "B")
        c = st.create_memory("project_fact", "事实C 刚更新", "C")
        st.touch_memory(b)  # B 最近被检索注入过 → 应排最前
        rows = st.retrieve_memories(limit=2)
        ids = [r["id"] for r in rows]
        assert ids[0] == b, "最近用过的应最先注入"
        # 失效条目不参与注入
        st.set_memory_status(c, "archived")
        rows = st.retrieve_memories(limit=10)
        assert c not in [r["id"] for r in rows], "失效条目不应注入"
        st.close()
        print("[6/6] 活跃度排序 + 失效排除 通过")


def test_schema_override():
    """方案 A：主题语义可插拔——换 schema（标题/分类）机制层零改动跟随变化。"""
    from src.config import DEFAULT_LONG_MEMORY_SCHEMA

    custom = {
        "name": "项目知识",
        "header_hint": "本项目相关稳定事实，直接信任",
        "categories": {
            "pref": "用户偏好",
            "tech": "技术栈与架构决策",
        },
        "max_items": 2,
        "extract_extra_rules": "本场景只关心技术决策，不记生活琐事。",
    }
    cfg = Config(long_memory_inject_limit=5, long_memory_max_chars=100, long_memory_schema=custom)
    with tempfile.TemporaryDirectory() as td:
        st = Storage(str(Path(td) / "m.db"))
        st.create_memory("tech", "后端用 FastAPI + SQLite", "FastAPI SQLite")
        # 注入块标题/引导语跟随 schema
        blk = build_long_memory_block(st, cfg)
        assert blk and "【项目知识】（本项目相关稳定事实，直接信任）" in blk, "注入块应使用自定义标题"
        assert "- [tech]" in blk, "自定义分类应出现在注入条目中"
        # 提炼把 schema 传给 LLM（prompt 由 schema 动态生成，分类/规则可覆写）
        task = _done_task(st, "决定用 FastAPI 重写后端，把端口定在 8000", "已决定", with_tool=True)
        llm = FakeLLM([{"action": "add", "category": "tech", "content": "后端定为 FastAPI，端口 8000", "keywords": "FastAPI 端口"}])
        extract_and_store(st, llm, task.task_id, cfg)
        assert llm.last_schema == custom, "LLM 应收到主题 schema"
        assert len(st.list_memory()) == 2, "自定义分类应能落库"
        # 默认 schema 仍可用（机制不依赖自定义主题）
        cfg2 = Config(long_memory_inject_limit=5, long_memory_max_chars=100)
        assert cfg2.long_memory_schema == DEFAULT_LONG_MEMORY_SCHEMA, "默认 schema 与内置常量一致"
        st.close()
        print("[7/7] schema 可插拔：标题/分类/规则跟随配置 通过")


def test_search_and_inject():
    """检索注入（S5.1）：FTS5 关键词命中相关记忆；零命中补足/兜底；失效排除。"""
    with tempfile.TemporaryDirectory() as td:
        st = Storage(str(Path(td) / "m.db"))
        m1 = st.create_memory("project_fact", "项目端口是 8000，models.txt 在根目录", "models.txt 端口")
        m2 = st.create_memory("user_profile", "用户偏好中文交流", "中文")
        # ① FTS 索引与 memory 表同步（create 后触发器已写入）
        n_fts = st._fetchone("SELECT count(*) FROM memory_fts")[0]
        assert n_fts == len(st.list_memory()), "FTS 索引应与 memory 同步"
        # ② 数字检索词命中（输入含 8000 → 命中端口记忆）
        rows = retrieve_memories_for_task(st, CFG, "服务跑在 8000 端口，帮我看看")
        assert rows and rows[0]["id"] == m1, "数字检索词应命中端口记忆"
        # ③ 文件名检索词命中
        rows = retrieve_memories_for_task(st, CFG, "models.txt 里有什么")
        assert rows and rows[0]["id"] == m1, "文件名检索词应命中"
        # ④ update 后 FTS 同步：新内容可被检索
        st.update_memory(m2, content="用户偏好中文交流，回复简洁")
        rows = retrieve_memories_for_task(st, CFG, "回复简洁")
        assert rows and rows[0]["id"] == m2, "更新后的内容应可被检索"
        # ⑤ 无信号词（纯 2 字寒暄）→ 活跃度兜底，不报错
        rows = retrieve_memories_for_task(st, CFG, "你好")
        assert isinstance(rows, list) and len(rows) >= 1, "无信号词应退回活跃度"
        # ⑥ 失效排除：archived 后不再被检索/注入
        st.set_memory_status(m1, "archived")
        rows = retrieve_memories_for_task(st, CFG, "8000")
        assert m1 not in [r["id"] for r in rows], "archived 不应被检索"
        st.set_memory_status(m1, "active")
        # ⑦ build_long_memory_block 按任务输入检索注入
        blk = build_long_memory_block(st, CFG, user_input="models.txt 里有哪些模型")
        assert blk and "models.txt" in blk, "注入块应含检索命中的记忆"
        st.close()
        print("[8/8] 检索注入：FTS 同步 + 关键词命中 + 兜底 + 失效排除 通过")


def main():
    test_value_filter()
    test_extract_add_and_dedup()
    test_extract_update()
    test_extract_skip_and_filter()
    test_inject_block_and_touch()
    test_retrieve_order()
    test_schema_override()
    test_search_and_inject()
    print("\n✅ 全部通过：S5 长期记忆（过滤/提炼/去重/注入/schema/检索注入）可用")


if __name__ == "__main__":
    main()
