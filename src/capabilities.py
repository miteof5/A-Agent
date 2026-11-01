"""能力表（capabilities）：按「网关 + 模型」解析出一组行为开关。

为什么不是「按厂商」：决定字段名、参数形状、回传规则的变量是 **`base_url` 后面那层兼容实现**。
同一个 `deepseek-v4.1-flash`，走官方 API、走百炼、走 OpenRouter 是三套不同行为；
「为 DeepSeek 写一条规则」这句话本身就有歧义。所以 key 的维度是网关 + 模型。

四层继承（后者覆盖前者，逐键合并）：
    probe（真实探测） > models（模型例外） > groups（厂商/分组默认） > gateways（网关默认） > 保守默认

保守默认的两条取向：
- `supports_temperature=False` —— 不传参数最多是「控不了温」，传了不支持是 400。宁可少控。
- `supports_reasoning=True` —— 默认认为可能有 reasoning，宁可多读也不要静默丢弃。

配置必然过期（厂商改版本、网关升级、清单加新模型），所以 `probe` 层优先级最高：
探测到的真相压过任何手写配置，`save_probe()` 就是「错了能自己改回来」的落点。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

from .model_registry import PROJECT_ROOT, flatten, load_models

logger = logging.getLogger(__name__)

CAPABILITIES_FILENAME = "capabilities.json"
PROBE_FILENAME = "capabilities.probe.json"

# 保守默认：未知网关 + 未知模型的兜底（也是四层都没写时的终值）
DEFAULTS: dict = {
    "supports_temperature": False,
    "supports_tools": True,
    "supports_reasoning": True,
    "reasoning_fields": ["reasoning_content", "reasoning"],
    "thinking_param": None,
    "echo_reasoning_with_tools": False,
    "must_echo_cross_turn": False,
    "supported": True,
    "reason": "",
}

# 允许出现在配置文件里的键（写错键名时给出可诊断的 warning，而不是静默忽略）
_KNOWN_KEYS = set(DEFAULTS)


@dataclass
class Capability:
    """解析结果。`source` 记录最终生效的层，用于诊断「为什么这个模型是这个行为」。"""

    model: str = ""
    gateway: str = ""
    supports_temperature: bool = DEFAULTS["supports_temperature"]
    supports_tools: bool = DEFAULTS["supports_tools"]
    supports_reasoning: bool = DEFAULTS["supports_reasoning"]
    reasoning_fields: list[str] = field(default_factory=lambda: list(DEFAULTS["reasoning_fields"]))
    thinking_param: dict | None = None
    echo_reasoning_with_tools: bool = DEFAULTS["echo_reasoning_with_tools"]
    must_echo_cross_turn: bool | str = DEFAULTS["must_echo_cross_turn"]
    enabled: bool = True          # supported:false 的模型仍在清单里，只是不可选
    reason: str = ""
    source: str = "default"       # probe / models / groups / gateways / default

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "gateway": self.gateway,
            "supports_temperature": self.supports_temperature,
            "supports_tools": self.supports_tools,
            "supports_reasoning": self.supports_reasoning,
            "reasoning_fields": list(self.reasoning_fields),
            "thinking_param": self.thinking_param,
            "echo_reasoning_with_tools": self.echo_reasoning_with_tools,
            "must_echo_cross_turn": self.must_echo_cross_turn,
            "enabled": self.enabled,
            "reason": self.reason,
            "source": self.source,
        }


# ---------------------------------------------------------------- 载入


def _path(path: str | Path | None, default_name: str) -> Path:
    return Path(path) if path else PROJECT_ROOT / default_name


def load_table(path: str | Path | None = None) -> dict:
    """读能力表 JSON；文件缺失或损坏时返回空表（不抛——配置坏了不该让服务起不来）。"""
    p = _path(path, CAPABILITIES_FILENAME)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        logger.warning("能力表不存在：%s（全部走保守默认）", p)
        return {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("能力表读取失败：%s（%s），全部走保守默认", p, e)
        return {}


def load_probes(path: str | Path | None = None) -> dict:
    """读探测结果；结构 {"probes": {model: {patch}}}。"""
    p = _path(path, PROBE_FILENAME)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        probes = raw.get("probes") if isinstance(raw, dict) else None
        return probes if isinstance(probes, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def probe_path_for(table_path: str | Path | None) -> Path | None:
    """探测结果文件的位置 = 能力表所在目录（不硬编码项目根）。

    这样测试/多实例用不同能力表时，探测结果跟着走，不会互相污染；
    也避免单测把 probe 写进真实项目目录。
    """
    if not table_path:
        return None
    return Path(table_path).parent / PROBE_FILENAME


def save_probe(model: str, patch: dict, path: str | Path | None = None) -> None:
    """把一次真实探测的结论写回（最高优先级层）。写失败只记日志，不影响主流程。"""
    p = _path(path, PROBE_FILENAME)
    data = {"probes": {}}
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("probes"), dict):
                data = raw
    except (OSError, json.JSONDecodeError):
        pass  # 旧文件损坏 → 直接重建
    data["probes"][model] = {**data["probes"].get(model, {}), **patch}
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("能力探测结果已写回 model=%s patch=%s", model, patch)
    except OSError as e:
        logger.warning("能力探测结果写回失败：%s", e)


# ---------------------------------------------------------------- 解析


def _match_gateway(base_url: str, table: dict) -> tuple[str, dict]:
    """按 `match` 子串找网关条目；找不到返回 ("", {})。"""
    gateways = table.get("gateways") or {}
    url = (base_url or "").lower()
    for name, conf in gateways.items():
        if not isinstance(conf, dict):
            continue
        needle = str(conf.get("match") or "").lower()
        if needle and needle in url:
            return name, conf
    return "", {}


def _group_of(model: str, models_file: str | Path | None) -> str:
    """在 models.txt 里查模型所属分组（清单是分组的唯一真相源）。"""
    for g in load_models(models_file):
        if model in g.models:
            return g.group
    return ""


def _clean(layer: dict, layer_name: str) -> dict:
    """过滤掉非能力键（match 等元数据），未知键给 warning。"""
    out = {}
    for k, v in (layer or {}).items():
        if k in ("match", "note", "comment"):
            continue
        if k not in _KNOWN_KEYS:
            logger.warning("能力表 %s 层出现未知键 %r（已忽略）", layer_name, k)
            continue
        out[k] = v
    return out


def resolve(
    model: str,
    base_url: str = "",
    *,
    table: dict | None = None,
    table_path: str | Path | None = None,
    models_file: str | Path | None = None,
    probe_path: str | Path | None = None,
    probes: dict | None = None,
) -> Capability:
    """解析一个模型的最终能力。四层继承，逐键覆盖。"""
    table = table if table is not None else load_table(table_path)
    if probes is None:
        # 探测结果默认与能力表同目录（同源配置才代表同一个环境）
        probes = load_probes(probe_path if probe_path is not None else probe_path_for(table_path))

    gateway_name, gateway_conf = _match_gateway(base_url, table)
    groups = table.get("groups") or {}
    models_conf = table.get("models") or {}

    merged = dict(DEFAULTS)
    source = "default"

    gw_layer = _clean(gateway_conf, "gateways")
    if gw_layer:
        merged.update(gw_layer)
        source = "gateways"

    group_name = _group_of(model, models_file)
    if group_name and isinstance(groups.get(group_name), dict):
        grp_layer = _clean(groups[group_name], "groups")
        if grp_layer:
            merged.update(grp_layer)
            source = "groups"

    if isinstance(models_conf.get(model), dict):
        mdl_layer = _clean(models_conf[model], "models")
        if mdl_layer:
            merged.update(mdl_layer)
            source = "models"

    if isinstance(probes.get(model), dict):
        probe_layer = _clean(probes[model], "probe")
        if probe_layer:
            merged.update(probe_layer)
            source = "probe"

    cap = Capability(
        model=model,
        gateway=gateway_name,
        supports_temperature=bool(merged["supports_temperature"]),
        supports_tools=bool(merged["supports_tools"]),
        supports_reasoning=bool(merged["supports_reasoning"]),
        reasoning_fields=list(merged["reasoning_fields"] or []),
        thinking_param=merged["thinking_param"],
        echo_reasoning_with_tools=bool(merged["echo_reasoning_with_tools"]),
        must_echo_cross_turn=merged["must_echo_cross_turn"],
        enabled=bool(merged["supported"]),
        reason=str(merged.get("reason") or ""),
        source=source,
    )
    if not cap.enabled and not cap.reason:
        cap.reason = "配置标记为不可用（supported: false）"
    return cap


def needs_echo(cap: Capability, *, thinking_enabled: bool) -> bool:
    """跨轮是否必须回传 reasoning（Spec §5.6 R2-R3）。

    三态而非布尔：DeepSeek 的要求只在开思考模式时成立，关掉后再传纯属浪费 token；
    用布尔会把「没开 thinking」也当成要回传。
    """
    value = cap.must_echo_cross_turn
    if value is True:
        return True
    if isinstance(value, str) and value.strip().lower() == "when_thinking":
        return bool(thinking_enabled)
    return False


def audit(
    models: list[str],
    base_url: str = "",
    *,
    table: dict | None = None,
    table_path: str | Path | None = None,
    models_file: str | Path | None = None,
    probe_path: str | Path | None = None,
) -> list[dict]:
    """启动校验：清单里每个模型都必须解析得出能力（不靠人工记得补配置）。

    返回每个模型的一行摘要；`source == "default"` 表示「清单里有，能力表里没有任何信息」，
    这类模型首次调用应当在真实探测后被 `save_probe()` 补上。
    """
    table = table if table is not None else load_table(table_path)
    rows = []
    for m in models:
        c = resolve(m, base_url, table=table, models_file=models_file, probe_path=probe_path)
        rows.append(
            {
                "model": m,
                "enabled": c.enabled,
                "source": c.source,
                "reason": c.reason,
                "supports_temperature": c.supports_temperature,
            }
        )
    unknown = [r["model"] for r in rows if r["source"] == "default"]
    if unknown:
        logger.info("能力表未覆盖的模型（走保守默认）：%s", ", ".join(unknown))
    return rows


def model_list() -> list[str]:
    """清单里的全部模型名（audit 的默认输入）。"""
    return flatten(load_models())


def clone(cap: Capability, **patch) -> Capability:
    """测试/探测时就地改写一份能力（不动原对象）。"""
    return replace(cap, **patch)
