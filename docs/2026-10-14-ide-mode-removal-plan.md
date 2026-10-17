# IDE 模式移除方案（2026-10-14）

> **执行状态：已于 2026-10-17 执行完毕**（分支 `chore/remove-ide-mode`，起点 `61cd378`）。
> 提交链（按时间序）：`ad55485` Phase 1 摘前端入口 → `28a692a` Phase 3 后端桥接 → `70c7af9` Phase 2 前端模块与依赖
> （与 Phase 3 并行产出，故顺序交错）→ `6ad7f16` Phase 4 删 `core/ide` 与 session_ui → `125333b` Phase 5 文档素材与注释。
> 结果：前端测试 164 文件 / 1503 用例 → 142 文件 / 1248 用例（全绿）；首屏 `index-*.js` 从 1,453,472 字节
> 降到 1,023,157 字节（-29.6%）；后端全量 pytest 5270 passed / 7 failed / 6 skipped。那 7 条失败全部是
> 秘书体系既有的测试隔离问题（`test_secretary_identity.py` 6 条 + `test_secretary_dispatch.py` 1 条）：
> 单独跑通过，且只跑 `tests/test_s*.py` 这批（与 IDE 测试零交集）在移除前的 `61cd378` 上同样复现，
> 判定与本次移除无关，另行处理。
> **以下各节是执行前写下的方案快照：行号与 HEAD 引用均已失效（HEAD 已从 `b1a0038` 变为 `61cd378`），
> 只作决策依据与「绝不能删」判据保留，请勿按其中的行号操作。**

> 依据：对当前工作区（HEAD `b1a0038` + 未提交改动）的全量扫描，行号均为**当前快照**，
> 执行前请用符号/文件名再搜索一次复核（编辑会让行号漂移）。

## 一、结论先说

IDE 模式是一套**收敛良好的平行子系统**，可以整体摘除，不需要动内核：

- Python 侧 3800 余行全部收在 `core/ide/`，**唯一消费者是 `gui_bridge.py`**，而且是方法内懒导入（`gui_bridge.py:8871` 注释明确「core.ide 的导入放在方法内」）。
- CLI/TUI 侧（`main.py`、`core/harness`、`core/tools.py`）**零依赖** IDE 模块。`main.py` 里 34 处 "ide" 全是 `provider` / `consider` 这类子串噪声。
- 前端是一套「一个开关（`appStore.ideMode`）+ 一个布局组件（`IdeLayout.vue`）+ 一组子组件 + 一套 CodeMirror 编辑器栈」。
- 模式入口已在 `a96e86d chore(topbar): 临时隐藏对话/IDE 模式切换入口` 里临时隐藏（`Topbar.vue:297` 注释），说明产品侧已先走了一步。

风险不在"删得掉删不掉"，而在三件事：**共用组件的分支收口**、**懒导入遗漏导致运行期才炸**、**会话级 `ide_mode` 旧数据兼容**。

规模一览：

| 层 | 内容 | 量级 |
| --- | --- | --- |
| Python | `core/ide/`（12 文件） | ≈ 3 810 行 |
| Python 桥接 | `gui_bridge.py` 四个区段 + 实例状态 | ≈ 1 100 行 |
| 前端模块 | 2 个 store + 9 个组件 + `editor/` + `ide/` + `utils/dapBus.ts` | ≈ 3 900 行 |
| 前端共用改造 | `App.vue` / `appStore.ts` / `Topbar.vue` / `FileTreeNode.vue` / `SettingsPage.vue` | ≈ 60 处 |
| 测试 | Python 18 个文件 + 前端 20 余个文件 | ≈ 6 000 行 |
| 文档 | 3 份 superpowers 计划 + 1 份设计稿 + 1 个 mockup | ≈ 13 000 行 |

---

## 二、边界三分法

### 2.1 可以整体删除（IDE 专属）

**Python**

- `core/ide/` 整个目录：`dap_manager.py`(626) `debug_session.py`(522) `debug_adapters.py`(275) `run_command.py`(442) `lsp_manager.py`(893) `lsp_codec.py`(114) `language_registry.py`(145) `diagnostics_batcher.py`(93) `server_provision.py`(262) `file_service.py`(243) `layout_store.py`(174) `__init__.py`(8)
- 测试与夹具：`tests/test_ide_*.py`（14 个）、`tests/test_gui_ide_bridge.py`、`tests/test_gui_ide_run.py`、`tests/test_gui_ide_debug.py`、`tests/test_gui_lsp_bridge.py`、`tests/fake_lsp_server.py`、`tests/fake_dap_adapter.py`

**前端**

- store：`stores/ideStore.ts`(472)、`stores/ideDebugStore.ts`(595) 及测试
- 组件：`components/IdeLayout.vue`(621)、`ActivityBar.vue`(123)、`EditorPane.vue`(311)、`EditorTabs.vue`(87)、`DebugPane.vue`(421)、`DebugToolbar.vue`(245)、`BottomPanel.vue`(125)、`WorkspaceWelcome.vue`(197)、`EditorLanguageSettings.vue`(253) 及各自测试
- 编辑器栈：`editor/setup.ts`(82)、`editor/lsp.ts`(660)、`editor/lspNavigation.ts`(287)、`editor/theme.ts`(50) 及测试
- 其它：`ide/cmBreakpoints.ts`(166)、`utils/dapBus.ts`(34)
- 类型声明：`types/pywebview.d.ts` 的 1396–1442 行（IDE / LSP API）与 1613–1660 行（IDE DTO）
- 测试文件：`App.ideChrome.test.ts`、`App.ideStatusbar.test.ts`、`Topbar.ideMode.test.ts`、`appStore.ideDebugEvents.test.ts`、`appStore.ideWorkspace.test.ts`、`BottomPanel.debugTab.test.ts`
- 依赖：`package.json` 的 15 个 `@codemirror/*` 包（引用方只有 `editor/` 与 `EditorPane.vue`；删前用 `grep -r "@codemirror" frontend/src` 复核）

**文档 / 素材**

- `docs/superpowers/plans/2026-10-06-ide-s1-skeleton.md`(3416)、`-s2-language-intelligence.md`(4957)、`-s3-run-debug.md`(4991)
- `docs/superpowers/specs/2026-10-06-ide-mode-design.md`(449)
- `mockups/ide-mode-redesign.html`

### 2.2 需要改造的共用文件（逐处列出）

**1) `gui_bridge.py`（核心改造面，四个区段）**

| 位置 | 内容 |
| --- | --- |
| 325 | `DEBUG_REAP_JOIN_TIMEOUT = 12.0` |
| 669–688 | `ApiBridge.__init__` 里的 `_debug_session` / `_debug_lock` / `_debug_last_thread_id` / `_debug_last_frame_id` / `_debug_reapers` / `_debug_debuggee_exited` / `_debug_session_id` |
| 1210 | 关窗清理调用 `self._shutdown_debug_session()` ← **必须连删，否则 AttributeError** |
| 3354–3508 | 文件服务 + 布局：`_ide_watcher_instance` / `_ide_ensure_watch_thread` / `_ide_poll_once` / `ide_set_open_files` / `_ide_guard` / `ide_read_file` / `ide_write_file` / `ide_stat_files` / `ide_get_layout` / `ide_save_layout`，事件 `ide_file_changed`(3388) |
| 3551–3795 | 语言服务：`_lsp_manager_instance` / `_lsp_batcher_instance` / `_lsp_on_status` / `_lsp_on_notification` / `_lsp_flush_diagnostics` / `_lsp_discard_diagnostics` / `_lsp_workspace_guard` / `_lsp_workspace_token` / `lsp_request` / `lsp_notify` / `lsp_sync_workspace` / `lsp_server_status` / `lsp_set_server_path`，事件 `lsp_status`(3587) / `lsp_diagnostics`(3606) |
| 8874–8932 | `ide_run_file` |
| 8933–9428 | 调试全段：`_emit_dap_event` / `_reset_debug_cursors` / `_discard_debug_session` / `_forget_debug_reaper` / `_reap_session_now` / `_force_kill_session_tree` / `_reap_debug_session` / `_shutdown_debug_session` / `_debug_path_denied` / `ide_debug_*`（9 个公开方法）/ `_debug_query` / `_debug_thread_id` / `_debug_frame_id` |

**2) `frontend/src/App.vue`（约 20 处分支）**

- 模板：49、53（侧栏在 IDE 模式退场）、63（`IdeLayout` 挂载）、75–76（`ide-docked` class）、83–115（IDE 对话头部与入口）、239–241（终端让位）、261（右侧面板让位）、267–273（IDE 徽标）、313–331（状态栏编辑器段）
- 脚本：390（`useIdeStore`）、391（`languageIdForPath`，改造时连带处理）、417（`IdeLayout` import）、563–603（编辑器段 computed 与语言标签表）、994–999（`Ctrl+Alt+I` 快捷键）
- 样式：1359–1363、1457–1501（IDE 停靠与窄栏）、1674（IDE 徽标）

**3) `frontend/src/stores/appStore.ts`（约 15 处）**

- 顶部注释与 import：4–12
- 状态：772（`ideModeBySession`）
- 动作：`ideMode()`(1583)、`toggleIdeMode()`(1587)、`persistIdeMode()`(1609)、`restoreIdeMode()`(1628)、`_moveIdeModeBucket()`(1652)、`ensureIdeWorkspace()`(3346)
- 调用点：1596、2329、3188、3537、3958、4709
- 事件分发：4932–4935（`ide_file_changed` → `useIdeStore().handleExternalChange`）、4946–4963（DAP 事件 → `useIdeDebugStore().reduceDapEvent`）
- 注意：`workdir` / `fileTree` / `refreshFileTree` / `loadFileTreeInBackground` / `fileTreeExpanded` **是对话模式与项目分组功能**，`ensureIdeWorkspace` 只是它的薄封装（唯一调用方是 IDE 侧），**不能连坐删除**。

**4) 其它共用文件**

- `components/Topbar.vue:57–63`：对话/IDE 切换按钮（当前已视觉隐藏，直接删逻辑与 297 行注释）
- `components/FileTreeNode.vue:82`：`if (store.ideMode() && isEditableText(...))` 分支 —— 改成非 IDE 的原有打开行为，组件本身保留（`RightPanel.vue:113` 在用）
- `components/SettingsPage.vue`：13（导航项「编辑器与语言服务」）、671（挂载）、780（import）、801（`activeSection` 类型联合里的 `'editor-language'`）
- `components/TerminalPane.vue`：保留；只拆掉 `App.vue:241` 的 `v-if="!store.ideMode()"` 条件与 `BottomPanel.vue` 里的宿主
- `core/session_ui.py`：`_clean_session`(50–62) 的字段白名单**只有 `ide_mode`**，`get_session_ui` / `save_session_ui`(114/123) 与 `gui_bridge.py:3523/3532` 成对（见第六节决策点）
- `build_release.py:233`：首屏包体积上限校验（注释提到 IDE 引入 CodeMirror）—— 校验逻辑**保留**，只改注释
- `ycode.spec`：`gui_hiddenimports`(147–170) **未显式声明 `core.ide`**（靠静态扫描），删除后无需改打包配置

### 2.3 绝不能删（易误删清单）

- `core/lsp.py`(252)：`find_definition` / `search_code` 工具用的 LSP-lite 符号查找，被 `core/tools.py:26`、`core/tool_registry.py:128` 使用；`core/ide/__init__.py:1–8` 也明确写了「与 core/lsp.py 互不依赖」。**与 IDE 无关，保留。**
- `utils/ptyBus.ts`、`core/gui_terminal.py`、`gui_bridge.py` 的 `pty_terminal_write/resize/kill`(8817–8873)：对话模式的内嵌终端，保留。
- `components/TerminalPane.vue`、`components/FileTreeNode.vue`、`RightPanel.vue` 文件面板、`WorkspaceWelcome` 之外的欢迎逻辑、`workdir` / `fileTree` 一族状态。
- `session_ui` 的存储机制本身（通用会话 UI 偏好）。
- `build_release.py` 的首屏体积上限校验。

---

## 三、分阶段执行步骤

原则：**每阶段结束都能构建、能跑、测试全绿**；先摘入口再拆模块，先前端后后端，删代码与删测试同批完成。

### Phase 0：基线与准备

1. 处理未提交改动：当前工作区有 `M .gitignore`、`M core/screen_snip_native.py`、`M tests/test_screen_snip_native.py`、未跟踪 `gzh-tokenrhythm/` —— 先提交或 stash，避免与移除工作混在一起。
2. 建工作分支：`git checkout -b chore/remove-ide-mode`。
3. 记录基线（结果存档，作为 Phase 5 对比依据）：
   ```
   venv\Scripts\python -m pytest -q
   cd frontend; npm run test
   cd frontend; npm run build
   ```
   同时记下 `frontend/dist/assets/index-*.js` 体积（当前有 1.8 MB 上限校验，`build_release.py:233`）。
4. 全仓确认 IDE 引用面（输出应为"仅 gui_bridge / tests / docs"，出现其它文件就必须先评估）：
   ```
   venv\Scripts\python -c "import pathlib,re;[print(f'{i}:{n}',l.strip()) for i in ['gui_bridge.py','main.py'] for n,l in enumerate(pathlib.Path(i).read_text(encoding='utf-8').splitlines(),1) if 'core.ide' in l]"
   ```

**验收**：分支建立、基线测试全绿、基线体积已记录。

### Phase 1：摘掉前端入口（IDE 不可进入，模块暂留）

改动：

1. `App.vue`：删 `IdeLayout` 挂载与 import、所有 `store.ideMode()` 分支（模板 49/53/63/75–76/83–115/239–241/261/267–273/313–331、脚本 390/417/563–603/994–999、样式 1359–1501/1674）。
2. `Topbar.vue`：删 57–63 的切换按钮与其注释。
3. `appStore.ts`：删 `ideModeBySession` 及 6 个动作、6 处调用点、`ide_file_changed` / DAP 事件分发段（4932–4963）；删 4–12 的注释与 import。
4. 同批删掉会因此失败的测试：`App.ideChrome.test.ts`、`App.ideStatusbar.test.ts`、`Topbar.ideMode.test.ts`、`appStore.ideWorkspace.test.ts`、`appStore.ideDebugEvents.test.ts`。

**验收**：
```
cd frontend; npm run build
cd frontend; npm run test
```
构建通过、vitest 全绿；启动 `venv\Scripts\python gui_main.py`，确认对话模式可用、无 IDE 徽标/入口、状态栏与终端正常。

### Phase 2：删前端 IDE 模块与依赖

改动：

1. 删 2.1 列出的 store / 组件 / `editor/` / `ide/` / `utils/dapBus.ts` 及其测试。
2. `SettingsPage.vue`：删导航项(13)、挂载(671)、import(780)、类型联合里的 `'editor-language'`(801)；同步删 `SettingsPage.customProvider.test.ts:16` 的桩。
3. `FileTreeNode.vue:82`：去掉 `store.ideMode()` 分支，恢复非 IDE 行为。
4. `types/pywebview.d.ts`：删 1396–1442 与 1613–1660 两段。
5. `package.json`：删 15 个 `@codemirror/*` 依赖，跑 `npm install` 更新 lock。

**验收**：
```
grep -rn "@codemirror\|ideStore\|ideDebugStore\|IdeLayout\|cmBreakpoints\|dapBus" frontend/src   # 应为空
cd frontend; npm run build
cd frontend; npm run test
```
构建通过、测试全绿、首屏包体积明显下降（对比 Phase 0 基线）。

### Phase 3：删后端桥接

改动（全部在 `gui_bridge.py`）：

1. 删 3354–3508（文件服务 + 布局）与 3551–3795（语言服务）两段，含 `ide_file_changed` / `lsp_status` / `lsp_diagnostics` 三个事件发射点。
2. 删 8874–8932（`ide_run_file`）。
3. 删 8933–9428（调试全段）。
4. 删 `__init__` 的 669–688 实例状态、325 的 `DEBUG_REAP_JOIN_TIMEOUT`、1210 的 `_shutdown_debug_session()` 调用。
5. 用全仓搜索复核懒导入无残留：
   ```
   grep -rn "core\.ide\|core import ide\|ide_read_file\|ide_write_file\|ide_save_layout\|ide_run_file\|ide_debug_\|lsp_sync_workspace\|lsp_server_status" --include=*.py .   # 期望：只剩 tests 与 docs
   ```

**验收**：
```
venv\Scripts\python -c "import gui_bridge"
venv\Scripts\python -m pytest -q
```
（Phase 3 完成时 IDE 相关测试已删，全量应绿；若 `test_gui_*` 中还有断言 IDE 桥的用例，同步删。）

### Phase 4：删 `core/ide/` 与会话标记

改动：

1. 删整个 `core/ide/` 目录。
2. 处理 `ide_mode`：按第六节决策点二选一——
   - A（推荐，省事）：保留 `core/session_ui.py` 与 `get_session_ui`/`save_session_ui`，把 `_clean_session` 的白名单改为空 dict 或直接保留 `ide_mode` 字段不再使用（旧数据天然兼容）。
   - B（彻底）：连 `core/session_ui.py`、`gui_bridge.py:3523/3532`、`tests/test_session_ui*.py` 一起删，并接受旧 `session_ui` 存储文件成为孤儿数据。
3. 删 `tests/fake_lsp_server.py`、`tests/fake_dap_adapter.py`、`tests/test_ide_*.py`、`tests/test_gui_ide_*.py`、`tests/test_gui_lsp_bridge.py`。

**验收**：
```
venv\Scripts\python -c "import core.ide"        # 期望 ModuleNotFoundError
venv\Scripts\python -m pytest -q
```

### Phase 5：收尾清理与回归

改动：

1. 文档：删 3 份 superpowers IDE 计划、1 份设计稿（或按决策点归档）、`mockups/ide-mode-redesign.html`；同步更新 `docs/all_features.md`、`docs/module_map.md`、`README.md` 中提及 IDE 模式的段落（实施前用 `grep -n "IDE" docs/*.md README.md` 定位）。
2. `build_release.py:233` 注释改写（去掉"IDE 模式引入 CodeMirror"表述，保留 1.8 MB 校验）。
3. 重建前端产物：`cd frontend; npm run build`（GUI 从 `frontend/dist` 读取，`ycode.spec:60`）。
4. 全流程回归：`verify_ycode.bat`（内含全量 pytest + `npm run build`）。
5. 手工冒烟：`venv\Scripts\python main.py`（TUI）与 `venv\Scripts\python gui_main.py`（GUI），确认：对话、终端、文件面板/项目分组、Git 面板、设置页各分区、窗口关闭无异常（重点看 Phase 3 删掉的关窗清理路径）。

**验收**：`verify_ycode.bat` 全绿；两个入口手工冒烟无异常；全仓搜索 IDE 关键词只剩文档历史记录。

---

## 四、风险清单

1. **懒导入遗漏只在运行期炸**：`core.ide` 的导入全在方法内（`gui_bridge.py:8871` 注释就是这么定的口径），静态检查不会报错。Phase 3 必须用第三节给出的全仓 grep 兜底，并手工走一遍每条功能路径（打开文件、调试启动、关窗）。
2. **关窗清理链**：`gui_bridge.py:1210` 调 `_shutdown_debug_session`，删方法不删调用 = 关窗即崩。
3. **共用组件误删**：`FileTreeNode.vue`（RightPanel 在用）、`TerminalPane.vue`（对话模式在用）、`fileTree` 一族状态（项目分组在用）。按 2.3 清单逐条比对。
4. **`core/lsp.py` 误删**：名字像 IDE，实际是 `find_definition` 工具依赖（`core/tools.py:26`）。删它会连带打断 CLI 的代码定位能力，`tests/test_ide_lsp_regression.py::test_core_layer_does_not_import_webview` 这类回归测试也会失效（该文件随 IDE 一起删，别用它当护栏）。
5. **旧数据兼容**：会话级 `session_ui` 里的 `ide_mode`、`layout_store` 落盘的布局快照、`server_provision` 写进配置目录的 overrides 文件，删除后都会变成残留数据。不影响功能，但要么保留读取兼容，要么明确清理。
6. **首屏体积与 vendor**：CodeMirror 移除后首屏会变小，但 `build_release.py` 的上限校验逻辑要留着。
7. **测试删除面**：Python 侧 18 个文件、前端 20 余个文件，删测试必须与删代码同批，否则中间态红一片，容易误判回归。
8. **未提交改动混入**：见 Phase 0，先把现有改动落地。

---

## 五、验证矩阵

| 阶段 | 命令 | 期望 |
| --- | --- | --- |
| Phase 0 | `venv\Scripts\python -m pytest -q` | 全绿（基线） |
| Phase 0 | `cd frontend; npm run test; npm run build` | 全绿（基线） |
| Phase 1 | `cd frontend; npm run build` | 通过 |
| Phase 1 | `cd frontend; npm run test` | 全绿 |
| Phase 2 | `grep -rn "ideStore\|IdeLayout\|@codemirror\|dapBus" frontend/src` | 无输出 |
| Phase 2 | `cd frontend; npm run build` | 通过，首屏 JS 体积下降 |
| Phase 3 | `venv\Scripts\python -c "import gui_bridge"` | 无异常 |
| Phase 3 | `grep -rn "core\.ide\|core import ide" --include=*.py .` | 只剩 docs |
| Phase 4 | `venv\Scripts\python -c "import core.ide"` | `ModuleNotFoundError` |
| Phase 4 | `venv\Scripts\python -m pytest -q` | 全绿 |
| Phase 5 | `verify_ycode.bat` | 全流程通过 |
| Phase 5 | `venv\Scripts\python gui_main.py` / `main.py` | 手工冒烟无异常 |

---

## 六、待确认的决策点

1. **`session_ui` 的 `ide_mode`**：保留通用会话 UI 存储（推荐，改动最小、旧数据兼容）还是一并删除？
2. **设置页「编辑器与语言服务」分区**：整块删除，还是保留为空壳（将来接别的编辑器能力）？
3. **IDE 文档**（3 份计划 + 1 份设计稿 + mockup）：直接删除，还是移到 `docs/archive/` 留档？
4. **工作树形态**：是否需要保留一个 `feat/ide-mode` 分支（`git branch feat/ide-mode`）作为将来召回点？

---

## 七、预估与回滚

- 工作量：Phase 1–2（前端）约占总量的 55%，Phase 3–4（后端）约 35%，Phase 5 约 10%。按阶段提交，每阶段一个 commit（`chore(ide): ...`）。
- 回滚：全程在 `chore/remove-ide-mode` 分支上做，主分支不受影响；若需召回，Phase 0 建分支时先留 `feat/ide-mode` 或在删除前打 tag `ide-mode-final`。
