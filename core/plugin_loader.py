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

清理钩子（插件回收自己写入的持久化数据）：
- 插件可选暴露无参函数 cleanup()，核心在禁用该插件时调用它；
- 调用包在 try/except 内，失败只在 toggle 返回的提示里追加说明，绝不让禁用动作失败；
- 启动扫描完成后由 _prune_orphan_providers() 兜底清理"其插件本次未加载"的孤儿
  provider（只处理带 managed_by 标记的 provider，用户手配的配置绝不触碰）。

信任机制（首次加载确认）：
- 插件以进程全权限执行，加载前按目录内容指纹（文件哈希）判定是否已获信任；
- 内置 plugins/ 目录随仓库发布，视为用户自己的代码：记录指纹并自动信任；
- 用户级 ~/.ycode/plugins/ 首次见到或指纹变化时必须确认：交互终端弹出
  [y/N] 确认（入口层可用 set_trust_prompt 注册自定义确认回调）；
  非交互/headless 环境默认跳过加载并 logger.warning 提示；
- 已确认指纹持久化到 ~/.ycode/plugin_trust.json（YCODE_DATA_DIR 优先）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

_logger = logging.getLogger("ycode.plugins")

# 内置插件目录（随 ycode 仓库发布）与用户插件目录（可覆盖内置同名插件）
def _builtin_plugin_dir() -> Path:
    """内置插件源目录：打包后位于 _MEIPASS/plugins，开发态为仓库根 plugins/。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "plugins"
    return Path(__file__).resolve().parent.parent / "plugins"

# 最近一次 load_and_register 的扫描结果（含禁用/失败/被覆盖的插件），
# 供 /plugins 命令与诊断查询；每次扫描开头会整体重建。
_PLUGIN_INFOS: list[dict] = []

# 已加载插件的模块引用：留着是为了插件被禁用时能调用它可选的 cleanup() 钩子。
# key 为插件名；禁用或加载失败时移除。
_LOADED_MODULES: dict[str, Any] = {}


def get_plugin_infos() -> list[dict]:
    """返回最近一次插件扫描的信息列表（dict 副本，调用方修改不影响内部状态）。

    每条记录字段：name / version / description / path / source(user|builtin) /
    status(loaded|disabled|failed|overridden|untrusted) / tools(list[str]) /
    has_settings(bool，是否提供设置页) / error(可选)。
    """
    result: list[dict] = []
    for info in _PLUGIN_INFOS:
        item = dict(info)
        # 设置页由插件入口模块的可选 plugin_settings 钩子声明；未加载（未信任/未启用）
        # 的插件代码不执行，一律视为没有设置页。
        module = _LOADED_MODULES.get(str(item.get("name") or ""))
        item["has_settings"] = callable(getattr(module, "plugin_settings", None))
        result.append(item)
    return result


def call_plugin_hook(plugin_name: str, hook_name: str, payload: dict | None = None) -> dict:
    """调用已加载插件的可选钩子（如 ``plugin_settings``）。

    安全边界：只对"已加载（=已信任 + 已启用）"的插件调用；未加载时返回
    ``needs`` 指引（"trust" / "enable"），绝不执行未加载插件的代码。

    返回约定：
    - 成功：``{"ok": True, "data": <钩子原样返回的 dict>}``
    - 失败：``{"ok": False, "message": str, "needs": "trust"|"enable"|None}``
    """
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
        needs = "trust" if status == "untrusted" else "enable"
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


# ─── 插件信任机制：首次加载/指纹变化需确认 ─────────────────────────

# 已确认指纹持久化文件：{str(plugin_dir.resolve()): fingerprint}
def _trust_file() -> Path:
    try:
        import config
        return Path(config.YCODE_DIR) / "plugin_trust.json"
    except Exception:
        return Path.home() / ".ycode" / "plugin_trust.json"


def _load_trust_store() -> dict:
    try:
        with open(_trust_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _save_trust_store(store: dict) -> None:
    try:
        path = _trust_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception as exc:
        _logger.warning("插件信任记录写入失败: %s", exc)


def _plugin_fingerprint(plugin_dir: Path) -> str:
    """计算插件目录内容指纹：所有文件（跳过 __pycache__/.pyc）的路径+内容哈希。

    plugin.json 例外：解析后剔除 "enabled" 字段、按键名排序再序列化后哈希。
    原因：enabled 开关翻转由 toggle_plugin 回写 plugin.json，属于用户正常操作
    而非内容篡改；指纹必须对它不敏感，否则"禁用状态下信任的插件，一开开关
    就被误判为已变更"。解析失败时回退为原始字节哈希。
    """
    digest = hashlib.sha256()
    for path in sorted(plugin_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(plugin_dir).as_posix()
        if "__pycache__" in rel.split("/") or rel.endswith(".pyc"):
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        if rel == "plugin.json":
            try:
                meta = json.loads(path.read_bytes().decode("utf-8"))
                if isinstance(meta, dict):
                    meta.pop("enabled", None)
                canon = json.dumps(meta, ensure_ascii=False, sort_keys=True)
                digest.update(canon.encode("utf-8"))
                continue
            except Exception:
                pass  # 非 JSON 内容回退为原始字节哈希
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _is_builtin_plugin(plugin_dir: Path) -> bool:
    """判断是否内置插件目录（plugin_dirs() 中除首个用户目录外的任何目录之下）。"""
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


# 插件信任确认回调：callback(path, name, fingerprint) -> bool；
# None 时用默认行为（交互终端 input 确认，headless 拒绝）。
_trust_prompt = None


def set_trust_prompt(callback) -> None:
    """注册插件信任确认回调（供入口层接入 CLI/GUI 弹窗）；传 None 恢复默认。"""
    global _trust_prompt
    _trust_prompt = callback


def _confirm_plugin_trust(plugin_dir: Path, name: str, fingerprint: str) -> bool:
    callback = _trust_prompt
    if callback is not None:
        try:
            return bool(callback(str(plugin_dir), name, fingerprint))
        except Exception:
            return False
    # 默认：交互终端（CLI）弹确认；非交互/headless 环境直接拒绝
    if sys.stdin is not None and sys.stdout is not None \
            and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            print(f"\n[插件安全确认] 插件 {name} 首次加载或内容已变化: {plugin_dir}")
            print("  插件代码将以 星瑶Code 进程的全部权限执行，请确认来源可信。")
            answer = input("  信任并加载该插件？[y/N] ").strip().lower()
            return answer in ("y", "yes")
        except (EOFError, OSError, KeyboardInterrupt):
            return False
    return False


def _check_plugin_trusted(plugin_dir: Path, name: str) -> bool:
    """判定插件是否可加载：内置自动信任；用户级按指纹确认，未确认拒绝。"""
    try:
        fingerprint = _plugin_fingerprint(plugin_dir)
    except OSError as exc:
        _logger.warning("插件 %s 指纹计算失败，已跳过: %s", name, exc)
        return False
    key = str(plugin_dir.resolve())
    store = _load_trust_store()
    if _is_builtin_plugin(plugin_dir):
        # 内置插件随仓库发布，视为用户自己的代码：记录指纹并自动信任
        if store.get(key) != fingerprint:
            store[key] = fingerprint
            _save_trust_store(store)
        return True
    if store.get(key) == fingerprint:
        return True
    if _confirm_plugin_trust(plugin_dir, name, fingerprint):
        store[key] = fingerprint
        _save_trust_store(store)
        _logger.info("插件 %s 已确认信任并记录指纹", name)
        return True
    _logger.warning(
        "插件 %s (%s) 未获信任，已跳过加载。"
        "在交互终端启动 ycode 时会弹出信任确认；或在确认来源可信后重试。",
        name, plugin_dir,
    )
    return False


def _user_plugin_dir() -> Path:
    try:
        import config
        return Path(config.YCODE_DIR) / "plugins"
    except Exception:
        return Path.home() / ".ycode" / "plugins"


def _live_registry():
    """取调度层（core/llm.py）的运行时工具注册表单例；取不到（如独立测试进程）返回 None。"""
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


def _prune_orphan_providers(loaded_names: set[str]) -> list[str]:
    """兜底清理"来源插件本次未加载"的孤儿 provider，返回被清理的 provider 名列表。

    只处理带 managed_by 标记的 provider：用户手配的配置没有该字段，永远不会被碰。
    典型场景：插件被禁用（或直接改了 plugin.json）后，它自举写入的 provider 仍留在
    配置里，模型选择器就会一直显示一个已经用不了的厂商。
    """
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

    orphans = [
        p for p in (cfg.get("providers") or [])
        if isinstance(p, dict) and p.get("managed_by") and p["managed_by"] not in loaded_names
    ]
    if not orphans:
        return []
    orphan_names = [str(p.get("name") or p.get("id") or "?") for p in orphans]

    def _mutate(data: dict) -> None:
        removed_model_ids = set()
        kept = []
        for p in data.get("providers") or []:
            # 写路径内二次判定：只删仍然符合"带标记且插件未加载"的项
            if isinstance(p, dict) and p.get("managed_by") and p["managed_by"] not in loaded_names:
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


def toggle_plugin(name: str, enabled: bool) -> tuple[bool, str]:
    """切换插件开关：写回 plugin.json 持久化，并热挂载/卸载运行时工具（无需重启）。

    返回 (ok, msg)。持久化保证 CLI / GUI 下次启动状态一致；热切换通过直接操作
    core.llm.tool_registry 单例让开关立即生效，两端行为同步。
    """
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

    # 热切换：直接在当前进程的运行时注册表上挂载/卸载插件工具
    hot_note = ""
    registry = _live_registry()
    if registry is not None:
        try:
            if enabled:
                # 启用即加载：未获信任的插件拒绝热加载（信任持久化不受影响）
                if not _check_plugin_trusted(target, name):
                    return False, (
                        f"插件 {name} 未通过信任确认，已拒绝热加载"
                        "（在交互终端重启 ycode 时会弹出信任确认）"
                    )
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
                # 禁用后调用插件可选的 cleanup()，回收它写入的持久化数据（如自举的 provider）
                hot_note = _run_cleanup_hook(name)
        except SystemExit as exc:
            # 同 load_and_register：SystemExit 按热切换失败处理，不穿透中断调用方
            #（KeyboardInterrupt 仍向上传播）。
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
    """显式信任指定插件：计算其内容指纹并写入信任持久化存储。

    供 GUI 桥接层调用，等效于 CLI 首次加载插件时在信任确认中回答 y。
    背景：GUI 进程通常非交互终端（stdin 非 tty），默认确认函数恒返回
    False，用户永远无法在 GUI 里信任插件；本接口让上层可以显式放行。

    行为：
    - 只按指纹持久化信任，不改动 plugin.json 的 enabled 状态、不注册工具；
    - 信任后（内容不变的前提下）下次 load_and_register / toggle 启用可直接
      加载，无需再弹确认；
    - 对已信任插件重复调用是幂等的（重写相同指纹，仍返回成功）。

    返回 (ok, msg)：成功时 msg 为可展示的中文提示；找不到插件或写入失败时
    返回 (False, 错误描述)。
    """
    target: Path | None = None
    for info in _PLUGIN_INFOS:
        if info.get("name") == name:
            target = Path(info["path"])
            break
    # 兜底：内存记录里没找到就扫一遍目录（与 toggle_plugin 同款）
    if target is None:
        for base in plugin_dirs():
            child = base / name
            if (child / "plugin.json").is_file():
                target = child
                break
    if target is None or not (target / "plugin.json").is_file():
        return False, f"未找到插件 {name}"

    try:
        fingerprint = _plugin_fingerprint(target)
    except OSError as exc:
        return False, f"计算插件 {name} 指纹失败: {exc}"

    try:
        store = _load_trust_store()
        store[str(target.resolve())] = fingerprint
        _save_trust_store(store)
    except Exception as exc:
        return False, f"写入插件信任记录失败: {exc}"

    _logger.info("插件 %s 已被显式信任并记录指纹: %s", name, target)
    return True, f"已信任插件 {name}，下次启动自动加载"


def plugin_dirs() -> list[Path]:
    """返回插件扫描目录列表。用户目录在前，使其同名插件优先于内置。"""
    return [_user_plugin_dir(), _builtin_plugin_dir()]


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
    # 注册成功后才记录模块引用，避免对半初始化的模块被调用 cleanup
    _LOADED_MODULES[name] = module
    return name


def _unload_removed_plugin(name: str, tools: list, registry) -> None:
    """插件目录被删除后的运行期清理：注销工具并调用插件的 cleanup 钩子。

    为什么需要：refresh_plugins 此前只清信息记录；已加载插件的工具仍挂在
    registry、cleanup 不会被调用（如 provider 自举的回收）。目录删除不经过
    toggle 流程，这里是唯一的清理点。每一步独立隔离，单项失败不影响其余
    清理与后续扫描。
    """
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
    """增量刷新插件信息：加载运行期间新安装的插件，移除已从磁盘删除的记录。

    解决"运行期间安装的插件要重启进程才显示"的问题（此前 _PLUGIN_INFOS 只在
    启动时 load_and_register 扫一次盘）。已加载的插件不会重复注册；
    单插件失败仅记录，不影响其他插件。registry 缺省取运行时注册表单例。
    """
    if registry is None:
        registry = _live_registry()

    # 1) 移除磁盘上已删除（plugin.json 不存在）的插件记录；已加载的做完整卸载
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
    # 已被占用的插件名（overridden 记录不占用：它是被同名压制的那个）
    taken_names = {
        str(info.get("name") or "")
        for info in _PLUGIN_INFOS
        if info.get("status") != "overridden"
    }

    # 2) 扫盘发现新插件（目录优先级与记录结构同 load_and_register）
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
            if registry is None:
                # 无运行时注册表（极端降级）：只记录元信息，不执行加载
                info["status"] = "disabled" if meta.get("enabled", True) is False else "loaded"
                taken_names.add(plugin_name)
                _PLUGIN_INFOS.append(info)
                continue
            if meta.get("enabled", True) is not False and not _check_plugin_trusted(child, plugin_name):
                info["status"] = "untrusted"
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
                # 同 load_and_register：SystemExit 按"该插件热加载失败"处理，
                # 不穿透中断刷新流程（KeyboardInterrupt 仍向上传播）。
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
    """扫描所有插件目录并注册插件工具。返回成功加载的插件名列表。

    单个插件的任何异常都被捕获、记录并跳过，保证 ycode 主流程不受影响。
    """
    # 重复调用时沿用上一轮的插件工具清单（本轮对已加载插件跳过重复注册，见下方分支）
    previous_tools = {
        str(info.get("name") or ""): list(info.get("tools") or [])
        for info in _PLUGIN_INFOS
    }
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
            if meta.get("enabled", True) is not False and not _check_plugin_trusted(child, plugin_name):
                info["status"] = "untrusted"
                seen.add(plugin_name)
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
                # SystemExit 继承 BaseException，不会落入 except Exception：
                # 单独按"该插件加载失败"处理，防止插件退出码穿透中断星瑶启动。
                # KeyboardInterrupt 不在此列（同为 BaseException 子类），维持向上传播。
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
    # 禁用/失败/未受信任的插件都不算已加载。整段隔离：清理异常绝不影响星瑶启动。
    try:
        _prune_orphan_providers(set(loaded))
    except Exception as exc:
        _logger.warning("孤儿 provider 兜底清理异常（已忽略）: %s", exc)
    return loaded
