"""启动入口。

用法（在 ActionAgent 项目根目录）：
    python -m src.main
然后访问 http://127.0.0.1:8000/docs 调试接口。

S5.2：启动时初始化日志基建（logs/agent.log 轮转 + 控制台），
uvicorn 的 access/error 日志接入同一 root logger（不再只打 stderr）。
"""

import logging

import uvicorn

from .api import create_app
from .logging_setup import setup_logging


def main() -> None:
    setup_logging()
    # uvicorn 日志统一走 root（清掉 uvicorn 自带 handler，靠 propagate 进 agent.log + 控制台）
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_config=None)


if __name__ == "__main__":
    main()
