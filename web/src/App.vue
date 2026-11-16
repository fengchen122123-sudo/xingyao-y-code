<template>
  <div class="app-shell">
    <a class="skip-link" href="#main-content">跳到主要内容</a>
    <SessionDrawer
      v-if="auth.signedIn && !remoteMode"
      :open="navigationOpen"
      :sessions="app.sessions"
      :current="app.currentSession"
      :preview="currentPreview"
      :busy="sessionBusy"
      @select="chooseSession"
      @new="newSession"
      @delete="deleteSession"
      @close="navigationOpen = false"
    />
    <button
      v-if="auth.signedIn && navigationOpen"
      class="drawer-scrim"
      type="button"
      aria-label="关闭会话列表"
      @click="navigationOpen = false"
    ></button>

    <div id="main-content" class="app-main" :class="{ remote: remoteMode }" tabindex="-1">
      <p v-if="actionError" class="action-error" role="alert">{{ actionError }}</p>
      <router-view @open-navigation="navigationOpen = true" />
    </div>

    <nav v-if="auth.signedIn && !remoteMode" class="mobile-nav" aria-label="主要导航">
      <router-link to="/chat">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 15a3 3 0 0 1-3 3H9l-5 3v-6a3 3 0 0 1-1-2V7a3 3 0 0 1 3-3h11a3 3 0 0 1 3 3Z"/></svg>
        <span>对话</span>
      </router-link>
      <router-link to="/approvals">
        <span class="mobile-icon-wrap">
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>
          <i v-if="app.approvals.length" class="nav-badge">{{ app.approvals.length }}</i>
        </span>
        <span>确认</span>
      </router-link>
      <router-link :to="{ path: '/chat', hash: '#watch' }">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18"/></svg>
        <span>围观</span>
      </router-link>
      <router-link to="/settings">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="11" rx="2"/><path d="M2 19h20"/></svg>
        <span>设备</span>
      </router-link>
      <router-link v-if="auth.role === 'admin'" to="/admin/accounts">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/></svg>
        <span>管理</span>
      </router-link>
      <router-link v-else to="/settings">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/></svg>
        <span>我的</span>
      </router-link>
    </nav>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import SessionDrawer from "./components/SessionDrawer.vue";
import { useAppStore } from "./stores/app";
import { useAuthStore } from "./stores/auth";

const auth = useAuthStore();
const app = useAppStore();
const route = useRoute();
const router = useRouter();
const navigationOpen = ref(false);
const sessionBusy = ref(false);
const actionError = ref("");
const remoteMode = computed(() => Boolean(route.meta.remote));
const currentPreview = computed(() => {
  const last = [...app.messages].reverse().find((message) => message.text.trim());
  return last?.text.replace(/\s+/g, " ").slice(0, 42) || "直接说你要做的事";
});

watch(() => route.fullPath, () => {
  navigationOpen.value = false;
  actionError.value = "";
});

async function chooseSession(name: string) {
  if (sessionBusy.value) return;
  sessionBusy.value = true;
  actionError.value = "";
  try {
    await app.chooseSession(name);
    await router.push("/chat");
    navigationOpen.value = false;
  } catch {
    actionError.value = "会话没有切换成功，请再试一次。";
  } finally {
    sessionBusy.value = false;
  }
}

async function newSession() {
  if (sessionBusy.value) return;
  sessionBusy.value = true;
  actionError.value = "";
  try {
    await app.newSession();
    await router.push("/chat");
    navigationOpen.value = false;
  } catch (cause) {
    actionError.value = cause instanceof Error ? cause.message : "新对话创建失败，请再试一次。";
  } finally {
    sessionBusy.value = false;
  }
}

async function deleteSession(name: string) {
  if (sessionBusy.value) return;
  sessionBusy.value = true;
  actionError.value = "";
  try {
    await app.deleteSession(name);
    // 删的是当前会话时后端会切到新建的空会话，留在对话页即可；删别的会话不动路由。
    await router.push("/chat");
  } catch (cause) {
    actionError.value = cause instanceof Error ? cause.message : "会话没有删除成功，请再试一次。";
  } finally {
    sessionBusy.value = false;
  }
}
</script>
