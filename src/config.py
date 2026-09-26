"""配置加载（v0.3 配置管理的最小版：环境变量驱动 + 合理默认值）。

约定环境变量前缀 AA_（ActionAgent）：
    AA_LLM_API_KEY    API Key（可选；未设置时回退读系统环境变量 DASHSCOPE_API_KEY）
    AA_LLM_BASE_URL   OpenAI 兼容接口地址（默认阿里云百炼 DashScope 兼容模式）
    AA_LLM_MODEL      模型名（默认 qwen-max，按你的服务商修改）
    AA_MAX_STEPS      单任务最大 step 数（v0.3 硬上限，默认 50）
    AA_TOOL_MAX_BYTES 工具输出上限（v0.3：64KB 内存有界，默认 65536）
    AA_REPEAT_SOFT_LIMIT  RepeatGuard 温和纠偏阈值（S5.1：同工具同参数/同结果连续 N 次触发，默认 3）
    AA_REPEAT_HARD_LIMIT  RepeatGuard 强硬终止阈值（S5.1：纠偏后仍重复 N 次即终止，默认 3）
    AA_DB_PATH        SQLite 文件路径（默认项目根目录 actionagent.db）
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "deepseek-v4-flash-0731"


@dataclass
class Config:
    llm_api_key: str = ""
    llm_base_url: str = DEFAULT_BASE_URL
    llm_model: str = DEFAULT_MODEL
    max_steps_per_turn: int = 50
    max_llm_calls_per_session: int = 200
    tool_output_max_bytes: int = 65536
    repeat_soft_limit: int = 3  # S5.1：RepeatGuard 温和纠偏阈值
    repeat_hard_limit: int = 3  # S5.1：RepeatGuard 强硬终止阈值
    db_path: str = "actionagent.db"


def load_config() -> Config:
    api_key = os.getenv("AA_LLM_API_KEY", "") or os.getenv("DASHSCOPE_API_KEY", "")
    return Config(
        llm_api_key=api_key,
        llm_base_url=os.getenv("AA_LLM_BASE_URL", DEFAULT_BASE_URL),
        llm_model=os.getenv("AA_LLM_MODEL", DEFAULT_MODEL),
        max_steps_per_turn=int(os.getenv("AA_MAX_STEPS", "50")),
        max_llm_calls_per_session=int(os.getenv("AA_MAX_LLM_CALLS", "200")),
        tool_output_max_bytes=int(os.getenv("AA_TOOL_MAX_BYTES", "65536")),
        repeat_soft_limit=int(os.getenv("AA_REPEAT_SOFT_LIMIT", "3")),
        repeat_hard_limit=int(os.getenv("AA_REPEAT_HARD_LIMIT", "3")),
        db_path=os.getenv("AA_DB_PATH", "actionagent.db"),
    )
