"""RepeatGuard 防死循环插件（S5.1）：识别重复模式，温和纠偏 + 强硬兜底。

背景：现有硬上限（max_steps_per_turn / max_llm_calls_per_session）只数次数、
不认重复模式——一个任务可能 50 步里 45 步都在重复同一个动作，照样跑满才失败。
本插件补"认模式"这一层，挂两个独立检测通道（互不干扰、各自计数）：

- 通道 A（tools/pre-execute bail，priority=50，权限检查之后）：
  同工具 + 同参数（json 归一化后完全相等）连续出现 ≥ soft_limit 次 →
  **不执行工具**，把纠偏文本作为工具结果喂回 LLM，让它自行换思路；
  纠偏后仍重复累计 ≥ hard_limit 次 → hard 拦截（metadata repeat_guard=hard，
  Reactor 检测后走既有失败通道终止任务）。

- 通道 B（tools/post-execute waterfall）：
  同工具 + 结果 content 完全相同连续出现 ≥ soft_limit 次（参数可能不同但
  输出一样，如反复读不同文件拿到相同内容）→ 在工具结果上附加纠偏文本；
  达到 hard → 标记 metadata repeat_guard=hard。

状态隔离：**thread-local**。架构事实是"一个任务一个后台线程"
（TaskManager.start 每任务建线程），线程即任务，天然隔离：
无需 task_id 传递、无需全局锁、任务结束线程结束即自动清理（无泄漏）。
bail 事件不带 task_id 的问题因此绕开。

原则：soft 只提示不中断（误判代价低）；hard 才终止（防烧完 200 次调用）。
hard 拦截不新增接口——Reactor 检查 metadata 后 _fail + agent/error，前端无需改动。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading

from ..kernel.plugin import AgentContext, BasePlugin
from ..models import ToolResult

logger = logging.getLogger(__name__)

# 默认阈值（可用环境变量 AA_REPEAT_SOFT_LIMIT / AA_REPEAT_HARD_LIMIT 覆盖，见 config.py）
DEFAULT_SOFT_LIMIT = 3
DEFAULT_HARD_LIMIT = 3


def _norm_args(arguments: dict) -> str:
    """参数归一化：key 排序 + 中文不转义，保证"同一调用"可比较。"""
    try:
        return json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)
    except Exception:
        return repr(arguments)


def _content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _correction_text(tool_name: str, count: int, channel: str) -> str:
    """温和纠偏文本：作为工具结果喂回 LLM，引导换思路（不打断任务）。"""
    hint = "参数与上一次完全相同" if channel == "call" else "返回结果与上一次完全相同"
    return (
        f"[RepeatGuard] 你已连续 {count} 次调用 {tool_name} 且{hint}，"
        "说明当前方法没有进展。请换一种方法：检查上一轮结果、换路径/参数/工具，"
        "或直接向用户说明你卡住了，不要重复同一个调用。"
    )


class RepeatGuardPlugin(BasePlugin):
    name = "repeat_guard"

    def __init__(self, soft_limit: int = DEFAULT_SOFT_LIMIT, hard_limit: int = DEFAULT_HARD_LIMIT):
        self.soft_limit = soft_limit
        self.hard_limit = hard_limit
        self.ctx: AgentContext | None = None
        self._local = threading.local()  # 线程即任务：每线程独立检测状态

    def _state(self) -> dict:
        """取当前线程（=当前任务）的检测状态，首次访问自动初始化。"""
        st = getattr(self._local, "state", None)
        if st is None:
            st = {
                "last_call": None,     # (tool, args_norm) 上一次发起/拦截的调用
                "call_repeats": 0,     # 同工具同参数连续次数
                "call_soft_hits": 0,   # 已纠偏次数（通道 A）
                "last_result": None,   # (tool, content_hash) 上一次工具结果
                "result_repeats": 0,   # 同工具同结果连续次数
                "result_soft_hits": 0, # 已纠偏次数（通道 B）
            }
            self._local.state = st
        return st

    def setup(self, ctx: AgentContext) -> None:
        self.ctx = ctx
        # 通道 A：执行前拦截（priority=50，低于权限检查 100——先过权限再查重复）
        ctx.events.on("tools/pre-execute", self._pre_execute, priority=50)
        # 通道 B：执行后瀑布流（可修改 ToolResult）
        ctx.events.on("tools/post-execute", self._post_execute)

    def teardown(self) -> None:
        self.ctx = None

    # ---- 通道 A：同工具同参数（执行前拦截）----

    def _pre_execute(self, tool_name: str, arguments: dict, sandbox_mode: str) -> ToolResult | None:
        """bail 拦截：命中 soft → 返回纠偏 ToolResult（工具不执行）；hard → 终止标记。"""
        st = self._state()
        key = (tool_name, _norm_args(arguments))
        if st["last_call"] == key:
            st["call_repeats"] += 1
        else:
            st["last_call"] = key
            st["call_repeats"] = 1
            st["call_soft_hits"] = 0  # 换方法 → 纠偏计数清零
        repeats = st["call_repeats"]
        if repeats < self.soft_limit:
            return None  # 未达阈值，放行执行

        st["call_soft_hits"] += 1
        if st["call_soft_hits"] >= self.hard_limit:
            # hard：Reactor 检测 metadata 后终止任务
            logger.warning(
                "RepeatGuard hard（通道A-同参数）tool=%s repeats=%d soft_hits=%d",
                tool_name, repeats, st["call_soft_hits"],
            )
            return ToolResult(
                ok=False,
                content="",
                error=f"检测到重复死循环：连续 {repeats} 次调用 {tool_name} 且参数相同，任务终止（RepeatGuard）",
                metadata={"repeat_guard": "hard", "tool_name": tool_name, "repeats": repeats},
            )
        logger.warning(
            "RepeatGuard soft（通道A-同参数）tool=%s repeats=%d soft_hits=%d",
            tool_name, repeats, st["call_soft_hits"],
        )
        return ToolResult(
            ok=False,
            content="",
            error=_correction_text(tool_name, repeats, "call"),
            metadata={"repeat_guard": "soft", "tool_name": tool_name, "repeats": repeats},
        )

    # ---- 通道 B：同工具同结果（执行后瀑布流）----

    def _post_execute(self, result: ToolResult, tool_name: str) -> ToolResult:
        """waterfall：结果与上一次同工具完全相同 → 附加纠偏；hard → 标记终止。"""
        st = self._state()
        key = (tool_name, _content_hash(result.content))
        if st["last_result"] == key:
            st["result_repeats"] += 1
        else:
            st["last_result"] = key
            st["result_repeats"] = 1
            st["result_soft_hits"] = 0  # 结果变了 → 纠偏计数清零
        repeats = st["result_repeats"]
        if repeats < self.soft_limit:
            return result

        st["result_soft_hits"] += 1
        if st["result_soft_hits"] >= self.hard_limit:
            logger.warning(
                "RepeatGuard hard（通道B-同结果）tool=%s repeats=%d soft_hits=%d",
                tool_name, repeats, st["result_soft_hits"],
            )
            result.metadata["repeat_guard"] = "hard"
            result.metadata["repeat_tool"] = tool_name
            result.error = (result.error or "") + "；[RepeatGuard] 检测到重复死循环，任务终止"
            return result
        logger.warning(
            "RepeatGuard soft（通道B-同结果）tool=%s repeats=%d soft_hits=%d",
            tool_name, repeats, st["result_soft_hits"],
        )
        result.error = (result.error or "") + "；" + _correction_text(tool_name, repeats, "result")
        return result
