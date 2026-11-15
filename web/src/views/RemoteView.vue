<template>
  <main class="remote-page">
    <p v-if="error" class="action-error" role="alert">{{ error }}</p>
    <ChatView v-else-if="ready" />
    <div v-else class="remote-loading">
      <span class="avatar avatar-lg" aria-hidden="true">星</span>
      <p>正在连接这台电脑…</p>
    </div>
  </main>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import ChatView from "./ChatView.vue";
import { useAppStore } from "../stores/app";
import { useAuthStore } from "../stores/auth";

const route = useRoute();
const auth = useAuthStore();
const app = useAppStore();
const ready = ref(false);
const error = ref("");

function payloadOf(token: string): Record<string, unknown> {
  const part = token.split(".")[1] || "";
  const normalized = part.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
  return JSON.parse(atob(padded)) as Record<string, unknown>;
}

onMounted(async () => {
  const token = String(route.query.token || "");
  const deviceId = String(route.params.deviceId || "");
  if (!token || !deviceId) {
    error.value = "远端链接缺少令牌或设备编号。";
    return;
  }
  try {
    const payload = payloadOf(token);
    const expires = Number(payload.exp || 0);
    if (expires && expires * 1000 <= Date.now()) {
      error.value = "远端链接已过期，请回到“我的电脑”重新打开。";
      return;
    }
    auth.server = window.location.origin;
    auth.remote = true;
    auth.credential = token;
    auth.accountId = String(payload.sub || "");
    auth.role = String(payload.role || "user");
    app.connectDesktop(deviceId);
    ready.value = true;
  } catch {
    error.value = "远端链接无效，请回到“我的电脑”重新打开。";
  }
});
</script>

<style scoped>
.remote-page {
  min-height: 100vh;
  background: var(--bg);
}
.remote-loading {
  min-height: 60vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  color: var(--muted);
}
</style>
