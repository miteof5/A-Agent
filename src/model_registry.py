"""模型清单注册表（模型切换功能）：从 models.txt 读取可用模型清单。

文件格式约定（平台相关——换平台重写此文件即可）：
- `# 分组名` 开头的行是分组标题（如厂商名）
- 其余非空行是模型名，按行序排列
- 重复模型名去重（保留首个）

换平台流程（配合 config.py）：
1. 改环境变量 AA_LLM_BASE_URL / AA_LLM_API_KEY（连接信息）
2. 重写 models.txt 的模型名与分组
3. 重启服务

当前模型持久化：切换后写入 <db 目录>/.current_model，
重启时 load_config 优先读取它（显式环境变量 > 上次切换 > 清单第一个 > 默认）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CURRENT_MODEL_FILENAME = ".current_model"


@dataclass
class ModelGroup:
    """一个分组（厂商/用途）及其模型名列表。"""

    group: str
    models: list[str] = field(default_factory=list)


def load_models(path: str | Path | None = None) -> list[ModelGroup]:
    """读取模型清单文件 → 分组列表（保持文件内顺序，去重）。

    - `# xxx` 行 → 新分组
    - 非空行 → 当前分组追加模型名（文件无分组头时归入"未分组"）
    - 重复模型名跳过（保留首个）
    """
    p = Path(path) if path else PROJECT_ROOT / "models.txt"
    if not p.exists():
        return []
    groups: list[ModelGroup] = []
    seen: set[str] = set()
    current: ModelGroup | None = None
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            current = ModelGroup(group=line.lstrip("#").strip())
            groups.append(current)
            continue
        if line in seen:
            continue  # 去重（保留首个）
        seen.add(line)
        if current is None:
            current = ModelGroup(group="未分组")
            groups.append(current)
        current.models.append(line)
    return groups


def flatten(groups: list[ModelGroup]) -> list[str]:
    """分组列表 → 扁平模型名列表（保持顺序）。"""
    return [m for g in groups for m in g.models]


def first_model(groups: list[ModelGroup]) -> str | None:
    """清单第一个模型（未显式指定默认模型时用）。"""
    names = flatten(groups)
    return names[0] if names else None


# ---- 当前模型持久化（切换后重启不丢）----

def _current_file(base_dir: Path | None) -> Path:
    return (base_dir or PROJECT_ROOT) / CURRENT_MODEL_FILENAME


def load_current_model(base_dir: Path | None = None) -> str | None:
    """读上次切换的模型名；文件不存在或为空返回 None。"""
    f = _current_file(base_dir)
    try:
        text = f.read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def save_current_model(name: str, base_dir: Path | None = None) -> None:
    """持久化当前模型名（切换成功时调用）。"""
    try:
        _current_file(base_dir).write_text(name.strip(), encoding="utf-8")
    except OSError:
        pass  # 写失败不阻塞切换（仅丢失记忆）
