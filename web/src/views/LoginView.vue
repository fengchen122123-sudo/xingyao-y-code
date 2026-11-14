<template>
  <main class="page">
    <form class="card login" @submit.prevent="submit">
      <div class="avatar">星</div>
      <h2>登录星瑶</h2>
      <p class="muted">账号把对话分开。直连工人时，这一枚令牌就是工人令牌，只放在这次浏览器会话里。</p>
      <label>服务器<input v-model="server" required placeholder="https://example.com" /></label>
      <template v-if="mode === 'gateway'">
        <label>用户名<input v-model="username" required /></label>
        <label>口令<input v-model="password" type="password" required /></label>
      </template>
      <label v-else>工人令牌<input v-model="token" required /></label>
      <p v-if="error" class="muted">{{ error }}</p>
      <div class="row">
        <button class="send" type="submit">进入</button>
        <button class="text-button" type="button" @click="mode = mode === 'gateway' ? 'worker' : 'gateway'">
          {{ mode === "gateway" ? "改用直连" : "改用账号" }}
        </button>
      </div>
    </form>
  </main>
</template>
<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "../stores/auth";

const auth = useAuthStore();
const router = useRouter();
const mode = ref<"gateway" | "worker">("gateway");
const server = ref(auth.server);
const username = ref("");
const password = ref("");
const token = ref("");
const error = ref("");

async function submit() {
  error.value = "";
  try {
    if (mode.value === "gateway") await auth.login(server.value, username.value, password.value);
    else auth.connectWorker(server.value, token.value);
    await router.push("/chat");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "登录没有通过";
  }
}
</script>
