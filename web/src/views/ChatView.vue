<template>
  <main class="page">
    <SessionDrawer :open="drawer" :sessions="app.sessions" />
    <div class="stream">
      <p class="muted"><span class="dot"></span> {{ app.status }}</p>
      <article v-for="message in app.messages" :key="message.id" class="bubble" :class="message.role">
        <div class="avatar" v-if="message.role === 'agent'">星</div>
        <p>{{ message.text }}</p>
      </article>
      <WatchPanel :src="app.previewUrl" />
    </div>
    <form class="composer" @submit.prevent="send">
      <button class="text-button" type="button" @click="drawer = !drawer">会话</button>
      <input v-model="text" placeholder="跟星瑶说" />
      <button class="send" type="submit">送</button>
    </form>
  </main>
</template>
<script setup lang="ts">
import { onMounted, ref } from "vue";
import SessionDrawer from "../components/SessionDrawer.vue";
import WatchPanel from "../components/WatchPanel.vue";
import { useAppStore } from "../stores/app";

const app = useAppStore();
const text = ref("");
const drawer = ref(false);
onMounted(() => app.connect());

async function send() {
  const value = text.value.trim();
  if (!value) return;
  text.value = "";
  await app.send(value);
}
</script>
