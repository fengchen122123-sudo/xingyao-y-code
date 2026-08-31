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
_BUILTIN_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"

# 最近一次 load_and_register 的扫描结果（含禁用/失败/被覆盖的插件），
# 供 /plugins 命令与诊断查询；每次扫描开头会整体重建。
_PLUGIN_INFOS: list[dict] = []


def get_plugin_infos() -> list[dict]:
    """返回最近一次插件扫描的信息列表（dict 副本，调用方修改不影响内部状态）。

    每条记录字段：name / version / description / path / source(user|builtin) /
    status(loaded|disabled|failed|overridden|untrusted) / tools(list[str]) / error(可选)。
    """
    return [dict(info) for info in _PLUGIN_INFOS]


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
            print("  插件代码将以 ycode 进程的全部权限执行，请确认来源可信。")
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


def refresh_plugins(registry=None) -> None:
    """增量刷新插件信息：加载运行期间新安装的插件，移除已从磁盘删除的记录。

    解决"运行期间安装的插件要重启进程才显示"的问题（此前 _PLUGIN_INFOS 只在
    启动时 load_and_register 扫一次盘）。已加载的插件不会重复注册；
    单插件失败仅记录，不影响其他插件。registry 缺省取运行时注册表单例。
    """
    if registry is None:
        registry = _live_registry()

    # 1) 移除磁盘上已删除（plugin.json 不存在）的插件记录
    for info in list(_PLUGIN_INFOS):
        path = info.get("path")
        if path and not (Path(path) / "plugin.json").is_file():
            _PLUGIN_INFOS.remove(info)

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
