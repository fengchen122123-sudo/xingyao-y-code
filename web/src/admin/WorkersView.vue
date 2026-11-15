<template>
  <section class="admin-page">
    <header class="page-header admin-heading">
      <div><p class="eyebrow">运行环境</p><h1>工人</h1><p>每个账号一个容器，在这里管理运行状态。</p></div>
      <span class="summary-chip">{{ admin.workers.length }} 个工人</span>
    </header>
    <p class="info-banner">空闲超过三十分钟且没有正在进行的回合时，网关会自动停止容器。</p>

    <div v-if="!admin.workers.length" class="card empty-card"><h2>还没有工人</h2><p class="muted">账号的工人创建后会显示在这里。</p></div>
    <div v-else class="worker-list">
      <article class="card worker-card" v-for="worker in admin.workers" :key="worker.account_id">
        <div class="worker-heading"><span class="section-icon" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01M11 7h6M11 17h6"/></svg></span><div><h2>{{ worker.container_name }}</h2><p>{{ worker.account_id }}</p></div><span class="status-pill" :class="worker.desired_state === 'running' ? 'success' : 'neutral'">{{ worker.desired_state === 'running' ? '运行中' : worker.desired_state }}</span></div>
        <div class="card-actions"><button class="button secondary-button" type="button" @click="admin.controlWorker(worker.account_id, 'start')">启动</button><button class="button secondary-button" type="button" @click="admin.controlWorker(worker.account_id, 'stop')">停止</button><button class="button secondary-button" type="button" @click="admin.controlWorker(worker.account_id, 'restart')">重启</button></div>
      </article>
    </div>
  </section>
</template>
<script setup lang="ts">
import { onMounted } from "vue";
import { useAdminStore } from "../stores/admin";
const admin = useAdminStore();
onMounted(() => admin.loadWorkers());
</script>
