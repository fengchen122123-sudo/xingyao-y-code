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
    <!-- QQ2009 皮肤专属：动态 QQ秀（非主流动图），置顶于右侧面板（页签条之上）——
         仅内置 QQ2009 主题且面板处于「打开标签页」状态时渲染；
         样式在 themes/qq2009.css，本组件不加样式 -->
    <div v-if="showPicker && store.guiTheme?.id === 'builtin.qq2009'" class="qq09-show" data-test="qq09-show">
      <div class="qq09-show-head">
        <span class="qq09-show-title">QQ秀</span>
      </div>
      <!-- src 走绑定写法（同 Topbar 的 :src="'/icon.png'"）：public 资源经
           绝对路径引用，静态 url() 会被构建期当作文件资产去解析 -->
      <div class="qq09-show-stage">
        <img class="qq09-show-bg" :src="'/qqshow-fzl.gif'" alt="">
        <img class="qq09-show-gif" :src="'/qqshow-fzl.gif'" alt="星瑶的非主流 QQ秀">
        <div class="qq09-show-nameplate">☆星瑶 · 非主流动图秀☆</div>
      </div>
    </div>

    <!-- 页签条：多页签并存（浏览器/Git/文件/后台任务/子代理…互不覆盖），
         每个页签可单独关闭；"+"打开选择页追加页签，最右 × 收起整个面板 -->
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

<!-- 浏览器与文档工作台都要「满幅」：去掉内容区内边距，让内部占位区/滚动区
         自己撑满并接管滚动（浏览器靠这个矩形叠加原生控件） -->
    <div id="right-content" :class="{ flush: visualTab === 'browser' || visualTab === 'doc' }">
      <!-- 打开标签页选择页（ZCode 式侧板）：无页签/点"+"时出现，从卡片
           追加页签；已打开的页签保留在页签条上随时可切回。定向入口经
           openRightPanelTab 直接开页签跳过本页 -->
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
      <!-- 文件树面板 -->
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

      <!-- 源代码管理面板（Git 工作台，规格 §11.2） -->
      <div v-show="visualTab === 'git'" class="rpane active" data-test="git-pane">
        <GitPanel />
      </div>

      <!-- 电脑操控面板（设计 16.2）：出现过 run 后自动切入的“操控”工作台 -->
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

      <!-- 浏览器工作台：DOM 工具栏 + 原生表面占位区；真实网页由宿主叠加的
           WebView2 控件渲染，前端负责矩形上报与起始页/不支持态 -->
      <div v-show="visualTab === 'browser'" class="rpane active" data-test="browser-pane">
        <BrowserPane :active="activeTab === 'browser'" />
      </div>

      <!-- 后台任务执行过程：底部执行条“详情”触发，2 秒轮询日志尾部与任务快照。
           多任务时顶部列表切换；元信息行带输出字节心跳，运行中日志只有启动
           标记时给出缓冲管道解释（Select-Object -Last 会攒到进程结束才输出） -->
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

      <!-- 小说工作台（设计 §7）：只读展示书名/字数/章节/追踪状态，激活时拉一次 -->
      <div v-show="visualTab === 'novel'" class="rpane active" data-test="novel-pane-wrap">
        <NovelPane :active="activeTab === 'novel'" />
      </div>

<!-- 子代理执行过程：聊天卡片“查看”触发，1 秒轮询任务阶段历史 -->
      <div v-show="visualTab === 'subagent'" class="rpane active" data-test="subagent-pane">
        <SubagentPane :active="activeTab === 'subagent'" />
      </div>
      </template>
    </div>

    <!-- 画笔截图覆盖层：进入模式前已捕获整块面板冻结帧（含原生浏览器表面），
         挂出时 rightPanelSnipActive 会让原生表面 hide，覆盖层完整接管右面板 -->
    <RightPanelSnip
      v-if="store.rightPanelSnipActive && snipSnapshot"
      :snapshot="snipSnapshot"
      @close="exitSnipMode"
    />

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

// 桌面端宽度由拖拽手柄写入 store，此处内联生效；折叠负 margin 跟随实际
// 宽度。移动端（<=768px）整体隐藏右面板，不输出内联样式。
const isMobile = useIsMobile();
const panelStyle = computed(() => {
  if (isMobile.value) return {};
  const style: Record<string, string> = {
    width: `${store.rightPanelWidth}px`,
    // 滑入/滑出动画的位移量来源：Transition 的 enter-from 用 calc(-1 * var(--rp-width))
    // 把面板推到视口右侧外，和 collapsed 的负 margin 是同一套位移
    '--rp-width': `${store.rightPanelWidth}px`,
  };
  if (store.rightPanelCollapsed) style.marginRight = `-${store.rightPanelWidth}px`;
  return style;
});

// 激活页签（store 驱动）：面板收起或停在选择页时为 ''——所有工作台隐藏，
// BrowserPane/SubagentPane 的 active prop 同时转 false（原生浏览器表面
// 隐藏、详情轮询停止），切会话时的浏览器隔离也经由这条路生效
const activeTab = computed<RightPanelTabId | ''>(() => {
  if (store.rightPanelCollapsed) return '';
  if (store.rightPanelPicker || store.rightPanelTabs.length === 0) return '';
  return store.rightPanelActiveTab;
});
const showPicker = computed(() => !store.rightPanelCollapsed && activeTab.value === '');

// ── 滑出过场期间保持画面 ──
// 收起时 activeTab 会立刻转 ''（原生表面 hide、轮询停止、切会话的浏览器隔离都靠它），
// 直接跟着清空会让"滑出"变成一个空面板平移。这里给渲染用的页签加一层缓冲：
// 收起时先原样保留，等滑出动画（PANEL_SLIDE_MS）跑完再清。
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
  // 收起：先原样留住在显示的画面，滑出跑完后再切到真实值（此时已 display:none）
  visualClearTimer = window.setTimeout(() => {
    visualTab.value = nextTab;
    visualPicker.value = nextPicker;
  }, PANEL_SLIDE_MS);
}, { immediate: true });
// 页签高亮跟随渲染用的页签（收起滑出期间保持高亮，不提前熄灭）
function isActiveTab(tab: RightPanelTabId) {
  return visualTab.value === tab;
}

// 选择页六张卡（对齐 ZCode 官方侧板，外加小说工作台）；“操控”不进选择页，只在
// 出现过电脑操控 run 后自动切入（设计 16.2）。星名只对应这一个场景（一名一景），
// 页签/卡片统一用「星名 · 通用名」的写法，新用户第一眼就知道是什么
// 全局子代理开关关闭时不出子代理卡片：开关关掉就意味着不再派发子代理，
// 卡片点开只会是一片空页，入口跟着消失；重新打开即恢复
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
// 小说页签图标：图标表里没有 book，用「稿件」语义的 file（不新增图标资源）
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

// run 开始时首次自动切入操控工作台（设计 16.2）：每个 run 只开一次（记录到
// computerLastAutoOpenedRunId），用户关闭页签不抢回，新 run 再开一次；
// 挂载补跑覆盖“run 启动时右面板尚未挂载（如设置页打开中）”的场景
async function autoOpenComputerTabOnce() {
  const runId = store.computerRun.run_id;
  if (!runId || runId === store.computerLastAutoOpenedRunId) return;
  if (!store.computerControlBarVisible) return;
  // 浏览器操控不弹「操控」工作台：右侧直接呈现浏览器页签（embedded_browser_automation
  // 已按会话打开并激活它），用户看到的就是一个可围观的浏览器而不是操控面板。
  // 仍记 lastAutoOpenedRunId：避免后续 watch（归属补齐）反复进入快照校准分支。
  if (store.computerRunIsBrowser) {
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
    // 静默校准：这是自动开页签的内部步骤，失败不该弹「操控状态刷新失败」的框
    // （那条提示留给用户手动刷新），校准不到就按旧后端回落。
    try {
      const snapshot = await (window as any).pywebview.api.get_computer_control_status();
      if (snapshot && snapshot.run_id) {
        store._applyComputerSnapshot(snapshot as Record<string, unknown>);
      }
    } catch { /* 校准失败：保持未知归属，下面按兼容路径回落 */ }
    // 校准期间 run 换代/收场：交给新 run 的触发逻辑，别替旧 run 登记页签
    if (store.computerRun.run_id !== runId || !store.computerControlBarVisible) return;
    owner = store.computerRun.owner_session_id || '';
  }
  store.computerLastAutoOpenedRunId = runId;
  // 只登记页签不摊开面板（设计 16.2 旧语义）：面板已展开的直接落在操控页签，
  // 收起中的等用户手动展开时再看到。
  // 按 run 归属会话写入：run 在 A 会话启动、用户正看 B 会话时写进 A 的 runtime，
  // 不把 B 的页签集写脏（快照也没有归属的旧后端才回落当前会话，保住入口）
  store.openRightPanelTabForSession(
    owner || store.currentSessionName,
    'computer',
    { expand: false },
  );
}
// owner 也要监听：归属随快照/后续事件补齐后仍要补开一次（每个 run 只开一次）
watch(
  () => [store.computerRun.run_id, store.computerRun.owner_session_id] as const,
  () => { void autoOpenComputerTabOnce(); },
);

// ── 后台任务执行过程：2 秒轮询日志尾部，跟随滚动到底 ──
const detailOutput = ref('');
const bgtLogRef = ref<HTMLElement | null>(null);
const detailTask = computed(
  () => store.currentSessionBackgroundTasks.find(t => t.task_id === store.bgTaskDetailId)
);
const detailCommand = computed(
  () => detailTask.value?.command || store.bgTaskDetailId
);
const detailRunning = computed(() => !!detailTask.value);

// 元信息行：PID / 回合短码 / 输出字节心跳 / 距最后输出秒数（旧后端无字段时整体隐藏）
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

// 运行中但日志还没有实际命令输出：优先用后端心跳字段判定（旧后端退化为
// 检查输出文本是否只有启动标记行）
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
    // 页签激活期间同步刷新任务快照：时长/输出心跳按 2 秒节拍跳动
    // （App.vue 全局轮询为 5 秒，只够底部执行条保鲜）
    void store.refreshBackgroundTasks();
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
  // 操控页签随收场一并关闭：run 消失后页签已无内容可看（关闭后自动切到
  // 邻居页签，全部关完回选择页）
  store.closeRightPanelTab('computer');
}

// 终态起算 60 秒：到点若用户仍在看（停在操控工作台且未折叠、未返回选择页，
// 或鼠标悬停在面板内）则顺延一个周期，否则关闭。「在看就不关」是唯一规则
function scheduleComputerAutoDismiss() {
  clearComputerDismissTimer();
  if (!store.computerRun.run_id || !store.computerRunTerminal) return;
  computerDismissTimer = setTimeout(() => {
    computerDismissTimer = null;
    // activeTab 已包含“页签激活、面板展开、非选择页”全部条件
    const stillWatching = activeTab.value === 'computer' || computerPanelHovered.value;
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
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  transition: margin-right 0.24s cubic-bezier(0.4, 0, 0.2, 1);
  /* 画笔截图覆盖层的定位基准（absolute inset:0 盖住页签条+内容区） */
  position: relative;
}
#rightbar.collapsed {
  margin-right: -330px;
}
/* 展开/收起都从右侧划入划出：面板是 flex item，靠负 margin 送出视口右侧，
   占位增减由同一条 margin 过渡带出，不动 width（拖拽调宽仍即时响应）。
   收起时面板内容由 visualTab/visualPicker 缓冲 PANEL_SLIDE_MS，滑走的是一直
   看到的那块画面，不会先变空白再平移。 */
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
/* 页签条：多页签并存 + “+”选择页 + 收起按钮；页签过多时横向滚动。
   高度钉死为 38px（+1px 下边框与 #chat-header 的 38px+1px 完全一致），
   两侧分隔线才在同一条水平线上——内容自适应高度会随页签/按钮字号浮动 */
.tabbar {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 38px;
  padding: 0 8px;
  border-bottom: 1px solid var(--border);
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
  max-width: 118px;
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
/* 浏览器工作台：表面占位区要撑满整个内容区（原生控件按此矩形叠加），
   需要去掉内边距并让页签容器成为竖向 flex 容器。用 id+类提升优先级，
   覆盖上面的 .rpane.active{display:block}，且只作用于 flush 时的激活页签 */
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
/* 多任务切换列表：状态点（橙=暂无输出）+ 命令 + 时长 */
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
/* 元信息心跳行与无输出解释提示 */
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
