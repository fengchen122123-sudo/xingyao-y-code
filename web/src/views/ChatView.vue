<template>
  <main class="chat-page">
    <header class="chat-header">
      <button class="icon-button mobile-menu" type="button" aria-label="打开会话列表" @click="$emit('open-navigation')">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
      </button>
      <div class="avatar avatar-sm" aria-hidden="true">星</div>
      <div class="chat-heading">
        <h1>{{ app.currentSession || "新对话" }}</h1>
        <p><span class="dot" :class="{ offline: app.status === '未连接' || app.status === '星瑶没有连上' }"></span>{{ app.status }} · {{ app.connectionTarget }}</p>
      </div>
      <label class="model-picker">
        <span class="sr-only">选择模型</span>
        <select v-model="selectedModel" :disabled="!app.models.length || modelBusy || app.modelStatus === 'loading'" @change="changeModel">
          <option v-if="!app.models.length" value="">{{ modelPlaceholder }}</option>
          <option v-for="model in app.models" :key="model.id" :value="model.id">{{ model.name }}</option>
        </select>
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m8 10 4 4 4-4"/></svg>
      </label>
      <button v-if="app.modelStatus === 'error'" class="stop-button" type="button" @click="retryModels">重试模型</button>
      <button class="stop-button" type="button" @click="stop">停止</button>
    </header>

    <div class="chat-stream">
      <div class="chat-column">
        <div v-if="!app.messages.length && !app.previewUrl" class="empty-state">
          <div class="avatar avatar-lg" aria-hidden="true">星</div>
          <p>直接说你要做的事。</p>
        </div>
        <article v-for="message in app.messages" :key="message.id" class="message" :class="message.role">
          <div v-if="message.role !== 'user'" class="avatar avatar-sm" aria-hidden="true">星</div>
          <div class="message-bubble">
            <details v-if="message.thinking" class="thinking">
              <summary>思考过程</summary>
              <p>{{ message.thinking }}</p>
            </details>
            <div v-if="message.role === 'user'" class="message-text">{{ message.text }}</div>
            <div v-else-if="message.text" class="message-text markdown" v-html="render(message.text)"></div>
            <div v-for="tool in message.tools || []" :key="tool.id" class="tool-note">
              <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m14.7 6.3 3 3M4 20l4.5-1 10-10a2.1 2.1 0 0 0-3-3l-10 10L4 20Z"/></svg>
              <span><strong>{{ tool.title }}</strong><small v-if="tool.detail">{{ tool.detail }}</small></span>
            </div>
          </div>
        </article>
        <div id="watch" class="watch-anchor">
          <WatchPanel :src="app.previewUrl" :state="deviceState" :reason="deviceReason" @stop="app.stopWatching()" />
        </div>
      </div>
    </div>

    <form class="composer-dock" @submit.prevent="send">
      <div class="composer">
        <label class="sr-only" for="message-input">发消息给星瑶</label>
        <textarea
          id="message-input"
          ref="input"
          v-model="text"
          rows="1"
          placeholder="发消息给星瑶"
          :disabled="sending"
          @input="resizeInput"
          @keydown.enter.exact.prevent="send"
        ></textarea>
        <button class="send" type="submit" :disabled="!text.trim() || sending" aria-label="发送">
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6"/></svg>
        </button>
      </div>
      <p v-if="error || app.modelError" class="composer-error" role="alert">{{ app.modelError || error }}</p>
    </form>
  </main>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import WatchPanel from "../components/WatchPanel.vue";
import { useAppStore } from "../stores/app";
import { useDeviceStore } from "../stores/devices";
import { renderMd } from "../utils/md";

interface ModelResult { ok?: boolean; msg?: string; active_model_id?: string; active_model_name?: string }

defineEmits<{ "open-navigation": [] }>();

const app = useAppStore();
const devices = useDeviceStore();
const text = ref("");
const input = ref<HTMLTextAreaElement | null>(null);
const sending = ref(false);
const modelBusy = ref(false);
const error = ref("");
const requestedModelId = ref("");
const selectedModel = computed({
  get: () => requestedModelId.value || app.activeModelId,
  set: (value: string) => { requestedModelId.value = value; },
});
// 围观的对象就是正在连接的这台电脑（desktop profile）；没选设备时不传状态。
const currentDevice = computed(() => (app.profile === "desktop" ? devices.devices.find((device) => device.id === app.deviceId) : undefined));
const deviceState = computed(() => currentDevice.value?.state);
const deviceReason = computed(() => currentDevice.value?.reason || "");
const modelPlaceholder = computed(() => {
  if (app.modelStatus === "loading") return "正在加载模型";
  if (app.modelStatus === "error") return "模型加载失败";
  if (app.activeModelName) return app.activeModelName;
  return app.modelStatus === "ready" ? "暂无可用模型" : "选择模型";
});

onMounted(() => {
  // 没有画面时也要能提示「设备离线 / 正在同步」，所以聊天页也拉一次设备列表。
  if (app.profile === "desktop") void devices.load().catch(() => undefined);
  if (app.shim) void app.loadModels();
  else app.connect();
});
watch(() => app.shim, (shim) => {
  requestedModelId.value = "";
  if (shim) void app.loadModels();
});

function render(value: string) {
  return renderMd(value);
}

function resizeInput() {
  if (!input.value) return;
  input.value.style.height = "auto";
  input.value.style.height = `${Math.min(input.value.scrollHeight, 140)}px`;
}

function retryModels() {
  void app.loadModels();
}

async function changeModel() {
  const requested = requestedModelId.value;
  if (!requested || !app.shim) return;
  modelBusy.value = true;
  error.value = "";
  try {
    const result = await app.shim.rpc("set_model", [requested, app.currentSession || undefined]) as ModelResult;
    if (result.ok === false) throw new Error(result.msg || "模型没有切换成功");
    app.activeModelId = String(result.active_model_id || requested);
    app.activeModelName = result.active_model_name || app.models.find((model) => model.id === app.activeModelId)?.name || app.activeModelName || "选择模型";
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "模型没有切换成功";
  } finally {
    requestedModelId.value = "";
    modelBusy.value = false;
  }
}

async function send() {
  const value = text.value.trim();
  if (!value || sending.value) return;
  sending.value = true;
  error.value = "";
  try {
    await app.send(value);
    text.value = "";
    await nextTick();
    resizeInput();
    input.value?.focus();
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "消息没有发出去，请再试一次。";
  } finally {
    sending.value = false;
  }
}

async function stop() {
  try {
    await app.stop();
  } catch {
    error.value = "暂时无法停止，请检查连接。";
  }
}
</script>
