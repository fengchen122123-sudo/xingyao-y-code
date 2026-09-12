<template>
  <div class="ap-wrap">
    <div class="ap-banner">
      ⏱ 自动化任务仅在星瑶运行期间生效（含最小化到托盘）；关闭应用期间不会触发，
      重新打开后按任务的「错过策略」处理。
      <button class="ap-btn primary ap-new" data-test="automation-new-btn" @click="openCreate">＋ 新建任务</button>
    </div>

    <!-- 任务列表 -->
    <div v-if="store.automationTasks.length === 0 && !form.show" class="ap-empty">
      还没有自动化任务。点击「新建任务」，把一段提示词绑定到定时器上，
      星瑶会到点自动向指定会话发起一次完整回合。
    </div>
    <div v-else class="ap-list">
      <div v-for="t in store.automationTasks" :key="t.id" class="ap-card" :data-test="`automation-card-${t.name}`">
        <div class="ap-card-top">
          <span class="ap-name">{{ t.name }}</span>
          <span class="ap-chip">{{ t.trigger_summary || t.trigger_type }}</span>
          <label class="ap-switch" title="启用/停用该任务" @click.stop>
            <input
              type="checkbox"
              :checked="t.enabled"
              :data-test="`automation-toggle-${t.name}`"
              @change="onToggle(t, ($event.target as HTMLInputElement).checked)"
            />
            <span class="ap-switch-track"></span>
          </label>
        </div>
        <div class="ap-meta">
          <span class="ap-meta-item" :title="`目标会话：${t.target_session}`">💬 {{ t.target_session }}</span>
          <span class="ap-meta-item">
            {{ t.enabled ? `下次：${formatNextRun(t.next_run_at)}` : '已停用' }}
          </span>
          <span v-if="t.last_run_at" class="ap-meta-item">上次：{{ t.last_run_at }}</span>
        </div>
        <div class="ap-prompt" :title="t.prompt">{{ t.prompt }}</div>
        <div class="ap-actions">
          <button class="ap-btn" title="立刻执行一次" :data-test="`automation-run-${t.name}`" @click="onRunNow(t)">立即执行</button>
          <button class="ap-btn" @click="openEdit(t)">编辑</button>
          <button class="ap-btn" @click="toggleHistory(t)">{{ expanded[t.id] ? '收起历史' : '运行历史' }}</button>
          <span class="ap-flex"></span>
          <button class="ap-btn danger" @click="onDelete(t)">删除</button>
        </div>

        <!-- 运行历史 -->
        <div v-if="expanded[t.id]" class="ap-history">
          <div class="ap-history-head">
            <span>最近运行（{{ (store.automationRuns[t.id] || []).length }} 条）</span>
            <button class="ap-btn" data-test="automation-clear-runs" @click="onClearRuns(t)">清空历史</button>
          </div>
          <div v-if="(store.automationRuns[t.id] || []).length === 0" class="ap-history-empty">暂无运行记录</div>
          <div v-for="r in store.automationRuns[t.id] || []" :key="r.id" class="ap-run" :title="r.error || ''">
            <span class="ap-run-time">{{ r.queued_at }}</span>
            <span class="ap-chip dim">{{ kindLabel(r.trigger_kind) }}</span>
            <span class="ap-chip" :class="statusClass(r.status)">{{ statusLabel(r.status) }}</span>
            <span class="ap-run-dur">{{ durationText(r) }}</span>
            <span v-if="r.error" class="ap-run-err">⚠ {{ r.error }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 新建 / 编辑表单 -->
    <div v-if="form.show" class="ap-form" data-test="automation-form">
      <div class="ap-form-title">{{ form.id ? '编辑任务' : '新建任务' }}</div>

      <div v-if="form.errors.length" class="ap-form-errors" data-test="automation-form-errors">
        <div v-for="e in form.errors" :key="e">⚠ {{ e }}</div>
      </div>

      <div class="ap-row">
        <input v-model="form.name" data-test="automation-name" placeholder="任务名称（如：每日站会摘要）" maxlength="50" />
        <select v-model="form.sessionMode" title="目标会话">
          <option value="" disabled>选择目标会话</option>
          <option v-for="s in sessionOptions" :key="s" :value="s">{{ s }}</option>
          <option value="__new__">新建会话…</option>
        </select>
        <input
          v-if="form.sessionMode === '__new__'"
          v-model="form.newSession"
          data-test="automation-new-session"
          placeholder="新会话名称"
          maxlength="100"
        />
      </div>

      <div class="ap-row">
        <select v-model="form.triggerType" data-test="automation-trigger-type" title="触发方式">
          <option value="interval">固定间隔</option>
          <option value="daily">每天定时</option>
          <option value="weekly">每周定时</option>
          <option value="startup">应用启动时</option>
        </select>
        <template v-if="form.triggerType === 'interval'">
          <input v-model.number="form.intervalMinutes" type="number" min="1" max="10080" style="width:110px" />
          <span class="ap-unit">分钟（1-10080）</span>
        </template>
        <template v-else-if="form.triggerType === 'daily'">
          <input v-model="form.time" type="time" data-test="automation-time" />
        </template>
        <template v-else-if="form.triggerType === 'weekly'">
          <label v-for="d in weekdayOptions" :key="d.value" class="ap-weekday">
            <input type="checkbox" :value="d.value" v-model="form.weekdays" /> {{ d.label }}
          </label>
          <input v-model="form.time" type="time" data-test="automation-time" />
        </template>
      </div>

      <textarea
        v-model="form.prompt"
        data-test="automation-prompt"
        rows="5"
        placeholder="提示词：到点后星瑶将把这段文字作为一次真实对话回合发送到目标会话"
      ></textarea>
      <div class="ap-char-count">{{ form.prompt.length }} / 6000</div>

      <div class="ap-row">
        <select v-model="form.permissionMode" data-test="automation-permission" title="无人值守权限档位">
          <option value="confirm">confirm —— 需要确认的操作自动拒绝</option>
          <option value="accept_edits">accept_edits —— 允许修改文件</option>
          <option value="full">full —— 允许修改文件与执行命令</option>
        </select>
        <select v-model="form.missedPolicy" title="应用未运行错过触发时">
          <option value="skip">错过则跳过</option>
          <option value="run_once">错过补跑一次</option>
        </select>
        <label class="ap-weekday"><input type="checkbox" v-model="form.notifyOnSuccess" /> 成功也通知</label>
      </div>
      <div v-if="form.permissionMode !== 'confirm'" class="ap-risk" data-test="automation-risk-note">
        ⚠ 无人值守档位：该任务的自动化回合可以不经确认{{ form.permissionMode === 'full' ? '修改文件并执行命令' : '修改文件' }}，
        每次触发都会在目标会话留下完整过程，可按回合撤销。
      </div>

      <div class="ap-row ap-form-actions">
        <button class="ap-btn" @click="form.show = false">取消</button>
        <button class="ap-btn primary" data-test="automation-save" :disabled="form.saving" @click="onSave">
          {{ form.saving ? '保存中…' : '保存' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { AutomationTaskInfo } from '../types/pywebview';

const store = useAppStore();

const weekdayOptions = [
  { value: 1, label: '周一' },
  { value: 2, label: '周二' },
  { value: 3, label: '周三' },
  { value: 4, label: '周四' },
  { value: 5, label: '周五' },
  { value: 6, label: '周六' },
  { value: 7, label: '周日' },
];

const expanded = reactive<Record<string, boolean>>({});

const form = reactive({
  show: false,
  saving: false,
  id: '',
  name: '',
  sessionMode: '',
  newSession: '',
  triggerType: 'daily' as AutomationTaskInfo['trigger_type'],
  intervalMinutes: 30,
  time: '09:30',
  weekdays: [1] as number[],
  prompt: '',
  permissionMode: 'confirm' as AutomationTaskInfo['permission_mode'],
  missedPolicy: 'skip' as 'skip' | 'run_once',
  notifyOnSuccess: false,
  errors: [] as string[],
});

const sessionOptions = computed(() => {
  const names: string[] = [];
  for (const group of store.sessionGroups || []) {
    for (const s of group.sessions || []) {
      if (s.name && !names.includes(s.name)) names.push(s.name);
    }
  }
  return names;
});

function openCreate() {
  form.id = '';
  form.name = '';
  form.sessionMode = sessionOptions.value[0] || '__new__';
  form.newSession = '';
  form.triggerType = 'daily';
  form.intervalMinutes = 30;
  form.time = '09:30';
  form.weekdays = [1];
  form.prompt = '';
  form.permissionMode = 'confirm';
  form.missedPolicy = 'skip';
  form.notifyOnSuccess = false;
  form.errors = [];
  form.show = true;
}

function openEdit(t: AutomationTaskInfo) {
  form.id = t.id;
  form.name = t.name;
  form.sessionMode = sessionOptions.value.includes(t.target_session) ? t.target_session : '__new__';
  form.newSession = sessionOptions.value.includes(t.target_session) ? '' : t.target_session;
  form.triggerType = t.trigger_type;
  const cfg = t.trigger_config || {};
  form.intervalMinutes = Number(cfg.interval_minutes) || 30;
  form.time = typeof cfg.time === 'string' ? cfg.time : '09:30';
  form.weekdays = Array.isArray(cfg.weekdays) ? cfg.weekdays.map(Number) : [1];
  form.prompt = t.prompt;
  form.permissionMode = t.permission_mode;
  form.missedPolicy = t.missed_run_policy === 'run_once' ? 'run_once' : 'skip';
  form.notifyOnSuccess = !!t.notify_on_success;
  form.errors = [];
  form.show = true;
}

async function onSave() {
  const targetSession = form.sessionMode === '__new__' ? form.newSession.trim() : form.sessionMode;
  if (!form.name.trim() || !form.prompt.trim() || !targetSession) {
    form.errors = ['任务名称、目标会话与提示词均不能为空'];
    return;
  }
  const config: Record<string, any> = {};
  if (form.triggerType === 'interval') config.interval_minutes = form.intervalMinutes;
  if (form.triggerType === 'daily' || form.triggerType === 'weekly') config.time = form.time;
  if (form.triggerType === 'weekly') config.weekdays = [...form.weekdays].sort((a, b) => a - b);
  form.saving = true;
  try {
    const res = await store.saveAutomationTask({
      id: form.id || undefined,
      name: form.name.trim(),
      trigger_type: form.triggerType,
      trigger_config: config,
      target_session: targetSession,
      prompt: form.prompt,
      permission_mode: form.permissionMode,
      missed_run_policy: form.missedPolicy,
      notify_on_success: form.notifyOnSuccess,
      enabled: editingEnabled(),
    });
    if (res.ok) {
      form.show = false;
    } else {
      form.errors = res.errors && res.errors.length ? res.errors : [res.msg || '保存失败'];
    }
  } finally {
    form.saving = false;
  }
}

function editingEnabled(): boolean {
  if (!form.id) return true;
  const existing = store.automationTasks.find(t => t.id === form.id);
  return existing ? !!existing.enabled : true;
}

function onToggle(t: AutomationTaskInfo, enabled: boolean) {
  void store.toggleAutomationTask(t.id, enabled);
}

async function onRunNow(t: AutomationTaskInfo) {
  const res = await store.runAutomationTaskNow(t.id);
  if (res.ok) expanded[t.id] = true;
}

async function onDelete(t: AutomationTaskInfo) {
  const ok = await store.askConfirm(
    '删除自动化任务',
    t.name,
    `将删除任务「${t.name}」及其全部运行历史，进行中的运行不受影响。`,
  );
  if (ok) void store.deleteAutomationTask(t.id);
}

function toggleHistory(t: AutomationTaskInfo) {
  expanded[t.id] = !expanded[t.id];
  if (expanded[t.id]) void store.loadAutomationRuns(t.id);
}

function onClearRuns(t: AutomationTaskInfo) {
  void store.clearAutomationRuns(t.id);
}

function formatNextRun(ts?: number | null): string {
  if (!ts) return '下次启动时';
  const date = new Date(ts * 1000);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString();
}

function kindLabel(kind: string): string {
  const map: Record<string, string> = {
    scheduled: '定时', startup: '启动', manual: '手动', catchup: '补跑',
  };
  return map[kind] || kind;
}

function statusLabel(status: string): string {
  const map: Record<string, string> = {
    queued: '排队中', running: '运行中', completed: '已完成', cancelled: '已取消',
    error: '失败', skipped: '已跳过', interrupted: '已中断',
  };
  return map[status] || status;
}

function statusClass(status: string): string {
  if (status === 'completed') return 'ok';
  if (status === 'error') return 'bad';
  if (status === 'running' || status === 'queued') return 'busy';
  return 'dim';
}

function durationText(r: { started_at?: string | null; finished_at?: string | null }): string {
  if (!r.started_at || !r.finished_at) return '—';
  const start = Date.parse(r.started_at.replace(' ', 'T'));
  const end = Date.parse(r.finished_at.replace(' ', 'T'));
  if (Number.isNaN(start) || Number.isNaN(end)) return '—';
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m${seconds % 60}s`;
}
</script>

<style scoped>
.ap-wrap {
  max-width: 960px;
  margin: 0 auto;
}
.ap-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  color: var(--text-dim);
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 14px;
  margin-bottom: 14px;
  line-height: 1.6;
}
.ap-flex { flex: 1; }
.ap-new { margin-left: auto; flex-shrink: 0; }
.ap-empty {
  color: var(--text-faint);
  font-size: 12.5px;
  text-align: center;
  padding: 30px 0;
  line-height: 2;
}
.ap-list { display: flex; flex-direction: column; gap: 12px; }
.ap-card {
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
}
.ap-card-top { display: flex; align-items: center; gap: 10px; }
.ap-name { font-size: 13.5px; font-weight: 600; }
.ap-chip {
  font-size: 10.5px;
  padding: 1px 8px;
  border-radius: 6px;
  background: var(--bg3);
  color: var(--text-dim);
  flex-shrink: 0;
}
.ap-chip.ok { background: var(--accent-soft); color: var(--accent); }
.ap-chip.bad { background: rgba(248, 81, 73, 0.12); color: var(--red); }
.ap-chip.busy { background: rgba(210, 153, 34, 0.14); color: var(--yellow); }
.ap-chip.dim { color: var(--text-faint); }
.ap-switch {
  position: relative; display: inline-block; width: 34px; height: 18px;
  margin-left: auto; cursor: pointer; flex-shrink: 0;
}
.ap-switch input { opacity: 0; width: 0; height: 0; position: absolute; }
.ap-switch-track {
  position: absolute; inset: 0; background: var(--bg3);
  border: 1px solid var(--border); border-radius: 99px; transition: 0.15s;
}
.ap-switch-track::before {
  content: ''; position: absolute; width: 12px; height: 12px; left: 2px; top: 2px;
  background: var(--text-faint); border-radius: 50%; transition: 0.15s;
}
.ap-switch input:checked + .ap-switch-track { background: var(--accent); border-color: transparent; }
.ap-switch input:checked + .ap-switch-track::before { transform: translateX(16px); background: #fff; }
.ap-meta {
  display: flex; flex-wrap: wrap; gap: 4px 14px;
  font-size: 11.5px; color: var(--text-dim); margin-top: 8px;
}
.ap-prompt {
  font-size: 12px; color: var(--text-faint); margin-top: 6px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  font-family: var(--mono);
}
.ap-actions { display: flex; gap: 6px; margin-top: 10px; align-items: center; }
.ap-history {
  margin-top: 10px; border-top: 1px dashed var(--border); padding-top: 10px;
}
.ap-history-head {
  display: flex; align-items: center; justify-content: space-between;
  font-size: 11.5px; color: var(--text-dim); margin-bottom: 8px;
}
.ap-history-empty { font-size: 11.5px; color: var(--text-faint); padding: 4px 0; }
.ap-run {
  display: flex; align-items: center; gap: 8px;
  font-size: 11.5px; padding: 4px 0; color: var(--text-dim);
  flex-wrap: wrap;
}
.ap-run-time { font-family: var(--mono); color: var(--text-faint); }
.ap-run-dur { color: var(--text-faint); }
.ap-run-err {
  color: var(--red); font-size: 11px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 420px;
}
.ap-form {
  margin-top: 14px;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
}
.ap-form-title { font-size: 13px; font-weight: 600; margin-bottom: 10px; }
.ap-form-errors {
  background: rgba(248, 81, 73, 0.1);
  border: 1px solid rgba(248, 81, 73, 0.35);
  color: var(--red);
  border-radius: 8px;
  font-size: 12px;
  padding: 8px 12px;
  margin-bottom: 10px;
  line-height: 1.7;
}
.ap-row { display: flex; gap: 8px; margin-bottom: 10px; align-items: center; flex-wrap: wrap; }
.ap-wrap input,
.ap-wrap select,
.ap-wrap textarea {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 7px;
  color: var(--text);
  padding: 7px 10px;
  font-size: 12.5px;
  outline: none;
  font-family: inherit;
}
.ap-row input:focus, .ap-row select:focus, textarea:focus { border-color: var(--accent); }
.ap-wrap textarea {
  width: 100%; box-sizing: border-box; resize: vertical; line-height: 1.6;
}
.ap-row input:first-child { flex: 1; min-width: 180px; }
.ap-row select { min-width: 150px; }
.ap-unit { font-size: 11.5px; color: var(--text-faint); }
.ap-weekday {
  font-size: 12px; color: var(--text-dim);
  display: inline-flex; align-items: center; gap: 4px; margin-right: 4px;
}
.ap-char-count {
  font-size: 10.5px; color: var(--text-faint); text-align: right; margin-top: -6px;
}
.ap-risk {
  font-size: 11.5px; color: var(--yellow);
  background: rgba(210, 153, 34, 0.1);
  border: 1px solid rgba(210, 153, 34, 0.3);
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 10px;
  line-height: 1.7;
}
.ap-form-actions { justify-content: flex-end; margin-bottom: 0; }
.ap-btn {
  background: var(--bg3);
  border: 1px solid var(--border);
  color: var(--text-dim);
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: 0.15s;
  display: inline-flex;
  align-items: center;
}
.ap-btn:hover { border-color: var(--text-faint); color: var(--text); }
.ap-btn.danger:hover { border-color: var(--red); color: var(--red); background: rgba(248, 81, 73, 0.1); }
.ap-btn.primary {
  background: var(--accent);
  border-color: transparent;
  color: #fff;
  font-weight: 600;
}
.ap-btn:disabled { opacity: 0.6; cursor: not-allowed; }
</style>
