<template>
  <!-- 账号中心原生弹窗：居中遮罩，直连官方接口拿真实数据，免去二次登录。
       三个入口（充值 / 用量明细 / 个人信息）共用这一个弹窗，按 store.trDialog.tab
       切视图。Teleport 到 body，独立于侧栏浮层，点击遮罩或关闭按钮关闭。 -->
  <Teleport to="body">
    <div
      v-if="store.trDialog.show"
      class="trd-mask"
      data-test="tr-dialog-mask"
      @click.self="store.closeTrDialog()"
    >
      <div
        class="trd"
        role="dialog"
        aria-modal="true"
        aria-label="基元律动账号中心"
        data-test="tr-dialog"
      >
        <!-- 标题栏：标签切换 + 关闭 -->
        <header class="trd-head">
          <nav class="trd-tabs">
            <button
              type="button"
              class="trd-tab"
              :class="{ active: store.trDialog.tab === 'recharge' }"
              data-test="trd-tab-recharge"
              @click="store.openTrDialog('recharge')"
            >充值</button>
            <button
              type="button"
              class="trd-tab"
              :class="{ active: store.trDialog.tab === 'usage' }"
              data-test="trd-tab-usage"
              @click="store.openTrDialog('usage')"
            >用量明细</button>
            <button
              type="button"
              class="trd-tab"
              :class="{ active: store.trDialog.tab === 'profile' }"
              data-test="trd-tab-profile"
              @click="store.openTrDialog('profile')"
            >个人信息</button>
          </nav>
          <button
            type="button"
            class="trd-close"
            aria-label="关闭"
            data-test="trd-close"
            @click="store.closeTrDialog()"
          >×</button>
        </header>

        <!-- 加载态 / 失败态（失败给后端原因 + 重试，不弹第二个窗） -->
        <div v-if="store.trDialog.loading" class="trd-state" data-test="trd-loading">正在加载…</div>
        <div v-else-if="store.trDialog.error" class="trd-state">
          <p class="trd-error" data-test="trd-error">{{ store.trDialog.error }}</p>
          <button type="button" class="trd-retry" data-test="trd-retry" @click="store.fetchTrDialog()">重试</button>
        </div>

        <!-- 充值视图：钱包流水（充值 / 扣费 / 退款），分页 -->
        <section v-else-if="store.trDialog.tab === 'recharge'" class="trd-body" data-test="trd-recharge">
          <p v-if="!transactions.length" class="trd-empty" data-test="trd-empty">暂无流水记录</p>
          <ul v-else class="trd-txn">
            <li v-for="(t, i) in transactions" :key="t.transactionId || i" class="trd-txn-item" data-test="trd-txn-item">
              <span class="trd-txn-desc">{{ t.description || t.type || '—' }}</span>
              <span class="trd-txn-amount" :class="amountClass(t)" data-test="trd-txn-amount">
                {{ directionSign(t) }}{{ money(t.amountCny) }}
              </span>
              <span class="trd-txn-time">{{ fmtTime(t.occurredAt) }}</span>
            </li>
          </ul>
          <footer v-if="transactions.length" class="trd-pager">
            <button
              type="button"
              class="trd-pg-btn"
              data-test="trd-prev"
              :disabled="store.trDialog.usagePage <= 1"
              @click="store.trDialogChangeUsagePage(store.trDialog.usagePage - 1)"
            >上一页</button>
            <span class="trd-pg-info" data-test="trd-page-info">第 {{ store.trDialog.usagePage }} 页</span>
            <button
              type="button"
              class="trd-pg-btn"
              data-test="trd-next"
              :disabled="!hasMoreTxn"
              @click="store.trDialogChangeUsagePage(store.trDialog.usagePage + 1)"
            >下一页</button>
          </footer>
        </section>

        <!-- 用量明细视图：时间窗切换 + summary 指标 + 调用日志分页 -->
        <section v-else-if="store.trDialog.tab === 'usage'" class="trd-body" data-test="trd-usage">
          <div class="trd-ranges">
            <button
              v-for="r in RANGES"
              :key="r.value"
              type="button"
              class="trd-range"
              :class="{ active: store.trDialog.usageRange === r.value }"
              :data-test="`trd-range-${r.value}`"
              @click="store.trDialogChangeUsageRange(r.value)"
            >{{ r.label }}</button>
          </div>

          <div v-if="summary" class="trd-summary">
            <div class="trd-cell">
              <b class="trd-num" data-test="trd-calls">{{ count(summary.calls) }}</b>
              <span class="trd-cell-label">调用次数</span>
            </div>
            <div class="trd-cell">
              <b class="trd-num" data-test="trd-total-tokens">{{ count(summary.totalTokens) }}</b>
              <span class="trd-cell-label">总 Token</span>
            </div>
            <div class="trd-cell">
              <b class="trd-num" data-test="trd-input-tokens">{{ count(summary.inputTokens) }}</b>
              <span class="trd-cell-label">输入 Token</span>
            </div>
            <div class="trd-cell">
              <b class="trd-num" data-test="trd-output-tokens">{{ count(summary.outputTokens) }}</b>
              <span class="trd-cell-label">输出 Token</span>
            </div>
            <div class="trd-cell">
              <b class="trd-num" data-test="trd-cost">{{ money(summary.costCny) }}</b>
              <span class="trd-cell-label">花费</span>
            </div>
          </div>
          <p v-else class="trd-empty">该时间窗暂无用量数据</p>

          <h4 class="trd-sub">调用日志</h4>
          <p v-if="!callLogs.length" class="trd-empty" data-test="trd-logs-empty">暂无调用记录</p>
          <ul v-else class="trd-logs">
            <li v-for="(log, i) in callLogs" :key="log.id || log.traceId || i" class="trd-log-item" data-test="trd-log-item">
              <span class="trd-log-model">{{ log.actualModelId || log.requestModelId || '—' }}</span>
              <span class="trd-log-status" :class="logClass(log)" data-test="trd-log-status">{{ statusText(log) }}</span>
              <span class="trd-log-tokens">Token {{ count(log.totalTokens) }}</span>
              <span class="trd-log-time">{{ log.time || fmtTime(log.requestAt) }}</span>
            </li>
          </ul>
          <footer v-if="callLogs.length" class="trd-pager">
            <button
              type="button"
              class="trd-pg-btn"
              data-test="trd-logs-prev"
              :disabled="store.trDialog.logsPage <= 1"
              @click="store.trDialogChangeLogsPage(store.trDialog.logsPage - 1)"
            >上一页</button>
            <span class="trd-pg-info" data-test="trd-logs-page-info">第 {{ store.trDialog.logsPage }} 页</span>
            <button
              type="button"
              class="trd-pg-btn"
              data-test="trd-logs-next"
              :disabled="!hasMoreLogs"
              @click="store.trDialogChangeLogsPage(store.trDialog.logsPage + 1)"
            >下一页</button>
          </footer>
        </section>

        <!-- 个人信息视图：账号基础资料 -->
        <section v-else class="trd-body" data-test="trd-profile">
          <div class="trd-profile">
            <span class="trd-avatar" data-test="trd-avatar">{{ avatarText }}</span>
            <div class="trd-profile-meta">
              <b class="trd-profile-name" data-test="trd-profile-name">{{ profileName }}</b>
              <span class="trd-profile-account" data-test="trd-profile-account">{{ maskedAccount }}</span>
            </div>
          </div>
          <dl class="trd-fields">
            <div class="trd-field" v-for="f in profileFields" :key="f.label">
              <dt class="trd-field-label">{{ f.label }}</dt>
              <dd class="trd-field-value" :data-test="f.test">{{ f.value || '—' }}</dd>
            </div>
          </dl>
        </section>

        <p class="trd-foot">
          <span>数据来自基元律动官方接口</span>
        </p>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

const PLACEHOLDER = '—';

/** 用量时间窗选项（与服务端路由一致） */
const RANGES = [
  { value: 'today', label: '今日' },
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
] as const;

const data = computed<Record<string, any>>(() => {
  const d = store.trDialog.data;
  return d && typeof d === 'object' ? d : {};
});

// ── 充值：钱包流水 ────────────────────────────────────────────────
const transactions = computed<Record<string, any>[]>(() => {
  const items = data.value.items;
  return Array.isArray(items) ? (items as Record<string, any>[]) : [];
});
const txnTotal = computed(() => Number(data.value.total) || transactions.value.length);
const hasMoreTxn = computed(() => transactions.value.length >= 20 || txnTotal.value > store.trDialog.usagePage * 20);

// ── 用量明细：summary + 调用日志 ───────────────────────────────────
const summary = computed<Record<string, any> | null>(() => {
  const s = data.value.summary;
  return s && typeof s === 'object' ? (s as Record<string, any>) : null;
});
const callLogs = computed<Record<string, any>[]>(() => {
  const list = data.value.list;
  return Array.isArray(list) ? (list as Record<string, any>[]) : [];
});
const hasMoreLogs = computed(() => callLogs.value.length >= 20);

// ── 个人信息 ─────────────────────────────────────────────────────
const profile = computed<Record<string, any>>(() => data.value as Record<string, any>);
const profileName = computed(() => {
  const named = String(profile.value.name || profile.value.nickname || '').trim();
  if (named) return named;
  const phone = String(profile.value.phone || '').trim();
  if (phone) return maskPhone(phone);
  const email = String(profile.value.email || '').trim();
  if (email) return email;
  return '基元律动账号';
});
const avatarText = computed(() => (profileName.value.slice(0, 1).toUpperCase() || '律'));
const maskedAccount = computed(() => {
  const phone = String(profile.value.phone || '').trim();
  if (phone) return maskPhone(phone);
  return String(profile.value.email || '').trim();
});
const profileFields = computed(() => [
  { label: '用户 ID', value: String(profile.value.id || ''), test: 'trd-field-id' },
  { label: '昵称', value: String(profile.value.nickname || profile.value.name || ''), test: 'trd-field-nickname' },
  { label: '手机号', value: String(profile.value.phone || ''), test: 'trd-field-phone' },
  { label: '邮箱', value: String(profile.value.email || ''), test: 'trd-field-email' },
  { label: '账号状态', value: statusLabel(profile.value.status), test: 'trd-field-status' },
  { label: '注册时间', value: fmtTime(profile.value.createdAt || profile.value.registeredAt), test: 'trd-field-created' },
]);

// ── 格式化辅助 ───────────────────────────────────────────────────
function maskPhone(phone: string): string {
  const digits = phone.replace(/\s+/g, '');
  if (digits.length < 7) return digits;
  return `${digits.slice(0, 3)}****${digits.slice(-4)}`;
}

function money(value: unknown): string {
  if (value === null || value === undefined || value === '') return PLACEHOLDER;
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return `¥${num.toFixed(2)}`;
}

function count(value: unknown): string {
  if (value === null || value === undefined || value === '') return PLACEHOLDER;
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return String(Math.round(num)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function fmtTime(iso: unknown): string {
  const raw = String(iso ?? '').trim();
  if (!raw) return '';
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())} ${p(date.getHours())}:${p(date.getMinutes())}`;
}

function directionSign(t: Record<string, any>): string {
  const dir = String(t.direction || '').toUpperCase();
  if (dir === 'CREDIT') return '+';
  if (dir === 'DEBIT') return '-';
  return '';
}

function amountClass(t: Record<string, any>): string {
  const dir = String(t.direction || '').toUpperCase();
  if (dir === 'CREDIT') return 'credit';
  if (dir === 'DEBIT') return 'debit';
  return '';
}

function statusText(log: Record<string, any>): string {
  const s = String(log.status || '').toUpperCase();
  if (s === 'ERROR') return '失败';
  if (s === 'ABORTED') return '已中止';
  if (s) return '成功';
  return '—';
}

function logClass(log: Record<string, any>): string {
  const s = String(log.status || '').toUpperCase();
  if (s === 'ERROR') return 'fail';
  if (s === 'ABORTED') return 'abort';
  return 'ok';
}

function statusLabel(status: unknown): string {
  const s = String(status || '').toLowerCase();
  if (s.includes('active') || s === 'normal' || s === 'ok') return '正常';
  if (s.includes('ban') || s.includes('block')) return '受限';
  if (s) return String(status);
  return '';
}
</script>

<style scoped>
.trd-mask {
  position: fixed;
  inset: 0;
  z-index: 1400;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.45);
  animation: trd-mask-in 0.14s ease-out;
}
@keyframes trd-mask-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
.trd {
  box-sizing: border-box;
  width: 520px;
  max-width: calc(100vw - 32px);
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  background: #fff;
  color: #111;
  color-scheme: light;
  border-radius: 14px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.28);
  font-size: 13px;
  animation: trd-pop 0.16s ease-out;
}
@keyframes trd-pop {
  from { transform: translateY(8px) scale(0.98); opacity: 0; }
  to { transform: none; opacity: 1; }
}
.trd-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 14px;
  border-bottom: 1px solid #f0f0f0;
}
.trd-tabs {
  display: flex;
  gap: 4px;
  flex: 1;
}
.trd-tab {
  height: 30px;
  padding: 0 14px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: #555;
  font-size: 13px;
  font-family: inherit;
  cursor: pointer;
}
.trd-tab:hover { background: #f5f5f5; }
.trd-tab.active {
  background: #111;
  color: #fff;
  font-weight: 600;
}
.trd-close {
  width: 30px;
  height: 30px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: #999;
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
}
.trd-close:hover { background: #f0f0f0; color: #333; }
.trd-body {
  padding: 14px;
  overflow-y: auto;
}
.trd-state {
  padding: 32px 14px;
  text-align: center;
  color: #8a8a8a;
}
.trd-error {
  margin: 0 0 10px;
  color: #c02626;
  font-size: 12.5px;
  line-height: 1.6;
  word-break: break-word;
}
.trd-retry {
  height: 28px;
  padding: 0 16px;
  border: 1px solid #e3e3e3;
  border-radius: 7px;
  background: #f5f5f5;
  color: #333;
  font-size: 12.5px;
  font-family: inherit;
  cursor: pointer;
}
.trd-retry:hover { background: #ededed; }
.trd-empty {
  margin: 8px 0;
  color: #8a8a8a;
  text-align: center;
  font-size: 12.5px;
}
/* 充值流水 */
.trd-txn { list-style: none; margin: 0; padding: 0; }
.trd-txn-item {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center;
  gap: 10px;
  padding: 9px 4px;
  border-bottom: 1px solid #f3f3f3;
}
.trd-txn-desc {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #222;
}
.trd-txn-amount {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.trd-txn-amount.credit { color: #1a7f37; }
.trd-txn-amount.debit { color: #c02626; }
.trd-txn-time {
  color: #a8a8a8;
  font-size: 11.5px;
  white-space: nowrap;
}
/* 用量：时间窗切换 */
.trd-ranges {
  display: flex;
  gap: 6px;
  margin-bottom: 12px;
}
.trd-range {
  height: 28px;
  padding: 0 14px;
  border: 1px solid #e3e3e3;
  border-radius: 8px;
  background: #f7f7f7;
  color: #333;
  font-size: 12.5px;
  font-family: inherit;
  cursor: pointer;
}
.trd-range.active { background: #111; color: #fff; border-color: #111; }
.trd-summary {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 6px;
  margin-bottom: 14px;
}
.trd-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 8px 2px;
  border: 1px solid #f0f0f0;
  border-radius: 8px;
  background: #fafafa;
}
.trd-num {
  font-size: 14px;
  font-weight: 600;
  color: #111;
}
.trd-cell-label {
  font-size: 10.5px;
  color: #8a8a8a;
  white-space: nowrap;
}
.trd-sub {
  margin: 4px 0 8px;
  font-size: 12.5px;
  color: #555;
  font-weight: 600;
}
.trd-logs { list-style: none; margin: 0; padding: 0; }
.trd-log-item {
  display: grid;
  grid-template-columns: 1fr auto auto auto;
  align-items: center;
  gap: 10px;
  padding: 7px 4px;
  border-bottom: 1px solid #f3f3f3;
  font-size: 12px;
}
.trd-log-model {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #222;
}
.trd-log-status { font-weight: 600; white-space: nowrap; }
.trd-log-status.ok { color: #1a7f37; }
.trd-log-status.fail { color: #c02626; }
.trd-log-status.abort { color: #9a6700; }
.trd-log-tokens { color: #666; white-space: nowrap; }
.trd-log-time { color: #a8a8a8; font-size: 11.5px; white-space: nowrap; }
/* 个人信息 */
.trd-profile {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid #f0f0f0;
}
.trd-avatar {
  flex: 0 0 42px;
  width: 42px;
  height: 42px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: #111;
  color: #fff;
  font-size: 18px;
  font-weight: 600;
}
.trd-profile-meta { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.trd-profile-name { font-size: 15px; color: #111; }
.trd-profile-account { font-size: 12px; color: #8a8a8a; }
.trd-fields {
  margin: 12px 0 0;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 16px;
}
.trd-field { display: flex; flex-direction: column; gap: 2px; }
.trd-field-label { font-size: 11.5px; color: #8a8a8a; }
.trd-field-value {
  margin: 0;
  font-size: 13px;
  color: #111;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 分页 */
.trd-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 12px;
}
.trd-pg-btn {
  height: 28px;
  padding: 0 14px;
  border: 1px solid #e3e3e3;
  border-radius: 7px;
  background: #f7f7f7;
  color: #333;
  font-size: 12.5px;
  font-family: inherit;
  cursor: pointer;
}
.trd-pg-btn:hover:not(:disabled) { background: #ededed; }
.trd-pg-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.trd-pg-info { font-size: 12px; color: #666; }
.trd-foot {
  margin: 0;
  padding: 10px 14px;
  border-top: 1px solid #f0f0f0;
  text-align: center;
  font-size: 10.5px;
  color: #a8a8a8;
}
</style>
