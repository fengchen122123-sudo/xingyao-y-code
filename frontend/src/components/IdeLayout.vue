<template>
  <div class="ide-layout">
    <!-- ActivityBar 的约定 prop/事件是 model / update:model（不是 v-model 默认的
         modelValue），所以这里必须用 v-model:model 传参，否则活动栏点击不生效 -->
    <ActivityBar v-model:model="activity" @action="onActivityAction" />

    <div v-if="activity === 'files'" class="ide-side" data-test="explorer-panel">
      <div class="side-head">
        <span class="side-title">资源管理器</span>
        <div class="side-acts">
          <!-- 两个按钮都只在已打开文件夹时出现：没有工作区时刷新会直接返回（refreshFileTree
               要求 workdir），留着就是个点了没反应的按钮，与下方空状态重复。

               新建文件：后端有 ide_write_file（expected_hash 传空串 = 明确写入），
               可以建空文件；但**没有**新建目录的接口（gui_bridge.py 里没有 mkdir 类
               桥方法），所以设计稿里的「新建文件夹」按钮刻意不实现——放一个点不动
               的按钮比少一个按钮更糟。 -->
          <button
            v-if="store.workdir"
            type="button"
            class="icon-btn"
            title="新建文件"
            data-test="explorer-new-file"
            @click="startCreateFile"
          ><AppIcon name="plus" :size="15" /></button>
          <button
            v-if="store.workdir"
            type="button"
            class="icon-btn"
            title="刷新"
            data-test="explorer-refresh"
            @click="store.refreshFileTree()"
          ><AppIcon name="refresh" :size="15" /></button>
        </div>
      </div>

      <!-- 新建文件的行内输入：后端没有弹输入框的桥方法，用行内输入框收集文件名，
           Enter 提交、Esc 取消。空名字不提交（避免建出无名文件）。 -->
      <form v-if="creatingFile" class="new-file-row" @submit.prevent="submitCreateFile">
        <input
          ref="newFileInputEl"
          v-model="newFileName"
          class="new-file-input"
          data-test="explorer-new-file-input"
          placeholder="文件名（可含子目录）"
          @keydown.esc="cancelCreateFile"
        />
      </form>

      <div v-if="store.workdir" class="tree" data-test="explorer-tree">
        <FileTreeNode
          v-for="node in store.fileTree"
          :key="node.n"
          :node="node"
          :parent-path="store.workdir"
        />
      </div>

      <!-- 未打开文件夹的空状态（设计稿 .side-empty）：图标 + 文案 + 真实可用的
           「打开文件夹」按钮（走 appStore.openWorkspaceFolder，与侧栏入口同一路径） -->
      <div v-else class="side-empty" data-test="explorer-empty">
        <AppIcon name="folder-open" :size="26" />
        <div class="side-empty-title">尚未打开文件夹</div>
        <button
          type="button"
          class="btn primary"
          data-test="explorer-open-folder"
          @click="store.openWorkspaceFolder()"
        >打开文件夹</button>
        <div class="side-empty-hint">星瑶的对话仍可使用，打开项目后文件树会出现在这里</div>
      </div>

      <!-- 大纲：设计稿里是「当前文件符号树」。核查结论——gui_bridge.py / lsp_manager.py
           没有 documentSymbol 类桥方法（只有通用的 lsp_request 透传，而它要求文档先经
           didOpen 同步、且符号树渲染、定位等一整套编辑器联动都还不存在），因此这里
           只渲染可折叠区段 + 一行诚实空态，不伪造任何符号条目。 -->
      <div v-if="store.workdir" class="sect" data-test="explorer-outline">
        <button
          type="button"
          class="sect-head"
          :aria-expanded="outlineOpen"
          @click="outlineOpen = !outlineOpen"
        >
          <AppIcon :name="outlineOpen ? 'chevron-down' : 'chevron-right'" :size="12" />
          <span>大纲</span>
        </button>
        <div v-if="outlineOpen" class="sect-body">
          <p class="sect-empty" data-test="explorer-outline-empty">语言服务器提供符号后显示</p>
        </div>
      </div>

      <!-- 时间线：设计稿里是「当前文件的 Git 提交历史」。核查结论——gui_git_mixin.py
           只有 git_load_history(workdir, skip, limit)（整个仓库的历史），没有按文件
           取历史的方法，因此同样只给折叠区段 + 空态说明。 -->
      <div v-if="store.workdir" class="sect" data-test="explorer-timeline">
        <button
          type="button"
          class="sect-head"
          :aria-expanded="timelineOpen"
          @click="timelineOpen = !timelineOpen"
        >
          <AppIcon :name="timelineOpen ? 'chevron-down' : 'chevron-right'" :size="12" />
          <span>时间线</span>
        </button>
        <div v-if="timelineOpen" class="sect-body">
          <p class="sect-empty" data-test="explorer-timeline-empty">Git 提交历史接入后显示</p>
        </div>
      </div>
    </div>
    <div v-else-if="activity === 'git'" class="ide-side">
      <GitPanel />
    </div>
    <!-- 运行和调试：DebugPane 是 S3 已交付的调试面板（调用栈/变量/监视/调试控制台），
         这里替换掉旧的「断点与调用栈将在 S3 接入后显示」占位——那句文案早已过时，
         而且它把 debug 与其余未知面板混在同一个 v-else 里，点调试看不到真面板。 -->
    <div v-else-if="activity === 'debug'" class="ide-side">
      <DebugPane />
    </div>
    <!-- 搜索：后端目前**没有**文件搜索桥方法（已核查 core/ide/ 与 gui_bridge.py，
         不存在 ide_search_files / search_files 之类的接口），因此这里只能是空态。
         文案不承诺任何不可用能力（不写 Ctrl+P 之类的快捷方式，因为前端也没有实现它），
         也不放假的搜索输入框——放个输入框却搜不出东西比空态更误导。 -->
    <div v-else-if="activity === 'search'" class="ide-side">
      <div class="ide-panel-empty" data-test="search-empty">
        <AppIcon name="search" :size="22" />
        <p class="ide-panel-empty-title">文件搜索将在后续版本接入</p>
        <p class="ide-panel-empty-hint">当前可在「资源管理器」中逐级展开目录查找文件</p>
      </div>
    </div>
    <!-- 扩展：扩展市场同样未接入，只给空态说明，不伪造"已安装/可安装"列表 -->
    <div v-else-if="activity === 'extensions'" class="ide-side">
      <div class="ide-panel-empty" data-test="extensions-empty">
        <AppIcon name="blocks" :size="22" />
        <p class="ide-panel-empty-title">扩展市场尚未接入</p>
        <p class="ide-panel-empty-hint">当前版本不支持浏览或安装扩展</p>
      </div>
    </div>
    <div v-else class="ide-side">
      <div class="ide-empty">请从左侧活动栏选择一个面板</div>
    </div>

    <div class="ide-main">
      <!-- 调试工具条挂在编辑器区顶部：它是「运行 / 调试当前文件」在 IDE 模式下的唯一
           入口。Task 9 只要求 Create 组件、全仓并无挂载点，导致计划自己的验收条款
           （Task 13 Step 5「点调试工具条『运行』」）在真实 IDE 模式里执行不了——
           等于交了死代码（S3-T9 评审）。工具条对活动文件操作，因此随编辑器区常驻，
           不随标签是否存在而退场（无文件时按钮自己 disabled）。 -->
      <DebugToolbar />
      <EditorTabs v-if="ide.tabs().length" />
      <!-- 无活动标签时分两种情形：没有工作区 → 欢迎卡片（设计稿 .welcome，动作逐项
           核查过后端能力）；已有工作区 → 提示从文件树打开文件（此时再让用户看到
           「打开文件夹」是误导，项目明明已经打开） -->
      <WorkspaceWelcome v-if="!ide.tabs().length && !store.workdir" />
      <div v-else-if="!ide.tabs().length" class="ide-empty-main">从左侧文件树打开文件</div>
      <EditorPane v-else :key="ide.activePath()" :path="ide.activePath()" />

      <!-- v-show 而非 v-if（S3-T12 接线）：底部面板收起期间必须保持挂载——调试控制台
           的行缓冲、断点 gutter 与执行行高亮都活在组件里，v-if 卸载会把它们一并丢掉，
           与「收起期间调试输出不丢」冲突。隐藏的是外面这层定高容器，所以收起后不占高度。 -->
      <div v-show="ide.bottomPanelOpen" class="ide-bottom" :style="{ height: bottomHeight + 'px' }">
        <BottomPanel @close="ide.toggleBottomPanel()" />
      </div>
      <div v-if="!ide.bottomPanelOpen" class="ide-bottom-open">
        <button type="button" class="icon-btn" @click="ide.toggleBottomPanel()">显示面板</button>
      </div>
    </div>

    <!-- 对话区占位：IDE 模式下 #center 由 CSS 停靠在 #main 右侧（见 App.vue 的
         #center.ide-docked），这里只负责把这块宽度让出来，别让编辑器区伸到对话区下面。
         宽度必须与 #center.ide-docked 保持一致（368px）。
         收起状态用 store.ideChatCollapsed 而不是组件本地 ref：收起按钮在 App.vue 的
         对话头部、恢复入口是本组件的活动栏「星瑶」动作，本地状态跨不过组件边界。 -->
    <div v-show="!store.ideChatCollapsed" class="ide-chat-gap" data-test="ide-chat-gap" />

    <!-- 对话区收起后的恢复入口：只留活动栏「星瑶」图标不够显眼，「收起」会像一次
         不可逆操作。这个按钮贴着编辑器区右缘，仅在收起时出现 -->
    <button
      v-if="store.ideChatCollapsed"
      type="button"
      class="ide-chat-restore"
      title="展开对话区"
      data-test="ide-chat-restore"
      @click="store.ideChatCollapsed = false"
    >
      <AppIcon name="chevron-left" :size="15" />
    </button>
  </div>
</template>

<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from 'vue';

import ActivityBar from './ActivityBar.vue';
import AppIcon from './AppIcon.vue';
import BottomPanel from './BottomPanel.vue';
import DebugPane from './DebugPane.vue';
import DebugToolbar from './DebugToolbar.vue';
import EditorPane from './EditorPane.vue';
import EditorTabs from './EditorTabs.vue';
import FileTreeNode from './FileTreeNode.vue';
import GitPanel from './GitPanel.vue';
import WorkspaceWelcome from './WorkspaceWelcome.vue';
import { useAppStore } from '../stores/appStore';
import { useIdeStore } from '../stores/ideStore';

const store = useAppStore();
const ide = useIdeStore();
// 用 ide 别名避免与 appStore 混用；文件树与工作区状态都来自 appStore，
// 标签、断点、底部面板状态都来自 ideStore
const activity = ref('files');
const bottomHeight = ref(200);

// 对话区收起状态放在 appStore.ideChatCollapsed（不是组件本地 ref）：收起按钮在
// App.vue 的对话头部、恢复入口是本组件的活动栏「星瑶」动作，本地状态跨不过组件边界。

// 大纲默认展开（里面只有一行空态说明，展开才看得见）；时间线默认折叠，与设计稿一致
const outlineOpen = ref(true);
const timelineOpen = ref(false);

// 新建文件的行内输入状态
const creatingFile = ref(false);
const newFileName = ref('');
const newFileInputEl = ref<HTMLInputElement | null>(null);

function startCreateFile() {
  creatingFile.value = true;
  newFileName.value = '';
  // 下一帧再聚焦：input 此刻还没渲染出来
  void nextTick(() => newFileInputEl.value?.focus());
}

function cancelCreateFile() {
  creatingFile.value = false;
  newFileName.value = '';
}

/**
 * 新建空文件。
 *
 * 后端没有"新建文件"专用接口，只有 `ide_write_file(path, content, expected_hash)`：
 * expected_hash 传空串是**明确的覆盖语义**（见 gui_bridge.py 的注释），因此写入前
 * 必须先探测目标是否已存在——否则同名文件会被静默清空。存在就拒绝并提示。
 */
async function submitCreateFile() {
  const name = newFileName.value.trim();
  if (!name || !store.workdir) return;
  const api = (window as any).pywebview?.api;
  if (!api?.ide_write_file) return;
  const base = store.workdir.replace(/[\\/]+$/, '');
  const path = `${base}\\${name}`;
  try {
    const existing = await api.ide_read_file(path);
    if (existing?.ok) {
      store.notifyChat(`文件已存在：${name}`);
      return;
    }
    const res = await api.ide_write_file(path, '', '', 'utf-8', false, 'lf');
    if (!res?.ok) {
      store.notifyChat(res?.msg || `新建文件失败：${name}`);
      return;
    }
    cancelCreateFile();
    // 先刷新文件树再打开：文件树里没有这个节点时打开也能成功，但刷新让用户
    // 立刻看到它落在哪一层
    await store.refreshFileTree();
    void ide.openFile(path);
  } catch (e) {
    store.notifyChat(`新建文件失败：${(e as Error)?.message || name}`);
  }
}

// 活动栏的动作型项（ActivityBar 里的 kind: 'action'）在这里落地。它们不是左侧面板，
// 所以走独立事件而不是 update:model——否则点「设置」会把当前面板顶成空白。
function onActivityAction(id: string) {
  if (id === 'settings') {
    // 复用 Sidebar 的「设置」入口（appStore.openModelSettings）保证两条路径一致：
    // 它除了置 settingsOpen=true 还会拉取模型配置，只置标志位会打开一个空设置页
    void store.openModelSettings();
    return;
  }
  if (id === 'ai') {
    // 对话区由 App.vue 渲染、CSS 停靠在 #main 右侧，不在本组件树内、拿不到 ref，
    // 只能按 DOM 查找。找不到就静默：活动栏不该因为对话区尚未挂载而报错。
    // 若对话区处于收起态，先展开再聚焦（展开后 DOM 才可见，focus 才有意义）。
    const focusInput = () => {
      const input = document.querySelector<HTMLElement>('#center textarea, #center input');
      input?.focus();
    };
    if (store.ideChatCollapsed) {
      store.ideChatCollapsed = false;
      void nextTick(focusInput);
      return;
    }
    focusInput();
  }
}

// 先登记会话、再恢复标签：顺序不能反（Task 8 评审 Important）——
// ideStore 的标签桶按会话隔离，若反过来，先 openFile 的标签会落进 '' 桶，
// 会话绑定后成为孤儿、用户已打开的文件凭空消失。
onMounted(async () => {
  ide.setSession(store.currentSessionName);
  // 进 IDE 模式就把当前会话的工作区"打开"（= 文件树加载好）。语义已由 appStore
  // 定死：有工作区才拉树，没有工作区直接返回、由欢迎页引导用户选文件夹，
  // **不自动弹目录选择器**。不 await：拉树可能耗时，布局不该被它挡住。
  void store.ensureIdeWorkspace();
  const api = (window as any).pywebview?.api;
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
    // 恢复 active 前必须确认该标签真的恢复了：快照里的 active 可能指向已被删除或
    // 移出工作区的文件——上面的 openFile 读取失败会被 try/catch 静默吞掉，标签根本没建。
    // 若无条件 activate，活动标签会指向一个不存在的标签，并随后经 persistLayout 把这条
    // 陈旧 active 写回快照（重开一次脏一次）。这里用 tab() 与 openFile 相同的查表口径
    // （realpath 精确比对 + 调用方路径别名）判存在性；不存在就保持恢复过程中最后成功
    // 打开的那个标签，空快照则保持空。
    if (res.layout.active && ide.tab(res.layout.active)) ide.activate(res.layout.active);
  }
});

// IDE 布局常驻不重建：会话切换时也把标签桶重新绑定到新会话，
// 否则切换会话后新打开的文件会写进上一个会话的桶（跨会话串标签）。
watch(() => store.currentSessionName, (name) => {
  ide.setSession(name);
  // 切会话 = 切工作区（工作区跟随会话），必须回收语言服务器：进程与工作区绑定
  // （LSP initialize 的 rootUri 写死），留着上一个工作区不仅白占内存，还会让
  // 迟到的诊断/跳转指向错误目录。回收幂等，与下面的 workdir 监听重复调用无副作用。
  void ide.syncWorkspace();
  // 新会话的工作区文件树同样要跟上（无工作区则保持欢迎页，不弹选择器）
  void store.ensureIdeWorkspace();
});

// 工作区变了但会话没变（例如给「对话」会话引入项目文件夹，或手机上按路径选目录）：
// 同样要回收，否则旧工作区的语言服务器会一直跑着（验收 S2-3：切工作区无残留进程）。
watch(() => store.workdir, () => {
  void ide.syncWorkspace();
  // 用户刚选好文件夹：文件树要立刻出现在资源管理器里（ensureIdeWorkspace 内部
  // 已加载就不重复打后端）
  void store.ensureIdeWorkspace();
});
</script>

<style scoped>
.ide-layout {
  display: flex;
  flex: 1;
  min-height: 0;
  min-width: 0;
  /* 对话区收起后的浮动恢复按钮用绝对定位贴在本布局右缘，这里建立定位上下文 */
  position: relative;
}

.ide-side {
  /* 资源管理器侧栏宽度按设计稿 .sidebar：246px（原 260px） */
  width: 246px;
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--bg1);
  border-right: 1px solid var(--border);
}

/* 面板头（设计稿 .side-head）：34px 高、小号加字距标题 + 右侧图标动作区 */
.side-head {
  height: 34px;
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 8px 0 12px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.7px;
  color: var(--text-dim);
  text-transform: uppercase;
}

.side-head .side-acts {
  margin-left: auto;
  display: flex;
  gap: 1px;
}

/* 面板头里的图标按钮收成设计稿的 24px（全局 .icon-btn 是 32px，对 34px 的头太高）。
   底部「显示面板」那颗带文字的按钮不受影响——它不在 .side-head 里。 */
.side-head .icon-btn {
  width: 24px;
  height: 24px;
  border-radius: 5px;
}

/* 新建文件的行内输入行 */
.new-file-row {
  flex: 0 0 auto;
  padding: 0 8px 6px;
}

.new-file-input {
  width: 100%;
  height: 26px;
  padding: 0 8px;
  border: 1px solid var(--accent);
  border-radius: 5px;
  background: var(--bg0);
  color: var(--text);
  font-family: inherit;
  font-size: 12.5px;
  outline: none;
}

/* 文件树滚动区（设计稿 .tree） */
.tree {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding-bottom: 6px;
  font-size: 12.5px;
}

/* 未打开文件夹的空状态（设计稿 .side-empty） */
.side-empty {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 0 22px;
  text-align: center;
  color: var(--text-faint);
  font-size: 12px;
}

.side-empty > svg {
  opacity: 0.55;
}

.side-empty-title {
  color: var(--text-dim);
}

.side-empty-hint {
  font-size: 11.5px;
  line-height: 1.6;
}

/* 按钮（设计稿 .btn / .btn.primary） */
.btn {
  height: 28px;
  padding: 0 13px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg1);
  color: var(--text);
  font-family: inherit;
  font-size: 12.5px;
  cursor: pointer;
}

.btn:hover {
  background: var(--bg2);
}

.btn.primary {
  background: var(--accent);
  border-color: var(--accent);
  /* 强调色底上的文字取面板底色：浅色主题下是白字、深色主题下是深色字，
     两个主题都有足够对比度，同时不引入任何硬编码色值 */
  color: var(--bg1);
  font-weight: 600;
}

.btn.primary:hover {
  background: var(--accent);
  filter: brightness(1.06);
}

/* 目录区段（大纲 / 时间线，设计稿 .sect） */
.sect {
  flex: 0 0 auto;
  border-top: 1px solid var(--border);
}

.sect-head {
  width: 100%;
  height: 30px;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 0 10px;
  border: 0;
  background: transparent;
  font-family: inherit;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.7px;
  color: var(--text-dim);
  text-transform: uppercase;
  text-align: left;
  cursor: pointer;
}

.sect-head:hover {
  background: var(--bg2);
  color: var(--text);
}

.sect-body {
  padding: 2px 0 8px;
}

.sect-empty {
  margin: 0;
  padding: 4px 14px 10px;
  color: var(--text-faint);
  font-size: 11.5px;
  line-height: 1.6;
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

/* 对话区占位：宽度与 App.vue 的 #center.ide-docked 保持一致（368px）。
   这里不再承载对话 DOM——#center 由 CSS 停靠在 #main 右侧，这块只负责让出宽度，
   避免编辑器区伸到对话区下面 */
.ide-chat-gap {
  width: 368px;
  flex: 0 0 auto;
  min-width: 0;
}

/* 对话区收起后的浮动恢复按钮：贴编辑器区右缘，仅在收起时出现 */
.ide-chat-restore {
  position: absolute;
  top: 44px;
  right: 10px;
  z-index: 7;
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg2);
  color: var(--text-dim);
  cursor: pointer;
  transition: 0.15s;
}

.ide-chat-restore:hover {
  background: var(--bg3);
  color: var(--text);
}

.ide-empty,
.ide-empty-main {
  padding: 12px;
  font-size: 12px;
  color: var(--text-faint);
}

/* 占位空态（搜索 / 扩展）：只说明能力边界，不放任何假按钮 */
.ide-panel-empty {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 16px;
  text-align: center;
  color: var(--text-dim);
}

.ide-panel-empty-title {
  margin: 0;
  font-size: 12px;
  color: var(--text);
}

.ide-panel-empty-hint {
  margin: 0;
  font-size: 11.5px;
  color: var(--text-faint);
  line-height: 1.6;
}

.ide-empty-main {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
