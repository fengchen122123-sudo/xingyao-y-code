<template>
  <main class="page login-page">
    <form class="card login-card" @submit.prevent="submit">
      <div class="avatar avatar-lg login-avatar" aria-hidden="true">星</div>
      <p class="eyebrow">你的个人助手</p>
      <h1>登录星瑶</h1>
      <p class="login-intro">用账号把对话分开，从任何设备继续。</p>

      <label class="field">
        <span>服务器</span>
        <input v-model="server" name="server" required inputmode="url" autocomplete="url" placeholder="http://127.0.0.1:18080" />
      </label>
      <label class="field">
        <span>用户名</span>
        <input v-model="username" name="username" required autocomplete="username" placeholder="你的账号" />
      </label>
      <label class="field">
        <span>口令</span>
        <input v-model="password" name="password" type="password" required autocomplete="current-password" placeholder="输入口令" />
      </label>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <button class="button primary-button login-submit" type="submit">进入星瑶</button>
      <p class="login-note">登录后由网关接通你的星瑶，浏览器不保存工人令牌。</p>
    </form>
  </main>
</template>
<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const router = useRouter();
const server = ref(auth.server || defaultServer());

function defaultServer(): string {
  const native = (window as unknown as { Capacitor?: { isNativePlatform?: () => boolean } }).Capacitor;
  if (native?.isNativePlatform?.()) return "";
  if (!import.meta.env.PROD) return "";
  if (window.location.protocol !== "http:" && window.location.protocol !== "https:") return "";
  return window.location.origin;
}
const username = ref("");
const password = ref("");
const error = ref("");

async function submit() {
  error.value = "";
  try {
    await auth.login(server.value, username.value, password.value);
    await router.push("/chat");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "登录没有通过";
  }
}
</script>
