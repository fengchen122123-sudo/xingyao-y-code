<template>
  <div id="rightbar" data-theme-region="right_panel" :class="{ collapsed: store.rightPanelCollapsed }" :style="panelStyle">
    <div id="right-tabs">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        class="rtab"
        :class="{ active: activeTab === tab.id }"
        @click="activeTab = tab.id"
      >
        {{ tab.label }}
        <span v-if="tab.id === 'plan' && store.planData.items.length > 0" class="badge-dot"></span>
      </button>
    </div>

    <div id="right-content">
      <!-- 计划面板 -->
      <div v-show="activeTab === 'plan'" class="rpane active">
        <div class="progress-line">
          <span>{{ store.planData.title || '任务计划' }}</span>
          <span style="color:var(--accent);font-weight:600">{{ store.planData.progress }}%</span>
        </div>
        <div class="progress-bar"><i :style="{ width: store.planData.progress + '%' }"></i></div>

        <div v-if="!store.planData.items || store.planData.items.length === 0" style="color:var(--text-faint);font-size:12px;padding:12px 0">
          暂无活动计划。当星瑶编排复杂大任务时，计划全景会自动在此呈现。
        </div>

        <div v-else class="plan-list">
          <div
            v-for="item in store.planData.items"
            :key="item.id"
            class="plan-item-row"
            :class="item.status"
          >
            <span class="p-status-ico">
              <AppIcon
                :name="item.status === 'done' ? 'check' : (item.status === 'in_progress' ? 'refresh' : 'circle')"
                :size="12"
                :stroke-width="2"
                :class="{ 'icon-spin': item.status === 'in_progress' }"
              />
            </span>
            <span class="p-text">{{ item.handoff_note || item.text }}</span>
          </div>
        </div>
      </div>

      <!-- 后台任务 -->
      <div v-show="activeTab === 'tasks'" class="rpane active">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)">
          <span style="font-size:12px;color:var(--text-dim);font-weight:600">后台运行任务</span>
          <button class="chip-btn" style="font-size:11.5px;padding:3px 9px" title="刷新后台任务列表" @click="refreshTasks"><AppIcon name="refresh" :size="12" /> 刷新</button>
        </div>

        <div v-if="(!store.backgroundTasks || store.backgroundTasks.length === 0) && (!store.subagentTasks || store.subagentTasks.length === 0)" style="color:var(--text-faint);font-size:12px;padding:12px 0">
          暂无后台活跃任务
        </div>

        <!-- 子代理任务（run_subagent 后台/前台任务） -->
        <div v-if="store.subagentTasks && store.subagentTasks.length" class="task-section">
          <div class="task-section-label">子代理任务</div>
          <div v-for="t in store.subagentTasks" :key="t.id" class="task-card">
            <div class="tc-top">
              <span class="tc-cmd">{{ t.description || t.id }}</span>
              <span class="tc-status">{{ t.status || '运行中' }}</span>
            </div>
            <div class="tc-foot">
              <span>{{ t.stage || '' }}{{ t.stage_detail ? ' · ' + t.stage_detail : '' }}</span>
              <span v-if="t.elapsed">{{ t.elapsed }}s</span>
            </div>
          </div>
        </div>

        <div v-if="store.backgroundTasks && store.backgroundTasks.length" class="task-section">
          <div class="task-section-label">后台运行任务</div>
          <div class="task-list">
            <div v-for="t in store.backgroundTasks" :key="t.task_id" class="task-card">
              <div class="tc-top">
                <span class="tc-cmd">{{ t.command || t.task_id }}</span>
                <span class="tc-status">{{ t.status || '运行中' }}</span>
              </div>
              <div class="tc-foot">
                <span style="font-size:11px;color:var(--text-faint)">{{ t.running_time || '活跃' }}</span>
                <button class="chip-btn danger" style="font-size:11px;padding:2px 7px" title="终止该后台任务" @click="killTask(t.task_id)">终止</button>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 内嵌终端 -->
      <div v-show="activeTab === 'term'" class="rpane active term-pane">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid var(--border)">
          <span style="font-size:12px;color:var(--text-dim);font-weight:600">终端 · {{ store.currentWorkdirLabel }}</span>
          <div style="display:flex;gap:6px">
            <button v-if="store.terminalRunning" class="chip-btn danger" style="font-size:11.5px;padding:3px 9px" title="终止正在运行的命令" @click="store.killTerminalCommand()"><AppIcon name="square" :size="11" :stroke-width="2" /> 终止</button>
            <button class="chip-btn" style="font-size:11.5px;padding:3px 9px" title="清空终端输出" @click="store.clearTerminal()"><AppIcon name="x" :size="11" :stroke-width="2" /> 清空</button>
          </div>
        </div>
        <div class="term-body" @click="focusTermInput">
          <div ref="termOutputEl" class="t-out"><template v-if="termLines.length === 0"><span class="t-cmd">$ # 终端已准备就绪，支持执行 Shell 命令</span></template><template v-for="(line, i) in termLines" :key="i">{{ i > 0 ? '\n' : '' }}<span :class="line.cls || undefined">{{ line.text }}</span></template></div>
          <div class="term-input-row">
            <span class="term-prompt">$</span>
            <input
              ref="termInputEl"
              v-model="termInput"
              class="term-input"
              :disabled="store.terminalRunning"
              :placeholder="store.terminalRunning ? '命令运行中…' : '输入命令，回车执行'"
              @keydown.enter="runTermCommand"
            />
          </div>
        </div>
      </div>

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

      <!-- 电脑操控面板（设计 16.2）：出现过 run 后动态追加的“操控”页签内容 -->
      <div v-show="activeTab === 'computer'" class="rpane active" data-test="computer-pane">
        <ComputerControlPane />
      </div>
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
import { renderMarkdown } from '../utils/markdown';
import { useIsMobile } from '../composables/useViewport';

type TabType = 'plan' | 'tasks' | 'term' | 'files' | 'computer';

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
const activeTab = ref<TabType>('plan');

// 既有四个页签静态定义；“操控”页签只在出现过电脑操控 run 后动态追加
// （设计 16.2），避免给从未使用操控的用户增加常驻页签
const baseTabs: Array<{ id: TabType; label: string }> = [
  { id: 'plan', label: '计划' },
  { id: 'tasks', label: '任务' },
  { id: 'term', label: '终端' },
  { id: 'files', label: '文件' }
];
const tabs = computed<Array<{ id: TabType; label: string }>>(() => (
  store.computerRun.run_id ? [...baseTabs, { id: 'computer', label: '操控' }] : baseTabs
));

// run 开始时首次自动切到操控页签（设计 16.2）：每个 run 只开一次（记录到
// computerLastAutoOpenedRunId），用户随后手动切走不抢回，新 run 再开一次；
// 挂载补跑覆盖“run 启动时右面板尚未挂载（如设置页打开中）”的场景
function autoOpenComputerTabOnce() {
  const runId = store.computerRun.run_id;
  if (!runId || runId === store.computerLastAutoOpenedRunId) return;
  if (!store.computerControlBarVisible) return;
  store.computerLastAutoOpenedRunId = runId;
  activeTab.value = 'computer';
}
watch(() => store.computerRun.run_id, autoOpenComputerTabOnce);

// ---------------- 内嵌终端 ----------------
const termInput = ref('');
const termOutputEl = ref<HTMLElement | null>(null);
const termInputEl = ref<HTMLInputElement | null>(null);

// 点击终端区域任意处即聚焦输入框（正在选中文字时不抢焦点）
function focusTermInput() {
  const sel = window.getSelection();
  if (sel && !sel.isCollapsed) return;
  termInputEl.value?.focus();
}

// 按行着色（对齐 demo 的 t-cmd / t-ok / t-warn 样式）：
// $ 开头的回显命令 => 白色；退出码 0 => 绿色；非零退出/启动失败 => 黄色
const termLines = computed(() => {
  const out = store.terminalOutput;
  if (!out) return [] as Array<{ text: string; cls: string }>;
  return out.split('\n').map((text) => {
    let cls = '';
    if (text.startsWith('$ ')) cls = 't-cmd';
    else if (text.startsWith('[进程已退出，代码 0]')) cls = 't-ok';
    else if (text.startsWith('[进程已退出') || text.startsWith('[启动失败') || text.startsWith('…[输出过长')) cls = 't-warn';
    return { text, cls };
  });
});

function runTermCommand() {
  const cmd = termInput.value.trim();
  if (!cmd || store.terminalRunning) return;
  termInput.value = '';
  store.runTerminalCommand(cmd);
}

// 有新输出时自动滚到底部
watch(() => store.terminalOutput, async () => {
  await nextTick();
  if (termOutputEl.value) {
    termOutputEl.value.scrollTop = termOutputEl.value.scrollHeight;
  }
});

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

async function refreshTasks() {
  if (window.pywebview && window.pywebview.api) {
    try {
      const list = await window.pywebview.api.get_background_tasks();
      store.backgroundTasks = list || [];
    } catch (e) {
      store.notifyModal('后台任务', '刷新后台任务失败: ' + e);
    }
  }
}

async function killTask(id: string) {
  if (window.pywebview && window.pywebview.api) {
    try {
      const res = await window.pywebview.api.kill_task(id);
      if (!res || !res.ok) {
        store.notifyModal('后台任务', res?.msg ? `终止任务失败: ${res.msg}` : '终止任务失败');
      } else {
        store.notifyModal('后台任务', res.msg ? `${res.msg}: ${id}` : '已请求终止后台任务: ' + id);
      }
    } catch (e) {
      store.notifyModal('后台任务', '终止任务失败: ' + e);
    }
    await refreshTasks();
  }
}

// 文件树自动刷新：切到“文件”页签或窗口重新聚焦时触发（节流 2 秒），
// 覆盖“用户在外部文件夹里新增/删除了文件再回来看”的场景；也可点 ↻ 手动刷新
let lastFileTreeAutoRefresh = 0;
function maybeAutoRefreshFileTree() {
  const now = Date.now();
  if (now - lastFileTreeAutoRefresh < 2000) return;
  lastFileTreeAutoRefresh = now;
  if (store.workdir) void store.refreshFileTree();
}
// 切到操控页签时校准 run 快照（设计 16.4）：_js_queue 满时允许丢新事件，
// 进入页签即用 get_computer_control_status() 复核终态
watch(activeTab, (t) => {
  if (t === 'files') maybeAutoRefreshFileTree();
  if (t === 'computer' && store.computerRun.run_id) void store.refreshComputerRun();
});
const onWindowFocus = () => { if (activeTab.value === 'files') maybeAutoRefreshFileTree(); };

onMounted(() => {
  refreshTasks();
  // 挂载补跑一次自动开页签（run 启动时右面板可能尚未挂载，见函数注释）
  autoOpenComputerTabOnce();
  window.addEventListener('focus', onWindowFocus);
});

onUnmounted(() => {
  window.removeEventListener('focus', onWindowFocus);
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
#right-tabs {
  display: flex;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.rtab {
  flex: 1;
  padding: 9px 0;
  text-align: center;
  font-size: 12.5px;
  color: var(--text-dim);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  background: none;
  border-top: none;
  border-left: none;
  border-right: none;
  transition: 0.15s;
  position: relative;
}
.rtab:hover {
  color: var(--text);
}
.rtab.active {
  color: var(--accent);
  border-bottom-color: var(--accent);
}
.badge-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  position: absolute;
  top: 5px;
  right: 7px;
}
.icon-spin {
  animation: rp-icon-spin 0.9s linear infinite;
}
@keyframes rp-icon-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
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

/* 内嵌终端：输出区占满剩余高度，输入行固定在底部 */
.rpane.active.term-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
}
/* 终端主体：输出区与输入行同处一个深色框内（对齐 demo 样式） */
.term-body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: 9px;
  padding: 12px;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.65;
  cursor: text;
}
.t-out {
  flex: 0 1 auto;
  min-height: 0;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-dim);
}
.t-out .t-cmd {
  color: var(--text-bright);
}
.t-out .t-ok {
  color: var(--green);
}
.t-out .t-warn {
  color: var(--yellow);
}
.term-input-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  color: var(--green);
  flex-shrink: 0;
}
.term-prompt {
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 700;
  flex-shrink: 0;
}
.term-input {
  flex: 1;
  min-width: 0;
  background: none;
  border: none;
  outline: none;
  color: var(--text-bright);
  font-family: var(--mono);
  font-size: 12px;
}
.term-input::placeholder {
  color: var(--text-faint);
}

.progress-line {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  margin-bottom: 8px;
  font-weight: 600;
}
.progress-bar {
  height: 6px;
  background: var(--bg3);
  border-radius: 99px;
  overflow: hidden;
  margin-bottom: 14px;
}
.progress-bar i {
  display: block;
  height: 100%;
  background: var(--accent);
  transition: width 0.3s ease;
}

.plan-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.plan-item-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--bg2);
  border: 1px solid var(--border);
  font-size: 12.5px;
  line-height: 1.5;
}
.plan-item-row.done {
  border-color: rgba(63, 185, 80, 0.3);
  background: rgba(63, 185, 80, 0.05);
}
.p-status-ico {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
}
.plan-item-row.done .p-status-ico {
  color: var(--green);
}
.plan-item-row.done .p-text {
  text-decoration: line-through;
  color: var(--text-faint);
}
.plan-item-row.in_progress {
  border-color: var(--accent);
  background: rgba(88, 166, 255, 0.08);
}
.plan-item-row.in_progress .p-status-ico {
  color: var(--accent);
}
.plan-item-row.in_progress .p-text {
  color: var(--text);
  font-weight: 600;
}
.plan-item-row.todo {
  color: var(--text-dim);
}

.task-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.task-section { margin-bottom: 12px; }
.task-section-label { padding: 4px 0 8px; color: var(--text-faint); font-size: 11px; font-weight: 600; }
.task-card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 9px 12px;
}
.tc-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}
.tc-cmd {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--text);
  font-weight: 600;
}
.tc-status {
  font-size: 11px;
  color: var(--green);
}
.tc-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
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
.chip-btn.danger {
  color: var(--red);
}
.chip-btn.danger:hover {
  background: rgba(248, 81, 73, 0.15);
  border-color: var(--red);
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
