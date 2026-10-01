"""file_write：写文本文件（S5.3，agent 元能力自提需求的兑现；S5.3+ 精准编辑）。

设计要点：
- 只写文本类内容（一切文本格式：.txt/.md/.json/.yaml/.toml/.csv/.py/.js/.html 等），
  统一 UTF-8 写入——根治 shell 重定向写中文乱码/转义不可控的问题
- mode：overwrite（默认，覆盖已有）/ append（追加）/ replace（精准替换）
- replace 对齐 Anthropic SWE-bench 验证的最佳实践（Claude Code Edit 工具）：
  * old_text 必须精确匹配（容忍 CRLF/LF 换行差异），且在整个文件中唯一
  * 0 匹配 / 多处匹配 → 明确报错，让模型核对后重试（绝不静默误改）
  * content 必须与 old_text 不同（相同无意义，拒绝）
  * 一个 replace 同时覆盖三种诉求：改（old→new）/ 删（old→空串）/ 插（old=锚点，new=锚点+新内容）
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
        "- 覆盖已有文件前先确认；追加内容用 mode=append。\n"
        "- 精准编辑（只改某行/某段、删某段、在某处插入）用 mode=replace：old_text 定位、content 是新文本，"
        "old_text 必须唯一匹配（工具会校验，找不到或找到多处会报错，此时用 file_view 核对后重试）。\n"
        "- 写 JSON 等结构化内容时确保内容合法（工具会回读校验，失败会报错）。"
    )

    spec = ToolSpec(
        name="file_write",
        description=(
            "写文本文件（UTF-8）。支持覆盖(overwrite)、追加(append)与精准替换(replace)；"
            "replace 可只改某段/删某段/在某处插入，不重写整个文件。"
            "适用于源码、配置、文档、JSON/YAML/CSV 等文本类文件。"
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
                    "description": "要写入的内容（UTF-8 文本）；mode=replace 时它是替换后的新文本(new_text)",
                },
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append", "replace"],
                    "description": (
                        "overwrite=覆盖已有文件（默认）；append=追加到文件末尾；"
                        "replace=精准替换（old_text 定位、content 替换，仅唯一匹配时执行）"
                    ),
                },
                "old_text": {
                    "type": "string",
                    "description": "仅 mode=replace 时必填：要替换的旧文本，必须精确匹配且在整个文件中唯一",
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
        if mode not in ("overwrite", "append", "replace"):
            return ToolResult(ok=False, content="", error=f"mode 仅支持 overwrite/append/replace，收到: {mode}")

        try:
            p = Path(path).expanduser()
        except OSError as e:
            return ToolResult(ok=False, content="", error=f"路径无效: {path}（{e}）")

        if mode == "replace":
            return self._replace(p, content, arguments)
        return self._write(p, content, mode)

    # ---------- overwrite / append ----------

    def _write(self, p: Path, content: str, mode: str) -> ToolResult:
        data = content.encode("utf-8")
        if len(data) > self.max_bytes:
            return ToolResult(
                ok=False, content="", error=f"内容 {len(data)} 字节超过单次写入上限 {self.max_bytes}"
            )
        try:
            existed = p.exists()
            if mode == "append":
                with p.open("a", encoding="utf-8") as f:
                    f.write(content)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                with p.open("w", encoding="utf-8", newline="\n") as f:
                    f.write(content)

            notes, json_valid = self._verify(p, content)
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
            return ToolResult(ok=False, content="", error=f"没有权限写入: {p}")
        except OSError as e:
            return ToolResult(ok=False, content="", error=f"写入失败: {e}")

    # ---------- replace：精准替换（唯一匹配才执行，对齐 Anthropic SWE-bench 结论） ----------

    def _replace(self, p: Path, new_text: str, arguments: dict) -> ToolResult:
        old_text = arguments.get("old_text")
        if not old_text or not isinstance(old_text, str) or not old_text.strip():
            return ToolResult(
                ok=False, content="", error="mode=replace 时必须提供非空 old_text（要定位替换的旧文本）"
            )
        if old_text == new_text:
            return ToolResult(
                ok=False, content="", error="new_text（content）必须与 old_text 不同（相同内容无需替换）"
            )

        try:
            if not p.exists():
                return ToolResult(ok=False, content="", error=f"要编辑的文件不存在: {p}（replace 只改已有文件，新建请用 overwrite）")
            # universal newlines 读入（\r\n → \n），与归一化后的 old_text 对称匹配
            original = p.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            return ToolResult(ok=False, content="", error=f"文件不是 UTF-8 可读文本，无法精准替换: {p}")
        except OSError as e:
            return ToolResult(ok=False, content="", error=f"读取失败: {e}")

        norm_old = old_text.replace("\r\n", "\n").replace("\r", "\n")
        count = original.count(norm_old)
        if count == 0:
            return ToolResult(
                ok=False,
                content="",
                error="未找到要替换的内容（old_text 在文件中无匹配）。请先用 file_view 核对文件实际内容后再重试",
            )
        if count > 1:
            return ToolResult(
                ok=False,
                content="",
                error=(
                    f"old_text 在文件中出现 {count} 处，无法唯一替换（拒绝猜测）。"
                    "请提供更长/更独特的上下文片段使 old_text 唯一，或改用 overwrite 整文件覆盖"
                ),
            )

        new_content = original.replace(norm_old, new_text, 1)
        data = new_content.encode("utf-8")
        if len(data) > self.max_bytes:
            return ToolResult(
                ok=False, content="", error=f"替换后文件 {len(data)} 字节超过单次写入上限 {self.max_bytes}"
            )

        try:
            with p.open("w", encoding="utf-8", newline="\n") as f:
                f.write(new_content)
            notes, json_valid = self._verify(p, new_content)
            return ToolResult(
                ok=True,
                content=f"已替换 1 处（{len(norm_old)} 字符 → {len(new_text)} 字符）到 {p}",
                metadata={
                    "path": str(p),
                    "mode": "replace",
                    "bytes_written": len(data),
                    "existed": True,
                    "json_valid": json_valid,
                    "replaced_occurrences": 1,
                    "notes": notes,
                },
            )
        except PermissionError:
            return ToolResult(ok=False, content="", error=f"没有权限写入: {p}")
        except OSError as e:
            return ToolResult(ok=False, content="", error=f"写入失败: {e}")

    # ---------- 回读校验 ----------

    @staticmethod
    def _verify(p: Path, content: str) -> tuple[list[str], bool | None]:
        """写入后回读校验：UTF-8 可读；JSON 内容再次解析验证。返回 (notes, json_valid)。"""
        read_back = p.read_text(encoding="utf-8", errors="strict")
        is_json = FileWriteTool._looks_like_json(content)
        json_valid = None
        if is_json:
            try:
                json.loads(read_back)
                json_valid = True
            except Exception:
                json_valid = False
        notes: list[str] = []
        if is_json and json_valid is False:
            notes.append("内容为 JSON 但回读解析失败（可能语法不合法）")
        return notes, json_valid

    @staticmethod
    def _looks_like_json(text: str) -> bool:
        t = text.strip()
        return t.startswith("{") or t.startswith("[")
