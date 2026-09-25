"""shell_run：执行 shell 命令（S2.2 新增工具，Agent 的"动手"能力）。

对应 v0.3 Shell 执行模块的 MVP 切片：
- 优先 PowerShell（v0.3：MVP 优先 PowerShell；后续可加 cmd/bash 参数）
- 前台 run（v0.3 的后台 start + AbortSignal 联合取消留到后续切片）
- 输出有界：64KB 内存截断（AA_TOOL_MAX_BYTES，契约 §2.3），metadata.truncated=true
- 超时取消：subprocess timeout；超时后 Windows 用 taskkill /T /F 清理进程树（防残留）
- 模型友好环境变量：NO_COLOR=1 / TERM=dumb / PAGER=cat（v0.3）
- PowerShell 输出强制 UTF-8，避免中文乱码；解码时 utf-8 → gbk 双保险
- 非零退出码 → ok=False + stderr 摘要（stdout 内容仍保留给模型分析）

权限边界（重要）：
S2.2 只做"工具能力"本身。高危命令识别、sandbox_mode 三档（read-only /
workspace-write / danger-full-access）、审批 bail 拦截由 S2.3 权限模块落地。
在此之前 shell_run 可执行任意命令，仅限本地开发使用。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from ..models import ToolResult
from .base import BaseTool, ToolSpec

# Windows 下不弹黑色控制台窗口
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 模型友好环境（v0.3：避免 ANSI 颜色、分页器挂起污染输出）
MODEL_FRIENDLY_ENV = {"NO_COLOR": "1", "TERM": "dumb", "PAGER": "cat"}

# PowerShell 输出强制 UTF-8（Windows PowerShell 5.1 默认向管道输出 OEM/GBK，会乱码）
PS_UTF8_PREFIX = "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8;"


def _decode(data: bytes) -> str:
    """UTF-8 → GBK → 兜底 replace，尽量保住中文。"""
    if not data:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    """按字节截断（中文字符 3 字节，按字符切片会超限，须按字节）。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


class ShellRunTool(BaseTool):
    # 规则片段：由 Reactor 注入 System Prompt（提示词动态组装，S2.4 微内核前置）
    prompt_fragment = (
        "【shell_run 使用规则】\n"
        "- 执行破坏性操作（删除 / 覆盖 / 移动 / 清空）前，必须先侦查确认目标：\n"
        "  ① 先列目录或查路径，确认目标存在且名称准确，不要凭空猜路径；\n"
        "  ② 名称不确定或找不到时，先列出父目录找相似项，不猜名硬删；\n"
        "  ③ 仍无法确定时，明确告诉用户让用户确认，绝不擅自扩大删除范围。\n"
        "- 搜索或定位文件时，关键词优先用中文（用户是中文环境），中英文都尝试后再下结论。\n"
        "- 命令有副作用（删除、写入、移动、下载、安装）时，先说明将要做什么再执行。\n"
        "- 一次命令只做一件事，宁可多调几次工具，不要用复杂管道一气呵成。\n"
        "- PowerShell 路径含变量（$env:XXX、$(...)）时必须用双引号包裹，如 "
        "$env:USERPROFILE\\Desktop\"——单引号是字面量，$(whoami) 等不会展开，会导致路径解析失败。"
    )

    spec = ToolSpec(
        name="shell_run",
        description=(
            "执行一条 shell 命令（PowerShell 语法）并返回输出。"
            "适用于查看目录、查系统信息、运行程序、git 操作等需要真实执行的场景。"
            "注意：输出最多保留 64KB，超时会终止命令；命令在工作目录内执行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 PowerShell 命令，如 Get-ChildItem、git status、python --version",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数，默认 30，超过将终止命令",
                },
                "cwd": {
                    "type": "string",
                    "description": "工作目录（绝对路径），默认 ActionAgent 项目根目录",
                },
            },
            "required": ["command"],
        },
    )

    def __init__(self, max_bytes: int = 65536, default_timeout: int = 30):
        self.max_bytes = max_bytes
        self.default_timeout = default_timeout

    def execute(self, arguments: dict) -> ToolResult:
        command = arguments.get("command")
        if not command or not isinstance(command, str) or not command.strip():
            return ToolResult(ok=False, content="", error="缺少 command 参数")

        # 参数规范化（v0.3 request→spec 层：校验并补默认值）
        try:
            timeout = int(arguments.get("timeout", self.default_timeout))
        except (TypeError, ValueError):
            return ToolResult(ok=False, content="", error="timeout 必须是整数")
        timeout = max(1, min(timeout, 600))  # 1s ~ 10min

        cwd = arguments.get("cwd")
        if cwd:
            # 支持 PowerShell 风格环境变量（$env:USERPROFILE 等）展开，避免 Agent 踩坑
            # os.path.expandvars 只认 %VAR%，$env:VAR 需手动展开（S3.4 场景打磨发现）
            cwd = re.sub(
                r"\$env:(\w+)",
                lambda m: os.environ.get(m.group(1), m.group(0)),
                cwd,
            )
            cwd = os.path.expandvars(cwd)
            p = Path(cwd).expanduser()
            if not p.is_dir():
                return ToolResult(ok=False, content="", error=f"工作目录不存在: {cwd}")
            cwd = str(p)
        else:
            cwd = os.getcwd()

        env = {**os.environ, **MODEL_FRIENDLY_ENV}
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            PS_UTF8_PREFIX + command,
        ]

        t0 = time.perf_counter()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as e:
            return ToolResult(ok=False, content="", error=f"无法启动 PowerShell: {e}")

        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # 超时：清理进程树（v0.3：SIGTERM→SIGKILL 升级，Windows 用 taskkill /T /F）
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=5,
                )
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            return ToolResult(
                ok=False,
                content="",
                error=f"命令超时（>{timeout}s），已终止。可减少工作量或增大 timeout 参数",
                metadata={"cwd": cwd, "timeout": timeout, "duration_ms": int((time.perf_counter() - t0) * 1000)},
            )

        duration_ms = int((time.perf_counter() - t0) * 1000)
        stdout = _decode(stdout_bytes)
        stderr = _decode(stderr_bytes)

        # 输出有界（契约 §2.3：≤64KB，截断并标记）
        content, truncated = _truncate(stdout, self.max_bytes)
        if truncated:
            content += f"\n…(输出超过 {self.max_bytes} 字节已截断)"

        exit_code = proc.returncode
        if exit_code != 0:
            return ToolResult(
                ok=False,
                content=content,
                error=f"命令退出码 {exit_code}: {stderr.strip()[:500] or '无错误输出'}",
                metadata={
                    "exit_code": exit_code,
                    "cwd": cwd,
                    "timeout": timeout,
                    "duration_ms": duration_ms,
                    "truncated": truncated,
                    "content_bytes": len(stdout_bytes),
                    "shell": "powershell",
                },
            )

        return ToolResult(
            ok=True,
            content=content,
            metadata={
                "exit_code": 0,
                "cwd": cwd,
                "timeout": timeout,
                "duration_ms": duration_ms,
                "truncated": truncated,
                "content_bytes": len(stdout_bytes),
                "shell": "powershell",
            },
        )
