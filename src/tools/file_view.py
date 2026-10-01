"""file_view：查看文件内容（S1 文本分页 → 2026-10-01 多格式增强）。

支持格式（主流 agent 能读的它都能读）：
- 文本：UTF-8 / GB18030（GBK）自动回退，按行分页，输出有界
- PDF：pdfplumber 提取文本（逐页）
- Word .docx：python-docx 提取段落 + 表格
- Excel .xlsx/.xlsm：openpyxl 按 sheet 转文本（每 sheet 前若干行）
- 图片/扫描件：不支持（本地无视觉模型/OCR 依赖，明确报错，不假装读成功）

分派策略：magic bytes（文件头）优先，扩展名兜底；
多格式提取后的"行"概念统一——offset/limit 作用于提取出的文本行，接口保持不变。

设计约束（v0.3 延续）：
- 只读，不修改任何文件
- 输出有界：超过 max_bytes 截断（"工具输出不撑爆 LLM 上下文"）
- 所有解析库为可选依赖：缺库时对应格式明确报错，不影响文本读取
"""

from __future__ import annotations

from pathlib import Path

from ..models import ToolResult
from .base import BaseTool, ToolSpec

# 可选解析库（pip install pdfplumber python-docx openpyxl）
try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None
try:
    import docx as docx_lib
except ImportError:  # pragma: no cover
    docx_lib = None
try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None

_TEXT_EXTS = {
    ".txt", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".scss", ".html", ".htm",
    ".xml", ".csv", ".log", ".env", ".sh", ".bat", ".ps1", ".sql",
    ".gitignore", ".dockerfile", ".conf", ".properties",
}
_MAX_XLSX_ROWS_PER_SHEET = 200  # Excel 大表截断（提取文本的行上限）


class FileViewTool(BaseTool):
    # 规则片段：由 Reactor 注入 System Prompt（提示词动态组装）
    prompt_fragment = (
        "【file_view 使用规则】\n"
        "- 可读文本/PDF/Word/Excel；读取大文件时用 offset/limit 按行分页，分段读完，不要假设文件内容。\n"
        "- 文件内容以工具返回为准，不凭记忆或猜测描述文件。"
    )

    spec = ToolSpec(
        name="file_view",
        description=(
            "查看文件内容。支持文本（自动识别 UTF-8/GBK）、PDF、Word(.docx)、Excel(.xlsx) 等主流格式；"
            "长文件可 offset/limit 按行分页。"
        ),
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
                    "description": "最多读取多少行，默认 200（上限 1000）",
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

            text, fmt = self._extract_text(p)
            lines = text.splitlines()
            total = len(lines)
            snippet = lines[offset: offset + limit]
            content = "\n".join(snippet)

            # 输出有界：超出 max_bytes 截断（v0.3 硬约束，不降级）
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
                    "format": fmt,
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

    # ---------- 格式分派 ----------

    def _extract_text(self, p: Path) -> tuple[str, str]:
        """按 magic bytes + 扩展名分派，返回 (提取文本, 格式标签)。"""
        magic = self._magic(p)
        if magic == b"%PDF":
            return self._read_pdf(p), "pdf"
        if magic.startswith(b"PK"):  # zip：docx / xlsx
            lower = p.name.lower()
            if lower.endswith((".docx", ".docm")):
                return self._read_docx(p), "docx"
            if lower.endswith((".xlsx", ".xlsm", ".xlsb")):
                return self._read_xlsx(p), "xlsx"
            return (
                f"文件 {p.name} 是 zip 压缩包格式（可能是不支持的 Office 旧格式），"
                f"请用 shell 解压后读取内部文件。",
                "zip",
            )
        # 文本（扩展名或未知）统一按编码回退读
        return self._read_text(p), "text"

    @staticmethod
    def _magic(p: Path) -> bytes:
        with p.open("rb") as f:
            return f.read(4)

    def _read_text(self, p: Path) -> str:
        """文本读取：UTF-8 优先 → GB18030（覆盖 GBK）→ 替换非法字节兜底。"""
        raw = p.read_bytes()
        for enc in ("utf-8", "gb18030"):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    def _read_pdf(self, p: Path) -> str:
        if pdfplumber is None:
            return "PDF 解析库未安装（pip install pdfplumber），无法读取 PDF。"
        parts: list[str] = []
        try:
            with pdfplumber.open(str(p)) as pdf:
                total = len(pdf.pages)
                for i, page in enumerate(pdf.pages, 1):
                    t = page.extract_text() or ""
                    t = t.strip()
                    if t:
                        parts.append(f"[第 {i}/{total} 页]\n{t}")
        except Exception as e:  # 解析失败不抛给主循环
            return f"PDF 解析失败: {e}"
        return "\n\n".join(parts) if parts else "（PDF 无可提取文本，可能是扫描件/图片型 PDF）"

    def _read_docx(self, p: Path) -> str:
        if docx_lib is None:
            return "Word 解析库未安装（pip install python-docx），无法读取 docx。"
        try:
            d = docx_lib.Document(str(p))
            parts = [para.text for para in d.paragraphs if para.text.strip()]
            for i, table in enumerate(d.tables, 1):
                parts.append(f"[表格 {i}]")
                for row in table.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    parts.append(" | ".join(cells))
            return "\n".join(parts) if parts else "（Word 文档无正文内容）"
        except Exception as e:
            return f"docx 解析失败: {e}"

    def _read_xlsx(self, p: Path) -> str:
        if openpyxl is None:
            return "Excel 解析库未安装（pip install openpyxl），无法读取 xlsx。"
        wb = None
        try:
            wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
            parts: list[str] = []
            for ws in wb.worksheets:
                parts.append(f"[Sheet: {ws.title}]")
                count = 0
                for row in ws.iter_rows(values_only=True):
                    if count >= _MAX_XLSX_ROWS_PER_SHEET:
                        parts.append(f"...（该 sheet 超过 {_MAX_XLSX_ROWS_PER_SHEET} 行，已截断）")
                        break
                    cells = ["" if v is None else str(v).replace("\n", " ") for v in row]
                    if any(cells):
                        parts.append(" | ".join(cells))
                    count += 1
            return "\n".join(parts) if parts else "（Excel 为空）"
        except Exception as e:
            return f"xlsx 解析失败: {e}"
        finally:
            if wb is not None:
                try:
                    wb.close()  # read_only 模式需显式关闭，否则 Windows 锁文件
                except Exception:
                    pass
