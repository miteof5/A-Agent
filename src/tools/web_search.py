"""web_search 工具（S5.4 联网能力）：Tavily Search API + 代理管理层。

设计（2026-10-03 与用户拍板，参考 Claude/Tavily 行业共识）：
- 只做搜索，不抓正文（正文精读 web_fetch 后续再做）——窄工具原则
- 参数极简：query（必需）+ max_results（可选，默认 5，上限 10）
- 返回固定结构化文本：标题/链接/来源/摘要，摘要截断 200 字（输出有界，不撑爆上下文）
- 网络链路：ProxyManager（Clash 优先 → v2rayN 兜底）→ Tavily API
- 敏感信息：API key 从配置读取（AA_SEARCH_API_KEY，.env 配置，不进代码/git）
- 权限：只读无副作用，两档权限均默认放行（permissions.py 增加分支）
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from ..config import Config
from ..models import ToolResult
from .base import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_MAX_RESULTS = 10
_SNIPPET_CHARS = 200

_PROMPT_FRAGMENT = """使用 web_search 工具的时机与规则（S5.4）：
- 需要实时信息、最新新闻、验证事实、查外部资料时调用；不要凭空猜测时效性信息。
- 搜索前把问题拆成 1~3 个精确关键词（不要整段照抄）；一次搜索通常够，必要时分多次。
- 结果只是线索不是结论：重要事实要基于多个来源交叉验证；来源冲突时在回答里说明。
- 回答中必须带上链接引用（来源 URL），不得只转述摘要不给出处。
- 搜索失败（代理不可用/无 key）时明确告知用户，不要假装查到了。"""


class WebSearchTool(BaseTool):
    spec = ToolSpec(
        name="web_search",
        description=(
            "搜索互联网获取实时信息（新闻、资料、事实核验、外部数据）。"
            "返回结构化结果：标题/链接/来源/摘要；每条带来源 URL 供引用。"
            "需要时效性信息或验证外部事实时使用；重要结论请多源交叉验证。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词（1~3 个精确词，不要整段问题）",
                },
                "max_results": {
                    "type": "integer",
                    "description": f"返回结果条数上限（1~{_MAX_RESULTS}，默认 5）",
                    "minimum": 1,
                    "maximum": _MAX_RESULTS,
                },
            },
            "required": ["query"],
        },
    )
    prompt_fragment = _PROMPT_FRAGMENT

    def __init__(self, config: Config, proxy_manager=None):
        self.config = config
        self.proxy_manager = proxy_manager  # 注入单例；None 则直连（测试/无代理场景）

    def execute(self, arguments: dict) -> ToolResult:
        query = (arguments.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, content="", error="query 必填")
        max_results = int(arguments.get("max_results") or 5)
        max_results = max(1, min(max_results, _MAX_RESULTS))

        if not self.config.search_api_key:
            return ToolResult(
                ok=False,
                content="",
                error="未配置搜索 API Key（AA_SEARCH_API_KEY）。请在项目根 .env 中填写后重试。",
            )

        # 顺带回收空闲超时的自拉代理（不影响本次搜索）
        if self.proxy_manager is not None:
            try:
                self.proxy_manager.release_idle()
            except Exception:  # noqa: BLE001 - 回收失败不影响搜索
                pass

        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",  # basic 省额度；摘要够用
        }
        # 主请求 + 一次故障转移重试（当前代理失效 → 切下一个候选再试）
        for attempt in (0, 1):
            proxy_url = None
            if self.proxy_manager is not None:
                proxy_url = self.proxy_manager.ensure_proxy()
                if proxy_url is None:
                    return ToolResult(
                        ok=False,
                        content="",
                        error="代理全部不可用（Clash/v2rayN 均未运行且拉起失败）。请检查代理工具后重试。",
                    )
            try:
                text = self._call_tavily(payload, proxy_url)
                if self.proxy_manager is not None:
                    self.proxy_manager.mark_used()
                return ToolResult(ok=True, content=text)
            except _TransientError as e:
                logger.warning("web_search 第 %d 次请求失败：%s", attempt + 1, e)
                if self.proxy_manager is None or attempt == 1:
                    return ToolResult(ok=False, content="", error=f"搜索请求失败：{e}")
                # 切下一个代理再试一次
                continue
            except Exception as e:  # noqa: BLE001 - key 无效等非瞬态错误，不重试
                return ToolResult(ok=False, content="", error=f"搜索失败：{e}")
        return ToolResult(ok=False, content="", error="搜索请求失败（已重试）")

    # ---- 内部 ----
    def _call_tavily(self, payload: dict, proxy_url: str | None) -> str:
        """调 Tavily /search，返回结构化文本；网络类错误抛 _TransientError，4xx 直接抛。"""
        import httpx

        headers = {
            "Authorization": f"Bearer {self.config.search_api_key}",
            "Content-Type": "application/json",
        }
        url = self.config.search_base_url.rstrip("/") + "/search"
        try:
            with httpx.Client(proxy=proxy_url, timeout=20.0) as client:
                resp = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise _TransientError(f"网络请求失败（代理 {proxy_url}）：{e}") from e

        if resp.status_code == 401 or resp.status_code == 403:
            raise ValueError(f"搜索 API Key 无效或额度不足（HTTP {resp.status_code}）")
        if resp.status_code != 200:
            raise ValueError(f"搜索 API 返回异常（HTTP {resp.status_code}）：{resp.text[:200]}")

        data = resp.json()
        results = data.get("results") or []
        if not results:
            return "（未找到相关结果）"

        lines = []
        for i, item in enumerate(results[: payload["max_results"]], start=1):
            title = (item.get("title") or "").strip() or "（无标题）"
            url = (item.get("url") or "").strip()
            source = _extract_source(url) or "未知来源"
            snippet = (item.get("content") or "").strip()
            if len(snippet) > _SNIPPET_CHARS:
                snippet = snippet[:_SNIPPET_CHARS] + "…"
            date = (item.get("published_date") or "").strip()
            lines.append(
                f"结果 {i}：\n标题：{title}\n链接：{url}\n来源：{source}"
                + (f"\n日期：{date}" if date else "")
                + f"\n摘要：{snippet}"
            )
        return "\n\n".join(lines)


class _TransientError(Exception):
    """网络类瞬态错误：可故障转移重试；业务/鉴权错误不是瞬态。"""


def _extract_source(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return host.removeprefix("www.")
    except Exception:  # noqa: BLE001
        return ""
