"""web_fetch 工具（S5.5 网页正文精读）：与 web_search 配合，构成"搜索→精读"完整链路。

设计（2026-10-03，延续 S5.4 拍板的方向）：
- web_search 只给线索（标题/摘要/链接）；web_fetch 精读选中 URL 的正文。
- 正文提取用标准库 HTMLParser（跳过 script/style/noscript 噪声），零新增依赖。
- 长正文 → 走 AA_SUMM_* 小模型压缩通道（compress_text）压成中文摘要；
  短正文直接返回（≤ max_chars），小文本不浪费一次 LLM 调用。
- 网络链路复用 ProxyManager（Clash 优先 → v2rayN 兜底），瞬态错误 failover 重试一次。
- 输出有界：响应体限 2MB、交小模型前正文截 50K 字符、最终返回 ≤ max_chars + 元信息。
- 权限：只读无副作用 → 两档均放行（permissions.py 增加分支）。
- 敏感信息：本工具零 key；代理配置走 proxy_config.json（可提交）。
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

from ..config import Config
from ..models import ToolResult
from .base import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 2 * 1024 * 1024  # 响应体上限 2MB（防抓超大型文件）
_MAX_COMPRESS_CHARS = 50_000  # 交小模型压缩前的正文截断（防超长输入）
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_PROMPT_FRAGMENT = """使用 web_fetch 工具的时机与规则（S5.5）：
- web_search 只给线索（标题/摘要/链接）；需要核实或精读某个链接的正文时调用 web_fetch（走代理抓取）。
- 传入的 URL 应来自搜索结果"链接"字段或用户明确提供；只抓 http/https。
- 正文默认压缩为中文摘要（max_chars 默认 400）；回答引用该链接时必须注明来源 URL。
- 若返回"代理不可用"类错误：先直接重试一次 web_fetch（工具会自动重新拉起代理并测试）；
  仍失败则告知用户手动打开代理工具。
- 抓取失败（目标非网页/超 2MB）时如实告知用户，不要编造内容。
- 需要对比多个来源时，逐个 web_fetch 精读后再下结论。"""


class WebFetchTool(BaseTool):
    spec = ToolSpec(
        name="web_fetch",
        description=(
            "抓取并精读网页正文（与 web_search 配合：搜索给线索，本工具读正文）。"
            "返回标题 + 正文摘要；长正文自动走小模型压缩。需要核实链接内容时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要精读的网页 URL（来自搜索结果链接或用户提供，仅 http/https）",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "正文摘要/截断字数上限（100~2000，默认 400）",
                    "minimum": 100,
                    "maximum": 2000,
                },
            },
            "required": ["url"],
        },
    )
    prompt_fragment = _PROMPT_FRAGMENT

    def __init__(self, config: Config, proxy_manager=None, llm=None):
        self.config = config
        self.proxy_manager = proxy_manager  # 复用代理管理层单例
        self.llm = llm  # LLMClient：长正文走压缩通道（llm 为 None 时降级截断）

    def execute(self, arguments: dict) -> ToolResult:
        url = (arguments.get("url") or "").strip()
        if not url:
            return ToolResult(ok=False, content="", error="url 必填")
        scheme = urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            return ToolResult(ok=False, content="", error=f"仅支持 http/https 链接，收到: {scheme or '空'}")
        max_chars = int(arguments.get("max_chars") or 400)
        max_chars = max(100, min(max_chars, 2000))

        # 主请求 + 一次故障转移重试
        for attempt in (0, 1):
            proxy_url = None
            if self.proxy_manager is not None:
                proxy_url = self.proxy_manager.ensure_proxy()
                if proxy_url is None:
                    return ToolResult(
                        ok=False,
                        content="",
                        error="代理全部不可用（已自动尝试拉起 Clash/v2rayN 但未就绪）。"
                        "请先重试一次本抓取（工具会自动重新拉起代理）；仍失败则告知用户。",
                    )
            try:
                title, body = self._fetch_text(url, proxy_url)
                return self._render(title, url, body, max_chars)
            except _TransientError as e:
                logger.warning("web_fetch 第 %d 次请求失败：%s", attempt + 1, e)
                if self.proxy_manager is None or attempt == 1:
                    return ToolResult(ok=False, content="", error=f"抓取失败：{e}")
                continue  # 切下一个代理再试一次
            except Exception as e:  # noqa: BLE001 - 内容类错误（非网页/超限等）不重试
                return ToolResult(ok=False, content="", error=f"抓取失败：{e}")
        return ToolResult(ok=False, content="", error="抓取失败（已重试）")

    # ---- 内部 ----
    def _fetch_text(self, url: str, proxy_url: str | None) -> tuple[str, str]:
        """抓取 URL → (标题, 正文文本)。网络类错误抛 _TransientError，内容类直接抛 ValueError。"""
        import httpx

        try:
            with httpx.Client(
                proxy=proxy_url, timeout=20.0, follow_redirects=True, headers={"User-Agent": _UA}
            ) as client, client.stream("GET", url) as resp:
                ctype = resp.headers.get("content-type", "").lower()
                if "html" not in ctype and "text/plain" not in ctype:
                    raise ValueError(f"目标不是网页文本（Content-Type: {ctype or '未知'}），web_fetch 仅精读网页")
                clen = resp.headers.get("content-length")
                if clen and int(clen) > _MAX_BODY_BYTES:
                    raise ValueError(f"目标过大（Content-Length {clen} 字节），超过 2MB 上限")
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > _MAX_BODY_BYTES:
                        raise ValueError("目标超过 2MB 上限，已停止抓取")
        except httpx.HTTPError as e:
            raise _TransientError(f"网络请求失败（代理 {proxy_url}）：{e}") from e

        raw = b"".join(chunks)
        # 解码：httpx 自动检测编码；若 replacement 字符比例高（乱码）→ 回退 GBK（中文站点）
        text = raw.decode(resp.encoding or "utf-8", errors="replace")
        if text.count("\ufffd") > max(2, len(text) // 500):
            try:
                text = raw.decode("gbk", errors="replace")
            except Exception:  # noqa: BLE001
                pass

        parser = _TextExtractor()
        try:
            parser.feed(text)
        except Exception as e:  # noqa: BLE001 - HTML 解析异常不致命
            logger.debug("HTML 解析异常：%s", e)
        title = (parser.title or "").strip()
        body = "\n".join(parser.text_parts)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()  # 压多余空行
        return title, body

    def _render(self, title: str, url: str, body: str, max_chars: int) -> ToolResult:
        source = _extract_source(url)
        head = f"标题：{title or '（无标题）'}\n链接：{url}\n来源：{source}\n"
        if len(body) <= max_chars:
            return ToolResult(ok=True, content=head + "正文：\n" + body)

        # 长正文 → 小模型压缩通道（LLM 缺失时降级为截断，仍保持输出有界）
        body_cut = body[:_MAX_COMPRESS_CHARS]
        if self.llm is None:
            return ToolResult(ok=True, content=head + f"正文（截断，原文 {len(body)} 字）：\n{body_cut[:max_chars]}…")
        try:
            summary, _usage = self.llm.compress_text(body_cut, purpose="网页正文", max_chars=max_chars)
        except Exception as e:  # noqa: BLE001 - 压缩失败不应让主任务崩溃
            return ToolResult(ok=False, content="", error=f"正文压缩失败：{e}")
        return ToolResult(ok=True, content=head + f"（原文 {len(body)} 字，已压缩为 {max_chars} 字内摘要）\n摘要：\n{summary}")


class _TransientError(Exception):
    """网络类瞬态错误：可故障转移重试；内容类错误不是瞬态。"""


class _TextExtractor(HTMLParser):
    """正文提取：优先 <main>/<article> 区域内文本；跳过 script/style/nav/header/footer 等噪声区。

    无 main/article 时退回收集全部 body 文本（标题永远收集）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0  # 当前在噪声标签内的嵌套深度
        self._skip_tags = {"script", "style", "noscript", "template", "nav", "header", "footer", "aside", "form"}
        self._main_depth: list[int] = []  # main/article 出现时的 tag 栈深度
        self._stack_depth = 0
        self.text_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self._skip_tags:
            self._skip_depth += 1
        elif t == "title":
            self._in_title = True
        elif t in ("main", "article") and not self._main_depth:
            self._main_depth.append(self._stack_depth)
        self._stack_depth += 1

    def handle_endtag(self, tag):
        t = tag.lower()
        self._stack_depth = max(0, self._stack_depth - 1)
        if t in self._skip_tags:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif t == "title":
            self._in_title = False
        elif t in ("main", "article") and self._main_depth and self._main_depth[-1] > self._stack_depth:
            # main/article 已闭合（栈深度回到进入点之前）
            self._main_depth.pop()

    def _in_main(self) -> bool:
        """优先只收 main/article 内文本；没有该结构时收全部 body。"""
        return not self._main_depth or self._stack_depth >= self._main_depth[0]

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip_depth > 0 or not self._in_main():
            return
        if data.strip():
            self.text_parts.append(data.strip())


def _extract_source(url: str) -> str:
    try:
        return urlparse(url).netloc.removeprefix("www.")
    except Exception:  # noqa: BLE001
        return ""
