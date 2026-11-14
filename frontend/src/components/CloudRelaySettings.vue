<template>
  <section class="settings-section" data-test="cloud-relay-settings">
    <h3 class="section-title">云中转</h3>
    <p class="field-hint">默认关闭。打开后，这台电脑会拿设备令牌主动连上你的服务器，手机会把操作转过来。令牌写入时会加密，不会出现在地址里。</p>
    <label class="field">
      <span>启用</span>
      <input type="checkbox" v-model="enabled" />
    </label>
    <label class="field">
      <span>网关地址</span>
      <input v-model="url" placeholder="wss://example.com/relay/device" />
    </label>
    <label class="field">
      <span>设备令牌</span>
      <input v-model="token" type="password" placeholder="只在需要更换时粘贴" />
    </label>
    <p class="field-hint">状态：{{ status }}</p>
    <div class="row-actions">
      <button type="button" @click="save">保存</button>
      <button type="button" @click="reconnect">重新连接</button>
    </div>
    <p v-if="message" class="field-hint">{{ message }}</p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue';

const enabled = ref(false);
const url = ref('');
const token = ref('');
const status = ref('未连接');
const message = ref('');

type BridgeApi = {
  get_cloud_relay_settings?: () => Promise<{ ok?: boolean; settings?: { enabled?: boolean; url?: string; state?: string } }>;
  save_cloud_relay_settings?: (payload: { enabled: boolean; url: string; token: string }) => Promise<{ ok?: boolean; msg?: string; settings?: { state?: string } }>;
};

function api(): BridgeApi | undefined {
  return (window as unknown as { pywebview?: { api?: BridgeApi } }).pywebview?.api;
}

onMounted(async () => {
  const result = await api()?.get_cloud_relay_settings?.();
  const settings = result?.settings;
  if (!settings) return;
  enabled.value = Boolean(settings.enabled);
  url.value = settings.url || '';
  status.value = settings.state || '未连接';
});

async function save() {
  const result = await api()?.save_cloud_relay_settings?.({
    enabled: enabled.value,
    url: url.value,
    token: token.value,
  });
  message.value = result?.ok ? '已保存' : (result?.msg || '保存失败');
  token.value = '';
  status.value = result?.settings?.state || status.value;
}

async function reconnect() {
  await save();
}
</script>
