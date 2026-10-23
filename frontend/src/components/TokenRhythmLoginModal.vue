<template>
  <div
    v-if="store.trLoginModal.show"
    class="tr-mask"
    data-test="tr-login-mask"
    @mousedown.self="close()"
  >
    <div class="tr-card" data-test="tr-login-modal" role="dialog" aria-modal="true" aria-labelledby="tr-login-title">
      <!-- 顶部品牌区：官方 logo（图形 + tokenrhythm.ai + 基元律动）+ 关闭按钮 -->
      <div class="tr-top">
        <img class="tr-brand" :src="brandLogo" alt="基元律动 TokenRhythm">
        <button class="tr-close" data-test="tr-login-close" title="关闭" @click="close()">✕</button>
      </div>

      <!-- 已登录态：不展示表单，只展示账号与 Key 配置情况 -->
      <template v-if="loggedIn">
        <h2 id="tr-login-title" class="tr-title" data-test="tr-login-title">已登录</h2>
        <p class="tr-sub">当前账号已连接基元律动</p>
        <div class="tr-account" data-test="tr-logged">
          <span class="tr-account-avatar">{{ avatarText }}</span>
          <span class="tr-account-meta">
            <b data-test="tr-logged-name">{{ userLabel }}</b>
            <span>{{ store.trAuth.configured ? 'API Key 已配置，可直接开始对话' : 'API Key 尚未配置，请到「设置 → 模型」手动填写' }}</span>
          </span>
        </div>
        <div class="tr-foot">
          <button class="tr-btn" type="button" data-test="tr-logout" :disabled="submitting" @click="logout()">退出登录</button>
          <button class="tr-btn primary" type="button" data-test="tr-login-done" @click="close()">完成</button>
        </div>
      </template>

      <template v-else>
        <h2 id="tr-login-title" class="tr-title" data-test="tr-login-title">登录</h2>
        <p class="tr-sub">
          {{ tab === 'sms' ? '未注册手机号将自动创建账号，已注册手机号直接登录。' : '使用账号或手机号与密码登录，登录后自动配置 API Key。' }}
        </p>

        <!-- 登录说明与责任划分：默认折叠成一行，点了才展开，避免把登录表单挤下去 -->
        <div class="tr-disclaimer">
          <button
            type="button"
            class="tr-disclaimer-toggle"
            data-test="tr-disclaimer-toggle"
            :aria-expanded="showDisclaimer"
            @click="showDisclaimer = !showDisclaimer"
          >
            <span class="tr-disclaimer-title">使用基元律动账号登录 · 说明与责任划分</span>
            <span class="tr-disclaimer-caret" :class="{ open: showDisclaimer }" aria-hidden="true">⌄</span>
          </button>
          <ul v-show="showDisclaimer" class="tr-disclaimer-list" data-test="tr-disclaimer">
            <li>账号、余额、额度与模型服务均由基元律动（TokenRhythm）提供并负责，相关权利义务以基元律动的《用户协议》《隐私政策》为准。</li>
            <li>星瑶Code 不收集、不上传你的手机号、密码和短信验证码：它们只用于本次登录请求，不写入任何本地配置文件。</li>
            <li>登录后仅调用基元律动公开接口：自动创建并绑定 API Key（加密保存在本机）、查询用量与余额，不转存账号数据。</li>
            <li>API Key 建在你的基元律动账号下，可随时到官方控制台删除；退出登录不会删除已绑定的 Key。</li>
          </ul>
        </div>

        <!-- 短信登录 -->
        <form v-if="tab === 'sms'" class="tr-form" data-test="tr-sms-form" @submit.prevent="submitSms()">
          <label class="tr-field">
            <span class="tr-label">手机号<i class="tr-req">*</i></span>
            <input
              v-model="phone"
              class="tr-input"
              type="tel"
              inputmode="numeric"
              autocomplete="tel"
              maxlength="11"
              placeholder="请输入手机号"
              data-test="tr-phone-input"
              :disabled="submitting"
            >
          </label>
          <label class="tr-field">
            <span class="tr-label">短信验证码<i class="tr-req">*</i></span>
            <div class="tr-code-row">
              <input
                class="tr-input tr-code-input"
                type="text"
                inputmode="numeric"
                autocomplete="one-time-code"
                maxlength="6"
                placeholder="请输入验证码"
                data-test="tr-code-input"
                :value="code"
                :disabled="submitting"
                @input="onCodeInput($event)"
              >
              <!-- id 是阿里云验证码 SDK 的 button 选择器（#tr-send-code-btn），改结构时别删 -->
              <button
                id="tr-send-code-btn"
                type="button"
                class="tr-send"
                data-test="tr-send-code"
                :disabled="sendDisabled"
                @click="sendCode()"
              >{{ sendButtonText }}</button>
            </div>
          </label>
          <p v-if="maskedTip" class="tr-hint tr-masked" data-test="tr-masked-tip">{{ maskedTip }}</p>
          <label class="tr-field">
            <span class="tr-label">邀请码（选填）</span>
            <input
              v-model="inviteCode"
              class="tr-input"
              type="text"
              autocomplete="off"
              maxlength="64"
              placeholder="请输入邀请码"
              data-test="tr-invite-input"
              :disabled="submitting"
            >
          </label>

          <label class="tr-terms">
            <input
              v-model="agreed"
              type="checkbox"
              class="tr-checkbox"
              data-test="tr-terms-checkbox"
              :disabled="submitting"
            >
            <span>
              我已阅读并同意
              <a class="tr-link-inline" href="#" @click.prevent.stop="openLegal('terms')">《用户协议》</a>
              和
              <a class="tr-link-inline" href="#" @click.prevent.stop="openLegal('privacy')">《隐私政策》</a>
              ，并已知悉登录说明
            </span>
          </label>

          <button
            type="submit"
            class="tr-submit"
            data-test="tr-sms-submit"
            :disabled="submitting || !phone.trim() || code.trim().length < 6"
          >
            <span>{{ submitting ? '正在登录…' : '登录' }}</span>
            <span class="tr-arrow" aria-hidden="true">→</span>
          </button>
        </form>

        <!-- 密码登录（账号支持手机号 / 邮箱） -->
        <form v-else class="tr-form" data-test="tr-password-form" @submit.prevent="submitPassword()">
          <label class="tr-field">
            <span class="tr-label">账号<i class="tr-req">*</i></span>
            <input
              v-model="account"
              class="tr-input"
              type="text"
              autocomplete="username"
              placeholder="请输入手机号或邮箱"
              data-test="tr-account-input"
              :disabled="submitting"
            >
          </label>
          <label class="tr-field">
            <span class="tr-label">密码<i class="tr-req">*</i></span>
            <input
              v-model="password"
              class="tr-input"
              type="password"
              autocomplete="current-password"
              placeholder="请输入密码"
              data-test="tr-password-input"
              :disabled="submitting"
            >
          </label>

          <label class="tr-terms">
            <input
              v-model="agreed"
              type="checkbox"
              class="tr-checkbox"
              data-test="tr-terms-checkbox"
              :disabled="submitting"
            >
            <span>
              我已阅读并同意
              <a class="tr-link-inline" href="#" @click.prevent.stop="openLegal('terms')">《用户协议》</a>
              和
              <a class="tr-link-inline" href="#" @click.prevent.stop="openLegal('privacy')">《隐私政策》</a>
              ，并已知悉登录说明
            </span>
          </label>

          <button
            type="submit"
            class="tr-submit"
            data-test="tr-password-submit"
            :disabled="submitting || !account.trim() || !password"
          >
            <span>{{ submitting ? '正在登录…' : '登录' }}</span>
            <span class="tr-arrow" aria-hidden="true">→</span>
          </button>
        </form>

        <!-- 底部切换：与官方登录页一致，用文字链接而不是胶囊 tab -->
        <div class="tr-switch">
          <button
            v-if="tab === 'sms'"
            type="button"
            class="tr-switch-link"
            data-test="tr-tab-password"
            @click="switchTab('password')"
          >密码登录</button>
          <button
            v-else
            type="button"
            class="tr-switch-link"
            data-test="tr-tab-sms"
            @click="switchTab('sms')"
          >短信登录</button>
        </div>
      </template>

      <!-- 结果提示：失败原样展示后端 msg；绑定失败单独提示，绝不静默 -->
      <div v-if="notice" class="tr-notice-wrap">
        <p
          class="tr-notice"
          :class="notice.kind"
          :data-test="noticeTestId"
        >{{ notice.text }}</p>
      </div>
    </div>
  </div>

  <!-- 阿里云人机验证挂载点：常驻在 v-if 之外（弹窗关闭会重建 DOM，只有常驻节点才能让
       初始化过一次的 SDK 实例在关窗再打开后继续有效）；本身不需要可见尺寸，弹窗由 SDK 自己定位 -->
  <div id="tr-aliyun-captcha" class="tr-captcha-slot"></div>
</template>

<script lang="ts">
// 阿里云验证码 SDK 走运行时 <script> 注入，是挂在 window 上的全局单例；
// 这里补最小类型声明，避免 vue-tsc 报"window 上不存在该属性"。
interface AliyunCaptchaInstance {
  show?: () => void;
  hide?: () => void;
}

interface AliyunCaptchaInitOptions {
  SceneId: string;
  mode: 'popup';
  element: string;
  button: string;
  success: (captchaVerifyParam: string) => void;
  fail: () => void;
  getInstance: (instance: AliyunCaptchaInstance) => void;
}

declare global {
  interface Window {
    AliyunCaptchaConfig?: { region?: string; prefix?: string };
    initAliyunCaptcha?: (options: AliyunCaptchaInitOptions) => void;
  }
}
</script>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import brandLogo from '../assets/tokenrhythm-logo.svg';

const store = useAppStore();

type LoginTab = 'sms' | 'password';
type NoticeKind = 'error' | 'warn' | 'success';

/** 后端没回 interval_seconds 时的发送间隔兜底（秒） */
const DEFAULT_SMS_INTERVAL = 60;
/** 登录成功后自动关窗的延时（毫秒） */
const AUTO_CLOSE_DELAY = 1200;
/** 人机验证脚本加载 + 实例初始化的超时（毫秒）：超时即降级直接发码，不卡住用户 */
const CAPTCHA_INIT_TIMEOUT = 10000;

// 阿里云验证码参数只写在这里（与基元律动官方登录页同款），别在别处再硬编码一遍
const CAPTCHA_SDK_SRC = 'https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js';
const CAPTCHA_SCRIPT_ID = 'tr-aliyun-captcha-sdk';
const CAPTCHA_SCENE_ID = '6mwyro6e';
const CAPTCHA_PREFIX = 'rw34fd';
const CAPTCHA_REGION = 'cn';
/** 与模板里的挂载点 / 发送按钮 id 对应，改模板时同步这里 */
const CAPTCHA_ELEMENT = '#tr-aliyun-captcha';
const CAPTCHA_BUTTON = '#tr-send-code-btn';

/** 邀请码默认预填值：仅是表单初始值，用户可自行修改或清空，不在登录逻辑里做任何强制 */
const DEFAULT_INVITE_CODE = 'rf_tr_WXK47U';

const tab = ref<LoginTab>('sms');

// 短信登录
const phone = ref('');
const code = ref('');
/** 邀请码选填：默认预填推荐邀请码，用户可修改或清空；空串由后端归一成 None（不上送 inviteCode） */
const inviteCode = ref(DEFAULT_INVITE_CODE);
const maskedTip = ref('');
const countdown = ref(0);
const sendingCode = ref(false);
/** 本次发码的目标手机号：验证码弹窗是异步完成的，回调用点击那一刻的号码更可靠 */
const pendingPhone = ref('');

// 人机验证内部状态：只用于本组件决定走认证码还是降级，不直接展示给用户
const captchaReady = ref(false);
const captchaFailed = ref(false);
let captchaInstance: AliyunCaptchaInstance | null = null;
let captchaInitTask: Promise<boolean> | null = null;

// 密码登录
const account = ref('');
const password = ref('');

// 提交态与结果提示
const submitting = ref(false);
const notice = ref<{ kind: NoticeKind; text: string } | null>(null);
/** 协议勾选：与官方登录页一致，未勾选不允许提交（后端 login-or-register 需要 termsAccepted） */
const agreed = ref(false);
/** 登录说明默认收起：只在需要时展开，避免把登录表单挤到折叠线以下 */
const showDisclaimer = ref(false);

let countdownTimer: number | undefined;
let autoCloseTimer: number | undefined;

const loggedIn = computed(() => store.trAuth.status === 'loggedIn');

// user 字段结构后端未在前端固化，按常见字段兜底，拿不到就显示账号类型名
const userLabel = computed(() => {
  const user = (store.trAuth.user || {}) as Record<string, any>;
  return String(user.name || user.nickname || user.phone || user.account || '基元律动账号');
});

const avatarText = computed(() => userLabel.value.slice(0, 1).toUpperCase() || '律');

const sendDisabled = computed(() => submitting.value || sendingCode.value || countdown.value > 0 || !phone.value.trim());

const sendButtonText = computed(() => {
  if (countdown.value > 0) return `${countdown.value} 秒后可重发`;
  if (sendingCode.value) return '正在发送…';
  return '发送验证码';
});

// 结果提示的 data-test 随类型变化：失败 / 绑定失败 / 成功可分别断言
const noticeTestId = computed(() => {
  if (!notice.value) return 'tr-login-notice';
  if (notice.value.kind === 'error') return 'tr-login-error';
  if (notice.value.kind === 'warn') return 'tr-bind-error';
  return 'tr-login-success';
});

function stopCountdown() {
  if (countdownTimer !== undefined) {
    clearInterval(countdownTimer);
    countdownTimer = undefined;
  }
}

function startCountdown(seconds: number) {
  stopCountdown();
  const total = Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : DEFAULT_SMS_INTERVAL;
  countdown.value = total;
  countdownTimer = window.setInterval(() => {
    countdown.value -= 1;
    if (countdown.value <= 0) {
      countdown.value = 0;
      stopCountdown();
    }
  }, 1000);
}

function stopAutoClose() {
  if (autoCloseTimer !== undefined) {
    clearTimeout(autoCloseTimer);
    autoCloseTimer = undefined;
  }
}

/** 干净的成功（Key 已绑上）才自动关窗；绑定失败时留在窗口里让用户看清原因 */
function scheduleAutoClose() {
  stopAutoClose();
  autoCloseTimer = window.setTimeout(() => {
    autoCloseTimer = undefined;
    store.closeTrLoginModal();
  }, AUTO_CLOSE_DELAY);
}

function resetForm() {
  tab.value = 'sms';
  phone.value = '';
  code.value = '';
  inviteCode.value = DEFAULT_INVITE_CODE;
  agreed.value = false;
  showDisclaimer.value = false;
  maskedTip.value = '';
  sendingCode.value = false;
  account.value = '';
  password.value = '';
  submitting.value = false;
  notice.value = null;
  pendingPhone.value = '';
  stopCountdown();
  countdown.value = 0;
  stopAutoClose();
}

function close() {
  stopAutoClose();
  store.closeTrLoginModal();
}

/** 协议正文由基元律动提供（站内没有对应页面）：交给系统浏览器打开官网登录页查看全文 */
function openLegal(_kind: 'terms' | 'privacy') {
  void store.openExternalUrl('https://tokenrhythm.studio/login');
}

function switchTab(next: LoginTab) {
  if (tab.value === next) return;
  tab.value = next;
  notice.value = null;
}

/** 验证码只留数字且最多 6 位：粘贴带空格/短横线的短信内容时也能直接用 */
function onCodeInput(e: Event) {
  const el = e.target as HTMLInputElement;
  const digits = el.value.replace(/\D/g, '').slice(0, 6);
  if (el.value !== digits) el.value = digits;
  code.value = digits;
}

// ── 阿里云人机验证 ────────────────────────────────────────────────────
// 官方登录页的做法：手机号校验通过后主动 instance.show()，真正的发码请求放在
// success(captchaVerifyParam) 回调里发。SDK 加载 / 初始化任一步失败都只记内部状态，
// 并降级成"不带 captchaVerifyParam 直接发码"，不把报错抛给用户。

/** 给任意 promise 加超时，避免 SDK 静默卡死时按钮一直转圈 */
function withTimeout<T>(task: Promise<T>, timeout: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error('验证码脚本加载超时')), timeout);
    task.then(
      (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        window.clearTimeout(timer);
        reject(err);
      },
    );
  });
}

/** 运行时注入 SDK；全局已有 initAliyunCaptcha（脚本已在页面里）时直接跳过 */
function loadCaptchaScript(): Promise<void> {
  if (typeof window.initAliyunCaptcha === 'function') return Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    // 上一次注入残留的失败节点先清掉，避免复用一个永远等不到 load 的节点
    document.getElementById(CAPTCHA_SCRIPT_ID)?.remove();
    const script = document.createElement('script');
    script.id = CAPTCHA_SCRIPT_ID;
    script.src = CAPTCHA_SDK_SRC;
    script.async = true;
    script.addEventListener('load', () => resolve(), { once: true });
    script.addEventListener('error', () => reject(new Error('验证码脚本加载失败')), { once: true });
    document.head.appendChild(script);
  });
}

/** SDK 内部渲染是异步的，getInstance 会稍后才回调，这里轮询等待实例真正交出来 */
function waitCaptchaReady(timeout: number): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    if (captchaInstance) {
      resolve(true);
      return;
    }
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      if (captchaInstance) {
        window.clearInterval(timer);
        resolve(true);
      } else if (Date.now() - startedAt >= timeout) {
        window.clearInterval(timer);
        resolve(false);
      }
    }, 50);
  });
}

function initCaptcha(): void {
  const init = window.initAliyunCaptcha;
  if (typeof init !== 'function') throw new Error('验证码 SDK 未就绪');
  // 官方要求：initAliyunCaptcha 之前先写好 region / prefix
  window.AliyunCaptchaConfig = { region: CAPTCHA_REGION, prefix: CAPTCHA_PREFIX };
  init({
    SceneId: CAPTCHA_SCENE_ID,
    mode: 'popup',
    element: CAPTCHA_ELEMENT,
    button: CAPTCHA_BUTTON,
    success: (captchaVerifyParam: string) => {
      void onCaptchaSuccess(captchaVerifyParam);
    },
    fail: () => {
      // 用户关掉弹窗或校验没通过：放开按钮即可，不额外弹错（可重试或改用密码登录）
      sendingCode.value = false;
      pendingPhone.value = '';
    },
    getInstance: (instance: AliyunCaptchaInstance) => {
      captchaInstance = instance || null;
      captchaReady.value = !!captchaInstance;
    },
  });
}

/**
 * 保证人机验证可用（幂等）：SDK 已在页面则跳过脚本加载，否则注入后初始化。
 * 失败（离线 / 被拦截 / 超时 / 环境不支持）只置内部标记并返回 false，由调用方降级。
 */
async function ensureCaptcha(): Promise<boolean> {
  if (captchaReady.value && captchaInstance) return true;
  if (captchaFailed.value) return false;
  if (captchaInitTask) return captchaInitTask;
  captchaInitTask = (async () => {
    try {
      await withTimeout(loadCaptchaScript(), CAPTCHA_INIT_TIMEOUT);
      initCaptcha();
      if (!(await waitCaptchaReady(CAPTCHA_INIT_TIMEOUT))) throw new Error('验证码实例初始化超时');
      return true;
    } catch (err) {
      captchaFailed.value = true;
      captchaReady.value = false;
      captchaInstance = null;
      console.warn('[tr-login] 人机验证不可用，本次降级直接发送验证码：', err);
      return false;
    } finally {
      captchaInitTask = null;
    }
  })();
  return captchaInitTask;
}

/** 走桥接发码；captchaParam 为空是降级路径（后端按空串归一，并可能回"安全验证"提示） */
async function requestSms(phoneValue: string, captchaParam?: string) {
  sendingCode.value = true;
  try {
    const res = await store.trSendSms(phoneValue, captchaParam);
    if (!res || !res.ok) {
      // 后端文案（如"安全验证未通过，请改用密码登录"）原样展示，别自己改写
      notice.value = { kind: 'error', text: (res && res.msg) || '验证码发送失败，请稍后重试' };
      return;
    }
    maskedTip.value = res.masked_target ? `已发送至 ${res.masked_target}` : '验证码已发送，请查收短信';
    startCountdown(Number(res.interval_seconds));
  } finally {
    sendingCode.value = false;
    pendingPhone.value = '';
  }
}

/** 人机验证通过：captchaVerifyParam 必须原样随发码请求提交（后端据此校验） */
async function onCaptchaSuccess(captchaVerifyParam: string) {
  const phoneValue = pendingPhone.value || phone.value.trim();
  if (!phoneValue) return;
  notice.value = null;
  await requestSms(phoneValue, captchaVerifyParam);
}

async function sendCode() {
  if (sendDisabled.value) return;
  const phoneValue = phone.value.trim();
  if (!phoneValue) {
    notice.value = { kind: 'error', text: '请先输入手机号' };
    return;
  }
  notice.value = null;
  pendingPhone.value = phoneValue;
  sendingCode.value = true;
  let captchaUsable = false;
  try {
    captchaUsable = await ensureCaptcha();
  } finally {
    sendingCode.value = false;
  }
  const instance = captchaInstance;
  if (!captchaUsable || !instance || typeof instance.show !== 'function') {
    // 人机验证不可用：直接按老路径发码，别让用户卡在弹窗上
    await requestSms(phoneValue);
    return;
  }
  try {
    // 只唤起验证码、不发请求：真正发码在 success 回调里
    instance.show();
  } catch (err) {
    console.warn('[tr-login] 唤起人机验证失败，降级直接发送验证码：', err);
    captchaFailed.value = true;
    captchaReady.value = false;
    captchaInstance = null;
    await requestSms(phoneValue);
  }
}

/** 登录结果统一处理：失败原样展示 msg，绑定失败明确提示，成功按 created 区分文案 */
function applyLoginResult(res: any) {
  if (!res || !res.ok) {
    notice.value = { kind: 'error', text: (res && res.msg) || '登录没有成功，请稍后再试' };
    return;
  }
  if (res.bind_ok === false) {
    notice.value = {
      kind: 'warn',
      text: `登录成功，但自动绑定 API Key 失败：${res.bind_error || '未知原因'}`,
    };
    return;
  }
  const created = !!(res.bind && res.bind.created);
  notice.value = {
    kind: 'success',
    text: created ? '登录成功，已自动创建并绑定 API Key' : '登录成功，本地已有可用 API Key',
  };
  // 全局账号状态以 store 为准，这里再拉一次权威快照（后端已写入登录态）
  void store.refreshTrAuth();
  scheduleAutoClose();
}

async function submitSms() {
  if (submitting.value) return;
  if (!agreed.value) {
    notice.value = { kind: 'error', text: '请先阅读并同意《用户协议》和《隐私政策》' };
    return;
  }
  const phoneValue = phone.value.trim();
  const codeValue = code.value.trim();
  if (!phoneValue) {
    notice.value = { kind: 'error', text: '请先输入手机号' };
    return;
  }
  if (codeValue.length < 6) {
    notice.value = { kind: 'error', text: '请输入 6 位验证码' };
    return;
  }
  submitting.value = true;
  notice.value = null;
  try {
    applyLoginResult(await store.trSmsLogin(phoneValue, codeValue, inviteCode.value.trim()));
  } finally {
    submitting.value = false;
  }
}

async function submitPassword() {
  if (submitting.value) return;
  if (!agreed.value) {
    notice.value = { kind: 'error', text: '请先阅读并同意《用户协议》和《隐私政策》' };
    return;
  }
  const accountValue = account.value.trim();
  if (!accountValue) {
    notice.value = { kind: 'error', text: '请输入手机号或邮箱' };
    return;
  }
  if (!password.value) {
    notice.value = { kind: 'error', text: '请输入密码' };
    return;
  }
  submitting.value = true;
  notice.value = null;
  try {
    applyLoginResult(await store.trPasswordLogin(accountValue, password.value));
  } finally {
    submitting.value = false;
  }
}

async function logout() {
  if (submitting.value) return;
  submitting.value = true;
  try {
    const res = await store.trLogout();
    if (res && res.ok) {
      // 文案以后端返回为准：删 Key / 清模型配置的结果都在里面，避免前端猜
      const deleted = (res as any)?.key_deleted;
      const hadKey = (res as any)?.had_key;
      const backendMsg = String((res as any)?.msg || '');
      notice.value = {
        kind: hadKey && deleted === false ? 'warn' : 'success',
        text: backendMsg || '已退出基元律动账号',
      };
      tab.value = 'sms';
    } else {
      notice.value = { kind: 'error', text: (res && res.msg) || '退出账号失败，请稍后再试' };
    }
  } finally {
    submitting.value = false;
  }
}

// 弹窗每次打开都从干净状态开始；关闭时清掉定时器，避免后台空转
watch(
  () => store.trLoginModal.show,
  (open) => {
    if (open) {
      resetForm();
      // 每次打开都允许重新尝试人机验证：上一次失败可能只是网络抖动或环境刚恢复，
      // 永久记住失败会让用户重启应用前一直走降级直发。已成功的实例仍复用。
      if (!captchaInstance) captchaFailed.value = false;
      if (store.trAuth.status === 'unknown') void store.refreshTrAuth();
    } else {
      pendingPhone.value = '';
      sendingCode.value = false;
      stopCountdown();
      stopAutoClose();
    }
  },
);

// Esc 关闭：与项目内其他弹窗保持一致的交互习惯（遮罩点击 / 右上角 × 也都能关）
function onWindowKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape' || !store.trLoginModal.show) return;
  close();
}

onMounted(() => window.addEventListener('keydown', onWindowKeydown));
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onWindowKeydown);
  // 只清本组件自己的定时器：SDK 与实例是页面级单例，这里不销毁，下次打开直接复用
  stopCountdown();
  stopAutoClose();
});
</script>

<style scoped>
/* 弹窗固定浅色：对齐基元律动官方登录页（白底黑字），不跟随深色主题，
   否则白卡片里的文字会被主题变量改成浅色而看不清。 */
.tr-mask {
  position: fixed;
  inset: 0;
  z-index: 1200;
  background: rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
}
/* 人机验证挂载点：常驻的 0 尺寸节点，SDK 弹窗自己是 fixed 定位，层级要压过登录遮罩 */
.tr-captcha-slot {
  position: fixed;
  top: 0;
  left: 0;
  width: 0;
  height: 0;
  z-index: 1300;
}
.tr-card {
  width: min(460px, 94vw);
  max-height: 92vh;
  overflow-y: auto;
  padding: 26px 38px 30px;
  background: #fff;
  color: #111;
  color-scheme: light;
  border-radius: 18px;
  box-shadow: 0 24px 70px rgba(0, 0, 0, 0.45);
  animation: tr-pop 0.2s;
}
@keyframes tr-pop {
  from {
    transform: scale(0.96);
    opacity: 0;
  }
  to {
    transform: none;
    opacity: 1;
  }
}
/* 顶部：官方 logo 靠左、关闭按钮靠右 */
.tr-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 26px;
}
.tr-brand {
  display: block;
  width: 150px;
  height: auto;
}
.tr-close {
  margin-top: 2px;
  padding: 4px 6px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: #8a8a8a;
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
}
.tr-close:hover {
  color: #111;
  background: #f2f2f2;
}
.tr-title {
  font-size: 30px;
  font-weight: 700;
  letter-spacing: -0.5px;
  color: #111;
}
.tr-sub {
  margin: 10px 0 26px;
  font-size: 14px;
  line-height: 1.7;
  color: #6b6b6b;
}
.tr-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.tr-field {
  display: block;
}
.tr-label {
  display: block;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
  color: #222;
}
/* 必填星号：与官方一致用红色 */
.tr-req {
  margin-left: 3px;
  color: #e5484d;
  font-style: normal;
}
.tr-input {
  width: 100%;
  height: 46px;
  padding: 0 14px;
  background: #fff;
  border: 1px solid #e3e3e3;
  border-radius: 10px;
  color: #111;
  font-size: 14px;
  font-family: inherit;
  outline: none;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.tr-input::placeholder {
  color: #a8a8a8;
}
.tr-input:focus {
  border-color: #111;
  box-shadow: 0 0 0 3px rgba(17, 17, 17, 0.06);
}
.tr-input:disabled {
  background: #fafafa;
  color: #999;
}
.tr-code-row {
  display: flex;
  gap: 10px;
}
.tr-code-input {
  flex: 1;
  min-width: 0;
  letter-spacing: 1px;
}
.tr-send {
  flex: 0 0 auto;
  min-width: 108px;
  height: 46px;
  padding: 0 16px;
  border: 1px solid #e3e3e3;
  border-radius: 10px;
  background: #f5f5f5;
  color: #333;
  font-size: 13px;
  white-space: nowrap;
  cursor: pointer;
  transition: background 0.15s ease;
}
.tr-send:hover:not(:disabled) {
  background: #ededed;
}
.tr-send:disabled {
  color: #9a9a9a;
  cursor: not-allowed;
}
.tr-hint {
  font-size: 12.5px;
  line-height: 1.6;
  color: #9a9a9a;
}
.tr-masked {
  color: #1a7f37;
}
/* 登录说明与责任划分：默认收起成一行，点标题展开，避免挤占登录表单 */
.tr-disclaimer {
  margin: 0 0 18px;
  padding: 10px 14px;
  border: 1px solid #eee;
  border-radius: 12px;
  background: #fafafa;
  color: #5f5f5f;
}
.tr-disclaimer-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  width: 100%;
  padding: 0;
  border: 0;
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.tr-disclaimer-title {
  font-size: 12.5px;
  font-weight: 600;
  color: #333;
}
.tr-disclaimer-caret {
  flex: 0 0 auto;
  font-size: 13px;
  color: #9a9a9a;
  transition: transform 0.18s ease;
}
.tr-disclaimer-caret.open {
  transform: rotate(180deg);
}
.tr-disclaimer-list {
  margin: 8px 0 0;
  padding-left: 16px;
  list-style: disc;
  font-size: 12.5px;
  line-height: 1.7;
}
.tr-disclaimer-list li + li {
  margin-top: 4px;
}
/* 协议勾选行 */
.tr-terms {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  font-size: 13px;
  line-height: 1.6;
  color: #444;
  cursor: pointer;
}
.tr-checkbox {
  flex: 0 0 auto;
  width: 16px;
  height: 16px;
  margin-top: 2px;
  accent-color: #111;
  cursor: pointer;
}
.tr-link-inline {
  color: #111;
  font-weight: 600;
  text-decoration: underline;
}
.tr-link-inline:hover {
  color: #000;
}
/* 登录按钮：黑底白字 + 右侧箭头，对齐官方样式 */
.tr-submit {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  height: 52px;
  margin-top: 6px;
  padding: 0 20px;
  border: none;
  border-radius: 10px;
  background: #111;
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  transition: opacity 0.15s ease, transform 0.1s ease;
}
.tr-submit:hover:not(:disabled) {
  opacity: 0.88;
}
.tr-submit:active:not(:disabled) {
  transform: scale(0.995);
}
.tr-submit:disabled {
  background: #c9c9c9;
  color: #fff;
  cursor: not-allowed;
}
.tr-arrow {
  font-size: 17px;
  font-weight: 400;
}
/* 底部登录方式切换：文字链接（不是胶囊 tab） */
.tr-switch {
  margin-top: 22px;
  text-align: center;
}
.tr-switch-link {
  padding: 4px 8px;
  border: 0;
  background: transparent;
  color: #6b6b6b;
  font-size: 13px;
  text-decoration: underline;
  cursor: pointer;
}
.tr-switch-link:hover {
  color: #111;
}
/* 结果提示 */
.tr-notice-wrap {
  margin-top: 16px;
}
.tr-notice {
  padding: 10px 12px;
  border-radius: 10px;
  font-size: 13px;
  line-height: 1.6;
  word-break: break-word;
}
.tr-notice.error {
  background: #fdecec;
  color: #c02626;
}
.tr-notice.warn {
  background: #fff6e5;
  color: #9a6700;
}
.tr-notice.success {
  background: #e9f7ec;
  color: #1a7f37;
}
/* 已登录态 */
.tr-account {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px;
  border: 1px solid #ececec;
  border-radius: 12px;
  background: #fafafa;
}
.tr-account-avatar {
  flex: 0 0 38px;
  width: 38px;
  height: 38px;
  border-radius: 50%;
  background: #111;
  color: #fff;
  display: grid;
  place-items: center;
  font-size: 15px;
  font-weight: 600;
}
.tr-account-meta {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.tr-account-meta b {
  font-size: 14px;
  color: #111;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tr-account-meta span {
  font-size: 12.5px;
  line-height: 1.5;
  color: #7a7a7a;
}
.tr-foot {
  margin-top: 22px;
  display: flex;
  gap: 10px;
  justify-content: flex-end;
}
.tr-btn {
  padding: 9px 18px;
  border: 1px solid #e3e3e3;
  border-radius: 10px;
  background: #f5f5f5;
  color: #333;
  font-size: 13.5px;
  cursor: pointer;
}
.tr-btn:hover {
  background: #ededed;
}
.tr-btn.primary {
  background: #111;
  border-color: #111;
  color: #fff;
  font-weight: 600;
}
.tr-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
/* 窄屏（手机 / 窄窗口）：收窄内边距，避免表单被挤压 */
@media (max-width: 480px) {
  .tr-card {
    padding: 22px 20px 24px;
  }
  .tr-title {
    font-size: 26px;
  }
}
</style>

