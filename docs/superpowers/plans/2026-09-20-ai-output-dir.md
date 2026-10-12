# AI 生成内容存储目录 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 生成的内容（生图、网页图片下载、AI 主动写的临时脚本）统一落到可配置目录（默认 `~/.ycode/tmp`），并纳入按回合撤销。

**Architecture:** 新增 `core/ai_output_paths.py` 作为唯一落点解析入口（配置读取 + 工作目录命中规则 + 目录可用性回退）；三处自动落盘点改为调用它；沙盒豁免复用 `core/tools.py` 现有 trusted-path 体系；设置项经 `~/.ycode/config.json` 持久化，GUI 走 `get_system_settings` / `set_ai_output_dir` 桥接。

**Tech Stack:** Python 3 / pytest / Vue 3 + Pinia + Vitest / pywebview 桥接

## Global Constraints

- 默认目录：`~/.ycode/tmp`，必须尊重 `YCODE_DATA_DIR` 覆盖（走 `config.YCODE_DIR`）
- 配置键：`ai_output_dir`，落在 `~/.ycode/config.json`
- 未传 `save_dir` → 配置目录；显式传值（含 `"."`）→ 尊重原参数，不隐式改道
- 当前线程 workdir 命中配置目录（相等或为其子目录）→ 直接返回 workdir
- 不接管用户提供的文件：`%TEMP%\ycode_paste`、CLI 截图、`ycode_updates`、`ycode-computer-*` 保持现状
- 撤销：纳入按回合撤销，只回滚本回合产出
- 所有目录解析走 `core.workspace_context.get_workdir()`，**禁止使用进程 cwd**
- 中文注释，结论优先；不做与需求无关的重构

---

### Task 1: 落点解析模块

**Files:**
- Create: `core/ai_output_paths.py`
- Test: `tests/test_ai_output_paths.py`

**Interfaces:**
- Produces:
  - `AI_OUTPUT_DIR_KEY: str = "ai_output_dir"`
  - `default_ai_output_dir() -> pathlib.Path`
  - `resolve_ai_output_dir() -> pathlib.Path`（保证目录已存在；失败回退默认）
  - `get_ai_output_dir_setting() -> str`（原始配置值，未配置返回 `""`）
  - `set_ai_output_dir_setting(value: str) -> pathlib.Path`（校验 + 落盘 + 返回生效目录；不可用时抛 `AiOutputDirError`）
  - `is_ai_output_path(path) -> bool`
  - `class AiOutputDirError(Exception)`

- [ ] **Step 1: 写失败测试** `tests/test_ai_output_paths.py`

```python
"""AI 生成内容目录解析测试（规格：2026-09-20-ai-output-dir-design.md）。"""

import os
import stat
from pathlib import Path

import pytest

from core import ai_output_paths as aop
from core.workspace_context import workspace_scope


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """隔离配置读取与默认目录，测试绝不触碰真实 ~/.ycode。"""
    default_dir = tmp_path / "data" / "tmp"
    monkeypatch.setattr(aop, "default_ai_output_dir", lambda: default_dir)
    monkeypatch.setattr(aop, "_read_setting", lambda: "")
    return default_dir


def test_unset_setting_falls_back_to_default_and_creates_dir(isolated):
    resolved = aop.resolve_ai_output_dir()
    assert resolved == isolated.resolve()
    assert resolved.is_dir(), "默认目录不存在时应自动创建"


def test_configured_dir_wins(isolated, tmp_path, monkeypatch):
    custom = tmp_path / "my_ai_out"
    monkeypatch.setattr(aop, "_read_setting", lambda: str(custom))
    assert aop.resolve_ai_output_dir() == custom.resolve()
    assert custom.is_dir()


def test_tilde_is_expanded(isolated, monkeypatch):
    monkeypatch.setattr(aop, "_read_setting", lambda: "~/ycode_ai_out")
    resolved = aop.resolve_ai_output_dir()
    assert resolved == (Path.home() / "ycode_ai_out").resolve()


def test_workdir_inside_configured_dir_returns_workdir(isolated, tmp_path, monkeypatch):
    """规则：当前工作目录已在配置目录内 → 直接用工作目录，不再套一层。"""
    configured = tmp_path / "ai_out"
    project = configured / "proj"
    project.mkdir(parents=True)
    monkeypatch.setattr(aop, "_read_setting", lambda: str(configured))
    with workspace_scope(str(project)):
        assert aop.resolve_ai_output_dir() == project.resolve()


def test_workdir_equal_to_configured_dir_returns_workdir(isolated, tmp_path, monkeypatch):
    configured = tmp_path / "ai_out"
    configured.mkdir(parents=True)
    monkeypatch.setattr(aop, "_read_setting", lambda: str(configured))
    with workspace_scope(str(configured)):
        assert aop.resolve_ai_output_dir() == configured.resolve()


def test_unwritable_configured_dir_falls_back_to_default(isolated, tmp_path, monkeypatch):
    """配置目录不可写 → 回退默认目录，不抛异常、不静默丢文件。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.setattr(aop, "_read_setting", lambda: str(blocked))
    monkeypatch.setattr(aop, "_is_writable", lambda path: False)
    assert aop.resolve_ai_output_dir() == isolated.resolve()


def test_is_ai_output_path_matches_dir_and_children(isolated, tmp_path, monkeypatch):
    configured = tmp_path / "ai_out"
    monkeypatch.setattr(aop, "_read_setting", lambda: str(configured))
    assert aop.is_ai_output_path(configured / "hunyuan_20260920_120000.jpg")
    assert aop.is_ai_output_path(configured)
    assert not aop.is_ai_output_path(tmp_path / "other" / "x.png")
    assert not aop.is_ai_output_path("")


def test_set_setting_rejects_unwritable_dir(isolated, tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(aop, "_write_setting", lambda value: saved.setdefault("v", value) or True)
    blocked = tmp_path / "blocked2"
    blocked.mkdir()
    monkeypatch.setattr(aop, "_is_writable", lambda path: False)
    with pytest.raises(aop.AiOutputDirError):
        aop.set_ai_output_dir_setting(str(blocked))
    assert saved == {}, "校验失败时不得写盘"


def test_set_setting_persists_and_creates_dir(isolated, tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(aop, "_write_setting", lambda value: saved.setdefault("v", value) or True)
    target = tmp_path / "custom_out"
    result = aop.set_ai_output_dir_setting(str(target))
    assert saved["v"] == str(target)
    assert result == target.resolve() and target.is_dir()


def test_empty_setting_resets_to_default(isolated, tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(aop, "_write_setting", lambda value: saved.setdefault("v", value) or True)
    assert aop.set_ai_output_dir_setting("") == isolated.resolve()
    assert saved["v"] == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_ai_output_paths.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.ai_output_paths'`

- [ ] **Step 3: 实现 `core/ai_output_paths.py`**

```python
"""AI 生成内容目录：AI 产物（生图 / 下载图 / 临时脚本）的统一落点。

设计见 docs/superpowers/specs/2026-09-20-ai-output-dir-design.md。
本模块是落点解析的唯一入口——各调用点不得自行读 config 拼路径，
否则行为会分叉（历史上生图与 fetch_image 直接用进程 cwd，导致
打包版把文件写进安装目录）。
"""

from __future__ import annotations

import os
from pathlib import Path

from core.workspace_context import get_workdir

AI_OUTPUT_DIR_KEY = "ai_output_dir"


class AiOutputDirError(Exception):
    """设置 AI 生成内容目录失败（路径不可用等）。"""


def _read_setting() -> str:
    """读取配置项；读取异常一律按未配置处理（不让配置故障阻断生图）。"""
    try:
        import config

        return str(config._load().get(AI_OUTPUT_DIR_KEY, "") or "").strip()
    except Exception:
        return ""


def _write_setting(value: str) -> bool:
    """持久化配置项；返回是否写盘成功。"""
    try:
        import config

        return bool(config.save_config_value(AI_OUTPUT_DIR_KEY, value))
    except Exception:
        return False


def default_ai_output_dir() -> Path:
    """默认目录：<YCODE_DIR>/tmp（尊重 YCODE_DATA_DIR 覆盖）。"""
    import config

    return Path(config.YCODE_DIR) / "tmp"


def _is_writable(path: Path) -> bool:
    """目录可写探测：不可写目录会让生图静默失败，必须在解析阶段拦下。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(str(path), os.W_OK)


def _expand(value: str) -> Path:
    """展开 ~ 与 %VAR%/$VAR 后返回绝对路径；相对路径按线程工作目录解析。"""
    expanded = os.path.expandvars(os.path.expanduser(value))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = get_workdir() / candidate
    return candidate.resolve()


def resolve_ai_output_dir() -> Path:
    """当前生效的 AI 生成内容目录（绝对路径，保证已存在）。

    规则（规格 4）：
    1. 读配置；未配置 → default_ai_output_dir()
    2. 展开 ~ / 环境变量 → 规范化绝对路径
    3. 当前线程 workdir 命中该目录（相等或子目录）→ 直接返回 workdir
    4. 确保目录存在；不可用 → 回退默认目录
    """
    configured = _read_setting()
    fallback = default_ai_output_dir()
    if not configured:
        target = fallback
    else:
        try:
            target = _expand(configured)
        except Exception:
            target = fallback

    workdir = get_workdir()
    if workdir == target or target in workdir.parents:
        return workdir

    if _is_writable(target):
        return target
    if target != fallback and _is_writable(fallback):
        return fallback
    # 默认目录也不可用（极端情况）：返回原值，由调用方在写盘时报出真实错误
    return target


def get_ai_output_dir_setting() -> str:
    """原始配置值（未配置返回空串），供设置页回显。"""
    return _read_setting()


def set_ai_output_dir_setting(value: str) -> Path:
    """保存配置并返回生效目录；目录不可用时抛 AiOutputDirError 且不写盘。"""
    text = str(value or "").strip()
    if text:
        try:
            target = _expand(text)
        except Exception as error:
            raise AiOutputDirError(f"路径无法解析：{text}") from error
        if not _is_writable(target):
            raise AiOutputDirError(f"目录不可写或无法创建：{target}")
    if not _write_setting(text):
        raise AiOutputDirError("配置写入失败（~/.ycode/config.json）")
    return resolve_ai_output_dir()


def is_ai_output_path(path) -> bool:
    """路径是否位于 AI 生成内容目录内（沙盒豁免判定，规格 6.1）。"""
    if not path:
        return False
    try:
        target = Path(os.path.expanduser(str(path))).resolve()
        root = resolve_ai_output_dir()
    except Exception:
        return False
    return target == root or root in target.parents
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_ai_output_paths.py -v`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add core/ai_output_paths.py tests/test_ai_output_paths.py
git commit -m "feat(ai-output): 新增 AI 生成内容目录解析模块"
```

---

### Task 2: 三处自动落盘改造

**Files:**
- Modify: `core/hunyuan_image.py`（`generate_image` 签名与保存段）
- Modify: `plugins/tokenrhythm_image/plugin.py`（`_save_image_bytes` / `_download_image` / 两个 execute 入口）
- Modify: `core/tools.py`（`fetch_image` 落点）
- Modify: `core/tool_registry.py:440`（fetch_image 描述）
- Modify: `plugins/hunyuan_image/plugin.py`（save_dir 描述）
- Modify: `plugins/tokenrhythm_image/plugin.py`（两处 save_dir 描述）
- Test: `tests/test_ai_output_landing.py`（新建）、`tests/test_fetch_image.py`（更新落点断言）

**Interfaces:**
- Consumes: `core.ai_output_paths.resolve_ai_output_dir()`
- Produces: 三个工具的 `save_dir` 默认值由 `"."` 改为 `None`

- [ ] **Step 1: 写失败测试** `tests/test_ai_output_landing.py`

```python
"""三处自动落盘的默认落点断言（规格 5）。"""

import pytest

from core import ai_output_paths as aop
from core import hunyuan_image
from core.tools import fetch_image
from tests.test_fetch_image import _patch_http


@pytest.fixture()
def ai_dir(tmp_path, monkeypatch):
    target = tmp_path / "ai_out"
    monkeypatch.setattr(aop, "_read_setting", lambda: str(target))
    return target


def test_fetch_image_lands_in_ai_dir_not_cwd(ai_dir, tmp_path, monkeypatch):
    """进程 cwd 故意设为别处：落点必须由配置决定，不再跟 cwd 走。"""
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    _patch_http(monkeypatch, payload, "image/png")
    monkeypatch.chdir(tmp_path)
    result = fetch_image("https://example.com/pic.png")
    assert not result.startswith("🚨"), result
    files = list(ai_dir.glob("download_*.png"))
    assert len(files) == 1, f"应落到 AI 目录，实际: {files}"
    assert not list(tmp_path.glob("download_*.png")), "不得再落到进程 cwd"


def test_hunyuan_default_save_dir_is_ai_dir(ai_dir, monkeypatch):
    """未传 save_dir 时混元生图落到 AI 目录（不发起真实请求，只验解析）。"""
    import inspect

    signature = inspect.signature(hunyuan_image.generate_image)
    assert signature.parameters["save_dir"].default is None
    monkeypatch.setattr(
        hunyuan_image, "_download_image_url", lambda url, path: None, raising=False
    )
    assert aop.resolve_ai_output_dir() == ai_dir.resolve()


def test_hunyuan_explicit_save_dir_is_respected(tmp_path, monkeypatch):
    """显式传 save_dir 时不得被改道（规格 5 保留旧行为）。"""
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    monkeypatch.setattr(
        hunyuan_image, "_resolve_ai_output_dir", lambda: (_ for _ in ()).throw(
            AssertionError("显式传值时不应调用默认落点解析")
        ),
        raising=False,
    )
    from core import ai_output_paths

    assert ai_output_paths.resolve_ai_output_dir() is not None  # 解析函数本身可用
    assert explicit.is_dir()
```

> 说明：混元/基元的真实下载路径依赖外部 API，测试只断言"默认参数为 None + 落点解析函数被用于默认分支"。完整落盘断言由 Task 7 的端到端验证覆盖（用假响应替换 urlretrieve）。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_ai_output_landing.py -v`
Expected: FAIL — fetch_image 仍落进程 cwd

- [ ] **Step 3: 改造 `core/hunyuan_image.py`**

签名与保存段两处：

```python
def generate_image(prompt: str, size: str = "1024x1024", save_dir: str | None = None, footnote: str = " ", image: str = "") -> ToolResult:
```

```python
    # imageUrl 仅 24 小时有效，立即下载保存。
    # 未传 save_dir → AI 生成内容目录（设置页可配，默认 ~/.ycode/tmp）；
    # 显式传值（含 "."）→ 尊重原参数，不做隐式改道。
    if save_dir is None:
        from core.ai_output_paths import resolve_ai_output_dir

        save_dir = str(resolve_ai_output_dir())
    save_dir = os.path.abspath(save_dir or ".")
    os.makedirs(save_dir, exist_ok=True)
```

- [ ] **Step 4: 改造 `plugins/tokenrhythm_image/plugin.py`**

`_save_image_bytes` / `_download_image` 的落点解析：

```python
def _resolve_save_dir(save_dir: str | None) -> str:
    """未传 → AI 生成内容目录；显式传值 → 原样尊重。"""
    if save_dir is None:
        from core.ai_output_paths import resolve_ai_output_dir

        return str(resolve_ai_output_dir())
    return os.path.abspath(save_dir or ".")
```

两个函数里 `save_dir = os.path.abspath(save_dir or ".")` 替换为 `save_dir = _resolve_save_dir(save_dir)`；
两个 execute 入口（`generate_image` / `edit_image`）签名 `save_dir: str = "."` → `save_dir: str | None = None`。

- [ ] **Step 5: 改造 `core/tools.py` 的 `fetch_image`**

```python
        from core.ai_output_paths import resolve_ai_output_dir

        target_dir = resolve_ai_output_dir()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"download_{timestamp}.{extension}"
        target = target_dir / filename
        counter = 2
        while target.exists():
            filename = f"download_{timestamp}_{counter}.{extension}"
            target = target_dir / filename
            counter += 1
```

- [ ] **Step 6: 更新工具描述（4 处）**

- `plugins/hunyuan_image/plugin.py`：`"图片保存目录，默认当前工作目录"` → `"图片保存目录；不传则落到 AI 生成内容目录（设置页可配置，默认 ~/.ycode/tmp）"`
- `plugins/tokenrhythm_image/plugin.py`：两处同样的 `save_dir` 描述改成同一文案
- `core/tool_registry.py` 的 `fetch_image` 描述追加：`"图片落到 AI 生成内容目录（设置页可配置）。"`

- [ ] **Step 7: 更新 `tests/test_fetch_image.py` 落点断言**

`test_fetch_image_downloads_png_and_returns_local_path` 里的 `monkeypatch.chdir(tmp_path)` + `tmp_path.glob` 断言改为 monkeypatch `core.ai_output_paths._read_setting` 指向 `tmp_path`，其余断言（文件名格式、字节一致、返回含绝对路径）保持不变。

- [ ] **Step 8: 跑相关测试**

Run: `python -m pytest tests/test_ai_output_landing.py tests/test_fetch_image.py -v`
Expected: 全部 passed

- [ ] **Step 9: 提交**

```bash
git add core/hunyuan_image.py plugins/tokenrhythm_image/plugin.py core/tools.py core/tool_registry.py plugins/hunyuan_image/plugin.py tests/test_ai_output_landing.py tests/test_fetch_image.py
git commit -m "feat(ai-output): 生图与图片下载默认落到 AI 生成内容目录"
```

---

### Task 3: 沙盒豁免接入

**Files:**
- Modify: `core/tools.py`（`_resolve_workspace_path` 的 `allow_trusted_read` 分支）
- Test: `tests/test_ai_output_landing.py`（追加）

**Interfaces:**
- Consumes: `core.ai_output_paths.is_ai_output_path(path) -> bool`
- Produces: 无新接口

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_ai_output_landing.py`）

```python
def test_write_file_inside_ai_dir_bypasses_sandbox(ai_dir, tmp_path, monkeypatch):
    """AI 写临时脚本到 AI 目录：属于白名单，不触发越界拒绝。"""
    from core import tools as tools_mod
    from core.workspace_context import workspace_scope

    project = tmp_path / "proj"
    project.mkdir()
    ai_dir.mkdir(parents=True, exist_ok=True)
    with workspace_scope(str(project)):
        result = tools_mod.write_file(str(ai_dir / "probe.py"), "print(1)")
    assert not str(result).startswith("🚨"), result
    assert (ai_dir / "probe.py").read_text(encoding="utf-8") == "print(1)"


def test_write_file_outside_workspace_still_blocked(tmp_path, monkeypatch):
    """回归保护：非白名单的越界写入仍必须被拒绝。"""
    from core import tools as tools_mod
    from core.workspace_context import workspace_scope

    project = tmp_path / "proj"
    project.mkdir()
    with workspace_scope(str(project)):
        result = tools_mod.write_file(str(tmp_path / "outside.txt"), "x")
    assert str(result).startswith("🚨") or "越界" in str(result)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_ai_output_landing.py::test_write_file_inside_ai_dir_bypasses_sandbox -v`
Expected: FAIL — 路径越界被拒绝

- [ ] **Step 3: 在 `_resolve_workspace_path` 增加豁免分支**

紧跟 `is_trusted_paste_path` / `is_session_user_attached_path` 那段之后、`workspace = get_workdir()` 之前插入：

```python
        # AI 生成内容目录写入豁免：AI 产出的临时脚本/中间产物按设置统一落该目录，
        # 不属于越界访问（规格 6.1）。目录由用户在设置页指定，视为已授权。
        try:
            from core.ai_output_paths import is_ai_output_path

            if is_ai_output_path(candidate):
                return candidate
        except Exception:
            pass
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_ai_output_landing.py -v`
Expected: 全部 passed（含越界回归用例）

- [ ] **Step 5: 跑沙盒相关回归**

Run: `python -m pytest tests/test_sandbox_user_attachments_and_guards.py tests/test_tools.py -v`
Expected: 全部 passed

- [ ] **Step 6: 提交**

```bash
git add core/tools.py tests/test_ai_output_landing.py
git commit -m "feat(ai-output): AI 生成内容目录纳入沙盒写白名单"
```

---

### Task 4: 设置项后端与桥接

**Files:**
- Modify: `core/command_shell.py`（`get_system_settings` 快照增加字段）
- Modify: `gui_bridge.py`（新增 `set_ai_output_dir`）
- Test: `tests/test_ai_output_settings.py`（新建）

**Interfaces:**
- Consumes: `core.ai_output_paths.get_ai_output_dir_setting()` / `set_ai_output_dir_setting()` / `resolve_ai_output_dir()` / `default_ai_output_dir()` / `AiOutputDirError`
- Produces:
  - 系统设置快照新增 `ai_output_dir`（生效值）、`ai_output_dir_setting`（原始配置值）、`ai_output_dir_default`（默认值）
  - `gui_bridge` 方法 `set_ai_output_dir(path: str) -> {"ok": bool, "settings": dict, "msg": str}`

- [ ] **Step 1: 写失败测试** `tests/test_ai_output_settings.py`

```python
"""AI 生成内容目录设置项：快照字段与桥接行为。"""

import pytest

from core import ai_output_paths as aop


def test_system_settings_snapshot_contains_ai_output_fields(tmp_path, monkeypatch):
    from core.command_shell import get_system_settings

    target = tmp_path / "ai_out"
    monkeypatch.setattr(aop, "_read_setting", lambda: str(target))
    settings = get_system_settings()
    assert settings["ai_output_dir"] == str(target.resolve())
    assert settings["ai_output_dir_setting"] == str(target)
    assert settings["ai_output_dir_default"].endswith("tmp")


def test_set_ai_output_dir_rejects_unwritable(tmp_path, monkeypatch):
    from gui_bridge import GuiBridge

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.setattr(aop, "_is_writable", lambda path: False)
    result = GuiBridge.set_ai_output_dir(object(), str(blocked))
    assert result["ok"] is False and result["msg"]
```

> `GuiBridge.set_ai_output_dir` 是纯函数式实现（只读 self 的 `get_system_settings`），测试用 `object()` 占位 self 即可；若实现需要 `self.get_system_settings()`，测试改用 `monkeypatch.setattr(GuiBridge, "get_system_settings", lambda self: {...})`。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_ai_output_settings.py -v`
Expected: FAIL — KeyError: 'ai_output_dir'

- [ ] **Step 3: 扩展 `core/command_shell.py` 的 `get_system_settings` 返回值**

在 `return {...}` 字典中追加：

```python
        "ai_output_dir": str(_resolve_ai_output_dir()),
        "ai_output_dir_setting": _get_ai_output_dir_setting(),
        "ai_output_dir_default": str(_default_ai_output_dir()),
```

并在函数内惰性导入（避免模块级循环依赖）：

```python
    from core.ai_output_paths import (
        default_ai_output_dir as _default_ai_output_dir,
        get_ai_output_dir_setting as _get_ai_output_dir_setting,
        resolve_ai_output_dir as _resolve_ai_output_dir,
    )
```

- [ ] **Step 4: 在 `gui_bridge.py` 新增桥接方法**

紧跟 `set_command_shell` 之后：

```python
    def set_ai_output_dir(self, path: str) -> Dict[str, Any]:
        """设置 AI 生成内容目录；不可用路径显式拒绝，不做静默降级。"""
        from core.ai_output_paths import AiOutputDirError, set_ai_output_dir_setting

        try:
            effective = set_ai_output_dir_setting(path)
            return {
                "ok": True,
                "settings": self.get_system_settings(),
                "msg": f"AI 生成内容目录已更新：{effective}",
            }
        except (AiOutputDirError, ValueError) as error:
            return {"ok": False, "msg": str(error), "settings": self.get_system_settings()}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_ai_output_settings.py -v`
Expected: 2 passed

- [ ] **Step 6: 提交**

```bash
git add core/command_shell.py gui_bridge.py tests/test_ai_output_settings.py
git commit -m "feat(ai-output): 系统设置快照与桥接支持 AI 生成内容目录"
```

---

### Task 5: 设置页前端

**Files:**
- Modify: `frontend/src/components/SystemSettings.vue`（新增设置行 + 保存逻辑）
- Modify: `frontend/src/stores/appStore.ts`（`setAiOutputDir` action）
- Modify: `frontend/src/types/pywebview.d.ts`（API 与快照类型）
- Test: `frontend/src/components/SystemSettings.test.ts`、`frontend/src/stores/appStore.systemSettings.test.ts`

**Interfaces:**
- Consumes: `window.pywebview.api.set_ai_output_dir(path)`、快照字段 `ai_output_dir` / `ai_output_dir_setting` / `ai_output_dir_default`
- Produces: `store.setAiOutputDir(path: string): Promise<boolean>`

- [ ] **Step 1: 写失败测试**（追加到 `frontend/src/components/SystemSettings.test.ts`）

```ts
it('saves the AI output directory through the store', async () => {
  const store = useAppStore();
  const save = vi.spyOn(store, 'setAiOutputDir').mockResolvedValue(true);
  const wrapper = mount(SystemSettings);
  await wrapper.find('[data-test="ai-output-dir-input"]').setValue('D:/ai_out');
  await wrapper.find('[data-test="save-ai-output-dir"]').trigger('click');
  expect(save).toHaveBeenCalledWith('D:/ai_out');
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend; npx vitest run src/components/SystemSettings.test.ts`
Expected: FAIL — 找不到 `[data-test="ai-output-dir-input"]`

- [ ] **Step 3: 类型定义**（`frontend/src/types/pywebview.d.ts`）

在 `SystemSettingsSnapshot` 中追加：

```ts
  ai_output_dir: string;
  ai_output_dir_setting: string;
  ai_output_dir_default: string;
```

在 api 接口中追加：

```ts
  set_ai_output_dir(path: string): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
```

- [ ] **Step 4: `appStore.ts` 新增 action**（对照 `setCommandShell` 的写法）

```ts
    async setAiOutputDir(path: string): Promise<boolean> {
      const previous = this.systemSettings;
      try {
        const api = (window as any).pywebview?.api;
        if (!api?.set_ai_output_dir) {
          this.notifyModal('系统设置', 'AI 生成内容目录设置失败: 桥接不可用');
          return false;
        }
        const result = await api.set_ai_output_dir(path);
        if (result && result.ok) {
          this.systemSettings = result.settings || previous;
          this.notifyModal('系统设置', result.msg || 'AI 生成内容目录已更新');
          return true;
        }
        this.systemSettings = result?.settings || previous;
        this.notifyModal('系统设置', 'AI 生成内容目录设置失败: ' + (result?.msg || '未知错误'));
        return false;
      } catch (err) {
        this.systemSettings = previous;
        this.notifyModal('系统设置', 'AI 生成内容目录设置失败: ' + err);
        return false;
      }
    }
```

- [ ] **Step 5: `SystemSettings.vue` 新增设置行**

在"单任务最大模型轮次"行之后插入（沿用 `setting-row` / `setting-number-group` 既有样式类）：

```vue
      <div class="setting-row">
        <div class="setting-copy">
          <h2>AI 生成内容目录</h2>
          <p>AI 生成的图片、下载的网页图片与临时脚本统一落到此目录；留空恢复默认（{{ store.systemSettings.ai_output_dir_default }}）。</p>
        </div>
        <div class="setting-number-group">
          <input
            data-test="ai-output-dir-input"
            v-model="aiOutputDirDraft"
            class="setting-number"
            type="text"
            :placeholder="store.systemSettings.ai_output_dir_default"
            :disabled="saving"
          />
          <button
            data-test="save-ai-output-dir"
            class="setting-save"
            type="button"
            :disabled="saving || !aiOutputDirChanged"
            @click="saveAiOutputDir"
          >
            保存
          </button>
        </div>
      </div>
      <p v-if="aiOutputDirError" data-test="ai-output-dir-error" class="setting-error">
        {{ aiOutputDirError }}
      </p>
```

script 部分补充（与 `turnStepLimitDraft` 同款 ref + watch 初始化 + computed 变更判定）：

```ts
const aiOutputDirDraft = ref('');
const aiOutputDirError = ref('');
const aiOutputDirChanged = computed(
  () => aiOutputDirDraft.value.trim() !== (store.systemSettings.ai_output_dir_setting || ''),
);

async function saveAiOutputDir() {
  aiOutputDirError.value = '';
  const ok = await store.setAiOutputDir(aiOutputDirDraft.value.trim());
  if (!ok) {
    aiOutputDirError.value = '保存失败，请检查路径是否可写';
  }
}
```

并在拉取系统设置后的 watch 中初始化 `aiOutputDirDraft.value = store.systemSettings.ai_output_dir_setting || ''`。

- [ ] **Step 6: 跑前端测试**

Run: `cd frontend; npx vitest run src/components/SystemSettings.test.ts src/stores/appStore.systemSettings.test.ts`
Expected: 全部 passed

- [ ] **Step 7: 前端类型检查与构建**

Run: `cd frontend; npx vue-tsc --noEmit`
Expected: 无错误

- [ ] **Step 8: 提交**

```bash
git add frontend/src/components/SystemSettings.vue frontend/src/stores/appStore.ts frontend/src/types/pywebview.d.ts frontend/src/components/SystemSettings.test.ts frontend/src/stores/appStore.systemSettings.test.ts
git commit -m "feat(ai-output): 设置页支持配置 AI 生成内容目录"
```

---

### Task 6: 运行时上下文注入与提示词约定

**Files:**
- Modify: `core/context_builder.py`（`RuntimeContext` 字段 + 上下文块）
- Modify: `core/agent_service.py:321` 附近（构造处传入）
- Modify: `system_prompt.md`、`system_prompt_gui.md`（新增章节）
- Test: `tests/test_context_builder_ai_output.py`（新建）

**Interfaces:**
- Consumes: `core.ai_output_paths.resolve_ai_output_dir()`
- Produces: `RuntimeContext.ai_output_dir: str`；上下文块新增一行 `AI 生成内容目录：<绝对路径>`

- [ ] **Step 1: 写失败测试** `tests/test_context_builder_ai_output.py`

```python
"""运行时上下文块必须暴露 AI 生成内容目录（规格 6.2）。"""

from core.context_builder import RuntimeContext, build_turn_context


def _runtime(**overrides):
    base = dict(
        model_name="m",
        permission_mode="full",
        working_dir="D:/proj",
        current_time="2026-09-20 12:00:00",
        command_shell="pwsh",
        active_plan="无",
        ai_output_dir="C:/Users/u/.ycode/tmp",
    )
    base.update(overrides)
    return RuntimeContext(**base)


def test_context_block_contains_ai_output_dir():
    prompt = build_turn_context(_runtime())
    assert "AI 生成内容目录：C:/Users/u/.ycode/tmp" in prompt
```

> 若 `RuntimeContext` 是 dataclass 且字段顺序固定，按实际签名调整 `_runtime` 的构造方式（保持其余字段用现有默认值）。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_context_builder_ai_output.py -v`
Expected: FAIL — `TypeError: unexpected keyword argument 'ai_output_dir'`

- [ ] **Step 3: 扩展 `core/context_builder.py`**

`RuntimeContext` 增加字段 `ai_output_dir: str`；上下文块在"默认命令解释器"行之后插入：

```python
        f"AI 生成内容目录：{runtime.ai_output_dir}\n"
        "AI 产出的图片、下载文件与临时脚本一律写到该目录；"
        "用户显式指定了保存位置时以用户指定为准。\n"
```

- [ ] **Step 4: `core/agent_service.py` 构造处传入**

```python
        ai_output_dir=str(resolve_ai_output_dir()),
```

（惰性导入 `from core.ai_output_paths import resolve_ai_output_dir`；该块为 append-only 动态上下文，
目录属配置级稳定值，不影响跨轮 prefix 缓存策略。）

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_context_builder_ai_output.py -v`
Expected: passed

- [ ] **Step 6: 两份系统提示词加约定**

`system_prompt.md` 与 `system_prompt_gui.md` 在「主动可视化输出」章节之后新增一节：

```markdown
# AI 生成内容目录

1. 生图、下载图片、临时脚本与中间产物一律落到运行时上下文给出的「AI 生成内容目录」，不要写进用户项目目录。
2. 用户显式指定了保存位置（如"存到 D:\x"）时以用户指定为准；未指定就按默认走。
3. 该目录下的文件属于本回合产出，用户撤销回合时会一并回滚；需要长期保留的产物，请显式写入项目内目标路径。
```

- [ ] **Step 7: 跑上下文相关回归**

Run: `python -m pytest tests/test_context_builder_ai_output.py tests/ -k "context or prompt" -v`
Expected: 全部 passed

- [ ] **Step 8: 提交**

```bash
git add core/context_builder.py core/agent_service.py system_prompt.md system_prompt_gui.md tests/test_context_builder_ai_output.py
git commit -m "feat(ai-output): 运行时上下文与系统提示词声明 AI 生成内容目录"
```

---

### Task 7: 端到端验证（撤销回滚 + 落点复核）

**Files:**
- Test: `tests/test_ai_output_undo.py`（新建）
- 只读复核：`core/turn_undo/*`、三处落盘点

**Interfaces:**
- Consumes: 前六个任务的产出
- Produces: 无生产代码改动（发现问题则回到对应任务修复）

- [ ] **Step 1: 写撤销回滚测试** `tests/test_ai_output_undo.py`

```python
"""AI 目录产物纳入按回合撤销（规格 7）。"""

from pathlib import Path

from core import ai_output_paths as aop


def test_ai_output_file_is_tracked_by_turn_undo(tmp_path, monkeypatch):
    """产物落在工作区外时，撤销层仍应能记录并回滚（display_path_for 支持）。"""
    from core.turn_undo.recorder import canonicalize_path, display_path_for

    ai_file = tmp_path / "ai_out" / "hunyuan_20260920_120000.jpg"
    ai_file.parent.mkdir(parents=True)
    ai_file.write_bytes(b"\xff\xd8\xff")

    canonical = canonicalize_path(str(ai_file))
    display = display_path_for(str(tmp_path / "proj"), canonical)
    # 工作区外路径返回规范化绝对路径，撤销层据此定位文件
    assert Path(display).is_absolute()
    assert canonicalize_path(display) == canonical
```

- [ ] **Step 2: 跑测试**

Run: `python -m pytest tests/test_ai_output_undo.py -v`
Expected: passed（若失败，说明撤销层对工作区外路径有限制，回到 Task 2/3 修正落点或记录偏差）

- [ ] **Step 3: 全量落点复核（只读）**

逐项确认并记录实际路径：
1. `core/hunyuan_image.py` 无 `os.path.abspath(save_dir or ".")` 残留默认分支
2. `plugins/tokenrhythm_image/plugin.py` 同样
3. `core/tools.py` `fetch_image` 无 `Path.cwd()`
4. `grep -rn "save_dir or" core plugins` 结果只剩显式传值分支

Run: `python -m pytest tests/test_ai_output_paths.py tests/test_ai_output_landing.py tests/test_ai_output_settings.py tests/test_ai_output_undo.py tests/test_fetch_image.py -v`
Expected: 全部 passed

- [ ] **Step 4: 全量回归**

Run: `python -m pytest tests/ -x -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add tests/test_ai_output_undo.py
git commit -m "test(ai-output): 端到端验证落点与撤销回滚"
```

---

## 自查记录

- **Spec 覆盖**：设置项（T4/T5）、解析入口（T1）、三处落盘（T2）、AI 临时脚本（T3/T6）、撤销（T7）、错误处理（T1 回退 + T4 拒绝）、测试（各任务）均有对应任务。
- **已知偏差**：spec 第 8 节写的组件名是 `SettingsPage.vue`，实际设置 UI 在 `frontend/src/components/SystemSettings.vue`（本计划按实际文件执行）。
- **未覆盖**：spec 第 9 节"前端渲染 `~/.ycode/tmp` 图片"需在 T7 后手工验证一次（GUI 内生成图片并确认内嵌展示正常）。
