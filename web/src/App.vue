<template>
  <div class="shell">
    <header class="top">
      <div class="avatar">星</div>
      <div>
        <strong>星瑶</strong>
        <p class="muted">个人助手</p>
      </div>
      <nav class="nav" v-if="auth.signedIn">
        <router-link to="/chat">对话</router-link>
        <router-link to="/approvals">确认</router-link>
        <router-link to="/settings">设置</router-link>
        <router-link v-if="auth.role === 'admin'" to="/admin/accounts">管理</router-link>
      </nav>
    </header>
    <router-view />
  </div>
</template>
<script setup lang="ts">
import { useAuthStore } from "./stores/auth";
const auth = useAuthStore();
</script>
