"""能力表（capabilities）解析测试 —— 思考与答案分离 P2。

运行（项目根目录下）：
    python tests/test_capabilities.py

这组测试守的是「规格的判定规则」，不是「某个模型的行为」：
    解析优先级 probe > models > groups > gateways > 保守默认
    以及 must_echo_cross_turn 的三态语义（false / true / "when_thinking"）

为什么先写测试：能力表是 P2 的判定中枢，一旦继承顺序写反（比如网关默认盖掉模型例外），
kimi-k3 会在切换时通过、在任务里 400，而且没有任何报错指向配置。这种错只能靠断言守住。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.capabilities import (
    Capability,
    audit,
    load_table,
    needs_echo,
    resolve,
    save_probe,
)

# 一份受控的能力表：只放测试需要的条目，避免真实配置变动导致测试飘
FIXTURE = {
    "gateways": {
        "fakegw": {
            "match": "fake-gateway.example.com",
            "reasoning_fields": ["reasoning_content"],
            "thinking_param": {"thinking": {"type": "enabled"}},
            "echo_reasoning_with_tools": True,
            "must_echo_cross_turn": False,
            "supports_temperature": True,
            "supports_tools": True,
        }
    },
    "groups": {
        "Kimi": {"supports_temperature": False},
        "DeepSeek": {"supports_temperature": True},
    },
    "models": {
        "kimi-k3": {"supports_temperature": False},
        "deepseek-v4.1-flash": {"must_echo_cross_turn": "when_thinking"},
        "qwen3.8-omni-flash-realtime": {
            "supported": False,
            "reason": "实时语音，不走 chat.completions + tools",
        },
    },
}

MODELS_TXT = """# 阿里云·通义千问
qwen3.8-max
qwen3.8-flash
# DeepSeek
deepseek-v4.1-flash
# Kimi
kimi-k3
kimi-k2-turbo
"""

GW = "https://fake-gateway.example.com/v1"


def write_fixture(tmp: Path) -> tuple[Path, Path]:
    cap = tmp / "capabilities.json"
    cap.write_text(json.dumps(FIXTURE, ensure_ascii=False), encoding="utf-8")
    models = tmp / "models.txt"
    models.write_text(MODELS_TXT, encoding="utf-8")
    return cap, models


def test_gateway_defaults(tmp: Path):
    """[1/7] 无分组、无模型条目 → 全部来自网关默认"""
    print("[1/7] 网关默认生效")
    cap, models = write_fixture(tmp)
    c = resolve("qwen3.8-max", GW, table_path=cap, models_file=models)
    assert c.supports_temperature is True, c
    assert c.supports_tools is True, c
    assert c.reasoning_fields == ["reasoning_content"], c.reasoning_fields
    assert c.echo_reasoning_with_tools is True, c
    assert c.source == "gateways", c.source
    print(f"    ✅ 来源={c.source} temperature={c.supports_temperature} fields={c.reasoning_fields}")


def test_group_default(tmp: Path):
    """[2/7] 模型名只出现在分组里 → 分组默认生效（Kimi 组不支持 temperature）"""
    print("[2/7] 分组默认生效")
    cap, models = write_fixture(tmp)
    c = resolve("kimi-k2-turbo", GW, table_path=cap, models_file=models)
    assert c.supports_temperature is False, c
    assert c.supports_tools is True, c  # 其余项仍继承网关
    assert c.source == "groups", c.source
    print(f"    ✅ kimi-k2-turbo 来源={c.source} temperature={c.supports_temperature}")


def test_model_override_wins(tmp: Path):
    """[3/7] 模型条目压过网关/分组默认"""
    print("[3/7] 模型例外优先级最高")
    cap, models = write_fixture(tmp)
    c = resolve("kimi-k3", GW, table_path=cap, models_file=models)
    assert c.supports_temperature is False, c
    assert c.source == "models", c.source
    # 反证：网关默认是 True，若继承顺序写反，这里会是 True
    gw = resolve("qwen3.8-max", GW, table_path=cap, models_file=models)
    assert gw.supports_temperature is True
    print(f"    ✅ kimi-k3={c.supports_temperature}（网关默认 True，被模型例外覆盖）")


def test_unsupported_model_kept_in_list(tmp: Path):
    """[4/7] supported:false 的模型仍可解析，只是 enabled=False 且带原因"""
    print("[4/7] 不可用模型不静默消失")
    cap, models = write_fixture(tmp)
    c = resolve("qwen3.8-omni-flash-realtime", GW, table_path=cap, models_file=models)
    assert c.enabled is False, c
    assert "实时语音" in c.reason, c.reason
    print(f"    ✅ enabled={c.enabled} reason={c.reason!r}")


def test_unknown_gateway_conservative(tmp: Path):
    """[5/7] 未知网关 + 未知模型 → 保守默认（不传 temperature，宁可不控温也不 400）"""
    print("[5/7] 未知网关走保守默认")
    cap, models = write_fixture(tmp)
    c = resolve("some-unseen-model", "https://api.unknown-vendor.com/v1", table_path=cap, models_file=models)
    assert c.supports_temperature is False, "保守默认必须是不传 temperature"
    assert c.supports_tools is True, "tools 是 ReAct 的前提，默认认为支持"
    assert c.supports_reasoning is True, "默认认为可能有 reasoning，避免静默丢弃"
    assert c.source == "default", c.source
    print(f"    ✅ 来源={c.source} temperature={c.supports_temperature} tools={c.supports_tools}")


def test_probe_overrides_everything(tmp: Path):
    """[6/7] 探测结果 > 手工配置（配置必然过期，真实探测才是真相）"""
    print("[6/7] 探测结果优先级最高")
    cap, models = write_fixture(tmp)
    probe_file = tmp / "capabilities.probe.json"
    save_probe("kimi-k3", {"supports_temperature": True}, path=probe_file)
    c = resolve("kimi-k3", GW, table_path=cap, models_file=models, probe_path=probe_file)
    assert c.supports_temperature is True, c  # 配置说 False，探测说 True
    assert c.source == "probe", c.source
    print(f"    ✅ 探测覆盖配置：来源={c.source} temperature={c.supports_temperature}")


def test_must_echo_tristate(tmp: Path):
    """[7/7] must_echo_cross_turn 三态：false / true / "when_thinking\""""
    print("[7/7] 跨轮回传三态")
    cap, models = write_fixture(tmp)
    ds = resolve("deepseek-v4.1-flash", GW, table_path=cap, models_file=models)
    assert ds.must_echo_cross_turn == "when_thinking", ds.must_echo_cross_turn
    # 开了 thinking → 必须回传；没开 → 不必回传（传了纯浪费 token）
    assert needs_echo(ds, thinking_enabled=True) is True
    assert needs_echo(ds, thinking_enabled=False) is False

    always = Capability(must_echo_cross_turn=True)
    assert needs_echo(always, thinking_enabled=False) is True

    never = Capability(must_echo_cross_turn=False)
    assert needs_echo(never, thinking_enabled=True) is False
    print("    ✅ when_thinking 只在开启时回传；true 恒回传；false 恒不回传")


def test_audit_covers_model_list(tmp: Path):
    """[8/8] 启动校验：清单里每个模型都能解析出能力（不靠人工记得补配置）"""
    print("[8/8] 清单全量可解析")
    cap, models = write_fixture(tmp)
    table = load_table(cap)
    rows = audit(["qwen3.8-max", "deepseek-v4.1-flash", "kimi-k3", "kimi-k2-turbo"],
                 GW, table=table, models_file=models)
    assert len(rows) == 4, rows
    assert all(r["enabled"] for r in rows), rows
    omni = audit(["qwen3.8-omni-flash-realtime"], GW, table=table, models_file=models)
    assert omni[0]["enabled"] is False, omni
    print(f"    ✅ {len(rows)} 个模型全部解析成功；omni 标记为不可用")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="aa_cap_", ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        for fn in (
            test_gateway_defaults,
            test_group_default,
            test_model_override_wins,
            test_unsupported_model_kept_in_list,
            test_unknown_gateway_conservative,
            test_probe_overrides_everything,
            test_must_echo_tristate,
            test_audit_covers_model_list,
        ):
            fn(tmp)
    print("\n✅ 全部通过：能力表继承顺序 + 三态回传 + 清单校验 可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
