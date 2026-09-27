<template>
  <div
    id="rightbar"
    data-theme-region="right_panel"
    :class="{ collapsed: store.rightPanelCollapsed }"
    :style="panelStyle"
    @mouseenter="computerPanelHovered = true"
    @mouseleave="computerPanelHovered = false"
  >
    <!-- 工作台头部：替代原页签栏，承载当前工作台标题与返回选择页入口 -->
    <div v-if="!store.rightPanelPicker" class="pane-head">
      <span class="pane-title" data-test="pane-title">{{ activePaneLabel }}</span>
      <button class="pane-close" data-test="pane-close" title="返回标签选择" aria-label="返回标签选择" @click="closePaneToPicker()">
        <AppIcon name="x" :size="13" :stroke-width="2" />
      </button>
    </div>

    <div id="right-content">
      <!-- 打开标签页选择页（ZCode 式侧板）：顶栏按钮等中性入口展开面板时
           先选择要看的内容，页签栏保留随时可切；定向入口经
           expandRightPanelDirect 跳过本页 -->
      <div v-if="store.rightPanelPicker" class="tab-picker" data-test="tab-picker">
        <h3 class="tp-title">打开标签页</h3>
        <p class="tp-sub">选择要在侧边面板中打开的标签。</p>
        <div class="tp-cards">
          <button
            v-for="card in pickerCards"
            :key="card.id"
            class="tp-card"
            :data-test="'picker-card-' + card.id"
            @click="openPicked(card.id)"
          >
            <AppIcon :name="card.icon" :size="15" :stroke-width="2" />
            <span>{{ card.label }}</span>
          </button>
        </div>
      </div>

      <template v-else>
      <!-- 文件树面板 -->
      <div v-show="activeTab === 'files'" class="rpane active">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)">
          <span style="font-size:12px;color:var(--text-dim);font-weight:600">项目文件</span>
          <span style="display:flex;gap:6px">
            <button
              class="chip-btn"
              style="font-size:11.5px;padding:3px 9px"
              :disabled="store.fileTreeRefreshing"
              title="重新扫描项目文件夹（外部新增/删除的文件会更新出来，展开状态保留）"
              @click="store.refreshFileTree()"
            >{{ store.fileTreeRefreshing ? '刷新中…' : '' }}<AppIcon v-if="!store.fileTreeRefreshing" name="refresh" :size="12" /> {{ store.fileTreeRefreshing ? '' : '刷新' }}</button>
            <button class="chip-btn" style="font-size:11.5px;padding:3px 9px" title="引入新的项目文件夹" @click="store.openWorkspaceFolder()">引入新文件夹</button>
          </span>
        </div>

        <div v-if="!store.fileTree || store.fileTree.length === 0" style="padding:20px 10px;text-align:center;color:var(--text-faint);font-size:12px">
          {{ store.workdir ? '当前项目文件夹为空' : '当前为通用自由对话，暂未引入项目文件夹' }}
        </div>

        <div v-else id="file-tree">
          <FileTreeNode v-for="node in store.fileTree" :key="node.n" :node="node" :parent-path="store.workdir" />
        </div>
      </div>

      <!-- 源代码管理面板（Git 工作台，规格 §11.2） -->
      <div v-show="activeTab === 'git'" class="rpane active" data-test="git-pane">
        <GitPanel />
      </div>

      <!-- 电脑操控面板（设计 16.2）：出现过 run 后自动切入的“操控”工作台 -->
      <div v-show="activeTab === 'computer' && !!store.computerRun.run_id" class="rpane active" data-test="computer-pane">
        <ComputerControlPane />
      </div>

      <!-- 浏览器工作台：承载电脑操控的浏览器实时画面与状态轨迹，
           无操控会话时与操控页签共用同一引导态 -->
      <div v-show="activeTab === 'browser'" class="rpane active" data-test="browser-pane">
        <ComputerControlPane />
      </div>

      <!-- 后台任务执行过程：底部执行条“详情”触发，2 秒轮询日志尾部 -->
      <div v-show="activeTab === 'tasks'" class="rpane active" data-test="bg-task-pane">
        <div class="bgt-wrap">
          <div class="bgt-head">
            <span class="bgt-cmd" :title="detailCommand">{{ detailCommand }}</span>
            <span class="bgt-state" :class="{ done: !detailRunning }">{{ detailRunning ? '运行中' : '已结束' }}</span>
          </div>
          <pre ref="bgtLogRef" class="bgt-log" data-test="bg-task-log">{{ detailOutput || (detailRunning ? '正在读取任务输出…' : '未找到任务输出。') }}</pre>
        </div>
      </div>
      </template>
    </div>

    <!-- 文件预览弹窗：Teleport 到 body——右栏分区带 isolation 分层，
         fixed 模态留在分区内会被后绘制的兄弟分区盖住 -->
    <Teleport to="body">
      <div class="modal-mask" :class="{ show: store.filePreview.show }" @click.self="store.closeFilePreview()">
        <div class="modal file-preview-modal">
          <div class="modal-head">
            <div class="mh-ico"><AppIcon name="file" :size="16" /></div>
            <div class="fp-title">
              <h3 class="fp-name">{{ fileBaseName(store.filePreview.path) }}</h3>
              <p class="fp-path">{{ store.filePreview.path }}</p>
            </div>
            <button class="chip-btn" title="在系统中打开此文件" @click="store.openInSystem(store.filePreview.path)"><AppIcon name="folder" :size="12" /> 在系统中打开</button>
            <button class="chip-btn" title="关闭预览" @click="store.closeFilePreview()"><AppIcon name="x" :size="12" :stroke-width="2" /> 关闭预览</button>
          </div>
          <div class="fp-body">
            <div v-if="store.filePreview.loading" class="fp-loading">加载中…</div>
            <div v-else-if="store.filePreview.kind === 'image'" class="fp-image-wrap">
              <img class="fp-image" :src="store.filePreview.dataUrl" :alt="fileBaseName(store.filePreview.path)" />
            </div>
            <div v-else-if="isMarkdownPreview" class="fp-md md-body" v-html="renderedMarkdown"></div>
            <pre v-else class="fp-pre"><code class="hljs" v-html="highlightedPreview"></code></pre>
            <div v-if="store.filePreview.truncated" class="fp-truncated"><AppIcon name="triangle-alert" :size="12" /> 文件内容过长，已截断显示</div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';
import FileTreeNode from './FileTreeNode.vue';
import ComputerControlPane from './ComputerControlPane.vue';
import GitPanel from './GitPanel.vue';
import { useGitStore } from '../stores/gitStore';
import { renderMarkdown } from '../utils/markdown';
import { useIsMobile } from '../composables/useViewport';

type TabType = 'files' | 'git' | 'browser' | 'tasks' | 'computer';

const store = useAppStore();

// 桌面端宽度由拖拽手柄写入 store，此处内联生效；折叠负 margin 跟随实际
// 宽度。移动端（<=768px）整体隐藏右面板，不输出内联样式。
const isMobile = useIsMobile();
const panelStyle = computed(() => {
  if (isMobile.value) return {};
  const style: Record<string, string> = { width: `${store.rightPanelWidth}px` };
  if (store.rightPanelCollapsed) style.marginRight = `-${store.rightPanelWidth}px`;
  return style;
});
const activeTab = ref<TabType>('files');

// 选择页三张卡（对齐 ZCode 官方侧板）：文件 / Git / 浏览器；
// “操控”不进选择页，只在出现过电脑操控 run 后自动切入（设计 16.2）
const pickerCards: Array<{ id: TabType; label: string; icon: string }> = [
  { id: 'files', label: '文件', icon: 'folder' },
  { id: 'git', label: 'Git', icon: 'git-branch' },
  { id: 'browser', label: '浏览器', icon: 'globe' }
];

const PANE_LABELS: Record<TabType, string> = {
  files: '文件',
  git: 'Git',
  browser: '浏览器',
  tasks: '后台任务',
  computer: '操控'
};
const activePaneLabel = computed(() => PANE_LABELS[activeTab.value] || '');

function openPicked(tab: TabType) {
  activeTab.value = tab;
  store.rightPanelPicker = false;
  if (tab === 'files') maybeAutoRefreshFileTree();
}

// 工作台头部 ×：返回选择页（hub-and-spoke），不销毁工作台状态；
// 操控 run 的停止走顶部安全条/自动收场
function closePaneToPicker() {
  store.rightPanelPicker = true;
}
// run 开始时首次自动切入操控工作台（设计 16.2）：每个 run 只开一次（记录到
// computerLastAutoOpenedRunId），用户返回选择页不抢回，新 run 再开一次；
// 挂载补跑覆盖“run 启动时右面板尚未挂载（如设置页打开中）”的场景
function autoOpenComputerTabOnce() {
  const runId = store.computerRun.run_id;
  if (!runId || runId === store.computerLastAutoOpenedRunId) return;
  if (!store.computerControlBarVisible) return;
  store.computerLastAutoOpenedRunId = runId;
  store.rightPanelPicker = false;
  activeTab.value = 'computer';
}
watch(() => store.computerRun.run_id, autoOpenComputerTabOnce);

// 状态栏分支项点击 → 打开源代码管理工作台（gitStore.panelOpenNonce 由 App.vue 触发）
const gitStore = useGitStore();
watch(
  () => gitStore.panelOpenNonce,
  () => {
    if (gitStore.panelOpenNonce > 0) {
      activeTab.value = 'git';
    }
  }
);

// 底部执行条“详情” → 打开后台任务执行过程工作台（nonce 每次点击自增，
// 同一任务重复点击也能从选择页/其他工作台切回）
watch(
  () => store.bgTaskOpenNonce,
  () => {
    if (store.bgTaskOpenNonce > 0 && store.bgTaskDetailId) {
      store.rightPanelPicker = false;
      activeTab.value = 'tasks';
    }
  }
);

// ── 后台任务执行过程：2 秒轮询日志尾部，跟随滚动到底 ──
const detailOutput = ref('');
const bgtLogRef = ref<HTMLElement | null>(null);
const detailCommand = computed(
  () => store.backgroundTasks.find(t => t.task_id === store.bgTaskDetailId)?.command || store.bgTaskDetailId
);
const detailRunning = computed(() =>
  store.backgroundTasks.some(t => t.task_id === store.bgTaskDetailId)
);

let bgtTimer: ReturnType<typeof setInterval> | undefined;
let bgtInFlight = false;
async function fetchTaskOutput() {
  const id = store.bgTaskDetailId;
  if (!id || !(window.pywebview && window.pywebview.api) || bgtInFlight) return;
  bgtInFlight = true;
  try {
    const res = await window.pywebview.api.get_background_task_output(id, 200);
    if (store.bgTaskDetailId === id && res?.ok) {
      const el = bgtLogRef.value;
      const follow = !el || (el.scrollHeight - el.scrollTop - el.clientHeight < 30);
      detailOutput.value = String(res.output || '');
      await nextTick();
      if (follow && bgtLogRef.value) bgtLogRef.value.scrollTop = bgtLogRef.value.scrollHeight;
    }
  } catch { /* 桥未就绪/单次失败：保留下次轮询 */ }
  finally { bgtInFlight = false; }
}
function stopTaskOutputPolling() {
  if (bgtTimer !== undefined) {
    clearInterval(bgtTimer);
    bgtTimer = undefined;
  }
}
watch(
  () => [activeTab.value, store.bgTaskDetailId] as const,
  ([tab, id]) => {
    stopTaskOutputPolling();
    detailOutput.value = '';
    if (tab === 'tasks' && id) {
      void fetchTaskOutput();
      bgtTimer = setInterval(() => { void fetchTaskOutput(); }, 2000);
    }
  },
);

// ── 操控页签关闭（2026-09-10 设计）──
// 手动关闭与终态自动关闭共用同一出口：清计时器 → 清空运行态 → 页签回退
const COMPUTER_AUTO_DISMISS_MS = 60000;
let computerDismissTimer: ReturnType<typeof setTimeout> | null = null;
const computerPanelHovered = ref(false);

function clearComputerDismissTimer() {
  if (computerDismissTimer !== null) {
    clearTimeout(computerDismissTimer);
    computerDismissTimer = null;
  }
}

function dismissComputerPane() {
  clearComputerDismissTimer();
  if (!store.computerRun.run_id) return;
  store.clearComputerRun();
  // 停在操控工作台时 run 消失后无内容可看，回退到选择页
  if (activeTab.value === 'computer') store.rightPanelPicker = true;
}

// 终态起算 60 秒：到点若用户仍在看（停在操控工作台且未折叠、未返回选择页，
// 或鼠标悬停在面板内）则顺延一个周期，否则关闭。「在看就不关」是唯一规则
function scheduleComputerAutoDismiss() {
  clearComputerDismissTimer();
  if (!store.computerRun.run_id || !store.computerRunTerminal) return;
  computerDismissTimer = setTimeout(() => {
    computerDismissTimer = null;
    const stillWatching = (activeTab.value === 'computer' && !store.rightPanelPicker && !store.rightPanelCollapsed)
      || computerPanelHovered.value;
    if (stillWatching) {
      scheduleComputerAutoDismiss();
      return;
    }
    dismissComputerPane();
  }, COMPUTER_AUTO_DISMISS_MS);
}

// run_id / 状态变化都重排计时：新 run 会先清掉旧计时器
watch(
  () => [store.computerRun.run_id, store.computerRun.status],
  () => scheduleComputerAutoDismiss(),
  { immediate: true },
);

function fileBaseName(path: string) {
  if (!path) return '';
  const idx = Math.max(path.lastIndexOf('\\'), path.lastIndexOf('/'));
  return idx >= 0 ? path.slice(idx + 1) : path;
}

// ---------------- 文件预览语法高亮 ----------------
// highlight.js 语言名与扩展名不一致的补充映射（py/sh 等常见别名 hljs 已内置）
const HLJS_EXT_ALIAS: Record<string, string> = {
  vue: 'xml', cs: 'csharp', yml: 'yaml',
  md: 'markdown', conf: 'ini', toml: 'ini', env: 'ini',
  jsx: 'javascript', tsx: 'typescript', mk: 'makefile',
};

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

const previewExt = computed(() => {
  const name = fileBaseName(store.filePreview.path || '');
  const dot = name.lastIndexOf('.');
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : '';
});

// Markdown 文件以“阅读样式”渲染（标题/列表/加粗等），而不是显示源码
const isMarkdownPreview = computed(() => ['md', 'markdown'].includes(previewExt.value));
// md 预览前统一换行符：Windows 文件多为 CRLF，\r 会导致代码块/标题等行级语法失配
const renderedMarkdown = computed(() =>
  renderMarkdown((store.filePreview.content || '').replace(/\r\n/g, '\n'))
);

// highlight.js 体积大（lib/common 约 180 kB min）：懒加载，首次预览文件时才
// 拉取独立 chunk，避免拖慢首屏主包。模块加载完成前/加载失败时先回退纯文本
// 转义展示；hljs 模块 promise 永不 reject（失败归一为 null），加载完成后
// content 变化会重算本 computed 补上高亮。
type HljsModule = {
  getLanguage: (name: string) => unknown;
  highlight: (code: string, opts: { language: string; ignoreIllegals?: boolean }) => { value: string };
};
let hljsPromise: Promise<HljsModule | null> | null = null;
function loadHljs(): Promise<HljsModule | null> {
  if (!hljsPromise) {
    hljsPromise = import('highlight.js/lib/common')
      .then((mod) => ((mod as Record<string, unknown>).default ?? mod) as HljsModule)
      .catch(() => null);
  }
  return hljsPromise;
}
// 加载完成后写入 ref：highlightedPreview computed 依赖它，加载完成自动重算补上高亮
const hljsResolved = ref<HljsModule | null>(null);
watch(() => [store.filePreview.path, store.filePreview.content], ([, content]) => {
  if (!content) return;
  void loadHljs().then((mod) => {
    if (mod && hljsResolved.value !== mod) hljsResolved.value = mod;
  });
});

const highlightedPreview = computed(() => {
  const content = store.filePreview.content || '';
  if (!content) return '';
  const hljs = hljsResolved.value;
  if (hljs) {
    let lang = previewExt.value;
    if (lang && !hljs.getLanguage(lang)) {
      lang = HLJS_EXT_ALIAS[lang] || '';
    }
    if (lang && hljs.getLanguage(lang)) {
      try {
        return hljs.highlight(content, { language: lang, ignoreIllegals: true }).value;
      } catch {
        // 高亮失败则退化为纯文本
      }
    }
  }
  return escapeHtml(content);
});

// 文件树自动刷新：进入“文件”工作台或窗口重新聚焦时触发（节流 2 秒），
// 覆盖“用户在外部文件夹里新增/删除了文件再回来看”的场景；也可点 ↻ 手动刷新
let lastFileTreeAutoRefresh = 0;
function maybeAutoRefreshFileTree() {
  const now = Date.now();
  if (now - lastFileTreeAutoRefresh < 2000) return;
  lastFileTreeAutoRefresh = now;
  if (store.workdir) void store.refreshFileTree();
}
// 进入操控工作台时校准 run 快照（设计 16.4）：_js_queue 满时允许丢新事件，
// 进入工作台即用 get_computer_control_status() 复核终态
watch(activeTab, (t) => {
  if (t === 'files') maybeAutoRefreshFileTree();
  if (t === 'computer' && store.computerRun.run_id) void store.refreshComputerRun();
});
const onWindowFocus = () => { if (activeTab.value === 'files') maybeAutoRefreshFileTree(); };

onMounted(() => {
  // 挂载补跑一次自动开工作台（run 启动时右面板可能尚未挂载，见函数注释）
  autoOpenComputerTabOnce();
  window.addEventListener('focus', onWindowFocus);
});

onUnmounted(() => {
  window.removeEventListener('focus', onWindowFocus);
  clearComputerDismissTimer();
  stopTaskOutputPolling();
});
</script>

<style scoped>
#rightbar {
  width: 330px;
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  transition: margin-right 0.25s;
}
#rightbar.collapsed {
  margin-right: -330px;
}
/* 工作台头部：标题 + 返回选择页 */
.pane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.pane-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-dim);
}
.pane-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 5px;
  border: none;
  background: none;
  color: var(--text-faint);
  cursor: pointer;
  transition: 0.15s;
}
.pane-close:hover {
  background: var(--bg3);
  color: var(--text);
}
.icon-spin {
  animation: rp-icon-spin 0.9s linear infinite;
}
@keyframes rp-icon-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* 打开标签页选择页（ZCode 式侧板）：标题居中 + 三张竖排大卡 */
.tab-picker {
  padding: 40px 6px 20px;
  text-align: center;
}
.tp-title {
  margin: 0 0 6px;
  font-size: 16.5px;
  font-weight: 700;
  color: var(--text);
}
.tp-sub {
  margin: 0 0 24px;
  font-size: 12.5px;
  color: var(--text-faint);
}
.tp-cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.tp-card {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 13px 16px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text);
  font-size: 13.5px;
  font-weight: 600;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: 0.15s;
}
.tp-card:hover {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
}

#right-content {
  flex: 1;
  overflow-y: auto;
  overflow-x: auto; /* 深层文件树横向滚动，滚动条固定在面板底部 */
  padding: 12px;
}
/* 文件树宽度取最宽一行的内容宽度，撑出横向滚动区间 */
#file-tree {
  display: inline-block;
  min-width: 100%;
}
.rpane {
  display: none;
}
.rpane.active {
  display: block;
}

.chip-btn {
  background: var(--bg3);
  border: 1px solid var(--border);
  color: var(--text-dim);
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: 0.15s;
}
.chip-btn:hover {
  border-color: var(--text-faint);
  color: var(--text);
}

/* 后台任务执行过程 */
.bgt-wrap {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bgt-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.bgt-cmd {
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bgt-state {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--green);
}
.bgt-state.done {
  color: var(--text-faint);
}
.bgt-log {
  margin: 0;
  padding: 10px 12px;
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-dim);
  max-height: calc(100vh - 220px);
  overflow-y: auto;
}

/* 文件预览弹窗 */
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  backdrop-filter: blur(2px);
}
.modal-mask.show {
  display: flex;
}
.modal {
  width: 640px;
  max-width: 90vw;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6);
  overflow: hidden;
}
/* 文件预览弹窗加宽，保证代码行能完整显示 */
.file-preview-modal {
  width: 980px;
  max-width: 92vw;
}
.modal-head {
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 10px;
}
.modal-head .mh-ico {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: var(--accent-soft);
  color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  flex-shrink: 0;
}
.fp-title {
  flex: 1;
  min-width: 0;
}
.fp-name {
  font-size: 14.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fp-path {
  font-size: 11px;
  color: var(--text-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fp-body {
  padding: 0;
  background: var(--code-bg);
}
.fp-loading {
  padding: 24px;
  text-align: center;
  color: var(--text-faint);
  font-size: 13px;
}
/* 图片预览：居中适配弹窗，超大图限高可滚动 */
.fp-image-wrap {
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding: 16px;
  max-height: 70vh;
  overflow: auto;
  /* 浅色棋盘格背景，透明 PNG 也能看清轮廓 */
  background:
    repeating-conic-gradient(var(--bg2) 0% 25%, var(--code-bg) 0% 50%)
    0 0 / 16px 16px;
}
.fp-image {
  max-width: 100%;
  height: auto;
  object-fit: contain;
}
.fp-pre {
  margin: 0;
  padding: 16px 18px;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-dim);
  max-height: 60vh;
  overflow-y: auto;
}
.fp-truncated {
  padding: 8px 18px;
  font-size: 11.5px;
  color: var(--orange);
  border-top: 1px solid var(--border);
  background: var(--bg1);
}
.fp-md {
  padding: 16px 20px;
  max-height: 60vh;
  overflow-y: auto;
  font-size: 13px;
  line-height: 1.7;
}
</style>
