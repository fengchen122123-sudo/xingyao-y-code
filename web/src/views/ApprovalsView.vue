<template>
  <main class="page page-narrow">
    <header class="page-header">
      <div>
        <p class="eyebrow">由你决定</p>
        <h1>待你确认<span v-if="app.approvals.length" class="count-badge">{{ app.approvals.length }}</span></h1>
        <p>星瑶会在继续操作前等你答复。</p>
      </div>
    </header>

    <p v-if="error" class="form-error" role="alert">{{ error }}</p>

    <section v-if="!app.approvals.length" class="card empty-card">
      <span class="empty-icon" aria-hidden="true">
        <svg class="icon" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></svg>
      </span>
      <h2>都处理好了</h2>
      <p class="muted">没有待确认的事。需要时，星瑶会在这里等你。</p>
      <router-link class="button secondary-button" to="/chat">回到对话</router-link>
    </section>
    <div v-else class="approval-list">
      <PermissionCard
        v-for="item in app.approvals"
        :key="item.id"
        :title="item.title"
        :path="item.path"
        @allow="answer(item.id, true)"
        @deny="answer(item.id, false)"
      />
    </div>
  </main>
</template>
<script setup lang="ts">
import { onMounted, ref } from "vue";
import PermissionCard from "../components/PermissionCard.vue";
import { useAppStore } from "../stores/app";

const app = useAppStore();
const error = ref("");

onMounted(() => void app.syncApprovals());

async function answer(id: string, allow: boolean) {
  error.value = "";
  try {
    await app.respondApproval(id, allow);
  } catch (cause) {
    // 服务端拒绝（已失效或已在别处处理）时卡片留着，并把原因说出来。
    error.value = cause instanceof Error ? cause.message : "这次确认没有送达，请再试一次";
    await app.syncApprovals();
  }
}
</script>
