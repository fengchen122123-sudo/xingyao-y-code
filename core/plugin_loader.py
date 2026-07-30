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

# 最近一次 load_and_register 的扫描结果（含禁用/失败/被覆盖的插件），
# 供 /plugins 命令与诊断查询；每次扫描开头会整体重建。
_PLUGIN_INFOS: list[dict] = []


def get_plugin_infos() -> list[dict]:
    """返回最近一次插件扫描的信息列表（dict 副本，调用方修改不影响内部状态）。

    每条记录字段：name / version / description / path / source(user|builtin) /
    status(loaded|disabled|failed|overridden) / tools(list[str]) / error(可选)。
    """
    return [dict(info) for info in _PLUGIN_INFOS]


def _user_plugin_dir() -> Path:
    try:
        import config
        return Path(config.YCODE_DIR) / "plugins"
    except Exception:
        return Path.home() / ".ycode" / "plugins"


def toggle_plugin(name: str, enabled: bool) -> tuple[bool, str]:
    """切换插件开关：写回其 plugin.json 的 enabled 字段，并同步刷新内存状态。

    返回 (ok, msg)。注意：实际加载/卸载需重启后生效（插件工具在启动时一次性注册），
    本函数只负责持久化开关与刷新 /plugins 列表的即时显示状态。
    """
    target: Path | None = None
    target_source = ""
    for info in _PLUGIN_INFOS:
        if info.get("name") == name:
            target = Path(info["path"])
            target_source = info.get("source", "")
            break
    # 兜底：内存记录里没找到就扫一遍目录
    if target is None:
        for base in plugin_dirs():
            child = base / name
            if (child / "plugin.json").is_file():
                target = child
                target_source = "user" if base == _user_plugin_dir() else "builtin"
                break
    if target is None or not (target / "plugin.json").is_file():
        return False, f"未找到插件 {name}"
    meta_path = target / "plugin.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception as exc:
        return False, f"读取 {meta_path} 失败: {exc}"
    meta["enabled"] = bool(enabled)
    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception as exc:
        return False, f"写入 {meta_path} 失败: {exc}"
    # 同步刷新内存中该插件的 status，让 /plugins 列表即时反映
    for info in _PLUGIN_INFOS:
        if info.get("name") == name:
            if enabled:
                # 标记为“待重启加载”，避免误显示为已加载（工具实际重启后才注册）
                if info.get("status") in ("disabled", "pending_disable"):
                    info["status"] = "pending_enable"
                    info.pop("error", None)
            else:
                # 标记为“待重启卸载”：当前进程内工具仍可用，重启后才真正不加载
                info["status"] = "pending_disable"
            break
    action = "启用" if enabled else "禁用"
    return True, f"已{action}插件 {name}（重启后完全生效）"


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
    _PLUGIN_INFOS.clear()
    # plugin_dirs() 约定用户目录在前：索引 0 即用户级，其余为内置
    for idx, base in enumerate(plugin_dirs()):
        source = "user" if idx == 0 else "builtin"
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
                    meta = json.load(f)
                plugin_name = str(meta.get("name") or child.name)
            except Exception:
                meta = {}
                plugin_name = child.name
            info: dict = {
                "name": plugin_name,
                "version": str(meta.get("version") or "?"),
                "description": str(meta.get("description") or ""),
                "path": str(child),
                "source": source,
                "status": "loaded",
                "tools": [],
            }
            if plugin_name in seen:
                info["status"] = "overridden"
                _PLUGIN_INFOS.append(info)
                continue
            before_tools = {s.name for s in registry.all()}
            try:
                name = _load_one(child, registry)
                seen.add(plugin_name)
                if name:
                    info["tools"] = sorted(
                        s.name for s in registry.all() if s.name not in before_tools
                    )
                    loaded.append(name)
                    _logger.info("插件已加载: %s (%s)", name, child)
                else:
                    info["status"] = "disabled"
            except Exception as exc:
                seen.add(plugin_name)
                info["status"] = "failed"
                info["error"] = str(exc)
                _logger.warning("插件 %s 加载失败，已跳过: %s", child.name, exc)
            _PLUGIN_INFOS.append(info)
    return loaded
