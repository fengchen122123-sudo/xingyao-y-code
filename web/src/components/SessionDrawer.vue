<template>
  <aside class="session-sidebar" :class="{ open }" aria-label="对话导航">
    <div class="sidebar-head">
      <router-link class="brand" to="/chat" aria-label="星瑶首页" @click="$emit('close')">
        <span class="avatar avatar-sm">星</span>
        <span>星瑶</span>
      </router-link>
      <button class="icon-button sidebar-close" type="button" aria-label="关闭会话列表" @click="$emit('close')">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>
      </button>
    </div>

    <button class="new-chat" type="button" :disabled="busy" @click="$emit('new')">
      <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
      新对话
    </button>
    <p class="sidebar-label">最近</p>
    <nav class="session-list" aria-label="会话">
      <p v-if="!sessions.length" class="sidebar-empty">还没有会话，直接说你要做的事。</p>
      <button
        v-for="session in sessions"
        :key="session.name"
        class="session-button"
        :class="{ active: session.name === current }"
        type="button"
        :disabled="busy"
        :aria-current="session.name === current ? 'page' : undefined"
        @click="$emit('select', session.name)"
      >
        <span class="session-topline">
          <strong>{{ session.name }}</strong>
          <span v-if="session.name === current">当前</span>
        </span>
        <small>{{ session.name === current ? preview : "打开这段对话" }}</small>
      </button>
    </nav>

    <div class="sidebar-foot">
      <router-link class="sidebar-link" to="/approvals" @click="$emit('close')">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>
        <span>待你确认</span>
      </router-link>
      <router-link class="sidebar-link" :to="{ path: '/chat', hash: '#watch' }" @click="$emit('close')">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18"/></svg>
        <span>围观操作</span>
      </router-link>
      <router-link class="sidebar-link" to="/settings" @click="$emit('close')">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="11" rx="2"/><path d="M2 19h20"/></svg>
        <span>我的电脑</span>
      </router-link>
      <router-link v-if="auth.role === 'admin'" class="sidebar-link" to="/admin/accounts" @click="$emit('close')">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20v-2a4 4 0 0 1 4-4h8a4 4 0 0 1 4 4v2M9 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7-1a3 3 0 0 0 0-6"/></svg>
        <span>管理后台</span>
      </router-link>
      <router-link class="account-link" to="/settings" @click="$emit('close')">
        <span class="avatar avatar-sm">{{ initial }}</span>
        <span class="account-copy"><strong>{{ auth.accountId || "我的账号" }}</strong><small>连接与设置</small></span>
      </router-link>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useAuthStore } from "../stores/auth";

defineProps<{
  open: boolean;
  sessions: { name: string }[];
  current: string;
  preview: string;
  busy: boolean;
}>();
defineEmits<{ select: [name: string]; new: []; close: [] }>();

const auth = useAuthStore();
const initial = computed(() => auth.accountId.trim().slice(0, 1) || "我");
</script>
