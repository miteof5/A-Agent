"""权限策略（S2.3：sandbox_mode 三档 + 高危命令识别 + tools/pre-execute bail）。

对应 v0.3 权限与审批模块的 MVP 切片：
- ApprovalOutcome 四态 → MVP 只落两态（allowed / denied），
  真正的用户审批弹窗（approve/reject、always/once/session/deny）留到 S3 前端交互
- 高危操作识别：删除/清空/格式化/系统级命令黑名单（不可逆或影响面大）
- 三档沙箱（v0.3 操作边界）：
    read-only          只读：file_view 放行；shell_run 仅允许只读查询命令
    workspace-write    工作区可写：普通命令放行；危险命令 bail 拦截
    danger-full-access 完全访问：全部放行
- MVP 为应用层限制（非内核级沙箱），按命令级判断，路径级判断后续增强
"""

from __future__ import annotations

import re
from enum import Enum


class SandboxMode(str, Enum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL = "danger-full-access"


class ApprovalOutcome(str, Enum):
    ALLOWED = "allowed"
    NEEDS_APPROVAL = "needs_approval"  # S3.5：workspace-write 高危命令需用户审批
    DENIED = "denied"


# ---- 危险命令规则表（不可逆 / 影响面大；PowerShell 与 cmd 别名都覆盖）----
# 每条 = (类别, 正则, 审批弹窗解释模板)。匹配前先剥离字符串字面量，避免 "Get-Content rm.txt" 误伤。
# 新增危险命令时在此追加一行：is_dangerous 判定与 describe_danger 解释自动同步生效（S3.5 优化）。
_DANGEROUS_RULES: list[tuple[str, str, str]] = [
    # 删除类（不进回收站，不可逆）。短别名（rm/del/rd）要求后跟空白/参数/路径分隔符，
    # 避免误伤 rm.txt、del.old 这类文件名（rd 同理，rmdir 是独立长词不受影响）
    ("delete", r"\bRemove-Item\b", "删除文件/文件夹"),
    ("delete", r"\bRemove-ItemRecurse\b", "删除文件/文件夹"),
    ("delete", r"\brm(?=\s|$|[-\/])", "删除文件/文件夹"),
    ("delete", r"\bdel(?=\s|$|[-\/])", "删除文件/文件夹"),
    ("delete", r"\berase(?=\s|$|[-\/])", "删除文件/文件夹"),
    ("delete", r"\brd(?=\s|$|[-\/])", "删除文件夹"),
    ("delete", r"\brmdir\b", "删除文件夹"),
    ("delete", r"\brmdir\s+/s\b", "删除文件夹（含子目录）"),
    # 清空 / 回收站
    ("clear", r"\bClear-Content\b", "清空文件内容"),
    ("clear", r"\bClear-Item\b", "清空对象内容"),
    ("clear", r"\bClear-RecycleBin\b", "清空回收站"),
    ("clear", r"\bClear-Host\b(?!\s*\()", "清空终端屏幕"),
    # 格式化 / 磁盘
    ("format", r"\bFormat-Volume\b", "格式化磁盘分区"),
    ("format", r"\bformat\s+[a-zA-Z]:", "格式化磁盘"),
    ("format", r"\bdiskpart\b", "磁盘分区管理"),
    # 系统级
    ("power", r"\bShutdown\b", "关闭电脑"),
    ("power", r"\bStop-Computer\b", "关闭电脑"),
    ("power", r"\bRestart-Computer\b", "重启电脑"),
    ("service", r"\bStop-Service\b", "停止服务"),
    ("service", r"\bDisable-Service\b", "禁用服务"),
    ("service", r"\bsc\s+stop\b", "停止服务"),
    # 进程
    ("process", r"\bStop-Process\b", "结束进程"),
    ("process", r"\btaskkill\b", "结束进程"),
    # 危险覆盖
    ("var", r"\bRemove-Variable\s+-Force\b", "强制删除变量"),
]

_DANGEROUS_RE = re.compile("|".join(rule[1] for rule in _DANGEROUS_RULES), re.IGNORECASE)

# ---- 目标提取（审批弹窗解释用）：从命令参数里取出"删哪个/结束哪个进程/格式化哪个盘" ----
# \x22 表示双引号：raw 字符串里避免裸 " 提前结束（Windows 路径含反斜杠，故排除集不含 \）
_PATH_TARGET_RE = re.compile(r"(?:-Path|-LiteralPath)\s+(?:'([^']+)'|\x22([^\x22]+)\x22|(\S+))", re.IGNORECASE)
_PROCESS_TARGET_RE = re.compile(r"(?:-Name|-ProcessName|-Id)\s+([^\s\x22']+)", re.IGNORECASE)
_TASKKILL_TARGET_RE = re.compile(r"/IM\s+([^\s\x22']+)", re.IGNORECASE)
_FORMAT_TARGET_RE = re.compile(r"format\s+([a-zA-Z]:)", re.IGNORECASE)


def _extract_target(command: str, category: str) -> str | None:
    """从命令里提取解释用目标（路径/进程名/盘符）；无目标返回 None。"""
    if category in ("delete", "clear"):
        m = _PATH_TARGET_RE.search(command)
        if m:
            return next((g for g in m.groups() if g), None)
        # 裸路径：取含 : \ / . 且不以 - 开头的词（如 rm C:\temp\x.txt）
        for word in command.split():
            if re.search(r"[:\\/.]", word) and not word.startswith("-"):
                return word.strip("\x22'")
        return None
    if category == "process":
        m = _PROCESS_TARGET_RE.search(command) or _TASKKILL_TARGET_RE.search(command)
        return m.group(1) if m else None
    if category == "format":
        m = _FORMAT_TARGET_RE.search(command)
        return m.group(1) if m else None
    return None  # power / service / var 无目标，只给类别解释


def describe_danger(command: str) -> str:
    """S3.5：给高危命令生成人话解释（审批弹窗用）。命中类别 + 提取目标；未识别给兜底。"""
    stripped = _strip_string_literals(command)
    for _category, pattern, desc in _DANGEROUS_RULES:
        if re.search(pattern, stripped, re.IGNORECASE):
            target = _extract_target(command, _category)
            return f"{desc}（目标：{target}）" if target else desc
    return "高危操作（未识别类别，请谨慎判断）"

# ---- 只读白名单（read-only 模式允许的 shell 命令）----
_READONLY_PATTERNS = [
    r"\bGet-\w+\b",              # Get-ChildItem / Get-Content / Get-Process ...
    r"\bSelect-\w+\b",           # Select-Object
    r"\bMeasure-\w+\b",          # Measure-Object
    r"\bSort-\w+\b", r"\bWhere-Object\b", r"\bFind-\w+\b",
    r"\bTest-\w+\b",             # Test-Path / Test-Connection
    r"\bFormat-\w+\b",           # Format-Table / Format-List（注意与 Format-Volume 区分：危险列表优先）
    r"\bWrite-Output\b", r"\becho\b",
    r"\bdir\b", r"\bls\b", r"\bcd\b", r"\bpwd\b", r"\bcls\b", r"\bclear\b",
    r"\bgit\s+(status|log|diff|branch|show|remote|rev-parse)\b",
    r"\bpython\s+--version\b", r"\bpython\s+-V\b", r"\bpython\s+-c\b",
    r"\bGet-Location\b", r"\bGet-Date\b", r"\bGet-Help\b", r"\bGet-Command\b",
]

_READONLY_RE = re.compile("|".join(_READONLY_PATTERNS), re.IGNORECASE)

_STRING_LITERAL_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def _strip_string_literals(command: str) -> str:
    """剥离命令中的字符串字面量，避免 '删除 rm.txt' 这类内容误触发危险判断。"""
    return _STRING_LITERAL_RE.sub(" ", command)


def _is_dangerous(command: str) -> bool:
    return bool(_DANGEROUS_RE.search(_strip_string_literals(command)))


def _is_readonly(command: str) -> bool:
    return bool(_READONLY_RE.search(_strip_string_literals(command)))


class PermissionPolicy:
    """按任务的 sandbox_mode 对工具调用做审批。"""

    def check(self, sandbox_mode: str, tool_name: str, arguments: dict) -> tuple[ApprovalOutcome, str]:
        """返回 (结果, 原因)。tools/pre-execute bail 拦截点。"""
        mode = sandbox_mode
        try:
            SandboxMode(mode)
        except ValueError:
            return ApprovalOutcome.DENIED, f"未知的沙箱模式: {mode}"

        # file_view：纯只读，三档都放行
        if tool_name == "file_view":
            return ApprovalOutcome.ALLOWED, ""

        # shell_run：按模式判断
        if tool_name == "shell_run":
            command = (arguments.get("command") or "").strip()
            if not command:
                return ApprovalOutcome.DENIED, "命令为空"

            if mode == SandboxMode.DANGER_FULL.value:
                return ApprovalOutcome.ALLOWED, ""

            if mode == SandboxMode.READ_ONLY.value:
                if _is_readonly(command) and not _is_dangerous(command):
                    return ApprovalOutcome.ALLOWED, ""
                return (
                    ApprovalOutcome.DENIED,
                    f"当前沙箱模式为 read-only，仅允许只读查询命令（如 Get-ChildItem、Test-Path）。"
                    f"请切换 workspace-write 或 danger-full-access 后重试",
                )

            # workspace-write：普通命令放行，危险命令需用户审批（S3.5 弹窗；read-only 仍直接拒）
            if _is_dangerous(command):
                return (
                    ApprovalOutcome.NEEDS_APPROVAL,
                    f"检测到高危命令，需要用户审批：{command[:120]}。"
                    f"批准后执行，拒绝则跳过；也可切换 danger-full-access 免审批",
                )
            return ApprovalOutcome.ALLOWED, ""

        # 未知工具：放行（Reactor 会兜底 unknown tool 错误）
        return ApprovalOutcome.ALLOWED, ""
