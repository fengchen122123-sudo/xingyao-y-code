<template>
  <section>
    <h2>设备</h2>
    <form class="card" @submit.prevent="create">
      <label>账号编号<input v-model="accountId" required /></label>
      <label>名称<input v-model="name" required placeholder="家里的台式机" /></label>
      <button class="send" type="submit">生成令牌</button>
      <p v-if="admin.freshToken" class="muted">这枚令牌只出现这一次：{{ admin.freshToken }}</p>
    </form>
    <article class="card" v-for="device in admin.devices" :key="device.id">
      <strong>{{ device.name }}</strong>
      <p class="muted">{{ device.revoked ? "已吊销" : "可用" }}</p>
      <button v-if="!device.revoked" class="text-button" type="button" @click="admin.revokeDevice(device.id)">吊销</button>
    </article>
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
