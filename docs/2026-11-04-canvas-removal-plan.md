# 删除星瑶旧画布 实施计划（M1，2026-11-04）

> **执行状态：已于 2026-11-04 执行完毕**（dev 分支）。提交链：`d71ea354` Task 1 前端整删+占位 → `e72cdc63` Task 2 后端桥删除 → `9edd0ccc` Task 3 依赖清理。
> 回归结果：前端 build 通过、vitest 158 文件 / 1491 用例全绿；C 类共用面前端 76 用例 + 后端 243 用例全绿；后端全量（not slow，排除真实 git 子进程慢套件与 7 个既存收集错误文件）6345 passed / 4 skipped / 5 failed——5 条失败逐条核实均与画布删除无关：2 条 hunyuan 缺 HUNYUAN_IMAGE_ENDPOINT 环境变量（设变量后 13 全过，且在删除前基线 b9fa49f5 的 worktree 上同样失败）、1 条 memory 删除耗时（基线 worktree 同样失败）、2 条（llm_events / tools kill）单跑通过属测试隔离抖动。秘书基线 54 用例单跑全绿。真实 git 子进程套件在本机每次子进程挂起分钟级（pytest.ini 已注明的环境问题），未纳入本轮全量。
> 另：工作区存在用户并行开发的大量未提交改动（秘书升级等 21 文件），三个提交均已精确隔离，未卷入任何用户改动。下文为执行前方案快照，行号已漂移，请勿再按其中行号操作。

> 规格来源：`docs/2026-11-04-topwin-canvas-integration-design.md`（已定稿）§2 旧画布退役策略、§8 M1。本文与之逐条对齐；文中全部行号已于 2026-11-04 用带行号读取核实过当前文件（设计文档中的行号快照有少量漂移，以本文为准）。

## 目标

一句话：按设计 §2 A/B 清单删除旧画布前后端代码与 `@vue-flow/core` 依赖，画布页签保留并显示「画布重构中」占位，C 类共用功能零回归。

## 架构

前端 Vue 3 + Pinia，画布域在 `frontend/src/canvas/` 与 `frontend/src/stores/canvas.ts`，由 `RightPanel.vue` 挂载、`appStore.ts` 做事件分发；后端画布桥为 `gui_canvas_mixin.py` 的 `CanvasBridgeMixin`，混入 `gui_bridge.py` 的 `ApiBridge`。删除后保留三处兼容骨架：`'canvas'` 页签 id 与占位容器（会话快照恢复不断裂）、`parseCanvasEditResult` / `parseCanvasVideoResult` 导出（M2 复用）、`canvas_*` 事件 no-op 分发（兜住旧 job 残留事件）。

## 全局约束（每个任务都适用）

1. **Shell 为 PowerShell 7**：命令串联用 `;`，条件串联用 `if ($LASTEXITCODE -eq 0) { ... }`，禁止用 `&` 串联命令。
2. **前端验证命令**：`cd frontend; npm run build`（含 `vue-tsc -b` 类型检查）与 `cd frontend; npx vitest run`。
3. **后端验证命令**：仓库根执行 `python -m pytest tests/ -x -q`，可按文件收窄（如 `python -m pytest tests/test_character_store.py -q`）。
4. **每阶段结束 `git commit`**，提交信息用中文 conventional 风格（`refactor: ...` / `test: ...` / `chore: ...`）。
5. **绝不能删清单（C 类，设计 §2.4）**——任何任务的删除操作都不得触碰：
   - `core/media_server.py`、`core/media_store.py`（附件缩略图 / idle_maintenance / media_migration / media_refs 共用；`media_server.py:89` 注释里提到 gui_canvas_mixin 只是文字引用，文件本身不动）；
   - `core/character_store.py` 与 `gui_bridge.py`:1325-1330 的角色库初始化（`from core.character_store import init_character_storage` → `self._character_storage = init_character_storage(self._memory)`，注释块从 :1325 起）；
   - `core/embedded_browser.py`、`gui_snip_mixin.py`；
   - `frontend/src/components/MessageBubble.vue`:717 与 `frontend/src/components/RightPanelSnip.vue`:229 的 `document.createElement('canvas')`（截图/标注用途，同名无关）。
6. **旧数据声明（设计 §2.1 / D3）**：各项目 `.ycode/canvas/*.json` 与 `.ycode/canvas/media/` 磁盘文件**不清理、不迁移、不读取**。本计划不含任何数据清理步骤。
7. **保留不删（B 类骨架）**：`RightPanelTabId` 联合类型与 `RIGHT_PANEL_TAB_IDS` 白名单中的 `'canvas'`（appStore.ts:236-240）；RightPanel.vue:70 的 flush 条件；:350 / :361 / :372 的页签卡片/标签/图标条目；appStore.ts:453-550 的两个 parse 函数及其接口类型导出。

## 任务顺序与依赖

```
Task 1 前端整体删除+占位改造（一次提交，含 4 个 store 测试文件删除）
   ↓
Task 2 后端删除（gui_canvas_mixin + 3 个桥测试 + gui_bridge 两处引用）
   ↓
Task 3 依赖清理（@vue-flow/core 移除 + lock 更新）
   ↓
Task 4 总回归（前后端全量 + C 类共用面确认 + 提交链检查）
```

顺序理由：`canvas.inlineEdit.test.ts` / `canvas.video.test.ts` 同时 import `useCanvasStore`（来自 stores/canvas.ts）与 parse 函数（来自 appStore.ts），若先改 appStore 再删测试会出现 import 断裂的中间态；因此 Task 1 内部**先删 canvas 目录、store 与 4 个测试，再改 appStore / RightPanel，一次提交完成前端侧全部**。Task 3 依赖 Task 1（`@vue-flow` 的源码引用全在 frontend/src/canvas/ 内，目录删净后才能移除依赖而不破坏构建）。

---

## Task 1：前端整体删除 + 占位改造

### Files

**删除（整目录/整文件）：**

- `frontend/src/canvas/`（整目录，实测含：`CanvasPane.vue`、`CanvasDock.vue`（已确认无引用方的死代码）、`AnnotationDialog.vue`、`CharacterRefDialog.vue`、`annotation.ts`、`annotation.test.ts`、`nodes/`（GenerateNode.vue、ImageNode.vue、PromptNode.vue、VideoNode.vue、types.ts、node-shell.css、ImageNode.character.test.ts、ImageNode.click.test.ts、VideoNode.test.ts））
- `frontend/src/stores/canvas.ts`
- `frontend/src/stores/canvas.character.test.ts`
- `frontend/src/stores/canvas.derived.test.ts`
- `frontend/src/stores/canvas.inlineEdit.test.ts`
- `frontend/src/stores/canvas.video.test.ts`

**修改：**

- `frontend/src/components/RightPanel.vue`
- `frontend/src/stores/appStore.ts`

已核实：删除上述文件后，`frontend/src` 内对 `useCanvasStore` / `stores/canvas` / `../canvas` 的全部引用只剩 RightPanel.vue:234 与 appStore.ts 的 :7 / :174 / :5638 / :5647 / :5660，全部在本任务的修改点内；`@vue-flow` 源码引用全部位于被删的 canvas 目录内。

### 步骤

**1.1 删除文件**（在仓库根，PowerShell 7）：

```powershell
Remove-Item -Recurse -Force frontend/src/canvas
Remove-Item -Force frontend/src/stores/canvas.ts, frontend/src/stores/canvas.character.test.ts, frontend/src/stores/canvas.derived.test.ts, frontend/src/stores/canvas.inlineEdit.test.ts, frontend/src/stores/canvas.video.test.ts
```

**1.2 RightPanel.vue：画布容器换内联占位（:177-181，注释一并替换）**

目标片段（当前 :177-181）：

```html
      <!-- 无限画布：满幅渲染（flush），页签未激活时把 active 传下去，
           让画布跳过测量/重绘这类只在可见时才有意义的工作 -->
      <div v-show="visualTab === 'canvas'" class="rpane active" data-test="canvas-pane">
        <CanvasPane :active="activeTab === 'canvas'" />
      </div>
```

替换片段：

```html
      <!-- 画布占位：旧画布已删除，M2 接入新 CanvasPane（设计 §2.5）。
           保留 data-test="canvas-pane" 与 flush 满幅口径，会话快照恢复不丢页签 -->
      <div v-show="visualTab === 'canvas'" class="rpane active" data-test="canvas-pane">
        <div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--text-secondary, #888)">画布重构中</div>
      </div>
```

**1.3 RightPanel.vue：删 CanvasPane import（:232-234，注释一并删）**

目标片段：

```ts
// 画布工作台与数据层都放在 src/canvas|stores 下的画布域，与 components 里的
// 通用面板区分，避免以后画布节点组件把 components 目录撑大
import CanvasPane from '../canvas/CanvasPane.vue';
```

替换片段：整段删除（不新增任何 import）。

**不动**：:70 的 flush 条件、:350 的 `{ id: 'canvas', label: '画布', icon: 'layers' }`、:361 的 `canvas: '画布'`、:372 的 `canvas: 'layers'`。

**1.4 appStore.ts：删 useCanvasStore import（:4-7）**

目标片段：

```ts
// 画布事件转发（canvas_focus_node）与生图产出识别：canvas store 只依赖 pinia，
// 不反向依赖 appStore，静态 import 不会成环（与上面 gitStore 同一套权衡：
// 惰性 require 反而让「谁依赖谁」在编译期看不见）
import { useCanvasStore } from './canvas';
```

替换片段：

```ts
// 画布 store 已随旧画布删除；M2 重建同名 store 后在此恢复 import（设计 §3.1）
```

**1.5 appStore.ts：删产出采集辅助块（:110-186）**

整段删除 :110-186：`IMAGE_OUTPUT_TOOL_NAMES`（:113-117）、`IMAGE_EDIT_TOOL_NAMES`（:123）、`MARKDOWN_IMAGE_RE` / `PLAIN_IMAGE_RE` / `LOCAL_IMAGE_EXT_RE`（:135-138）、`extractLocalImagePaths`（:141-162）、`collectGeneratedImages`（:169-186），含各自的注释块（:110-112、:119-122、:125-134、:140、:164-168）。已核实这些符号在仓库内无其他引用方（`extractLocalImagePaths` 仅被 `collectGeneratedImages` 调用），留下会触发 vue-tsc 未使用告警/死代码。原位置放一行标记：

```ts
// 生图产出采集（collectGeneratedImages 及路径提取辅助）已随旧画布删除；
// M4 重建后继续把聊天生图产出推给新画布「最近产出托盘」（设计 §2.3 / §3.3）
```

**1.6 appStore.ts：删调用点 try/catch 整块（:6046-6053）**

目标片段：

```ts
        // 生图产出识别：结果文本里出现的本地图片路径放进画布「最近产出」托盘。
        // 单独 try/catch 包住——正则解析出任何意外都只丢托盘这一条，
        // 绝不影响上面已经完成的 tool 结果记录（工具轨迹比托盘重要得多）
        try {
          collectGeneratedImages(String(d.name || ''), String(d.result || ''));
        } catch (err) {
          console.debug('[canvas] 生图产出识别失败，已忽略', err);
        }
```

替换片段：整段删除（其上 :6045 的 `}` 与其下 :6054 的 `} else if (type === 'plan_updated') {` 直接相接，不改其余逻辑）。

**1.7 appStore.ts：事件分发改 no-op（:5632-5665）**

目标片段（:5632-5640，canvas_focus_node）：

```ts
      // 画布节点聚焦（Agent 侧发起「带我去看这张图」）：只改画布 store 的选中态，
      // 不参与会话 runtime 归属，处理完直接 return。
      // 节点 id 缺失时什么都不做——静默忽略比清空选中更安全（清空会让用户
      // 当前正在看的节点莫名丢失高亮）
      if (type === 'canvas_focus_node') {
        const nodeId = String(data.node_id || data.id || '');
        if (nodeId) useCanvasStore().selectNode(nodeId);
        return;
      }
```

替换片段：

```ts
      // 画布节点聚焦：M2 接新 store（canvas store 的选中态）。删除阶段 no-op，
      // 兜住 Agent 侧残留事件，避免未处理告警（设计 §2.5）。
      if (type === 'canvas_focus_node') {
        return;
      }
```

目标片段（:5641-5654，canvas_edit_result）：

```ts
      // 画布改图产出回执（就地改图 / 标注改图共用一个事件）：落成源图的下游节点
      // 或托盘条目，成败都清掉该节点的 pending 遮罩。独立支路，不参与会话
      // runtime 归属，处理完直接 return；失败时才在本会话对话流里说一句原因，
      // 成功不打扰用户（图自己出现在画布上就是最好的反馈）。
      if (type === 'canvas_edit_result') {
        const result = parseCanvasEditResult(data);
        const done = useCanvasStore().applyEditResult(result);
        if (!done && result.error) {
          // 桥事件处理器必须走 notifyChatLocal：notifyChat/broadcastChatNotice
          // 会把已广播过的事件再广播一次，各端重复插入
          this.notifyChatLocal(`改图失败：${result.error}`, 'error');
        }
        return;
      }
```

替换片段：

```ts
      // 画布改图产出回执：M2 接新 store——届时恢复
      // parseCanvasEditResult(data) → 新 store 回填动作（设计 §3.3）。
      // 删除阶段 no-op：旧 job 可能仍在回填，静默吞掉不产生未处理告警。
      if (type === 'canvas_edit_result') {
        return;
      }
```

目标片段（:5655-5665，canvas_video_result）：

```ts
      // 画布生成视频回执：把产物回填进受理时先落的 pending 视频节点（按
      // meta.job_id 认领），成败都清 pending；失败在对话流里说一句原因，
      // 成功不打扰（视频自己出现在画布上就是最好的反馈）。
      if (type === 'canvas_video_result') {
        const result = parseCanvasVideoResult(data);
        const done = useCanvasStore().applyVideoResult(result);
        if (!done && result.error) {
          this.notifyChatLocal(`视频生成失败：${result.error}`, 'error');
        }
        return;
      }
```

替换片段：

```ts
      // 画布生成视频回执：M2 接新 store——届时恢复
      // parseCanvasVideoResult(data) → 新 store 按 job_id 回填（设计 §3.3）。
      // 删除阶段 no-op，理由同 canvas_edit_result。
      if (type === 'canvas_video_result') {
        return;
      }
```

**不动**：:236 `RightPanelTabId` 联合类型与 :238-240 白名单里的 `'canvas'`；:453-550 的 `CanvasEditResultData` / `parseCanvasEditResult` / `CanvasVideoResultData` / `parseCanvasVideoResult`（**保留导出**，M2 复用；export 符号不触发未使用告警）。

**1.8 残留自查**（仓库根）：

```powershell
cd frontend/src; Get-ChildItem -Recurse -Include *.ts,*.vue -File | Select-String -Pattern "useCanvasStore|stores/canvas|CanvasPane|collectGeneratedImages|extractLocalImagePaths"; cd ../..
```

预期：无输出（`data-test="canvas-pane"` 字符串与 parse 函数不在此模式内，属预期保留）。

### 验证命令

```powershell
cd frontend; npx vitest run src/components/RightPanel.test.ts src/components/RightPanelSnip.test.ts src/App.panelResize.test.ts
if ($LASTEXITCODE -eq 0) { npm run build }
```

说明：4 个 canvas store 测试与 canvas 目录内测试已随本任务删除，不再单独跑；`RightPanel.test.ts` 真实挂载 RightPanel 组件，能验证占位容器不报错、页签白名单不丢 `'canvas'`。

### 提交

```powershell
git add -A frontend/src
git commit -m "refactor: 删除旧画布前端并以占位替换画布页签（M1 前端侧）"
```

---

## Task 2：后端删除

### Files

**删除：**

- `gui_canvas_mixin.py`（1418 行 CanvasBridgeMixin 整体）
- `tests/test_canvas_inline_edit.py`
- `tests/test_canvas_video_bridge.py`
- `tests/test_canvas_character_bridge.py`

**修改：**

- `gui_bridge.py`（仅 :57 与 :1118 两处）

已核实：`gui_canvas_mixin` / `CanvasBridgeMixin` 在仓库根 `.py`、`.spec`（ycode.spec 等三个打包脚本）与 `tests/` 中的引用只有 gui_bridge.py 两处与上述三个测试文件；`core/` 与 `plugins/` 无代码级引用（core/media_server.py:89 仅注释提及，不改）。

### 步骤

**2.1 删除文件：**

```powershell
Remove-Item -Force gui_canvas_mixin.py, tests/test_canvas_inline_edit.py, tests/test_canvas_video_bridge.py, tests/test_canvas_character_bridge.py
```

**2.2 gui_bridge.py：删 import（:57）**

目标片段（含上下文 :56-58）：

```python
from gui_snip_mixin import SnipBridgeMixin
from gui_canvas_mixin import CanvasBridgeMixin
from core.model_manager import (
```

替换片段：

```python
from gui_snip_mixin import SnipBridgeMixin
# 画布桥已随旧画布删除；M2 在此恢复新 CanvasBridgeMixin 的同位 import（设计 §4）
from core.model_manager import (
```

**2.3 gui_bridge.py：删继承（:1118）**

目标片段：

```python
class ApiBridge(GitBridgeMixin, BrowserBridgeMixin, SnipBridgeMixin, CanvasBridgeMixin):
```

替换片段：

```python
class ApiBridge(GitBridgeMixin, BrowserBridgeMixin, SnipBridgeMixin):
```

**不动**：:1325-1330 角色库初始化（`from core.character_store import init_character_storage` / `self._character_storage = init_character_storage(self._memory)`）与 `core/character_store.py`——新画布角色一致性继续使用（设计 §2.4 / §4 角色分组）。

### 验证命令

```powershell
python -m pytest tests/test_character_store.py -q
if ($LASTEXITCODE -eq 0) { python -c "import gui_bridge; print('gui_bridge import ok')" }
```

预期：角色库测试全绿；import 无 ModuleNotFoundError / NameError。

### 提交

```powershell
git add -A gui_bridge.py gui_canvas_mixin.py tests
git commit -m "refactor: 删除旧画布桥 CanvasBridgeMixin 及其测试（M1 后端侧）"
```

---

## Task 3：依赖清理

### Files

**修改：**

- `frontend/package.json`（:13）
- `frontend/package-lock.json`（由 npm install 自动更新，不手改）

### 步骤

**3.1 package.json 删依赖行**

目标片段（:12-14）：

```json
  "dependencies": {
    "@vue-flow/core": "^1.48.2",
    "@xterm/addon-fit": "^0.11.0",
```

替换片段：

```json
  "dependencies": {
    "@xterm/addon-fit": "^0.11.0",
```

**3.2 更新 lock：**

```powershell
cd frontend; npm install; cd ..
```

**3.3 残留确认（源码零引用）：**

```powershell
cd frontend/src; Get-ChildItem -Recurse -Include *.ts,*.vue,*.css -File | Select-String -Pattern '@vue-flow'; cd ../..
```

预期：无输出（Task 1 已删净 canvas 目录，`@vue-flow` 引用只曾存在于该目录）。再确认 lock 已同步：

```powershell
Select-String -Path frontend/package.json, frontend/package-lock.json -Pattern '@vue-flow'
```

预期：无输出。

### 验证命令

```powershell
cd frontend; npm run build
```

### 提交

```powershell
git add frontend/package.json frontend/package-lock.json
git commit -m "chore: 移除 @vue-flow/core 依赖（新画布自研平移层，设计 D6）"
```

---

## Task 4：总回归

### Files

只读验证，不改文件（若回归发现遗漏引用，回到对应任务修复后重新验证再提交修复）。

### 步骤与验证命令

**4.1 前端全量：**

```powershell
cd frontend; npm run build
if ($LASTEXITCODE -eq 0) { npx vitest run }
cd ..
```

预期：构建通过；vitest 全绿（canvas 相关测试文件已删除，剩余套件不含画布依赖）。

**4.2 后端全量（记录基线）：**

```powershell
python -m pytest tests/ -q
```

基线口径：**秘书体系存在 7 条已知的隔离性失败，不算本次回归**。执行时先记录失败清单，与「7 条秘书隔离性失败」逐条比对：凡属该基线的失败照录不修；出现基线之外的新失败即回归，必须定位到本计划的改动并修复后重跑。

**4.3 C 类共用面确认全绿**（附件缩略图/角色库/画笔截图/内嵌浏览器，测试文件均已核实存在）：

```powershell
python -m pytest tests/test_media_store.py tests/test_media_server.py tests/test_media_refs.py tests/test_media_migration.py tests/test_gui_media_save.py tests/test_character_store.py tests/test_gui_snip_mixin.py tests/test_screen_snip.py tests/test_embedded_browser.py tests/test_gui_browser_mixin.py -q
cd frontend; npx vitest run src/components/RightPanelSnip.test.ts src/components/AttachmentChips.test.ts src/components/AttachMenu.test.ts src/components/ImageLightbox.test.ts src/components/BrowserPane.test.ts src/components/MessageBubble.test.ts; cd ..
```

预期：全部通过（这些是共用面，不允许出现任何失败）。

**4.4 手工冒烟（可选但建议）**：启动 GUI，打开「画布」页签显示「画布重构中」；重启后会话快照恢复画布页签不报错；聊天生图、附件缩略图、画笔截图、内嵌浏览器功能正常。

**4.5 提交链检查：**

```powershell
git log --oneline -6
```

预期看到本计划的三个提交依序在链上（前端删除 → 后端删除 → 依赖清理），工作区干净（`git status` 无未提交改动）。若 4.1-4.3 期间产生了修复提交，一并核对。

### 提交

回归本身无文件改动则无需提交；如有回归修复：

```powershell
git add -A
git commit -m "fix: 修复旧画布删除后的回归问题（M1 总回归）"
```

---

## 自查记录

- 无 TBD/TODO/占位符：全文所有编辑点均给出真实「目标片段→替换片段」，摘录自 2026-11-04 带行号读取的文件内容。
- 行号核实：RightPanel.vue :70/:177-181/:232-234/:350/:361/:372；appStore.ts :4-7/:110-186/:236-240/:453-550/:5632-5665/:6046-6053；gui_bridge.py :57/:1118/:1325-1330；package.json :13——均与设计文档快照比对过，差异（如调用点实际 :6049-6053、parse 函数实际到 :550）已按当前文件修正。
- 依赖顺序：Task 1 内先删测试/store/canvas 目录再改 appStore（避免 canvas.inlineEdit/video.test.ts 的 import 断裂中间态）；Task 3 依赖 Task 1 删净 `@vue-flow` 源码引用；Task 2 独立于前端但按序执行；Task 4 收尾。
- 引用闭合核实：`useCanvasStore` / `stores/canvas` / `../canvas` 在删除清单之外的引用仅 RightPanel.vue 与 appStore.ts 的已列改造点；`gui_canvas_mixin` / `CanvasBridgeMixin` 在 Python 侧的引用仅 gui_bridge.py 两处与三个被删测试；三个 `.spec` 打包脚本无引用。
- 与规格一致：A 类全删（含 CanvasDock 死代码与 @vue-flow 修订注记）、B 类骨架保留（flush/'canvas' id/parse 函数/no-op 分发）、C 类零触碰、旧数据不清理不迁移、先删后建过渡态占位——逐条对齐设计 §2 与 §8 M1。