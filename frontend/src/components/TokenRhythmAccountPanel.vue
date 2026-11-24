<template>
  <Teleport to="body">
  <div
    v-if="store.trAccountPanel.show"
    ref="panelEl"
    class="tr-acc"
    :style="panelStyle"
    data-test="tr-account-panel"
    role="dialog"
    aria-label="基元律动账号"
  >
    <div class="tr-acc-head">
      <span class="tr-acc-avatar" data-test="tr-acc-avatar">{{ avatarText }}</span>
      <span class="tr-acc-meta">
        <b class="tr-acc-name" data-test="tr-acc-name" :title="displayNameFull">{{ displayName }}</b>
        <span class="tr-acc-account" data-test="tr-acc-account">{{ accountText }}</span>
      </span>
      <!-- 官网入口：与「充值」同一套打开逻辑（右栏内嵌浏览器，不支持时回退系统浏览器） -->
      <button
        class="tr-acc-site"
        type="button"
        data-test="tr-acc-site"
        title="在右侧浏览器打开基元律动官网账户页"
        @click="store.openTrConsolePage(SITE_URL)"
      >官网 ↗</button>
    </div>

    <div v-if="loading" class="tr-acc-state" data-test="tr-acc-loading">正在加载…</div>
    <div v-else-if="error" class="tr-acc-state">
      <p class="tr-acc-error" data-test="tr-acc-error">{{ error }}</p>
      <div class="tr-acc-actions">
        <button
          v-if="isLoginExpiredError(error)"
          class="tr-acc-retry"
          type="button"
          data-test="tr-acc-relogin"
          @click="relogin()"
        >重新登录</button>
        <button class="tr-acc-retry" type="button" data-test="tr-acc-retry" @click="retry()">重试</button>
      </div>
    </div>

    <template v-else>
      <div class="tr-acc-balance">
        <span class="tr-acc-label">可用余额</span>
        <b class="tr-acc-amount" data-test="tr-acc-balance">{{ balanceText }}</b>
        <span class="tr-acc-today" data-test="tr-acc-today-cost">今日已用 {{ todayCostText }}</span>
      </div>

      <div class="tr-acc-temp">
        <span class="tr-acc-temp-label">临时额度</span>
        <b class="tr-acc-temp-amount" data-test="tr-acc-temporary">{{ temporaryText }}</b>
        <span
          v-if="temporaryExpireText"
          class="tr-acc-temp-expire"
          data-test="tr-acc-temporary-expire"
        >最近到期 {{ temporaryExpireText }}<template v-if="temporaryNearestText">{{ ` ${temporaryNearestText}` }}</template></span>
      </div>

      <div
        v-if="activeModelCredits.length"
        class="tr-acc-model-gifts"
        data-test="tr-acc-model-gifts"
      >
        <div class="tr-acc-model-gifts-head">
          <span class="tr-acc-model-gifts-label">模型专属赠金</span>
          <span
            class="tr-acc-model-gifts-total"
            data-test="tr-acc-model-gifts-total"
          >共 {{ modelCreditsTotalText }}</span>
        </div>
        <ul class="tr-acc-model-gifts-list">
          <li
            v-for="item in activeModelCredits"
            :key="item.key"
            class="tr-acc-model-gift-item"
            data-test="tr-acc-model-gift-item"
            :title="item.tip"
          >
            <span class="tr-acc-model-gift-name" data-test="tr-acc-model-gift-name">{{ item.name }}</span>
            <span
              v-if="item.expireShort"
              class="tr-acc-model-gift-expire"
              data-test="tr-acc-model-gift-expire"
            >{{ item.expireShort }}到期</span>
            <b class="tr-acc-model-gift-amount" data-test="tr-acc-model-gift-amount">{{ item.amountText }}</b>
          </li>
        </ul>
      </div>

      <div class="tr-acc-usage">
        <div class="tr-acc-cell">
          <b class="tr-acc-num" data-test="tr-acc-calls">{{ callsText }}</b>
          <span class="tr-acc-cell-label">调用次数</span>
        </div>
        <div class="tr-acc-cell" :title="tokenTip">
          <b class="tr-acc-num" data-test="tr-acc-tokens">{{ tokensText }}</b>
          <span class="tr-acc-cell-label">Token</span>
        </div>
        <div class="tr-acc-cell" :title="ioTip">
          <b class="tr-acc-num tr-acc-num-sm" data-test="tr-acc-io">
            <span>入 {{ inputText }}</span>
            <span>出 {{ outputText }}</span>
          </b>
          <span class="tr-acc-cell-label">输入 / 输出</span>
        </div>
      </div>
    </template>

    <div class="tr-acc-actions">
      <button class="tr-acc-btn primary" type="button" data-test="tr-acc-recharge" @click="store.openTrConsolePage(RECHARGE_URL)">充值</button>
      <button class="tr-acc-btn" type="button" data-test="tr-acc-usage-detail" @click="store.openTrDialog('usage')">用量明细</button>
      <button class="tr-acc-btn" type="button" data-test="tr-acc-profile" @click="store.openTrDialog('profile')">个人信息</button>
      <button
        class="tr-acc-btn"
        type="button"
        data-test="tr-acc-logout"
        :disabled="loggingOut"
        @click="logout()"
      >{{ loggingOut ? '正在退出…' : '退出登录' }}</button>
    </div>

    <p v-if="logoutError" class="tr-acc-error tr-acc-logout-error" data-test="tr-acc-logout-error">{{ logoutError }}</p>
    <p v-if="partial" class="tr-acc-partial" data-test="tr-acc-partial">部分数据获取失败</p>
    <p class="tr-acc-foot">
      <span>数据来自基元律动官方接口</span>
      <button
        type="button"
        class="tr-acc-refresh"
        data-test="tr-acc-refresh"
        :disabled="store.trOverview.loading"
        @click="store.refreshTrOverview()"
      >{{ store.trOverview.loading ? '刷新中…' : '刷新' }}</button>
    </p>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import { shortLabel } from '../utils/shortLabel';
import { isLoginExpiredError } from '../utils/loginExpired';

const store = useAppStore();

const PLACEHOLDER = '—';

const RECHARGE_URL = 'https://tokenrhythm.studio/account/recharge?entry_source=header_menu';

/** 官方账户页：点头部「官网」进控制台，与充值页同一套（右栏内嵌浏览器 / 不支持时回退系统浏览器） */
const SITE_URL = 'https://tokenrhythm.studio/account/account';

const panelEl = ref<HTMLElement | null>(null);

const PANEL_WIDTH = 300;
const PANEL_GAP = 8;
const panelStyle = ref<Record<string, string>>({ left: '0px', bottom: '0px', width: `${PANEL_WIDTH}px` });
const loggingOut = ref(false);
const logoutError = ref('');

const overview = computed<Record<string, any> | null>(() => {
  const data = store.trOverview.data;
  return data && typeof data === 'object' ? data : null;
});

const user = computed<Record<string, any>>(() => {
  const fromOverview = overview.value?.user;
  if (fromOverview && typeof fromOverview === 'object') return fromOverview as Record<string, any>;
  const cached = store.trAuth.user;
  return cached && typeof cached === 'object' ? cached : {};
});

const wallet = computed<Record<string, any>>(() => {
  const value = overview.value?.wallet;
  return value && typeof value === 'object' ? value : {};
});

const usage = computed<Record<string, any>>(() => {
  const value = overview.value?.usage_today;
  return value && typeof value === 'object' ? value : {};
});

const temporaryCredits = computed<Record<string, any>>(() => {
  const value = overview.value?.temporary_credits;
  return value && typeof value === 'object' ? value : {};
});

// 已有数据时刷新不退回"正在加载…"，避免每次打开都闪一下空白
const loading = computed(() => store.trOverview.loading && !overview.value);
const error = computed(() => store.trOverview.error);
const partial = computed(() => !!overview.value?.partial);

function amountText(value: unknown): string {
  if (value === null || value === undefined || value === '') return PLACEHOLDER;
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return num.toFixed(2);
}

function countText(value: unknown): string {
  if (value === null || value === undefined || value === '') return PLACEHOLDER;
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return thousands(Math.round(num));
}

function thousands(num: number): string {
  return String(num).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

const currency = computed(() => String(wallet.value.currency || 'CNY').toUpperCase());

/** 人民币用 ¥ 前缀；其它币种把代码跟在数字后面，避免把外币当成人民币 */
function withCurrency(money: string): string {
  if (money === PLACEHOLDER) return PLACEHOLDER;
  return currency.value === 'CNY' ? `¥${money}` : `${money} ${currency.value}`;
}

const balanceText = computed(() => withCurrency(amountText(wallet.value.available_balance_cny)));
const todayCostText = computed(() => withCurrency(amountText(usage.value.cost_cny)));
const callsText = computed(() => countText(usage.value.calls));
const tokensText = computed(() => countText(usage.value.total_tokens));
const inputText = computed(() => countText(usage.value.input_tokens));
const outputText = computed(() => countText(usage.value.output_tokens));

const tokenTip = computed(() => (tokensText.value === PLACEHOLDER
  ? '今日 Token 用量暂不可用'
  : `今日共 ${tokensText.value} tokens`));
const ioTip = computed(() => (
  inputText.value === PLACEHOLDER && outputText.value === PLACEHOLDER
    ? '今日输入 / 输出 Token 暂不可用'
    : `输入 ${inputText.value} tokens · 输出 ${outputText.value} tokens`
));

// 只看金额：服务端 summary 可能给出合计而列表被分页/过滤掉（count 为 0），
// 那种情况额度其实是有的，不能因为 count 为 0 就显示占位符。
const hasTemporary = computed(() => {
  const total = Number(temporaryCredits.value.total_cny);
  return Number.isFinite(total) && total > 0;
});

const temporaryText = computed(() => (
  hasTemporary.value ? withCurrency(amountText(temporaryCredits.value.total_cny)) : PLACEHOLDER
));

function formatExpireDate(iso: unknown): string {
  const raw = String(iso ?? '').trim();
  if (!raw) return '';
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return '';
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

const temporaryExpireText = computed(() => (
  hasTemporary.value ? formatExpireDate(temporaryCredits.value.nearest_expires_at) : ''
));

const temporaryNearestText = computed(() => {
  if (!hasTemporary.value || !temporaryExpireText.value) return '';
  const amount = amountText(temporaryCredits.value.nearest_amount_cny);
  if (amount === PLACEHOLDER || !(Number(amount) > 0)) return '';
  return withCurrency(amount);
});

function creditAmountText(num: number): string {
  if (!Number.isFinite(num) || num <= 0) return PLACEHOLDER;
  if (num >= 0.01) return withCurrency(num.toFixed(2));
  const small = num.toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
  if (small === '0' || small === '0.0000') {
    return currency.value === 'CNY' ? '<¥0.0001' : `<0.0001 ${currency.value}`;
  }
  return withCurrency(small);
}

function formatShortExpireDate(iso: unknown): string {
  const raw = String(iso ?? '').trim();
  if (!raw) return '';
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return '';
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  if (date.getFullYear() !== new Date().getFullYear()) {
    return `${date.getFullYear()}-${month}-${day}`;
  }
  return `${month}-${day}`;
}

interface ModelCreditRow {
  key: string;
  name: string;
  amountText: string;
  expireShort: string;
  tip: string;
  remainingUnits: number;
}

const activeModelCredits = computed<ModelCreditRow[]>(() => {
  const rawList = overview.value?.model_credits;
  if (!Array.isArray(rawList)) return [];
  const rows: ModelCreditRow[] = [];
  for (let i = 0; i < rawList.length; i += 1) {
    const item = rawList[i];
    if (!item || typeof item !== 'object') continue;
    const remaining = Number((item as Record<string, any>).remaining_cny);
    if (!Number.isFinite(remaining) || remaining <= 0) continue;
    const modelId = String((item as Record<string, any>).model_id || '').trim();
    const modelName = String((item as Record<string, any>).model_name || modelId || '模型专属赠金').trim();
    const grantedNum = Number((item as Record<string, any>).granted_cny);
    const expiresAt = (item as Record<string, any>).expires_at;
    const expireFull = formatExpireDate(expiresAt);
    const expireShort = formatShortExpireDate(expiresAt);
    const remainingDisplay = creditAmountText(remaining);
    const tipParts = [
      modelId && modelId !== modelName ? `${modelName} (${modelId})` : modelName,
      `剩余 ${remainingDisplay}`,
    ];
    if (Number.isFinite(grantedNum) && grantedNum > 0) {
      tipParts.push(`初始 ${creditAmountText(grantedNum)}`);
    }
    if (expireFull) {
      tipParts.push(`到期 ${expireFull}`);
    }
    const id = String((item as Record<string, any>).id || '').trim();
    rows.push({
      key: id || `${modelId || modelName}-${i}`,
      name: modelName,
      amountText: remainingDisplay,
      expireShort,
      tip: tipParts.join(' · '),
      remainingUnits: Math.round(remaining * 1e8),
    });
  }
  return rows;
});

const modelCreditsTotalText = computed(() => {
  const totalUnits = activeModelCredits.value.reduce((acc, item) => acc + item.remainingUnits, 0);
  return creditAmountText(totalUnits / 1e8);
});

function maskPhone(phone: string): string {
  const digits = phone.replace(/\s+/g, '');
  if (digits.length < 7) return digits;
  return `${digits.slice(0, 3)}****${digits.slice(-4)}`;
}

const rawPhone = computed(() => String(user.value.phone || '').trim());
const rawEmail = computed(() => String(user.value.email || '').trim());

const accountText = computed(() => {
  if (rawPhone.value) return maskPhone(rawPhone.value);
  if (rawEmail.value) return rawEmail.value;
  return PLACEHOLDER;
});

const displayNameFull = computed(() => {
  const named = String(user.value.name || user.value.nickname || '').trim();
  if (named) return named;
  if (rawPhone.value) return maskPhone(rawPhone.value);
  if (rawEmail.value) return rawEmail.value;
  return '基元律动账号';
});

const displayName = computed(() => shortLabel(displayNameFull.value));

const avatarText = computed(() => displayName.value.slice(0, 1).toUpperCase() || '律');

function retry() {
  void store.refreshTrOverview();
}

function relogin() {
  store.closeTrAccountPanel();
  store.openTrLoginModal();
  void store.refreshTrAuth();
}

async function logout() {
  if (loggingOut.value) return;
  loggingOut.value = true;
  logoutError.value = '';
  try {
    const res = await store.trLogout();
    if (res && res.ok) {
      // store.trLogout 内部已经清空快照、收起浮层并重新探测登录态，
      // 这里只做幂等兜底，保证浮层一定关掉
      store.closeTrAccountPanel();
      void store.refreshTrAuth();
    } else {
      logoutError.value = String((res && res.msg) || '退出账号失败，请稍后再试');
    }
  } finally {
    loggingOut.value = false;
  }
}

// ── 点击浮层外部关闭 ──────────────────────────────────────────────────
// 用 document 上的 mousedown（捕获阶段）而不是 click：点在别处时立刻收起，
// 不必等 click 走完，也不会被外部元素的 stopPropagation 拦掉。
// 卡片自身负责 toggle，算"内部"，否则点卡片会先被这里关掉、再被 toggle 打开。
const CARD_SELECTOR = '[data-test="side-user-card"]';

function updatePosition() {
  const card = document.querySelector(CARD_SELECTOR) as HTMLElement | null;
  if (!card) return;
  const rect = card.getBoundingClientRect();
  let left = rect.left;
  if (left + PANEL_WIDTH + PANEL_GAP > window.innerWidth) {
    left = window.innerWidth - PANEL_WIDTH - PANEL_GAP;
  }
  if (left < PANEL_GAP) left = PANEL_GAP;
  const bottom = window.innerHeight - rect.top + PANEL_GAP;
  panelStyle.value = {
    left: `${Math.round(left)}px`,
    bottom: `${Math.round(bottom)}px`,
    width: `${PANEL_WIDTH}px`,
  };
}

watch(
  () => store.trAccountPanel.show,
  async (open) => {
    if (!open) return;
    await nextTick();
    updatePosition();
  },
);

function onDocumentMousedown(event: MouseEvent) {
  if (!store.trAccountPanel.show) return;
  const target = event.target as Node | null;
  if (!target) return;
  if (panelEl.value && panelEl.value.contains(target)) return;
  const card = document.querySelector(CARD_SELECTOR);
  if (card && card.contains(target)) return;
  store.closeTrAccountPanel();
}

onMounted(() => {
  document.addEventListener('mousedown', onDocumentMousedown, true);
  window.addEventListener('resize', updatePosition);
  window.addEventListener('scroll', updatePosition, true);
});
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocumentMousedown, true);
  window.removeEventListener('resize', updatePosition);
  window.removeEventListener('scroll', updatePosition, true);
});
</script>

<style scoped>
.tr-acc {
  position: fixed;
  z-index: 1200;
  box-sizing: border-box;
  max-height: calc(100vh - 24px);
  overflow-y: auto;
  padding: 14px;
  background: #fff;
  color: #111;
  color-scheme: light;
  border: 1px solid #e6e6e6;
  border-radius: 12px;
  box-shadow: 0 14px 40px rgba(0, 0, 0, 0.2);
  font-size: 12.5px;
  line-height: 1.5;
  animation: tr-acc-pop 0.16s ease-out;
}
@keyframes tr-acc-pop {
  from {
    transform: translateY(4px);
    opacity: 0;
  }
  to {
    transform: none;
    opacity: 1;
  }
}
.tr-acc-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-bottom: 11px;
  border-bottom: 1px solid #f0f0f0;
}
.tr-acc-avatar {
  flex: 0 0 34px;
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: #111;
  color: #fff;
  font-size: 14px;
  font-weight: 600;
}
.tr-acc-meta {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.tr-acc-name {
  font-size: 13.5px;
  color: #111;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tr-acc-account {
  font-size: 11.5px;
  color: #8a8a8a;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tr-acc-site {
  flex: 0 0 auto;
  margin-left: auto;
  height: 22px;
  padding: 0 8px;
  border: 1px solid #e3e3e3;
  border-radius: 6px;
  background: #f7f7f7;
  color: #333;
  font-size: 11px;
  font-family: inherit;
  line-height: 1;
  white-space: nowrap;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.tr-acc-site:hover {
  background: #ededed;
  border-color: #d8d8d8;
}
.tr-acc-state {
  padding: 18px 0;
  text-align: center;
  color: #8a8a8a;
}
.tr-acc-error {
  margin: 0 0 8px;
  color: #c02626;
  font-size: 12px;
  line-height: 1.6;
  word-break: break-word;
}
.tr-acc-actions {
  display: flex;
  justify-content: center;
  gap: 8px;
}
.tr-acc-retry {
  height: 26px;
  padding: 0 12px;
  border: 1px solid #e3e3e3;
  border-radius: 7px;
  background: #f5f5f5;
  color: #333;
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
}
.tr-acc-retry:hover {
  background: #ededed;
}
.tr-acc-balance {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 11px 0 10px;
}
.tr-acc-label {
  font-size: 11.5px;
  color: #8a8a8a;
}
.tr-acc-amount {
  font-size: 23px;
  font-weight: 700;
  letter-spacing: -0.4px;
  color: #111;
  line-height: 1.25;
}
.tr-acc-today {
  font-size: 11.5px;
  color: #8a8a8a;
}
.tr-acc-temp {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 0 0 10px;
}
.tr-acc-temp-label {
  font-size: 11.5px;
  color: #8a8a8a;
}
.tr-acc-temp-amount {
  font-size: 13px;
  font-weight: 600;
  color: #111;
}
.tr-acc-temp-expire {
  margin-left: auto;
  font-size: 10.5px;
  color: #a8a8a8;
  white-space: nowrap;
}
.tr-acc-model-gifts {
  margin: 0 0 10px;
  padding: 8px 10px;
  border: 1px solid #f0f0f0;
  border-radius: 8px;
  background: #fafafa;
}
.tr-acc-model-gifts-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}
.tr-acc-model-gifts-label {
  font-size: 11px;
  color: #8a8a8a;
}
.tr-acc-model-gifts-total {
  font-size: 11.5px;
  font-weight: 600;
  color: #333;
  font-variant-numeric: tabular-nums;
}
.tr-acc-model-gifts-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 140px;
  overflow-y: auto;
}
.tr-acc-model-gift-item {
  display: flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  line-height: 1.4;
}
.tr-acc-model-gift-name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 11.5px;
  color: #333;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tr-acc-model-gift-expire {
  flex: 0 0 auto;
  font-size: 10px;
  color: #a8a8a8;
  white-space: nowrap;
}
.tr-acc-model-gift-amount {
  flex: 0 0 auto;
  font-size: 12px;
  font-weight: 600;
  color: #111;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.tr-acc-usage {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
}
.tr-acc-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  align-items: center;
  padding: 7px 2px;
  border: 1px solid #f0f0f0;
  border-radius: 8px;
  background: #fafafa;
  min-width: 0;
}
.tr-acc-num {
  font-size: 13px;
  font-weight: 600;
  color: #111;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}
.tr-acc-num-sm {
  display: flex;
  flex-direction: column;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.35;
  text-align: center;
}
.tr-acc-cell-label {
  font-size: 10.5px;
  color: #8a8a8a;
  white-space: nowrap;
}
.tr-acc-actions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
  margin-top: 12px;
}
.tr-acc-btn {
  width: 100%;
  height: 30px;
  padding: 0 8px;
  border: 1px solid #e3e3e3;
  border-radius: 8px;
  background: #f7f7f7;
  color: #333;
  font-size: 12px;
  font-family: inherit;
  cursor: pointer;
  transition: background 0.15s ease, opacity 0.15s ease;
}
.tr-acc-btn:hover:not(:disabled) {
  background: #ededed;
}
.tr-acc-btn.primary {
  background: #111;
  border-color: #111;
  color: #fff;
  font-weight: 600;
}
.tr-acc-btn.primary:hover:not(:disabled) {
  background: #000;
}
.tr-acc-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.tr-acc-logout-error {
  margin: 8px 0 0;
}
.tr-acc-refresh {
  border: 0;
  background: transparent;
  padding: 2px 4px;
  color: #111;
  font-size: 11.5px;
  text-decoration: underline;
  cursor: pointer;
}
.tr-acc-refresh:disabled {
  color: #9a9a9a;
  cursor: not-allowed;
  text-decoration: none;
}
.tr-acc-partial {
  margin: 8px 0 0;
  font-size: 11px;
  color: #9a6700;
}
.tr-acc-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 8px 0 0;
  font-size: 10.5px;
  color: #a8a8a8;
  text-align: center;
}
</style>
