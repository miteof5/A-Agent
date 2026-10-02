"""配置加载（v0.3 配置管理的最小版：环境变量驱动 + 合理默认值）。

约定环境变量前缀 AA_（ActionAgent）：
    AA_LLM_API_KEY    API Key（可选；未设置时回退读系统环境变量 DASHSCOPE_API_KEY）
    AA_LLM_BASE_URL   OpenAI 兼容接口地址（默认阿里云百炼 DashScope 兼容模式）
    AA_LLM_MODEL      模型名（显式设置时优先；未设置时按下方优先级取）
    AA_MAX_STEPS      单任务最大 step 数（v0.3 硬上限，默认 50）
    AA_TOOL_MAX_BYTES 工具输出上限（v0.3：64KB 内存有界，默认 65536）
    AA_REPEAT_SOFT_LIMIT  RepeatGuard 温和纠偏阈值（S5.1：同工具同参数/同结果连续 N 次触发，默认 3）
    AA_REPEAT_HARD_LIMIT  RepeatGuard 强硬终止阈值（S5.1：纠偏后仍重复 N 次即终止，默认 3）
    AA_MODELS_FILE    模型清单文件（默认 models.txt；换平台重写此文件即可）
    AA_DB_PATH        SQLite 文件路径（默认项目根目录 actionagent.db）

S5.4 联网能力（敏感信息一律走 .env，禁止写进代码/git）：
    AA_SEARCH_API_KEY   搜索 API Key（Tavily；.env 里配置，不提交）
    AA_SEARCH_BASE_URL  搜索 API 地址（默认 https://api.tavily.com）
    AA_SUMM_API_KEY     压缩小模型 Key（可选；未设置回退 AA_LLM_API_KEY）
    AA_SUMM_BASE_URL    压缩小模型接口（可选；未设置回退 AA_LLM_BASE_URL）
    AA_SUMM_MODEL       压缩小模型名（可选；未设置回退主模型——换小模型只改这一个变量）

模型生效优先级（模型切换功能）：
    显式 AA_LLM_MODEL > 上次切换持久化（.current_model，随 db 目录）> DEFAULT_MODEL（下方代码常量，可直接改）> models.txt 清单第一个
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .model_registry import first_model, load_current_model, load_models


def _load_dotenv() -> None:
    """启动时读取项目根目录 .env（若存在），注入 os.environ（不覆盖已有变量）。

    key 不进代码、不进 git（.gitignore 已排除 .env）；缺失时静默跳过。
    """
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_dotenv()

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_SEARCH_BASE_URL = "https://api.tavily.com"
# 默认模型（用户可直接改这里，或改用环境变量 AA_LLM_MODEL 覆盖）
DEFAULT_MODEL = "deepseek-v4-flash-0731"
DEFAULT_MODELS_FILE = "models.txt"

# S5 长期记忆：主题语义配置块（方案 A：机制通用、主题可插拔）。
# 换主题/换场景时只改这里（或环境变量 AA_LONG_MEMORY_SCHEMA=JSON），机制层代码零改动：
#   name            注入块标题用词（渲染为【name】）
#   header_hint     注入块引导语（渲染为（header_hint）：）
#   categories      分类字典：key → 中文语义说明（写进提炼 prompt，模型按此分类）
#   max_items       单次提炼最多输出候选条数
#   extract_extra_rules  主题方追加的提炼判断标准（可选，追加到 prompt）
DEFAULT_LONG_MEMORY_SCHEMA = {
    "name": "长期记忆",
    "header_hint": "与用户跨会话积累的稳定事实，直接信任并用于本次任务",
    "categories": {
        "user_profile": "用户画像：身份/偏好/沟通习惯",
        "project_fact": "项目与环境事实：路径/命令/踩坑/决策理由",
        "user_goal": "用户目标与关注点",
    },
    "max_items": 3,
    "extract_extra_rules": "",
}


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
    models_file: str = DEFAULT_MODELS_FILE  # 模型切换：可用模型清单文件
    memory_full_turns: int = 5  # 滚动记忆：完整上下文保留的最近轮数
    memory_summary_chunk: int = 10  # 滚动记忆：逐轮 Q+A 攒满多少条触发一次 LLM 总结
    memory_summary_chars: int = 400  # 滚动记忆：每条 LLM 摘要字数上限
    long_memory_inject_limit: int = 5  # S5 长期记忆：每次注入的活跃记忆条数上限
    long_memory_max_chars: int = 100  # S5 长期记忆：每条注入内容的字数上限
    long_memory_schema: dict = field(  # S5 长期记忆：主题语义配置（默认通用三类，可整体覆写）
        default_factory=lambda: dict(DEFAULT_LONG_MEMORY_SCHEMA)
    )
    db_path: str = "actionagent.db"
    # S5.4 联网：搜索 API（key 一律走 .env / 环境变量，不进代码）
    search_api_key: str = ""
    search_base_url: str = DEFAULT_SEARCH_BASE_URL
    # S5.4 压缩小模型：未显式设置时回退主 LLM 配置（换小模型只改 AA_SUMM_MODEL）
    summ_api_key: str = ""
    summ_base_url: str = ""
    summ_model: str = ""


def load_config() -> Config:
    api_key = os.getenv("AA_LLM_API_KEY", "") or os.getenv("DASHSCOPE_API_KEY", "")
    db_path = os.getenv("AA_DB_PATH", "actionagent.db")
    models_file = os.getenv("AA_MODELS_FILE", DEFAULT_MODELS_FILE)

    # 模型生效优先级：显式环境变量 > 上次切换持久化 > 代码默认值 > 清单第一个
    model = os.getenv("AA_LLM_MODEL", "").strip()
    if not model:
        model = load_current_model(base_dir=Path(db_path).parent) or ""
    if not model:
        model = DEFAULT_MODEL
    if not model:
        model = first_model(load_models(models_file)) or ""

    llm_base_url = os.getenv("AA_LLM_BASE_URL", DEFAULT_BASE_URL)
    # 压缩小模型：显式设置优先，否则回退主模型三件套（key/base_url/model）
    summ_model = os.getenv("AA_SUMM_MODEL", "").strip() or model
    summ_base_url = os.getenv("AA_SUMM_BASE_URL", "").strip() or llm_base_url
    summ_api_key = os.getenv("AA_SUMM_API_KEY", "").strip() or api_key

    return Config(
        llm_api_key=api_key,
        llm_base_url=llm_base_url,
        llm_model=model,
        max_steps_per_turn=int(os.getenv("AA_MAX_STEPS", "50")),
        max_llm_calls_per_session=int(os.getenv("AA_MAX_LLM_CALLS", "200")),
        tool_output_max_bytes=int(os.getenv("AA_TOOL_MAX_BYTES", "65536")),
        repeat_soft_limit=int(os.getenv("AA_REPEAT_SOFT_LIMIT", "3")),
        repeat_hard_limit=int(os.getenv("AA_REPEAT_HARD_LIMIT", "3")),
        models_file=models_file,
        memory_full_turns=int(os.getenv("AA_MEMORY_FULL_TURNS", "5")),
        memory_summary_chunk=int(os.getenv("AA_MEMORY_SUMMARY_CHUNK", "10")),
        memory_summary_chars=int(os.getenv("AA_MEMORY_SUMMARY_CHARS", "400")),
        long_memory_inject_limit=int(os.getenv("AA_LONG_MEMORY_INJECT_LIMIT", "5")),
        long_memory_max_chars=int(os.getenv("AA_LONG_MEMORY_MAX_CHARS", "100")),
        long_memory_schema=_load_long_memory_schema(),
        db_path=db_path,
        search_api_key=os.getenv("AA_SEARCH_API_KEY", "").strip(),
        search_base_url=os.getenv("AA_SEARCH_BASE_URL", DEFAULT_SEARCH_BASE_URL).strip(),
        summ_api_key=summ_api_key,
        summ_base_url=summ_base_url,
        summ_model=summ_model,
    )


def _load_long_memory_schema() -> dict:
    """读 AA_LONG_MEMORY_SCHEMA（JSON）覆写主题语义；缺失/非法 → 默认 schema。

    JSON 为整体覆写；未给出的键保留默认值（浅合并），便于只改分类或只改标题。
    """
    raw = os.getenv("AA_LONG_MEMORY_SCHEMA", "").strip()
    if not raw:
        return dict(DEFAULT_LONG_MEMORY_SCHEMA)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return dict(DEFAULT_LONG_MEMORY_SCHEMA)
        merged = dict(DEFAULT_LONG_MEMORY_SCHEMA)
        for k, v in data.items():
            if k in merged and isinstance(v, dict) and isinstance(merged[k], dict):
                merged[k] = {**merged[k], **v}  # categories 等嵌套字典浅合并
            else:
                merged[k] = v
        return merged
    except Exception:
        return dict(DEFAULT_LONG_MEMORY_SCHEMA)
