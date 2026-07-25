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
          <button class="chip-btn" style="font-size:11.5px;padding:3px 9px" @click="refreshTasks">↻ 刷新</button>
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
              <button class="chip-btn danger" style="font-size:11px;padding:2px 7px" @click="killTask(t.task_id)">终止</button>
            </div>
          </div>
        </div>
      </div>

      <!-- 终端面板 -->
      <div v-show="activeTab === 'terminal'" class="rpane active">
        <div class="term-body">
          <div class="t-out" v-html="termOutputs"></div>
          <div class="term-input-row">
            $&nbsp;<input 
              v-model="termInput" 
              placeholder="输入命令，回车执行" 
              @keydown.enter="runTermCmd"
            />
          </div>
        </div>
      </div>

      <!-- 文件树面板 -->
      <div v-show="activeTab === 'files'" class="rpane active">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)">
          <span style="font-size:12px;color:var(--text-dim);font-weight:600">项目文件</span>
          <button class="chip-btn" style="font-size:11.5px;padding:3px 9px" @click="store.openWorkspaceFolder()">📂 引入新文件夹</button>
        </div>

        <div v-if="!store.fileTree || store.fileTree.length === 0" style="padding:20px 10px;text-align:center;color:var(--text-faint);font-size:12px">
          💬 当前为通用自由对话<br>暂未引入项目文件夹
        </div>

        <div v-else id="file-tree">
          <FileTreeNode v-for="node in store.fileTree" :key="node.n" :node="node" />
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import FileTreeNode from './FileTreeNode.vue';

type TabType = 'plan' | 'tasks' | 'terminal' | 'files';

const store = useAppStore();
const activeTab = ref<TabType>('plan');

const tabs: Array<{ id: TabType; label: string }> = [
  { id: 'plan', label: '📋 计划' },
  { id: 'tasks', label: '⏵ 任务' },
  { id: 'terminal', label: '🖥 终端' },
  { id: 'files', label: '📁 文件' }
];

const termInput = ref('');
const termOutputs = ref('<span class="t-cmd">$ # 终端已准备就绪，支持执行 Shell 命令</span>\n');

async function refreshTasks() {
  if (window.pywebview && window.pywebview.api) {
    try {
      const list = await window.pywebview.api.get_background_tasks();
      store.backgroundTasks = list || [];
    } catch (e) {
      console.error(e);
    }
  }
}

async function killTask(id: string) {
  if (window.pywebview && window.pywebview.api) {
    await window.pywebview.api.kill_task(id);
    store.showToast('已请求终止后台任务: ' + id, 'warn');
    await refreshTasks();
  }
}

async function runTermCmd() {
  if (!termInput.value.trim()) return;
  const cmd = termInput.value.trim();
  termInput.value = '';

  if (cmd === 'clear') {
    termOutputs.value = '';
    return;
  }

  termOutputs.value += `<span class="t-cmd">$ ${cmd.replace(/</g, '&lt;')}</span>\n`;

  if (window.pywebview && window.pywebview.api) {
    try {
      const res = await window.pywebview.api.execute_terminal_cmd(cmd);
      if (res.stdout) termOutputs.value += `${res.stdout.replace(/</g, '&lt;')}\n`;
      if (res.stderr) termOutputs.value += `<span class="t-warn">${res.stderr.replace(/</g, '&lt;')}</span>\n`;
    } catch (err) {
      termOutputs.value += `<span class="t-warn">执行出错: ${err}</span>\n`;
    }
  } else {
    termOutputs.value += '无后端连接，无法执行 Shell 命令\n';
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

.term-body {
  background: #0a0e14;
  border: 1px solid var(--border);
  border-radius: 9px;
  padding: 12px;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.65;
  min-height: 320px;
}
.t-out {
  white-space: pre-wrap;
  color: var(--text-dim);
}
.term-input-row {
  display: flex;
  gap: 6px;
  margin-top: 4px;
  color: var(--green);
}
.term-input-row input {
  flex: 1;
  background: none;
  border: none;
  outline: none;
  color: #fff;
  font-family: var(--mono);
  font-size: 12px;
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
</style>
