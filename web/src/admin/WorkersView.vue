<template>
  <section>
    <h2>工人</h2>
    <p class="muted">每个账号一个容器。空闲超过三十分钟且没有正在进行的回合时，网关会停掉它。</p>
    <article class="card" v-for="worker in admin.workers" :key="worker.account_id">
      <strong>{{ worker.container_name }}</strong>
      <p class="muted">{{ worker.desired_state }}</p>
      <button class="text-button" type="button" @click="admin.controlWorker(worker.account_id, 'start')">启动</button>
      <button class="text-button" type="button" @click="admin.controlWorker(worker.account_id, 'stop')">停止</button>
    </article>
  </section>
</template>
<script setup lang="ts">
import { onMounted } from "vue";
import { useAdminStore } from "../stores/admin";
const admin = useAdminStore();
onMounted(() => admin.loadWorkers());
</script>
