from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SETTINGS_DIRNAME = ".ycode_tasks"
_SETTINGS_FILENAME = "settings.json"

_lock = threading.RLock()


def _settings_path(workdir: str | os.PathLike[str]) -> Path:
    base = Path(workdir)
    if not base.is_absolute():
        raise ValueError(f"工作区目录必须是绝对路径：{workdir!r}")
    return base / _SETTINGS_DIRNAME / _SETTINGS_FILENAME


def _read_settings_locked(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("工作区设置文件读取失败（按空处理）: %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def get_workspace_setting(
    workdir: str | os.PathLike[str], key: str, default: Any = None
) -> Any:
    """读取单个工作区设置；文件缺失/损坏/键不存在一律返回 default。"""
    key = str(key).strip()
    if not key:
        raise ValueError("workspace setting key 不能为空")
    path = _settings_path(workdir)
    if not path.is_file():
        return default
    try:
        with _lock:
            data = _read_settings_locked(path)
    except OSError:
        logger.warning("工作区设置不可读: %s", path, exc_info=True)
        return default
    return data.get(key, default)


def set_workspace_setting(workdir: str | os.PathLike[str], key: str, value: Any) -> bool:
    """写入单个工作区设置（合并而非整体覆盖）；返回是否落盘成功。

    workdir 必须是已存在的绝对路径项目目录；value 必须可 JSON 序列化。
    """
    key = str(key).strip()
    if not key:
        raise ValueError("workspace setting key 不能为空")
    path = _settings_path(workdir)
    if not path.parent.parent.is_dir():
        raise ValueError(f"工作区目录不存在：{path.parent.parent}")
    with _lock:
        data = _read_settings_locked(path)
        data[key] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            if path.is_file():
                shutil.copyfile(path, path.with_name(path.name + ".bak"))
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return True
