"""调试适配器供给：定位 debugpy / dlv / lldb-dap 并给出缺失时的可执行提示。

只用标准库；不自动安装任何东西（Python 侧不引入第三方依赖）。缺失时返回
带安装命令的中文提示，由用户在终端自行安装——星瑶不替用户改环境。

传输方式差异（实测口径）：
- debugpy / lldb-dap 走 stdio：直接对适配器进程的 stdin/stdout 说 DAP。
- dlv dap 只支持 TCP：进程启动后监听随机端口并把端口打进 stderr，
  因此用 --listen=127.0.0.1:0，由 DapProcess 从 stderr 解析端口再连。

与 S2 的 `core/ide/server_provision.py` 同构：只回答「能不能找到、怎么起」，
不启动进程、不校验协议版本；探测结果进程内缓存并做克隆隔离（`_clone_probe`），
返回给调用方（Task 3 的 DapProcess）的对象与缓存不共享可变字段。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

STDIO = "stdio"
TCP = "tcp"

# 语言 id -> 适配器 id（语言 id 由 S2 的 language_registry 提供）
LANGUAGE_ADAPTERS: Dict[str, str] = {
    "python": "debugpy",
    "go": "dlv",
    "c": "lldb-dap",
    "cpp": "lldb-dap",
    "rust": "lldb-dap",
}

# .cmd/.bat 是 Windows 批处理 shim，不是 PE 可执行文件，不能直接作为 argv[0] 交给
# CreateProcess（S2 踩过，见 core/ide/lsp_manager.py 的 wrap_command）。按后缀判断
# 即可，不需要再判断平台：只有 Windows 的 PATH 探测才会命中这类 shim。
_WINDOWS_SCRIPT_SUFFIXES = (".cmd", ".bat")

_CACHE_LOCK = threading.Lock()

# 探测结果缓存：python 的探测要起一个解释器进程（几十到几百毫秒），
# 而调试面板刷新与 adapter_availability() 会反复问同一批语言。
# 只放成功结果（失败不缓存，见 probe_adapter）：失败一旦被缓存，用户运行中把适配器
# 装好之后仍会一直看到「缺失」，只能靠重启星瑶。
_CACHE: Dict[str, "AdapterProbe"] = {}


@dataclass(frozen=True)
class AdapterSpec:
    """一个可用的调试适配器启动规格。"""

    adapter_id: str           # "debugpy" / "dlv" / "lldb-dap"
    language_id: str
    command: List[str]        # 完整启动命令（已按平台调整成可直接 Popen 的形式）
    transport: str = STDIO    # STDIO / TCP
    cwd: str = ""


@dataclass(frozen=True)
class AdapterProbe:
    """探测结果：ok=True 时 spec 有效，否则 hint 是给用户的中文说明。"""

    ok: bool
    adapter_id: str
    spec: Optional[AdapterSpec] = None
    hint: str = ""


def invalidate_cache() -> None:
    """清空探测缓存（用户装好适配器、或 PATH 变了之后调用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


def _clone_spec(spec: Optional[AdapterSpec]) -> Optional[AdapterSpec]:
    if spec is None:
        return None
    return AdapterSpec(
        spec.adapter_id,
        spec.language_id,
        list(spec.command),
        spec.transport,
        spec.cwd,
    )


def _clone_probe(probe: AdapterProbe) -> AdapterProbe:
    """返回与缓存、与调用方都互不共享可变对象的结果副本。

    `AdapterSpec` 虽是 frozen，但它的 `command` 是 list：把缓存里的同一个
    `AdapterSpec` 直接交出去，调用方一旦原地 append 启动参数，下一次探测拿到的
    命令就被改过了（S2 的 `_clone_result` 踩过同一个坑）。这里显式复制 list。
    """
    return AdapterProbe(probe.ok, probe.adapter_id, _clone_spec(probe.spec), probe.hint)


def _run_probe(command: List[str], timeout: float = 10.0) -> bool:
    """跑一条探测命令，退出码 0 视为可用（输出丢弃，绝不弹控制台窗口）。"""
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _debugpy_python() -> str:
    """debugpy 必须装在与星瑶同一解释器里，否则 -m debugpy.adapter 找不到模块。"""
    return sys.executable or "python"


def _wrap_for_popen(argv: List[str]) -> List[str]:
    """把 argv 调整成真正能被 Popen 执行的形式（.cmd/.bat 用 cmd.exe /c 包一层）。

    在探测阶段就包装（而不是留给 Task 3 的 DapProcess），是为了让 AdapterSpec.command
    的契约简单且唯一：`spec.command` 一律可以直接交给 `Popen`。
    """
    argv = [str(part) for part in argv]
    if argv and argv[0].lower().endswith(_WINDOWS_SCRIPT_SUFFIXES):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/c", *argv]
    return argv


def _find_executable(names: Sequence[str]) -> str:
    """在 PATH 里按顺序找第一个命中的候选名（复用 shutil.which 的平台规则）。"""
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def _assess_candidate(path: str) -> str:
    """候选路径是否真的能用；返回空串表示「没什么可补充的」，否则是中文原因。

    `shutil.which` 已经过滤了多数坏候选，但它命中的东西在用户环境里仍可能是
    目录、或（POSIX 下）没有执行权限的文件。这类「找着了但用不了」不能静默当成
    「没安装适配器」——否则用户永远看不出自己的 PATH 里有一个坏条目。

    完全没有候选（path 为空）时返回空串：这种情况的说明由各适配器的安装提示
    （「未找到 X。安装：…」）负责，不需要再叠一层原因。

    **调用契约（Task 6 的「手动指定适配器路径」必须遵守）**：任何来自用户输入的候选
    路径都要先过本函数校验再交给下面的分支使用。本函数只查「目录 / 无执行权限」这类
    坏条目，因为它的历史输入一律来自 `shutil.which`（命中即存在）；用户手填的路径
    可能压根不存在，接入入口时除了本函数还要补 `os.path.exists` 检查，否则用户填一个
    不存在的路径会拿到「假成功」的 AdapterSpec，直到真正开始调试才失败。当前没有手填
    入口，所以这条只是契约（见 tests 里钉住该边界的用例）。
    """
    if not path:
        return ""
    if os.path.isdir(path):
        return f"候选是目录而不是可执行文件：{path}"
    if os.name != "nt" and os.path.exists(path) and not os.access(path, os.X_OK):
        return f"候选文件没有执行权限：{path}"
    return ""


def _missing_hint(problem: str, install_hint: str) -> str:
    if problem:
        return f"PATH 中的候选无法使用（{problem}）。{install_hint}"
    return install_hint


def _probe_adapter_uncached(language_id: str) -> AdapterProbe:
    adapter_id = LANGUAGE_ADAPTERS.get(language_id, "")
    if not adapter_id:
        return AdapterProbe(False, "", hint=f"星瑶暂不支持调试该语言：{language_id or '未知'}")
    if adapter_id == "debugpy":
        python = _debugpy_python()
        if _run_probe([python, "-c", "import debugpy"]):
            return AdapterProbe(
                True, adapter_id,
                AdapterSpec(
                    adapter_id, language_id,
                    _wrap_for_popen([python, "-m", "debugpy.adapter"]),
                    STDIO,
                ),
            )
        return AdapterProbe(
            False, adapter_id,
            hint=(
                "未安装 debugpy（Python 调试适配器）。请在本机终端执行："
                f'"{python}" -m pip install debugpy'
            ),
        )
    if adapter_id == "dlv":
        path = _find_executable(("dlv",))
        problem = _assess_candidate(path)
        if path and not problem:
            return AdapterProbe(
                True, adapter_id,
                AdapterSpec(
                    adapter_id, language_id,
                    _wrap_for_popen([path, "dap", "--listen=127.0.0.1:0"]),
                    TCP,
                ),
            )
        return AdapterProbe(
            False, adapter_id,
            hint=_missing_hint(
                problem,
                "未找到 dlv（Go 调试器）。安装："
                "go install github.com/go-delve/delve/cmd/dlv@latest",
            ),
        )
    lldb_dap = _find_executable(("lldb-dap", "lldb-vscode"))
    problem = _assess_candidate(lldb_dap)
    if lldb_dap and not problem:
        # `--stdio` 只是显式声明，保留不影响启动（S3-T3 评审第 3 条已核实）：
        # LLVM 15 的 lldb-vscode 与 LLVM 18 的 lldb-dap，Options.td 里都**没有**
        # stdio 选项，而且两者解析完 argv 后都不检查 OPT_unknown——未知开关被静默
        # 忽略，默认就是 stdio 通信。所以旧名 fallback 不会因为这个开关启动失败。
        # 若将来某版本的二进制改成拒绝未知开关（或要求显式 --stdio），改这一行即可。
        return AdapterProbe(
            True, adapter_id,
            AdapterSpec(
                adapter_id, language_id,
                _wrap_for_popen([lldb_dap, "--stdio"]),
                STDIO,
            ),
        )
    return AdapterProbe(
        False, adapter_id,
        hint=_missing_hint(
            problem,
            "未找到 lldb-dap（LLVM 调试适配器）。安装 LLVM 后确认 lldb-dap 在 PATH 中"
            "（Windows 版 LLVM 自带 lldb-dap.exe）",
        ),
    )


def probe_adapter(language_id: str) -> AdapterProbe:
    """探测某语言的调试适配器；未支持的语言也返回 probe（ok=False + 提示）。

    返回的 probe 与缓存不共享可变对象（见 `_clone_probe`）。

    **只缓存成功结果**（S3-T3 评审发现）：失败结果一旦进缓存，用户运行中按提示在终端
    装好 debugpy/dlv 之后，调试面板会一直显示缺失，直到重启星瑶——而 `invalidate_cache()`
    在 S3-T3 之前全仓只有测试在调，没有生产失效入口。「装完回来看面板」是主路径，所以
    失败必须重新探测；「探测要起解释器进程、慢」的性能动机由成功结果缓存保住。
    """
    key = str(language_id or "")
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return _clone_probe(cached)
    probe = _probe_adapter_uncached(key)
    if probe.ok:
        with _CACHE_LOCK:
            _CACHE[key] = _clone_probe(probe)
    return probe


def adapter_availability() -> List[Dict[str, object]]:
    """给调试面板与设置页用的全量探测摘要（每语言一条）。"""
    rows: List[Dict[str, object]] = []
    for language_id in ("python", "go", "c", "cpp", "rust"):
        probe = probe_adapter(language_id)
        rows.append({
            "language_id": language_id,
            "adapter_id": LANGUAGE_ADAPTERS[language_id],
            "available": probe.ok,
            "hint": probe.hint,
        })
    return rows
