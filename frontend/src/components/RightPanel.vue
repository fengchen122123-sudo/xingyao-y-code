<template>
  <div id="rightbar" :class="{ collapsed: store.rightPanelCollapsed }">
    <div id="right-tabs">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        class="rtab"
        :class="{ active: activeTab === tab.id }"
        @click="activeTab = tab.id"
      >
        {{ tab.label }}
        <span v-if="tab.id === 'plan' && store.planData.items.length > 0" class="badge-dot">●</span>
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
          📋 暂无活动计划。<br>当星瑶编排复杂大任务时，计划全景会自动在此呈现。
        </div>

        <div v-else class="plan-list">
          <div
            v-for="item in store.planData.items"
            :key="item.id"
            class="plan-item-row"
            :class="item.status"
          >
            <span class="p-status-ico">
              {{ item.status === 'done' ? '✓' : (item.status === 'in_progress' ? '⟳' : '○') }}
            </span>
            <span class="p-text">{{ item.text }}</span>
          </div>
        </div>
      </div>

      <!-- 后台任务 -->
      <div v-show="activeTab === 'tasks'" class="rpane active">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)">
          <span style="font-size:12px;color:var(--text-dim);font-weight:600">后台运行任务</span>
          <button class="chip-btn" style="font-size:11.5px;padding:3px 9px" title="刷新后台任务列表" @click="refreshTasks">↻ 刷新</button>
        </div>

        <div v-if="!store.backgroundTasks || store.backgroundTasks.length === 0" style="color:var(--text-faint);font-size:12px;padding:12px 0">
          ⏵ 暂无后台活跃任务
        </div>

        <div v-else class="task-list">
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

      <!-- 内嵌终端 -->
      <div v-show="activeTab === 'term'" class="rpane active term-pane">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid var(--border)">
          <span style="font-size:12px;color:var(--text-dim);font-weight:600">终端 · {{ store.currentWorkdirLabel }}</span>
          <div style="display:flex;gap:6px">
            <button v-if="store.terminalRunning" class="chip-btn danger" style="font-size:11.5px;padding:3px 9px" title="终止正在运行的命令" @click="store.killTerminalCommand()">⏹ 终止</button>
            <button class="chip-btn" style="font-size:11.5px;padding:3px 9px" title="清空终端输出" @click="store.clearTerminal()">✕ 清空</button>
          </div>
        </div>
        <pre ref="termOutputEl" class="term-output">{{ store.terminalOutput || '在下方输入命令，在当前工作区目录中执行。\n' }}</pre>
        <div class="term-input-row">
          <span class="term-prompt">$</span>
          <input
            v-model="termInput"
            class="term-input"
            :disabled="store.terminalRunning"
            :placeholder="store.terminalRunning ? '命令运行中…' : '输入命令，Enter 执行'"
            @keydown.enter="runTermCommand"
          />
        </div>
      </div>

      <!-- 文件树面板 -->
      <div v-show="activeTab === 'files'" class="rpane active">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)">
          <span style="font-size:12px;color:var(--text-dim);font-weight:600">项目文件</span>
          <button class="chip-btn" style="font-size:11.5px;padding:3px 9px" title="引入新的项目文件夹" @click="store.openWorkspaceFolder()">📂 引入新文件夹</button>
        </div>

        <div v-if="!store.fileTree || store.fileTree.length === 0" style="padding:20px 10px;text-align:center;color:var(--text-faint);font-size:12px">
          💬 当前为通用自由对话<br>暂未引入项目文件夹
        </div>

        <div v-else id="file-tree">
          <FileTreeNode v-for="node in store.fileTree" :key="node.n" :node="node" :parent-path="store.workdir" />
        </div>
      </div>
    </div>

    <!-- 文件预览弹窗 -->
    <div class="modal-mask" :class="{ show: store.filePreview.show }" @click.self="store.closeFilePreview()">
      <div class="modal file-preview-modal">
        <div class="modal-head">
          <div class="mh-ico">📄</div>
          <div class="fp-title">
            <h3 class="fp-name">{{ fileBaseName(store.filePreview.path) }}</h3>
            <p class="fp-path">{{ store.filePreview.path }}</p>
          </div>
          <button class="chip-btn" title="在系统中打开此文件" @click="store.openInSystem(store.filePreview.path)">🗂 在系统中打开</button>
          <button class="chip-btn" title="关闭预览" @click="store.closeFilePreview()">✕ 关闭</button>
        </div>
        <div class="fp-body">
          <div v-if="store.filePreview.loading" class="fp-loading">加载中…</div>
          <pre v-else class="fp-pre">{{ store.filePreview.content }}</pre>
          <div v-if="store.filePreview.truncated" class="fp-truncated">⚠ 文件内容过长，已截断显示</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, nextTick, onMounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import FileTreeNode from './FileTreeNode.vue';

type TabType = 'plan' | 'tasks' | 'term' | 'files';

const store = useAppStore();
const activeTab = ref<TabType>('plan');

const tabs: Array<{ id: TabType; label: string }> = [
  { id: 'plan', label: '📋 计划' },
  { id: 'tasks', label: '⏵ 任务' },
  { id: 'term', label: '⌨ 终端' },
  { id: 'files', label: '📁 文件' }
];

// ---------------- 内嵌终端 ----------------
const termInput = ref('');
const termOutputEl = ref<HTMLElement | null>(null);

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

async function refreshTasks() {
  if (window.pywebview && window.pywebview.api) {
    try {
      const list = await window.pywebview.api.get_background_tasks();
      store.backgroundTasks = list || [];
    } catch (e) {
      store.showToast('刷新后台任务失败: ' + e, 'warn');
    }
  }
}

async function killTask(id: string) {
  if (window.pywebview && window.pywebview.api) {
    try {
      const res = await window.pywebview.api.kill_task(id);
      if (!res || !res.ok) {
        store.showToast('终止任务失败', 'warn');
      } else {
        store.showToast('已请求终止后台任务: ' + id, 'warn');
      }
    } catch (e) {
      store.showToast('终止任务失败: ' + e, 'warn');
    }
    await refreshTasks();
  }
}

onMounted(() => {
  refreshTasks();
});
</script>

<style scoped>
#rightbar {
  width: 330px;
  background: var(--bg1);
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
  color: var(--accent);
  font-size: 8px;
  position: absolute;
  top: 4px;
  right: 6px;
}

#right-content {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
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
.term-output {
  flex: 1;
  min-height: 0;
  margin: 0 0 8px;
  padding: 10px 12px;
  background: #0a0e14;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-dim);
  white-space: pre-wrap;
  word-break: break-all;
  overflow-y: auto;
}
.term-input-row {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 10px;
  flex-shrink: 0;
}
.term-input-row:focus-within {
  border-color: var(--accent);
}
.term-prompt {
  font-family: var(--mono);
  font-size: 12.5px;
  color: var(--accent);
  font-weight: 700;
  flex-shrink: 0;
}
.term-input {
  flex: 1;
  min-width: 0;
  background: none;
  border: none;
  outline: none;
  color: var(--text);
  font-family: var(--mono);
  font-size: 12.5px;
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
  background: linear-gradient(90deg, var(--accent), var(--accent2));
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
  border-color: var(--accent);
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
  background: rgba(79, 140, 255, 0.15);
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
  background: #0a0e14;
}
.fp-loading {
  padding: 24px;
  text-align: center;
  color: var(--text-faint);
  font-size: 13px;
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
</style>
