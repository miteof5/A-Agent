"""get_current_time 工具：获取当前日期时间（本地时区 + UTC）。

设计（2026-10-04，参考豆包 MainAgent 取时方式的两层结构）：
- 环境基准层：System Prompt 组装时注入"当前日期时间"（reactor.build_system_prompt），
  agent 日常判断"今天几号/周几"直接用基准，不用调工具；
- 实时精确层：本工具——需要精确到秒、计算时间差、跨时区判断时调用。
- 零依赖：datetime.astimezone() 取系统本地时区（自动带偏移），不依赖 tzdata。
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import ToolResult
from .base import BaseTool, ToolSpec

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_PROMPT_FRAGMENT = """使用 get_current_time 工具的时机与规则：
- 需要知道"现在几点/今天几号/周几"、计算时间差、判断时效（如"今天中午前"）时调用；
- 不要凭记忆或猜测报时间，涉及具体时刻必须用本工具获取；
- 返回本地时间（本机时区）与 UTC 两个值，跨时区判断用 UTC。"""


class TimeTool(BaseTool):
    spec = ToolSpec(
        name="get_current_time",
        description=(
            "获取当前日期时间（本机本地时区 + UTC）。"
            "需要知道现在几点/今天日期/周几、计算时间差或跨时区判断时使用。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    prompt_fragment = _PROMPT_FRAGMENT

    def execute(self, arguments: dict) -> ToolResult:
        now_local = datetime.now().astimezone()  # 系统本地时区（自动带偏移，零依赖）
        now_utc = datetime.now(timezone.utc)
        wd = _WEEKDAYS[now_local.weekday()]
        content = (
            f"本地时间：{now_local.strftime('%Y-%m-%d %H:%M:%S')} {wd}（时区 {now_local.tzname()}，"
            f"UTC{now_local.strftime('%z')}）\n"
            f"UTC 时间：{now_utc.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        return ToolResult(ok=True, content=content)
