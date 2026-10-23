<template>
  <!-- 账号浮层（不是全屏遮罩弹窗）：相对 .side-bottom 定位，浮在左下角用户卡片正上方。
       打开/关闭由 store.trAccountPanel.show 驱动，外部点击与卡片点击的关闭/切换也走 store。 -->
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
    <!-- 顶部：圆形头像 + 昵称 + 账号 + 右侧官网入口 -->
    <div class="tr-acc-head">
      <span class="tr-acc-avatar" data-test="tr-acc-avatar">{{ avatarText }}</span>
      <span class="tr-acc-meta">
        <b class="tr-acc-name" data-test="tr-acc-name">{{ displayName }}</b>
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

    <!-- 加载态 / 失败态（失败给后端原因 + 重试，不弹第二次窗） -->
    <div v-if="loading" class="tr-acc-state" data-test="tr-acc-loading">正在加载…</div>
    <div v-else-if="error" class="tr-acc-state">
      <p class="tr-acc-error" data-test="tr-acc-error">{{ error }}</p>
      <button class="tr-acc-retry" type="button" data-test="tr-acc-retry" @click="retry()">重试</button>
    </div>

    <!-- 数据区：余额 / 今日用量 -->
    <template v-else>
      <div class="tr-acc-balance">
        <span class="tr-acc-label">可用余额</span>
        <b class="tr-acc-amount" data-test="tr-acc-balance">{{ balanceText }}</b>
        <span class="tr-acc-today" data-test="tr-acc-today-cost">今日已用 {{ todayCostText }}</span>
      </div>

      <!-- 临时额度：无额度 / 数据缺失时不隐藏整行，固定占位避免布局跳动 -->
      <div class="tr-acc-temp">
        <span class="tr-acc-temp-label">临时额度</span>
        <b class="tr-acc-temp-amount" data-test="tr-acc-temporary">{{ temporaryText }}</b>
        <span
          v-if="temporaryExpireText"
          class="tr-acc-temp-expire"
          data-test="tr-acc-temporary-expire"
        >最近到期 {{ temporaryExpireText }}<template v-if="temporaryNearestText">{{ ` ${temporaryNearestText}` }}</template></span>
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

    <!-- 操作区：充值 / 用量明细 / 个人信息 / 退出登录（两行两列，样式统一） -->
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
    <!-- 部分接口失败只做底部一行提示，不加弹窗打断 -->
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

const store = useAppStore();

/** 数据缺失时的占位符：任何情况下都不把 undefined / NaN 甩到界面上 */
const PLACEHOLDER = '—';

/** 官方充值入口：点「充值」直接在右栏内嵌浏览器打开充值页，不再走原生弹窗 */
const RECHARGE_URL = 'https://tokenrhythm.studio/account/recharge?entry_source=header_menu';

/** 官方账户页：点头部「官网」进控制台，与充值页同一套（右栏内嵌浏览器 / 不支持时回退系统浏览器） */
const SITE_URL = 'https://tokenrhythm.studio/account/account';

const panelEl = ref<HTMLElement | null>(null);

/** 浮层宽度（px）：三列用量 + 两列按钮在 268px 下会挤，固定 300 更稳 */
const PANEL_WIDTH = 300;
/** 浮层与卡片、视口边缘之间的间距 */
const PANEL_GAP = 8;
/** fixed 定位：浮层 Teleport 到 body，不受侧栏宽度/overflow 裁剪 */
const panelStyle = ref<Record<string, string>>({ left: '0px', bottom: '0px', width: `${PANEL_WIDTH}px` });
const loggingOut = ref(false);
const logoutError = ref('');

const overview = computed<Record<string, any> | null>(() => {
  const data = store.trOverview.data;
  return data && typeof data === 'object' ? data : null;
});

/** 账号信息：优先用总览里的 user；总览未回来时回落到登录态缓存，头部不至于空白 */
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

/** 临时额度汇总：{ total_cny, count, nearest_expires_at }，缺失时当空对象处理 */
const temporaryCredits = computed<Record<string, any>>(() => {
  const value = overview.value?.temporary_credits;
  return value && typeof value === 'object' ? value : {};
});

// 已有数据时刷新不退回"正在加载…"，避免每次打开都闪一下空白
const loading = computed(() => store.trOverview.loading && !overview.value);
const error = computed(() => store.trOverview.error);
const partial = computed(() => !!overview.value?.partial);

/** 金额字符串 → 两位小数；解析不出数字就原样展示，空值给占位符 */
function amountText(value: unknown): string {
  if (value === null || value === undefined || value === '') return PLACEHOLDER;
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return num.toFixed(2);
}

/** 整数计数 → 千分位；同样不产生 NaN */
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

/** 有临时额度：条数 > 0 且金额解析后 > 0（0 额度与数据缺失走同一占位分支） */
// 只看金额：服务端 summary 可能给出合计而列表被分页/过滤掉（count 为 0），
// 那种情况额度其实是有的，不能因为 count 为 0 就显示占位符。
const hasTemporary = computed(() => {
  const total = Number(temporaryCredits.value.total_cny);
  return Number.isFinite(total) && total > 0;
});

const temporaryText = computed(() => (
  hasTemporary.value ? withCurrency(amountText(temporaryCredits.value.total_cny)) : PLACEHOLDER
));

/** ISO 时间 → 本地时区的 YYYY-MM-DD；空值或解析失败（NaN）返回空串，不展示小字 */
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

/** 最早到期那一笔的剩余金额：只在有到期日期且金额大于 0 时展示（¥x.xx） */
const temporaryNearestText = computed(() => {
  if (!hasTemporary.value || !temporaryExpireText.value) return '';
  const amount = amountText(temporaryCredits.value.nearest_amount_cny);
  if (amount === PLACEHOLDER || !(Number(amount) > 0)) return '';
  return withCurrency(amount);
});

/** 手机号掩码：只保留前三位与后四位（138****8000），太短没法掩码就原样展示 */
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

const displayName = computed(() => {
  const named = String(user.value.name || user.value.nickname || '').trim();
  if (named) return named;
  if (rawPhone.value) return maskPhone(rawPhone.value);
  if (rawEmail.value) return rawEmail.value;
  return '基元律动账号';
});

const avatarText = computed(() => displayName.value.slice(0, 1).toUpperCase() || '律');

function retry() {
  void store.refreshTrOverview();
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

/**
 * 按卡片位置计算浮层坐标：贴在卡片上方，左边界优先与卡片对齐，
 * 超出视口时向内侧收，保证整个浮层始终可见（不再被侧栏宽度裁掉）。
 */
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

// 打开时算一次（等 DOM 渲染完），窗口尺寸/滚动变化时跟着重算
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
  // 捕获阶段监听滚动：侧栏内部滚动不会冒泡到 window
  window.addEventListener('scroll', updatePosition, true);
});
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocumentMousedown, true);
  window.removeEventListener('resize', updatePosition);
  window.removeEventListener('scroll', updatePosition, true);
});
</script>

<style scoped>
/* 固定浅色，与登录弹窗同源（白底 / 细边框 / 黑色主按钮），尺寸更紧凑 */
.tr-acc {
  /* fixed + Teleport 到 body：宽度由内联 style 给出，不受侧栏 overflow 裁剪 */
  position: fixed;
  z-index: 1200;
  box-sizing: border-box;
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
/* 顶部账号信息 */
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
/* 头部右侧「官网」入口：小号描边按钮，margin-left:auto 把它推到行尾，昵称过长时自己缩 */
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
/* 加载态 / 失败态 */
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
/* 余额区 */
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
/* 临时额度一行：额度小、右侧到期日期更淡；无额度时只剩占位符与空白 */
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
/* 今日用量三列小格 */
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
/* 输入 / 输出是两个数字，压小一号并允许换行 */
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
/* 操作区：两行两列，尺寸统一 */
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
/* 部分数据失败 / 数据来源说明 */
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
/* 底部：数据来源 + 手动刷新（余额/额度是实时变动的，给个随手刷新的入口） */
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
