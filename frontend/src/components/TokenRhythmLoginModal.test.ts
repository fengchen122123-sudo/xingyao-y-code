import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import TokenRhythmLoginModal from './TokenRhythmLoginModal.vue';
import { useAppStore } from '../stores/appStore';

// 桥接 mock：默认未登录、发码成功、两种登录都成功；单个用例按需覆盖
function installBridge(overrides: Record<string, any> = {}) {
  const api: Record<string, any> = {
    tr_auth_status: vi.fn(async () => ({ ok: true, logged_in: false, user: null, configured: false })),
    tr_auth_send_sms: vi.fn(async () => ({
      ok: true,
      challenge_id: 'challenge-1',
      interval_seconds: 60,
      ttl_seconds: 300,
      masked_target: '138****8000',
      msg: '验证码已发送',
    })),
    tr_auth_sms_login: vi.fn(async () => ({
      ok: true,
      user: { name: '阿晨' },
      bind: { ok: true, created: true },
      bind_ok: true,
      bind_error: '',
      settings: {},
      msg: '登录成功，已自动配置基元律动 API Key',
    })),
    tr_auth_password_login: vi.fn(async () => ({
      ok: true,
      user: { nickname: '阿晨' },
      bind: { ok: true, created: false },
      bind_ok: true,
      bind_error: '',
      settings: {},
      msg: '登录成功，本地已有可用 API Key',
    })),
    tr_auth_logout: vi.fn(async () => ({ ok: true, msg: '已退出基元律动账号' })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

// ── 阿里云人机验证 mock ────────────────────────────────────────────────
// 默认装"SDK 不可用"（等价 jsdom 里脚本加载失败 / 超时 / 被拦截）：这样原有 8 个
// 用例走的是降级发码路径，行为与集成前一致；要覆盖验证码路径的用例自己调
// installWorkingCaptcha()。
let lastInitOptions: any = null;

function installBrokenCaptcha() {
  (window as any).initAliyunCaptcha = vi.fn(() => {
    throw new Error('vitest: captcha sdk unavailable');
  });
}

/** 可用的人机验证：记录初始化参数；show() 不自动成功，由用例显式触发 success */
function installWorkingCaptcha() {
  const instance = { show: vi.fn(), hide: vi.fn() };
  const init = vi.fn((options: any) => {
    lastInitOptions = options;
    options.getInstance(instance);
  });
  (window as any).initAliyunCaptcha = init;
  return { init, instance };
}

let wrapper: VueWrapper | null = null;

function openModal() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.openTrLoginModal();
  wrapper = mount(TokenRhythmLoginModal, { global: { plugins: [pinia] } });
  return { store, wrapper, api: (window as any).pywebview.api };
}

/** 短信表单填好手机号与验证码并提交（按钮在校验通过后才可点，故直接提交表单）；invite 非空时一并填邀请码 */
async function submitSms(w: VueWrapper, phone = '13800138000', code = '123456', invite = '') {
  await w.get('[data-test="tr-phone-input"]').setValue(phone);
  await w.get('[data-test="tr-code-input"]').setValue(code);
  if (invite) await w.get('[data-test="tr-invite-input"]').setValue(invite);
  await w.get('[data-test="tr-terms-checkbox"]').setValue(true);
  await w.get('[data-test="tr-sms-form"]').trigger('submit');
  await flushPromises();
}

beforeEach(() => {
  installBrokenCaptcha();
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  delete (window as any).pywebview;
  // 人机验证的 mock 是挂在 window 上的全局状态，必须清掉，避免污染其它组件的测试
  delete (window as any).initAliyunCaptcha;
  delete (window as any).AliyunCaptchaConfig;
  document.getElementById('tr-aliyun-captcha-sdk')?.remove();
  lastInitOptions = null;
  vi.useRealTimers();
});

it('登录前展示使用基元律动账号登录的说明与责任划分', async () => {
  installBridge();
  const { wrapper: w } = openModal();

  const tip = w.get('[data-test="tr-disclaimer"]').text();
  expect(tip).toContain('基元律动');
  // 三条关键承诺必须写清楚：不收集信息、只用公开接口、账号责任归属
  expect(tip).toContain('不收集');
  expect(tip).toContain('公开接口');
  expect(tip).toContain('提供并负责');
});

it('登录说明默认收起，点标题展开、再点收起', async () => {
  installBridge();
  const { wrapper: w } = openModal();

  // v-show 收起时由 Vue 写入内联 display: none，这里直接断言该样式，比 isVisible 稳
  const listStyle = () => w.get('[data-test="tr-disclaimer"]').attributes('style') || '';
  expect(listStyle()).toContain('display: none');
  expect(w.get('[data-test="tr-disclaimer-toggle"]').attributes('aria-expanded')).toBe('false');

  await w.get('[data-test="tr-disclaimer-toggle"]').trigger('click');
  expect(listStyle()).not.toContain('display: none');
  expect(w.get('[data-test="tr-disclaimer-toggle"]').attributes('aria-expanded')).toBe('true');
  expect(w.get('[data-test="tr-disclaimer"]').text()).toContain('不收集');

  await w.get('[data-test="tr-disclaimer-toggle"]').trigger('click');
  expect(listStyle()).toContain('display: none');
});

it('默认展示短信登录，可切换到密码登录', async () => {
  installBridge();
  const { wrapper: w } = openModal();

  expect(w.find('[data-test="tr-sms-form"]').exists()).toBe(true);
  expect(w.find('[data-test="tr-password-form"]').exists()).toBe(false);
  // 新样式改成底部文字链接切换：短信页展示的是「密码登录」入口
  expect(w.get('[data-test="tr-tab-password"]').text()).toContain('密码登录');

  await w.get('[data-test="tr-tab-password"]').trigger('click');

  expect(w.find('[data-test="tr-password-form"]').exists()).toBe(true);
  expect(w.find('[data-test="tr-sms-form"]').exists()).toBe(false);
  expect(w.get('[data-test="tr-password-input"]').attributes('type')).toBe('password');
});

it('发送验证码成功后展示掩码手机号，按钮进入倒计时禁用', async () => {
  vi.useFakeTimers();
  const api = installBridge();
  const { wrapper: w } = openModal();

  await w.get('[data-test="tr-phone-input"]').setValue('13800138000');
  await w.get('[data-test="tr-send-code"]').trigger('click');
  await vi.advanceTimersByTimeAsync(0);
  await nextTick();

  expect(api.tr_auth_send_sms).toHaveBeenCalledWith('13800138000');
  expect(w.get('[data-test="tr-masked-tip"]').text()).toContain('138****8000');

  const sendBtn = w.get('[data-test="tr-send-code"]');
  expect(sendBtn.attributes('disabled')).toBeDefined();
  expect(sendBtn.text()).toContain('60');

  await vi.advanceTimersByTimeAsync(1000);
  await nextTick();
  expect(w.get('[data-test="tr-send-code"]').text()).toContain('59');

  // 倒计时走完后恢复可点
  await vi.advanceTimersByTimeAsync(59000);
  await nextTick();
  expect(w.get('[data-test="tr-send-code"]').attributes('disabled')).toBeUndefined();
});

it('短信登录成功时调用桥接并展示自动创建 API Key 的成功文案', async () => {
  const api = installBridge({
    tr_auth_status: vi.fn(async () => ({ ok: true, logged_in: true, user: { name: '阿晨' }, configured: true })),
  });
  const { store, wrapper: w } = openModal();

  await submitSms(w);

  expect(api.tr_auth_sms_login).toHaveBeenCalledWith('13800138000', '123456', 'rf_tr_WXK47U');
  expect(w.get('[data-test="tr-login-success"]').text()).toContain('自动创建并绑定 API Key');
  expect(store.trAuth.status).toBe('loggedIn');
});

it('登录失败时原样展示后端返回的 msg', async () => {
  installBridge({
    tr_auth_sms_login: vi.fn(async () => ({ ok: false, msg: '验证码错误或已过期，请重新获取' })),
  });
  const { wrapper: w } = openModal();

  await submitSms(w);

  expect(w.get('[data-test="tr-login-error"]').text()).toContain('验证码错误或已过期，请重新获取');
});

it('密码登录成功时调用桥接并区分「本地已有可用 API Key」', async () => {
  const api = installBridge({
    tr_auth_status: vi.fn(async () => ({ ok: true, logged_in: true, user: { nickname: '阿晨' }, configured: true })),
  });
  const { wrapper: w } = openModal();

  await w.get('[data-test="tr-tab-password"]').trigger('click');
  await w.get('[data-test="tr-account-input"]').setValue('me@example.com');
  await w.get('[data-test="tr-password-input"]').setValue('secret-pass');
  await w.get('[data-test="tr-terms-checkbox"]').setValue(true);
  await w.get('[data-test="tr-password-form"]').trigger('submit');
  await flushPromises();

  expect(api.tr_auth_password_login).toHaveBeenCalledWith('me@example.com', 'secret-pass');
  expect(w.get('[data-test="tr-login-success"]').text()).toContain('本地已有可用 API Key');
});

it('登录成功但绑定失败时明确提示 bind_error，不静默', async () => {
  installBridge({
    tr_auth_sms_login: vi.fn(async () => ({
      ok: true,
      user: { phone: '138****8000' },
      bind: { ok: false, created: false, error: '网络超时' },
      bind_ok: false,
      bind_error: '网络超时',
      settings: {},
      msg: '登录成功，但自动绑定 API Key 失败：网络超时',
    })),
  });
  const { wrapper: w } = openModal();

  await submitSms(w);

  const tip = w.get('[data-test="tr-bind-error"]').text();
  expect(tip).toContain('登录成功');
  expect(tip).toContain('自动绑定 API Key 失败');
  expect(tip).toContain('网络超时');
});

it('登录成功 1.2 秒后自动关闭弹窗', async () => {
  vi.useFakeTimers();
  installBridge({
    tr_auth_status: vi.fn(async () => ({ ok: true, logged_in: true, user: { name: '阿晨' }, configured: true })),
  });
  const { store, wrapper: w } = openModal();

  await w.get('[data-test="tr-phone-input"]').setValue('13800138000');
  await w.get('[data-test="tr-code-input"]').setValue('123456');
  await w.get('[data-test="tr-terms-checkbox"]').setValue(true);
  await w.get('[data-test="tr-sms-form"]').trigger('submit');
  await vi.advanceTimersByTimeAsync(0);
  await nextTick();

  expect(store.trLoginModal.show).toBe(true);

  await vi.advanceTimersByTimeAsync(1200);
  await nextTick();
  expect(store.trLoginModal.show).toBe(false);
});

it('点击遮罩、点关闭按钮或按 Esc 都能关闭弹窗', async () => {
  installBridge();
  const { store, wrapper: w } = openModal();

  await w.get('[data-test="tr-login-mask"]').trigger('mousedown');
  expect(store.trLoginModal.show).toBe(false);

  store.openTrLoginModal();
  await nextTick();
  await w.get('[data-test="tr-login-close"]').trigger('click');
  expect(store.trLoginModal.show).toBe(false);

  store.openTrLoginModal();
  await nextTick();
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await nextTick();
  expect(store.trLoginModal.show).toBe(false);
});

// ── 人机验证（阿里云验证码）流程 ──────────────────────────────────────
it('人机验证可用时：点击发送只唤起验证码，success 回调后才带 captchaVerifyParam 发码', async () => {
  const { init, instance } = installWorkingCaptcha();
  const api = installBridge();
  const { wrapper: w } = openModal();

  await w.get('[data-test="tr-phone-input"]').setValue('13800138000');
  await w.get('[data-test="tr-send-code"]').trigger('click');
  await flushPromises();

  // 只弹验证码，绝不提前发码
  expect(init).toHaveBeenCalledTimes(1);
  expect(instance.show).toHaveBeenCalledTimes(1);
  expect(api.tr_auth_send_sms).not.toHaveBeenCalled();

  // 模拟用户在弹窗里完成验证：param 必须原样作为第二个参数带给桥接
  lastInitOptions.success('captcha-xyz');
  await flushPromises();

  expect(api.tr_auth_send_sms).toHaveBeenCalledTimes(1);
  expect(api.tr_auth_send_sms).toHaveBeenCalledWith('13800138000', 'captcha-xyz');
  expect(w.get('[data-test="tr-masked-tip"]').text()).toContain('138****8000');
  expect(w.get('[data-test="tr-send-code"]').attributes('disabled')).toBeDefined();
  expect(w.get('[data-test="tr-send-code"]').text()).toContain('60');
});

it('人机验证不可用时：降级直接发码（不带 captchaVerifyParam），仍进入倒计时', async () => {
  const api = installBridge();
  const { wrapper: w } = openModal();

  await w.get('[data-test="tr-phone-input"]').setValue('13800138000');
  await w.get('[data-test="tr-send-code"]').trigger('click');
  await flushPromises();

  expect(api.tr_auth_send_sms).toHaveBeenCalledTimes(1);
  expect(api.tr_auth_send_sms.mock.calls[0][0]).toBe('13800138000');
  // 降级时不传第二个参数，后端按空串归一
  expect(api.tr_auth_send_sms.mock.calls[0].length).toBe(1);
  expect(w.get('[data-test="tr-masked-tip"]').text()).toContain('138****8000');
  expect(w.get('[data-test="tr-send-code"]').text()).toContain('60');
});

it('初始化人机验证时使用官方 SceneId / popup 模式，并先写入 region 与 prefix', async () => {
  const { init } = installWorkingCaptcha();
  installBridge();
  const { wrapper: w } = openModal();

  await w.get('[data-test="tr-phone-input"]').setValue('13800138000');
  await w.get('[data-test="tr-send-code"]').trigger('click');
  await flushPromises();

  const options = init.mock.calls[0][0];
  expect(options.SceneId).toBe('6mwyro6e');
  expect(options.mode).toBe('popup');
  expect(options.element).toBe('#tr-aliyun-captcha');
  expect(options.button).toBe('#tr-send-code-btn');
  expect((window as any).AliyunCaptchaConfig).toEqual({ region: 'cn', prefix: 'rw34fd' });
});

it('降级发码时后端返回「安全验证」错误，原样展示后端文案', async () => {
  installBridge({
    tr_auth_send_sms: vi.fn(async () => ({ ok: false, msg: '安全验证未通过，请改用密码登录' })),
  });
  const { wrapper: w } = openModal();

  await w.get('[data-test="tr-phone-input"]').setValue('13800138000');
  await w.get('[data-test="tr-send-code"]').trigger('click');
  await flushPromises();

  expect(w.get('[data-test="tr-login-error"]').text()).toContain('安全验证未通过，请改用密码登录');
  // 发码失败不该进入倒计时，按钮仍是可点的「发送验证码」
  expect(w.get('[data-test="tr-send-code"]').text()).toContain('发送验证码');
  expect(w.get('[data-test="tr-send-code"]').attributes('disabled')).toBeUndefined();
});

// ── 协议勾选 ──────────────────────────────────────────────────────────
it('未勾选用户协议时提交会被拦截，并提示先同意协议', async () => {
  const api = installBridge();
  const { wrapper: w } = openModal();

  await w.get('[data-test="tr-phone-input"]').setValue('13800138000');
  await w.get('[data-test="tr-code-input"]').setValue('123456');
  await w.get('[data-test="tr-sms-form"]').trigger('submit');
  await flushPromises();

  expect(api.tr_auth_sms_login).not.toHaveBeenCalled();
  expect(w.get('[data-test="tr-login-error"]').text()).toContain('用户协议');
});

// ── 邀请码（选填） ────────────────────────────────────────────────────
it('填写邀请码时，作为第三个参数透传给短信登录桥接', async () => {
  const api = installBridge({
    tr_auth_status: vi.fn(async () => ({ ok: true, logged_in: true, user: { name: '阿晨' }, configured: true })),
  });
  const { wrapper: w } = openModal();

  const inviteInput = w.get('[data-test="tr-invite-input"]');
  expect(inviteInput.attributes('placeholder')).toBe('请输入邀请码');
  expect(inviteInput.attributes('maxlength')).toBe('64');

  await submitSms(w, '13800138000', '123456', 'INV-2026');

  expect(api.tr_auth_sms_login).toHaveBeenCalledTimes(1);
  expect(api.tr_auth_sms_login).toHaveBeenCalledWith('13800138000', '123456', 'INV-2026');
  expect(w.get('[data-test="tr-login-success"]').text()).toContain('自动创建并绑定 API Key');
});

it('默认预填推荐邀请码，不改动直接提交时随登录上送', async () => {
  const api = installBridge({
    tr_auth_status: vi.fn(async () => ({ ok: true, logged_in: true, user: { name: '阿晨' }, configured: true })),
  });
  const { wrapper: w } = openModal();

  expect((w.get('[data-test="tr-invite-input"]').element as HTMLInputElement).value).toBe('rf_tr_WXK47U');

  await submitSms(w);

  expect(api.tr_auth_sms_login).toHaveBeenCalledTimes(1);
  expect(api.tr_auth_sms_login).toHaveBeenCalledWith('13800138000', '123456', 'rf_tr_WXK47U');
});

it('清空默认邀请码后提交，第三个参数为空串（后端据此归一成 None）', async () => {
  const api = installBridge({
    tr_auth_status: vi.fn(async () => ({ ok: true, logged_in: true, user: { name: '阿晨' }, configured: true })),
  });
  const { wrapper: w } = openModal();

  await w.get('[data-test="tr-invite-input"]').setValue('');

  await submitSms(w);

  expect(api.tr_auth_sms_login).toHaveBeenCalledTimes(1);
  expect(api.tr_auth_sms_login).toHaveBeenCalledWith('13800138000', '123456', '');
});
