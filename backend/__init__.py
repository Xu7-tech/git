"""老年人语音数字银行管家 —— 后端包。

导入本包时先加载项目根目录的 .env，这样各子模块在模块级读取环境变量也能拿到值。
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path: Path | None = None) -> int:
    """加载 .env。已存在的环境变量优先，不覆盖；返回读入的条目数。

    十来行就够，不为此引入 python-dotenv 依赖。
    """
    env_path = path or Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return 0
    loaded = 0
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


load_env_file()
