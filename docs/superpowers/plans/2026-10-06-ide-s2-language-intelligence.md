# 星瑶 IDE 模式 S2「语言智能」实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 IDE 编辑器具备接近 VSCode 的语言智能：按工作区按需拉起语言服务器，把补全、诊断、悬停、跳转定义、格式化、重命名接到 CodeMirror 6 上，并在设置页暴露各语言服务器的状态与手动路径入口。

**Architecture:** 后端在既有 `core/ide/` 包内新增四块：`language_registry`（扩展名到语言 id 到服务器启动配置的静态映射）、`server_provision`（内置件定位 + PATH 探测 + 用户手动路径覆盖）、`lsp_codec`（`Content-Length` 帧的有状态编解码，容忍分片与粘包）、`lsp_manager`（按工作区按需启动进程、initialize/initialized 握手、shutdown+exit 优雅退出、超时强杀、崩溃限次重启、切工作区回收），外加 `diagnostics_batcher`（200 ms 节流合批）。`gui_bridge.py` 暴露 `lsp_request` / `lsp_notify` / `lsp_server_status` / `lsp_set_server_path` / `lsp_sync_workspace`，事件 `lsp_diagnostics` / `lsp_status` 一律经 `emit_event` 进 `_js_queue`。前端 `editor/lsp.ts` 负责桥调用、文档同步（didOpen/didChange/didClose）与 CodeMirror 能力映射，`EditorLanguageSettings.vue` 承载设置分区。

**Tech Stack:** Python 3.10+（仅标准库）、pywebview 桥（`window.pywebview.api` + 事件管线）、LSP 3.17（stdio + `Content-Length` 帧）、jedi-language-server（内置件，外部可执行文件）、Vue 3 + Pinia + Vite + TypeScript、CodeMirror 6（`@codemirror/autocomplete` / `@codemirror/lint`）、pytest、vitest + @vue/test-utils。

## Global Constraints

以下约束适用于本计划的每一个任务，逐字取自 `docs/superpowers/specs/2026-10-06-ide-mode-design.md`，并补充 S1 已确立的工程约定：

- 开发分支固定为 `ide`，不切到 `dev` 或 `master`。
- 版本号只在发布时改 `core/version.py`，本计划任何任务都不得改版本号。
- 不得修改 `core/lsp.py`（`find_definition` 工具的实现），IDE 相关能力一律放 `core/ide/`。
- 不得改变 `build_stable_prompt` 的字节输出；`tests/test_prompt_byte_stability.py` 必须保持通过。
- 不得把编辑器内容或打开文件列表注入 Agent 上下文；IDE 对模型请求路径零影响。
- 所有前端可见事件必须经 `gui_bridge.py` 的 `_js_queue` 与 `gui-js-dispatcher` 单消费者线程派发，事件源线程只允许 `put_nowait`，禁止直接 `evaluate_js`；桥对象上已有的公开方法是 `emit_event(name, payload)`，新代码只能调它。
- `evaluate_js` 只能在 `gui-js-dispatcher` 单线程调用，语言服务器的读线程只做协议解析与回调，不发任何 GUI 调用。
- 语言服务器是用户级进程，不经过 `permission_broker`；但进程只能由 Python 后端拉起，路径限定工作区（凭据配置文件永久拒绝）。
- Python 侧不引入第三方依赖：语言服务器是外部可执行文件，由安装包内置件或用户环境提供，不算 Python 依赖。
- 诊断推送必须节流合批（200 ms），补全请求必须前端防抖并支持 `$/cancelRequest`。
- 语言服务器生命周期绑定工作区：切换工作区必须优雅 `shutdown` + `exit`，超时强杀，不得残留进程。
- 前端 CSP 保持 `default-src 'self'`，不引入 eval、不引入外部 CDN。
- 后端测试用 pytest（`tests/test_*.py`，运行 `python -m pytest tests/test_xxx.py -v`），前端测试用 vitest（`frontend/src/**/*.test.ts` 与源文件同目录，运行 `cd frontend && npx vitest run src/xxx.test.ts`）。
- `frontend/dist` 与 `dist/` 永不提交。

---

## File Structure

**后端（全部为 Python 标准库实现）**

| 文件 | 职责 |
|---|---|
| `core/ide/language_registry.py` | 扩展名 → LSP languageId → 语言服务器启动配置（命令、参数、初始化选项、根标识文件） |
| `core/ide/server_provision.py` | 内置件定位（`<安装根>/lsp_bin/`）、`shutil.which` 探测、用户手动路径覆盖、状态查询 |
| `core/ide/lsp_codec.py` | `Content-Length` 帧编解码：有状态解码器，容忍分片到达与粘包 |
| `core/ide/lsp_manager.py` | LSP 进程生命周期（启动、握手、请求转发、优雅退出、超时强杀、崩溃限次重启、按工作区回收） |
| `core/ide/diagnostics_batcher.py` | `publishDiagnostics` 的 200 ms 节流合批 |
| `gui_bridge.py`（改） | 新增 LSP 桥方法、诊断合批派发、工作区切换回收 |

**前端**

| 文件 | 职责 |
|---|---|
| `frontend/src/editor/lsp.ts` | LSP 前端客户端：请求/通知、文档同步、诊断缓存、事件分发、CodeMirror 能力映射 |
| `frontend/src/editor/setup.ts`（改） | `createEditorState` 支持追加额外扩展（LSP 扩展由此注入） |
| `frontend/src/components/EditorPane.vue`（改） | 打开/关闭/编辑时同步文档，注入 LSP 扩展 |
| `frontend/src/components/EditorLanguageSettings.vue` | 设置页「编辑器与语言服务」分区组件 |
| `frontend/src/App.vue`（改） | 把 `lsp_diagnostics` / `lsp_status` 事件转给 `dispatchLspEvent` |
| `frontend/src/types/pywebview.d.ts`（改） | LSP 桥方法与结果类型声明 |

**测试**

| 文件 | 职责 |
|---|---|
| `tests/fake_lsp_server.py` | 测试用的假语言服务器（最小 LSP 骨架 + `test/*` 分支钩子） |
| `tests/test_ide_language_registry.py` | 扩展名映射与配置一致性 |
| `tests/test_ide_server_provision.py` | 定位优先级、探测缓存、手动覆盖 |
| `tests/test_ide_lsp_codec.py` | 帧编解码（分片、粘包、异常头） |
| `tests/test_ide_lsp_process.py` | 单进程生命周期 |
| `tests/test_ide_lsp_manager.py` | 按工作区启动、崩溃限次重启、回收 |
| `tests/test_ide_diagnostics_batcher.py` | 节流合批语义 |
| `tests/test_gui_lsp_bridge.py` | 桥方法、路径策略、诊断事件派发 |
| `tests/test_ide_lsp_workspace_recycle.py` | 切换工作区回收进程 |
| `tests/test_ide_lsp_regression.py` | 回归红线守卫（提示词字节稳定、事件通道、依赖） |
| `frontend/src/editor/lsp.test.ts` | 前端客户端与事件分发 |
| `frontend/src/editor/lspExtensions.test.ts` | CodeMirror 能力映射与扩展组装 |
| `frontend/src/components/EditorLanguageSettings.test.ts` | 设置分区组件 |

---

## Task 1: 语言注册表（扩展名 → languageId → 服务器配置）

**Files:**
- Create: `core/ide/language_registry.py`
- Test: `tests/test_ide_language_registry.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `ServerSpec`（dataclass，frozen）：字段 `server_id: str`、`language_ids: tuple[str, ...]`、`command: tuple[str, ...]`、`args: tuple[str, ...]`、`initialization_options: dict`、`root_markers: tuple[str, ...]`
  - `SPECS: dict[str, ServerSpec]`，键为 `server_id`
  - `language_id_for_path(path: str) -> str`
  - `server_id_for_language(language_id: str) -> str`（未登记返回 `""`）
  - `get_spec(server_id: str) -> ServerSpec | None`
  - `all_language_servers() -> list[tuple[str, str]]`（`(language_id, server_id)`，已排序）
  - `root_markers_for_language(language_id: str) -> tuple[str, ...]`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_ide_language_registry.py`：

```python
# -*- coding: utf-8 -*-
"""扩展名 -> languageId -> 语言服务器配置 的映射表。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ide import language_registry as reg


def test_python_extensions_map_to_python():
    assert reg.language_id_for_path("E:\\proj\\a.py") == "python"
    assert reg.language_id_for_path("/home/u/a.pyi") == "python"


def test_react_extensions_have_their_own_language_id():
    # LSP 的 languageId 里 jsx / tsx 是独立取值，不能归一成 javascript
    assert reg.language_id_for_path("a.jsx") == "javascriptreact"
    assert reg.language_id_for_path("a.tsx") == "typescriptreact"


def test_windows_and_posix_paths_are_both_handled():
    assert reg.language_id_for_path("E:\\proj\\src\\main.go") == "go"
    assert reg.language_id_for_path("/srv/src/main.go") == "go"


def test_unknown_extension_returns_empty():
    assert reg.language_id_for_path("README.txt") == ""
    assert reg.language_id_for_path("Makefile") == ""


def test_uppercase_extension_still_matches():
    assert reg.language_id_for_path("A.PY") == "python"


def test_server_id_lookup():
    assert reg.server_id_for_language("python") == "jedi"
    assert reg.server_id_for_language("typescriptreact") == "typescript"
    assert reg.server_id_for_language("cobol") == ""


def test_specs_are_internally_consistent():
    for key, spec in reg.SPECS.items():
        assert spec.server_id == key
        assert spec.language_ids, f"{key} 没有登记语言"
        assert spec.command, f"{key} 没有启动命令"
        # 一个 languageId 只能属于一个服务器，否则 didOpen 之后不知道发给谁
        for language_id in spec.language_ids:
            owners = [s.server_id for s in reg.SPECS.values() if language_id in s.language_ids]
            assert owners == [key], f"{language_id} 被 {owners} 重复登记"


def test_all_language_servers_is_sorted_and_complete():
    pairs = reg.all_language_servers()
    assert pairs == sorted(pairs)
    assert len(pairs) == sum(len(s.language_ids) for s in reg.SPECS.values())
    assert ("python", "jedi") in pairs


def test_root_markers():
    assert "pyproject.toml" in reg.root_markers_for_language("python")
    assert reg.root_markers_for_language("cobol") == ()
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_language_registry.py -v
```

预期：FAIL，`ModuleNotFoundError: No module named 'core.ide.language_registry'`。

- [ ] **Step 3: 实现注册表**

创建 `core/ide/language_registry.py`：

```python
# -*- coding: utf-8 -*-
"""扩展名 -> LSP languageId -> 语言服务器配置 的映射表。

本模块只描述「某种语言该用什么服务器、怎么启动、初始化选项是什么」，
不关心可执行文件是否真实存在（那是 server_provision 的职责），也不启动进程
（那是 lsp_manager 的职责）。

languageId 直接透传给 textDocument/didOpen，必须与语言服务器的约定一致，
不能为了好读而改名；LSP 里 jsx / tsx 是独立取值，不归一成 javascript。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ServerSpec:
    """一个语言服务器的静态配置。"""

    server_id: str
    language_ids: tuple[str, ...]
    command: tuple[str, ...]
    args: tuple[str, ...] = ()
    initialization_options: dict = field(default_factory=dict)
    root_markers: tuple[str, ...] = ()


SPECS: dict[str, ServerSpec] = {
    "jedi": ServerSpec(
        server_id="jedi",
        language_ids=("python",),
        command=("jedi-language-server",),
        args=(),
        initialization_options={
            "diagnostics": {"enable": True, "didOpen": True, "didChange": True},
            "workspace": {"extraPaths": []},
        },
        root_markers=("pyproject.toml", "setup.py", "setup.cfg", "pyrightconfig.json", ".git"),
    ),
    "typescript": ServerSpec(
        server_id="typescript",
        language_ids=("javascript", "javascriptreact", "typescript", "typescriptreact"),
        command=("typescript-language-server",),
        args=("--stdio",),
        initialization_options={
            "preferences": {"quotePreference": "double", "includeInlayParameterNameHints": "none"},
        },
        root_markers=("package.json", "tsconfig.json", "jsconfig.json"),
    ),
    "gopls": ServerSpec(
        server_id="gopls",
        language_ids=("go",),
        command=("gopls",),
        args=(),
        initialization_options={
            "usePlaceholders": False,
            "completeUnimported": True,
            "staticcheck": False,
        },
        root_markers=("go.mod", "go.work"),
    ),
    "rust-analyzer": ServerSpec(
        server_id="rust-analyzer",
        language_ids=("rust",),
        command=("rust-analyzer",),
        args=(),
        initialization_options={
            "checkOnSave": {"enable": False},
            "cargo": {"allFeatures": False},
        },
        root_markers=("Cargo.toml",),
    ),
    "clangd": ServerSpec(
        server_id="clangd",
        language_ids=("c", "cpp"),
        command=("clangd",),
        args=("--background-index=false",),
        initialization_options={
            "clangdFileStatus": True,
            "fallbackFlags": ["-std=c++17"],
        },
        root_markers=("compile_commands.json", "CMakeLists.txt", ".clangd", ".git"),
    ),
    "jdtls": ServerSpec(
        server_id="jdtls",
        language_ids=("java",),
        command=("jdtls",),
        args=(),
        initialization_options={
            "settings": {"java": {"signatureHelp": {"enabled": True}}},
        },
        root_markers=("pom.xml", "build.gradle", "settings.gradle", ".classpath"),
    ),
}

_EXT_LANGUAGE: dict[str, str] = {
    "py": "python", "pyi": "python", "pyw": "python",
    "js": "javascript", "mjs": "javascript", "cjs": "javascript",
    "jsx": "javascriptreact",
    "ts": "typescript", "mts": "typescript", "cts": "typescript",
    "tsx": "typescriptreact",
    "go": "go",
    "rs": "rust",
    "c": "c", "h": "c",
    "cc": "cpp", "cpp": "cpp", "cxx": "cpp", "hpp": "cpp", "hh": "cpp", "hxx": "cpp",
    "java": "java",
}


def language_id_for_path(path: str) -> str:
    """按扩展名判断 LSP languageId；未登记的类型返回空串。

    只看扩展名，不看内容与 shebang：打开文件是热路径，多读一次文件会带来
    额外的 IO 与编码歧义，收益不足。
    """
    name = (path or "").replace("\\", "/").rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot < 0:
        return ""
    return _EXT_LANGUAGE.get(name[dot + 1:].lower(), "")


def server_id_for_language(language_id: str) -> str:
    for spec in SPECS.values():
        if language_id in spec.language_ids:
            return spec.server_id
    return ""


def get_spec(server_id: str) -> ServerSpec | None:
    return SPECS.get(server_id)


def all_language_servers() -> list[tuple[str, str]]:
    """返回 (language_id, server_id) 列表，供设置页枚举全部语言服务。"""
    pairs: list[tuple[str, str]] = []
    for spec in SPECS.values():
        for language_id in spec.language_ids:
            pairs.append((language_id, spec.server_id))
    return sorted(pairs)


def root_markers_for_language(language_id: str) -> tuple[str, ...]:
    spec = SPECS.get(server_id_for_language(language_id))
    return spec.root_markers if spec else ()
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_language_registry.py -v
```

预期：9 个用例全部 PASS。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add core/ide/language_registry.py tests/test_ide_language_registry.py
git commit -m "feat(ide): 新增语言注册表（扩展名到语言服务器配置的映射）"
```

---

## Task 2: 语言服务器供给（内置件定位、PATH 探测、手动路径覆盖）

**Files:**
- Create: `core/ide/server_provision.py`
- Test: `tests/test_ide_server_provision.py`

**Interfaces:**
- Consumes: `core.ide.language_registry.SPECS`、`get_spec`
- Produces:
  - `ProvisionError(ValueError)`
  - `resolve_executable(server_id: str) -> dict`，键 `server` / `executable` / `source`（`"override" | "bundled" | "path" | "missing"`）/ `extra_args: list[str]` / `msg`
  - `save_override(server_id: str, path: str) -> dict`、`clear_override(server_id: str) -> dict`
  - `load_overrides() -> dict[str, str]`、`invalidate_cache() -> None`
  - `server_status(server_id: str) -> dict`、`all_server_status() -> list[dict]`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_ide_server_provision.py`：

```python
# -*- coding: utf-8 -*-
"""语言服务器可执行文件的定位优先级与手动覆盖。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ide import language_registry, server_provision as sp


def _prepare(tmp_path, monkeypatch):
    """把数据目录与内置件目录都指到临时目录，测试之间互不污染。"""
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(sp, "_builtin_dir", lambda: str(tmp_path / "bin"))
    sp.invalidate_cache()


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def test_missing_when_nowhere_found(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)
    res = sp.resolve_executable("jedi")
    assert res["source"] == "missing"
    assert res["executable"] == ""
    assert "jedi-language-server" in res["msg"]
    assert "手动指定" in res["msg"]


def test_bundled_wins_over_path(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    _touch(tmp_path / "bin" / "jedi-language-server")
    monkeypatch.setattr(sp.shutil, "which", lambda name: "/usr/bin/" + name)
    res = sp.resolve_executable("jedi")
    assert res["source"] == "bundled"
    assert res["executable"].endswith("jedi-language-server")
    assert res["extra_args"] == []


def test_path_used_when_no_bundled(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(sp.shutil, "which", lambda name: "/opt/ts/" + name)
    res = sp.resolve_executable("typescript")
    assert res["source"] == "path"
    assert res["executable"].endswith("typescript-language-server")


def test_override_wins_over_bundled(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    _touch(tmp_path / "bin" / "jedi-language-server")
    custom = _touch(tmp_path / "my-jedi")
    sp.save_override("jedi", str(custom))
    res = sp.resolve_executable("jedi")
    assert res["source"] == "override"
    assert res["executable"] == os.path.realpath(str(custom))


def test_save_override_rejects_missing_file(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    with pytest.raises(sp.ProvisionError):
        sp.save_override("jedi", str(tmp_path / "nope"))


def test_save_override_rejects_unknown_server(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    f = _touch(tmp_path / "x")
    with pytest.raises(sp.ProvisionError):
        sp.save_override("no-such-server", str(f))


def test_stale_override_falls_back_and_explains(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    gone = _touch(tmp_path / "gone")
    sp.save_override("jedi", str(gone))
    os.unlink(str(gone))
    sp.invalidate_cache()
    monkeypatch.setattr(sp.shutil, "which", lambda name: "/usr/bin/" + name)
    res = sp.resolve_executable("jedi")
    assert res["source"] == "path"
    assert "失效" in res["msg"]


def test_clear_override_restores_autodetect(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    custom = _touch(tmp_path / "my-jedi")
    sp.save_override("jedi", str(custom))
    cleared = sp.clear_override("jedi")
    assert cleared["cleared"] is True
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)
    assert sp.resolve_executable("jedi")["source"] == "missing"


def test_lookup_is_cached_until_invalidated(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    calls = {"n": 0}

    def _which(name):
        calls["n"] += 1
        return None

    monkeypatch.setattr(sp.shutil, "which", _which)
    sp.resolve_executable("jedi")
    first = calls["n"]
    sp.resolve_executable("jedi")
    assert calls["n"] == first
    sp.invalidate_cache()
    sp.resolve_executable("jedi")
    assert calls["n"] > first


def test_all_server_status_covers_every_spec(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)
    rows = sp.all_server_status()
    assert len(rows) == len(language_registry.SPECS)
    assert {r["server"] for r in rows} == set(language_registry.SPECS)
    python_row = [r for r in rows if r["server"] == "jedi"][0]
    assert python_row["languages"] == ["python"]
    assert python_row["command"] == "jedi-language-server"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_server_provision.py -v
```

预期：FAIL，`ModuleNotFoundError: No module named 'core.ide.server_provision'`。

- [ ] **Step 3: 实现供给模块**

创建 `core/ide/server_provision.py`：

```python
# -*- coding: utf-8 -*-
"""语言服务器可执行文件的定位、探测与手动覆盖。

定位优先级（从高到低）：
1. 用户在设置页手动指定的绝对路径（存 <数据目录>/ide_lsp_servers.json）；
2. 随安装包分发的内置件（<安装根>/lsp_bin/<名字>[.exe]，打包时登记）；
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


def _builtin_dir() -> str:
    """随包分发的语言服务器目录：<安装根>/lsp_bin。

    冻结运行时以可执行文件所在目录为根，源码运行时以 core/ide 的上两级为根，
    两者都落在「安装根目录/lsp_bin」，与项目其它内置资源的相对位置约定一致。
    """
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "lsp_bin")
    here = os.path.abspath(__file__)
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(here))), "lsp_bin")


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
    directory = _builtin_dir()
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
        return dict(cached)

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
        _CACHE[server_id] = dict(result)
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
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_server_provision.py -v
```

预期：10 个用例全部 PASS。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add core/ide/server_provision.py tests/test_ide_server_provision.py
git commit -m "feat(ide): 新增语言服务器供给（内置件定位/PATH 探测/手动路径覆盖）"
```

---

## Task 3: LSP 帧编解码（Content-Length，分片与粘包）

**Files:**
- Create: `core/ide/lsp_codec.py`
- Test: `tests/test_ide_lsp_codec.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `LspFrameError(Exception)`
  - `encode_frame(payload: dict, *, encoding: str = "utf-8") -> bytes`
  - `FrameDecoder(*, encoding: str = "utf-8")`，方法 `feed(chunk: bytes) -> list[dict]`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_ide_lsp_codec.py`：

```python
# -*- coding: utf-8 -*-
"""LSP Content-Length 帧编解码：分片到达、粘包、异常头。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ide.lsp_codec import FrameDecoder, LspFrameError, encode_frame


def test_encode_decode_roundtrip():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"x": "中文"}}
    assert FrameDecoder().feed(encode_frame(payload)) == [payload]


def test_length_counts_utf8_bytes_not_characters():
    payload = {"m": "中文"}
    frame = encode_frame(payload)
    length = int(frame.split(b"\r\n", 1)[0].split(b":")[1])
    # 中文一个字符三字节，按字符数算会让服务器少读一半正文
    assert length == len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def test_split_arrival_at_every_byte():
    payload = {"jsonrpc": "2.0", "method": "test/pushed", "params": {"n": 1}}
    frame = encode_frame(payload)
    decoder = FrameDecoder()
    out = []
    for i in range(len(frame)):
        out.extend(decoder.feed(frame[i:i + 1]))
    assert out == [payload]


def test_two_frames_in_one_chunk():
    a = {"id": 1}
    b = {"id": 2}
    assert FrameDecoder().feed(encode_frame(a) + encode_frame(b)) == [a, b]


def test_extra_headers_are_tolerated():
    body = json.dumps({"id": 3}).encode("utf-8")
    frame = (
        b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n"
        b"Content-Length: %d\r\n\r\n" % len(body)
    ) + body
    assert FrameDecoder().feed(frame) == [{"id": 3}]


def test_missing_content_length_raises():
    with pytest.raises(LspFrameError):
        FrameDecoder().feed(b"X-Test: 1\r\n\r\n{}")


def test_invalid_json_body_raises():
    body = b"{ not json"
    frame = b"Content-Length: %d\r\n\r\n" % len(body) + body
    with pytest.raises(LspFrameError):
        FrameDecoder().feed(frame)


def test_negative_length_raises():
    with pytest.raises(LspFrameError):
        FrameDecoder().feed(b"Content-Length: -5\r\n\r\n")


def test_oversized_header_raises():
    with pytest.raises(LspFrameError):
        FrameDecoder().feed(b"X-Test: " + b"a" * (17 * 1024))


def test_partial_header_waits_instead_of_raising():
    decoder = FrameDecoder()
    assert decoder.feed(b"Content-Len") == []
    assert decoder.feed(b"gth: 2\r\n\r\n{}") == [{}]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_lsp_codec.py -v
```

预期：FAIL，`ModuleNotFoundError: No module named 'core.ide.lsp_codec'`。

- [ ] **Step 3: 实现编解码**

创建 `core/ide/lsp_codec.py`：

```python
# -*- coding: utf-8 -*-
"""LSP / DAP 共用的 Content-Length 帧编解码。

帧格式：`Content-Length: <十进制字节数>\r\n\r\n<UTF-8 JSON 正文>`，头部允许
携带其它行（如 Content-Type）。

stdio 是字节流，一次 read 可能只拿到半个帧（分片），也可能一次拿到多个帧
（粘包），因此解码器必须是有状态的累加器：绝不能在缺正文时提前消费缓冲区，
否则下一个帧的头部会被当成正文解析。

本模块是纯状态机，不涉及进程与线程，便于用各种切分方式单测覆盖。
"""
from __future__ import annotations

import json

_HEADER_SEP = b"\r\n\r\n"
_MAX_HEADER_BYTES = 16 * 1024


class LspFrameError(Exception):
    """帧结构非法（缺 Content-Length、长度非整数、头部超长、正文非 JSON）。"""


def encode_frame(payload: dict, *, encoding: str = "utf-8") -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode(encoding)
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def _parse_content_length(header: bytes) -> int:
    for line in header.split(b"\r\n"):
        if not line:
            continue
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                length = int(value.strip())
            except ValueError as exc:
                raise LspFrameError("Content-Length 不是整数") from exc
            if length < 0:
                raise LspFrameError("Content-Length 不能为负")
            return length
    raise LspFrameError("缺少 Content-Length 头")


class FrameDecoder:
    """有状态字节流解码器：喂入任意切分的字节，产出完整消息。"""

    def __init__(self, *, encoding: str = "utf-8"):
        self._buffer = bytearray()
        self._encoding = encoding

    @property
    def pending_bytes(self) -> int:
        """缓冲区里尚未成帧的字节数，用于排查半截帧。"""
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[dict]:
        if chunk:
            self._buffer.extend(chunk)
        messages: list[dict] = []
        while True:
            sep = self._buffer.find(_HEADER_SEP)
            if sep < 0:
                if len(self._buffer) > _MAX_HEADER_BYTES:
                    raise LspFrameError("头部超过 16 KB 仍未见空行，帧结构异常")
                break
            if sep > _MAX_HEADER_BYTES:
                raise LspFrameError("头部超过 16 KB，帧结构异常")
            length = _parse_content_length(bytes(self._buffer[:sep]))
            body_start = sep + len(_HEADER_SEP)
            total = body_start + length
            if len(self._buffer) < total:
                # 正文还没到齐，保持原样等待下一次 feed
                break
            body = bytes(self._buffer[body_start:total])
            del self._buffer[:total]
            try:
                messages.append(json.loads(body.decode(self._encoding)))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LspFrameError(f"正文不是合法 JSON：{exc}") from exc
        return messages
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_lsp_codec.py -v
```

预期：10 个用例全部 PASS。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add core/ide/lsp_codec.py tests/test_ide_lsp_codec.py
git commit -m "feat(ide): 新增 LSP Content-Length 帧编解码，容忍分片与粘包"
```

---

## Task 4: 假语言服务器测试夹具与单进程生命周期

**Files:**
- Create: `tests/fake_lsp_server.py`
- Create: `core/ide/lsp_manager.py`
- Test: `tests/test_ide_lsp_process.py`

**Interfaces:**
- Consumes: `core.ide.lsp_codec.encode_frame` / `FrameDecoder` / `LspFrameError`
- Produces:
  - `tests/fake_lsp_server.py`：`FAKE_SERVER_SOURCE: str`、`write_fake_server(tmp_path) -> str`、`make_fake_command(tmp_path, mode="normal") -> list[str]`、`fake_env(mode="normal") -> dict`
  - `core/ide/lsp_manager.py`：
    - `LspError(Exception)`、`LspStartError`、`LspHandshakeError`、`LspRequestError(code, message, data=None)`、`LspNotRunning`
    - `DEFAULT_CLIENT_CAPABILITIES: dict`
    - `path_to_uri(path: str) -> str`、`uri_to_path(uri: str) -> str`
    - `LspProcess(command, *, cwd, name="", initialization_options=None, client_capabilities=None, on_notification=None, on_exit=None, initialize_timeout=20.0, request_timeout=15.0, shutdown_timeout=5.0, env=None)`，方法 `start()` / `initialize(root_path) -> dict` / `request(method, params=None, *, timeout=None)` / `notify(method, params=None)` / `shutdown(*, grace=None)` / `kill()` / `is_alive() -> bool`，属性 `server_capabilities: dict` / `exit_code: int | None` / `pid` / `stderr_tail: list[str]`

- [ ] **Step 1: 写假语言服务器夹具**

创建 `tests/fake_lsp_server.py`：

```python
# -*- coding: utf-8 -*-
"""测试用的假语言服务器。

它只实现 LSP 的最小骨架（Content-Length 帧 + initialize / initialized /
shutdown / exit），再加若干 test/* 方法用来触发各种分支（超时、崩溃、
主动推送、拒绝退出、错误响应）。

刻意用手写最小实现而非真实语言服务器：单测必须不依赖用户机器上装了什么，
也不能因为某个语言服务器的行为变化而变红。
"""
import os
import sys

FAKE_SERVER_SOURCE = r'''
import json
import os
import sys


def send(payload):
    data = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode("ascii").partition(":")
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def main():
    mode = os.environ.get("FAKE_LSP_MODE", "normal")
    while True:
        msg = read_message()
        if msg is None:
            return 0
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg_id,
                  "result": {"capabilities": {"hoverProvider": True}}})
        elif method == "initialized":
            if mode == "exit-after-init":
                sys.exit(3)
        elif method == "shutdown":
            if mode == "ignore-shutdown":
                continue
            send({"jsonrpc": "2.0", "id": msg_id, "result": None})
        elif method == "exit":
            if mode == "ignore-shutdown":
                continue
            return 0
        elif method == "test/echo":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"echo": msg.get("params")}})
        elif method == "test/slow":
            continue
        elif method == "test/notify":
            send({"jsonrpc": "2.0", "method": "test/pushed", "params": {"n": 1}})
            send({"jsonrpc": "2.0", "id": msg_id, "result": "ok"})
        elif method == "test/publishDiagnostics":
            params = msg.get("params") or {}
            send({
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": params.get("uri", ""),
                    "diagnostics": [
                        {
                            "range": {"start": {"line": 0, "character": 0},
                                      "end": {"line": 0, "character": 1}},
                            "severity": 1,
                            "message": "undefined name x",
                            "source": "fake",
                        }
                    ],
                },
            })
            send({"jsonrpc": "2.0", "id": msg_id, "result": "ok"})
        elif method == "test/error":
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32602, "message": "bad params"}})
        elif method == "test/crash":
            sys.exit(9)
        else:
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32601, "message": "unknown method"}})


if __name__ == "__main__":
    sys.exit(main())
'''


def write_fake_server(tmp_path) -> str:
    path = os.path.join(str(tmp_path), "fake_lsp.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(FAKE_SERVER_SOURCE)
    return path


def make_fake_command(tmp_path, mode: str = "normal") -> list[str]:
    """返回可直接当 argv 用的命令；mode 通过环境变量传给子进程。"""
    return [sys.executable, "-u", write_fake_server(tmp_path)]


def fake_env(mode: str = "normal") -> dict:
    env = dict(os.environ)
    env["FAKE_LSP_MODE"] = mode
    return env
```

- [ ] **Step 2: 写失败的测试**

创建 `tests/test_ide_lsp_process.py`：

```python
# -*- coding: utf-8 -*-
"""LspProcess：握手、请求转发、通知回调、优雅退出与超时强杀。"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_lsp_server import fake_env, make_fake_command
from core.ide.lsp_manager import (
    LspProcess,
    LspRequestError,
    LspStartError,
)


def _spawn(tmp_path, mode="normal", **kwargs):
    proc = LspProcess(
        make_fake_command(tmp_path),
        cwd=str(tmp_path),
        name="fake",
        env=fake_env(mode),
        **kwargs,
    )
    proc.start()
    return proc


def test_initialize_returns_server_capabilities(tmp_path):
    proc = _spawn(tmp_path)
    try:
        result = proc.initialize(str(tmp_path))
        assert result["capabilities"]["hoverProvider"] is True
        assert proc.is_alive() is True
    finally:
        proc.shutdown()


def test_request_returns_result(tmp_path):
    proc = _spawn(tmp_path)
    try:
        assert proc.request("test/echo", {"v": 1}) == {"echo": {"v": 1}}
    finally:
        proc.shutdown()


def test_request_error_is_raised_with_code(tmp_path):
    proc = _spawn(tmp_path)
    try:
        with pytest.raises(LspRequestError) as exc:
            proc.request("test/error", {})
        assert exc.value.code == -32602
        assert exc.value.message == "bad params"
    finally:
        proc.shutdown()


def test_request_timeout_does_not_hang(tmp_path):
    proc = _spawn(tmp_path)
    try:
        started = time.time()
        with pytest.raises(LspRequestError) as exc:
            proc.request("test/slow", {}, timeout=0.5)
        assert "超时" in exc.value.message
        assert time.time() - started < 5
    finally:
        proc.shutdown()


def test_notification_reaches_callback(tmp_path):
    seen = []
    proc = _spawn(tmp_path, on_notification=lambda p, m, params: seen.append((m, params)))
    try:
        proc.request("test/notify", {})
        deadline = time.time() + 5
        while not seen and time.time() < deadline:
            time.sleep(0.02)
        assert seen[0][0] == "test/pushed"
        assert seen[0][1] == {"n": 1}
    finally:
        proc.shutdown()


def test_shutdown_is_graceful(tmp_path):
    proc = _spawn(tmp_path)
    proc.initialize(str(tmp_path))
    proc.shutdown()
    assert proc.is_alive() is False
    assert proc.exit_code == 0


def test_shutdown_kills_server_that_ignores_exit(tmp_path):
    proc = _spawn(tmp_path, mode="ignore-shutdown", shutdown_timeout=1.0)
    proc.initialize(str(tmp_path))
    started = time.time()
    proc.shutdown()
    assert proc.is_alive() is False
    # shutdown + exit 都无响应，必须在超时后强杀，总耗时不超过 grace*2 + 余量
    assert time.time() - started < 8


def test_exit_callback_receives_intentional_flag(tmp_path):
    exits = []
    proc = _spawn(tmp_path, on_exit=lambda p, code, intentional: exits.append((code, intentional)))
    proc.initialize(str(tmp_path))
    proc.shutdown()
    deadline = time.time() + 5
    while not exits and time.time() < deadline:
        time.sleep(0.02)
    assert exits == [(0, True)]


def test_start_failure_raises_lsp_start_error(tmp_path):
    proc = LspProcess(["definitely-not-a-real-binary-9f3a"], cwd=str(tmp_path), name="bad")
    with pytest.raises(LspStartError):
        proc.start()


def test_request_before_start_raises(tmp_path):
    proc = LspProcess(make_fake_command(tmp_path), cwd=str(tmp_path), name="fake")
    with pytest.raises(Exception):
        proc.request("test/echo", {})


def test_uri_roundtrip():
    from core.ide.lsp_manager import path_to_uri, uri_to_path

    path = os.path.join(str(os.path.abspath(os.sep)), "tmp", "子目录", "a.py")
    assert uri_to_path(path_to_uri(path)) == os.path.normpath(path)
```

- [ ] **Step 3: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_lsp_process.py -v
```

预期：FAIL，`ModuleNotFoundError: No module named 'core.ide.lsp_manager'`。

- [ ] **Step 4: 实现 LspProcess**

创建 `core/ide/lsp_manager.py`：

```python
# -*- coding: utf-8 -*-
"""LSP 语言服务器进程与工作区生命周期管理。

职责边界：
- 只管进程与 JSON-RPC，不懂任何具体语言（语言配置在 language_registry）；
- 可执行文件定位交给 server_provision，本模块只接收完整 argv；
- 所有要送到前端的通知都通过 on_notification 回调交给 gui_bridge，由后者
  走 emit_event 进 _js_queue，本模块内不与 WebView 通信；
- 读线程是唯一的 recv 方，写侧由进程内锁串行化：LSP 的 stdio 是单写者模型，
  两个线程同时写同一个 Content-Length 帧会让服务器解析错位。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

from core.ide.lsp_codec import FrameDecoder, LspFrameError, encode_frame

logger = logging.getLogger(__name__)


class LspError(Exception):
    """LSP 层错误基类，消息可直接展示给用户。"""


class LspStartError(LspError):
    """进程启动失败（可执行文件不存在 / 权限不足）。"""


class LspHandshakeError(LspError):
    """initialize 握手失败或超时。"""


class LspNotRunning(LspError):
    """服务器进程已退出，无法处理请求。"""


class LspRequestError(LspError):
    """服务器返回了错误响应，或请求超时。"""

    def __init__(self, code, message, data=None):
        super().__init__(f"[{code}] {message}")
        self.code = int(code)
        self.message = str(message)
        self.data = data


DEFAULT_CLIENT_CAPABILITIES = {
    "textDocument": {
        "synchronization": {"didSave": True, "dynamicRegistration": False},
        "publishDiagnostics": {"relatedInformation": True},
        "completion": {
            "completionItem": {
                "snippetSupport": False,
                "documentationFormat": ["markdown", "plaintext"],
            }
        },
        "hover": {"contentFormat": ["markdown", "plaintext"]},
        "definition": {"linkSupport": False},
        "formatting": {"dynamicRegistration": False},
        "rename": {"prepareSupport": False},
    },
    "workspace": {"workspaceFolders": True, "configuration": True},
    "window": {"workDoneProgress": False},
}

# 服务器反向请求里我们真正实现的少数几个，其余一律回 method not found
_ACK_ONLY_METHODS = frozenset({
    "window/workDoneProgress/create",
    "client/registerCapability",
    "client/unregisterCapability",
})


def path_to_uri(path: str) -> str:
    """本地路径转 file:// URI；Windows 盘符写成 file:///E:/... 形式。"""
    from pathlib import Path

    return Path(os.path.abspath(path)).as_uri()


def uri_to_path(uri: str) -> str:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(uri or "")
    if parsed.scheme != "file":
        return ""
    raw = unquote(parsed.path)
    if os.name == "nt" and len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return os.path.normpath(raw)


class LspProcess:
    """单个语言服务器进程：stdio + Content-Length 帧 + 请求表。"""

    def __init__(
        self,
        command,
        *,
        cwd,
        name="",
        initialization_options=None,
        client_capabilities=None,
        on_notification=None,
        on_exit=None,
        initialize_timeout=20.0,
        request_timeout=15.0,
        shutdown_timeout=5.0,
        env=None,
    ):
        self.command = [str(c) for c in command]
        self.cwd = cwd
        self.name = name or (os.path.basename(self.command[0]) if self.command else "lsp")
        self.initialization_options = dict(initialization_options or {})
        self.client_capabilities = client_capabilities or DEFAULT_CLIENT_CAPABILITIES
        self.initialize_timeout = initialize_timeout
        self.request_timeout = request_timeout
        self.shutdown_timeout = shutdown_timeout
        self._env = env
        self._on_notification = on_notification
        self._on_exit = on_exit

        self._proc: subprocess.Popen | None = None
        self._decoder = FrameDecoder()
        self._write_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._next_id = 1
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._closed = False
        self.server_capabilities: dict = {}
        self.exit_code: int | None = None
        self.stderr_tail: list[str] = []

    # ---------------- 生命周期 ----------------

    @property
    def pid(self):
        return self._proc.pid if self._proc is not None else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self._proc is not None:
            return
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            self._proc = subprocess.Popen(
                self.command,
                cwd=self.cwd or None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=flags,
                env=self._env,
            )
        except OSError as exc:
            raise LspStartError(f"无法启动语言服务器 {self.name}：{exc}") from exc
        self._reader = threading.Thread(
            target=self._read_loop, name=f"lsp-read-{self.name}", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._stderr_loop, name=f"lsp-stderr-{self.name}", daemon=True
        )
        self._stderr_reader.start()

    def initialize(self, root_path: str) -> dict:
        root_uri = path_to_uri(root_path)
        params = {
            "processId": os.getpid(),
            "clientInfo": {"name": "ycode-ide", "version": "1"},
            "rootUri": root_uri,
            "rootPath": root_path,
            "workspaceFolders": [
                {"uri": root_uri, "name": os.path.basename(root_path.rstrip("\\/")) or root_path}
            ],
            "capabilities": self.client_capabilities,
            "initializationOptions": self.initialization_options,
        }
        try:
            result = self.request("initialize", params, timeout=self.initialize_timeout)
        except LspError as exc:
            raise LspHandshakeError(f"语言服务器 {self.name} 握手失败：{exc}") from exc
        self.server_capabilities = (result or {}).get("capabilities", {}) or {}
        self.notify("initialized", {})
        return result or {}

    def shutdown(self, *, grace=None) -> None:
        """优雅退出：shutdown 请求 + exit 通知，超时强杀。"""
        grace = self.shutdown_timeout if grace is None else grace
        proc = self._proc
        if proc is None:
            return
        # 先立旗标：读线程据此区分「主动关闭」与「崩溃」，避免触发重启
        self._closed = True
        if proc.poll() is None:
            try:
                self.request("shutdown", {}, timeout=grace)
            except LspError:
                pass
            try:
                self.notify("exit", {})
            except LspError:
                pass
        if not self._wait(grace):
            self._kill()
        self._join_threads()

    def kill(self) -> None:
        self._closed = True
        self._kill()
        self._join_threads()

    # ---------------- JSON-RPC ----------------

    def request(self, method, params=None, *, timeout=None):
        if not self.is_alive():
            raise LspNotRunning(f"语言服务器 {self.name} 未运行")
        with self._id_lock:
            req_id = self._next_id
            self._next_id += 1
        entry = {"event": threading.Event(), "result": None}
        with self._pending_lock:
            self._pending[req_id] = entry
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        try:
            self._send(payload)
        except Exception:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise
        wait_time = self.request_timeout if timeout is None else timeout
        if not entry["event"].wait(wait_time):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise LspRequestError(-32001, f"请求 {method} 超时（{wait_time}s）")
        response = entry["result"] or {}
        if "error" in response:
            err = response["error"] or {}
            raise LspRequestError(
                err.get("code", -32603), err.get("message", "未知错误"), err.get("data")
            )
        return response.get("result")

    def notify(self, method, params=None) -> None:
        if not self.is_alive():
            raise LspNotRunning(f"语言服务器 {self.name} 未运行")
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _send(self, payload) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise LspNotRunning(f"语言服务器 {self.name} 未启动")
        data = encode_frame(payload)
        with self._write_lock:
            if proc.poll() is not None:
                raise LspNotRunning(f"语言服务器 {self.name} 已退出（exit={proc.returncode}）")
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise LspNotRunning(f"语言服务器 {self.name} 写入失败：{exc}") from exc

    # ---------------- 读线程 ----------------

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                try:
                    messages = self._decoder.feed(chunk)
                except LspFrameError as exc:
                    logger.warning("语言服务器 %s 帧解析失败：%s", self.name, exc)
                    break
                for msg in messages:
                    self._dispatch(msg)
        except (ValueError, OSError):
            pass
        finally:
            self._on_reader_end()

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self.stderr_tail.append(line)
                    del self.stderr_tail[:-20]
        except (ValueError, OSError):
            return

    def _dispatch(self, msg) -> None:
        if not isinstance(msg, dict):
            return
        msg_id = msg.get("id")
        if msg_id is not None and ("result" in msg or "error" in msg):
            with self._pending_lock:
                entry = self._pending.pop(msg_id, None)
            if entry is not None:
                entry["result"] = msg
                entry["event"].set()
            return
        method = msg.get("method")
        if not isinstance(method, str):
            return
        if msg_id is not None:
            self._handle_server_request(msg_id, method, msg.get("params") or {})
            return
        if self._on_notification is not None:
            try:
                self._on_notification(self, method, msg.get("params") or {})
            except Exception:
                # 通知处理出问题不允许打断读线程，否则整条连接会静默死掉
                logger.exception("语言服务器 %s 通知处理失败", self.name)

    def _handle_server_request(self, msg_id, method, params) -> None:
        if method == "workspace/configuration":
            items = params.get("items") or []
            self._send({"jsonrpc": "2.0", "id": msg_id, "result": [{} for _ in items]})
            return
        if method in _ACK_ONLY_METHODS:
            self._send({"jsonrpc": "2.0", "id": msg_id, "result": None})
            return
        self._send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not supported: {method}"},
        })

    def _on_reader_end(self) -> None:
        proc = self._proc
        self.exit_code = proc.returncode if proc is not None else None
        with self._pending_lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for entry in pendings:
            entry["result"] = {
                "error": {"code": -32002, "message": f"语言服务器 {self.name} 已退出"}
            }
            entry["event"].set()
        if self._on_exit is not None:
            try:
                self._on_exit(self, self.exit_code, self._closed)
            except Exception:
                logger.exception("LSP 退出回调失败")

    # ---------------- 进程收尾 ----------------

    def _wait(self, timeout) -> bool:
        proc = self._proc
        if proc is None:
            return True
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        self.exit_code = proc.returncode
        return True

    def _kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            logger.error("语言服务器 %s 强杀后仍未退出", self.name)
        self.exit_code = proc.returncode

    def _join_threads(self) -> None:
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2)
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_lsp_process.py -v
```

预期：11 个用例全部 PASS（`test_shutdown_kills_server_that_ignores_exit` 约 2 秒）。

- [ ] **Step 6: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add tests/fake_lsp_server.py core/ide/lsp_manager.py tests/test_ide_lsp_process.py
git commit -m "feat(ide): 新增 LSP 单进程封装（握手/请求/优雅退出/超时强杀）与假服务器夹具"
```

---

## Task 5: LSP 管理器（按工作区按需启动、崩溃限次重启、回收）

**Files:**
- Modify: `core/ide/lsp_manager.py`（追加 `LspManager`）
- Test: `tests/test_ide_lsp_manager.py`

**Interfaces:**
- Consumes: `LspProcess`、`core.ide.language_registry.ServerSpec`
- Produces:
  - `LspManager(*, provision=None, on_notification=None, on_status=None, max_restarts=None, restart_backoff=None, process_factory=None)`
    - `provision(server_id) -> dict` 默认取 `server_provision.resolve_executable`
    - `on_notification(workspace, language_id, method, params) -> None`
    - `on_status(status: dict) -> None`，status 键为 `server` / `language` / `workspace` / `state` / `msg`
  - `ensure_server(workspace: str, language_id: str) -> LspProcess`
  - `request(workspace, language_id, method, params=None, *, timeout=None)`
  - `notify(workspace, language_id, method, params=None) -> None`
  - `status(workspace: str | None = None) -> list[dict]`
  - `recycle_workspace(workspace: str) -> list[tuple[str, str]]`、`recycle_all() -> list[tuple[str, str]]`
  - 类属性 `MAX_RESTARTS = 3`、`RESTART_BACKOFF = 0.5`、`RESTART_RESET_SECONDS = 60.0`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_ide_lsp_manager.py`：

```python
# -*- coding: utf-8 -*-
"""LspManager：按工作区按需启动、通知转发、崩溃限次重启、回收。"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_lsp_server import make_fake_command
from core.ide.lsp_manager import LspManager, LspStartError


def _provision_for(command):
    def _resolve(server_id):
        return {
            "server": server_id,
            "executable": command[0],
            "extra_args": command[1:],
            "source": "path",
            "msg": "",
        }
    return _resolve


def _manager(tmp_path, **kwargs):
    kwargs.setdefault("restart_backoff", 0.0)
    kwargs.setdefault("provision", _provision_for(make_fake_command(tmp_path)))
    return LspManager(**kwargs)


def _wait_for(getter, predicate, timeout=10.0, interval=0.05):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = getter()
        if predicate(last):
            return last
        time.sleep(interval)
    return last


def test_ensure_server_starts_once_per_workspace_language(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    manager = _manager(tmp_path)
    try:
        first = manager.ensure_server(str(tmp_path), "python")
        second = manager.ensure_server(str(tmp_path), "python")
        assert first is second
        assert first.is_alive() is True
        assert first.server_capabilities == {"hoverProvider": True}
    finally:
        manager.recycle_all()


def test_request_and_notify_route_through_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    seen = []
    manager = _manager(tmp_path, on_notification=lambda ws, lang, m, p: seen.append((ws, lang, m, p)))
    try:
        assert manager.request(str(tmp_path), "python", "test/echo", {"v": 2}) == {"echo": {"v": 2}}
        manager.notify(str(tmp_path), "python", "test/notify", {})
        _wait_for(lambda: seen, lambda s: len(s) >= 1)
        assert seen[0][1] == "python"
        assert seen[0][2] == "test/pushed"
    finally:
        manager.recycle_all()


def test_status_reports_starting_then_running(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    states = []
    manager = _manager(tmp_path, on_status=lambda s: states.append(s["state"]))
    try:
        manager.ensure_server(str(tmp_path), "python")
        assert states[0] == "starting"
        assert states[-1] == "running"
        row = manager.status(str(tmp_path))[0]
        assert row["state"] == "running"
        assert row["server"] == "jedi"
        assert row["pid"] == manager.ensure_server(str(tmp_path), "python").pid
    finally:
        manager.recycle_all()


def test_missing_executable_raises_start_error(tmp_path, monkeypatch):
    manager = LspManager(
        provision=lambda server_id: {
            "server": server_id, "executable": "", "extra_args": [], "source": "missing",
            "msg": "未找到 jedi-language-server",
        },
        restart_backoff=0.0,
    )
    with pytest.raises(LspStartError):
        manager.ensure_server(str(tmp_path), "python")


def test_unknown_language_raises_start_error(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(LspStartError):
        manager.ensure_server(str(tmp_path), "cobol")


def test_two_workspaces_get_separate_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    manager = _manager(tmp_path)
    try:
        p1 = manager.ensure_server(str(ws1), "python")
        p2 = manager.ensure_server(str(ws2), "python")
        assert p1 is not p2
        assert p1.pid != p2.pid
        assert len(manager.status(None)) == 2
    finally:
        manager.recycle_all()


def test_recycle_workspace_only_touches_that_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    manager = _manager(tmp_path)
    p1 = manager.ensure_server(str(ws1), "python")
    p2 = manager.ensure_server(str(ws2), "python")
    manager.recycle_workspace(str(ws1))
    assert p1.is_alive() is False
    assert p2.is_alive() is True
    remaining = manager.status(None)
    assert len(remaining) == 1
    assert remaining[0]["workspace"] == os.path.normcase(os.path.abspath(str(ws2)))
    manager.recycle_all()
    assert p2.is_alive() is False
    assert manager.status(None) == []


def test_crash_restarts_up_to_limit_then_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "exit-after-init")
    manager = LspManager(
        provision=_provision_for(make_fake_command(tmp_path)),
        restart_backoff=0.0,
        max_restarts=2,
    )
    try:
        manager.ensure_server(str(tmp_path), "python")
        row = _wait_for(
            lambda: (manager.status(str(tmp_path)) or [{}])[0],
            lambda r: r.get("state") == "failed",
            timeout=15,
        )
        assert row["state"] == "failed"
        assert row["restarts"] == 2
        assert "重启" in row["msg"]
    finally:
        manager.recycle_all()


def test_crash_does_not_restart_after_intentional_shutdown(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    statuses = []
    manager = _manager(tmp_path, on_status=lambda s: statuses.append(s["state"]), max_restarts=3)
    manager.ensure_server(str(tmp_path), "python")
    manager.recycle_all()
    time.sleep(0.3)
    assert "restarting" not in statuses
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_lsp_manager.py -v
```

预期：FAIL，`ImportError: cannot import name 'LspManager'`。

- [ ] **Step 3: 实现 LspManager**

在 `core/ide/lsp_manager.py` 的 import 区补 `import os`（已有）与

```python
from core.ide import language_registry, server_provision
```

并在文件末尾追加：

```python
class LspManager:
    """按「工作区 + 语言」缓冲语言服务器进程。

    一个工作区的一种语言只跑一个进程；同一种语言在不同工作区各跑一个，
    因为 initialize 的 rootUri 不同，跨区复用会让跳转与诊断指向别的工作区。

    状态机：stopped -> starting -> running ->（崩溃）restarting -> running
    -> ... 连续崩溃超过 max_restarts 次后进入 failed 并停止自动重启。
    """

    MAX_RESTARTS = 3
    RESTART_BACKOFF = 0.5
    # 存活超过这个时长就认为服务器已经稳定，崩溃计数清零，
    # 避免「偶发崩溃 + 长间隔」把额度用光
    RESTART_RESET_SECONDS = 60.0

    def __init__(
        self,
        *,
        provision=None,
        on_notification=None,
        on_status=None,
        max_restarts=None,
        restart_backoff=None,
        process_factory=None,
    ):
        self._provision = provision or server_provision.resolve_executable
        self._on_notification = on_notification
        self._on_status = on_status
        self.max_restarts = self.MAX_RESTARTS if max_restarts is None else max_restarts
        self.restart_backoff = self.RESTART_BACKOFF if restart_backoff is None else restart_backoff
        self._process_factory = process_factory or LspProcess
        self._lock = threading.RLock()
        self._slots: dict[tuple[str, str], dict] = {}

    # ---------------- 内部工具 ----------------

    @staticmethod
    def _norm(workspace) -> str:
        return os.path.normcase(os.path.abspath(workspace or ""))

    def _emit_status(self, workspace, language_id, server_id, state, msg="") -> None:
        if self._on_status is None:
            return
        self._on_status({
            "server": server_id,
            "language": language_id,
            "workspace": workspace,
            "state": state,
            "msg": msg,
        })

    def _make_notification_handler(self, workspace, language_id):
        def _handler(proc, method, params):
            if self._on_notification is not None:
                self._on_notification(workspace, language_id, method, params)
        return _handler

    def _make_exit_handler(self, key, workspace, language_id):
        def _handler(proc, exit_code, intentional):
            self._handle_exit(key, workspace, language_id, proc, exit_code, intentional)
        return _handler

    # ---------------- 启动 ----------------

    def ensure_server(self, workspace: str, language_id: str) -> LspProcess:
        key = (self._norm(workspace), language_id)
        with self._lock:
            slot = self._slots.get(key)
            if slot is not None and slot["proc"] is not None and slot["proc"].is_alive():
                return slot["proc"]
            server_id = language_registry.server_id_for_language(language_id)
            if not server_id:
                raise LspStartError(f"没有为语言 {language_id} 配置语言服务器")
            provision = self._provision(server_id)
            if not provision.get("executable"):
                raise LspStartError(
                    provision.get("msg") or f"未找到语言服务器 {server_id} 的可执行文件"
                )
            spec = language_registry.get_spec(server_id)
            if slot is None:
                slot = {
                    "proc": None,
                    "restarts": 0,
                    "state": "stopped",
                    "server_id": server_id,
                    "msg": "",
                    "started_at": 0.0,
                }
                self._slots[key] = slot
            slot["state"] = "starting"
            slot["msg"] = ""
            slot["started_at"] = time.time()
            self._emit_status(workspace, language_id, server_id, "starting")

            command = [provision["executable"]]
            command.extend(provision.get("extra_args") or [])
            command.extend(spec.args)
            proc = self._process_factory(
                command,
                cwd=workspace or None,
                name=server_id,
                initialization_options=dict(spec.initialization_options),
                on_notification=self._make_notification_handler(workspace, language_id),
                on_exit=self._make_exit_handler(key, workspace, language_id),
            )
            try:
                proc.start()
                proc.initialize(workspace or os.getcwd())
            except LspError as exc:
                slot["state"] = "failed"
                slot["msg"] = str(exc)
                try:
                    proc.kill()
                except Exception:
                    pass
                self._emit_status(workspace, language_id, server_id, "failed", str(exc))
                raise
            slot["proc"] = proc
            slot["state"] = "running"
            self._emit_status(workspace, language_id, server_id, "running")
            return proc

    # ---------------- 请求 / 通知 ----------------

    def request(self, workspace, language_id, method, params=None, *, timeout=None):
        return self.ensure_server(workspace, language_id).request(method, params, timeout=timeout)

    def notify(self, workspace, language_id, method, params=None) -> None:
        self.ensure_server(workspace, language_id).notify(method, params)

    # ---------------- 状态 ----------------

    def status(self, workspace=None) -> list[dict]:
        wanted = self._norm(workspace) if workspace is not None else None
        with self._lock:
            items = list(self._slots.items())
        rows = []
        for (ws, language_id), slot in items:
            if wanted is not None and ws != wanted:
                continue
            proc = slot["proc"]
            alive = bool(proc is not None and proc.is_alive())
            state = slot["state"]
            if state == "running" and not alive:
                state = "stopped"
            rows.append({
                "workspace": ws,
                "language": language_id,
                "server": slot["server_id"],
                "state": state,
                "pid": proc.pid if proc is not None else None,
                "restarts": slot["restarts"],
                "msg": slot["msg"],
            })
        return rows

    # ---------------- 崩溃处理 ----------------

    def _handle_exit(self, key, workspace, language_id, proc, exit_code, intentional) -> None:
        with self._lock:
            slot = self._slots.get(key)
            if slot is None or slot["proc"] is not proc:
                return
            slot["proc"] = None
            if intentional:
                slot["state"] = "stopped"
                self._emit_status(workspace, language_id, slot["server_id"], "stopped")
                return
            uptime = time.time() - (slot["started_at"] or 0.0)
            if uptime > self.RESTART_RESET_SECONDS:
                slot["restarts"] = 0
            if slot["restarts"] >= self.max_restarts:
                slot["state"] = "failed"
                slot["msg"] = (
                    f"语言服务器连续异常退出 {slot['restarts']} 次，已停止自动重启；"
                    f"退出码 {exit_code}，请检查设置页的语言服务器路径"
                )
                self._emit_status(
                    workspace, language_id, slot["server_id"], "failed", slot["msg"]
                )
                return
            slot["restarts"] += 1
            restarts = slot["restarts"]
            server_id = slot["server_id"]
            slot["state"] = "restarting"
        self._emit_status(workspace, language_id, server_id, "restarting", f"第 {restarts} 次重启")
        if self.restart_backoff:
            time.sleep(self.restart_backoff)
        try:
            self.ensure_server(workspace, language_id)
        except LspError as exc:
            logger.warning("语言服务器 %s 自动重启失败：%s", server_id, exc)

    # ---------------- 回收 ----------------

    def recycle_workspace(self, workspace) -> list[tuple[str, str]]:
        """回收某工作区的全部语言服务器进程，返回被回收的 (workspace, language) 列表。"""
        norm = self._norm(workspace)
        with self._lock:
            keys = [k for k in self._slots if k[0] == norm]
            slots = [(k, self._slots.pop(k)) for k in keys]
        recycled = []
        for (ws, language_id), slot in slots:
            proc = slot["proc"]
            if proc is not None:
                try:
                    proc.shutdown()
                except Exception:
                    logger.exception("关闭语言服务器失败")
                    try:
                        proc.kill()
                    except Exception:
                        pass
            recycled.append((ws, language_id))
            self._emit_status(ws, language_id, slot["server_id"], "stopped")
        return recycled

    def recycle_all(self) -> list[tuple[str, str]]:
        with self._lock:
            workspaces = sorted({k[0] for k in self._slots})
        recycled: list[tuple[str, str]] = []
        for ws in workspaces:
            recycled.extend(self.recycle_workspace(ws))
        return recycled
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_lsp_manager.py -v
```

预期：9 个用例全部 PASS（崩溃重启用例约 0.5 秒内收敛）。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add core/ide/lsp_manager.py tests/test_ide_lsp_manager.py
git commit -m "feat(ide): 新增 LSP 管理器（按工作区启动、崩溃限次重启、工作区回收）"
```

---

## Task 6: 诊断节流合批（200 ms）

**Files:**
- Create: `core/ide/diagnostics_batcher.py`
- Test: `tests/test_ide_diagnostics_batcher.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `DEFAULT_THROTTLE_MS = 200`
  - `DiagnosticsBatcher(*, interval_ms=None, on_flush=None, timer_factory=None)`
    - `push(path: str, diagnostics: list) -> None`
    - `flush() -> list[tuple[str, list]]`（立即冲刷，返回实际派发的批次）
    - `discard(path: str) -> None`（文档关闭时清掉缓存与去重指纹）
    - `interval_ms: int` 属性、`flush_count: int` 属性

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_ide_diagnostics_batcher.py`：

```python
# -*- coding: utf-8 -*-
"""publishDiagnostics 的节流合批语义。"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ide.diagnostics_batcher import DEFAULT_THROTTLE_MS, DiagnosticsBatcher


class FakeTimer:
    """可手动触发的定时器，让节流窗口在测试里完全确定。"""

    instances = []

    def __init__(self, interval, callback):
        self.interval = interval
        self.callback = callback
        self.daemon = False
        self.cancelled = False
        FakeTimer.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


def _diag(message):
    return [{
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
        "severity": 1,
        "message": message,
        "source": "fake",
    }]


def _batcher():
    FakeTimer.instances = []
    flushes = []
    batcher = DiagnosticsBatcher(
        on_flush=lambda path, diagnostics: flushes.append((path, diagnostics)),
        timer_factory=FakeTimer,
    )
    return batcher, flushes


def test_default_window_is_200ms():
    batcher, _ = _batcher()
    assert batcher.interval_ms == DEFAULT_THROTTLE_MS == 200
    batcher.push("E:\\ws\\a.py", _diag("x"))
    assert FakeTimer.instances[-1].interval == 0.2


def test_consecutive_pushes_are_coalesced_into_one_flush():
    batcher, flushes = _batcher()
    batcher.push("E:\\ws\\a.py", _diag("第一次"))
    batcher.push("E:\\ws\\a.py", _diag("第二次"))
    batcher.push("E:\\ws\\a.py", _diag("第三次"))
    assert flushes == []
    FakeTimer.instances[-1].fire()
    assert len(flushes) == 1
    assert flushes[0][1][0]["message"] == "第三次"


def test_unchanged_diagnostics_are_not_flushed_twice():
    batcher, flushes = _batcher()
    batcher.push("E:\\ws\\a.py", _diag("一样"))
    batcher.flush()
    assert len(flushes) == 1
    batcher.push("E:\\ws\\a.py", _diag("一样"))
    batcher.flush()
    # 内容没变就不该再打扰前端，否则连续击键会把 evaluate_js 队列打满
    assert len(flushes) == 1


def test_changed_diagnostics_are_flushed_again():
    batcher, flushes = _batcher()
    batcher.push("E:\\ws\\a.py", _diag("旧"))
    batcher.flush()
    batcher.push("E:\\ws\\a.py", _diag("新"))
    batcher.flush()
    assert [f[1][0]["message"] for f in flushes] == ["旧", "新"]


def test_empty_diagnostics_report_clears_previous():
    batcher, flushes = _batcher()
    batcher.push("E:\\ws\\a.py", _diag("有错"))
    batcher.flush()
    batcher.push("E:\\ws\\a.py", [])
    batcher.flush()
    assert flushes[-1][1] == []


def test_different_paths_are_batched_independently():
    batcher, flushes = _batcher()
    batcher.push("E:\\ws\\a.py", _diag("a"))
    batcher.push("E:\\ws\\b.py", _diag("b"))
    batcher.flush()
    assert sorted(path for path, _ in flushes) == ["E:\\ws\\a.py", "E:\\ws\\b.py"]


def test_discard_forgets_path_state():
    batcher, flushes = _batcher()
    batcher.push("E:\\ws\\a.py", _diag("一样"))
    batcher.flush()
    batcher.discard("E:\\ws\\a.py")
    batcher.push("E:\\ws\\a.py", _diag("一样"))
    batcher.flush()
    # 关闭再打开同一个文件，诊断必须原样再推一次
    assert len(flushes) == 2


def test_real_timer_fires_without_manual_flush():
    flushes = []
    batcher = DiagnosticsBatcher(
        interval_ms=10,
        on_flush=lambda path, diagnostics: flushes.append(path),
    )
    batcher.push("E:\\ws\\a.py", _diag("自动"))
    deadline = threading.Event()
    deadline.wait(2.0)
    assert flushes == ["E:\\ws\\a.py"]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_diagnostics_batcher.py -v
```

预期：FAIL，`ModuleNotFoundError: No module named 'core.ide.diagnostics_batcher'`。

- [ ] **Step 3: 实现节流合批**

创建 `core/ide/diagnostics_batcher.py`：

```python
# -*- coding: utf-8 -*-
"""publishDiagnostics 的节流合批。

为什么必须合批：`evaluate_js` 只能在 gui-js-dispatcher 单线程调用，而语言
服务器在用户连续输入时会按击键频率推送整份文件的诊断（大文件一次几千条），
逐条直推会把前端主线程打满（设计规格 §6.1 的性能红线）。

策略：按文件路径累积「最后一次」的完整诊断列表，窗口期（默认 200 ms）结束后
一次性回调。同时做内容指纹去重：内容与上次已派发结果相同就不再派发，
这样连续击键只会在语义变化时才打扰前端。
"""
from __future__ import annotations

import threading

DEFAULT_THROTTLE_MS = 200


def _fingerprint(diagnostics) -> tuple:
    items = []
    for item in diagnostics or []:
        if not isinstance(item, dict):
            items.append(("?",))
            continue
        rng = item.get("range") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or {}
        items.append((
            start.get("line"),
            start.get("character"),
            end.get("line"),
            end.get("character"),
            item.get("severity"),
            str(item.get("message") or ""),
            str(item.get("source") or ""),
            str(item.get("code") or ""),
        ))
    return tuple(sorted(items, key=repr))


class DiagnosticsBatcher:
    """按路径合批诊断，窗口期结束或显式 flush 时回调。"""

    def __init__(self, *, interval_ms=None, on_flush=None, timer_factory=None):
        self._interval_ms = DEFAULT_THROTTLE_MS if interval_ms is None else int(interval_ms)
        self._on_flush = on_flush
        self._timer_factory = timer_factory or threading.Timer
        self._lock = threading.Lock()
        self._pending: dict[str, list] = {}
        self._last_flushed: dict[str, tuple] = {}
        self._timer = None
        self.flush_count = 0

    @property
    def interval_ms(self) -> int:
        return self._interval_ms

    def push(self, path: str, diagnostics) -> None:
        with self._lock:
            self._pending[str(path)] = list(diagnostics or [])
            if self._timer is None:
                timer = self._timer_factory(self._interval_ms / 1000.0, self.flush)
                timer.daemon = True
                self._timer = timer
                timer.start()

    def flush(self) -> list:
        """立即冲刷待派发批次（窗口到点自动调用，测试与关闭流程也会显式调用）。"""
        with self._lock:
            pending = self._pending
            self._pending = {}
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        batches = []
        for path, diagnostics in pending.items():
            fingerprint = _fingerprint(diagnostics)
            if self._last_flushed.get(path) == fingerprint:
                continue
            self._last_flushed[path] = fingerprint
            batches.append((path, diagnostics))
        for path, diagnostics in batches:
            self.flush_count += 1
            if self._on_flush is not None:
                self._on_flush(path, diagnostics)
        return batches

    def discard(self, path: str) -> None:
        """文档关闭时丢弃该路径的待派发内容与去重指纹。"""
        key = str(path)
        with self._lock:
            self._pending.pop(key, None)
        self._last_flushed.pop(key, None)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_diagnostics_batcher.py -v
```

预期：8 个用例全部 PASS。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add core/ide/diagnostics_batcher.py tests/test_ide_diagnostics_batcher.py
git commit -m "feat(ide): 新增诊断节流合批（200ms 窗口 + 内容指纹去重）"
```

---

## Task 7: 桥方法接入与 LSP 事件派发

**Files:**
- Modify: `gui_bridge.py`（在 S1 的「IDE 模式：编辑器文件读写 / 布局 / 已打开文件变更」分区之后追加 LSP 分区）
- Test: `tests/test_gui_lsp_bridge.py`

**Interfaces:**
- Consumes: `core.ide.language_registry`、`core.ide.server_provision`、`core.ide.lsp_manager`（`LspManager` / `LspRequestError` / `LspError` / `uri_to_path`）、`core.ide.diagnostics_batcher`、S1 已存在的 `ApiBridge._ide_guard(path, action)` 与 `ApiBridge.emit_event(name, payload)`
- Produces（`ApiBridge` 公开方法）：
  - `lsp_request(path: str, method: str, params: dict=None) -> dict`，返回 `{"ok": True, "result": ...}` 或 `{"ok": False, "error": {"code": int, "message": str}, "msg": str}`
  - `lsp_notify(path: str, method: str, params: dict=None) -> dict`
  - `lsp_server_status(workspace: str="") -> dict`，返回 `{"ok": True, "workspace": str, "servers": [{server, language, state, executable, source, pid, msg}]}`
  - `lsp_set_server_path(server_id: str, path: str) -> dict`（`path` 为空串表示恢复自动探测）
  - 事件 `lsp_diagnostics`（载荷 `{"path": str, "diagnostics": [...]}`）、`lsp_status`（载荷 `{server, language, workspace, state, msg}`）

> 说明：`lsp_set_server_path` 是设计规格 §5.5 未列出的补充——设置页的「手动指定路径」必须有对应写入入口。设计规格需同步补记此项。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_gui_lsp_bridge.py`：

```python
# -*- coding: utf-8 -*-
"""IDE 语言服务桥方法：请求转发、路径策略、状态查询、诊断事件合批派发。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core.gui_path_policy as gpp
from fake_lsp_server import make_fake_command
from gui_bridge import ApiBridge
from core.ide.lsp_manager import LspManager, path_to_uri


def _manager(tmp_path):
    command = make_fake_command(tmp_path)

    def _resolve(server_id):
        return {
            "server": server_id,
            "executable": command[0],
            "extra_args": command[1:],
            "source": "path",
            "msg": "",
        }

    return LspManager(provision=_resolve, restart_backoff=0.0)


def _make_bridge(workdir, tmp_path, monkeypatch, manager=None):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))
    bridge = ApiBridge(mcp_manager=None)
    bridge._session_workdir = lambda session_name="": workdir  # type: ignore[method-assign]
    bridge._current_session = "test-session"
    if manager is not None:
        bridge._lsp_manager = manager
    return bridge


def _wait_for(getter, predicate, timeout=10.0, interval=0.02):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = getter()
        if predicate(last):
            return last
        time.sleep(interval)
    return last


def test_lsp_request_forwards_to_server(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, manager)
    try:
        res = bridge.lsp_request(str(f), "test/echo", {"v": 1})
        assert res["ok"] is True
        assert res["result"] == {"echo": {"v": 1}}
    finally:
        manager.recycle_all()


def test_lsp_request_rejects_file_without_language_server(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "notes.txt"
    f.write_text("hi\n", encoding="utf-8")
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, _manager(tmp_path))
    res = bridge.lsp_request(str(f), "test/echo", {})
    assert res["ok"] is False
    assert "语言服务器" in res["error"]["message"]


def test_lsp_request_maps_server_error_code(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, manager)
    try:
        res = bridge.lsp_request(str(f), "test/error", {})
        assert res["ok"] is False
        assert res["error"]["code"] == -32602
        assert res["error"]["message"] == "bad params"
    finally:
        manager.recycle_all()


def test_lsp_request_denied_for_credential_config(tmp_path, monkeypatch):
    ydata = tmp_path / "ydata"
    ydata.mkdir()
    f = ydata / "config.json"
    f.write_text('{"api_key": "secret"}', encoding="utf-8")
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(ydata))
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: True)
    bridge = _make_bridge("GLOBAL", tmp_path, monkeypatch, _manager(tmp_path))
    res = bridge.lsp_request(str(f), "test/echo", {})
    assert res["ok"] is False


def test_lsp_notify_starts_server_and_pushes_diagnostics(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, manager)
    events = []
    bridge.emit_event = lambda name, payload=None: events.append((name, payload))
    try:
        res = bridge.lsp_notify(str(f), "test/publishDiagnostics", {"uri": path_to_uri(str(f))})
        assert res["ok"] is True
        batcher = bridge._lsp_batcher_instance()
        _wait_for(lambda: batcher._pending, lambda p: len(p) >= 1)
        batcher.flush()
        diag_events = [e for e in events if e[0] == "lsp_diagnostics"]
        assert len(diag_events) == 1
        payload = diag_events[0][1]
        assert os.path.normcase(payload["path"]) == os.path.normcase(str(f))
        assert payload["diagnostics"][0]["message"] == "undefined name x"
    finally:
        manager.recycle_all()


def test_lsp_status_event_is_emitted_on_start(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, manager)
    events = []
    bridge.emit_event = lambda name, payload=None: events.append((name, payload))
    try:
        bridge.lsp_request(str(f), "test/echo", {})
        statuses = _wait_for(
            lambda: [e[1] for e in events if e[0] == "lsp_status"],
            lambda rows: any(r["state"] == "running" for r in rows),
        )
        assert any(r["state"] == "starting" for r in statuses)
        assert any(r["server"] == "jedi" for r in statuses)
    finally:
        manager.recycle_all()


def test_lsp_server_status_lists_every_configured_language(tmp_path, monkeypatch):
    from core.ide import server_provision

    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        server_provision,
        "resolve_executable",
        lambda server_id: {
            "server": server_id,
            "executable": "/fake/" + server_id,
            "extra_args": [],
            "source": "path",
            "msg": "",
        },
    )
    server_provision.invalidate_cache()
    ws = tmp_path / "ws"
    ws.mkdir()
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch)
    res = bridge.lsp_server_status()
    assert res["ok"] is True
    languages = [row["language"] for row in res["servers"]]
    assert "python" in languages
    assert "typescript" in languages
    assert all(row["executable"] for row in res["servers"])


def test_lsp_server_status_shows_missing_with_guidance(tmp_path, monkeypatch):
    from core.ide import server_provision

    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(server_provision, "invalidate_cache", lambda: None)
    monkeypatch.setattr(
        server_provision,
        "resolve_executable",
        lambda server_id: {
            "server": server_id,
            "executable": "",
            "extra_args": [],
            "source": "missing",
            "msg": "未找到 " + server_id,
        },
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch)
    res = bridge.lsp_server_status()
    assert res["ok"] is True
    assert all(row["state"] == "missing" for row in res["servers"])
    assert all("未找到" in row["msg"] for row in res["servers"])


def test_lsp_set_server_path_rejects_bad_path(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, _manager(tmp_path))
    res = bridge.lsp_set_server_path("jedi", str(tmp_path / "nope"))
    assert res["ok"] is False
    assert "路径" in res["msg"]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_gui_lsp_bridge.py -v
```

预期：FAIL，`AttributeError: 'ApiBridge' object has no attribute 'lsp_request'`。

- [ ] **Step 3: 实现桥方法与事件派发**

在 `gui_bridge.py` 的 IDE 分区之后追加：

```python
    # ------------------------------------------------------------------
    # IDE 模式 S2：语言服务（LSP）
    # ------------------------------------------------------------------
    # 单次 LSP 请求的等待上限：补全 / 悬停这类交互请求超过这个时间对用户
    # 已经没有意义，宁可让前端拿到失败去回落纯文本编辑
    _LSP_REQUEST_TIMEOUT = 15.0

    def _lsp_manager_instance(self):
        """惰性创建 LSP 管理器；两个回调都只做「进事件队列」这件事。"""
        manager = getattr(self, "_lsp_manager", None)
        if manager is None:
            from core.ide import lsp_manager

            manager = lsp_manager.LspManager(
                on_notification=self._lsp_on_notification,
                on_status=self._lsp_on_status,
            )
            self._lsp_manager = manager
        return manager

    def _lsp_batcher_instance(self):
        batcher = getattr(self, "_lsp_batcher", None)
        if batcher is None:
            from core.ide import diagnostics_batcher

            batcher = diagnostics_batcher.DiagnosticsBatcher(
                on_flush=self._lsp_flush_diagnostics
            )
            self._lsp_batcher = batcher
        return batcher

    def _lsp_on_status(self, status) -> None:
        """可能在 LSP 读线程 / 重启回调里被调用，只允许 emit_event。"""
        self.emit_event("lsp_status", status)

    def _lsp_on_notification(self, workspace: str, language_id: str, method: str, params) -> None:
        """语言服务器主动推送的入口，运行在该服务器的读线程上。"""
        if method != "textDocument/publishDiagnostics":
            return
        from core.ide.lsp_manager import uri_to_path

        params = params or {}
        uri = params.get("uri") or ""
        path = uri_to_path(uri) or uri
        self._lsp_batcher_instance().push(path, params.get("diagnostics") or [])

    def _lsp_flush_diagnostics(self, path: str, diagnostics) -> None:
        """由合批定时器线程调用，仍然只做 emit_event。"""
        self.emit_event("lsp_diagnostics", {"path": path, "diagnostics": diagnostics})

    def _lsp_workspace_guard(self) -> str:
        """工作区变化时回收全部语言服务器：进程与工作区绑定，不跨区复用。"""
        workspace = self._session_workdir(self._current_session) or ""
        if getattr(self, "_lsp_workspace", None) != workspace:
            if getattr(self, "_lsp_manager", None) is not None:
                try:
                    self._lsp_manager.recycle_all()
                except Exception:
                    logger.exception("切换工作区时回收语言服务器失败")
            self._lsp_workspace = workspace
        return workspace

    def lsp_request(self, path: str, method: str, params=None) -> Dict[str, Any]:
        from core.ide.lsp_manager import LspError, LspRequestError

        try:
            workspace = self._lsp_workspace_guard()
            real_path, deny = self._ide_guard(path, "preview")
            if deny:
                return {"ok": False, "error": {"code": -32003, "message": deny}, "msg": deny}
            from core.ide import language_registry

            language_id = language_registry.language_id_for_path(real_path)
            if not language_id:
                msg = "该文件类型未配置语言服务器，已降级为纯文本编辑"
                return {"ok": False, "error": {"code": -32000, "message": msg}, "msg": msg}
            manager = self._lsp_manager_instance()
            result = manager.request(
                workspace, language_id, method, params or {}, timeout=self._LSP_REQUEST_TIMEOUT
            )
            return {"ok": True, "result": result}
        except LspRequestError as e:
            return {"ok": False, "error": {"code": e.code, "message": e.message}, "msg": str(e)}
        except LspError as e:
            return {"ok": False, "error": {"code": -32001, "message": str(e)}, "msg": str(e)}
        except Exception as e:
            logger.exception("lsp_request 失败")
            return {"ok": False, "error": {"code": -32603, "message": str(e)}, "msg": str(e)}

    def lsp_notify(self, path: str, method: str, params=None) -> Dict[str, Any]:
        from core.ide.lsp_manager import LspError

        try:
            workspace = self._lsp_workspace_guard()
            real_path, deny = self._ide_guard(path, "preview")
            if deny:
                return {"ok": False, "error": {"code": -32003, "message": deny}, "msg": deny}
            from core.ide import language_registry

            language_id = language_registry.language_id_for_path(real_path)
            if not language_id:
                msg = "该文件类型未配置语言服务器，已降级为纯文本编辑"
                return {"ok": False, "error": {"code": -32000, "message": msg}, "msg": msg}
            manager = self._lsp_manager_instance()
            manager.notify(workspace, language_id, method, params or {})
            return {"ok": True}
        except LspError as e:
            return {"ok": False, "error": {"code": -32001, "message": str(e)}, "msg": str(e)}
        except Exception as e:
            logger.exception("lsp_notify 失败")
            return {"ok": False, "error": {"code": -32603, "message": str(e)}, "msg": str(e)}

    def lsp_server_status(self, workspace: str = "") -> Dict[str, Any]:
        try:
            from core.ide import language_registry, server_provision

            current = workspace or self._lsp_workspace_guard()
            manager = getattr(self, "_lsp_manager", None)
            live = {}
            if manager is not None:
                for row in manager.status(current):
                    live[row["language"]] = row
            servers = []
            for language_id, server_id in language_registry.all_language_servers():
                provision = server_provision.resolve_executable(server_id)
                row = live.get(language_id) or {}
                if provision["source"] == "missing":
                    state = "missing"
                else:
                    state = row.get("state", "stopped")
                servers.append({
                    "server": server_id,
                    "language": language_id,
                    "state": state,
                    "executable": provision["executable"],
                    "source": provision["source"],
                    "pid": row.get("pid"),
                    "msg": row.get("msg") or provision["msg"],
                })
            return {"ok": True, "workspace": current, "servers": servers}
        except Exception as e:
            logger.exception("lsp_server_status 失败")
            return {"ok": False, "servers": [], "msg": str(e)}

    def lsp_set_server_path(self, server_id: str, path: str) -> Dict[str, Any]:
        """设置页手动指定可执行文件路径；path 为空串表示恢复自动探测。"""
        try:
            from core.ide import server_provision

            if path:
                result = server_provision.save_override(server_id, path)
            else:
                result = server_provision.clear_override(server_id)
            # 可执行文件变了，旧进程必须换掉，否则用户改了路径却毫无效果
            if getattr(self, "_lsp_manager", None) is not None:
                self._lsp_manager.recycle_all()
            return {"ok": True, **result}
        except server_provision.ProvisionError as e:
            return {"ok": False, "msg": str(e)}
        except Exception as e:
            logger.exception("lsp_set_server_path 失败")
            return {"ok": False, "msg": str(e)}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_gui_lsp_bridge.py -v
```

预期：9 个用例全部 PASS。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add gui_bridge.py tests/test_gui_lsp_bridge.py
git commit -m "feat(ide): 桥接层新增 LSP 请求/通知/状态方法并派发诊断与状态事件"
```

---

## Task 8: 前端 LSP 客户端（请求、文档同步、事件分发）

**Files:**
- Modify: `frontend/src/types/pywebview.d.ts`
- Create: `frontend/src/editor/lsp.ts`
- Test: `frontend/src/editor/lsp.test.ts`

**Interfaces:**
- Consumes: Task 7 的桥方法
- Produces（`frontend/src/editor/lsp.ts`）：
  - 类型 `LspResponse<T>`、`LspServerStatus`、`LspPosition`、`LspRange`、`LspDiagnostic`、`LspLocation`、`LspCompletionItem`、`LspBridgeApi`
  - `CHANGE_DEBOUNCE_MS = 150`
  - `bridgeApi(): LspBridgeApi | null`、`normKey(path: string): string`、`fileUri(path: string): string`、`uriToPath(uri: string): string`
  - `openDocument(path, languageId, text): Promise<void>`、`changeDocument(path, text): void`、`closeDocument(path): Promise<void>`
  - `lspRequest<T>(path, method, params): Promise<T | null>`
  - `dispatchLspEvent(name: string, payload: any): void`、`diagnosticsFor(path): LspDiagnostic[]`
  - `onDiagnostics(fn): () => void`、`onServerStatus(fn): () => void`
  - `resetClientState(): void`

- [ ] **Step 1: 写失败的测试**

创建 `frontend/src/editor/lsp.test.ts`：

```ts
// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import {
  changeDocument,
  closeDocument,
  diagnosticsFor,
  dispatchLspEvent,
  fileUri,
  lspRequest,
  normKey,
  openDocument,
  resetClientState,
  uriToPath,
} from './lsp';

const DIAG = {
  range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } },
  severity: 1 as const,
  message: 'undefined name x',
  source: 'fake',
};

function installBridge(overrides: Record<string, unknown> = {}) {
  const api = {
    lsp_request: vi.fn(async () => ({ ok: true, result: { items: [{ label: 'x' }] } })),
    lsp_notify: vi.fn(async () => ({ ok: true })),
    lsp_server_status: vi.fn(async () => ({ ok: true, workspace: 'E:\\ws', servers: [] })),
    lsp_set_server_path: vi.fn(async () => ({ ok: true })),
    lsp_sync_workspace: vi.fn(async () => ({ ok: true, workspace: 'E:\\ws', recycled: [] })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

beforeEach(() => {
  vi.useFakeTimers();
  (window as any).pywebview = undefined;
  resetClientState();
});

afterEach(() => {
  vi.useRealTimers();
});

it('fileUri 与 uriToPath 往返一致（含中文目录）', () => {
  const path = 'E:\\proj\\子目录\\a.py';
  expect(uriToPath(fileUri(path))).toBe(path);
});

it('normKey 归一化斜杠与大小写，便于诊断按路径命中', () => {
  expect(normKey('E:/Proj/A.py')).toBe(normKey('E:\\proj\\a.py'));
});

it('openDocument 发送 didOpen 并登记文档', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  expect(api.lsp_notify).toHaveBeenCalledWith('E:\\p\\a.py', 'textDocument/didOpen', {
    textDocument: {
      uri: fileUri('E:\\p\\a.py'),
      languageId: 'python',
      version: 1,
      text: 'x = 1\n',
    },
  });
});

it('changeDocument 防抖：连续改动只发一次 didChange 且版本递增', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  changeDocument('E:\\p\\a.py', 'x = 2\n');
  changeDocument('E:\\p\\a.py', 'x = 3\n');
  expect(api.lsp_notify).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(150);
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
  const second = api.lsp_notify.mock.calls[1] as unknown[];
  expect(second[1]).toBe('textDocument/didChange');
  expect((second[2] as any).textDocument.version).toBe(2);
  expect((second[2] as any).contentChanges).toEqual([{ text: 'x = 3\n' }]);
});

it('未打开的文档收到 changeDocument 时被忽略', async () => {
  const api = installBridge();
  changeDocument('E:\\p\\ghost.py', 'x = 1\n');
  await vi.advanceTimersByTimeAsync(200);
  expect(api.lsp_notify).not.toHaveBeenCalled();
});

it('closeDocument 发送 didClose 并清掉本地诊断', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  dispatchLspEvent('lsp_diagnostics', { path: 'E:\\p\\a.py', diagnostics: [DIAG] });
  expect(diagnosticsFor('E:/p/a.py')).toHaveLength(1);
  await closeDocument('E:\\p\\a.py');
  expect(diagnosticsFor('E:\\p\\a.py')).toEqual([]);
  expect(api.lsp_notify).toHaveBeenLastCalledWith('E:\\p\\a.py', 'textDocument/didClose', {
    textDocument: { uri: fileUri('E:\\p\\a.py') },
  });
});

it('lspRequest 在桥返回失败时返回 null', async () => {
  installBridge({
    lsp_request: vi.fn(async () => ({ ok: false, error: { code: -32000, message: 'no server' } })),
  });
  expect(await lspRequest('E:\\p\\a.py', 'textDocument/hover', {})).toBeNull();
});

it('桥不可用时全部请求安全降级', async () => {
  expect(await lspRequest('E:\\p\\a.py', 'textDocument/hover', {})).toBeNull();
  await expect(openDocument('E:\\p\\a.py', 'python', 'x')).resolves.toBeUndefined();
  await expect(closeDocument('E:\\p\\a.py')).resolves.toBeUndefined();
});

it('dispatchLspEvent 只认识 lsp_diagnostics / lsp_status', () => {
  const seen: unknown[] = [];
  dispatchLspEvent('lsp_status', { server: 'jedi', language: 'python', state: 'running' });
  dispatchLspEvent('some_other_event', { x: 1 });
  expect(seen).toEqual([]);
  expect(diagnosticsFor('E:\\p\\a.py')).toEqual([]);
});

it('resetClientState 清掉文档与诊断，重新打开会再发 didOpen', async () => {
  const api = installBridge();
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  dispatchLspEvent('lsp_diagnostics', { path: 'E:\\p\\a.py', diagnostics: [DIAG] });
  resetClientState();
  expect(diagnosticsFor('E:\\p\\a.py')).toEqual([]);
  await openDocument('E:\\p\\a.py', 'python', 'x = 1\n');
  expect(api.lsp_notify).toHaveBeenCalledTimes(2);
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/editor/lsp.test.ts
```

预期：FAIL，`Failed to resolve import "./lsp"`。

- [ ] **Step 3: 实现客户端**

创建 `frontend/src/editor/lsp.ts`：

```ts
/**
 * LSP 前端客户端。
 *
 * 分工：本模块负责「怎么发请求、怎么同步文档、诊断怎么缓存、事件怎么分发」，
 * 并在 Task 9 / Task 10 里把结果映射成 CodeMirror 的补全、诊断、悬停、
 * 跳转、格式化、重命名。后端进程管理与事件派发全部在 Python 侧，前端不接触子进程。
 *
 * 路径键：诊断缓存与视图索引统一用 normKey() 归一化，因为后端把 LSP 的
 * file:// URI 还原成本地路径后，盘符大小写与斜杠方向可能与编辑器侧不完全一致。
 */

export interface LspError {
  code: number;
  message: string;
}

export interface LspResponse<T = unknown> {
  ok: boolean;
  result?: T;
  error?: LspError;
  msg?: string;
}

export interface LspServerStatus {
  server: string;
  language: string;
  state: 'missing' | 'stopped' | 'starting' | 'running' | 'restarting' | 'failed';
  executable?: string;
  source?: 'override' | 'bundled' | 'path' | 'missing';
  pid?: number | null;
  msg?: string;
}

export interface LspPosition {
  line: number;
  character: number;
}

export interface LspRange {
  start: LspPosition;
  end: LspPosition;
}

export interface LspDiagnostic {
  range: LspRange;
  severity?: 1 | 2 | 3 | 4;
  message: string;
  source?: string;
  code?: string | number;
}

export interface LspLocation {
  path: string;
  line: number;
  character: number;
}

export interface LspCompletionItem {
  label: string;
  kind?: number;
  detail?: string;
  documentation?: string | { value: string };
  insertText?: string;
}

export interface LspBridgeApi {
  lsp_request(path: string, method: string, params: Record<string, unknown>): Promise<LspResponse>;
  lsp_notify(path: string, method: string, params: Record<string, unknown>): Promise<LspResponse>;
  lsp_server_status(workspace?: string): Promise<{
    ok: boolean;
    workspace: string;
    servers: LspServerStatus[];
    msg?: string;
  }>;
  lsp_set_server_path(serverId: string, path: string): Promise<{ ok: boolean; msg?: string }>;
  lsp_sync_workspace(): Promise<{
    ok: boolean;
    workspace: string;
    recycled: string[];
    msg?: string;
  }>;
}

/** didChange 防抖窗口：比后端 200ms 诊断合批略小，保证一次停击键只发一次变更。 */
export const CHANGE_DEBOUNCE_MS = 150;

const openDocs = new Map<string, { languageId: string; version: number }>();
const pendingChanges = new Map<string, { text: string; timer: ReturnType<typeof setTimeout> }>();
const diagnosticsByPath = new Map<string, LspDiagnostic[]>();
const diagnosticListeners = new Set<(path: string, diagnostics: LspDiagnostic[]) => void>();
const statusListeners = new Set<(status: LspServerStatus) => void>();

export function bridgeApi(): LspBridgeApi | null {
  const api = (window as any).pywebview?.api;
  if (!api || typeof api.lsp_request !== 'function') return null;
  return api as LspBridgeApi;
}

export function normKey(path: string): string {
  return path.replace(/\//g, '\\').toLowerCase();
}

export function fileUri(path: string): string {
  const normalized = path.replace(/\\/g, '/');
  const withRoot = normalized.startsWith('/') ? normalized : `/${normalized}`;
  return `file://${encodeURI(withRoot)}`;
}

export function uriToPath(uri: string): string {
  if (!uri.startsWith('file://')) return '';
  let rest = uri.slice('file://'.length);
  if (/^\/[A-Za-z]:/.test(rest)) rest = rest.slice(1);
  return decodeURIComponent(rest).replace(/\//g, '\\');
}

export async function openDocument(path: string, languageId: string, text: string): Promise<void> {
  const api = bridgeApi();
  if (!api) return;
  if (openDocs.has(normKey(path))) {
    changeDocument(path, text);
    return;
  }
  openDocs.set(normKey(path), { languageId, version: 1 });
  await api.lsp_notify(path, 'textDocument/didOpen', {
    textDocument: { uri: fileUri(path), languageId, version: 1, text },
  });
}

export function changeDocument(path: string, text: string): void {
  const key = normKey(path);
  if (!openDocs.has(key)) return;
  const existing = pendingChanges.get(key);
  if (existing) clearTimeout(existing.timer);
  const timer = setTimeout(() => {
    void flushChange(key);
  }, CHANGE_DEBOUNCE_MS);
  pendingChanges.set(key, { text, timer });
}

async function flushChange(key: string): Promise<void> {
  const doc = openDocs.get(key);
  const pending = pendingChanges.get(key);
  if (!doc || !pending) return;
  pendingChanges.delete(key);
  doc.version += 1;
  const api = bridgeApi();
  if (!api) return;
  await api.lsp_notify(pendingPath(key), 'textDocument/didChange', {
    textDocument: { uri: fileUri(pendingPath(key)), version: doc.version },
    contentChanges: [{ text: pending.text }],
  });
}

/** normKey 会改写盘符大小写，回传后端时必须用原样路径，这里从登记表反查。 */
function pendingPath(key: string): string {
  return docPaths.get(key) ?? key;
}

const docPaths = new Map<string, string>();

export async function closeDocument(path: string): Promise<void> {
  const key = normKey(path);
  if (!openDocs.has(key)) return;
  const pending = pendingChanges.get(key);
  if (pending) {
    clearTimeout(pending.timer);
    pendingChanges.delete(key);
  }
  openDocs.delete(key);
  docPaths.delete(key);
  diagnosticsByPath.delete(key);
  const api = bridgeApi();
  if (!api) return;
  await api.lsp_notify(path, 'textDocument/didClose', {
    textDocument: { uri: fileUri(path) },
  });
}

export async function lspRequest<T = unknown>(
  path: string,
  method: string,
  params: Record<string, unknown>,
): Promise<T | null> {
  const api = bridgeApi();
  if (!api) return null;
  const res = await api.lsp_request(path, method, params);
  if (!res.ok) return null;
  return (res.result ?? null) as T | null;
}

export function onDiagnostics(
  fn: (path: string, diagnostics: LspDiagnostic[]) => void,
): () => void {
  diagnosticListeners.add(fn);
  return () => {
    diagnosticListeners.delete(fn);
  };
}

export function onServerStatus(fn: (status: LspServerStatus) => void): () => void {
  statusListeners.add(fn);
  return () => {
    statusListeners.delete(fn);
  };
}

export function diagnosticsFor(path: string): LspDiagnostic[] {
  return diagnosticsByPath.get(normKey(path)) ?? [];
}

/** 事件入口：由 App.vue 的桥事件分发处转发 lsp_diagnostics / lsp_status。 */
export function dispatchLspEvent(name: string, payload: any): void {
  if (name === 'lsp_diagnostics') {
    const path = String(payload?.path ?? '');
    if (!path) return;
    const diagnostics = (payload?.diagnostics ?? []) as LspDiagnostic[];
    diagnosticsByPath.set(normKey(path), diagnostics);
    diagnosticListeners.forEach((fn) => fn(path, diagnostics));
    return;
  }
  if (name === 'lsp_status') {
    const status = payload as LspServerStatus;
    if (!status || typeof status.server !== 'string') return;
    statusListeners.forEach((fn) => fn(status));
  }
}

/** 切换工作区 / 关闭标签页时清空本地状态，避免把旧工作区的诊断挂到新文件上。 */
export function resetClientState(): void {
  pendingChanges.forEach((entry) => clearTimeout(entry.timer));
  pendingChanges.clear();
  openDocs.clear();
  docPaths.clear();
  diagnosticsByPath.clear();
}
```

同时在 `openDocument` 里登记原样路径：把 `openDocs.set(normKey(path), { languageId, version: 1 });` 之后补一行

```ts
  docPaths.set(normKey(path), path);
```

并把 `export const CHANGE_DEBOUNCE_MS` 上方的模块级 map 声明补上 `docPaths`：

```ts
const docPaths = new Map<string, string>();
```

（`docPaths` 声明要放在 `pendingPath` 之前，避免函数声明提升带来的 `const` 暂时性死区问题。）

- [ ] **Step 4: 声明桥类型**

在 `frontend/src/types/pywebview.d.ts` 的 `interface PyWebViewAPI {` 中追加：

```ts
  // ---- IDE 模式 S2：语言服务 ----
  lsp_request(
    path: string,
    method: string,
    params: Record<string, unknown>,
  ): Promise<{ ok: boolean; result?: unknown; error?: { code: number; message: string }; msg?: string }>;
  lsp_notify(
    path: string,
    method: string,
    params: Record<string, unknown>,
  ): Promise<{ ok: boolean; error?: { code: number; message: string }; msg?: string }>;
  lsp_server_status(workspace?: string): Promise<{
    ok: boolean;
    workspace: string;
    servers: {
      server: string;
      language: string;
      state: 'missing' | 'stopped' | 'starting' | 'running' | 'restarting' | 'failed';
      executable: string;
      source: 'override' | 'bundled' | 'path' | 'missing';
      pid?: number | null;
      msg?: string;
    }[];
    msg?: string;
  }>;
  lsp_set_server_path(serverId: string, path: string): Promise<{ ok: boolean; msg?: string }>;
  lsp_sync_workspace(): Promise<{
    ok: boolean;
    workspace: string;
    recycled: string[];
    msg?: string;
  }>;
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/editor/lsp.test.ts
```

预期：PASS，11 个用例全绿。

- [ ] **Step 6: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add frontend/src/types/pywebview.d.ts frontend/src/editor/lsp.ts frontend/src/editor/lsp.test.ts
git commit -m "feat(ide): 新增前端 LSP 客户端（请求/文档同步/诊断缓存/事件分发）"
```

---

## Task 9: 接入补全与诊断（CodeMirror autocomplete + lint）

**Files:**
- Modify: `frontend/package.json`（新增 `@codemirror/autocomplete`、`@codemirror/lint`）
- Modify: `frontend/src/editor/setup.ts`（`createEditorState` 支持追加扩展）
- Modify: `frontend/src/editor/lsp.ts`（追加位置换算、映射函数、`lspExtensions`）
- Modify: `frontend/src/components/EditorPane.vue`（注入 LSP 扩展、同步文档）
- Modify: `frontend/src/App.vue`（转发 LSP 事件）
- Test: `frontend/src/editor/lspExtensions.test.ts`

**Interfaces:**
- Consumes: Task 8 的 `lspRequest`、`diagnosticsFor`、`normKey`、`fileUri`、`openDocument` / `changeDocument` / `closeDocument`、`dispatchLspEvent`
- Produces:
  - `lineOffsets(text: string): number[]`
  - `positionToOffset(offsets: number[], line: number, character: number, docLength: number): number`
  - `offsetToPosition(offsets: number[], offset: number): LspPosition`
  - `severityOf(severity?: number): 'hint' | 'info' | 'warning' | 'error'`
  - `completionType(kind?: number): string`
  - `toCompletions(items: LspCompletionItem[]): Completion[]`
  - `toDiagnostics(items: LspDiagnostic[], offsets: number[], docLength: number): Diagnostic[]`
  - `lspDiagnosticsFor(view: EditorView, path: string): Diagnostic[]`
  - `lspCompletionSource(path: string): (context: CompletionContext) => Promise<CompletionResult | null>`
  - `lspExtensions(options: { path: string; languageId: string }): Extension[]`（补全、lint、视图注册三件套）
  - `setup.ts`：`createEditorState(doc: string, languageId: string, extraExtensions: Extension[] = []): Promise<EditorState>`

- [ ] **Step 1: 安装依赖**

```bash
cd /d E:\fch\Y-code_llm\frontend
npm install @codemirror/autocomplete @codemirror/lint
```

预期：退出码 0，`package.json` 的 `dependencies` 新增两项。

- [ ] **Step 2: 写失败的测试**

创建 `frontend/src/editor/lspExtensions.test.ts`：

```ts
// @vitest-environment jsdom
import { expect, it } from 'vitest';

import { createEditorState, LANGUAGE_LOADERS } from './setup';
import {
  dispatchLspEvent,
  lineOffsets,
  lspExtensions,
  offsetToPosition,
  positionToOffset,
  resetClientState,
  severityOf,
  toCompletions,
  toDiagnostics,
} from './lsp';

it('行号与文档偏移互转', () => {
  const text = 'a\nbb\nccc';
  const offsets = lineOffsets(text);
  expect(offsets).toEqual([0, 2, 5]);
  expect(positionToOffset(offsets, 1, 1, text.length)).toBe(3);
  expect(offsetToPosition(offsets, 4)).toEqual({ line: 1, character: 2 });
});

it('越界的行号被夹到文档范围内，不抛异常', () => {
  const offsets = lineOffsets('a\n');
  expect(positionToOffset(offsets, 99, 0, 2)).toBe(2);
  expect(positionToOffset(offsets, 0, 999, 2)).toBe(2);
});

it('severity 映射：1 是 error，2 是 warning，3 是 info，4 是 hint', () => {
  expect(severityOf(1)).toBe('error');
  expect(severityOf(2)).toBe('warning');
  expect(severityOf(3)).toBe('info');
  expect(severityOf(4)).toBe('hint');
  expect(severityOf(undefined)).toBe('error');
});

it('toCompletions 把 LSP kind 映射成 CM6 类型并保留文档', () => {
  const items = toCompletions([
    { label: 'foo', kind: 3, detail: 'def foo()' },
    { label: 'Bar', kind: 7 },
    { label: 'baz', kind: 999 },
  ]);
  expect(items[0]).toMatchObject({ label: 'foo', type: 'function', detail: 'def foo()' });
  expect(items[1].type).toBe('class');
  expect(items[2].type).toBe('text');
});

it('toDiagnostics 把 LSP 范围换算成文档偏移', () => {
  const text = 'x = 1\nundefined_name\n';
  const offsets = lineOffsets(text);
  const diags = toDiagnostics(
    [
      {
        range: { start: { line: 1, character: 0 }, end: { line: 1, character: 5 } },
        severity: 1,
        message: 'undefined',
        source: 'jedi',
      },
    ],
    offsets,
    text.length,
  );
  expect(diags[0].from).toBe(offsets[1]);
  expect(diags[0].to).toBe(offsets[1] + 5);
  expect(diags[0].severity).toBe('error');
  expect(diags[0].source).toBe('jedi');
});

it('lspExtensions 返回补全、诊断与视图注册三个扩展', () => {
  resetClientState();
  const exts = lspExtensions({ path: 'E:\\p\\a.py', languageId: 'python' });
  expect(exts.length).toBe(3);
});

it('诊断事件不会在视图未登记时抛异常', () => {
  resetClientState();
  expect(() =>
    dispatchLspEvent('lsp_diagnostics', {
      path: 'E:\\p\\a.py',
      diagnostics: [
        {
          range: { start: { line: 0, character: 0 }, end: { line: 0, character: 1 } },
          severity: 1,
          message: 'm',
        },
      ],
    }),
  ).not.toThrow();
});

it('createEditorState 接受额外扩展且不破坏文档', async () => {
  resetClientState();
  const state = await createEditorState(
    'x = 1\n',
    'python',
    lspExtensions({ path: 'E:\\p\\a.py', languageId: 'python' }),
  );
  expect(state.doc.toString()).toBe('x = 1\n');
  // 语言扩展仍在：python 语言数据可查
  expect(state.languageData.length).toBeGreaterThan(0);
  expect(Object.keys(LANGUAGE_LOADERS)).toContain('python');
});
```

- [ ] **Step 3: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/editor/lspExtensions.test.ts
```

预期：FAIL，`does not provide an export named 'lspExtensions'`。

- [ ] **Step 4: 让 createEditorState 支持追加扩展**

在 `frontend/src/editor/setup.ts` 中，把 `createEditorState` 改为：

```ts
/** extraExtensions 用于注入 LSP 等按文件实例化的扩展（补全、lint、悬停）。 */
export async function createEditorState(
  doc: string,
  languageId: string,
  extraExtensions: Extension[] = [],
): Promise<EditorState> {
  return EditorState.create({
    doc,
    extensions: [...baseExtensions(), ...(await languageExtension(languageId)), ...extraExtensions],
  });
}
```

- [ ] **Step 5: 实现补全与诊断映射**

在 `frontend/src/editor/lsp.ts` 顶部 import 区追加：

```ts
import { autocompletion, type Completion, type CompletionContext, type CompletionResult } from '@codemirror/autocomplete';
import { forceLinting, linter, type Diagnostic } from '@codemirror/lint';
import type { Extension } from '@codemirror/state';
import { EditorView, ViewPlugin, type PluginValue } from '@codemirror/view';
```

在文件末尾追加：

```ts
// ---------------------------------------------------------------------------
// 位置换算：LSP 用「行 + 字符」定位，CodeMirror 用文档绝对偏移
// ---------------------------------------------------------------------------

export function lineOffsets(text: string): number[] {
  const offsets = [0];
  for (let i = 0; i < text.length; i += 1) {
    if (text.charCodeAt(i) === 10) offsets.push(i + 1);
  }
  return offsets;
}

export function positionToOffset(
  offsets: number[],
  line: number,
  character: number,
  docLength: number,
): number {
  if (line < 0 || line >= offsets.length) return docLength;
  return Math.min(docLength, offsets[line] + Math.max(0, character));
}

export function offsetToPosition(offsets: number[], offset: number): LspPosition {
  let lo = 0;
  let hi = offsets.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (offsets[mid] <= offset) lo = mid;
    else hi = mid - 1;
  }
  return { line: lo, character: Math.max(0, offset - offsets[lo]) };
}

export function severityOf(severity?: number): 'hint' | 'info' | 'warning' | 'error' {
  switch (severity) {
    case 2:
      return 'warning';
    case 3:
      return 'info';
    case 4:
      return 'hint';
    default:
      return 'error';
  }
}

export function completionType(kind?: number): string {
  switch (kind) {
    case 2:
      return 'method';
    case 3:
      return 'function';
    case 4:
    case 7:
      return 'class';
    case 5:
    case 10:
      return 'property';
    case 6:
      return 'variable';
    case 8:
      return 'interface';
    case 9:
      return 'namespace';
    case 14:
      return 'keyword';
    default:
      return 'text';
  }
}

function documentationText(doc: LspCompletionItem['documentation']): string | undefined {
  if (!doc) return undefined;
  if (typeof doc === 'string') return doc;
  return doc.value;
}

export function toCompletions(items: LspCompletionItem[]): Completion[] {
  return items.map((item) => ({
    label: item.label,
    type: completionType(item.kind),
    detail: item.detail,
    info: documentationText(item.documentation),
    apply: item.insertText ?? item.label,
  }));
}

export function toDiagnostics(
  items: LspDiagnostic[],
  offsets: number[],
  docLength: number,
): Diagnostic[] {
  return items.map((item) => ({
    from: positionToOffset(offsets, item.range.start.line, item.range.start.character, docLength),
    to: positionToOffset(offsets, item.range.end.line, item.range.end.character, docLength),
    severity: severityOf(item.severity),
    message: item.message,
    source: item.source,
  }));
}

export function lspDiagnosticsFor(view: EditorView, path: string): Diagnostic[] {
  const text = view.state.doc.toString();
  return toDiagnostics(diagnosticsFor(path), lineOffsets(text), text.length);
}

export function lspCompletionSource(
  path: string,
): (context: CompletionContext) => Promise<CompletionResult | null> {
  return async (context) => {
    const word = context.matchBefore(/[\w.$]+/);
    if (!word) return null;
    // 非显式触发时，光标前没有标识符字符就不要打扰语言服务器
    if (word.from === word.to && !context.explicit) return null;
    const text = context.state.doc.toString();
    const offsets = lineOffsets(text);
    const position = offsetToPosition(offsets, word.from);
    const result = await lspRequest<{ items?: LspCompletionItem[] }>(
      path,
      'textDocument/completion',
      {
        textDocument: { uri: fileUri(path) },
        position,
        context: { triggerKind: context.explicit ? 1 : 2 },
      },
    );
    const items = result?.items ?? [];
    if (!items.length) return null;
    return { from: word.from, options: toCompletions(items), validFor: /^[\w.$]*$/ };
  };
}

const viewsByPath = new Map<string, EditorView>();

function viewRegistry(path: string): Extension {
  const key = normKey(path);
  return ViewPlugin.define((view) => {
    viewsByPath.set(key, view);
    return {
      update() {
        /* 视图已登记，无需在每次更新时做额外工作 */
      },
      destroy() {
        if (viewsByPath.get(key) === view) viewsByPath.delete(key);
      },
    } as PluginValue;
  });
}

/**
 * 挂到编辑器上的 LSP 扩展：补全源、诊断 lint 源、视图注册。
 * 诊断到达时（dispatchLspEvent）会主动 forceLinting，不需要额外的轮询。
 */
export function lspExtensions(options: { path: string; languageId: string }): Extension[] {
  const { path } = options;
  return [
    autocompletion({ override: [lspCompletionSource(path)] }),
    linter((view) => lspDiagnosticsFor(view, path), { delay: 300 }),
    viewRegistry(path),
  ];
}
```

同时把 `dispatchLspEvent` 的 `lsp_diagnostics` 分支改为在缓存后主动触发 lint：

```ts
  if (name === 'lsp_diagnostics') {
    const path = String(payload?.path ?? '');
    if (!path) return;
    const key = normKey(path);
    const diagnostics = (payload?.diagnostics ?? []) as LspDiagnostic[];
    diagnosticsByPath.set(key, diagnostics);
    const view = viewsByPath.get(key);
    if (view) forceLinting(view);
    diagnosticListeners.forEach((fn) => fn(path, diagnostics));
    return;
  }
```

- [ ] **Step 6: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/editor/lspExtensions.test.ts src/editor/lsp.test.ts src/editor/setup.test.ts
```

预期：三个文件全部 PASS。

- [ ] **Step 7: 在 EditorPane 里接线**

`frontend/src/components/EditorPane.vue`（S1 计划中已给出完整实现）需要在三个位置接入 LSP：

1）import 区补：

```ts
import { changeDocument, closeDocument, lspExtensions, openDocument } from '../editor/lsp';
```

2）构造编辑器状态处（S1 里调用 `createEditorState(tab.content, languageId)` 的地方）改为：

```ts
const languageId = store.languageId(tab.path);
const state = await createEditorState(tab.content, languageId, lspExtensions({ path: tab.path, languageId }));
```

3）标签打开 / 激活后与关闭时同步文档：

```ts
void openDocument(tab.path, store.languageId(tab.path), tab.content);
```

```ts
void closeDocument(tab.path);
```

4）编辑器内容变化处（S1 把内容回写 ideStore 的 `updateListener` 里）追加一行：

```ts
changeDocument(tab.path, view.state.doc.toString());
```

- [ ] **Step 8: 在 App.vue 转发 LSP 事件**

在 `frontend/src/App.vue` 处理桥事件的既有位置（S1 新增 `ide_file_changed` 分支的同一处分发逻辑里）新增两个分支：

```ts
if (name === 'lsp_diagnostics' || name === 'lsp_status') {
  dispatchLspEvent(name, payload);
  return;
}
```

并在 `<script setup>` 的 import 区补 `import { dispatchLspEvent } from './editor/lsp';`。

- [ ] **Step 9: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add frontend/package.json frontend/package-lock.json frontend/src/editor/setup.ts frontend/src/editor/lsp.ts frontend/src/editor/lspExtensions.test.ts frontend/src/components/EditorPane.vue frontend/src/App.vue
git commit -m "feat(ide): 语言服务接入 CodeMirror 补全与诊断（含事件转发）"
```

---

## Task 10: 接入悬停、跳转定义、格式化、重命名

**Files:**
- Modify: `frontend/src/editor/lsp.ts`
- Modify: `frontend/src/components/EditorPane.vue`（追加导航类扩展与跳转回调）
- Test: `frontend/src/editor/lspExtensions.test.ts`（追加用例）

**Interfaces:**
- Consumes: Task 8/9 的 `lspRequest`、`lineOffsets`、`offsetToPosition`、`positionToOffset`、`fileUri`、`uriToPath`
- Produces:
  - `toHoverText(hover: unknown): string`
  - `toLocations(result: unknown): LspLocation[]`
  - `toTextEdits(edits: unknown[], offsets: number[], docLength: number): { from: number; to: number; insert: string }[]`
  - `buildTooltipDom(text: string): HTMLElement`
  - `lspHover(path: string): Extension`
  - `lspDefinitionKeymap(path: string, onJump: (locations: LspLocation[]) => void): Extension`
  - `lspFormatKeymap(path: string): Extension`
  - `lspRenameKeymap(path: string, requestNewName: (oldName: string) => string | null): Extension`
  - `lspNavigationExtensions(options: { path: string; onJump: (locations: LspLocation[]) => void; requestNewName: (oldName: string) => string | null }): Extension[]`（4 个扩展）

- [ ] **Step 1: 写失败的测试**

在 `frontend/src/editor/lspExtensions.test.ts` 末尾追加：

```ts
import {
  lspNavigationExtensions,
  toHoverText,
  toLocations,
  toTextEdits,
  uriToPath,
} from './lsp';

it('toHoverText 支持字符串、MarkupContent 与数组三种形态', () => {
  expect(toHoverText({ contents: 'plain' })).toBe('plain');
  expect(toHoverText({ contents: { kind: 'markdown', value: '**bold**' } })).toBe('**bold**');
  expect(toHoverText({ contents: ['line1', { value: 'line2' }] })).toBe('line1\n\nline2');
  expect(toHoverText(null)).toBe('');
});

it('toLocations 同时支持 Location 与 LocationLink', () => {
  const plain = toLocations([
    { uri: fileUri('E:\\p\\a.py'), range: { start: { line: 3, character: 4 } } },
  ]);
  expect(plain).toEqual([{ path: 'E:\\p\\a.py', line: 3, character: 4 }]);

  const links = toLocations([
    {
      targetUri: fileUri('E:\\p\\b.py'),
      targetRange: { start: { line: 7, character: 0 } },
    },
  ]);
  expect(links).toEqual([{ path: 'E:\\p\\b.py', line: 7, character: 0 }]);

  expect(toLocations(null)).toEqual([]);
  expect(toLocations([{ uri: 'http://example.com', range: { start: { line: 0, character: 0 } } }])).toEqual([]);
});

it('toTextEdits 把 LSP 编辑范围换算成文档偏移', () => {
  const text = 'a\nbb\n';
  const offsets = lineOffsets(text);
  const edits = toTextEdits(
    [
      {
        range: { start: { line: 1, character: 0 }, end: { line: 1, character: 2 } },
        newText: 'BB',
      },
    ],
    offsets,
    text.length,
  );
  expect(edits).toEqual([{ from: 2, to: 4, insert: 'BB' }]);
  expect(toTextEdits(undefined as unknown as unknown[], offsets, text.length)).toEqual([]);
});

it('lspNavigationExtensions 返回悬停、跳转、格式化、重命名四个扩展', () => {
  resetClientState();
  const exts = lspNavigationExtensions({
    path: 'E:\\p\\a.py',
    onJump: () => undefined,
    requestNewName: () => null,
  });
  expect(exts.length).toBe(4);
});

it('uriToPath 对非 file URI 返回空串，避免把远端路径当本地文件打开', () => {
  expect(uriToPath('https://example.com/a.py')).toBe('');
});
```

注意：`fileUri`、`lineOffsets`、`resetClientState` 已在文件头部 import；如果重复 import 会编译报错，实现时把它们合进同一条 import 语句。

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/editor/lspExtensions.test.ts
```

预期：FAIL，`does not provide an export named 'lspNavigationExtensions'`。

- [ ] **Step 3: 实现导航类能力**

在 `frontend/src/editor/lsp.ts` 的 import 区补 `hoverTooltip` 与 `keymap`：

```ts
import { EditorView, ViewPlugin, hoverTooltip, keymap, type PluginValue } from '@codemirror/view';
```

（若第 5 步已经 import 了 `EditorView` / `ViewPlugin`，只需在这条语句上追加 `hoverTooltip, keymap`。）

在文件末尾追加：

```ts
// ---------------------------------------------------------------------------
// 悬停 / 跳转定义 / 格式化 / 重命名
// ---------------------------------------------------------------------------

export function toHoverText(hover: unknown): string {
  if (!hover || typeof hover !== 'object') return '';
  const contents = (hover as { contents?: unknown }).contents;
  if (typeof contents === 'string') return contents;
  if (Array.isArray(contents)) {
    return contents
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object' && typeof (item as { value?: unknown }).value === 'string') {
          return (item as { value: string }).value;
        }
        return '';
      })
      .filter((item) => item !== '')
      .join('\n\n');
  }
  if (contents && typeof contents === 'object') {
    const value = (contents as { value?: unknown }).value;
    if (typeof value === 'string') return value;
  }
  return '';
}

export function buildTooltipDom(text: string): HTMLElement {
  const dom = document.createElement('div');
  dom.className = 'ycode-lsp-hover';
  dom.textContent = text;
  return dom;
}

export function toLocations(result: unknown): LspLocation[] {
  if (!result) return [];
  const raw = Array.isArray(result) ? result : [result];
  const locations: LspLocation[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const row = item as Record<string, any>;
    if (row.targetUri) {
      locations.push({
        path: uriToPath(String(row.targetUri)),
        line: row.targetRange?.start?.line ?? 0,
        character: row.targetRange?.start?.character ?? 0,
      });
      continue;
    }
    const uri = row.uri ?? row.location?.uri;
    const range = row.range ?? row.location?.range;
    if (!uri || !range) continue;
    locations.push({
      path: uriToPath(String(uri)),
      line: range.start?.line ?? 0,
      character: range.start?.character ?? 0,
    });
  }
  return locations.filter((location) => location.path !== '');
}

export function toTextEdits(
  edits: unknown[],
  offsets: number[],
  docLength: number,
): { from: number; to: number; insert: string }[] {
  if (!Array.isArray(edits)) return [];
  return edits.map((raw) => {
    const edit = (raw ?? {}) as Record<string, any>;
    return {
      from: positionToOffset(offsets, edit.range?.start?.line ?? 0, edit.range?.start?.character ?? 0, docLength),
      to: positionToOffset(offsets, edit.range?.end?.line ?? 0, edit.range?.end?.character ?? 0, docLength),
      insert: String(edit.newText ?? ''),
    };
  });
}

export function lspHover(path: string): Extension {
  return hoverTooltip(async (view, pos) => {
    const text = view.state.doc.toString();
    const position = offsetToPosition(lineOffsets(text), pos);
    const hover = await lspRequest(path, 'textDocument/hover', {
      textDocument: { uri: fileUri(path) },
      position,
    });
    const hoverText = toHoverText(hover);
    if (!hoverText) return null;
    return {
      pos,
      create: () => ({ dom: buildTooltipDom(hoverText) }),
    };
  });
}

export function lspDefinitionKeymap(
  path: string,
  onJump: (locations: LspLocation[]) => void,
): Extension {
  return keymap.of([
    {
      key: 'F12',
      run: (view) => {
        void (async () => {
          const text = view.state.doc.toString();
          const cursor = view.state.selection.main.head;
          const result = await lspRequest(path, 'textDocument/definition', {
            textDocument: { uri: fileUri(path) },
            position: offsetToPosition(lineOffsets(text), cursor),
          });
          const locations = toLocations(result);
          if (locations.length) onJump(locations);
        })();
        return true;
      },
    },
  ]);
}

export function lspFormatKeymap(path: string): Extension {
  return keymap.of([
    {
      key: 'Shift-Alt-f',
      run: (view) => {
        void (async () => {
          const text = view.state.doc.toString();
          const result = await lspRequest<unknown[]>(path, 'textDocument/formatting', {
            textDocument: { uri: fileUri(path) },
            options: { tabSize: 4, insertSpaces: true },
          });
          const changes = toTextEdits(result ?? [], lineOffsets(text), text.length);
          if (changes.length) view.dispatch({ changes });
        })();
        return true;
      },
    },
  ]);
}

export function lspRenameKeymap(
  path: string,
  requestNewName: (oldName: string) => string | null,
): Extension {
  return keymap.of([
    {
      key: 'F2',
      run: (view) => {
        const text = view.state.doc.toString();
        const offsets = lineOffsets(text);
        const word = view.state.wordAt(view.state.selection.main.head);
        if (!word) return false;
        const oldName = text.slice(word.from, word.to);
        const newName = requestNewName(oldName);
        if (!newName) return true;
        void (async () => {
          const edit = await lspRequest<{ changes?: Record<string, unknown[]> }>(
            path,
            'textDocument/rename',
            {
              textDocument: { uri: fileUri(path) },
              position: offsetToPosition(offsets, word.from),
              newName,
            },
          );
          const changes = edit?.changes ?? {};
          const currentUri = fileUri(path);
          const rawEdits = (changes[currentUri] ?? []) as unknown[];
          const mapped = toTextEdits(rawEdits, offsets, text.length);
          if (mapped.length) view.dispatch({ changes: mapped });
          // 跨文件重命名只改当前文件；其余文件的编辑由用户手动保存后重新触发，
          // 避免在前端静默改写未打开的磁盘文件
        })();
        return true;
      },
    },
  ]);
}

export function lspNavigationExtensions(options: {
  path: string;
  onJump: (locations: LspLocation[]) => void;
  requestNewName: (oldName: string) => string | null;
}): Extension[] {
  const { path, onJump, requestNewName } = options;
  return [
    lspHover(path),
    lspDefinitionKeymap(path, onJump),
    lspFormatKeymap(path),
    lspRenameKeymap(path, requestNewName),
  ];
}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/editor/lspExtensions.test.ts
```

预期：PASS，12 个用例全绿。

- [ ] **Step 5: 在 EditorPane 挂上导航扩展**

把 Task 9 第 7 步里的状态构造改为：

```ts
const languageId = store.languageId(tab.path);
const state = await createEditorState(tab.content, languageId, [
  ...lspExtensions({ path: tab.path, languageId }),
  ...lspNavigationExtensions({
    path: tab.path,
    onJump: (locations) => {
      const first = locations[0];
      void store.openFile(first.path).then(() => {
        store.jumpTo(first.path, first.line, first.character);
      });
    },
    requestNewName: (oldName) => window.prompt('重命名为', oldName),
  }),
]);
```

`store.jumpTo(path, line, character)` 是 S1 计划 Task 8 冻结契约里的定位动作：它会打开（或激活）目标文件，并把 `pendingJump` 写入 ideStore；`EditorPane.vue` 的 watcher 负责把 `(line, character)` 换算成文档偏移后 `dispatch({ selection: { anchor }, scrollIntoView: true })` 并调用 `clearPendingJump()`。S2 只需调用 `store.jumpTo(...)`，不要自行操作编辑器视图。

- [ ] **Step 6: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add frontend/src/editor/lsp.ts frontend/src/editor/lspExtensions.test.ts frontend/src/components/EditorPane.vue
git commit -m "feat(ide): 接入悬停、跳转定义、格式化与重命名"
```

---

## Task 11: 设置页「编辑器与语言服务」分区

**Files:**
- Create: `frontend/src/components/EditorLanguageSettings.vue`
- Test: `frontend/src/components/EditorLanguageSettings.test.ts`
- Modify: 设置面板容器（渲染各设置分区的那个组件，通常是 `frontend/src/components/SettingsModal.vue`；用下面的命令定位）

**Interfaces:**
- Consumes: `lsp_server_status()` / `lsp_set_server_path(serverId, path)` 桥方法、`frontend/src/editor/lsp.ts` 的 `onServerStatus` 与 `LspServerStatus`
- Produces: `EditorLanguageSettings.vue`（无 props、无 emits，自取数据），DOM 上给出 `data-testid="pick-<server>"` 与 `data-testid="clear-<server>"` 两个操作入口

- [ ] **Step 1: 定位设置面板容器**

```bash
cd /d E:\fch\Y-code_llm\frontend
findstr /s /n /c:"设置" src\components\Settings*.vue
```

预期：输出设置面板容器的文件路径（承载「通用 / 外观 / 模型」等分区的那个组件）。记下它的路径，Step 4 用它。

- [ ] **Step 2: 写失败的测试**

创建 `frontend/src/components/EditorLanguageSettings.test.ts`：

```ts
// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, expect, it, vi } from 'vitest';

import EditorLanguageSettings from './EditorLanguageSettings.vue';
import { resetClientState } from '../editor/lsp';

const STATUS = {
  ok: true,
  workspace: 'E:\\ws',
  servers: [
    {
      server: 'jedi',
      language: 'python',
      state: 'running',
      executable: 'E:\\app\\lsp_bin\\jedi-language-server.exe',
      source: 'bundled',
    },
    {
      server: 'gopls',
      language: 'go',
      state: 'missing',
      executable: '',
      source: 'missing',
      msg: '未找到 gopls：请安装它并加入 PATH',
    },
  ],
};

function installApi(overrides: Record<string, unknown> = {}) {
  const api = {
    lsp_server_status: vi.fn(async () => STATUS),
    lsp_set_server_path: vi.fn(async () => ({ ok: true })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

beforeEach(() => {
  (window as any).pywebview = undefined;
  (window as any).prompt = vi.fn(() => null);
  resetClientState();
});

it('挂载后列出各语言服务器状态与路径', async () => {
  installApi();
  const wrapper = mount(EditorLanguageSettings);
  await flushPromises();
  const text = wrapper.text();
  expect(text).toContain('编辑器与语言服务');
  expect(text).toContain('jedi');
  expect(text).toContain('python');
  expect(text).toContain('运行中');
  expect(text).toContain('go');
  expect(text).toContain('未安装');
  expect(text).toContain('未找到 gopls');
});

it('手动指定路径后调用桥并刷新状态', async () => {
  const api = installApi();
  (window as any).prompt = vi.fn(() => 'E:\\tools\\gopls.exe');
  const wrapper = mount(EditorLanguageSettings);
  await flushPromises();
  await wrapper.find('[data-testid="pick-gopls"]').trigger('click');
  await flushPromises();
  expect(api.lsp_set_server_path).toHaveBeenCalledWith('gopls', 'E:\\tools\\gopls.exe');
  // 设置后必定重新拉一次状态，用户能看到生效结果
  expect(api.lsp_server_status).toHaveBeenCalledTimes(2);
});

it('用户取消输入时不调用桥', async () => {
  const api = installApi();
  (window as any).prompt = vi.fn(() => null);
  const wrapper = mount(EditorLanguageSettings);
  await flushPromises();
  await wrapper.find('[data-testid="pick-gopls"]').trigger('click');
  await flushPromises();
  expect(api.lsp_set_server_path).not.toHaveBeenCalled();
});

it('已被手动覆盖的服务器提供恢复默认入口', async () => {
  installApi({
    lsp_server_status: vi.fn(async () => ({
      ok: true,
      workspace: 'E:\\ws',
      servers: [
        {
          server: 'jedi',
          language: 'python',
          state: 'stopped',
          executable: 'E:\\custom\\jedi.exe',
          source: 'override',
        },
      ],
    })),
  });
  const wrapper = mount(EditorLanguageSettings);
  await flushPromises();
  await wrapper.find('[data-testid="clear-jedi"]').trigger('click');
  await flushPromises();
  expect((window as any).pywebview.api.lsp_set_server_path).toHaveBeenCalledWith('jedi', '');
}, 20000);

it('桥不可用时给出明确提示且不抛异常', async () => {
  const wrapper = mount(EditorLanguageSettings);
  await flushPromises();
  expect(wrapper.text()).toContain('桥未就绪');
});
```

- [ ] **Step 3: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/components/EditorLanguageSettings.test.ts
```

预期：FAIL，`Failed to resolve import "./EditorLanguageSettings.vue"`。

- [ ] **Step 4: 实现设置分区组件**

创建 `frontend/src/components/EditorLanguageSettings.vue`：

```vue
<template>
  <section class="editor-language-settings">
    <header class="editor-language-settings__header">
      <h3>编辑器与语言服务</h3>
      <button type="button" data-testid="refresh" @click="refresh">刷新状态</button>
    </header>
    <p class="editor-language-settings__hint">
      语言服务器是可执行文件，由安装包内置件或系统 PATH 提供；未找到的语言不影响编辑器基本编辑，可在此手动指定可执行文件路径。
    </p>
    <p v-if="errorMsg" class="editor-language-settings__error" data-testid="error">{{ errorMsg }}</p>
    <table class="editor-language-settings__table">
      <thead>
        <tr>
          <th>语言</th>
          <th>服务器</th>
          <th>状态</th>
          <th>可执行文件</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in servers" :key="row.language + ':' + row.server" :data-testid="'row-' + row.server">
          <td>{{ row.language }}</td>
          <td>{{ row.server }}</td>
          <td :data-state="row.state">{{ stateText(row.state) }}</td>
          <td class="editor-language-settings__path">
            <span>{{ row.executable || '未找到' }}</span>
            <span class="editor-language-settings__source">{{ sourceText(row.source) }}</span>
          </td>
          <td>
            <button type="button" :data-testid="'pick-' + row.server" @click="pick(row)">指定路径</button>
            <button
              v-if="row.source === 'override'"
              type="button"
              :data-testid="'clear-' + row.server"
              @click="restore(row)"
            >
              恢复默认
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-if="servers.length && servers[0].msg" class="editor-language-settings__msg">
      {{ servers[0].msg }}
    </p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';

import { onServerStatus, type LspServerStatus } from '../editor/lsp';

const servers = ref<LspServerStatus[]>([]);
const errorMsg = ref('');

const STATE_TEXT: Record<string, string> = {
  missing: '未安装',
  stopped: '未启动',
  starting: '启动中',
  running: '运行中',
  restarting: '重启中',
  failed: '不可用',
};

const SOURCE_TEXT: Record<string, string> = {
  override: '手动指定',
  bundled: '内置',
  path: 'PATH 探测',
  missing: '',
};

function stateText(state: string): string {
  return STATE_TEXT[state] ?? state;
}

function sourceText(source?: string): string {
  if (!source) return '';
  return SOURCE_TEXT[source] ?? '';
}

function api() {
  return (window as any).pywebview?.api;
}

async function refresh(): Promise<void> {
  const bridge = api();
  if (!bridge || typeof bridge.lsp_server_status !== 'function') {
    errorMsg.value = '桥未就绪，无法读取语言服务器状态';
    return;
  }
  const res = await bridge.lsp_server_status();
  if (!res?.ok) {
    errorMsg.value = res?.msg || '读取语言服务器状态失败';
    return;
  }
  errorMsg.value = '';
  servers.value = res.servers ?? [];
}

async function applyPath(server: string, path: string): Promise<void> {
  const bridge = api();
  if (!bridge || typeof bridge.lsp_set_server_path !== 'function') {
    errorMsg.value = '桥未就绪，无法设置语言服务器路径';
    return;
  }
  const res = await bridge.lsp_set_server_path(server, path);
  if (!res?.ok) {
    errorMsg.value = res?.msg || '设置失败';
  }
  await refresh();
}

function pick(row: LspServerStatus): void {
  const picked = window.prompt(
    `为 ${row.language} 指定 ${row.server} 可执行文件的完整路径`,
    row.executable || '',
  );
  if (picked === null) return;
  void applyPath(row.server, picked.trim());
}

function restore(row: LspServerStatus): void {
  void applyPath(row.server, '');
}

onMounted(() => {
  void refresh();
  onServerStatus(() => {
    void refresh();
  });
});
</script>

<style scoped>
.editor-language-settings {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.editor-language-settings__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.editor-language-settings__hint {
  color: var(--text-secondary, #888);
  font-size: 12px;
  line-height: 1.6;
}

.editor-language-settings__error {
  color: var(--danger, #d33);
}

.editor-language-settings__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.editor-language-settings__table th,
.editor-language-settings__table td {
  border-bottom: 1px solid var(--border, #333);
  padding: 4px 6px;
  text-align: left;
}

.editor-language-settings__path {
  word-break: break-all;
}

.editor-language-settings__source {
  margin-left: 6px;
  color: var(--text-secondary, #888);
}
</style>
```

然后 Step 1 定位到的设置面板容器里，把组件挂成「编辑器与语言服务」分区：在模板里各分区并列的位置插入一行

```html
<EditorLanguageSettings />
```

并在 `<script setup>` 的 import 区补

```ts
import EditorLanguageSettings from './EditorLanguageSettings.vue';
```

（若容器位于其它目录，import 路径按相对位置相应调整；组件本身与它的测试不依赖容器路径。）

- [ ] **Step 5: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/components/EditorLanguageSettings.test.ts
```

预期：PASS，5 个用例全绿。

- [ ] **Step 6: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add frontend/src/components/EditorLanguageSettings.vue frontend/src/components/EditorLanguageSettings.test.ts frontend/src/components/SettingsModal.vue
git commit -m "feat(ide): 设置页新增「编辑器与语言服务」分区（状态展示与手动路径）"
```

（如果你定位到的容器不是 `SettingsModal.vue`，把 `git add` 里的文件名换成实际路径。）

---

## Task 12: 切换工作区回收全部 LSP 进程

**Files:**
- Modify: `gui_bridge.py`（新增 `lsp_sync_workspace` 公开方法）
- Modify: `frontend/src/stores/ideStore.ts`（新增 `syncWorkspace()` 动作）
- Modify: `frontend/src/App.vue`（会话 / 工作区切换后调用 `syncWorkspace()`）
- Test: `tests/test_ide_lsp_workspace_recycle.py`
- Test: `frontend/src/editor/lsp.test.ts`（追加用例）

**Interfaces:**
- Consumes: Task 7 的 `_lsp_workspace_guard`、`LspManager.recycle_all`、Task 8 的 `resetClientState`
- Produces:
  - `ApiBridge.lsp_sync_workspace() -> dict`，返回 `{"ok": True, "workspace": str, "recycled": [str, ...]}`
  - `useIdeStore().syncWorkspace(): Promise<void>`

- [ ] **Step 1: 写失败的后端测试**

创建 `tests/test_ide_lsp_workspace_recycle.py`：

```python
# -*- coding: utf-8 -*-
"""切换工作区必须回收全部语言服务器进程，不得残留。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core.gui_path_policy as gpp
from fake_lsp_server import make_fake_command
from gui_bridge import ApiBridge
from core.ide.lsp_manager import LspManager


def _manager(tmp_path):
    command = make_fake_command(tmp_path)

    def _resolve(server_id):
        return {
            "server": server_id,
            "executable": command[0],
            "extra_args": command[1:],
            "source": "path",
            "msg": "",
        }

    return LspManager(provision=_resolve, restart_backoff=0.0)


def _make_bridge(workdir, tmp_path, monkeypatch, manager):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))
    bridge = ApiBridge(mcp_manager=None)
    bridge._session_workdir = lambda session_name="": workdir  # type: ignore[method-assign]
    bridge._current_session = "s1"
    bridge._lsp_manager = manager
    return bridge


def test_lsp_sync_workspace_kills_all_servers(tmp_path, monkeypatch):
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    f = ws1 / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws1), tmp_path, monkeypatch, manager)

    bridge.lsp_request(str(f), "test/echo", {})
    running = [row for row in manager.status(None) if row["state"] == "running"]
    assert len(running) == 1
    procs = [slot["proc"] for slot in manager._slots.values()]
    assert all(p is not None and p.is_alive() for p in procs)

    res = bridge.lsp_sync_workspace()
    assert res["ok"] is True
    assert res["workspace"] == str(ws1)
    assert manager.status(None) == []
    assert all(not p.is_alive() for p in procs)


def test_switching_session_workdir_recycles_on_next_request(tmp_path, monkeypatch):
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    f1 = ws1 / "a.py"
    f1.write_text("x = 1\n", encoding="utf-8")
    f2 = ws2 / "b.py"
    f2.write_text("y = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws1), tmp_path, monkeypatch, manager)

    bridge.lsp_request(str(f1), "test/echo", {})
    first = [slot["proc"] for slot in manager._slots.values()][0]

    # 用户切到另一个工作区：即使前端没主动调 lsp_sync_workspace，
    # 下一次请求也必须先把旧进程收掉
    monkeypatch.setattr(bridge, "_session_workdir", lambda session_name="": str(ws2))
    bridge.lsp_request(str(f2), "test/echo", {})
    assert first.is_alive() is False
    rows = manager.status(None)
    assert len(rows) == 1
    assert rows[0]["workspace"] == os.path.normcase(os.path.abspath(str(ws2)))
    bridge.lsp_sync_workspace()
    assert manager.status(None) == []


def test_lsp_sync_workspace_without_manager_is_safe(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, None)
    res = bridge.lsp_sync_workspace()
    assert res["ok"] is True
    assert res["recycled"] == []
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_lsp_workspace_recycle.py -v
```

预期：FAIL，`AttributeError: 'ApiBridge' object has no attribute 'lsp_sync_workspace'`。

- [ ] **Step 3: 实现后端回收方法**

在 `gui_bridge.py` 的 LSP 分区里追加：

```python
    def lsp_sync_workspace(self) -> Dict[str, Any]:
        """前端在会话 / 工作区切换后主动调用：旧工作区的语言服务器立刻退出。

        配套的兜底是 _lsp_workspace_guard：即使前端漏调，下一次 LSP 请求也会
        先回收旧工作区的进程，因此不存在「切了工作区还在跑旧服务器」的窗口。
        """
        try:
            workspace = self._session_workdir(self._current_session) or ""
            recycled = []
            manager = getattr(self, "_lsp_manager", None)
            if manager is not None:
                recycled = [language_id for _, language_id in manager.recycle_all()]
            batcher = getattr(self, "_lsp_batcher", None)
            if batcher is not None:
                batcher.flush()
            self._lsp_workspace = workspace
            return {"ok": True, "workspace": workspace, "recycled": recycled}
        except Exception as e:
            logger.exception("lsp_sync_workspace 失败")
            return {"ok": False, "workspace": "", "recycled": [], "msg": str(e)}
```

- [ ] **Step 4: 写失败的前端测试**

在 `frontend/src/editor/lsp.test.ts` 末尾追加：

```ts
import { useIdeStore } from '../stores/ideStore';
import { createPinia, setActivePinia } from 'pinia';

it('切换工作区时回收语言服务器并清空本地 LSP 状态', async () => {
  setActivePinia(createPinia());
  const api = installBridge({
    ide_read_file: vi.fn(async (path: string) => ({
      ok: true,
      path,
      content: 'x = 1\n',
      encoding: 'utf-8',
      bom: false,
      eol: 'lf',
      hash: 'h1',
    })),
    ide_set_open_files: vi.fn(async () => ({ ok: true, count: 1 })),
  });
  const store = useIdeStore();
  store.setSession('s1');
  await store.openFile('E:\\ws\\a.py');
  await openDocument('E:\\ws\\a.py', 'python', 'x = 1\n');
  dispatchLspEvent('lsp_diagnostics', { path: 'E:\\ws\\a.py', diagnostics: [DIAG] });
  expect(diagnosticsFor('E:\\ws\\a.py')).toHaveLength(1);

  await store.syncWorkspace();

  expect(api.lsp_sync_workspace).toHaveBeenCalledTimes(1);
  expect(diagnosticsFor('E:\\ws\\a.py')).toEqual([]);
});
```

- [ ] **Step 5: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/editor/lsp.test.ts
```

预期：FAIL，`store.syncWorkspace is not a function`。

- [ ] **Step 6: 实现前端回收动作**

在 `frontend/src/stores/ideStore.ts` 的 import 区补：

```ts
import { resetClientState } from '../editor/lsp';
```

并在 actions 里追加：

```ts
    /** 会话 / 工作区切换后调用：后端回收全部语言服务器，前端丢弃 LSP 缓存。 */
    async syncWorkspace() {
      resetClientState();
      const api = ideApi();
      if (!api || typeof api.lsp_sync_workspace !== 'function') return;
      await api.lsp_sync_workspace();
    },
```

在 `frontend/src/App.vue` 里，处理会话切换与工作区切换的既有位置（S1 切换后刷新文件树的那处）追加：

```ts
void ideStore.syncWorkspace();
```

- [ ] **Step 7: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_lsp_workspace_recycle.py -v
cd frontend
npx vitest run src/editor/lsp.test.ts src/stores/ideStore.test.ts
```

预期：后端 3 个用例 PASS；前端两个文件全部 PASS。

- [ ] **Step 8: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add gui_bridge.py tests/test_ide_lsp_workspace_recycle.py frontend/src/stores/ideStore.ts frontend/src/editor/lsp.test.ts frontend/src/App.vue
git commit -m "feat(ide): 切换工作区回收全部语言服务器并清理前端 LSP 状态"
```

---

## Task 13: 回归红线守卫

**Files:**
- Create: `tests/test_ide_lsp_regression.py`

**Interfaces:**
- Consumes: `core/ide/*.py` 的源码、`tests/test_prompt_byte_stability.py`
- Produces: 三个断言（AST 级禁用符号、依赖白名单、字节稳定测试实跑通过）

- [ ] **Step 1: 写守卫测试**

创建 `tests/test_ide_lsp_regression.py`：

```python
# -*- coding: utf-8 -*-
"""IDE 语言智能的回归红线守卫。

红线一（设计规格 §7.3）：IDE 模块不得触碰提示词构造与缓存前缀，
IDE 对模型请求路径零影响。

红线二（设计规格 §7.2）：IDE 模块不得直接操作 WebView 通道，
前端可见事件只能经 gui_bridge.emit_event 进 _js_queue 与 dispatcher 线程。

红线三（AGENTS.md 依赖条款）：core/ide 只允许标准库与项目内模块，
语言服务器一律是外部可执行文件，不引入 Python 第三方依赖。

用 AST 而不是关键字扫描：注释与文档字符串里必然会提到 evaluate_js /
prompt 这类词，字符串匹配会把正常的解释性注释判成违规。
"""
import ast
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDE_DIR = os.path.join(REPO_ROOT, "core", "ide")

# 项目内模块前缀：这些不算第三方依赖
PROJECT_MODULES = {"core", "config", "gui_bridge", "ui", "tools", "agents", "harness"}

FORBIDDEN_NAMES = {
    "build_stable_prompt",
    "build_prompt",
    "PROMPT_TEMPLATE",
    "_js_queue",
}

FORBIDDEN_ATTRS = {
    "evaluate_js",
}

STDLIB_NAMES = set(getattr(sys, "stdlib_module_names", ()))


def _ide_sources():
    names = sorted(n for n in os.listdir(IDE_DIR) if n.endswith(".py"))
    assert names, "core/ide 下没有任何模块，测试本身失去意义"
    for name in names:
        path = os.path.join(IDE_DIR, name)
        with open(path, "r", encoding="utf-8") as fh:
            yield path, fh.read()


def _iter_violations(source, filename):
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            yield (filename, node.lineno, f"引用了禁用符号 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            yield (filename, node.lineno, f"调用了禁用属性 {node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield from _module_violation(filename, node.lineno, node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield from _module_violation(filename, node.lineno, alias.name)


def _module_violation(filename, lineno, module):
    if module == "__future__":
        return
    root = module.split(".")[0]
    if root in PROJECT_MODULES or root in STDLIB_NAMES:
        return
    yield (filename, lineno, f"引入了非标准库依赖 {module}")


def test_ide_modules_do_not_touch_prompt_or_webview_channel():
    violations = []
    for path, source in _ide_sources():
        name = os.path.join("core", "ide", os.path.basename(path))
        violations.extend(_iter_violations(source, name))
    assert violations == [], "\n".join(f"{f}:{ln} {msg}" for f, ln, msg in violations)


def test_core_ide_only_imports_stdlib_and_project_modules():
    assert STDLIB_NAMES, "需要 Python 3.10+ 的 sys.stdlib_module_names 才能做依赖白名单检查"
    offenders = []
    for path, source in _ide_sources():
        filename = os.path.basename(path)
        for node in ast.walk(ast.parse(source, filename=filename)):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                modules = [node.module]
            for module in modules:
                if module == "__future__":
                    continue
                root = module.split(".")[0]
                if root in PROJECT_MODULES or root in STDLIB_NAMES:
                    continue
                offenders.append(f"core/ide/{filename}: {module}")
    assert offenders == []


def test_importing_core_ide_does_not_pull_prompt_modules():
    code = (
        "import sys\n"
        "import core.ide.language_registry\n"
        "import core.ide.server_provision\n"
        "import core.ide.lsp_codec\n"
        "import core.ide.lsp_manager\n"
        "import core.ide.diagnostics_batcher\n"
        "import core.ide.file_service\n"
        "import core.ide.layout_store\n"
        "bad = sorted(m for m in sys.modules if 'prompt' in m.lower())\n"
        "print('|'.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "", f"导入 core.ide 时加载了提示词模块：{result.stdout.strip()}"


def test_prompt_byte_stability_still_passes():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_prompt_byte_stability.py", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 2: 运行守卫测试**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_lsp_regression.py -v
```

预期：4 个用例全部 PASS。若 `test_ide_modules_do_not_touch_prompt_or_webview_channel` 失败，说明有 IDE 模块直接碰了 WebView 通道或提示词构造，按报错的 `文件:行号` 改为经 `emit_event` 派发。

- [ ] **Step 3: 跑一遍 S2 全量回归**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_language_registry.py tests/test_ide_server_provision.py tests/test_ide_lsp_codec.py tests/test_ide_lsp_process.py tests/test_ide_lsp_manager.py tests/test_ide_diagnostics_batcher.py tests/test_gui_lsp_bridge.py tests/test_ide_lsp_workspace_recycle.py tests/test_ide_lsp_regression.py tests/test_ide_file_service.py tests/test_ide_layout_store.py tests/test_gui_ide_bridge.py -v
```

预期：全部 PASS（S2 新增 60+ 个用例，S1 的 4 个文件作为回归守卫一并通过）。

- [ ] **Step 4: 前端全量回归**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/editor src/stores src/components/EditorLanguageSettings.test.ts
```

预期：全部 PASS。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add tests/test_ide_lsp_regression.py
git commit -m "test(ide): 新增语言智能回归守卫（提示词字节稳定/事件通道/依赖白名单）"
```

---

## Self-Review

**1. 规格覆盖（对照设计规格 §6.1 与 §10 S2 验收标准）**

| 规格要求 | 覆盖任务 |
|---|---|
| 每语言服务器一个子进程、stdio + Content-Length 帧 | Task 3、Task 4 |
| 生命周期绑定工作区：按需启动、关闭/切换时 shutdown + exit、超时强杀 | Task 5、Task 12 |
| 前端请求走桥的 request/response | Task 7、Task 8 |
| 服务器主动推送走事件管线 | Task 6、Task 7 |
| 诊断节流合批 200 ms | Task 6 |
| 补全前端防抖 | Task 8（`CHANGE_DEBOUNCE_MS=150` 的 didChange 防抖）与 Task 9（`linter` 300 ms / autocomplete 由 CM6 自带延迟触发） |
| 内置 jedi-language-server 覆盖 Python | Task 1、Task 2 |
| 探测 typescript-language-server / gopls / rust-analyzer / clangd / jdtls | Task 1、Task 2 |
| 设置页「编辑器与语言服务」分区（状态、探测结果、手动路径） | Task 11 |
| 语言服务器不触发 permission_broker、路径限定工作区 | Task 7（`_ide_guard(path, "preview")`） |
| 事件只经 `_js_queue` / `emit_event` | Task 7、Task 13 |
| 崩溃限次重启（3 次） | Task 5 |
| 验收 S2-1（Python 六项能力可用） | Task 8、Task 9、Task 10 |
| 验收 S2-2（连续输入不卡顿、诊断节流生效） | Task 6、Task 8、Task 9 |
| 验收 S2-3（切工作区无残留进程） | Task 5、Task 12 |
| 验收 S2-4（未安装服务器状态 + 手动指定入口，不影响基本编辑） | Task 2、Task 7、Task 11 |
| 回归红线（字节稳定、对请求路径零影响） | Task 13 |

**2. 占位符扫描**

全文无 TBD / TODO / 稍后补充 / 类似上文 / 「添加适当的错误处理」；每个代码步骤都给出可直接落盘的完整实现与可运行命令。

**3. 类型与命名一致性**

- `resolve_executable` 的 `extra_args` 从 Task 2 定义、Task 5 消费（`command = [executable, *extra_args, *spec.args]`），Task 7/12 的测试桩按同一形状提供。
- `diagnostics_batcher` 的 `push/flush/discard/interval_ms/flush_count` 在 Task 6 定义，Task 7 的桥与 Task 12 的 `lsp_sync_workspace` 使用同一组名字。
- LSP 事件名在 Task 7（后端 emit）、Task 8（前端 `dispatchLspEvent`）、Task 9（App.vue 转发）三处一致：`lsp_diagnostics`、`lsp_status`。
- `_ide_guard(path, action)` 沿用 S1 Task 7 的桥内部方法；`_lsp_workspace_guard` 是本计划新增，Task 7 定义、Task 12 复述不重复定义。
- 前端 `normKey` / `fileUri` / `uriToPath` 在 Task 8 定义，Task 9/10 复用；`lspExtensions`（3 个扩展）与 `lspNavigationExtensions`（4 个扩展）互不重叠，测试断言与之一致。

**4. 已知与设计规格的不一致（需回写规格）**

1. 桥方法新增 `lsp_set_server_path`：规格 §5.5 只列了 `lsp_request` / `lsp_notify` / `lsp_server_status`，但 §6.1 要求「设置页允许手动指定可执行文件路径」，必须有写入入口。
2. 桥方法新增 `lsp_sync_workspace`：规格对「切换工作区回收」只描述了行为，未定义触发方式；本计划同时提供前端显式调用与后端 `_lsp_workspace_guard` 兜底两条路径。
3. 新增文件 `core/ide/lsp_codec.py` 与 `core/ide/diagnostics_batcher.py`：规格 §4 的目录清单只列了 `language_registry` / `server_provision` / `lsp_manager`，本计划按「一个文件一个职责」把它们拆出来，便于单测分片与粘包。
4. 新增 `lsp_bin/` 目录约定：规格 §7.1 说「体积超阈值时改为首次启动解压到用户目录」，本计划把内置件目录固定为 `<安装根>/lsp_bin`，把体积决策留给打包阶段。
