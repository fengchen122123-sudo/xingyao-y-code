<template>
  <section class="admin-page">
    <header class="page-header admin-heading">
      <div><p class="eyebrow">设备接入</p><h1>设备</h1><p>为账号配对电脑，或轮换、吊销设备令牌。</p></div>
      <span class="summary-chip">{{ admin.devices.length }} 台设备</span>
    </header>

    <div v-if="admin.freshToken" class="token-notice" role="status"><strong>请立即保存新令牌</strong><p>它只完整显示这一次。</p><code>{{ admin.freshToken }}</code></div>

    <details class="card create-panel">
      <summary><span><strong>配对一台设备</strong><small>为账号生成一次性设备令牌</small></span><span class="summary-action">添加</span></summary>
      <form class="form-grid two-columns" @submit.prevent="create">
        <label class="field"><span>账号编号</span><input v-model="accountId" required placeholder="账号 ID" /></label>
        <label class="field"><span>设备名称</span><input v-model="name" required placeholder="家里的台式机" /></label>
        <div class="form-actions"><button class="button primary-button" type="submit">生成令牌</button></div>
      </form>
    </details>

    <div v-if="!admin.devices.length" class="card empty-card"><h2>还没有设备</h2><p class="muted">配对后的设备会显示在这里。</p></div>
    <div v-else class="data-card"><div class="data-table-wrap"><table class="data-table">
      <thead><tr><th>设备</th><th>所属账号</th><th>状态</th><th class="align-right">操作</th></tr></thead>
      <tbody><tr v-for="device in admin.devices" :key="device.id">
        <td><div class="entity-name"><span class="device-icon small-device-icon"><svg class="icon" viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="11" rx="2"/><path d="M2 19h20"/></svg></span><div><strong>{{ device.name }}</strong><small>{{ device.id }}</small></div></div></td>
        <td>{{ device.account_id }}</td>
        <td><span class="status-pill" :class="device.revoked ? 'neutral' : 'success'">{{ device.revoked ? "已吊销" : "可用" }}</span></td>
        <td><div v-if="!device.revoked" class="table-actions"><button class="button secondary-button small-button" type="button" @click="admin.rotateDevice(device.id)">轮换</button><button class="button quiet-danger-button small-button" type="button" @click="admin.revokeDevice(device.id)">吊销</button></div><span v-else class="align-right muted">—</span></td>
      </tr></tbody>
    </table></div></div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useAdminStore } from "../stores/admin";
const admin = useAdminStore();
const accountId = ref("");
const name = ref("");
onMounted(() => admin.loadDevices());

async function create() {
  await admin.createDevice(accountId.value, name.value);
  name.value = "";
}
</script>
