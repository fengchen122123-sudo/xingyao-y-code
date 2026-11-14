<template>
  <main class="page">
    <form class="card login" @submit.prevent="submit">
      <div class="avatar">星</div>
      <h2>登录星瑶</h2>
      <p class="muted">账号把对话分开。登录之后由网关接通你的星瑶，浏览器不保存工人令牌。</p>
      <label>服务器<input v-model="server" required placeholder="https://example.com" /></label>
      <label>用户名<input v-model="username" required /></label>
      <label>口令<input v-model="password" type="password" required /></label>
      <p v-if="error" class="muted">{{ error }}</p>
      <div class="row">
        <button class="send" type="submit">进入</button>
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
const server = ref(auth.server);
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
