"""LLM 客户端（S1：同步非流式，OpenAI 兼容接口）。

chat() 返回 LLMCall：要么带最终文本，要么带工具调用列表（或两者都有）。
Reactor 只依赖 chat() 这一个方法——测试时注入 FakeLLM 即可，无需真实 Key。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from .capabilities import Capability, load_table, resolve
from .config import Config

logger = logging.getLogger(__name__)

_TEMPERATURE = 0.2  # 默认采样温度；不支持该参数的模型不下发（见 capability()）

# 试水用的最小工具：真实任务一定带 tools，所以「模型存在」不等于「模型接受 tools」
_PROBE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "noop",
            "description": "探测用空工具（验证模型是否接受 tools 参数）",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]


def _get(obj, name):
    """从 object 或 dict 里安全取字段（流式 chunk 可能是任一形态）。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _tok(usage, attr):
    """从 usage（object 或 dict）安全取 token 数；取不到返回 '?'。"""
    if usage is None:
        return "?"
    if isinstance(usage, dict):
        return usage.get(attr, "?")
    return getattr(usage, attr, "?")


def _usage_dict(usage) -> dict | None:
    """usage（object/dict/None）→ 规整 dict；取不到按 0。"""
    if usage is None:
        return None

    def num(attr):
        v = _tok(usage, attr)
        return v if isinstance(v, int) else 0

    total = num("total_tokens")
    if not total:
        total = num("prompt_tokens") + num("completion_tokens")
    return {
        "prompt_tokens": num("prompt_tokens"),
        "completion_tokens": num("completion_tokens"),
        "total_tokens": total,
    }


def _check_key_ascii(key: str, var: str) -> None:
    """Key 含非 ASCII（多半是 .env 里未替换的示例占位符，如"sk-你的key"）→ 明确报错。

    这类占位符会拼进 Authorization 头，httpx 对非 ASCII 头值直接抛难以理解的编码错误，
    这里提前给出可操作的提示。
    """
    if any(ord(c) > 127 for c in key):
        raise ValueError(
            f"{var} 包含非 ASCII 字符（可能是未替换的示例占位符）。"
            "请在项目根 .env 中填写真实的 API Key（形如 sk-xxx）。"
        )


def _is_thinking_rejection(err: Exception) -> bool:
    """判断异常是不是「思考参数不被接受」。

    只看关键词不看状态码：有的网关是 400，有的是 422，还有的包装成普通异常。
    但关键词必须精确到参数名附近——泛泛匹配 "reasoning" 会把
    "reasoning_content must be passed back" 这类**回传**错误也误判成参数错误。
    """
    text = str(err).lower()
    if "enable_thinking" in text:
        return True
    if "thinking" in text and ("not support" in text or "invalid" in text or "unknown" in text
                               or "unexpected" in text or "unsupported" in text):
        return True
    return False


@dataclass
class LLMCall:
    """一次 LLM 响应的结构化结果"""

    text: str | None = None
    reasoning: str | None = None  # S6.2：真思维链（与 text 是两条独立通道，绝不合并）
    # S6.3：这次 reasoning 是从哪个字段读出来的。回传时必须用**同一个**字段名——
    # 各家叫法不统一（reasoning_content / reasoning），猜错就等于没带。
    reasoning_field: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict | None = None  # token 消耗（记忆压缩/token 标识用）：{prompt_tokens, completion_tokens, total_tokens}
    # tool_calls 元素格式: {"id": str, "name": str, "arguments": dict}


class LLMClient:
    """惰性构造：服务启动时不需要 Key，第一次调用 chat() 时才校验并连接。"""

    def __init__(self, config: Config):
        self.config = config
        self.model = config.llm_model
        self._client = None
        self._cap_cache: dict[str, Capability] = {}  # S6.2：(model|base_url) → 能力，避免每次调用读配置
        # S5.4：压缩专用小模型客户端（AA_SUMM_* 配置；未显式设置时回退主模型三件套）
        self._summ_client = None

    def _get_client(self):
        if self._client is None:
            if not self.config.llm_api_key:
                raise ValueError(
                    "缺少 AA_LLM_API_KEY。请设置环境变量后重试（如 $env:AA_LLM_API_KEY='sk-xxx'）。"
                )
            _check_key_ascii(self.config.llm_api_key, "AA_LLM_API_KEY")
            from openai import OpenAI

            self._client = OpenAI(api_key=self.config.llm_api_key, base_url=self.config.llm_base_url)
        return self._client

    def _create(self, **kwargs):
        """发起请求；思考参数被拒时自愈（Spec §5.6 R7）。

        为什么需要这条兜底：`thinking_param` 是猜出来的（社区约定，不是标准），
        各网关叫法/形状都可能不同。猜错不该让整个任务 400——去掉参数重试一次、
        把结论记进能力表，下次不再发。配置必然过期，得留一条"错了能自己改回来"的路。
        """
        client = self._get_client()
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            if "extra_body" in kwargs and _is_thinking_rejection(e):
                logger.warning(
                    "思考参数被 %s 拒绝，改用普通模式重试（结论已记入能力表）：%s", self.model, e
                )
                self._record_probe(self.model, {"thinking_param": None})
                kwargs.pop("extra_body", None)
                return client.chat.completions.create(**kwargs)
            raise

    def _get_summ_client(self):
        """压缩小模型客户端（记忆压缩/网页正文压缩用；与主模型 key/base_url 相互独立）。"""
        if self._summ_client is None:
            if not self.config.summ_api_key:
                raise ValueError(
                    "缺少压缩模型 Key（AA_SUMM_API_KEY 或 AA_LLM_API_KEY）。"
                    "请设置环境变量后重试。"
                )
            _check_key_ascii(self.config.summ_api_key, "AA_SUMM_API_KEY")
            from openai import OpenAI

            self._summ_client = OpenAI(
                api_key=self.config.summ_api_key,
                base_url=self.config.summ_base_url,
            )
        return self._summ_client

    # ---- 模型切换（模型名运行时热切换；base_url/key 不变时 client 无需重建）----

    def set_model(self, name: str) -> None:
        """运行时切换模型名（只改 model；chat/chat_stream 每次请求都会读 self.model）。"""
        self.model = name
        logger.info("当前模型已切换 name=%s", name)

    # ---- 能力表（S6.2：请求参数按「网关 + 模型」下发，不再硬编码）----

    def capability(self, model: str | None = None) -> Capability:
        """当前模型的能力（按 model+base_url 缓存；换模型/换网关自动失效）。"""
        name = model or self.model
        key = f"{name}|{self.config.llm_base_url}"
        cap = self._cap_cache.get(key)
        if cap is None:
            cap = resolve(
                name,
                self.config.llm_base_url,
                table_path=getattr(self.config, "capabilities_file", None),
                models_file=getattr(self.config, "models_file", None),
            )
            self._cap_cache[key] = cap
        return cap

    def _sampling_kwargs(self, model: str | None = None) -> dict:
        """采样参数：不支持 temperature 的模型**不传**（传了就 400，kimi-k3 已实证）。"""
        cap = self.capability(model)
        return {"temperature": _TEMPERATURE} if cap.supports_temperature else {}

    def _thinking_kwargs(self, model: str | None = None) -> dict:
        """思考模式开关（S6.3）：能力表说「怎么开」+ 配置说「开不开」。

        两层职责不同，不能合并成一层：
        - 能力表描述事实（这个网关的思考参数长什么样），会过期，靠探测自愈
        - `AA_THINKING` 是策略（这次要不要付这份 token 与延迟成本），运行时可变

        `thinking_param` 为 null 表示「没证据说明怎么开」——这种情况下不发，
        因为猜错参数名会直接 400，代价远大于拿不到思维链。
        """
        if not getattr(self.config, "thinking_enabled", False):
            return {}
        cap = self.capability(model)
        if not cap.supports_reasoning or not cap.thinking_param:
            return {}
        # OpenAI SDK 用 extra_body 透传厂商私有参数（enable_thinking / thinking 等）
        return {"extra_body": cap.thinking_param}

    def verify_model(self, name: str) -> tuple[bool, str]:
        """校验模型可用性（S6.2：口径与真实调用一致）。

        旧实现只发 max_tokens=1，不带 temperature、不带 tools、不开 thinking，
        所以 kimi-k3 能通过试水写进 .current_model，然后在第一个真实任务里 400
        （events id=1012 就是这条）。现在按能力表下发同样的参数形状。

        返回 (ok, reason)。错误分类：403 额度/401 key 无效/404 模型不存在/参数不支持/其他。
        """
        cap = self.capability(name)
        if not cap.enabled:
            return False, f"该模型在能力表里标记为不可用：{cap.reason or '未说明原因'}"

        kwargs = {
            "model": name,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
            "tools": _PROBE_TOOLS,  # 真实任务一定带 tools；试水必须覆盖这条路径
            **self._sampling_kwargs(name),
        }
        try:
            client = self._get_client()
            client.chat.completions.create(**kwargs)
            return True, ""
        except Exception as e:
            status = getattr(e, "status_code", None)
            if status is None:
                status = getattr(getattr(e, "response", None), "status_code", None)
            text = str(e)
            if "temperature" in text.lower():
                # 自愈：记下「不支持 temperature」，下次不再下发（配置会过期，探测才是真相）
                self._record_probe(name, {"supports_temperature": False})
                return False, f"该模型不接受 temperature 参数（已记入能力表）：{name}"
            if status == 403:
                return False, f"额度不足或未开通（403）：{name}"
            if status == 401:
                return False, "API Key 无效（401）"
            if status == 404:
                return False, f"模型不存在（404）：{name}"
            return False, f"校验失败：{e}"

    def _record_probe(self, model: str, patch: dict) -> None:
        """把探测结论写回能力表缓存与磁盘（失败不影响主流程）。"""
        try:
            from .capabilities import probe_path_for, save_probe

            save_probe(
                model,
                patch,
                path=probe_path_for(getattr(self.config, "capabilities_file", None)),
            )
        except Exception as e:  # noqa: BLE001 - 写探测结果失败不该让调用失败
            logger.warning("写回探测结果失败：%s", e)
        key = f"{model}|{self.config.llm_base_url}"
        base = self._cap_cache.get(key) or self.capability(model)
        from dataclasses import replace

        self._cap_cache[key] = replace(base, **{k: v for k, v in patch.items() if hasattr(base, k)}, source="probe")


    def summarize(self, history_text: str, max_chars: int = 400) -> tuple[str, dict]:
        """记忆压缩：把一批轮次的完整历史原文压缩成一段中文摘要。

        返回 (摘要文本, usage)。只做一次性投入——换取后续每轮不再重复上传这批原文。
        S5.4：压缩统一走 AA_SUMM_* 小模型配置（未设置时回退主模型），换小模型只改配置。
        """
        client = self._get_summ_client()
        system = (
            "你是对话记忆压缩器。把用户提供的多轮 AI Agent 任务历史压缩成中文摘要："
            "保留每轮的用户要求、关键动作、最终结论；不要复述推理细节和工具输出；"
            f"整段控制在 {max_chars} 字以内。"
        )
        resp = client.chat.completions.create(
            model=self.config.summ_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": history_text},
            ],
            temperature=0.3,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text, _usage_dict(getattr(resp, "usage", None)) or {}

    def compress_text(self, text: str, purpose: str = "网页正文", max_chars: int = 400) -> tuple[str, dict]:
        """通用文本压缩（S5.4）：网页正文等长文本 → 小模型摘要，主模型只见摘要。

        与 summarize 的区别：summarize 面向"多轮对话历史"，compress_text 面向
        "单段长文本"（如搜索结果页面正文），供未来 web_fetch 精读链路使用。
        """
        client = self._get_summ_client()
        system = (
            f"你是内容压缩器。把用户提供的{purpose}压缩成中文摘要："
            "保留与主题直接相关的关键事实、数据、结论；剔除排版、导航、广告与重复内容；"
            f"整段控制在 {max_chars} 字以内。只输出摘要正文，不要任何前后缀。"
        )
        resp = client.chat.completions.create(
            model=self.config.summ_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
        )
        out = (resp.choices[0].message.content or "").strip()
        return out, _usage_dict(getattr(resp, "usage", None)) or {}

    def extract_memories(
        self,
        user_input: str,
        result: str,
        existing: list[dict],
        schema: dict | None = None,
        max_items: int = 3,
        max_chars: int = 100,
    ) -> tuple[list[dict], dict]:
        """S5 长期记忆提炼：本轮 user+result 与已有记忆摘要 → 结构化候选。

        schema（主题语义配置，见 config.DEFAULT_LONG_MEMORY_SCHEMA）决定分类与判断
        标准——机制通用、主题可插拔；传 None 时用内置默认三类。

        返回 (candidates, usage)：
          candidates: [{"action": "add", "category", "content", "keywords"},
                       {"action": "update", "target_id", "content", "keywords"},
                       {"action": "skip", "reason"}]
          空列表表示"本次无有价值信息"（合法，宁缺毋滥）。
        解析失败/异常 → 返回空列表（安全兜底，绝不抛错影响主流程）。
        """
        schema = schema or {}
        categories = schema.get("categories") or {}
        cat_desc = " / ".join(f"{k}（{v}）" for k, v in categories.items())
        if not cat_desc:
            cat_desc = "任意稳定的跨会话事实"
        extra_rules = (schema.get("extract_extra_rules") or "").strip()
        extra_line = f"{extra_rules}\n" if extra_rules else ""
        existing_text = "\n".join(
            f"- id={m.get('id')} [{m.get('category')}] {m.get('content')}"
            for m in existing
        ) or "（暂无已有记忆）"
        system = (
            "你是长期记忆提炼器。根据“本轮任务”与“已有记忆”，决定新增/更新哪些跨会话稳定事实。\n"
            "判断标准（宁缺毋滥，宁可不记不可乱记）：\n"
            f"1. 只提炼这些类别：{cat_desc}。\n"
            "2. 黄金标准：用户下一个新会话会不会再重复说一遍？会→值得记；不会→不记。\n"
            "3. 不记：任务流水、会话内过程细节、可自动重新发现的信息（如文件内容、目录结构）。\n"
            f"4. 每条 content 不超过 {max_chars} 字；拿不准就不记；允许返回空数组（本次无有价值信息）。\n"
            "5. 去重：候选与已有记忆“说的是同一件事”（写法不同也算）→ 用 update 更新旧条目，"
            "不要 add 新条目；完全无冲突 → add。\n"
            f"6. 一次最多输出 {max_items} 条。\n"
            f"{extra_line}"
            "只输出 JSON 数组，不要任何其他文字。"
        )
        user = (
            "已有记忆：\n" + existing_text + "\n\n"
            "本轮任务：\n用户输入：\n" + (user_input or "") + "\n\n"
            "任务结果：\n" + (result or "") + "\n\n"
            "请输出 JSON 数组（每条含 action/category/content/keywords；update 需含 target_id）："
        )
        client = self._get_client()
        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
            )
            text = (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning("长期记忆提炼调用失败: %s", e)
            return [], {}
        usage = _usage_dict(getattr(resp, "usage", None)) or {}
        # 容错解析：剥离 ```json 代码块，取第一个 [ ... ] 数组
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1] if "```" in text[3:] else text
            text = text.strip().lstrip("json").strip()
        try:
            start, end = text.find("["), text.rfind("]")
            if start == -1 or end == -1 or end <= start:
                logger.info("长期记忆提炼：无数组输出（空）len=%d", len(text))
                return [], usage
            data = json.loads(text[start : end + 1])
            items = [d for d in data if isinstance(d, dict) and d.get("action") in ("add", "update", "skip")][:max_items]
            return items, usage
        except Exception as e:
            logger.warning("长期记忆提炼 JSON 解析失败: %s", e)
            return [], usage

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMCall:
        t0 = time.monotonic()
        resp = self._create(
            model=self.model,
            messages=messages,
            tools=tools or None,
            **self._sampling_kwargs(),
            **self._thinking_kwargs(),
        )
        elapsed = time.monotonic() - t0
        msg = resp.choices[0].message
        tool_calls = []
        for tc in msg.tool_calls or []:
            try:
                arguments = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"_raw": tc.function.arguments}
            tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": arguments})
        usage = getattr(resp, "usage", None)
        # S6.2：非流式分支同样要读 reasoning（此前只取 msg.content，真思维链被丢弃）
        reasoning, rfield = self._extract_reasoning([msg])
        logger.info(
            "LLM chat model=%s elapsed=%.2fs prompt=%s completion=%s tool_calls=%d reasoning_len=%d",
            self.model, elapsed,
            _tok(usage, "prompt_tokens"), _tok(usage, "completion_tokens"),
            len(tool_calls), len(reasoning or ""),
        )
        return LLMCall(
            text=msg.content,
            reasoning=reasoning,
            reasoning_field=rfield,
            tool_calls=tool_calls,
            usage=_usage_dict(getattr(resp, "usage", None)),
        )

    def _extract_reasoning(self, sources) -> tuple[str | None, str | None]:
        """按能力表的候选字段名逐个尝试（字段名是社区约定，不是标准）。

        返回 (内容, 命中的字段名)。字段名要带出去——回传时必须用同一个名字，
        用错等于没带，DeepSeek 带 tools 的场景会直接 400。
        """
        fields = self.capability().reasoning_fields or ["reasoning_content", "reasoning"]
        for src in sources:
            for f in fields:
                val = _get(src, f)
                if val:
                    return (val if isinstance(val, str) else str(val)), f
        return None, None

    def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_delta=None,
        should_stop=None,
        on_reasoning_delta=None,
    ) -> LLMCall:
        """流式 chat（S3.1）：逐 token 回调 on_delta(delta_text)，返回完整 LLMCall。

        OpenAI 流式响应中 content / reasoning / tool_calls 分片到达：
        - reasoning 走 `on_reasoning_delta`（S6.2：独立通道，绝不并入 text_parts）
        - content 按 token 增量（delta.content），实时回调
        - tool_calls 按 index 分片累积（id/name 首片给全，arguments 逐片拼接），完成后 json 解析
        """
        cap = self.capability()  # 每个流只解析一次能力表
        t0 = time.monotonic()
        stream = self._create(
            model=self.model,
            messages=messages,
            tools=tools or None,
            **self._sampling_kwargs(),
            **self._thinking_kwargs(),
            stream=True,
            stream_options={"include_usage": True},  # S5.2 修复：流式末尾 chunk 带 usage（token 统计）
        )
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        r_field: str | None = None  # 命中的字段名（回传时要用同一个）
        tc_acc: dict[int, dict] = {}  # index -> {id, name, arguments}
        usage = None
        aborted = False
        rf = cap.reasoning_fields if cap.supports_reasoning else []
        for chunk in stream:
            # 2026-09-26：用户停止 → 流式中断（丢弃未消费 chunk，close 流防泄漏）
            if should_stop and should_stop():
                aborted = True
                break
            if not chunk.choices:
                # 流式末尾可能带 usage（部分实现）
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue
            # S6.2：先取 reasoning（候选字段逐个试），命中就走独立回调，不进 text_parts
            r_chunk = None
            for f in rf:
                r_chunk = _get(delta, f)
                if r_chunk:
                    if r_field is None:
                        r_field = f
                    break
            if r_chunk:
                reasoning_parts.append(r_chunk)
                if on_reasoning_delta:
                    on_reasoning_delta(r_chunk)
            if delta.content:
                text_parts.append(delta.content)
                if on_delta:
                    on_delta(delta.content)
            for tc in delta.tool_calls or []:
                acc = tc_acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    acc["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        acc["name"] = tc.function.name
                    if tc.function.arguments:
                        acc["arguments"] += tc.function.arguments
        if aborted:
            try:
                stream.close()
            except Exception:
                pass
            logger.info("LLM stream aborted（用户停止）model=%s elapsed=%.2fs", self.model, time.monotonic() - t0)
            return LLMCall(text=None, tool_calls=[])  # 中断：丢弃不完整的 text/tool_calls
        elapsed = time.monotonic() - t0
        tool_calls = []
        for idx in sorted(tc_acc):
            acc = tc_acc[idx]
            try:
                arguments = json.loads(acc["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {"_raw": acc["arguments"]}
            tool_calls.append({"id": acc["id"], "name": acc["name"], "arguments": arguments})
        text = "".join(text_parts)
        reasoning = "".join(reasoning_parts) or None
        logger.info(
            "LLM stream model=%s elapsed=%.2fs prompt=%s completion=%s tool_calls=%d text_len=%d reasoning_len=%d",
            self.model, elapsed,
            _tok(usage, "prompt_tokens"), _tok(usage, "completion_tokens"),
            len(tool_calls), len(text), len(reasoning or ""),
        )
        return LLMCall(text=text or None, reasoning=reasoning, reasoning_field=r_field, tool_calls=tool_calls, usage=_usage_dict(usage))
