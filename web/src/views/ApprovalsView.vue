<template>
  <main class="page">
    <h2>确认</h2>
    <p v-if="!app.approvals.length" class="muted">没有待确认的事</p>
    <PermissionCard
      v-for="item in app.approvals"
      :key="item.id"
      :title="item.title"
      :path="item.path"
      @allow="answer(item.id, true)"
      @deny="answer(item.id, false)"
    />
  </main>
</template>
<script setup lang="ts">
import PermissionCard from "../components/PermissionCard.vue";
import { useAppStore } from "../stores/app";

const app = useAppStore();

async function answer(id: string, allow: boolean) {
  await app.shim?.rpc(allow ? "respond_permission_allow" : "respond_permission_deny", { id });
  app.approvals = app.approvals.filter((item) => item.id !== id);
}
</script>
