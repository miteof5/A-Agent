"""模型切换测试（便捷切换模型功能）：清单解析 / 校验分类 / 切换接口（含回滚）。

运行（项目根目录下）：
    pytest tests/test_model_switch.py -v     # 推荐
    python tests/test_model_switch.py        # 无 pytest 时手动跑

覆盖：
1. load_models：分组解析 + 去重 + 无分组头兜底
2. flatten / first_model：扁平化与取首
3. save/load_current_model：切换持久化（随 db 目录）
4. LLMClient.verify_model：403（额度）/404（模型不存在）/401（key 无效）/成功 分类
5. API：GET /models 分组清单正确
6. API：POST /switch 成功 → current 更新 + .current_model 落盘
7. API：POST /switch 不在清单 → 400 MODEL_NOT_IN_LIST（当前模型不变）
8. API：POST /switch 校验失败 → 400 MODEL_VERIFY_FAILED（自动回滚）
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.api import create_app
from src.config import Config
from src.llm_client import LLMCall, LLMClient
from src.model_registry import first_model, flatten, load_current_model, load_models, save_current_model

TEST_MODELS = """# 阿里云·通义千问
qwen3.8-max
qwen3.8-flash
qwen3.8-flash
# DeepSeek
deepseek-v4-flash-0731
# 多模态（阿里云·通义千问）
qwen3.8-omni-flash
qwen3.8-omni-flash-realtime
"""


# ---- 1. 清单解析 ----

def test_load_models(tmp: Path):
    """[1/8] 分组解析 + 去重 + 顺序保持"""
    print("[1/8] load_models 分组解析")
    f = tmp / "models.txt"
    f.write_text(TEST_MODELS, encoding="utf-8")
    groups = load_models(f)
    assert [g.group for g in groups] == ["阿里云·通义千问", "DeepSeek", "多模态（阿里云·通义千问）"], groups
    assert groups[0].models == ["qwen3.8-max", "qwen3.8-flash"], groups[0]  # 重复 qwen3.8-flash 已去重
    assert groups[2].models == ["qwen3.8-omni-flash", "qwen3.8-omni-flash-realtime"], groups[2]
    print(f"    ✅ 分组={[g.group for g in groups]} / 去重后模型数={len(flatten(groups))}")


def test_load_models_no_header(tmp: Path):
    """[2/8] 无分组头 → 归入「未分组」"""
    print("[2/8] load_models 无分组头兜底")
    f = tmp / "models.txt"
    f.write_text("alpha\nbeta\n", encoding="utf-8")
    groups = load_models(f)
    assert groups[0].group == "未分组" and groups[0].models == ["alpha", "beta"], groups
    assert first_model(groups) == "alpha"
    assert flatten(groups) == ["alpha", "beta"]
    print(f"    ✅ 未分组={groups[0].models} / first_model={first_model(groups)!r}")


# ---- 2. 持久化 ----

def test_current_model_persist(tmp: Path):
    """[3/8] 切换持久化：save → load 一致；文件缺失返回 None"""
    print("[3/8] .current_model 持久化")
    assert load_current_model(tmp) is None
    save_current_model("qwen3.8-flash", base_dir=tmp)
    assert load_current_model(tmp) == "qwen3.8-flash"
    assert (tmp / ".current_model").read_text(encoding="utf-8").strip() == "qwen3.8-flash"
    print(f"    ✅ 写入 {(tmp / '.current_model')} = {load_current_model(tmp)!r}")


# ---- 3. verify_model 错误分类（不联网：注入假 client）----

class _FakeErr(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"fake err {status_code}")


class _Completions:
    def __init__(self, owner):
        self._owner = owner

    def create(self, **kw):
        raise _FakeErr(self._owner._status)


class _Chat:
    def __init__(self, owner):
        self.completions = _Completions(owner)


class _BoomClient:
    """模拟 OpenAI client：client.chat.completions.create 恒抛指定 status_code。"""

    def __init__(self, status_code: int):
        self._status = status_code
        self.chat = _Chat(self)


class _OkClient:
    """模拟成功 client：create 直接返回。"""

    def __init__(self):
        self.chat = type(
            "Chat",
            (),
            {"completions": type("Completions", (), {"create": staticmethod(lambda **kw: None)})()},
        )()


def test_verify_model_classify(tmp: Path):
    """[4/8] verify_model 错误分类：403 额度 / 404 不存在 / 401 key 无效 / 成功"""
    print("[4/8] verify_model 分类")
    llm = LLMClient(Config(llm_api_key="fake-key", llm_model="m0"))
    llm._client = _BoomClient(403)
    ok, reason = llm.verify_model("m")
    assert not ok and "403" in reason and "额度" in reason, reason
    llm._client = _BoomClient(404)
    ok, reason = llm.verify_model("m")
    assert not ok and "404" in reason and "模型不存在" in reason, reason
    llm._client = _BoomClient(401)
    ok, reason = llm.verify_model("m")
    assert not ok and "401" in reason and "Key" in reason, reason
    # 成功：假 client 直接成功（无异常）
    llm._client = _OkClient()
    ok, reason = llm.verify_model("m")
    assert ok and reason == "", (ok, reason)
    print(f"    ✅ 403/404/401/成功 分类正确")


# ---- 4. API 层（TestClient + FakeLLM 注入，不联网）----

class FakeLLM:
    """替代 LLMClient：model 属性 + verify_model/set_model 可编程。"""

    def __init__(self, model: str = "deepseek-v4-flash-0731", verify_ok: bool = True, verify_reason: str = ""):
        self.model = model
        self.verify_ok = verify_ok
        self.verify_reason = verify_reason
        self.set_calls: list[str] = []

    def verify_model(self, name: str):
        return (self.verify_ok, self.verify_reason)

    def set_model(self, name: str) -> None:
        self.model = name
        self.set_calls.append(name)

    def chat(self, messages, tools=None):
        return LLMCall(text="ok")

    def chat_stream(self, messages, tools=None, on_delta=None, should_stop=None):
        return LLMCall(text="ok")


def _build_app(tmp: Path, llm: FakeLLM):
    models_file = tmp / "models.txt"
    models_file.write_text(TEST_MODELS, encoding="utf-8")
    config = Config(
        llm_api_key="fake-key",
        llm_model=llm.model,
        models_file=str(models_file),
        db_path=str(tmp / "test.db"),
    )
    return create_app(config, llm=llm), config


def test_api_list_models(tmp: Path):
    """[5/8] GET /api/v1/models：current + 分组清单（多模态最后）"""
    print("[5/8] GET /models")
    from fastapi.testclient import TestClient

    app, _ = _build_app(tmp, FakeLLM())
    with TestClient(app) as client:
        r = client.get("/api/v1/models")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["current"] == "deepseek-v4-flash-0731"
    groups = data["groups"]
    assert groups[-1]["group"].startswith("多模态"), groups
    assert "qwen3.8-omni-flash" in groups[-1]["models"]
    print(f"    ✅ current={data['current']} / 分组数={len(groups)} / 多模态在最后")


def test_api_switch_ok(tmp: Path):
    """[6/8] POST /models/switch 成功：current 更新 + 持久化落盘"""
    print("[6/8] switch 成功")
    from fastapi.testclient import TestClient

    llm = FakeLLM()
    app, config = _build_app(tmp, llm)
    with TestClient(app) as client:
        r = client.post("/api/v1/models/switch", json={"name": "qwen3.8-flash"})
    assert r.status_code == 200, r.text
    assert r.json()["current"] == "qwen3.8-flash"
    assert llm.set_calls == ["qwen3.8-flash"]
    # 持久化落在 db 目录
    assert load_current_model(Path(config.db_path).parent) == "qwen3.8-flash"
    print(f"    ✅ current={llm.model} / 已持久化")


def test_api_switch_not_in_list(tmp: Path):
    """[7/8] 不在清单 → 400 MODEL_NOT_IN_LIST（当前模型不变）"""
    print("[7/8] switch 不在清单")
    from fastapi.testclient import TestClient

    llm = FakeLLM()
    app, _ = _build_app(tmp, llm)
    with TestClient(app) as client:
        r = client.post("/api/v1/models/switch", json={"name": "not-exist-model"})
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "MODEL_NOT_IN_LIST", r.json()
    assert llm.model == "deepseek-v4-flash-0731" and llm.set_calls == []  # 未切换
    print(f"    ✅ 400 MODEL_NOT_IN_LIST / 当前模型未变")


def test_api_switch_verify_failed_rollback(tmp: Path):
    """[8/8] 校验失败（额度不足）→ 400 MODEL_VERIFY_FAILED + 自动回滚"""
    print("[8/8] switch 校验失败回滚")
    from fastapi.testclient import TestClient

    llm = FakeLLM(verify_ok=False, verify_reason="额度不足或未开通（403）：qwen3.8-max")
    app, _ = _build_app(tmp, llm)
    with TestClient(app) as client:
        r = client.post("/api/v1/models/switch", json={"name": "qwen3.8-max"})
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["code"] == "MODEL_VERIFY_FAILED", body
    assert "额度不足" in body["message"], body
    assert llm.model == "deepseek-v4-flash-0731" and llm.set_calls == []  # 回滚：未调用 set_model
    print(f"    ✅ 400 MODEL_VERIFY_FAILED / 未调用 set_model（自动回滚）")


def main() -> int:
    import tempfile

    # ignore_cleanup_errors：Windows 下 SQLite 文件句柄由各 API 测试的 Storage 持有，
    # 测试完成后句柄随进程退出释放，此处忽略清理时的文件锁报错即可
    with tempfile.TemporaryDirectory(prefix="aa_models_", ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        test_load_models(tmp)
        test_load_models_no_header(tmp)
        test_current_model_persist(tmp)
        test_verify_model_classify(tmp)
        test_api_list_models(tmp)
        test_api_switch_ok(tmp)
        test_api_switch_not_in_list(tmp)
        test_api_switch_verify_failed_rollback(tmp)
    print("\n✅ 全部通过：清单解析 / 校验分类 / 切换接口（含回滚与持久化） 可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
