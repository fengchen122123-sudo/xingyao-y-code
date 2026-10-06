# 星瑶 IDE 模式 S1「IDE 骨架」实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 GUI 上新增「IDE 模式」，让用户可以直接编辑、保存工作区内的文件，并与星瑶的写盘体系安全共存。

**Architecture:** 后端新增 `core/ide/` 包（文件读写服务 + 布局持久化），在 `gui_bridge.py` 暴露 6 个桥方法并新增「已打开文件」轮询派发；前端新增 `ideStore`（按会话隔离的标签与脏状态）与 `IdeLayout`（左侧文件树 + 中间编辑器 + 底部面板组 + 右侧对话），编辑器内核用 CodeMirror 6，全部语言包动态 import。对话模式与 IDE 模式共用同一套会话与工作区状态，切换不重建会话。

**Tech Stack:** Python 3 / pywebview 桥（`window.pywebview.api` + 事件管线）、Vue 3 + Pinia + Vite + TypeScript、CodeMirror 6、pytest、vitest。

## Global Constraints

以下约束适用于本计划的每一个任务，逐字取自 `docs/superpowers/specs/2026-10-06-ide-mode-design.md`：

- 开发分支固定为 `ide`，不切到 `dev` 或 `master`。
- 版本号只在发布时改 `core/version.py`，本计划任何任务都不得改版本号。
- 不得修改 `core/lsp.py`（`find_definition` 工具的实现），IDE 相关能力一律放 `core/ide/`。
- 不得改变 `build_stable_prompt` 的字节输出；`tests/test_prompt_byte_stability.py` 必须保持通过。
- 不得把编辑器内容或打开文件列表注入 Agent 上下文（会破坏缓存前缀与字节稳定）。
- 所有前端可见事件必须经 `gui_bridge.py` 的 `_js_queue` 与 `gui-js-dispatcher` 单消费者线程派发，事件源线程只允许 `put_nowait`，禁止直接 `evaluate_js`。
- 用户手动编辑与保存**不接入**「按回合撤销」体系，也不写 Journal 正文。
- 前端 CSP 保持 `default-src 'self'`（`frontend/index.html:7-11`），不引入 eval、不引入外部 CDN。
- 文本编码不得混淆：读进来是 GBK 就写回 GBK，BOM 与换行风格原样保持。
- 工作区外文件可只读，保存必须拒绝；`~/.ycode` 顶层凭据配置文件读写一律拒绝。
- `frontend/dist` 与 `dist/` 永不提交。
- 后端测试用 pytest（`tests/test_*.py`），前端测试用 vitest（`frontend/src/**/*.test.ts` 与组件同目录）。

---

## File Structure

**后端**

| 文件 | 职责 |
|---|---|
| `core/ide/__init__.py` | 包标识，只放版本与导出 |
| `core/ide/file_service.py` | 编辑器文件读写：编码/BOM/换行保持、原子写、冲突检测、大文件与二进制拒绝 |
| `core/ide/layout_store.py` | IDE 布局状态（标签、光标、断点）的后端原子写持久化 |
| `core/gui_path_policy.py`（改） | 新增 `action="edit"` 分支 |
| `gui_bridge.py`（改） | 6 个 IDE 桥方法 + 已打开文件轮询派发 |

**前端**

| 文件 | 职责 |
|---|---|
| `frontend/src/editor/setup.ts` | CodeMirror 6 基础扩展组装与语言包动态加载表 |
| `frontend/src/editor/theme.ts` | 从 GUI 主题 CSS 变量构造 CM6 主题 |
| `frontend/src/stores/ideStore.ts` | 按会话隔离的标签、脏标记、外部改动状态与读写动作 |
| `frontend/src/components/ActivityBar.vue` | 左侧图标活动栏 |
| `frontend/src/components/EditorTabs.vue` | 标签页条 |
| `frontend/src/components/EditorPane.vue` | 编辑器宿主（挂载 CM6、脏标记、保存、外部改动提示） |
| `frontend/src/components/BottomPanel.vue` | 底部面板组容器（终端 / 问题 / 输出 / 调试控制台页签） |
| `frontend/src/components/IdeLayout.vue` | IDE 模式总布局 |
| `frontend/src/App.vue`（改） | 按 `ideMode` 切换布局 |
| `frontend/src/components/Topbar.vue`（改） | 模式切换按钮 |
| `frontend/src/components/FileTreeNode.vue`（改） | 按模式决定点击行为（预览 vs 打开到编辑器） |
| `frontend/src/types/pywebview.d.ts`（改） | IDE 桥方法与结果类型声明 |

---

## Task 1: 引入 CodeMirror 6 并打通编辑器状态工厂

**Files:**
- Modify: `frontend/package.json`
- Create: `frontend/src/editor/setup.ts`
- Test: `frontend/src/editor/setup.test.ts`

**Interfaces:**
- Consumes: 无
- Produces:
  - `createEditorState(doc: string, languageId: string): Promise<EditorState>`
  - `LANGUAGE_LOADERS: Record<string, () => Promise<Extension>>`

- [ ] **Step 1: 安装依赖**

```bash
cd /d E:\fch\Y-code_llm\frontend
npm install @codemirror/state @codemirror/view @codemirror/commands @codemirror/search @codemirror/language @codemirror/lang-python @codemirror/lang-javascript @codemirror/lang-json @codemirror/lang-markdown @codemirror/lang-html @codemirror/lang-css @codemirror/legacy-modes
```

预期：`package.json` 的 `dependencies` 新增上述包，`npm install` 退出码 0。

- [ ] **Step 2: 写失败的测试**

创建 `frontend/src/editor/setup.test.ts`：

```ts
import { expect, it } from 'vitest';

import { createEditorState, LANGUAGE_LOADERS } from './setup';

it('createEditorState 保留文档内容并设置语言扩展', async () => {
  const state = await createEditorState('x = 1\n', 'python');
  expect(state.doc.toString()).toBe('x = 1\n');
  // python 语言扩展已挂载：语言数据里能查到 python 的缩进信息
  expect(state.languageData.length).toBeGreaterThan(0);
});

it('未登记的语言回落到纯文本，不抛异常', async () => {
  const state = await createEditorState('plain', 'no-such-language');
  expect(state.doc.toString()).toBe('plain');
});

it('语言加载表登记了 Python 与 JavaScript', () => {
  expect(Object.keys(LANGUAGE_LOADERS)).toContain('python');
  expect(Object.keys(LANGUAGE_LOADERS)).toContain('javascript');
});
```

- [ ] **Step 3: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/editor/setup.test.ts
```

预期：FAIL，报错形如 `Failed to resolve import "./setup"`。

- [ ] **Step 4: 实现 setup.ts**

创建 `frontend/src/editor/setup.ts`：

```ts
import { EditorState, type Extension } from '@codemirror/state';
import { EditorView, lineNumbers, highlightActiveLine, highlightActiveLineGutter } from '@codemirror/view';
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands';
import { searchKeymap, highlightSelectionMatches } from '@codemirror/search';
import { bracketMatching, indentOnInput, syntaxHighlighting, defaultHighlightStyle } from '@codemirror/language';
import { keymap } from '@codemirror/view';

/**
 * 语言扩展按需动态加载：与 RightPanel.vue 里 highlight.js 的懒加载同一思路，
 * 语言包不进首屏 chunk。
 */
export const LANGUAGE_LOADERS: Record<string, () => Promise<Extension>> = {
  python: () => import('@codemirror/lang-python').then((m) => m.python()),
  javascript: () => import('@codemirror/lang-javascript').then((m) => m.javascript()),
  typescript: () => import('@codemirror/lang-javascript').then((m) => m.javascript({ typescript: true })),
  json: () => import('@codemirror/lang-json').then((m) => m.json()),
  markdown: () => import('@codemirror/lang-markdown').then((m) => m.markdown()),
  html: () => import('@codemirror/lang-html').then((m) => m.html()),
  css: () => import('@codemirror/lang-css').then((m) => m.css()),
};

/** 扩展名到语言 id 的映射；未命中的语言一律走纯文本。 */
const EXT_LANGUAGE: Record<string, string> = {
  py: 'python',
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  jsx: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  json: 'json',
  md: 'markdown',
  html: 'html',
  htm: 'html',
  css: 'css',
};

export function languageIdForPath(path: string): string {
  const name = path.split(/[\\/]/).pop() ?? '';
  const dot = name.lastIndexOf('.');
  if (dot < 0) return '';
  return EXT_LANGUAGE[name.slice(dot + 1).toLowerCase()] ?? '';
}

export function baseExtensions(): Extension[] {
  return [
    lineNumbers(),
    highlightActiveLine(),
    highlightActiveLineGutter(),
    history(),
    bracketMatching(),
    indentOnInput(),
    highlightSelectionMatches(),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    keymap.of([...defaultKeymap, ...historyKeymap, ...searchKeymap, indentWithTab]),
  ];
}

/** 语言加载失败时静默回落到纯文本：编辑器可用性优先于高亮。 */
export async function languageExtension(languageId: string): Promise<Extension[]> {
  const loader = LANGUAGE_LOADERS[languageId];
  if (!loader) return [];
  try {
    return [await loader()];
  } catch {
    return [];
  }
}

export async function createEditorState(doc: string, languageId: string): Promise<EditorState> {
  return EditorState.create({
    doc,
    extensions: [...baseExtensions(), ...(await languageExtension(languageId))],
  });
}

export { EditorView };
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/editor/setup.test.ts
```

预期：PASS，3 个用例全绿。

- [ ] **Step 6: 校验生产构建未被撑爆**

```bash
cd /d E:\fch\Y-code_llm\frontend
npm run build
```

预期：构建成功；记录 `dist/assets/` 下最大 chunk 体积作为后续体积门禁基线（写进本步骤的提交信息里，例如「首屏最大 chunk 1.1 MB，语言包已独立成 chunk」）。

- [ ] **Step 7: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add frontend/package.json frontend/package-lock.json frontend/src/editor/setup.ts frontend/src/editor/setup.test.ts
git commit -m "feat(ide): 引入 CodeMirror 6 并新增编辑器状态工厂与语言懒加载表"
```

---

## Task 2: 路径策略新增 edit 动作

**Files:**
- Modify: `core/gui_path_policy.py`
- Test: `tests/test_gui_path_policy_edit.py`

**Interfaces:**
- Consumes: 无
- Produces: `gui_path_access_allowed(real_path, workspace, *, action="edit", session_id=None) -> tuple[bool, str]`（与既有签名一致，新增 `action="edit"` 取值）

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_gui_path_policy_edit.py`：

```python
# -*- coding: utf-8 -*-
"""gui_path_access_allowed 的 edit 动作放行矩阵。

edit 与 preview 的规则完全一致（凭据永久拒绝 / GLOBAL 放行 / 工作区内放行 /
工作区外跟随沙盒开关），只有拒绝文案里的动词不同。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.gui_path_policy as gpp


def _inject(monkeypatch, tmp_path):
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))


def test_edit_allowed_inside_workspace(tmp_path, monkeypatch):
    _inject(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1", encoding="utf-8")
    ok, msg = gpp.gui_path_access_allowed(str(f), str(ws), action="edit")
    assert ok is True
    assert msg == ""


def test_edit_denied_outside_workspace_when_sandbox_off(tmp_path, monkeypatch):
    _inject(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "b.py"
    f.write_text("y = 2", encoding="utf-8")
    ok, msg = gpp.gui_path_access_allowed(str(f), str(ws), action="edit")
    assert ok is False
    assert "工作区" in msg


def test_edit_allowed_outside_workspace_when_sandbox_on(tmp_path, monkeypatch):
    _inject(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: True)
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "b.py"
    f.write_text("y = 2", encoding="utf-8")
    ok, _ = gpp.gui_path_access_allowed(str(f), str(ws), action="edit")
    assert ok is True


def test_edit_denied_on_dotdot_traversal(tmp_path, monkeypatch):
    """工作区内路径经 .. 指向工作区外，realpath 归一化后必须拒绝。"""
    _inject(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "b.py"
    f.write_text("y = 2", encoding="utf-8")
    sneaky = os.path.join(str(ws), "..", "outside", "b.py")
    ok, _ = gpp.gui_path_access_allowed(sneaky, str(ws), action="edit")
    assert ok is False


def test_edit_credential_config_always_denied(tmp_path, monkeypatch):
    """凭据文件即使沙盒开启、GLOBAL 会话也必须拒绝，且文案随动作变化。"""
    ydata = tmp_path / "ydata"
    ydata.mkdir()
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(ydata))
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: True)
    f = ydata / "config.json"
    f.write_text('{"api_key": "secret"}', encoding="utf-8")
    ok, msg = gpp.gui_path_access_allowed(str(f), "GLOBAL", action="edit")
    assert ok is False
    assert "编辑" in msg


def test_preview_action_noun_unchanged(tmp_path, monkeypatch):
    """回归：原有 preview / open 文案不能被改动。"""
    _inject(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "b.py"
    f.write_text("y = 2", encoding="utf-8")
    _, msg_preview = gpp.gui_path_access_allowed(str(f), str(ws), action="preview")
    _, msg_open = gpp.gui_path_access_allowed(str(f), str(ws), action="open")
    assert "预览" in msg_preview
    assert "打开" in msg_open
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_gui_path_policy_edit.py -v
```

预期：`test_edit_credential_config_always_denied` FAIL（文案里没有「编辑」），其余用例通过——因为现有实现忽略未知 action、统一用「预览」措辞。

- [ ] **Step 3: 实现 edit 动作**

在 `core/gui_path_policy.py` 中，把函数开头的文案计算替换为动作映射表。原代码为：

```python
    noun = "打开" if action == "open" else "预览"
```

改为：

```python
    noun = _ACTION_NOUN.get(action, _ACTION_NOUN["preview"])
```

并在 `_CREDENTIAL_BASENAMES` 定义之后新增常量：

```python
# 动作到文案动词的映射：edit 与 preview 共用同一套放行规则，只有措辞不同
_ACTION_NOUN = {"preview": "预览", "open": "打开", "edit": "编辑"}
```

同时在模块 docstring 的策略列表里补一条，说明 `edit` 与 `preview` 规则一致（避免后来者以为编辑有更宽松或更严格的规则）：

```
5. action="edit"（IDE 编辑器保存）：规则与 preview 完全一致，仅拒绝文案
   的动词不同；工作区外保存是否放行同样跟随沙盒开关。
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_gui_path_policy_edit.py tests/test_read_file_preview.py -v
```

预期：两个文件全部 PASS（后者是回归守卫，确认既有 preview / open 行为未被改动）。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add core/gui_path_policy.py tests/test_gui_path_policy_edit.py
git commit -m "feat(ide): 路径策略新增 edit 动作，与 preview 共用放行规则"
```

---

## Task 3: 文件服务读取（编码、BOM、换行、大文件、二进制）

**Files:**
- Create: `core/ide/__init__.py`
- Create: `core/ide/file_service.py`
- Test: `tests/test_ide_file_service.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `IDE_EDIT_MAX_BYTES: int`
  - `FileServiceError(Exception)`
  - `read_text_file(path: str) -> dict`，键为 `path` / `content` / `encoding` / `bom` / `eol` / `hash` / `size`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_ide_file_service.py`：

```python
# -*- coding: utf-8 -*-
"""IDE 编辑器文件服务：读取路径的编码 / BOM / 换行 / 体积 / 二进制处理。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ide import file_service
from core.ide.file_service import FileServiceError, read_text_file


def test_read_utf8_plain(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    info = read_text_file(str(f))
    assert info["content"] == "x = 1\n"
    assert info["encoding"] == "utf-8"
    assert info["bom"] is False
    assert info["eol"] == "lf"
    assert len(info["hash"]) == 64


def test_read_utf8_with_bom_marks_bom(tmp_path):
    f = tmp_path / "bom.py"
    f.write_bytes(b"\xef\xbb\xbfx = 1\n")
    info = read_text_file(str(f))
    assert info["bom"] is True
    # BOM 不进正文，否则编辑器里会出现一个不可见字符
    assert info["content"] == "x = 1\n"
    assert info["encoding"] == "utf-8"


def test_read_gbk_file(tmp_path):
    f = tmp_path / "gbk.txt"
    f.write_bytes("中文内容\n".encode("gbk"))
    info = read_text_file(str(f))
    assert info["content"] == "中文内容\n"
    assert info["encoding"] == "gbk"


def test_read_detects_crlf(tmp_path):
    f = tmp_path / "win.py"
    f.write_bytes(b"a = 1\r\nb = 2\r\n")
    info = read_text_file(str(f))
    assert info["eol"] == "crlf"


def test_read_detects_mixed_eol(tmp_path):
    f = tmp_path / "mixed.py"
    f.write_bytes(b"a = 1\r\nb = 2\n")
    info = read_text_file(str(f))
    assert info["eol"] == "mixed"


def test_read_rejects_binary(tmp_path):
    f = tmp_path / "app.exe"
    f.write_bytes(b"MZ\x90\x00" + b"\x00" * 32)
    with pytest.raises(FileServiceError) as exc:
        read_text_file(str(f))
    assert "二进制" in str(exc.value)


def test_read_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(file_service, "IDE_EDIT_MAX_BYTES", 8)
    f = tmp_path / "big.txt"
    f.write_text("0123456789", encoding="utf-8")
    with pytest.raises(FileServiceError) as exc:
        read_text_file(str(f))
    assert "过大" in str(exc.value)


def test_read_rejects_unknown_encoding(tmp_path):
    # 0x81 在 GBK 里是非法首字节，UTF-8 也解不开
    f = tmp_path / "weird.txt"
    f.write_bytes(b"\x81\x81\x81")
    with pytest.raises(FileServiceError) as exc:
        read_text_file(str(f))
    assert "编码" in str(exc.value)


def test_read_rejects_missing_file(tmp_path):
    with pytest.raises(FileServiceError):
        read_text_file(str(tmp_path / "nope.py"))
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_file_service.py -v
```

预期：FAIL，报错 `ModuleNotFoundError: No module named 'core.ide'`。

- [ ] **Step 3: 创建包与读取实现**

创建 `core/ide/__init__.py`：

```python
# -*- coding: utf-8 -*-
"""IDE 模式的 Python 侧支撑模块。

与 core/lsp.py 的区别：core/lsp.py 是 find_definition 工具用的符号查找实现，
本包服务于 GUI 编辑器的文件读写与语言服务，两者互不依赖。
"""

__all__ = ["file_service", "layout_store"]
```

创建 `core/ide/file_service.py`：

```python
# -*- coding: utf-8 -*-
"""IDE 编辑器文件读写服务。

与只读预览（gui_bridge.read_file_preview）的关键区别：
- 预览允许截断、允许图片；编辑器不允许——内容不完整会写坏文件；
- 编辑器必须原样保留编码、BOM 与换行风格，写回时一个字节都不能变。

编码口径与预览保持一致（UTF-8 -> GBK），避免同一个文件在两处显示不同。
"""
from __future__ import annotations

import hashlib
import os
import tempfile

# 编辑器单文件上限：超过此体积 CodeMirror 的渲染与语法分析会明显卡顿
IDE_EDIT_MAX_BYTES = 2 * 1024 * 1024

# 编码探测顺序，与 read_file_preview 一致
_ENCODINGS = ("utf-8", "gbk")

_BOM_UTF8 = b"\xef\xbb\xbf"


class FileServiceError(Exception):
    """文件服务业务错误，消息可直接展示给用户。"""


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _detect_eol(text: str) -> str:
    """判断换行风格；两种以上混用时返回 mixed，写回时不做转换。"""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    kinds = [bool(crlf), bool(lf), bool(cr)]
    if sum(kinds) > 1:
        return "mixed"
    if crlf:
        return "crlf"
    if cr:
        return "cr"
    return "lf"


def read_text_file(path: str) -> dict:
    """读取文本文件用于编辑。

    返回 content（不含 BOM 的正文）、encoding、bom、eol、hash（原始字节的
    SHA-256，作为保存时的冲突检测基准）与 size。
    """
    real = os.path.realpath(os.path.abspath(path))
    if not os.path.isfile(real):
        raise FileServiceError("文件不存在或不是普通文件")
    size = os.path.getsize(real)
    if size > IDE_EDIT_MAX_BYTES:
        raise FileServiceError(
            f"文件过大（{size / 1024 / 1024:.1f} MB，编辑上限 "
            f"{IDE_EDIT_MAX_BYTES // 1024 // 1024} MB），请使用只读预览"
        )
    with open(real, "rb") as f:
        raw = f.read()
    if b"\x00" in raw:
        raise FileServiceError("二进制文件不支持编辑，可在系统中打开")
    bom = raw.startswith(_BOM_UTF8)
    payload = raw[len(_BOM_UTF8):] if bom else raw
    content = None
    encoding = ""
    for enc in _ENCODINGS:
        try:
            content = payload.decode(enc)
            encoding = enc
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        raise FileServiceError("无法识别文件编码（仅支持 UTF-8 / GBK 文本）")
    return {
        "path": real,
        "content": content,
        "encoding": encoding,
        "bom": bom,
        "eol": _detect_eol(content),
        "hash": _hash_bytes(raw),
        "size": size,
    }
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_file_service.py -v
```

预期：9 个用例全部 PASS。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add core/ide/__init__.py core/ide/file_service.py tests/test_ide_file_service.py
git commit -m "feat(ide): 新增文件服务读取，保持编码/BOM/换行元信息并拒绝大文件与二进制"
```

---

## Task 4: 文件服务写入（原子写、编码与换行保持、冲突检测）

**Files:**
- Modify: `core/ide/file_service.py`
- Test: `tests/test_ide_file_service.py`（追加用例）

**Interfaces:**
- Consumes: `read_text_file` 返回的 `encoding` / `bom` / `eol` / `hash`
- Produces: `write_text_file(path, content, expected_hash, *, encoding, bom, eol) -> dict`，成功返回 `{"ok": True, "hash": str}`，冲突返回 `{"ok": False, "conflict": {"disk_hash": str}, "msg": str}`

- [ ] **Step 1: 写失败的测试**

在 `tests/test_ide_file_service.py` 末尾追加：

```python
# ---------------------------------------------------------------------------
# 写入：编码与换行原样保持、原子写、冲突检测
# ---------------------------------------------------------------------------
from core.ide.file_service import write_text_file


def test_write_keeps_utf8_and_lf(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    info = read_text_file(str(f))
    res = write_text_file(
        str(f), "x = 2\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    assert res["ok"] is True
    assert f.read_bytes() == b"x = 2\n"


def test_write_keeps_gbk_encoding(tmp_path):
    """GBK 文件写回必须还是 GBK，否则中文变乱码。"""
    f = tmp_path / "gbk.txt"
    f.write_bytes("中文\n".encode("gbk"))
    info = read_text_file(str(f))
    res = write_text_file(
        str(f), "中文改\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    assert res["ok"] is True
    assert f.read_bytes() == "中文改\n".encode("gbk")


def test_write_keeps_bom(tmp_path):
    f = tmp_path / "bom.py"
    f.write_bytes(b"\xef\xbb\xbfx = 1\n")
    info = read_text_file(str(f))
    write_text_file(
        str(f), "x = 2\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    assert f.read_bytes() == b"\xef\xbb\xbfx = 2\n"


def test_write_keeps_crlf(tmp_path):
    """编辑器内部统一用 LF，写回时必须还原成原文件的 CRLF。"""
    f = tmp_path / "win.py"
    f.write_bytes(b"a = 1\r\nb = 2\r\n")
    info = read_text_file(str(f))
    write_text_file(
        str(f), "a = 1\nb = 3\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    assert f.read_bytes() == b"a = 1\r\nb = 3\r\n"


def test_write_mixed_eol_not_converted(tmp_path):
    f = tmp_path / "mixed.py"
    f.write_bytes(b"a = 1\r\nb = 2\n")
    info = read_text_file(str(f))
    assert info["eol"] == "mixed"
    write_text_file(
        str(f), "a = 9\nb = 2\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    # mixed 不做归一化，正文按 LF 落盘
    assert f.read_bytes() == b"a = 9\nb = 2\n"


def test_write_detects_conflict(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    info = read_text_file(str(f))
    # 模拟星瑶在用户编辑期间改了同一个文件
    f.write_text("x = 1  # changed by agent\n", encoding="utf-8")
    res = write_text_file(
        str(f), "x = 2\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    assert res["ok"] is False
    assert res["conflict"]["disk_hash"] != info["hash"]
    # 冲突时绝不落盘，磁盘内容保持星瑶写的版本
    assert f.read_text(encoding="utf-8") == "x = 1  # changed by agent\n"


def test_write_skips_conflict_check_when_hash_empty(tmp_path):
    """expected_hash 为空表示调用方明确要求覆盖（用户选择了「用我的覆盖」）。"""
    f = tmp_path / "a.py"
    f.write_text("old\n", encoding="utf-8")
    res = write_text_file(
        str(f), "new\n", "",
        encoding="utf-8", bom=False, eol="lf",
    )
    assert res["ok"] is True
    assert f.read_text(encoding="utf-8") == "new\n"


def test_write_is_atomic_no_temp_left(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    info = read_text_file(str(f))
    write_text_file(
        str(f), "x = 2\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".ycode-ide-")]
    assert leftovers == []
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_file_service.py -v -k write
```

预期：FAIL，报错 `ImportError: cannot import name 'write_text_file'`。

- [ ] **Step 3: 实现写入**

在 `core/ide/file_service.py` 末尾追加：

```python
def _normalize_eol(text: str, eol: str) -> str:
    """把编辑器内部的 LF 还原成原文件的换行风格。

    mixed 不做转换：混合换行的文件无法用单一风格还原，强行统一会改动
    用户没碰过的行，属于「保存一次全文件 diff」的经典事故。
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    if eol == "crlf":
        return unified.replace("\n", "\r\n")
    if eol == "cr":
        return unified.replace("\n", "\r")
    return unified


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """临时文件 + 原子替换；失败时清理临时文件。"""
    directory = os.path.dirname(path) or "."
    try:
        mode = os.stat(path).st_mode
    except OSError:
        mode = None
    fd, tmp = tempfile.mkstemp(prefix=".ycode-ide-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if mode is not None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def write_text_file(
    path: str,
    content: str,
    expected_hash: str,
    *,
    encoding: str,
    bom: bool,
    eol: str,
) -> dict:
    """保存编辑器内容。

    expected_hash 为打开文件时 read_text_file 返回的 hash：磁盘内容与它不
    一致说明文件在编辑期间被外部（星瑶或其他程序）改过，返回 conflict 且
    不落盘，由前端让用户选择。传空字符串表示调用方明确要求覆盖。
    """
    real = os.path.realpath(os.path.abspath(path))
    if expected_hash:
        try:
            with open(real, "rb") as fh:
                disk_hash = _hash_bytes(fh.read())
        except FileNotFoundError:
            disk_hash = ""
        if disk_hash != expected_hash:
            return {
                "ok": False,
                "conflict": {"disk_hash": disk_hash},
                "msg": "文件已被外部修改，请选择以磁盘为准重载或用当前内容覆盖",
            }
    text = _normalize_eol(content, eol)
    try:
        data = text.encode(encoding)
    except UnicodeEncodeError as exc:
        raise FileServiceError(
            f"内容包含 {encoding} 无法表示的字符（{exc.start} 处），请先改用 UTF-8 保存"
        ) from exc
    if bom:
        data = _BOM_UTF8 + data
    _atomic_write_bytes(real, data)
    return {"ok": True, "hash": _hash_bytes(data)}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_file_service.py -v
```

预期：全部 PASS（读取 9 个 + 写入 8 个）。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add core/ide/file_service.py tests/test_ide_file_service.py
git commit -m "feat(ide): 文件服务新增原子写与冲突检测，原样保持编码/BOM/换行"
```

---

## Task 5: 已打开文件的外部改动轮询

**Files:**
- Modify: `core/ide/file_service.py`
- Test: `tests/test_ide_file_service.py`（追加用例）

**Interfaces:**
- Consumes: 无
- Produces: `OpenFileWatcher(interval: float = 1.0)`，含 `set_open_files(paths: list[str]) -> None` 与 `poll() -> list[str]`（返回自上次 poll 后 `(mtime, size)` 变化的绝对路径）

- [ ] **Step 1: 写失败的测试**

在 `tests/test_ide_file_service.py` 末尾追加：

```python
# ---------------------------------------------------------------------------
# 外部改动轮询：只盯已打开文件，用显式 utime 保证结果确定
# ---------------------------------------------------------------------------
from core.ide.file_service import OpenFileWatcher


def test_watcher_reports_modified_file_once(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    base = os.stat(str(f)).st_mtime
    watcher = OpenFileWatcher()
    watcher.set_open_files([str(f)])

    os.utime(str(f), (base + 10, base + 10))
    assert watcher.poll() == [str(f)]
    # 第二次轮询不应重复上报同一个变更
    assert watcher.poll() == []


def test_watcher_reports_deleted_file_once(tmp_path):
    f = tmp_path / "gone.py"
    f.write_text("x = 1\n", encoding="utf-8")
    watcher = OpenFileWatcher()
    watcher.set_open_files([str(f)])
    os.unlink(str(f))
    assert watcher.poll() == [str(f)]
    assert watcher.poll() == []


def test_watcher_ignores_unopened_files(tmp_path):
    opened = tmp_path / "open.py"
    opened.write_text("x = 1\n", encoding="utf-8")
    other = tmp_path / "other.py"
    other.write_text("y = 1\n", encoding="utf-8")
    base = os.stat(str(other)).st_mtime
    watcher = OpenFileWatcher()
    watcher.set_open_files([str(opened)])

    os.utime(str(other), (base + 10, base + 10))
    assert watcher.poll() == []


def test_watcher_set_open_files_resets_baseline(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    watcher = OpenFileWatcher()
    base = os.stat(str(f)).st_mtime
    os.utime(str(f), (base + 10, base + 10))
    # 重新登记时才建立基线，不会把登记前的改动当作变更上报
    watcher.set_open_files([str(f)])
    assert watcher.poll() == []
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_file_service.py -v -k watcher
```

预期：FAIL，报错 `ImportError: cannot import name 'OpenFileWatcher'`。

- [ ] **Step 3: 实现轮询器**

在 `core/ide/file_service.py` 的 import 区补 `import threading`，并在末尾追加：

```python
class OpenFileWatcher:
    """只轮询当前已打开文件的 (mtime, size)。

    刻意不做全仓扫描：IDE 打开的文件通常个位数，按 1 秒轮询的代价可以忽略，
    而目录监听（watchdog）需要额外依赖，与项目的依赖红线冲突。
    """

    def __init__(self, interval: float = 1.0):
        self._interval = interval
        self._files: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    @property
    def interval(self) -> float:
        return self._interval

    def _stamp(self, real: str) -> tuple[float, int]:
        try:
            st = os.stat(real)
            return (st.st_mtime, st.st_size)
        except OSError:
            return (0.0, -1)

    def set_open_files(self, paths) -> None:
        """重设监视集合，并以当前磁盘状态建立基线。"""
        snapshot: dict[str, tuple[float, int]] = {}
        for p in paths or []:
            real = os.path.realpath(os.path.abspath(str(p)))
            snapshot[real] = self._stamp(real)
        with self._lock:
            self._files = snapshot

    def poll(self) -> list[str]:
        """返回自上次 poll 后发生变化的文件（含被删除的文件，标记为 size=-1）。"""
        changed: list[str] = []
        with self._lock:
            for real in list(self._files.keys()):
                current = self._stamp(real)
                if current != self._files[real]:
                    self._files[real] = current
                    changed.append(real)
        return changed
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_file_service.py -v
```

预期：全部 PASS（新增 4 个用例）。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add core/ide/file_service.py tests/test_ide_file_service.py
git commit -m "feat(ide): 新增已打开文件外部改动轮询器"
```

---

## Task 6: IDE 布局状态后端持久化

**Files:**
- Create: `core/ide/layout_store.py`
- Test: `tests/test_ide_layout_store.py`

**Interfaces:**
- Consumes: `config._read_json_with_backup`、`config._atomic_write_json`（沿用 `core/ui_layout.py` 的写法）
- Produces:
  - `get_ide_layout(session: str) -> dict`（缺省返回 `{"version": 1, "tabs": [], "active": "", "breakpoints": []}`）
  - `save_ide_layout(session: str, payload) -> dict`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_ide_layout_store.py`：

```python
# -*- coding: utf-8 -*-
"""IDE 布局状态后端持久化：按会话隔离、脏数据清洗、原子写。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.ide.layout_store as store


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path))
    return tmp_path


def test_default_layout_when_file_absent(data_dir):
    layout = store.get_ide_layout("s1")
    assert layout == {"version": 1, "tabs": [], "active": "", "breakpoints": []}


def test_save_then_get_roundtrip(data_dir):
    saved = store.save_ide_layout("s1", {
        "tabs": [{"path": "E:\\p\\a.py", "line": 12, "dirty": False}],
        "active": "E:\\p\\a.py",
        "breakpoints": [{"path": "E:\\p\\a.py", "line": 3}],
    })
    assert saved["tabs"][0]["path"] == "E:\\p\\a.py"
    again = store.get_ide_layout("s1")
    assert again["active"] == "E:\\p\\a.py"
    assert again["breakpoints"] == [{"path": "E:\\p\\a.py", "line": 3}]


def test_sessions_are_isolated(data_dir):
    store.save_ide_layout("s1", {"tabs": [{"path": "a.py", "line": 1}], "active": "a.py", "breakpoints": []})
    assert store.get_ide_layout("s2")["tabs"] == []


def test_dirty_payload_is_sanitized(data_dir):
    saved = store.save_ide_layout("s1", {
        "tabs": [
            {"path": "ok.py", "line": 1},
            {"path": "", "line": 1},          # 空路径丢弃
            {"path": "bad.py", "line": "x"},  # 行号非整数丢弃
            "not-a-dict",                      # 结构错误丢弃
        ],
        "active": 123,
        "breakpoints": [{"path": "b.py", "line": -1}],  # 行号必须为正
    })
    assert [t["path"] for t in saved["tabs"]] == ["ok.py"]
    assert saved["active"] == ""
    assert saved["breakpoints"] == []


def test_tabs_are_capped(data_dir):
    tabs = [{"path": f"f{i}.py", "line": 1} for i in range(store.MAX_TABS + 20)]
    saved = store.save_ide_layout("s1", {"tabs": tabs, "active": "", "breakpoints": []})
    assert len(saved["tabs"]) == store.MAX_TABS


def test_corrupt_file_falls_back_to_default(data_dir):
    (data_dir / "ide_layout.json").write_text("{ not json", encoding="utf-8")
    assert store.get_ide_layout("s1")["tabs"] == []


def test_saved_file_is_valid_json(data_dir):
    store.save_ide_layout("s1", {"tabs": [{"path": "a.py", "line": 1}], "active": "a.py", "breakpoints": []})
    raw = (data_dir / "ide_layout.json").read_text(encoding="utf-8")
    assert json.loads(raw)["sessions"]["s1"]["active"] == "a.py"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_layout_store.py -v
```

预期：FAIL，报错 `ModuleNotFoundError: No module named 'core.ide.layout_store'`。

- [ ] **Step 3: 实现 layout_store**

创建 `core/ide/layout_store.py`：

```python
# -*- coding: utf-8 -*-
"""IDE 布局状态持久化（打开的标签、光标行、断点）。

为什么不用 localStorage 做权威副本：WebView2 的 localStorage 异步落盘，
用户改完立刻关窗会因 checksum mismatch 丢数据（详见 core/ui_layout.py 文件头
记录的同类事故）。这里沿用同一套「后端原子写 + 前端缓存」策略。

写盘走 config._atomic_write_json，读取失败一律回落空快照——磁盘异常不该
让 IDE 打不开。
"""
from __future__ import annotations

import threading
from typing import Any

_STORE_LOCK = threading.Lock()

_STORE_VERSION = 1

# 结构上限：防御性约束，正常使用远达不到
MAX_TABS = 100
MAX_BREAKPOINTS = 500


class IdeLayoutError(ValueError):
    """布局快照写入失败。"""


def _store_path():
    try:
        import config

        return (
            config.YCODE_DIR / "ide_layout.json"
            if hasattr(config.YCODE_DIR, "__truediv__")
            else config.YCODE_DIR + "/ide_layout.json"
        )
    except Exception:
        from pathlib import Path

        import os

        data_dir = os.environ.get("YCODE_DATA_DIR") or os.path.join(str(Path.home()), ".ycode")
        return os.path.join(data_dir, "ide_layout.json")


def _clean_tab(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    path = item.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    line = item.get("line", 1)
    if not isinstance(line, int) or line < 1:
        line = 1
    return {"path": path, "line": line}


def _clean_breakpoint(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    path = item.get("path")
    line = item.get("line")
    if not isinstance(path, str) or not path.strip():
        return None
    if not isinstance(line, int) or line < 1:
        return None
    return {"path": path, "line": line}


def _clean_session(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {"tabs": [], "active": "", "breakpoints": []}
    tabs: list[dict] = []
    for raw in payload.get("tabs") or []:
        cleaned = _clean_tab(raw)
        if cleaned is not None:
            tabs.append(cleaned)
        if len(tabs) >= MAX_TABS:
            break
    breakpoints: list[dict] = []
    for raw in payload.get("breakpoints") or []:
        cleaned = _clean_breakpoint(raw)
        if cleaned is not None:
            breakpoints.append(cleaned)
        if len(breakpoints) >= MAX_BREAKPOINTS:
            break
    active = payload.get("active")
    if not isinstance(active, str) or active not in {t["path"] for t in tabs}:
        active = tabs[0]["path"] if tabs else ""
    return {"tabs": tabs, "active": active, "breakpoints": breakpoints}


def _empty() -> dict:
    return {"version": _STORE_VERSION, "sessions": {}}


def _read_all() -> dict:
    try:
        import config

        data = config._read_json_with_backup(_store_path(), default=None)
    except Exception:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        return _empty()
    sessions = {}
    for name, payload in data["sessions"].items():
        if isinstance(name, str) and name:
            sessions[name] = _clean_session(payload)
    return {"version": _STORE_VERSION, "sessions": sessions}


def get_ide_layout(session: str) -> dict:
    """读取指定会话的 IDE 布局；任何异常回落空布局。"""
    sessions = _read_all()["sessions"]
    payload = sessions.get(session or "")
    if payload is None:
        return {"version": _STORE_VERSION, "tabs": [], "active": "", "breakpoints": []}
    return {"version": _STORE_VERSION, **payload}


def save_ide_layout(session: str, payload: Any) -> dict:
    """全量覆盖指定会话的 IDE 布局，返回规范化后的会话快照。

    全量覆盖而非合并：标签是「整份列表」语义，合并会让已关闭的标签
    在文件里无限残留。
    """
    if not session:
        raise IdeLayoutError("session 不能为空")
    with _STORE_LOCK:
        data = _read_all()
        cleaned = _clean_session(payload)
        data["sessions"][session] = cleaned
        try:
            import config

            config._atomic_write_json(_store_path(), data, indent=2)
        except Exception as exc:  # pragma: no cover - 磁盘故障等极端情况
            raise IdeLayoutError(f"保存失败：{exc}") from exc
    return {"version": _STORE_VERSION, **cleaned}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_layout_store.py -v
```

预期：7 个用例全部 PASS。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add core/ide/layout_store.py tests/test_ide_layout_store.py
git commit -m "feat(ide): 新增 IDE 布局状态后端持久化（按会话隔离，原子写）"
```

---

## Task 7: 桥方法接入与已打开文件变更事件

**Files:**
- Modify: `gui_bridge.py`（在文件树 / 工作区分区之后追加 IDE 分区）
- Test: `tests/test_gui_ide_bridge.py`

**Interfaces:**
- Consumes: `core.ide.file_service`、`core.ide.layout_store`、`core.gui_path_policy.gui_path_access_allowed`、`gui_bridge._resolve_real_path`、`gui_bridge.ApiBridge.emit_event`
- Produces（前端可见的桥方法，均为 `ApiBridge` 的公开方法）：
  - `ide_read_file(path: str) -> dict`
  - `ide_write_file(path: str, content: str, expected_hash: str, encoding: str, bom: bool, eol: str) -> dict`
  - `ide_stat_files(paths: list[str]) -> dict`
  - `ide_set_open_files(paths: list[str]) -> dict`
  - `ide_get_layout(session: str) -> dict`
  - `ide_save_layout(session: str, layout: dict) -> dict`
  - 事件 `ide_file_changed`，载荷 `{"path": str}`

> 说明：`ide_set_open_files` 是设计规格 §5.5 未列出的补充——轮询集合必须由前端（唯一知道哪些标签打开着的一方）驱动，后端无法自行推断。设计规格需同步补记此项。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_gui_ide_bridge.py`：

```python
# -*- coding: utf-8 -*-
"""IDE 桥方法：路径策略、读写往返、布局存取、变更事件派发。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.gui_path_policy as gpp
from gui_bridge import ApiBridge


def _make_bridge(workdir: str, monkeypatch, tmp_path) -> ApiBridge:
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))
    bridge = ApiBridge(mcp_manager=None)
    bridge._session_workdir = lambda session_name="": workdir  # type: ignore[method-assign]
    bridge._current_session = "test-session"
    return bridge


def test_ide_read_write_roundtrip(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)

    read = bridge.ide_read_file(str(f))
    assert read["ok"] is True
    assert read["content"] == "x = 1\n"

    written = bridge.ide_write_file(
        str(f), "x = 2\n", read["hash"], read["encoding"], read["bom"], read["eol"]
    )
    assert written["ok"] is True
    assert f.read_text(encoding="utf-8") == "x = 2\n"


def test_ide_write_outside_workspace_denied(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)

    res = bridge.ide_write_file(str(f), "hacked\n", "", "utf-8", False, "lf")
    assert res["ok"] is False
    assert "工作区" in res["msg"]
    assert f.read_text(encoding="utf-8") == "x = 1\n"


def test_ide_read_credential_config_denied(tmp_path, monkeypatch):
    ydata = tmp_path / "ydata"
    ydata.mkdir()
    f = ydata / "config.json"
    f.write_text('{"api_key": "secret"}', encoding="utf-8")
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(ydata))
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: True)
    bridge = _make_bridge("GLOBAL", monkeypatch, tmp_path)
    res = bridge.ide_read_file(str(f))
    assert res["ok"] is False


def test_ide_write_conflict_reported(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)
    read = bridge.ide_read_file(str(f))
    f.write_text("x = 1  # agent\n", encoding="utf-8")
    res = bridge.ide_write_file(
        str(f), "x = 2\n", read["hash"], read["encoding"], read["bom"], read["eol"]
    )
    assert res["ok"] is False
    assert "conflict" in res


def test_ide_stat_files_reports_size_and_hash(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)
    res = bridge.ide_stat_files([str(f), str(ws / "nope.py")])
    assert res["ok"] is True
    assert res["stats"][0]["size"] == 6
    assert len(res["stats"][0]["hash"]) == 64
    assert res["stats"][1]["missing"] is True


def test_ide_set_open_files_returns_count(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)
    res = bridge.ide_set_open_files([str(f)])
    assert res["ok"] is True
    assert res["count"] == 1


def test_ide_poll_once_emits_event_on_change(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)
    events = []
    bridge.emit_event = lambda name, payload=None: events.append((name, payload))  # type: ignore[method-assign]

    bridge.ide_set_open_files([str(f)])
    base = os.stat(str(f)).st_mtime
    os.utime(str(f), (base + 10, base + 10))

    assert bridge._ide_poll_once() == 1
    assert events == [("ide_file_changed", {"path": os.path.realpath(str(f))})]
    # 无变化时不再派发
    assert bridge._ide_poll_once() == 0
    assert len(events) == 1


def test_ide_layout_roundtrip(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path))
    saved = bridge.ide_save_layout("s1", {"tabs": [{"path": "a.py", "line": 3}], "active": "a.py", "breakpoints": []})
    assert saved["ok"] is True
    got = bridge.ide_get_layout("s1")
    assert got["ok"] is True
    assert got["layout"]["active"] == "a.py"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_gui_ide_bridge.py -v
```

预期：FAIL，报错 `AttributeError: 'ApiBridge' object has no attribute 'ide_read_file'`。

- [ ] **Step 3: 实现桥方法与轮询派发**

在 `gui_bridge.py` 的「文件树 / 工作区」分区之后追加 IDE 分区。注意 `emit_event` 是本类既有方法，事件会自动经 `_js_queue` 与 `gui-js-dispatcher` 线程派发，这里不需要（也不允许）直接调用 `evaluate_js`：

```python
    # ------------------------------------------------------------------
    # IDE 模式：编辑器文件读写 / 布局 / 已打开文件变更
    # ------------------------------------------------------------------
    _IDE_WATCH_INTERVAL = 1.0

    def _ide_watcher_instance(self):
        """惰性创建轮询器与线程；线程只负责轮询，派发交给 emit_event。"""
        from core.ide import file_service

        watcher = getattr(self, "_ide_watcher", None)
        if watcher is None:
            watcher = file_service.OpenFileWatcher(interval=self._IDE_WATCH_INTERVAL)
            self._ide_watcher = watcher
        return watcher

    def _ide_ensure_watch_thread(self) -> None:
        if getattr(self, "_ide_watch_thread", None) is not None:
            return
        import threading

        stop_event = threading.Event()
        self._ide_watch_stop = stop_event

        def _loop():
            while not stop_event.wait(self._IDE_WATCH_INTERVAL):
                try:
                    self._ide_poll_once()
                except Exception:
                    logger.exception("IDE 已打开文件轮询失败")

        thread = threading.Thread(target=_loop, name="ide-open-file-watcher", daemon=True)
        self._ide_watch_thread = thread
        thread.start()

    def _ide_poll_once(self) -> int:
        """轮询一次并派发变更事件，返回变更文件数（线程循环与测试共用）。"""
        watcher = self._ide_watcher_instance()
        changed = watcher.poll()
        for path in changed:
            self.emit_event("ide_file_changed", {"path": path})
        return len(changed)

    def ide_set_open_files(self, paths) -> Dict[str, Any]:
        """登记当前编辑器打开的文件（轮询集合由前端驱动）。"""
        try:
            watcher = self._ide_watcher_instance()
            watcher.set_open_files(paths or [])
            self._ide_ensure_watch_thread()
            return {"ok": True, "count": len(watcher._files)}
        except Exception as e:
            logger.exception("ide_set_open_files 失败")
            return {"ok": False, "msg": str(e)}

    def _ide_guard(self, path: str, action: str) -> "tuple[str, str]":
        """统一路径策略校验，返回 (real_path, 拒绝原因)。"""
        from core.gui_path_policy import gui_path_access_allowed

        real_path = _resolve_real_path(path)
        workspace = self._session_workdir(self._current_session)
        allowed, deny_msg = gui_path_access_allowed(
            real_path, workspace, action=action, session_id=self._current_session
        )
        if not allowed:
            return real_path, deny_msg
        return real_path, ""

    def ide_read_file(self, path: str) -> Dict[str, Any]:
        from core.ide import file_service

        try:
            real_path, deny = self._ide_guard(path, "edit")
            if deny:
                return {"ok": False, "msg": deny}
            return {"ok": True, **file_service.read_text_file(real_path)}
        except file_service.FileServiceError as e:
            return {"ok": False, "msg": str(e)}
        except Exception as e:
            logger.exception("ide_read_file 失败")
            return {"ok": False, "msg": str(e)}

    def ide_write_file(
        self,
        path: str,
        content: str,
        expected_hash: str = "",
        encoding: str = "utf-8",
        bom: bool = False,
        eol: str = "lf",
    ) -> Dict[str, Any]:
        from core.ide import file_service

        try:
            real_path, deny = self._ide_guard(path, "edit")
            if deny:
                return {"ok": False, "msg": deny}
            return file_service.write_text_file(
                real_path, content, expected_hash,
                encoding=encoding, bom=bom, eol=eol,
            )
        except file_service.FileServiceError as e:
            return {"ok": False, "msg": str(e)}
        except Exception as e:
            logger.exception("ide_write_file 失败")
            return {"ok": False, "msg": str(e)}

    def ide_stat_files(self, paths) -> Dict[str, Any]:
        try:
            from core.ide import file_service

            stats = []
            for raw in paths or []:
                real_path, deny = self._ide_guard(raw, "preview")
                if deny:
                    stats.append({"path": raw, "denied": True, "msg": deny})
                    continue
                try:
                    st = os.stat(real_path)
                    with open(real_path, "rb") as fh:
                        digest = file_service._hash_bytes(fh.read())
                    stats.append({
                        "path": real_path,
                        "mtime": st.st_mtime,
                        "size": st.st_size,
                        "hash": digest,
                    })
                except OSError:
                    stats.append({"path": real_path, "missing": True})
            return {"ok": True, "stats": stats}
        except Exception as e:
            logger.exception("ide_stat_files 失败")
            return {"ok": False, "stats": [], "msg": str(e)}

    def ide_get_layout(self, session: str) -> Dict[str, Any]:
        try:
            from core.ide import layout_store

            return {"ok": True, "layout": layout_store.get_ide_layout(session)}
        except Exception as e:
            logger.exception("ide_get_layout 失败")
            return {"ok": False, "layout": None, "msg": str(e)}

    def ide_save_layout(self, session: str, layout) -> Dict[str, Any]:
        try:
            from core.ide import layout_store

            return {"ok": True, "layout": layout_store.save_ide_layout(session, layout)}
        except Exception as e:
            logger.exception("ide_save_layout 失败")
            return {"ok": False, "msg": str(e)}
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_gui_ide_bridge.py -v
```

预期：8 个用例全部 PASS。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add gui_bridge.py tests/test_gui_ide_bridge.py
git commit -m "feat(ide): 桥接层新增 IDE 文件读写/布局方法并派发已打开文件变更事件"
```

---

## Task 8: 前端桥类型声明与 ideStore

**Files:**
- Modify: `frontend/src/types/pywebview.d.ts`
- Create: `frontend/src/stores/ideStore.ts`
- Test: `frontend/src/stores/ideStore.test.ts`

**Interfaces:**
- Consumes: Task 7 的桥方法
- Produces（`useIdeStore()`，这是 S2 / S3 共同消费的**冻结契约**，后续任务不得改名）：
  - state：`tabsBySession`、`activeBySession`、`activeTab: IdeTab | null`、`breakpointsBySession: Record<string, {path, line}[]>`、`bottomTabBySession`、`bottomPanelOpenBySession`、`pendingJump: {path, line, character} | null`
  - getters：`breakpoints: Set<string>`（键为 `${path}:${line}`）、`bottomTab: string`、`bottomPanelOpen: boolean`
  - actions：`setSession(session)`、`openFile(path)`、`closeTab(path)`、`activate(path)`、`updateContent(path, content)`、`saveTab(path)`、`reloadFromDisk(path)`、`handleExternalChange(path)`、`persistLayout()`、`breakpointsFor(path): number[]`、`toggleBreakpoint(path, line)`、`jumpTo(path, line, character)`、`clearPendingJump()`、`setBottomTab(tab)`、`toggleBottomPanel()`、`tabs()`、`activePath()`、`tab(path)`
  - 类型 `IdeTab { path, content, encoding, bom, eol, hash, dirty, externalChange, conflict }`
  - 说明：`activeTab` 是对象形态（S3 的调试界面与断点 gutter 读 `activeTab.path`），`activePath()` 是字符串形态，两者由 `_syncActiveTab()` 保持同步

- [ ] **Step 1: 写失败的测试**

创建 `frontend/src/stores/ideStore.test.ts`：

```ts
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';

import { useIdeStore } from './ideStore';

function installBridge(overrides: Record<string, unknown> = {}) {
  const api = {
    ide_read_file: vi.fn(async (path: string) => ({
      ok: true,
      path,
      content: 'x = 1\n',
      encoding: 'utf-8',
      bom: false,
      eol: 'lf',
      hash: 'h1',
    })),
    ide_write_file: vi.fn(async () => ({ ok: true, hash: 'h2' })),
    ide_set_open_files: vi.fn(async () => ({ ok: true, count: 1 })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = undefined;
});

it('openFile 读取内容并登记为标签', async () => {
  const api = installBridge();
  const store = useIdeStore();
  await store.openFile('E:\\p\\a.py');

  expect(api.ide_read_file).toHaveBeenCalledWith('E:\\p\\a.py');
  expect(store.tabs().map((t) => t.path)).toEqual(['E:\\p\\a.py']);
  expect(store.activePath()).toBe('E:\\p\\a.py');
  expect(store.tab('E:\\p\\a.py')?.dirty).toBe(false);
  // 打开后必须把打开集合同步给后端轮询
  expect(api.ide_set_open_files).toHaveBeenCalledWith(['E:\\p\\a.py']);
});

it('重复打开同一文件不会产生第二个标签', async () => {
  installBridge();
  const store = useIdeStore();
  await store.openFile('a.py');
  await store.openFile('a.py');
  expect(store.tabs().length).toBe(1);
});

it('updateContent 置脏，saveTab 用打开时的 hash 保存并清脏', async () => {
  const api = installBridge();
  const store = useIdeStore();
  await store.openFile('a.py');
  store.updateContent('a.py', 'x = 2\n');
  expect(store.tab('a.py')?.dirty).toBe(true);

  await store.saveTab('a.py');
  expect(api.ide_write_file).toHaveBeenCalledWith('a.py', 'x = 2\n', 'h1', 'utf-8', false, 'lf');
  expect(store.tab('a.py')?.dirty).toBe(false);
  expect(store.tab('a.py')?.hash).toBe('h2');
});

it('保存冲突时不覆盖本地内容，标记冲突状态', async () => {
  installBridge({
    ide_write_file: vi.fn(async () => ({ ok: false, conflict: { disk_hash: 'h9' }, msg: '文件已被外部修改' })),
  });
  const store = useIdeStore();
  await store.openFile('a.py');
  store.updateContent('a.py', 'x = 2\n');
  await store.saveTab('a.py');

  expect(store.tab('a.py')?.dirty).toBe(true);
  expect(store.tab('a.py')?.conflict).toBe(true);
  expect(store.tab('a.py')?.content).toBe('x = 2\n');
});

it('外部改动：文件干净时自动重载，脏时只标记不覆盖', async () => {
  const api = installBridge();
  const store = useIdeStore();
  await store.openFile('a.py');

  // 干净：自动重载为新内容
  api.ide_read_file.mockResolvedValueOnce({
    ok: true, path: 'a.py', content: 'x = 99\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h3',
  } as any);
  await store.handleExternalChange('a.py');
  expect(store.tab('a.py')?.content).toBe('x = 99\n');
  expect(store.tab('a.py')?.externalChange).toBe(false);

  // 脏：保留用户内容，只标记
  store.updateContent('a.py', 'x = 100\n');
  await store.handleExternalChange('a.py');
  expect(store.tab('a.py')?.content).toBe('x = 100\n');
  expect(store.tab('a.py')?.externalChange).toBe(true);
});

it('会话之间标签互相隔离', async () => {
  installBridge();
  const store = useIdeStore();
  store.setSession('s1');
  await store.openFile('a.py');
  store.setSession('s2');
  expect(store.tabs().length).toBe(0);
  store.setSession('s1');
  expect(store.tabs().length).toBe(1);
});

it('closeTab 关闭最后一个标签后活动路径为空', async () => {
  installBridge();
  const store = useIdeStore();
  await store.openFile('a.py');
  store.closeTab('a.py');
  expect(store.tabs()).toEqual([]);
  expect(store.activePath()).toBe('');
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/stores/ideStore.test.ts
```

预期：FAIL，报错 `Failed to resolve import "./ideStore"`。

- [ ] **Step 3: 声明桥类型**

在 `frontend/src/types/pywebview.d.ts` 中，找到 `interface PyWebViewAPI {`，在其中追加：

```ts
  // ---- IDE 模式 ----
  ide_read_file(path: string): Promise<IdeReadFileResult>;
  ide_write_file(
    path: string,
    content: string,
    expectedHash: string,
    encoding: string,
    bom: boolean,
    eol: string,
  ): Promise<IdeWriteFileResult>;
  ide_stat_files(paths: string[]): Promise<{ ok: boolean; stats: IdeFileStat[] }>;
  ide_set_open_files(paths: string[]): Promise<{ ok: boolean; count: number }>;
  ide_get_layout(session: string): Promise<{ ok: boolean; layout: IdeLayoutSnapshot | null }>;
  ide_save_layout(session: string, layout: IdeLayoutSnapshot): Promise<{ ok: boolean; layout: IdeLayoutSnapshot }>;
```

并在同文件内追加类型定义（放在文件末尾导出区附近即可）：

```ts
export interface IdeReadFileResult {
  ok: boolean;
  path?: string;
  content?: string;
  encoding?: 'utf-8' | 'gbk';
  bom?: boolean;
  eol?: 'lf' | 'crlf' | 'cr' | 'mixed';
  hash?: string;
  size?: number;
  msg?: string;
}

export interface IdeWriteFileResult {
  ok: boolean;
  hash?: string;
  conflict?: { disk_hash: string };
  msg?: string;
}

export interface IdeFileStat {
  path: string;
  mtime?: number;
  size?: number;
  hash?: string;
  missing?: boolean;
  denied?: boolean;
  msg?: string;
}

export interface IdeLayoutSnapshot {
  version: number;
  tabs: { path: string; line: number }[];
  active: string;
  breakpoints: { path: string; line: number }[];
}
```

- [ ] **Step 4: 实现 ideStore**

创建 `frontend/src/stores/ideStore.ts`：

```ts
import { defineStore } from 'pinia';

import { languageIdForPath } from '../editor/setup';
import type { IdeReadFileResult } from '../types/pywebview';

export interface IdeTab {
  path: string;
  content: string;
  encoding: 'utf-8' | 'gbk';
  bom: boolean;
  eol: 'lf' | 'crlf' | 'cr' | 'mixed';
  hash: string;
  dirty: boolean;
  /** 磁盘内容被外部改动且本地有未保存内容时为 true */
  externalChange: boolean;
  /** 保存时检测到冲突 */
  conflict: boolean;
}

function ideApi() {
  const api = (window as any).pywebview?.api;
  if (!api || typeof api.ide_read_file !== 'function') return null;
  return api;
}

export const useIdeStore = defineStore('ide', {
  state: () => ({
    session: '',
    tabsBySession: {} as Record<string, IdeTab[]>,
    activeBySession: {} as Record<string, string>,
    /** 当前活动标签对象：S3 的调试界面与断点 gutter 直接读 activeTab.path */
    activeTab: null as IdeTab | null,
    /** 断点按会话隔离，元素形状与 core/ide/layout_store.py 的校验结构一致 */
    breakpointsBySession: {} as Record<string, { path: string; line: number }[]>,
    bottomTabBySession: {} as Record<string, string>,
    bottomPanelOpenBySession: {} as Record<string, boolean>,
    /** 待执行的跳转请求：S2 的「跳转定义」把目标行列写在这里，EditorPane 消费后清空 */
    pendingJump: null as { path: string; line: number; character: number } | null,
  }),

  getters: {
    /** `${path}:${line}` 集合，供断点 gutter 做 O(1) 判定 */
    breakpoints(state): Set<string> {
      return new Set(
        (state.breakpointsBySession[state.session] ?? []).map((b) => `${b.path}:${b.line}`),
      );
    },

    bottomTab(state): string {
      return state.bottomTabBySession[state.session] ?? 'terminal';
    },

    bottomPanelOpen(state): boolean {
      return state.bottomPanelOpenBySession[state.session] ?? true;
    },
  },

  actions: {
    setSession(session: string) {
      this.session = session || '';
      if (!this.tabsBySession[this.session]) this.tabsBySession[this.session] = [];
      this._syncActiveTab();
    },

    /** 打开目标文件并把光标定位到指定行列（S2 的跳转定义 / 诊断跳转复用） */
    async jumpTo(path: string, line: number, character = 1) {
      await this.openFile(path);
      this.pendingJump = { path: this.tab(path)?.path ?? path, line, character };
    },

    /** EditorPane 消费完跳转请求后调用，避免重复定位 */
    clearPendingJump() {
      this.pendingJump = null;
    },

    setBottomTab(tab: string) {
      this.bottomTabBySession[this.session] = tab;
    },

    toggleBottomPanel() {
      this.bottomPanelOpenBySession[this.session] = !this.bottomPanelOpen;
    },

    breakpointsFor(path: string): number[] {
      return (this.breakpointsBySession[this.session] ?? [])
        .filter((b) => b.path === path)
        .map((b) => b.line)
        .sort((a, b) => a - b);
    },

    toggleBreakpoint(path: string, line: number) {
      if (!this.breakpointsBySession[this.session]) this.breakpointsBySession[this.session] = [];
      const list = this.breakpointsBySession[this.session];
      const idx = list.findIndex((b) => b.path === path && b.line === line);
      if (idx >= 0) list.splice(idx, 1);
      else list.push({ path, line });
      void this.persistLayout();
    },

    /** 活动标签对象与 activeBySession 保持同步，避免两处状态漂移 */
    _syncActiveTab() {
      this.activeTab = this.tab(this.activePath()) ?? null;
    },

    tabs(): IdeTab[] {
      return this.tabsBySession[this.session] ?? [];
    },

    tab(path: string): IdeTab | undefined {
      return this.tabs().find((t) => t.path === path);
    },

    activePath(): string {
      return this.activeBySession[this.session] ?? '';
    },

    activate(path: string) {
      this.activeBySession[this.session] = path;
      this._syncActiveTab();
    },

    languageId(path: string): string {
      return languageIdForPath(path);
    },

    async _syncOpenFiles() {
      const api = ideApi();
      if (!api) return;
      await api.ide_set_open_files(this.tabs().map((t) => t.path));
    },

    async openFile(path: string) {
      const api = ideApi();
      if (!api) return;
      const existing = this.tab(path);
      if (existing) {
        this.activate(path);
        return;
      }
      const res: IdeReadFileResult = await api.ide_read_file(path);
      if (!res.ok) {
        throw new Error(res.msg || '打开文件失败');
      }
      const tab: IdeTab = {
        path: res.path || path,
        content: res.content ?? '',
        encoding: (res.encoding as 'utf-8' | 'gbk') ?? 'utf-8',
        bom: !!res.bom,
        eol: (res.eol as IdeTab['eol']) ?? 'lf',
        hash: res.hash ?? '',
        dirty: false,
        externalChange: false,
        conflict: false,
      };
      this.tabs().push(tab);
      this.activate(tab.path);
      await this._syncOpenFiles();
    },

    closeTab(path: string) {
      const list = this.tabs();
      const idx = list.findIndex((t) => t.path === path);
      if (idx < 0) return;
      list.splice(idx, 1);
      if (this.activePath() === path) {
        this.activeBySession[this.session] = list[Math.min(idx, list.length - 1)]?.path ?? '';
      }
      this._syncActiveTab();
      void this._syncOpenFiles();
    },

    updateContent(path: string, content: string) {
      const tab = this.tab(path);
      if (!tab) return;
      tab.content = content;
      tab.dirty = true;
    },

    async saveTab(path: string) {
      const api = ideApi();
      const tab = this.tab(path);
      if (!api || !tab) return;
      const res = await api.ide_write_file(
        tab.path,
        tab.content,
        tab.hash,
        tab.encoding,
        tab.bom,
        tab.eol,
      );
      if (res.ok) {
        tab.hash = res.hash ?? tab.hash;
        tab.dirty = false;
        tab.conflict = false;
        tab.externalChange = false;
        return;
      }
      // 冲突：保留本地内容，交由界面让用户选择
      if (res.conflict) {
        tab.conflict = true;
        tab.externalChange = true;
        return;
      }
      throw new Error(res.msg || '保存失败');
    },

    /** 用磁盘内容覆盖当前标签（冲突时用户选择「以磁盘为准」） */
    async reloadFromDisk(path: string) {
      const api = ideApi();
      const tab = this.tab(path);
      if (!api || !tab) return;
      const res: IdeReadFileResult = await api.ide_read_file(tab.path);
      if (!res.ok) return;
      tab.content = res.content ?? '';
      tab.encoding = (res.encoding as 'utf-8' | 'gbk') ?? tab.encoding;
      tab.bom = !!res.bom;
      tab.eol = (res.eol as IdeTab['eol']) ?? tab.eol;
      tab.hash = res.hash ?? tab.hash;
      tab.dirty = false;
      tab.conflict = false;
      tab.externalChange = false;
    },

    /** 外部（星瑶或其他程序）改动了磁盘文件 */
    async handleExternalChange(path: string) {
      const tab = this.tab(path);
      if (!tab) return;
      if (tab.dirty) {
        // 有未保存内容：绝不静默覆盖，只标记
        tab.externalChange = true;
        return;
      }
      await this.reloadFromDisk(path);
    },
  },
});
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/stores/ideStore.test.ts
```

预期：7 个用例全部 PASS。

- [ ] **Step 6: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add frontend/src/types/pywebview.d.ts frontend/src/stores/ideStore.ts frontend/src/stores/ideStore.test.ts
git commit -m "feat(ide): 前端新增 ideStore 与 IDE 桥方法类型声明"
```

---

## Task 9: 编辑器主题与 EditorPane

**Files:**
- Create: `frontend/src/editor/theme.ts`
- Create: `frontend/src/components/EditorPane.vue`
- Test: `frontend/src/components/EditorPane.test.ts`

**Interfaces:**
- Consumes: `createEditorState`（Task 1）、`useIdeStore`（Task 8）
- Produces: `buildEditorTheme(isLight: boolean): Extension`；组件 `EditorPane.vue`（props: `path: string`）

- [ ] **Step 1: 写失败的测试**

创建 `frontend/src/components/EditorPane.test.ts`：

```ts
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';

vi.mock('../editor/setup', async () => {
  const actual = await vi.importActual<typeof import('../editor/setup')>('../editor/setup');
  return {
    ...actual,
    // jsdom 下不做真实挂载，只验证组件把文档与回调正确接线
    createEditorState: vi.fn(async (doc: string) => ({ doc: { toString: () => doc } })),
  };
});

import EditorPane from './EditorPane.vue';
import { useIdeStore } from '../stores/ideStore';

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = {
    api: {
      ide_read_file: vi.fn(async (path: string) => ({
        ok: true, path, content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
      })),
      ide_write_file: vi.fn(async () => ({ ok: true, hash: 'h2' })),
      ide_set_open_files: vi.fn(async () => ({ ok: true, count: 1 })),
    },
  };
});

it('Ctrl+S 触发保存且阻止浏览器默认行为', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  store.updateContent('a.py', 'x = 2\n');
  const wrapper = mount(EditorPane, { props: { path: 'a.py' } });

  const event = new KeyboardEvent('keydown', { key: 's', ctrlKey: true, cancelable: true });
  window.dispatchEvent(event);
  await Promise.resolve();

  expect((window as any).pywebview.api.ide_write_file).toHaveBeenCalled();
  expect(event.defaultPrevented).toBe(true);
  wrapper.unmount();
});

it('文件被外部改动且本地脏时显示提示条', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  store.updateContent('a.py', 'x = 2\n');
  await store.handleExternalChange('a.py');
  const wrapper = mount(EditorPane, { props: { path: 'a.py' } });

  expect(wrapper.text()).toContain('已被星瑶修改');
  wrapper.unmount();
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/components/EditorPane.test.ts
```

预期：FAIL，报错 `Failed to resolve import "./EditorPane.vue"`。

- [ ] **Step 3: 实现主题与组件**

创建 `frontend/src/editor/theme.ts`：

```ts
import type { Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

/**
 * 从 GUI 主题的 CSS 变量构造编辑器主题。
 *
 * 变量名取自 frontend/src/style.css（深色在 :root，浅色在 body.light 覆盖），
 * 这样主题切换时编辑器自动跟随，不需要维护第二套配色。
 */
function readVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.body).getPropertyValue(name).trim();
  return value || fallback;
}

export function buildEditorTheme(isLight: boolean): Extension {
  const bg = readVar('--bg1', isLight ? '#ffffff' : '#20201e');
  const text = readVar('--text', isLight ? '#2c2c2a' : '#d9d9d4');
  const dim = readVar('--text-dim', isLight ? '#5c5c56' : '#a8a7a0');
  const faint = readVar('--text-faint', isLight ? '#7a7a73' : '#85847d');
  const border = readVar('--border', isLight ? '#e3e3de' : '#34332f');
  const accent = readVar('--accent', '#6a92d8');
  const accentSoft = readVar('--accent-soft', 'rgba(106,146,216,0.16)');

  return EditorView.theme(
    {
      '&': { backgroundColor: bg, color: text, height: '100%' },
      '.cm-content': { caretColor: accent },
      '.cm-cursor, .cm-dropCursor': { borderLeftColor: accent },
      '.cm-gutters': { backgroundColor: bg, color: faint, border: 'none', borderRight: `1px solid ${border}` },
      '.cm-activeLine': { backgroundColor: accentSoft },
      '.cm-activeLineGutter': { backgroundColor: accentSoft, color: text },
      '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': { backgroundColor: accentSoft },
      '.cm-selectionMatch': { backgroundColor: accentSoft },
      '.cm-scroller': { fontFamily: 'Consolas, "Cascadia Mono", monospace', lineHeight: '1.5' },
      '.cm-tooltip': { backgroundColor: bg, border: `1px solid ${border}`, color: text },
      '.cm-panels': { backgroundColor: bg, color: text },
      '.cm-panels input, .cm-panels button': { backgroundColor: bg, color: text, border: `1px solid ${border}` },
    },
    { dark: !isLight },
  );
}
```

创建 `frontend/src/components/EditorPane.vue`：

```vue
<template>
  <div class="editor-pane">
    <div v-if="tab?.externalChange && tab?.dirty" class="external-bar">
      该文件已被星瑶修改
      <button type="button" @click="onReload">以磁盘为准</button>
      <button type="button" @click="onKeepMine">保留我的内容</button>
    </div>
    <div v-if="tab?.conflict" class="external-bar conflict">
      保存冲突：磁盘内容已变
      <button type="button" @click="onReload">以磁盘为准</button>
      <button type="button" @click="onOverwrite">用我的覆盖</button>
    </div>
    <div ref="hostRef" class="editor-host"></div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { buildEditorTheme } from '../editor/theme';
import { createEditorState, EditorView } from '../editor/setup';
import { useIdeStore } from '../stores/ideStore';

const props = defineProps<{ path: string }>();
const store = useIdeStore();
const hostRef = ref<HTMLElement | null>(null);
let view: EditorView | null = null;
let applyingExternal = false;

const tab = computed(() => store.tab(props.path));

function isLightTheme() {
  return document.body.classList.contains('light');
}

async function mountView() {
  if (!hostRef.value || !props.path) return;
  const current = store.tab(props.path);
  if (!current) return;
  const state = await createEditorState(current.content, store.languageId(props.path));
  view?.destroy();
  view = new EditorView({
    state,
    parent: hostRef.value,
    extensions: [
      buildEditorTheme(isLightTheme()),
      EditorView.updateListener.of((update) => {
        if (!update.docChanged || applyingExternal) return;
        store.updateContent(props.path, update.state.doc.toString());
      }),
    ],
  });
}

onMounted(() => {
  void mountView();
  window.addEventListener('keydown', onKeydown, true);
});

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown, true);
  view?.destroy();
  view = null;
});

watch(
  () => props.path,
  () => void mountView(),
);

// 外部改动或重载后，把 store 的内容同步进编辑器
watch(
  () => tab.value?.content,
  (next) => {
    if (!view || next === undefined) return;
    if (next === view.state.doc.toString()) return;
    applyingExternal = true;
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: next } });
    applyingExternal = false;
  },
);

// 跳转定义 / 诊断定位：把 store 里的 pendingJump 落到编辑器选区
watch(
  () => store.pendingJump,
  (jump) => {
    if (!view || !jump || jump.path !== props.path) return;
    const total = view.state.doc.lines;
    const lineInfo = view.state.doc.line(Math.min(Math.max(jump.line, 1), total));
    const anchor = Math.min(lineInfo.from + Math.max(jump.character - 1, 0), lineInfo.to);
    view.dispatch({ selection: { anchor }, scrollIntoView: true });
    view.focus();
    store.clearPendingJump();
  },
);

function onKeydown(event: KeyboardEvent) {
  if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 's') return;
  event.preventDefault();
  void store.saveTab(props.path);
}

function onReload() {
  void store.reloadFromDisk(props.path);
}

function onKeepMine() {
  const current = store.tab(props.path);
  if (current) current.externalChange = false;
}

function onOverwrite() {
  const current = store.tab(props.path);
  if (!current) return;
  current.hash = '';
  void store.saveTab(props.path);
}
</script>

<style scoped>
.editor-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.editor-host {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.external-bar {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 6px 10px;
  font-size: 12px;
  background: color-mix(in srgb, var(--accent) 12%, var(--bg1));
  border-bottom: 1px solid var(--border);
}

.external-bar.conflict {
  background: color-mix(in srgb, var(--red, #d9534f) 14%, var(--bg1));
}

.external-bar button {
  background: var(--bg2);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 2px 8px;
  cursor: pointer;
}
</style>
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/components/EditorPane.test.ts src/editor/setup.test.ts
```

预期：全部 PASS。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add frontend/src/editor/theme.ts frontend/src/components/EditorPane.vue frontend/src/components/EditorPane.test.ts
git commit -m "feat(ide): 新增编辑器主题与 EditorPane（Ctrl+S 保存、外部改动提示）"
```

---

## Task 10: 标签页条 EditorTabs

**Files:**
- Create: `frontend/src/components/EditorTabs.vue`
- Test: `frontend/src/components/EditorTabs.test.ts`

**Interfaces:**
- Consumes: `useIdeStore`
- Produces: 组件 `EditorTabs.vue`（无 props，直接读 store）

- [ ] **Step 1: 写失败的测试**

创建 `frontend/src/components/EditorTabs.test.ts`：

```ts
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';

import EditorTabs from './EditorTabs.vue';
import { useIdeStore } from '../stores/ideStore';

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = {
    api: {
      ide_read_file: vi.fn(async (path: string) => ({
        ok: true, path, content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
      })),
      ide_set_open_files: vi.fn(async () => ({ ok: true, count: 1 })),
      ide_write_file: vi.fn(async () => ({ ok: true, hash: 'h2' })),
    },
  };
});

it('渲染已打开文件的文件名，脏标签带圆点', async () => {
  const store = useIdeStore();
  await store.openFile('E:\\p\\a.py');
  await store.openFile('E:\\p\\b.ts');
  store.updateContent('E:\\p\\b.ts', 'y');

  const wrapper = mount(EditorTabs);
  expect(wrapper.text()).toContain('a.py');
  expect(wrapper.text()).toContain('b.ts');
  expect(wrapper.findAll('.tab-dot').length).toBe(1);
  wrapper.unmount();
});

it('点击标签切换活动文件', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  await store.openFile('b.py');
  const wrapper = mount(EditorTabs);

  await wrapper.findAll('.tab')[0].trigger('click');
  expect(store.activePath()).toBe('a.py');
  wrapper.unmount();
});

it('点击关闭按钮移除标签且不触发切换', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  await store.openFile('b.py');
  const wrapper = mount(EditorTabs);

  await wrapper.findAll('.tab-close')[0].trigger('click');
  expect(store.tabs().map((t) => t.path)).toEqual(['b.py']);
  wrapper.unmount();
});
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/components/EditorTabs.test.ts
```

预期：FAIL，报错 `Failed to resolve import "./EditorTabs.vue"`。

- [ ] **Step 3: 实现组件**

创建 `frontend/src/components/EditorTabs.vue`：

```vue
<template>
  <div class="editor-tabs">
    <div
      v-for="tab in store.tabs()"
      :key="tab.path"
      class="tab"
      :class="{ active: tab.path === store.activePath() }"
      :title="tab.path"
      @click="store.activate(tab.path)"
    >
      <span class="tab-name">{{ baseName(tab.path) }}</span>
      <span v-if="tab.dirty" class="tab-dot" aria-label="未保存"></span>
      <span class="tab-close" @click.stop="store.closeTab(tab.path)">×</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useIdeStore } from '../stores/ideStore';

const store = useIdeStore();

function baseName(path: string) {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}
</script>

<style scoped>
.editor-tabs {
  display: flex;
  align-items: stretch;
  gap: 2px;
  overflow-x: auto;
  background: var(--bg1);
  border-bottom: 1px solid var(--border);
  scrollbar-width: thin;
}

.tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px 5px 12px;
  font-size: 12px;
  color: var(--text-dim);
  border-right: 1px solid var(--border);
  cursor: pointer;
  white-space: nowrap;
  user-select: none;
}

.tab:hover {
  background: var(--bg2);
  color: var(--text);
}

.tab.active {
  background: var(--bg2);
  color: var(--text-bright, var(--text));
  box-shadow: inset 0 -2px 0 var(--accent);
}

.tab-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
}

.tab-close {
  padding: 0 3px;
  border-radius: 4px;
  color: var(--text-faint);
}

.tab-close:hover {
  background: var(--bg3);
  color: var(--text);
}
</style>
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/components/EditorTabs.test.ts
```

预期：3 个用例全部 PASS。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add frontend/src/components/EditorTabs.vue frontend/src/components/EditorTabs.test.ts
git commit -m "feat(ide): 新增编辑器标签页条"
```

---

## Task 11: IDE 布局、模式切换与底部面板组

**Files:**
- Create: `frontend/src/components/ActivityBar.vue`
- Create: `frontend/src/components/BottomPanel.vue`
- Create: `frontend/src/components/IdeLayout.vue`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/components/Topbar.vue`
- Test: `frontend/src/components/IdeLayout.test.ts`

**Interfaces:**
- Consumes: `useIdeStore`、`useAppStore`、`EditorTabs.vue`、`EditorPane.vue`、`FileTreeNode.vue`、`TerminalPane.vue`
- Produces: `IdeLayout.vue`（无 props）；`appStore.ideMode: boolean` 与 `appStore.toggleIdeMode()`

- [ ] **Step 1: 盘点现有快捷键（决定模式切换绑定）**

```bash
cd /d E:\fch\Y-code_llm
findstr /s /i /n "keydown keyup ctrlKey metaKey" frontend\src\*.vue frontend\src\*.ts
```

预期：输出一批命中行。逐行确认 `Ctrl+Shift+I` 是否被占用；若已被占用，改用 `Ctrl+Alt+I` 并在下面 Step 3 的实现里替换（快捷键常量集中在 `IdeLayout.vue` 顶部的 `TOGGLE_SHORTCUT` 注释处）。把最终选定的组合与理由写进本次提交信息。

- [ ] **Step 2: 写失败的测试**

创建 `frontend/src/components/IdeLayout.test.ts`：

```ts
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';

vi.mock('./EditorPane.vue', () => ({ default: { name: 'EditorPane', template: '<div class="editor-pane-stub" />' } }));
vi.mock('./TerminalPane.vue', () => ({ default: { name: 'TerminalPane', template: '<div class="terminal-stub" />' } }));

import IdeLayout from './IdeLayout.vue';
import { useIdeStore } from '../stores/ideStore';

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = {
    api: {
      ide_read_file: vi.fn(async (path: string) => ({
        ok: true, path, content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
      })),
      ide_set_open_files: vi.fn(async () => ({ ok: true, count: 1 })),
      ide_write_file: vi.fn(async () => ({ ok: true, hash: 'h2' })),
    },
  };
});

it('没有打开标签时显示空状态，不挂载编辑器', () => {
  const wrapper = mount(IdeLayout);
  expect(wrapper.find('.editor-pane-stub').exists()).toBe(false);
  expect(wrapper.text()).toContain('打开文件');
  wrapper.unmount();
});

it('打开文件后挂载编辑器，底部面板可切换到终端', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  const wrapper = mount(IdeLayout);

  expect(wrapper.find('.editor-pane-stub').exists()).toBe(true);
  expect(wrapper.text()).toContain('终端');

  await wrapper.find('.bottom-tab.terminal').trigger('click');
  expect(wrapper.find('.terminal-stub').exists()).toBe(true);
  wrapper.unmount();
});
```

- [ ] **Step 3: 实现三个组件并接线**

创建 `frontend/src/components/ActivityBar.vue`：

```vue
<template>
  <div class="activity-bar">
    <button
      v-for="item in items"
      :key="item.id"
      type="button"
      class="activity-btn"
      :class="{ active: model === item.id }"
      :title="item.title"
      @click="$emit('update:model', item.id)"
    >
      <AppIcon :name="item.icon" />
    </button>
  </div>
</template>

<script setup lang="ts">
import AppIcon from './AppIcon.vue';

defineProps<{ model: string }>();
defineEmits<{ (e: 'update:model', value: string): void }>();

// 图标名必须是 frontend/src/components/iconPaths.ts 里已注册的键（该文件第 2 行
// 明确要求保持纯字符串注册表以便控制体积），下面三个均已存在，不要新增图标
const items = [
  { id: 'files', icon: 'folder', title: '文件' },
  { id: 'git', icon: 'git-branch', title: 'Git' },
  { id: 'debug', icon: 'play', title: '调试' },
];
</script>

<style scoped>
.activity-bar {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 4px;
  background: var(--bg1);
  border-right: 1px solid var(--border);
}

.activity-btn {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
}

.activity-btn:hover {
  background: var(--bg2);
  color: var(--text);
}

.activity-btn.active {
  background: var(--bg3);
  color: var(--text-bright, var(--text));
  box-shadow: inset 2px 0 0 var(--accent);
}
</style>
```

> 注：图标名 `folder` / `git-branch` / `play` 均已在 `frontend/src/components/iconPaths.ts` 中注册（第 21、12、42 行），无需新增。

创建 `frontend/src/components/BottomPanel.vue`：

```vue
<template>
  <div class="bottom-panel">
    <div class="bottom-tabs">
      <button
        v-for="item in tabs"
        :key="item.id"
        type="button"
        class="bottom-tab"
        :class="[item.id, { active: store.bottomTab === item.id }]"
        @click="store.setBottomTab(item.id)"
      >
        {{ item.title }}
      </button>
      <button type="button" class="bottom-close" @click="$emit('close')">×</button>
    </div>
    <div class="bottom-body">
      <TerminalPane v-if="store.bottomTab === 'terminal'" />
      <div v-else class="bottom-placeholder">{{ placeholder }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue';

import TerminalPane from './TerminalPane.vue';
import { useAppStore } from '../stores/appStore';
import { useIdeStore } from '../stores/ideStore';

defineEmits<{ (e: 'close'): void }>();

const store = useIdeStore();
const appStore = useAppStore();

onMounted(() => {
  // TerminalPane 的启动条件是 bridgeReady && terminalPanelOpen，IDE 模式下必须显式打开
  if (!appStore.terminalPanelOpen) appStore.terminalPanelOpen = true;
});

const tabs = [
  { id: 'terminal', title: '终端' },
  { id: 'problems', title: '问题' },
  { id: 'output', title: '输出' },
  { id: 'debug', title: '调试控制台' },
];

const placeholder = computed(() => {
  if (store.bottomTab === 'problems') return '语言诊断将在 S2 接入后显示';
  if (store.bottomTab === 'output') return '输出通道将在 S2 接入后显示';
  return '调试控制台将在 S3 接入后显示';
});
</script>

<style scoped>
.bottom-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--bg1);
  border-top: 1px solid var(--border);
}

.bottom-tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  border-bottom: 1px solid var(--border);
}

.bottom-tab {
  background: transparent;
  border: none;
  color: var(--text-dim);
  font-size: 12px;
  padding: 3px 8px;
  border-radius: 6px;
  cursor: pointer;
}

.bottom-tab:hover {
  background: var(--bg2);
  color: var(--text);
}

.bottom-tab.active {
  background: var(--bg3);
  color: var(--text-bright, var(--text));
}

.bottom-close {
  margin-left: auto;
  background: transparent;
  border: none;
  color: var(--text-faint);
  cursor: pointer;
}

.bottom-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.bottom-placeholder {
  padding: 12px;
  font-size: 12px;
  color: var(--text-faint);
}
</style>
```

创建 `frontend/src/components/IdeLayout.vue`：

```vue
<template>
  <div class="ide-layout">
    <ActivityBar v-model="activity" />

    <div v-if="activity === 'files'" class="ide-side">
      <div class="ide-side-head">
        <span>文件</span>
        <button type="button" class="icon-btn" title="刷新" @click="store.refreshFileTree()">⟳</button>
      </div>
      <div class="file-tree">
        <FileTreeNode
          v-for="node in store.fileTree"
          :key="node.n"
          :node="node"
          :parent-path="store.workdir"
        />
      </div>
    </div>
    <div v-else-if="activity === 'git'" class="ide-side">
      <GitPanel />
    </div>
    <div v-else class="ide-side">
      <div class="ide-empty">断点与调用栈将在 S3 接入后显示</div>
    </div>

    <div class="ide-main">
      <EditorTabs v-if="store.tabs().length" />
      <div v-if="!store.tabs().length" class="ide-empty-main">从左侧文件树打开文件</div>
      <EditorPane v-else :key="store.activePath()" :path="store.activePath()" />

      <div class="ide-bottom" :style="{ height: bottomHeight + 'px' }">
        <BottomPanel v-if="ide.bottomPanelOpen" @close="ide.toggleBottomPanel()" />
      </div>
      <div v-if="!ide.bottomPanelOpen" class="ide-bottom-open">
        <button type="button" class="icon-btn" @click="ide.toggleBottomPanel()">显示面板</button>
      </div>
    </div>

    <div class="ide-chat">
      <slot name="chat" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';

import ActivityBar from './ActivityBar.vue';
import BottomPanel from './BottomPanel.vue';
import EditorPane from './EditorPane.vue';
import EditorTabs from './EditorTabs.vue';
import FileTreeNode from './FileTreeNode.vue';
import GitPanel from './GitPanel.vue';
import { useAppStore } from '../stores/appStore';
import { useIdeStore } from '../stores/ideStore';

const store = useAppStore();
const ide = useIdeStore();
// 用 ide 别名避免与 appStore 混用；文件树与工作区状态都来自 appStore，
// 标签、断点、底部面板状态都来自 ideStore
const activity = ref('files');
const bottomHeight = ref(200);
</script>

<style scoped>
.ide-layout {
  display: flex;
  flex: 1;
  min-height: 0;
  min-width: 0;
}

.ide-side {
  width: 260px;
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--bg1);
  border-right: 1px solid var(--border);
}

.ide-side-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 8px;
  font-size: 12px;
  color: var(--text-dim);
  border-bottom: 1px solid var(--border);
}

.file-tree {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.ide-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
}

.ide-bottom {
  flex: 0 0 auto;
  min-height: 0;
  overflow: hidden;
}

.ide-bottom-open {
  flex: 0 0 auto;
  padding: 4px 8px;
  border-top: 1px solid var(--border);
}

.ide-chat {
  width: 380px;
  flex: 0 0 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--border);
}

.ide-empty,
.ide-empty-main {
  padding: 12px;
  font-size: 12px;
  color: var(--text-faint);
}

.ide-empty-main {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
```

在 `frontend/src/stores/appStore.ts` 中，仿照现有 `rightPanelTabs` 的会话隔离写法新增模式状态（找到 `rightPanelTabs` 的 state 定义与按会话恢复的位置，紧邻追加）：

```ts
    // IDE 模式：与 rightPanelTabs 同样按会话隔离
    ideModeBySession: {} as Record<string, boolean>,
```

并新增 action：

```ts
    ideMode(): boolean {
      return !!this.ideModeBySession[this.currentSessionName];
    },

    toggleIdeMode() {
      this.ideModeBySession[this.currentSessionName] = !this.ideMode();
    },
```

> 注：会话名字段是 `currentSessionName`（`frontend/src/stores/appStore.ts:622`），不要用别的字段。

在 `frontend/src/App.vue` 的 `#main` 内，把中央对话区包进条件分支：`appStore.ideMode()` 为真时渲染 `IdeLayout`，并把原有对话区作为 `#chat` 插槽传入；为假时保持现有布局不变。

**终端只能有一个实例**（这一步最容易出错，务必按下面做）：`App.vue` 目前用 `v-show` 常挂载 `TerminalPane`（`App.vue:191-193`）。IDE 模式下 `BottomPanel` 也会挂载一个，若两处同时存在就会出现两个 xterm 实例、两个 PTY 会话。因此把 `App.vue` 里的 `TerminalPane` 改为 `v-if="!store.ideMode()"`，IDE 模式下终端完全由 `BottomPanel` 拥有。

同时注意 `TerminalPane` 的启动条件是 `bridgeReady && terminalPanelOpen`（`TerminalPane.vue:198-218`），所以 `BottomPanel` 挂载时必须确保该开关为真，否则终端不会启动——这一点已经写进上面 `BottomPanel.vue` 的 `onMounted` 里，不要漏掉。

**已知取舍（写进提交信息，不要当成 bug）**：切换模式会卸载并重建 `TerminalPane`，PTY 会话本身由 `pty_terminal_ensure` 复用不会重启（`gui_bridge.py:7816` 的语义是「没有或已退出才新开」），但 xterm 的滚动缓冲会在切换时重置。本期接受这个取舍；若后续要求保留缓冲，改法是让 `App.vue` 用 `<Teleport>` 把同一个实例搬进底部面板，而不是两处各挂一个。

在 `frontend/src/components/Topbar.vue` 中加入切换按钮：

```vue
      <button
        type="button"
        class="icon-btn"
        :title="store.ideMode() ? '切换到对话模式' : '切换到 IDE 模式'"
        @click="store.toggleIdeMode()"
      >
        {{ store.ideMode() ? '对话' : 'IDE' }}
      </button>
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/components/IdeLayout.test.ts
```

预期：2 个用例 PASS。

- [ ] **Step 5: 全量前端测试与构建**

```bash
cd /d E:\fch\Y-code_llm\frontend
npm test
npm run build
```

预期：全部测试通过，构建成功。

- [ ] **Step 6: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add frontend/src/components/ActivityBar.vue frontend/src/components/BottomPanel.vue frontend/src/components/IdeLayout.vue frontend/src/components/IdeLayout.test.ts frontend/src/App.vue frontend/src/components/Topbar.vue frontend/src/stores/appStore.ts
git commit -m "feat(ide): 新增 IDE 布局与对话/IDE 双模式切换"
```

---

## Task 12: 文件树按模式路由与外部改动事件接线

**Files:**
- Modify: `frontend/src/components/FileTreeNode.vue`
- Modify: `frontend/src/stores/appStore.ts`（`handleBridgeEvent` 增加 `ide_file_changed` 分支）
- Modify: `frontend/src/stores/ideStore.ts`（新增 `persistLayout` 并在标签变化时调用）
- Modify: `frontend/src/components/IdeLayout.vue`（首次挂载时加载布局快照）
- Test: `frontend/src/components/FileTreeNode.test.ts`（追加用例）
- Test: `frontend/src/stores/ideStore.test.ts`（追加用例）

**Interfaces:**
- Consumes: `useIdeStore`、`appStore.ideMode()`
- Produces: 事件 `ide_file_changed` → `ideStore.handleExternalChange(path)`

- [ ] **Step 1: 写失败的测试**

在 `frontend/src/components/FileTreeNode.test.ts` 末尾追加：

```ts
it('IDE 模式下点击代码文件走编辑器打开，不弹预览', async () => {
  const store = useAppStore();
  const ide = useIdeStore();
  store.ideModeBySession = { '': true };
  const opened: string[] = [];
  ide.openFile = (async (p: string) => { opened.push(p); }) as any;
  const previewCalls: string[] = [];
  store.openFilePreview = (async (p: string) => { previewCalls.push(p); }) as any;

  const node = { n: 'app.py', d: 0 } as FileNode;
  const wrapper = mount(FileTreeNode, { props: { node, parentPath: 'D:\\proj' } });
  await wrapper.get('.file-row').trigger('click');

  expect(opened).toEqual(['D:\\proj\\app.py']);
  expect(previewCalls).toEqual([]);
  wrapper.unmount();
});

it('对话模式下点击代码文件仍走源码预览', async () => {
  const store = useAppStore();
  const ide = useIdeStore();
  store.ideModeBySession = { '': false };
  const opened: string[] = [];
  ide.openFile = (async (p: string) => { opened.push(p); }) as any;
  const previewCalls: string[] = [];
  store.openFilePreview = (async (p: string) => { previewCalls.push(p); }) as any;

  const node = { n: 'app.py', d: 0 } as FileNode;
  const wrapper = mount(FileTreeNode, { props: { node, parentPath: 'D:\\proj' } });
  await wrapper.get('.file-row').trigger('click');

  expect(previewCalls).toEqual(['D:\\proj\\app.py']);
  expect(opened).toEqual([]);
  wrapper.unmount();
});
```

在文件顶部补 import：

```ts
import { useIdeStore } from '../stores/ideStore';
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/components/FileTreeNode.test.ts
```

预期：新增的「IDE 模式下点击代码文件走编辑器打开」FAIL（当前实现无条件走预览）。

- [ ] **Step 3: 实现模式路由与事件接线**

在 `frontend/src/components/FileTreeNode.vue` 的点击处理函数中，在现有「按扩展名分流（html / pdf / md 走右栏，其余走预览）」逻辑**之前**插入 IDE 模式分支：

```ts
  // IDE 模式下，代码与文本文件直接在编辑器打开；图片等非文本仍走既有预览分流
  if (appStore.ideMode() && isEditableText(name)) {
    void useIdeStore().openFile(fullPath);
    return;
  }
```

并在同文件内新增判定函数（与 `frontend/src/editor/setup.ts` 的扩展名表保持同一份口径，避免两处漂移）：

```ts
const EDITABLE_EXT = new Set([
  'py', 'js', 'mjs', 'cjs', 'jsx', 'ts', 'tsx', 'json', 'md', 'html', 'htm',
  'css', 'txt', 'log', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'sql', 'sh',
  'bat', 'ps1', 'go', 'rs', 'java', 'c', 'h', 'cpp', 'hpp', 'cs', 'php', 'rb',
]);

function isEditableText(name: string): boolean {
  const dot = name.lastIndexOf('.');
  if (dot < 0) return false;
  return EDITABLE_EXT.has(name.slice(dot + 1).toLowerCase());
}
```

在 `frontend/src/stores/appStore.ts` 的 `handleBridgeEvent(event)` 分支链中，仿照 `pty_data` 分支追加：

```ts
      } else if (type === 'ide_file_changed') {
        void useIdeStore().handleExternalChange(event.payload?.path ?? '');
```

> 注：`useIdeStore` 需要在该文件顶部按现有 store 引用方式 import；若担心循环依赖，改为在分支内 `const { useIdeStore } = await import('../stores/ideStore')` 动态导入。

在 `frontend/src/components/IdeLayout.vue` 中，挂载时加载布局快照并登记会话：

```ts
import { onMounted } from 'vue';

onMounted(async () => {
  const api = (window as any).pywebview?.api;
  ide.setSession(store.currentSessionName);
  if (!api?.ide_get_layout) return;
  const res = await api.ide_get_layout(store.currentSessionName);
  if (res?.ok && res.layout) {
    for (const item of res.layout.tabs) {
      try {
        await ide.openFile(item.path);
      } catch {
        // 上次打开的文件可能已被删除或移出工作区，静默跳过
      }
    }
    if (res.layout.active) ide.activate(res.layout.active);
  }
});
```

> 注：会话名字段是 `currentSessionName`（`frontend/src/stores/appStore.ts:622`），与 Task 11 保持一致。

**布局快照的写回**：Task 8 只做了读取侧接线，而验收标准第 8 条（重开软件后恢复标签）要求写回。在 `frontend/src/stores/ideStore.ts` 中新增带防抖的持久化动作：

```ts
    async persistLayout() {
      const api = ideApi();
      if (!api?.ide_save_layout || !this.session) return;
      const tabs = this.tabs();
      await api.ide_save_layout(this.session, {
        version: 1,
        tabs: tabs.map((t, index) => ({ path: t.path, line: index + 1 })),
        active: this.activePath(),
        // 断点由 S3 写入 ideStore，这里必须持久化真实值，不能写死空数组
        breakpoints: this.breakpointsBySession[this.session] ?? [],
      });
    },
```

在 `openFile`、`closeTab`、`activate` 三个动作的末尾各调用一次 `void this.persistLayout();`（`activate` 也必须调，否则活动标签不会持久化）。

在 `frontend/src/stores/ideStore.test.ts` 中补用例锁住这个行为：

```ts
it('打开与关闭标签都会写回布局快照', async () => {
  const api = installBridge({
    ide_save_layout: vi.fn(async (_s: string, l: any) => ({ ok: true, layout: l })),
  });
  const store = useIdeStore();
  store.setSession('s1');
  await store.openFile('a.py');
  expect(api.ide_save_layout).toHaveBeenCalledWith('s1', expect.objectContaining({ active: 'a.py' }));

  store.closeTab('a.py');
  await Promise.resolve();
  expect(api.ide_save_layout).toHaveBeenLastCalledWith('s1', expect.objectContaining({ active: '' }));
});
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd /d E:\fch\Y-code_llm\frontend
npx vitest run src/components/FileTreeNode.test.ts
```

预期：原有 4 个用例 + 新增 2 个用例全部 PASS。

- [ ] **Step 5: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add frontend/src/components/FileTreeNode.vue frontend/src/components/FileTreeNode.test.ts frontend/src/stores/appStore.ts frontend/src/stores/ideStore.ts frontend/src/stores/ideStore.test.ts frontend/src/components/IdeLayout.vue
git commit -m "feat(ide): 文件树按模式路由点击行为，接线外部改动事件与布局持久化"
```

---

## Task 13: 端到端回归与体积门禁

**Files:**
- Modify: `build_release.py`（前端构建步骤增加体积校验）
- Test: `tests/test_ide_regression.py`

**Interfaces:**
- Consumes: 前 12 个任务的全部产出
- Produces: 回归守卫

- [ ] **Step 1: 写回归测试**

创建 `tests/test_ide_regression.py`：

```python
# -*- coding: utf-8 -*-
"""IDE 模式的回归守卫。

核心断言：IDE 能力对模型请求路径零影响——这是设计规格 §7.3 的硬约束，
一旦有人把编辑器内容注入上下文，这里会立刻失败。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_ide_package_does_not_import_lsp_tool():
    """core/ide 不得依赖 core/lsp.py（那是 find_definition 的实现）。"""
    import core.ide.file_service as fs

    source = open(fs.__file__, encoding="utf-8").read()
    assert "core.lsp" not in source
    assert "find_definition" not in source


def test_ide_modules_do_not_touch_prompt_builder():
    """IDE 模块不得引用提示词构造，防止把编辑器状态注入请求。"""
    import core.ide.layout_store as ls

    for module in (fs := __import__("core.ide.file_service", fromlist=["x"]), ls):
        source = open(module.__file__, encoding="utf-8").read()
        for forbidden in ("build_stable_prompt", "context_builder", "system_prompt"):
            assert forbidden not in source, f"{module.__name__} 引用了 {forbidden}"


def test_prompt_byte_stability_still_passes():
    """委托给既有黄金测试：IDE 不得改变请求字节。"""
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_prompt_byte_stability.py", "-q"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Step 2: 运行回归测试**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_regression.py -v
```

预期：3 个用例 PASS。

- [ ] **Step 3: 加入前端体积门禁**

在 `build_release.py` 的 `step0_build_frontend` 中，`npm run build` 成功之后、校验 `frontend/dist/index.html` 存在之前，插入体积校验：

```python
    # IDE 模式引入 CodeMirror 6 后，首屏包体积必须受控：
    # 语言包与重扩展一律动态 import，一旦被静态引入这里会失败。
    dist_assets = os.path.join(base_dir, "frontend", "dist", "assets")
    MAX_ENTRY_BYTES = 1_800_000  # 首屏 JS 上限 1.8 MB
    if os.path.isdir(dist_assets):
        entry_files = [
            f for f in os.listdir(dist_assets)
            if f.startswith("index-") and f.endswith(".js")
        ]
        for name in entry_files:
            size = os.path.getsize(os.path.join(dist_assets, name))
            if size > MAX_ENTRY_BYTES:
                raise SystemExit(
                    f"前端首屏包过大：{name} {size / 1024 / 1024:.2f} MB，"
                    f"上限 {MAX_ENTRY_BYTES / 1024 / 1024:.2f} MB。"
                    "请检查是否有语言包被静态引入。"
                )
```

- [ ] **Step 4: 运行完整验证**

```bash
cd /d E:\fch\Y-code_llm
python -m pytest tests/test_ide_file_service.py tests/test_ide_layout_store.py tests/test_gui_ide_bridge.py tests/test_gui_path_policy_edit.py tests/test_read_file_preview.py tests/test_prompt_byte_stability.py -v
cd frontend
npm test
npm run build
```

预期：后端全部 PASS，前端全部 PASS，构建成功且体积门禁未触发。

- [ ] **Step 5: 手工验收（对照设计规格 §10 的 S1 九条）**

```bash
cd /d E:\fch\Y-code_llm
python gui_main.py
```

逐条走查并记录结果：

1. 顶部切换按钮在对话模式与 IDE 模式之间切换，切换后会话消息与进行中的回合不受影响
2. 文件树打开 `.py` 文件进编辑器，多标签切换与关闭正常，行号与高亮生效，`Ctrl+F` 可查找替换
3. 编辑后标签出现脏标记，`Ctrl+S` 保存；用 GBK 文件验证保存后编码与换行不变（可用 `python -c "print(open('x.txt','rb').read())"` 对比）
4. 让星瑶用 `edit_file` 改一个已打开且干净的文件，编辑器应在 2 秒内自动重载并提示
5. 让星瑶改一个已打开且脏的文件，编辑器应标注「已被星瑶修改」且不覆盖本地内容
6. 打开工作区外文件被拒；打开 `~/.ycode/config.json` 被拒
7. 打开一个大于 2 MB 的文件被拒且界面不卡
8. 关闭软件重开，上次的标签与活动标签恢复
9. 对话模式下的终端、Git、内嵌浏览器、电脑操控均无回归

- [ ] **Step 6: 提交**

```bash
cd /d E:\fch\Y-code_llm
git add build_release.py tests/test_ide_regression.py
git commit -m "test(ide): 新增 IDE 回归守卫与前端首屏体积门禁"
```

---

## 交付判据

- 后端：`tests/test_ide_file_service.py`、`tests/test_ide_layout_store.py`、`tests/test_gui_ide_bridge.py`、`tests/test_gui_path_policy_edit.py`、`tests/test_ide_regression.py` 全绿
- 前端：`npm test` 全绿，`npm run build` 成功且未触发体积门禁
- 既有回归：`tests/test_read_file_preview.py`、`tests/test_prompt_byte_stability.py` 全绿
- 手工验收：设计规格 §10 的 S1 九条全部通过
