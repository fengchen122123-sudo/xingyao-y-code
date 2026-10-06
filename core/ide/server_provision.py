# -*- coding: utf-8 -*-
"""语言服务器可执行文件的定位、探测与手动覆盖。

定位优先级（从高到低）：
1. 用户在设置页手动指定的绝对路径（存 <数据目录>/ide_lsp_servers.json）；
2. 随安装包分发的内置件（冻结时优先 <_MEIPASS>/lsp_bin，再回落 exe 同级 lsp_bin；
   源码运行时为仓库根 lsp_bin，打包时登记）；
3. 系统 PATH 探测（shutil.which）。

本模块只回答「能不能找到、在哪」，不校验协议版本、不启动进程。
Python 侧不引入任何第三方依赖：内置件是外部可执行文件，由安装包携带，
用户环境里没装的语言则回落 PATH 探测，探测不到也不影响编辑器基本编辑。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading

from core.ide import language_registry

_OVERRIDE_FILE = "ide_lsp_servers.json"

_CACHE_LOCK = threading.Lock()

# 探测结果缓存：设置页每次刷新都会重跑定位，进程生命周期内缓存即可
_CACHE: dict[str, dict] = {}


class ProvisionError(ValueError):
    """手动指定的路径非法。"""


def _config_dir() -> str:
    env = os.environ.get("YCODE_DATA_DIR")
    if env:
        return env
    try:
        import config

        base = getattr(config, "YCODE_DIR", None)
        if base:
            return str(base)
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), ".ycode")


def _config_path() -> str:
    return os.path.join(_config_dir(), _OVERRIDE_FILE)


def _builtin_dirs() -> list[str]:
    """随包分发的语言服务器候选目录（按优先级从高到低）。

    本仓库的随包资源一律走 PyInstaller 的 `_MEIPASS`（onedir 打包下即
    `_internal/`，见 core/lsp.py 的 ripgrep、core/diagram_export.py 的 mermaid、
    core/plugin_loader.py、core/skills.py）。`ycode.spec` 是 onedir 且 datas 收在
    `_internal/`，所以冻结运行时必须优先查 `<_MEIPASS>/lsp_bin`；exe 同级目录
    只作为历史约定兜底（按 exe 同级查找会让内置服务器永远发现不了、静默回落 PATH）。
    源码运行时以仓库根（core/ide 的上两级）为根。去重后返回。
    """
    candidates: list[str] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(str(meipass), "lsp_bin"))
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.join(exe_dir, "lsp_bin"))
    else:
        here = os.path.abspath(__file__)
        root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
        candidates.append(os.path.join(root, "lsp_bin"))
    unique: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _builtin_dir() -> str:
    """首选内置件目录，仅供日志与排查使用（真正查找请用 _builtin_dirs）。"""
    return _builtin_dirs()[0]


def _candidate_names(name: str) -> list[str]:
    names = [name]
    if os.name == "nt":
        names.extend([f"{name}.exe", f"{name}.cmd", f"{name}.bat"])
    return names


def load_overrides() -> dict:
    path = _config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str) and v}


def _write_overrides(data: dict) -> None:
    path = _config_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ycode-lsp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_override(server_id: str, path: str) -> dict:
    """记录手动指定的可执行文件路径。

    路径不存在、不是文件、或（POSIX 下）没有执行权限时抛 ProvisionError：
    宁可当场报错，也不要让用户以为设置成功了、实际打开文件时才发现启动失败。
    """
    if server_id not in language_registry.SPECS:
        raise ProvisionError(f"未知的语言服务器：{server_id}")
    real = os.path.realpath(os.path.abspath(path))
    if not os.path.isfile(real):
        raise ProvisionError(f"路径不存在或不是文件：{path}")
    if os.name != "nt" and not os.access(real, os.X_OK):
        raise ProvisionError(f"文件没有执行权限：{path}")
    data = load_overrides()
    data[server_id] = real
    _write_overrides(data)
    invalidate_cache()
    return {"server": server_id, "path": real, "source": "override"}


def clear_override(server_id: str) -> dict:
    data = load_overrides()
    existed = data.pop(server_id, None)
    _write_overrides(data)
    invalidate_cache()
    return {"server": server_id, "path": "", "cleared": bool(existed)}


def invalidate_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _find_in_builtin(name: str) -> str | None:
    """按优先级遍历内置件候选目录，先命中先返回。"""
    for directory in _builtin_dirs():
        for candidate in _candidate_names(name):
            full = os.path.join(directory, candidate)
            if os.path.isfile(full):
                return os.path.realpath(full)
    return None


def _find_in_path(name: str) -> str | None:
    for candidate in _candidate_names(name):
        found = shutil.which(candidate)
        if found:
            return os.path.realpath(found)
    return None


def _clone_result(result: dict) -> dict:
    """返回与缓存、与调用方都互不共享可变对象的结果副本。

    `dict(result)` 是浅拷贝：extra_args 这个列表会在缓存与调用方之间共享，
    调用方一旦原地追加参数就会污染缓存里的同一份结果（跨工作区泄漏）。
    这里显式复制列表，保证「谁都不许原地改别人的数据」。
    """
    clone = dict(result)
    clone["extra_args"] = list(result.get("extra_args") or [])
    return clone


def resolve_executable(server_id: str) -> dict:
    """解析某个语言服务器的可执行文件。

    source 为 "missing" 时 executable 为空串，msg 里带可执行的修复指引；
    调用方（lsp_manager）据此报错，设置页据此展示状态。
    """
    spec = language_registry.get_spec(server_id)
    if spec is None:
        return {
            "server": server_id,
            "executable": "",
            "source": "missing",
            "extra_args": [],
            "msg": f"未知的语言服务器：{server_id}",
        }
    with _CACHE_LOCK:
        cached = _CACHE.get(server_id)
    if cached is not None:
        return _clone_result(cached)

    result = {
        "server": server_id,
        "executable": "",
        "source": "missing",
        "extra_args": [],
        "msg": "",
    }
    override_path = load_overrides().get(server_id)
    if override_path and os.path.isfile(override_path):
        result["executable"] = os.path.realpath(override_path)
        result["source"] = "override"
    else:
        if override_path:
            result["msg"] = f"手动指定的路径已失效（{override_path}），已回落为自动探测；"
        bundled = _find_in_builtin(spec.command[0])
        if bundled:
            result["executable"] = bundled
            result["source"] = "bundled"
        else:
            in_path = _find_in_path(spec.command[0])
            if in_path:
                result["executable"] = in_path
                result["source"] = "path"
    if not result["executable"]:
        result["msg"] += (
            f"未找到 {spec.command[0]}：请安装它并加入 PATH，"
            f"或在设置页「编辑器与语言服务」手动指定可执行文件路径"
        )
    with _CACHE_LOCK:
        _CACHE[server_id] = _clone_result(result)
    return result


def server_status(server_id: str) -> dict:
    """设置页用：合并静态配置与定位结果。"""
    spec = language_registry.get_spec(server_id)
    resolved = resolve_executable(server_id)
    return {
        "server": server_id,
        "languages": list(spec.language_ids) if spec else [],
        "command": " ".join(spec.command) if spec else "",
        "executable": resolved["executable"],
        "source": resolved["source"],
        "msg": resolved["msg"],
    }


def all_server_status() -> list[dict]:
    return [server_status(server_id) for server_id in sorted(language_registry.SPECS)]
