<template>
  <section v-if="visible" class="watch-panel">
    <header class="watch-header">
      <span class="section-icon" aria-hidden="true">
        <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18"/></svg>
      </span>
      <div><h2>围观星瑶操作</h2><p>浏览器实时画面</p></div>
      <span v-if="statusPill" class="status-pill" :class="statusTone"><span class="dot" :class="{ offline: state === 'offline' }"></span>{{ statusPill }}</span>
    </header>
    <p v-if="error" class="muted" role="alert">{{ error }}</p>
    <div v-if="objectUrl" class="watch-screen"><img :src="objectUrl" alt="星瑶正在操作的浏览器实时画面" /></div>
    <p v-else-if="statusHint" class="muted">{{ statusHint }}</p>
    <footer class="watch-footer">
      <span>仅展示当前浏览器画面</span>
      <button class="button danger-button" type="button" @click="$emit('stop')">紧急停止</button>
    </footer>
  </section>
</template>
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { PreviewLoader } from "../transport/preview";
import { useAuthStore } from "../stores/auth";

type DeviceState = "unknown" | "online" | "offline" | "resyncing";

const props = defineProps<{ src: string; state?: DeviceState; reason?: string }>();
defineEmits<{ stop: [] }>();
const auth = useAuthStore();
const objectUrl = ref("");
const error = ref("");
const loader = new PreviewLoader(auth.server, () => auth.credential);

/** 没有画面时也要能说明设备怎么了：离线/整态同步不依赖预览接口。 */
const visible = computed(() => Boolean(props.src || objectUrl.value || error.value || props.state === "offline" || props.state === "resyncing"));
const statusPill = computed(() => {
  if (props.state === "offline") return "设备离线";
  if (props.state === "resyncing") return "正在同步";
  if (objectUrl.value) return "正在围观";
  if (props.state === "online") return "设备在线";
  return "";
});
const statusTone = computed(() => {
  if (props.state === "offline") return "danger";
  if (props.state === "resyncing") return "warning";
  return "success";
});
const reasonLabel = computed(() => {
  switch (props.reason) {
    case "boot": return "设备重启";
    case "gap": return "补帧窗口断裂";
    case "overflow": return "补帧窗口溢出";
    case "queue_drop": return "出站队列丢帧";
    default: return props.reason || "重新对齐状态";
  }
});
const statusHint = computed(() => {
  if (objectUrl.value) return "";
  if (props.state === "offline") return "当前电脑已离线，暂时看不到浏览器画面；重新连上后会自动继续。";
  if (props.state === "resyncing") return `设备正在整态同步（${reasonLabel.value}），画面稍后刷新。`;
  if (props.state === "online") return "电脑在线，等它开始操作浏览器后这里会出现画面。";
  return "";
});

watch(() => props.src, async (src) => {
  error.value = "";
  try {
    objectUrl.value = await loader.load(src);
  } catch (cause) {
    objectUrl.value = loader.url;
    error.value = cause instanceof Error ? cause.message : "预览加载失败";
  }
}, { immediate: true });

onBeforeUnmount(() => loader.dispose());
</script>
