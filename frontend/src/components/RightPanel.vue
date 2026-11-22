<template>
  <div
    id="rightbar"
    ref="rightbarRef"
    data-theme-region="right_panel"
    :class="{ collapsed: store.rightPanelCollapsed }"
    :style="panelStyle"
    @mouseenter="computerPanelHovered = true"
    @mouseleave="computerPanelHovered = false"
  >
    <div v-if="showPicker && store.guiTheme?.id === 'builtin.qq2009'" class="qq09-show" data-test="qq09-show">
      <div class="qq09-show-head">
        <span class="qq09-show-title">QQ秀</span>
      </div>
      <div class="qq09-show-stage">
        <img class="qq09-show-bg" :src="'/qqshow-fzl.gif'" alt="">
        <img class="qq09-show-gif" :src="'/qqshow-fzl.gif'" alt="星瑶的非主流 QQ秀">
        <div class="qq09-show-nameplate">☆星瑶 · 非主流动图秀☆</div>
      </div>
    </div>

    <div class="tabbar" data-test="tabbar">
      <div class="tabbar-tabs">
        <button
          v-for="tab in store.rightPanelTabs"
          :key="tab"
          class="tabbar-tab"
          :class="{ active: isActiveTab(tab) }"
          :data-test="'tab-' + tab"
          :title="PANE_LABELS[tab]"
          @click="store.activateRightPanelTab(tab)"
        >
          <AppIcon :name="TAB_ICONS[tab] || 'circle'" :size="12" :stroke-width="2" />
          <span class="tabbar-label">{{ PANE_LABELS[tab] }}</span>
          <span
            class="tabbar-close"
            :data-test="'tab-close-' + tab"
            title="关闭页签"
            @click.stop="store.closeRightPanelTab(tab)"
          >
            <AppIcon name="x" :size="11" :stroke-width="2" />
          </span>
        </button>
      </div>
      <span class="tabbar-spacer"></span>
      <button
        class="tabbar-btn"
        data-test="tabbar-snip"
        title="画笔截图：圈选面板任意内容生成图片"
        :disabled="snipBusy"
        @click="enterSnipMode"
      >
        <AppIcon name="pencil" :size="13" :stroke-width="2" />
      </button>
      <button class="tabbar-btn" data-test="tabbar-add" title="打开标签页" @click="store.showRightPanelPicker()">
        <AppIcon name="plus" :size="13" :stroke-width="2" />
      </button>
      <button class="tabbar-btn" data-test="tabbar-collapse" title="收起面板" @click="store.toggleRightPanel()">
        <AppIcon name="x" :size="13" :stroke-width="2" />
      </button>
    </div>

    <div id="right-content" :class="{ flush: visualTab === 'browser' || visualTab === 'doc' }">
      <div v-if="visualPicker" class="tab-picker" data-test="tab-picker">
        <h3 class="tp-title">打开标签页</h3>
        <p class="tp-sub">选择要在侧边面板中打开的标签。</p>
        <div class="tp-cards">
          <button
            v-for="card in pickerCards"
            :key="card.id"
            class="tp-card"
            :data-test="'picker-card-' + card.id"
            :title="card.label"
            @click="store.openRightPanelTab(card.id)"
          >
            <AppIcon :name="card.icon" :size="15" :stroke-width="2" />
            <span>{{ card.label }}</span>
          </button>
        </div>
      </div>

      <template v-else>
      <div v-show="visualTab === 'files'" class="rpane active">
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

      <div v-show="visualTab === 'git'" class="rpane active" data-test="git-pane">
        <GitPanel />
      </div>

      <div v-show="visualTab === 'computer' && !!store.computerRun.run_id" class="rpane active" data-test="computer-pane">
        <ComputerControlPane />
      </div>

      <!-- 文档工作台：PDF / Word / Excel / PPT 在面板里用 pdf.js 自绘阅读，
           不叠加原生浏览器表面（用户要求点文档不要弹出浏览器 UI）。
           与浏览器页签互不影响：激活本页签时 BrowserPane 的 active 转 false，
           原生表面自动 hide。 -->
      <div v-show="visualTab === 'doc'" class="rpane active" data-test="doc-pane">
        <DocumentPane :active="activeTab === 'doc'" />
      </div>

      <div v-show="visualTab === 'browser'" class="rpane active" data-test="browser-pane">
        <BrowserPane :active="activeTab === 'browser'" />
      </div>

      <div v-show="visualTab === 'tasks'" class="rpane active" data-test="bg-task-pane">
        <div class="bgt-wrap">
          <div v-if="store.currentSessionBackgroundTasks.length > 1" class="bgt-list" data-test="bg-task-list">
            <button
              v-for="t in store.currentSessionBackgroundTasks"
              :key="t.task_id"
              class="bgt-item"
              :class="{ active: t.task_id === store.bgTaskDetailId }"
              :data-test="'bg-task-item-' + t.task_id"
              :title="t.command"
              @click="store.bgTaskDetailId = t.task_id"
            >
              <span class="bgt-dot" :class="{ idle: t.no_output_yet }"></span>
              <span class="bgt-item-cmd">{{ t.command || t.task_id }}</span>
              <span class="bgt-item-time">{{ t.running_time }}</span>
            </button>
          </div>
          <div class="bgt-head">
            <span class="bgt-cmd" :title="detailCommand">{{ detailCommand }}</span>
            <span class="bgt-state" :class="{ done: !detailRunning }">{{ detailRunning ? '运行中' : '已结束' }}</span>
          </div>
          <div v-if="detailMeta" class="bgt-meta" data-test="bg-task-meta">{{ detailMeta }}</div>
          <div v-if="detailRunning && detailNoOutput" class="bgt-hint" data-test="bg-task-hint">
            进程运行中，日志暂无命令输出（只有启动标记）。任务仍在正常执行——若命令末尾接了
            <code>| Select-Object -Last</code>、<code>| tail</code> 等缓冲管道，输出会到进程结束才一次性写入日志。
          </div>
          <pre ref="bgtLogRef" class="bgt-log" data-test="bg-task-log">{{ detailOutput || (detailRunning ? '正在读取任务输出…' : '未找到任务输出。') }}</pre>
        </div>
      </div>

      <div v-show="visualTab === 'novel'" class="rpane active" data-test="novel-pane-wrap">
        <NovelPane :active="activeTab === 'novel'" />
      </div>

      <div v-show="visualTab === 'subagent'" class="rpane active" data-test="subagent-pane">
        <SubagentPane :active="activeTab === 'subagent'" />
      </div>
      </template>
    </div>

    <RightPanelSnip
      v-if="store.rightPanelSnipActive && snipSnapshot"
      :snapshot="snipSnapshot"
      @close="exitSnipMode"
    />

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
import { useAppStore, type RightPanelTabId } from '../stores/appStore';
import AppIcon from './AppIcon.vue';
import FileTreeNode from './FileTreeNode.vue';
import ComputerControlPane from './ComputerControlPane.vue';
import BrowserPane from './BrowserPane.vue';
import DocumentPane from './DocumentPane.vue';
import GitPanel from './GitPanel.vue';
import SubagentPane from './SubagentPane.vue';
import NovelPane from './NovelPane.vue';
import RightPanelSnip from './RightPanelSnip.vue';
import { renderMarkdown } from '../utils/markdown';
import { useIsMobile } from '../composables/useViewport';

const store = useAppStore();

// ── 画笔截图：入口按钮 → 捕获冻结帧 → 挂覆盖层 ──
// 捕获必须在置位 rightPanelSnipActive 之前完成：原生浏览器表面此刻仍可见，
// 冻结帧才包含其像素；置位后 BrowserPane 的表面门控自动 hide 表面，关闭时
// 既有状态机自动 re-show。快照 dataURL 只走本地 ref（模态瞬态，不进 store）
const rightbarRef = ref<HTMLElement | null>(null);
const snipSnapshot = ref('');
const snipBusy = ref(false);

async function enterSnipMode() {
  if (store.rightPanelSnipActive || snipBusy.value) return;
  const el = rightbarRef.value;
  const api = typeof window !== 'undefined' ? window.pywebview?.api : undefined;
  if (!el || !api?.capture_right_panel_region) {
    store.notifyChat('桌面桥未就绪，无法使用画笔截图');
    return;
  }
  const r = el.getBoundingClientRect();
  snipBusy.value = true;
  try {
    const res = await api.capture_right_panel_region({
      x: Math.round(r.left),
      y: Math.round(r.top),
      width: Math.round(r.width),
      height: Math.round(r.height),
    });
    if (res?.ok && res.data_url) {
      snipSnapshot.value = res.data_url;
      store.rightPanelSnipActive = true;
    } else {
      store.notifyChat(res?.msg || '面板截图失败，请重试');
    }
  } catch {
    store.notifyChat('面板截图失败：桥接调用异常');
  } finally {
    snipBusy.value = false;
  }
}

function exitSnipMode() {
  store.rightPanelSnipActive = false;
  snipSnapshot.value = '';
}

// 切会话时退出画笔截图：store 侧经 _bindSessionRuntime 复位 rightPanelSnipActive，
// 这里一并清掉本地冻结帧快照——否则会话切换后本地仍残留上一会话的帧
watch(() => store.currentSessionName, () => { exitSnipMode(); });

const isMobile = useIsMobile();
const panelStyle = computed(() => {
  if (isMobile.value) return {};
  const style: Record<string, string> = {
    width: `${store.rightPanelWidth}px`,
    '--rp-width': `${store.rightPanelWidth}px`,
  };
  if (store.rightPanelCollapsed) style.marginRight = `-${store.rightPanelWidth}px`;
  return style;
});

const activeTab = computed<RightPanelTabId | ''>(() => {
  if (store.rightPanelCollapsed) return '';
  if (store.rightPanelPicker || store.rightPanelTabs.length === 0) return '';
  return store.rightPanelActiveTab;
});
const showPicker = computed(() => !store.rightPanelCollapsed && activeTab.value === '');

const PANEL_SLIDE_MS = 240;
const visualTab = ref<RightPanelTabId | ''>(activeTab.value);
const visualPicker = ref(showPicker.value);
let visualClearTimer: number | undefined;

watch([activeTab, showPicker], ([nextTab, nextPicker]) => {
  window.clearTimeout(visualClearTimer);
  if (!store.rightPanelCollapsed) {
    visualTab.value = nextTab;
    visualPicker.value = nextPicker;
    return;
  }
  visualClearTimer = window.setTimeout(() => {
    visualTab.value = nextTab;
    visualPicker.value = nextPicker;
  }, PANEL_SLIDE_MS);
}, { immediate: true });
function isActiveTab(tab: RightPanelTabId) {
  return visualTab.value === tab;
}

const pickerCards = computed<Array<{ id: RightPanelTabId; label: string; icon: string }>>(() => {
  const cards: Array<{ id: RightPanelTabId; label: string; icon: string }> = [
    { id: 'files', label: '文件', icon: 'folder' },
    { id: 'git', label: 'Git', icon: 'git-branch' },
    { id: 'doc', label: '文档', icon: 'file' },
    { id: 'browser', label: '星眼 · 浏览器', icon: 'globe' },
    { id: 'tasks', label: '星港 · 后台任务', icon: 'square-terminal' },
    { id: 'subagent', label: '星舰 · 子代理', icon: 'blocks' },
    { id: 'novel', label: '小说', icon: 'file' }
  ];
  if (store.systemSettings.subagent_enabled) return cards;
  return cards.filter(card => card.id !== 'subagent');
});

const PANE_LABELS: Record<RightPanelTabId, string> = {
  files: '文件',
  git: 'Git',
  doc: '文档',
  browser: '星眼 · 浏览器',
  tasks: '星港 · 后台任务',
  computer: '操控',
  subagent: '星舰 · 子代理',
  novel: '小说'
};
const TAB_ICONS: Partial<Record<RightPanelTabId, string>> = {
  files: 'folder',
  git: 'git-branch',
  doc: 'file',
  browser: 'globe',
  tasks: 'square-terminal',
  computer: 'scan',
  subagent: 'blocks',
  novel: 'file'
};

async function autoOpenComputerTabOnce() {
  const runId = store.computerRun.run_id;
  if (!runId || runId === store.computerLastAutoOpenedRunId) return;
  if (!store.computerControlBarVisible) return;
  // 内置浏览器操控不弹「操控」工作台：右侧直接呈现浏览器页签（embedded_browser_automation
  // 已按会话打开并激活它），用户看到的就是一个可围观的浏览器而不是操控面板。
  // 外部浏览器（Edge/Chrome）则需要在对应会话开出「操控」预览页签以显示实时截图与状态。
  // 仍记 lastAutoOpenedRunId：避免后续 watch（归属补齐）反复进入快照校准分支。
  if (store.computerRunIsEmbeddedBrowser) {
    store.computerLastAutoOpenedRunId = runId;
    return;
  }
  // 归属会话优先取 run 自己的 owner_session_id（事件信封/快照都带）。为空时
  // 先拉一次完整快照校准——绝不能直接回落 currentSessionName：run 在后台会话
  // 启动、用户正看另一个会话时，回落会把「操控」页签登记到用户眼前的会话上
  // （2026-10-30 反馈：openquilla 会话的浏览器操控显示在小说会话下）。
  // 没有桥（浏览器开发态/测试环境）时无从校准，保持同步回落，不引入额外等待。
  let owner = store.computerRun.owner_session_id || '';
  const automationApi = (window as any).pywebview?.api?.get_computer_control_status;
  if (!owner && typeof automationApi === 'function') {
    try {
      const snapshot = await (window as any).pywebview.api.get_computer_control_status();
      if (snapshot && snapshot.run_id) {
        store._applyComputerSnapshot(snapshot as Record<string, unknown>);
      }
    } catch { /* 校准失败：保持未知归属，下面按兼容路径回落 */ }
    if (store.computerRun.run_id !== runId || !store.computerControlBarVisible) return;
    owner = store.computerRun.owner_session_id || '';
  }
  store.computerLastAutoOpenedRunId = runId;
  store.openRightPanelTabForSession(
    owner || store.currentSessionName,
    'computer',
    { expand: false },
  );
}
watch(
  () => [store.computerRun.run_id, store.computerRun.owner_session_id] as const,
  () => { void autoOpenComputerTabOnce(); },
);

const detailOutput = ref('');
const bgtLogRef = ref<HTMLElement | null>(null);
const detailTask = computed(
  () => store.currentSessionBackgroundTasks.find(t => t.task_id === store.bgTaskDetailId)
);
const detailCommand = computed(
  () => detailTask.value?.command || store.bgTaskDetailId
);
const detailRunning = computed(() => !!detailTask.value);

const detailMeta = computed(() => {
  const t = detailTask.value;
  if (!t) return '';
  const parts: string[] = [];
  if (t.pid) parts.push(`PID ${t.pid}`);
  if (t.turn_id) parts.push(`回合 ${t.turn_id.slice(0, 8)}`);
  const size = t.output_size;
  if (size !== undefined) {
    parts.push(size < 1024 ? `输出 ${size} B` : `输出 ${(size / 1024).toFixed(1)} KB`);
  }
  if (t.last_output_age !== null && t.last_output_age !== undefined) {
    parts.push(`最后输出 ${t.last_output_age}s 前`);
  }
  return parts.join(' · ');
});

const detailNoOutput = computed(() => {
  const t = detailTask.value;
  if (t && t.no_output_yet !== undefined) return t.no_output_yet;
  const txt = detailOutput.value.trim();
  return txt === '' || /^\[turn:[0-9a-f]{8}\] 后台任务启动$/.test(txt);
});

let bgtTimer: ReturnType<typeof setInterval> | undefined;
let bgtInFlight = false;
async function fetchTaskOutput() {
  const id = store.bgTaskDetailId;
  if (!id || !(window.pywebview && window.pywebview.api) || bgtInFlight) return;
  bgtInFlight = true;
  try {
    void store.refreshBackgroundTasks();
    const res = await window.pywebview.api.get_background_task_output(id, 200);
    if (store.bgTaskDetailId === id && res?.ok) {
      const el = bgtLogRef.value;
      const follow = !el || (el.scrollHeight - el.scrollTop - el.clientHeight < 30);
      detailOutput.value = String(res.output || '');
      await nextTick();
      if (follow && bgtLogRef.value) bgtLogRef.value.scrollTop = bgtLogRef.value.scrollHeight;
    }
  } catch { }
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
  store.closeRightPanelTab('computer');
}

// 终态起算 60 秒：到点若用户仍在看（停在操控工作台且未折叠、未返回选择页，
// 或鼠标悬停在面板内）则顺延一个周期，否则关闭。「在看就不关」是唯一规则
function scheduleComputerAutoDismiss() {
  clearComputerDismissTimer();
  if (!store.computerRun.run_id || !store.computerRunTerminal) return;
  computerDismissTimer = setTimeout(() => {
    computerDismissTimer = null;
    const stillWatching = activeTab.value === 'computer' || computerPanelHovered.value;
    if (stillWatching) {
      scheduleComputerAutoDismiss();
      return;
    }
    dismissComputerPane();
  }, COMPUTER_AUTO_DISMISS_MS);
}

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

const isMarkdownPreview = computed(() => ['md', 'markdown'].includes(previewExt.value));
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
watch(activeTab, (t) => {
  if (t === 'files') maybeAutoRefreshFileTree();
  if (t === 'computer' && store.computerRun.run_id) void store.refreshComputerRun();
});
const onWindowFocus = () => { if (activeTab.value === 'files') maybeAutoRefreshFileTree(); };

onMounted(() => {
  void autoOpenComputerTabOnce();
  window.addEventListener('focus', onWindowFocus);
});

onUnmounted(() => {
  window.removeEventListener('focus', onWindowFocus);
  window.clearTimeout(visualClearTimer);
  clearComputerDismissTimer();
  stopTaskOutputPolling();
});
</script>

<style scoped>
#rightbar {
  width: 330px;
  border-left: 1px solid var(--xy-border, var(--border));
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  transition: margin-right 0.24s cubic-bezier(0.4, 0, 0.2, 1);
  position: relative;
}
#rightbar.collapsed {
  margin-right: -330px;
}
.right-panel-slide-enter-active,
.right-panel-slide-leave-active {
  transition: margin-right 0.24s cubic-bezier(0.4, 0, 0.2, 1);
  will-change: margin-right;
}
.right-panel-slide-enter-from,
.right-panel-slide-leave-to {
  margin-right: calc(-1 * var(--rp-width, 330px));
}
@media (prefers-reduced-motion: reduce) {
  #rightbar,
  .right-panel-slide-enter-active,
  .right-panel-slide-leave-active {
    transition: none;
  }
}
.tabbar {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 44px;
  padding: 0 10px;
  border-bottom: 1px solid var(--xy-border, var(--border));
  flex-shrink: 0;
  min-width: 0;
}
.tabbar-tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
}
.tabbar-tabs::-webkit-scrollbar {
  display: none;
}
.tabbar-spacer {
  flex: 1;
}
.tabbar-tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 168px;
  padding: 4px 6px 4px 9px;
  border-radius: 6px;
  border: 1px solid transparent;
  background: none;
  color: var(--text-faint);
  font-size: 12px;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  transition: 0.15s;
  flex-shrink: 0;
}
.tabbar-tab:hover {
  background: var(--bg2);
  color: var(--text-dim);
}
.tabbar-tab.active {
  border-color: var(--border);
  background: var(--bg2);
  color: var(--text);
}
.tabbar-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tabbar-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  border-radius: 4px;
  color: var(--text-faint);
  opacity: 0.55;
  transition: 0.15s;
  flex-shrink: 0;
}
.tabbar-close:hover {
  background: var(--bg3);
  color: var(--text);
  opacity: 1;
}
.tabbar-btn {
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
  flex-shrink: 0;
}
.tabbar-btn:hover {
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
  overflow-x: auto;
  padding: 12px;
}
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
#right-content.flush {
  padding: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
#right-content.flush .rpane.active {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
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
.bgt-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.bgt-item {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  padding: 6px 9px;
  border-radius: 7px;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-dim);
  font-size: 11.5px;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: 0.15s;
}
.bgt-item:hover {
  border-color: var(--text-faint);
}
.bgt-item.active {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--text);
}
.bgt-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--green);
  flex-shrink: 0;
}
.bgt-dot.idle {
  background: var(--orange);
}
.bgt-item-cmd {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--mono);
}
.bgt-item-time {
  flex-shrink: 0;
  font-size: 10.5px;
  color: var(--text-faint);
}
.bgt-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  font-size: 11px;
  color: var(--text-faint);
}
.bgt-hint {
  padding: 8px 11px;
  border: 1px solid rgba(240, 136, 62, 0.35);
  background: rgba(240, 136, 62, 0.08);
  border-radius: 8px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--text-dim);
}
.bgt-hint code {
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--orange);
}

.modal-mask {
  position: fixed;
  inset: 0;
  padding: 16px;
  background: var(--xy-modal-mask, rgba(0, 0, 0, 0.36));
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
}
.modal-mask.show {
  display: flex;
}
.modal {
  width: 640px;
  max-width: calc(100vw - 32px);
  background: var(--xy-card, var(--bg1));
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 16px;
  box-shadow: var(--xy-shadow-pop, 0 16px 40px rgba(0, 0, 0, 0.35));
  overflow: hidden;
}
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
.fp-image-wrap {
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding: 16px;
  max-height: 70vh;
  overflow: auto;
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
