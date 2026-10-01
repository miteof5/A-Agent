"""file_write：写文本文件（S5.3，agent 元能力自提需求的兑现）。

设计要点：
- 只写文本类内容（一切文本格式：.txt/.md/.json/.yaml/.toml/.csv/.py/.js/.html 等），
  统一 UTF-8 写入——根治 shell 重定向写中文乱码/转义不可控的问题
- mode：overwrite（默认，覆盖已有）/ append（追加）
- 写入后回读校验：UTF-8 可读；若内容为合法 JSON，则回读后再次 json.loads 校验
  （防模型产出坏 JSON），校验结果随 metadata 返回
- 修改性操作 → 由权限插件在 on-demand 档弹窗审批（permissions.py），full-access 放行
- 不写二进制（zip/图片/PDF 等不属于写文件职责）
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models import ToolResult
from .base import BaseTool, ToolSpec


class FileWriteTool(BaseTool):
    # 规则片段：由 Reactor 注入 System Prompt（提示词动态组装）
    prompt_fragment = (
        "【file_write 使用规则】\n"
        "- 写文件一律用 file_write（UTF-8，中文不乱码），不要用 shell 拼字符串写文件（转义与编码不可控）。\n"
        "- 覆盖已有文件前先确认；需要追加内容时用 mode=append。\n"
        "- 写 JSON 等结构化内容时确保内容合法（工具会回读校验，失败会报错）。"
    )

    spec = ToolSpec(
        name="file_write",
        description=(
            "写文本文件（UTF-8）。支持覆盖与追加；适用于源码、配置、文档、JSON/YAML/CSV 等文本类文件。"
            "修改性操作，on-demand 模式下会请求用户审批。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（绝对路径，或相对 ActionAgent 运行目录的路径）",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整内容（UTF-8 文本）",
                },
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "description": "overwrite=覆盖已有文件（默认）；append=追加到文件末尾",
                },
            },
            "required": ["path", "content"],
        },
    )

    def __init__(self, max_bytes: int = 65536):
        self.max_bytes = max_bytes  # 单次写入内容上限（防撑爆）

    def execute(self, arguments: dict) -> ToolResult:
        path = arguments.get("path")
        content = arguments.get("content")
        if not path or not isinstance(path, str):
            return ToolResult(ok=False, content="", error="缺少 path 参数")
        if content is None or not isinstance(content, str):
            return ToolResult(ok=False, content="", error="缺少 content 参数（须为字符串）")

        mode = arguments.get("mode", "overwrite")
        if mode not in ("overwrite", "append"):
            return ToolResult(ok=False, content="", error=f"mode 仅支持 overwrite/append，收到: {mode}")

        data = content.encode("utf-8")
        if len(data) > self.max_bytes:
            return ToolResult(
                ok=False, content="", error=f"内容 {len(data)} 字节超过单次写入上限 {self.max_bytes}"
            )

        try:
            p = Path(path).expanduser()
            existed = p.exists()
            if mode == "append":
                with p.open("a", encoding="utf-8") as f:
                    f.write(content)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("w", encoding="utf-8", newline="\n") as f:
                    f.write(content)

            # 回读校验：UTF-8 可读；JSON 内容再次解析验证
            read_back = p.read_text(encoding="utf-8", errors="strict")
            is_json = self._looks_like_json(content)
            json_valid = None
            if is_json:
                try:
                    json.loads(read_back)
                    json_valid = True
                except Exception:
                    json_valid = False

            notes = []
            if is_json and json_valid is False:
                notes.append("内容为 JSON 但回读解析失败（可能语法不合法）")
            return ToolResult(
                ok=True,
                content=f"已{'覆盖' if mode == 'overwrite' else '追加'}写入 {len(data)} 字节到 {p}",
                metadata={
                    "path": str(p),
                    "mode": mode,
                    "bytes_written": len(data),
                    "existed": existed,
                    "json_valid": json_valid,
                    "notes": notes,
                },
            )
        except PermissionError:
            return ToolResult(ok=False, content="", error=f"没有权限写入: {path}")
        except OSError as e:
            return ToolResult(ok=False, content="", error=f"写入失败: {e}")

    @staticmethod
    def _looks_like_json(text: str) -> bool:
        t = text.strip()
        return t.startswith("{") or t.startswith("[")
