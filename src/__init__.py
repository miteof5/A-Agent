"""ActionAgent S1 最小闭环。

对应《接口契约-S0.md》§7 落地点：
- 后端：POST /task/create（同步执行）+ GET /task/status + GET /task/logs
- 工具：file_view（唯一）
- 存储：SQLite（tasks + steps 两张表）
- 模型：OpenAI 兼容接口，同步非流式
"""

__version__ = "0.1.0"
