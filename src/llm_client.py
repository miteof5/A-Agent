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


@dataclass
class LLMCall:
    """一次 LLM 响应的结构化结果"""

    text: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    # tool_calls 元素格式: {"id": str, "name": str, "arguments": dict}


class LLMClient:
    """惰性构造：服务启动时不需要 Key，第一次调用 chat() 时才校验并连接。"""

    def __init__(self, config: Config):
        self.config = config
        self.model = config.llm_model
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self.config.llm_api_key:
                raise ValueError(
                    "缺少 AA_LLM_API_KEY。请设置环境变量后重试（如 $env:AA_LLM_API_KEY='sk-xxx'）。"
                )
            from openai import OpenAI

            self._client = OpenAI(api_key=self.config.llm_api_key, base_url=self.config.llm_base_url)
        return self._client

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
        return LLMCall(text=msg.content, tool_calls=tool_calls)

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
        return LLMCall(text=text or None, tool_calls=tool_calls)
