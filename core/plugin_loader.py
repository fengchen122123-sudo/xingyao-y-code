"""通用插件加载框架。

扫描内置插件目录与用户插件目录，加载每个插件的 plugin.json + 入口模块，
调用其 register(registry) 完成工具注册。

设计原则：
- 对核心零侵入：build_default_registry() 末尾追加一次 load_and_register() 调用即可；
- 单插件隔离：任一插件加载失败只记录日志并跳过，绝不影响 ycode 启动；
- 用户级覆盖内置：同名插件以 ~/.ycode/plugins/ 下的为准；
- 插件工具自动获得 ToolSpec 元数据驱动的权限拦截、schema 参数过滤与结果截断，
  无需插件自行实现。

插件目录结构：
    <plugin_dir>/
        plugin.json   # {"name", "version", "description", "entry", "enabled"}
        <entry>.py    # 必须暴露 register(registry) 函数；可选 plugin.json 指定 entry

插件内兄弟模块请用 import_sibling(__file__, "module_name") 加载，
避免不同插件间的同名模块在 sys.modules 中互相覆盖。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

_logger = logging.getLogger("ycode.plugins")

# 内置插件目录（随 ycode 仓库发布）与用户插件目录（可覆盖内置同名插件）
_BUILTIN_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _user_plugin_dir() -> Path:
    try:
        import config
        return Path(config.YCODE_DIR) / "plugins"
    except Exception:
        return Path.home() / ".ycode" / "plugins"


def plugin_dirs() -> list[Path]:
    """返回插件扫描目录列表。用户目录在前，使其同名插件优先于内置。"""
    return [_user_plugin_dir(), _BUILTIN_PLUGIN_DIR]


def import_sibling(plugin_file: str, module_name: str) -> Any:
    """加载插件目录内的兄弟模块，模块名带插件目录前缀，避免跨插件冲突。

    用法（在插件的入口模块或兄弟模块内）：
        from core.plugin_loader import import_sibling
        scanner = import_sibling(__file__, "scanner")
    """
    plugin_dir = Path(plugin_file).resolve().parent
    unique = f"_ycode_plugin_{plugin_dir.name}_{module_name}"
    cached = sys.modules.get(unique)
    if cached is not None:
        return cached
    path = plugin_dir / f"{module_name}.py"
    if not path.is_file():
        raise ImportError(f"插件兄弟模块不存在: {path}")
    spec = importlib.util.spec_from_file_location(unique, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为插件模块创建加载规格: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique] = module
    spec.loader.exec_module(module)
    return module


def _load_one(plugin_dir: Path, registry) -> str:
    """加载单个插件并注册其工具。返回插件名。异常向上抛，由调用方隔离。"""
    meta_path = plugin_dir / "plugin.json"
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    name = str(meta.get("name") or plugin_dir.name)
    if meta.get("enabled", True) is False:
        return ""  # 空串表示"已禁用，跳过"，与加载失败区分
    entry = plugin_dir / str(meta.get("entry") or "plugin.py")
    if not entry.is_file():
        raise ImportError(f"插件入口不存在: {entry}")

    module_name = f"_ycode_plugin_{name}_entry"
    spec = importlib.util.spec_from_file_location(module_name, entry)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为插件创建加载规格: {entry}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    register = getattr(module, "register", None)
    if not callable(register):
        raise AttributeError(f"插件 {name} 的入口 {entry.name} 未暴露 register(registry) 函数")
    register(registry)
    return name


def load_and_register(registry) -> list[str]:
    """扫描所有插件目录并注册插件工具。返回成功加载的插件名列表。

    单个插件的任何异常都被捕获、记录并跳过，保证 ycode 主流程不受影响。
    """
    loaded: list[str] = []
    seen: set[str] = set()
    for base in plugin_dirs():
        try:
            if not base.is_dir():
                continue
            children = sorted(p for p in base.iterdir() if p.is_dir())
        except OSError as exc:
            _logger.warning("插件目录不可访问，已跳过 %s: %s", base, exc)
            continue
        for child in children:
            if not (child / "plugin.json").is_file():
                continue
            # 同名覆盖：用户目录先扫，内置目录遇到同名直接跳过
            try:
                with open(child / "plugin.json", "r", encoding="utf-8") as f:
                    plugin_name = str(json.load(f).get("name") or child.name)
            except Exception:
                plugin_name = child.name
            if plugin_name in seen:
                continue
            try:
                name = _load_one(child, registry)
                seen.add(plugin_name)
                if name:
                    loaded.append(name)
                    _logger.info("插件已加载: %s (%s)", name, child)
            except Exception as exc:
                seen.add(plugin_name)
                _logger.warning("插件 %s 加载失败，已跳过: %s", child.name, exc)
    return loaded
