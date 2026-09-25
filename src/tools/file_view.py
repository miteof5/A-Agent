"""file_view：查看文本文件内容（S1 唯一工具）。

设计要点（对应 v0.3 文件操作模块的最小切片）：
- 只读，不修改任何文件
- 输出有界：超过 max_bytes 截断（v0.3"工具输出不撑爆 LLM 上下文"）
- 支持 offset/limit 按行分页，长文件可分段读完
"""

from __future__ import annotations

from pathlib import Path

from ..models import ToolResult
from .base import BaseTool, ToolSpec


class FileViewTool(BaseTool):
    # 规则片段：由 Reactor 注入 System Prompt（提示词动态组装）
    prompt_fragment = (
        "【file_view 使用规则】\n"
        "- 读取大文件时用 offset/limit 按行分页，分段读完，不要假设文件内容。\n"
        "- 文件内容以工具返回为准，不凭记忆或猜测描述文件。"
    )

    spec = ToolSpec(
        name="file_view",
        description="查看文本文件的内容。支持按行分页读取，适用于源码、配置、日志、文档等文本文件。",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（绝对路径，或相对 ActionAgent 运行目录的路径）",
                },
                "offset": {
                    "type": "integer",
                    "description": "从第几行开始读取（0 起），默认 0",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多读取多少行，默认 200",
                },
            },
            "required": ["path"],
        },
    )

    def __init__(self, max_bytes: int = 65536, max_lines: int = 200):
        self.max_bytes = max_bytes
        self.max_lines = max_lines

    def execute(self, arguments: dict) -> ToolResult:
        path = arguments.get("path")
        if not path or not isinstance(path, str):
            return ToolResult(ok=False, content="", error="缺少 path 参数")

        try:
            offset = max(0, int(arguments.get("offset", 0)))
        except (TypeError, ValueError):
            return ToolResult(ok=False, content="", error="offset 必须是整数")
        try:
            limit = int(arguments.get("limit", self.max_lines))
        except (TypeError, ValueError):
            return ToolResult(ok=False, content="", error="limit 必须是整数")
        limit = max(1, min(limit, 1000))

        try:
            p = Path(path).expanduser()
            if not p.exists():
                return ToolResult(ok=False, content="", error=f"文件不存在: {path}")
            if not p.is_file():
                return ToolResult(ok=False, content="", error=f"不是文件: {path}")

            text = p.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            total = len(lines)
            snippet = lines[offset: offset + limit]
            content = "\n".join(snippet)

            # 输出有界：超出 max_bytes 截断（v0.3 硬约束，S1 不降级）
            truncated = False
            encoded = content.encode("utf-8")
            if len(encoded) > self.max_bytes:
                content = encoded[: self.max_bytes].decode("utf-8", errors="ignore")
                truncated = True

            shown_end = offset + len(snippet)
            if shown_end < total and not truncated:
                content += f"\n...(共 {total} 行，已显示 {shown_end} 行，可设置 offset={shown_end} 继续)"

            return ToolResult(
                ok=True,
                content=content,
                metadata={
                    "path": str(p),
                    "total_lines": total,
                    "lines_shown": len(snippet),
                    "truncated": truncated,
                    "content_bytes": len(content.encode("utf-8")),
                },
            )
        except PermissionError:
            return ToolResult(ok=False, content="", error=f"没有权限读取: {path}")
        except OSError as e:
            return ToolResult(ok=False, content="", error=f"读取失败: {e}")
