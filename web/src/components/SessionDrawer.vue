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
      <div
        v-for="session in sessions"
        :key="session.name"
        class="session-item"
        :class="{ active: session.name === current }"
      >
        <button
          class="session-button"
          type="button"
          :disabled="busy"
          :aria-current="session.name === current ? 'page' : undefined"
          @click="$emit('select', session.name)"
        >
          <span class="session-topline">
            <strong>{{ session.name }}</strong>
            <span v-if="session.name === current && pendingDelete !== session.name">当前</span>
          </span>
          <small>{{ session.name === current ? preview : "打开这段对话" }}</small>
        </button>
        <template v-if="!busy">
          <button
            v-if="pendingDelete !== session.name"
            class="session-delete"
            type="button"
            :aria-label="`删除会话 ${session.name}`"
            :title="`删除会话 ${session.name}`"
            @click.stop="pendingDelete = session.name"
          >
            <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg>
          </button>
          <span v-else class="session-confirm">
            <button class="confirm-yes" type="button" @click.stop="confirmDelete(session.name)">删除</button>
            <button class="confirm-no" type="button" @click.stop="pendingDelete = ''">取消</button>
          </span>
        </template>
      </div>
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
import { computed, ref, watch } from "vue";
import { useAuthStore } from "../stores/auth";

const props = defineProps<{
  open: boolean;
  sessions: { name: string }[];
  current: string;
  preview: string;
  busy: boolean;
}>();
const emit = defineEmits<{ select: [name: string]; new: []; close: []; delete: [name: string] }>();

const auth = useAuthStore();
const initial = computed(() => auth.accountId.trim().slice(0, 1) || "我");
/** 正在等待二次确认的会话名：一次只确认一条，避免多个确认条同屏。 */
const pendingDelete = ref("");

// 切走会话或列表刷新后，残留的确认条会指着一个已经不在列表里的名字。
watch(() => props.current, () => { pendingDelete.value = ""; });
watch(() => props.sessions, () => {
  if (pendingDelete.value && !props.sessions.some((session) => session.name === pendingDelete.value)) {
    pendingDelete.value = "";
  }
});

function confirmDelete(name: string) {
  pendingDelete.value = "";
  emit("delete", name);
}
</script>
