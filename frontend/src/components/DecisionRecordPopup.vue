<template>
  <!-- 会话头部「决策」入口的悬浮面板：展示 JEV 决策记录（只读轻量视图），
       锚定在按钮下方；点外部/Esc 由 App.vue 统一收起 -->
  <div class="decision-pop" data-test="decision-dialog" @click.stop>
    <div class="pop-head">
      <div class="ph-ico"><AppIcon name="brain" :size="14" :stroke-width="2" /></div>
      <div class="ph-title">
        <h3>决策记录</h3>
        <p class="ph-sub">JEV 决策模型调用记录（仅记录观测，不改变会话行为）</p>
      </div>
      <button class="chip-btn" title="关闭" @click="$emit('close')"><AppIcon name="x" :size="12" :stroke-width="2" /> 关闭</button>
    </div>

    <div class="pop-body">
      <div class="status-line" data-test="decision-pop-status">
        <span class="dot" :class="{ ok: enabled }"></span>
        <span>{{ statusText }}</span>
      </div>

      <template v-if="summary && summary.today_count > 0">
        <div class="stats-line" data-test="decision-pop-stats">
          今日 {{ summary.today_count }} 次决策 · 影子 {{ summary.today_shadow_count }} · 采纳 {{ summary.today_adopted_count }} · 回退 {{ summary.today_fallback_count }}
        </div>
        <ul class="record-list" data-test="decision-pop-list">
          <li v-for="(item, index) in summary.recent" :key="index" :class="{ shadow: item.shadow }">
            {{ line(item) }}
          </li>
        </ul>
      </template>
      <div v-else class="pop-empty" data-test="decision-pop-empty">
        还没有决策记录。启用后在设置里跑一次探针就会产生记录；业务场景接入后每次决策都会出现在这里。
      </div>
    </div>

    <div class="pop-foot">
      <button class="chip-btn" data-test="decision-pop-open-settings" @click="openSettings">去设置</button>
      <button class="chip-btn" data-test="decision-pop-open-audit" @click="openAuditDir">打开审计目录</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';

import { useAppStore } from '../stores/appStore';
import type { DecisionAuditRecentItem, DecisionAuditSummary } from '../types/pywebview';
import AppIcon from './AppIcon.vue';

// 头部「决策」入口的悬浮面板：挂载时拉一次审计汇总与启用状态；
// 任何失败都静默（面板只做观测展示，绝不阻塞或报错）。
const emit = defineEmits<{ close: [] }>();

const store = useAppStore();
const summary = ref<DecisionAuditSummary | null>(null);
const enabled = ref(false);
const modelId = ref('');

const statusText = computed(() => {
  if (!enabled.value) return '决策模型未启用（可在设置 → 决策模型（JEV）中开启）';
  return `已启用 · 模型 ${modelId.value || 'NeoHorse-Jev-4B'}`;
});

/** 单条记录展示行：时间 · 场景 · 模式 · 延迟 · 回退原因 */
function line(item: DecisionAuditRecentItem): string {
  const time = item.ts.length >= 19 ? item.ts.slice(11, 19) : item.ts;
  const parts = [time, item.scene || '未知场景', item.shadow ? '影子' : '正式'];
  if (typeof item.latency_ms === 'number') parts.push(`${item.latency_ms}ms`);
  if (item.fallback_reason) parts.push(`回退：${item.fallback_reason}`);
  return parts.join(' · ');
}

async function loadSummary() {
  try {
    const api = window.pywebview?.api;
    if (!api?.get_decision_audit_summary) return;
    const res = await api.get_decision_audit_summary(10);
    if (res?.ok) {
      summary.value = {
        ok: true,
        today_count: Number(res.today_count ?? 0),
        today_shadow_count: Number(res.today_shadow_count ?? 0),
        today_adopted_count: Number(res.today_adopted_count ?? 0),
        today_fallback_count: Number(res.today_fallback_count ?? 0),
        recent: Array.isArray(res.recent) ? res.recent : [],
      };
    }
  } catch {
    // 静默：记录面板失败不影响头部其余功能
  }
}

async function loadSettings() {
  try {
    const api = window.pywebview?.api;
    if (!api?.get_decision_settings) return;
    const res = await api.get_decision_settings();
    if (res?.ok && res.settings) {
      enabled.value = res.settings.enabled === true;
      modelId.value = String(res.settings.model_id ?? '');
    }
  } catch {
    // 静默
  }
}

async function openAuditDir() {
  try {
    const api = window.pywebview?.api;
    if (!api?.open_decision_audit_dir) return;
    await api.open_decision_audit_dir();
  } catch {
    // 静默
  }
}

function openSettings() {
  store.settingsSection = 'decision';
  store.settingsOpen = true;
  emit('close');
}

onMounted(() => {
  loadSummary();
  loadSettings();
  // 打开面板即视为已读：清零头部徽章
  store.decisionUnread = 0;
});

// 面板开启期间有新决策落盘（decision_recorded 事件）：立即刷新记录并保持已读
watch(
  () => store.decisionEventTick,
  () => {
    loadSummary();
    store.decisionUnread = 0;
  },
);
</script>

<style scoped>
.decision-pop {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: 460px;
  max-width: min(460px, calc(100vw - 32px));
  background: var(--float-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(14px);
  z-index: 210;
  display: flex;
  flex-direction: column;
  max-height: min(560px, 62vh);
  overflow: hidden;
  text-align: left;
}
.pop-head { display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-bottom: 1px solid var(--border); }
.ph-ico { width: 26px; height: 26px; display: grid; place-items: center; border-radius: 8px; background: var(--bg3); color: var(--accent); flex: none; }
.ph-title { flex: 1; min-width: 0; }
.ph-title h3 { margin: 0; font-size: 14px; }
.ph-sub { margin: 2px 0 0; color: var(--text-faint); font-size: 11px; }
.pop-body { padding: 12px 14px; overflow: auto; display: grid; gap: 10px; }
.status-line { display: flex; align-items: center; gap: 8px; color: var(--text-dim); font-size: 12px; }
.status-line .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-faint); flex: none; }
.status-line .dot.ok { background: var(--green); }
.stats-line { color: var(--text-dim); font-size: 12px; }
.record-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 5px; font-size: 12px; color: var(--text-dim); }
.record-list li { padding: 5px 8px; border-radius: 7px; background: var(--bg2); }
.record-list li.shadow { opacity: .75; }
.pop-empty { color: var(--text-faint); font-size: 12px; line-height: 1.7; padding: 6px 0; }
.pop-foot { display: flex; gap: 8px; padding: 10px 14px; border-top: 1px solid var(--border); }
</style>
