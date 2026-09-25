"""日志基建（S5.2）：标准库 logging + 文件轮转 + 控制台 + task_id 自动串联。

- logs/agent.log：RotatingFileHandler，单文件 10MB、保留 5 个备份（agent.log.1 …）
- 控制台同步输出（保留开发期看终端输出的习惯）
- task_id 自动提取：后台任务线程名固定为 task-{task_id[:8]}（runtime.py），
  TaskIdFilter 从线程名提取并附加到每条日志；API 线程无该前缀则记 '-'
- 分级：DEBUG（token 级，默认 INFO）/ INFO（业务事件）/ WARNING（重复检测、权限拒绝）/ ERROR（失败）
- 幂等：重复调用 setup_logging 不叠加 handler（uvicorn reload 等场景安全）

用法（各模块）：
    logger = logging.getLogger(__name__)
    logger.info("任务已创建 task_id=%s", task_id)
"""

from __future__ import annotations

import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = "agent.log"
MAX_BYTES = 10 * 1024 * 1024  # 10MB
BACKUP_COUNT = 5

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] task=%(task)s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class TaskIdFilter(logging.Filter):
    """从后台任务线程名提取 task-xxxx 附加到记录（无则 '-'）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        name = getattr(threading.current_thread(), "name", "") or ""
        record.task = name if name.startswith("task-") else "-"
        return True


class AccessNoiseFilter(logging.Filter):
    """过滤 uvicorn.access 高频噪音（S5.2 修复）：前端每 1.5s 轮询 /status + favicon 404。"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "/api/v1/task/status" in msg or "/favicon.ico" in msg:
            return False
        return True


def setup_logging(
    level: int = logging.INFO,
    log_dir: Path | str | None = None,
    log_file: str = LOG_FILE,
) -> None:
    """初始化根 logger：文件轮转 + 控制台 + task_id 过滤。幂等（重复调用直接返回）。"""
    root = logging.getLogger()
    if root.handlers:
        return
    log_dir = Path(log_dir) if log_dir else LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    root.setLevel(level)
    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    file_handler = RotatingFileHandler(
        log_dir / log_file, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    for h in (file_handler, stream):
        h.addFilter(TaskIdFilter())
        root.addHandler(h)

    # 第三方 SDK 噪音收敛（请求级 DEBUG/INFO 信息不刷屏）
    for noisy in ("httpx", "httpx2", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # uvicorn.access 高频轮询/噪音降噪（logger 级 Filter：先于 propagate 过滤）
    logging.getLogger("uvicorn.access").addFilter(AccessNoiseFilter())
