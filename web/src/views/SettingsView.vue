<template>
  <main class="page page-narrow settings-page">
    <header class="page-header">
      <div>
        <p class="eyebrow">账号与连接</p>
        <h1>我的</h1>
        <p>查看当前连接，或给这台星瑶发消息的电脑。</p>
      </div>
    </header>

    <section class="card profile-card">
      <span class="avatar avatar-lg" aria-hidden="true">{{ initial }}</span>
      <div class="profile-copy">
        <h2>{{ auth.accountId || "我的账号" }}</h2>
        <p><span class="dot" :class="{ offline: app.status === '未连接' || app.status === '星瑶没有连上' }"></span>{{ app.status }}</p>
      </div>
      <span class="role-label">{{ auth.role === "admin" ? "管理员" : "个人账号" }}</span>
    </section>

    <section class="card settings-section">
      <div class="section-heading compact">
        <span class="section-icon" aria-hidden="true">
          <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18"/></svg>
        </span>
        <div><h2>连接</h2><p>服务器只负责接通你的星瑶。</p></div>
      </div>
      <dl class="detail-list">
        <div><dt>服务器</dt><dd>{{ auth.server || "还没有服务器" }}</dd></div>
        <div><dt>模型服务</dt><dd>由管理员统一提供，无需填写 Key</dd></div>
      </dl>
    </section>

    <section class="settings-section device-section">
      <div class="section-heading">
        <div><h2>我的电脑</h2><p>选择一台在线电脑，把接下来的消息转发过去。</p></div>
      </div>
      <div v-if="!devices.devices.length" class="card empty-card small-empty">
        <span class="section-icon" aria-hidden="true">
          <svg class="icon" viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="11" rx="2"/><path d="M2 19h20"/></svg>
        </span>
        <h2>还没有配对的电脑</h2>
        <p class="muted">配对完成后，在线设备会出现在这里。</p>
      </div>
      <div v-else class="device-list">
        <button
          v-for="device in devices.devices"
          :key="device.id"
          type="button"
          class="device-card"
          :class="{ offline: !device.online }"
          :disabled="!device.online"
          @click="useDesktop(device.id)"
        >
          <span class="device-icon" aria-hidden="true">
            <svg class="icon" viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="11" rx="2"/><path d="M2 19h20"/></svg>
          </span>
          <span class="device-copy"><strong>{{ device.name }}</strong><small>{{ device.online ? "在线 · 可以连接" : "电脑上的星瑶未运行" }}</small></span>
          <span class="status-pill" :class="device.online ? 'success' : 'neutral'">{{ device.online ? "在线" : "离线" }}</span>
          <svg v-if="device.online" class="icon device-arrow" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>
        </button>
      </div>
    </section>

    <div class="account-actions">
      <router-link v-if="auth.role === 'admin'" class="button secondary-button" to="/admin/accounts">管理后台</router-link>
      <button class="button quiet-danger-button" type="button" @click="leave">退出登录</button>
    </div>
  </main>
</template>
<script setup lang="ts">
import { computed, onMounted } from "vue";
import { useRouter } from "vue-router";
import { useAppStore } from "../stores/app";
import { useAuthStore } from "../stores/auth";
import { useDeviceStore } from "../stores/devices";

const auth = useAuthStore();
const app = useAppStore();
const devices = useDeviceStore();
const router = useRouter();
const initial = computed(() => auth.accountId.trim().slice(0, 1) || "我");
onMounted(() => devices.load().catch(() => undefined));

function useDesktop(deviceId: string) {
  app.disconnect();
  app.connectDesktop(deviceId);
}

async function leave() {
  app.disconnect();
  await auth.logout();
  await router.push("/login");
}
</script>
