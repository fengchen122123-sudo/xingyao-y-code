from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

_logger = logging.getLogger("ycode.plugins")

def _builtin_plugin_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "plugins"
    return Path(__file__).resolve().parent.parent / "plugins"

_PLUGIN_INFOS: list[dict] = []

_REFRESH_LOCK = threading.RLock()

# 已加载插件的模块引用：留着是为了插件被禁用时能调用它可选的 cleanup() 钩子。
# key 为插件名；禁用或加载失败时移除。
_LOADED_MODULES: dict[str, Any] = {}


def get_plugin_infos() -> list[dict]:
    result: list[dict] = []
    for info in _PLUGIN_INFOS:
        item = dict(info)
        module = _LOADED_MODULES.get(str(item.get("name") or ""))
        item["has_settings"] = callable(getattr(module, "plugin_settings", None))
        result.append(item)
    return result


def call_plugin_hook(plugin_name: str, hook_name: str, payload: dict | None = None) -> dict:
    name = str(plugin_name or "").strip()
    if not name:
        return {"ok": False, "message": "插件名不能为空", "needs": None}
    module = _LOADED_MODULES.get(name)
    if module is None:
        status = ""
        for info in _PLUGIN_INFOS:
            if str(info.get("name") or "") == name:
                status = str(info.get("status") or "")
                break
        needs = "enable"
        if status == "disabled":
            message = f"插件 {name} 未启用，启用后即可配置"
        else:
            message = f"插件 {name} 尚未加载（{status or '未安装'}）"
        return {"ok": False, "message": message, "needs": needs}
    hook = getattr(module, hook_name, None)
    if not callable(hook):
        return {"ok": False, "message": f"插件 {name} 未提供 {hook_name}", "needs": None}
    try:
        result = hook(payload if payload is not None else {})
    except Exception as exc:
        _logger.exception("插件 %s 的钩子 %s 调用失败", name, hook_name)
        return {"ok": False, "message": f"插件 {name} 的 {hook_name} 执行失败: {exc}", "needs": None}
    if not isinstance(result, dict):
        return {"ok": False, "message": f"插件 {name} 的 {hook_name} 返回值必须是 dict", "needs": None}
    return {"ok": True, "data": result}


def _is_builtin_plugin(plugin_dir: Path) -> bool:
    try:
        resolved = plugin_dir.resolve()
    except OSError:
        resolved = plugin_dir
    for base in plugin_dirs()[1:]:
        try:
            if resolved.is_relative_to(base.resolve()):
                return True
        except OSError:
            continue
    return False


def set_trust_prompt(callback) -> None:
    """兼容桩：信任校验已移除，入口层可继续调用但不会产生任何确认流程。"""
    return None


def _user_plugin_dir() -> Path:
    try:
        import config
        return Path(config.YCODE_DIR) / "plugins"
    except Exception:
        return Path.home() / ".ycode" / "plugins"


def _live_registry():
    try:
        from core import llm
        return getattr(llm, "tool_registry", None)
    except Exception:
        return None


def _run_cleanup_hook(name: str) -> str:
    """调用插件可选的 cleanup() 钩子，回收插件写入的持久化数据。

    返回空串表示无事发生；失败时返回可拼进提示语的说明，由调用方追加到 toggle
    的结果消息里——绝不让"禁用插件"这个动作本身失败。
    """
    module = _LOADED_MODULES.pop(name, None)
    if module is None:
        return ""
    hook = getattr(module, "cleanup", None)
    if not callable(hook):
        return ""
    try:
        hook()
        return ""
    except SystemExit as exc:
        # 同 load_and_register：SystemExit 只记清理失败，兑现"绝不让禁用动作失败"
        #（KeyboardInterrupt 仍向上传播）。
        _logger.warning("插件 %s 的 cleanup 钩子触发 SystemExit(%r)，已忽略", name, exc.code)
        return f"（清理失败: SystemExit({exc.code!r})）"
    except Exception as exc:
        _logger.warning("插件 %s 的 cleanup 钩子执行失败: %s", name, exc)
        return "（清理失败: %s）" % exc


def _core_managed_markers() -> frozenset[str]:
    try:
        from core.tokenrhythm_auth import MANAGED_PROVIDER_VALUE

        return frozenset({str(MANAGED_PROVIDER_VALUE)})
    except Exception:  # noqa: BLE001 - 取不到就退回字面量：宁可漏清理也绝不误删
        return frozenset({"tokenrhythm"})


def _is_orphan_provider(
    provider: dict, loaded_names: set[str], core_markers: frozenset[str]
) -> bool:
    marker = provider.get("managed_by")
    if not marker:
        return False
    text = str(marker)
    if text in core_markers:
        return False
    return text not in loaded_names


def _prune_orphan_providers(loaded_names: set[str]) -> list[str]:
    try:
        import config
        from core.model_manager import load_config
    except Exception as exc:
        _logger.warning("孤儿 provider 清理无法导入依赖，已跳过: %s", exc)
        return []

    try:
        cfg = load_config()
    except Exception as exc:
        _logger.warning("孤儿 provider 清理读取配置失败，已跳过: %s", exc)
        return []

    core_markers = _core_managed_markers()
    orphans = [
        p for p in (cfg.get("providers") or [])
        if isinstance(p, dict) and _is_orphan_provider(p, loaded_names, core_markers)
    ]
    if not orphans:
        return []
    orphan_names = [str(p.get("name") or p.get("id") or "?") for p in orphans]

    def _mutate(data: dict) -> None:
        removed_model_ids = set()
        kept = []
        for p in data.get("providers") or []:
            if isinstance(p, dict) and _is_orphan_provider(p, loaded_names, core_markers):
                for m in (p.get("models") or []):
                    if isinstance(m, dict) and m.get("id"):
                        removed_model_ids.add(str(m["id"]))
                continue
            kept.append(p)
        data["providers"] = kept
        # 激活模型若属于被删 provider，一并清空，避免留下悬空 id
        for key in ("active_model_id", "secondary_model_id"):
            if str(data.get(key) or "") in removed_model_ids:
                data[key] = None

    try:
        config.update_config(_mutate)
    except Exception as exc:
        _logger.warning("孤儿 provider 清理写入配置失败，已跳过: %s", exc)
        return []

    _logger.info("已清理孤儿 provider: %s", ", ".join(orphan_names))
    return orphan_names


def _removed_file() -> Path:
    try:
        import config
        return Path(config.YCODE_DIR) / "plugin_removed.json"
    except Exception:
        return Path.home() / ".ycode" / "plugin_removed.json"


def _load_removed() -> set[str]:
    try:
        with open(_removed_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        names = data.get("builtin") if isinstance(data, dict) else None
        if isinstance(names, list):
            return {str(n).strip() for n in names if str(n).strip()}
    except Exception:
        pass
    return set()


def _save_removed(names: set[str]) -> None:
    path = _removed_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"builtin": sorted(names)}, f, ensure_ascii=False, indent=2)
        f.write("\n")


def is_builtin_removed(name: str) -> bool:
    """内置插件是否已被用户移除（列表隐藏 + 加载时跳过）。"""
    return str(name or "").strip() in _load_removed()


def delete_plugin(name: str) -> tuple[bool, str]:
    if not isinstance(name, str) or not name.strip():
        return False, "插件名无效"
    name = name.strip()
    if name in (".", "..") or any(sep in name for sep in ("/", "\\", "\x00")):
        return False, "插件名无效"

    with _REFRESH_LOCK:
        target = _locate_plugin_dir(name)
        if target is None or not (target / "plugin.json").is_file():
            return False, f"未找到插件 {name}"
        if _is_builtin_plugin(target):
            return False, "内置插件随应用发布，不能删除"
        try:
            resolved = target.resolve()
            user_root = plugin_dirs()[0].resolve()
        except OSError as exc:
            return False, f"无法定位插件目录: {exc}"
        if resolved == user_root or not resolved.is_relative_to(user_root):
            return False, "插件目录不在用户插件目录内，已拒绝删除"
        try:
            shutil.rmtree(resolved)
        except OSError as exc:
            return False, f"删除插件目录失败: {exc}"

        tools: list = []
        for info in _PLUGIN_INFOS:
            if info.get("name") == name and info.get("status") != "overridden":
                tools = list(info.get("tools") or [])
                break
        _unload_removed_plugin(name, tools, _live_registry())
        _PLUGIN_INFOS[:] = [
            info for info in _PLUGIN_INFOS
            if str(info.get("name") or "") != name or info.get("status") == "overridden"
        ]
        return True, f"已删除插件 {name}"


def _locate_plugin_dir(name: str) -> Path | None:
    candidates: list[Path] = []
    for info in _PLUGIN_INFOS:
        if info.get("name") == name and info.get("path"):
            candidates.append(Path(info["path"]))
    for base in plugin_dirs():
        child = base / name
        if (child / "plugin.json").is_file():
            candidates.append(child)
    for path in candidates:
        if not _is_builtin_plugin(path):
            return path
    return candidates[0] if candidates else None


def remove_plugin(name: str) -> tuple[bool, str]:
    if not isinstance(name, str) or not name.strip():
        return False, "插件名无效"
    name = name.strip()

    with _REFRESH_LOCK:
        target = _locate_plugin_dir(name)
        if target is None or not (target / "plugin.json").is_file():
            return False, f"未找到插件 {name}"
        if not _is_builtin_plugin(target):
            return delete_plugin(name)

        removed = _load_removed()
        removed.add(name)
        try:
            _save_removed(removed)
        except Exception as exc:
            return False, f"写入移除记录失败: {exc}"

        tools: list = []
        for info in _PLUGIN_INFOS:
            if info.get("name") == name and info.get("status") != "overridden":
                tools = list(info.get("tools") or [])
                info["status"] = "removed"
                info["tools"] = []
                info.pop("error", None)
                break
        _unload_removed_plugin(name, tools, _live_registry())
        return True, f"已移除内置插件 {name}（随包文件未删除，可随时恢复）"


def restore_plugin(name: str) -> tuple[bool, str]:
    """恢复被移除的内置插件：删移除记录并立即热加载，无需重启。"""
    if not isinstance(name, str) or not name.strip():
        return False, "插件名无效"
    name = name.strip()

    with _REFRESH_LOCK:
        removed = _load_removed()
        if name not in removed:
            return False, f"插件 {name} 不在已移除名单里"
        removed.discard(name)
        try:
            _save_removed(removed)
        except Exception as exc:
            return False, f"写入移除记录失败: {exc}"

        target: Path | None = None
        for base in plugin_dirs():
            child = base / name
            if (child / "plugin.json").is_file() and _is_builtin_plugin(child):
                target = child
                break
        if target is None:
            return False, f"未找到内置插件 {name} 的目录"
        registry = _live_registry()
        info = next(
            (
                i for i in _PLUGIN_INFOS
                if i.get("name") == name and i.get("status") == "removed"
            ),
            None,
        )
        if registry is None:
            if info is not None:
                info["status"] = "disabled"
            return True, f"已恢复内置插件 {name}（运行时注册表不可用，重启后加载）"

        before = {s.name for s in registry.all()}
        try:
            loaded_name = _load_one(target, registry)
        except SystemExit as exc:
            if info is not None:
                info["status"] = "failed"
                info["error"] = f"SystemExit({exc.code!r})"
            return True, f"已恢复内置插件 {name}，但热加载失败: SystemExit({exc.code!r})"
        except Exception as exc:
            if info is not None:
                info["status"] = "failed"
                info["error"] = str(exc)
            return True, f"已恢复内置插件 {name}，但热加载失败: {exc}"
        if not loaded_name:
            if info is not None:
                info["status"] = "disabled"
            return True, f"已恢复内置插件 {name}（当前为禁用状态）"
        if info is not None:
            info["status"] = "loaded"
            info["tools"] = sorted(s.name for s in registry.all() if s.name not in before)
        return True, f"已恢复内置插件 {name}"


def toggle_plugin(name: str, enabled: bool) -> tuple[bool, str]:
    target: Path | None = None
    for info in _PLUGIN_INFOS:
        if info.get("name") == name:
            target = Path(info["path"])
            break
    # 兜底：内存记录里没找到就扫一遍目录
    if target is None:
        for base in plugin_dirs():
            child = base / name
            if (child / "plugin.json").is_file():
                target = child
                break
    if target is None or not (target / "plugin.json").is_file():
        return False, f"未找到插件 {name}"
    if enabled and _is_builtin_plugin(target) and is_builtin_removed(name):
        return False, f"插件 {name} 已被移除，请先恢复再启用"
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

    hot_note = ""
    registry = _live_registry()
    if registry is not None:
        try:
            if enabled:
                # 先卸下同名旧工具，避免重复开关时 duplicate 报错
                for info in _PLUGIN_INFOS:
                    if info.get("name") == name:
                        for t in info.get("tools") or []:
                            try:
                                registry.unregister(t)
                            except KeyError:
                                pass
                        break
                before = {s.name for s in registry.all()}
                _load_one(target, registry)
                new_tools = sorted(s.name for s in registry.all() if s.name not in before)
                for info in _PLUGIN_INFOS:
                    if info.get("name") == name:
                        info["status"] = "loaded"
                        info["tools"] = new_tools
                        info.pop("error", None)
                        break
            else:
                for info in _PLUGIN_INFOS:
                    if info.get("name") == name:
                        for t in info.get("tools") or []:
                            try:
                                registry.unregister(t)
                            except KeyError:
                                pass
                        info["status"] = "disabled"
                        break
                hot_note = _run_cleanup_hook(name)
        except SystemExit as exc:
            _logger.warning("插件 %s 热切换触发 SystemExit(%r)，已按失败处理", name, exc.code)
            hot_note = f"（热切换失败: SystemExit({exc.code!r})，重启后生效）"
        except Exception as exc:
            _logger.warning("插件 %s 热切换失败: %s", name, exc)
            hot_note = f"（热切换失败: {exc}，重启后生效）"
    else:
        hot_note = "（运行时注册表不可用，重启后生效）"

    action = "启用" if enabled else "禁用"
    return True, f"已{action}插件 {name}{hot_note}"


def trust_plugin(name: str) -> tuple[bool, str]:
    """兼容桩：信任校验已移除。保留入口是为了让 GUI 插件页 / CLI / RPC 调用不报错。"""
    if not isinstance(name, str) or not name.strip():
        return False, "插件名无效"
    return True, "插件信任校验已关闭，无需确认"


def plugin_dirs() -> list[Path]:
    """返回插件扫描目录列表。用户目录在前，使其同名插件优先于内置。"""
    return [_user_plugin_dir(), _builtin_plugin_dir()]


def import_sibling(plugin_file: str, module_name: str) -> Any:
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
    meta_path = plugin_dir / "plugin.json"
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    name = str(meta.get("name") or plugin_dir.name)
    if meta.get("enabled", True) is False:
        return ""
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
    # 注册成功后才记录模块引用，避免对半初始化的模块被调用 cleanup
    _LOADED_MODULES[name] = module
    return name


def _unload_removed_plugin(name: str, tools: list, registry) -> None:
    if not name:
        return
    module = _LOADED_MODULES.pop(name, None)
    if module is None:
        return
    cleanup = getattr(module, "cleanup", None)
    if callable(cleanup):
        try:
            cleanup()
        except Exception:
            _logger.exception("插件 %s 被删除后 cleanup 执行失败，已跳过", name)
    for tool_name in tools or []:
        try:
            registry.unregister(str(tool_name))
        except Exception as exc:
            _logger.warning("插件 %s 被删除后注销工具 %s 失败: %s", name, tool_name, exc)


def refresh_plugins(registry=None) -> None:
    with _REFRESH_LOCK:
        _refresh_plugins_locked(registry)


def _refresh_plugins_locked(registry=None) -> None:
    """refresh_plugins 的锁内实现；调用方必须已持有 _REFRESH_LOCK。"""
    if registry is None:
        registry = _live_registry()

    for info in list(_PLUGIN_INFOS):
        path = info.get("path")
        if path and not (Path(path) / "plugin.json").is_file():
            _PLUGIN_INFOS.remove(info)
            _unload_removed_plugin(
                str(info.get("name") or ""),
                list(info.get("tools") or []),
                registry,
            )

    known_paths = {str(info.get("path") or "") for info in _PLUGIN_INFOS}
    taken_names = {
        str(info.get("name") or "")
        for info in _PLUGIN_INFOS
        if info.get("status") != "overridden"
    }
    removed_names = _load_removed()

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
            if not (child / "plugin.json").is_file() or str(child) in known_paths:
                continue
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
            if plugin_name in taken_names:
                info["status"] = "overridden"
                _PLUGIN_INFOS.append(info)
                continue
            if source == "builtin" and plugin_name in removed_names:
                info["status"] = "removed"
                _PLUGIN_INFOS.append(info)
                continue
            if registry is None:
                # 无运行时注册表（极端降级）：只记录元信息，不执行加载
                info["status"] = "disabled" if meta.get("enabled", True) is False else "loaded"
                taken_names.add(plugin_name)
                _PLUGIN_INFOS.append(info)
                continue
            before_tools = {s.name for s in registry.all()}
            try:
                name = _load_one(child, registry)
                taken_names.add(plugin_name)
                if name:
                    info["tools"] = sorted(
                        s.name for s in registry.all() if s.name not in before_tools
                    )
                    _logger.info("插件已热加载: %s (%s)", name, child)
                else:
                    info["status"] = "disabled"
            except SystemExit as exc:
                taken_names.add(plugin_name)
                info["status"] = "failed"
                info["error"] = f"SystemExit({exc.code!r})"
                _logger.warning("插件 %s 触发 SystemExit(%r)，已按热加载失败跳过", child.name, exc.code)
            except Exception as exc:
                taken_names.add(plugin_name)
                info["status"] = "failed"
                info["error"] = str(exc)
                _logger.warning("插件 %s 热加载失败，已跳过: %s", child.name, exc)
            _PLUGIN_INFOS.append(info)


def load_and_register(registry) -> list[str]:
    previous_tools = {
        str(info.get("name") or ""): list(info.get("tools") or [])
        for info in _PLUGIN_INFOS
    }
    loaded: list[str] = []
    seen: set[str] = set()
    _PLUGIN_INFOS.clear()
    removed_names = _load_removed()
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
            if source == "builtin" and plugin_name in removed_names:
                info["status"] = "removed"
                _PLUGIN_INFOS.append(info)
                continue
            # 已加载跳过：同一进程内重复调用 load_and_register（多入口/测试场景）时，
            # 插件模块与工具仍在运行注册表中；若再次 exec + register 会触发
            # "duplicate tool" 并把插件误标为加载失败，且 loaded 置空会让孤儿
            # provider 兜底清理误伤。跳过条件须同时满足"上一轮记录过工具"且
            # "这些工具确实仍在当前注册表"；否则（新注册表 / 工具已注销 / 上次
            # 并未加载成功）落回正常加载路径。
            if plugin_name in _LOADED_MODULES:
                previous = list(previous_tools.get(plugin_name) or [])
                current_names = {s.name for s in registry.all()}
                if previous and all(tool in current_names for tool in previous):
                    info["tools"] = previous
                    seen.add(plugin_name)
                    loaded.append(plugin_name)
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
            except SystemExit as exc:
                seen.add(plugin_name)
                info["status"] = "failed"
                info["error"] = f"SystemExit({exc.code!r})"
                _logger.warning("插件 %s 触发 SystemExit(%r)，已按加载失败跳过", child.name, exc.code)
            except Exception as exc:
                seen.add(plugin_name)
                info["status"] = "failed"
                info["error"] = str(exc)
                _logger.warning("插件 %s 加载失败，已跳过: %s", child.name, exc)
            _PLUGIN_INFOS.append(info)

    # 兜底清理孤儿 provider：只处理带 managed_by 标记、且其插件本次未加载的项。
    # 禁用/失败/未加载的插件都不算已加载。整段隔离：清理异常绝不影响星瑶启动。
    try:
        _prune_orphan_providers(set(loaded))
    except Exception as exc:
        _logger.warning("孤儿 provider 兜底清理异常（已忽略）: %s", exc)
    return loaded
