"""LLM 客户端（S1：同步非流式，OpenAI 兼容接口）。

chat() 返回 LLMCall：要么带最终文本，要么带工具调用列表（或两者都有）。
Reactor 只依赖 chat() 这一个方法——测试时注入 FakeLLM 即可，无需真实 Key。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from .config import Config

logger = logging.getLogger(__name__)


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


@dataclass
class LLMCall:
    """一次 LLM 响应的结构化结果"""

    text: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict | None = None  # token 消耗（记忆压缩/token 标识用）：{prompt_tokens, completion_tokens, total_tokens}
    # tool_calls 元素格式: {"id": str, "name": str, "arguments": dict}


class LLMClient:
    """惰性构造：服务启动时不需要 Key，第一次调用 chat() 时才校验并连接。"""

    def __init__(self, config: Config):
        self.config = config
        self.model = config.llm_model
        self._client = None
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

    def verify_model(self, name: str) -> tuple[bool, str]:
        """校验模型可用性：发一个 max_tokens=1 的最小请求试水。

        返回 (ok, reason)。错误分类：403 额度/401 key 无效/404 模型不存在/其他。
        切换接口先调它，失败即回滚——避免切到不可用模型后所有任务挂掉。
        """
        try:
            client = self._get_client()
            client.chat.completions.create(
                model=name,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
            )
            return True, ""
        except Exception as e:
            status = getattr(e, "status_code", None)
            if status is None:
                status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 403:
                return False, f"额度不足或未开通（403）：{name}"
            if status == 401:
                return False, "API Key 无效（401）"
            if status == 404:
                return False, f"模型不存在（404）：{name}"
            return False, f"校验失败：{e}"

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
        client = self._get_client()
        t0 = time.monotonic()
        resp = client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools or None,
            temperature=0.2,
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
        logger.info(
            "LLM chat model=%s elapsed=%.2fs prompt=%s completion=%s tool_calls=%d",
            self.model, elapsed,
            _tok(usage, "prompt_tokens"), _tok(usage, "completion_tokens"),
            len(tool_calls),
        )
        return LLMCall(text=msg.content, tool_calls=tool_calls, usage=_usage_dict(getattr(resp, "usage", None)))

    def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_delta=None,
        should_stop=None,
    ) -> LLMCall:
        """流式 chat（S3.1）：逐 token 回调 on_delta(delta_text)，返回完整 LLMCall。

        OpenAI 流式响应中 content 与 tool_calls 分片到达：
        - content 按 token 增量（delta.content），实时回调
        - tool_calls 按 index 分片累积（id/name 首片给全，arguments 逐片拼接），完成后 json 解析
        """
        client = self._get_client()
        t0 = time.monotonic()
        stream = client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools or None,
            temperature=0.2,
            stream=True,
            stream_options={"include_usage": True},  # S5.2 修复：流式末尾 chunk 带 usage（token 统计）
        )
        text_parts: list[str] = []
        tc_acc: dict[int, dict] = {}  # index -> {id, name, arguments}
        usage = None
        aborted = False
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
        logger.info(
            "LLM stream model=%s elapsed=%.2fs prompt=%s completion=%s tool_calls=%d text_len=%d",
            self.model, elapsed,
            _tok(usage, "prompt_tokens"), _tok(usage, "completion_tokens"),
            len(tool_calls), len(text),
        )
        return LLMCall(text=text or None, tool_calls=tool_calls, usage=_usage_dict(usage))
