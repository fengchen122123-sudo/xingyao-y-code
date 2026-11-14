<template>
  <main class="page">
    <section class="card">
      <h2>连接</h2>
      <p>{{ auth.server || "还没有服务器" }}</p>
      <p class="muted">{{ app.status }}</p>
    </section>
    <section class="card">
      <h2>我的电脑</h2>
      <p v-if="!devices.devices.length" class="muted">还没有配对的电脑</p>
      <p v-for="device in devices.devices" :key="device.id">
        <button type="button" class="text-button" :disabled="!device.online" @click="useDesktop(device.id)">
          {{ device.name }} · {{ device.online ? "在线" : "电脑上的星瑶未运行" }}
        </button>
      </p>
    </section>
    <button class="text-button" type="button" @click="leave">退出</button>
  </main>
</template>
<script setup lang="ts">
import { onMounted } from "vue";
import { useRouter } from "vue-router";
import { useAppStore } from "../stores/app";
import { useAuthStore } from "../stores/auth";
import { useDeviceStore } from "../stores/devices";

const auth = useAuthStore();
const app = useAppStore();
const devices = useDeviceStore();
const router = useRouter();
onMounted(() => devices.load().catch(() => undefined));

function useDesktop(deviceId: string) {
  app.disconnect();
  app.connectDesktop(deviceId);
}

async function leave() {
  app.disconnect();
  await auth.logout();
  await router.push("/login");
}
</script>
