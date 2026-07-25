<template>
  <div id="subpage" :class="{ show: visible }">
    <div class="sub-head">
      <button class="sub-back" @click="emit('close')">← 返回</button>
      <h2>{{ title }}</h2>
      <span class="sub-desc">{{ desc }}</span>
    </div>

    <div class="sub-body">
      <div class="sub-grid">
        <template v-if="subType === 'skills'">
          <div v-for="s in store.skills" :key="s.id || s.name" class="sub-card">
            <div class="sc-top">
              <div class="sc-ico">⚡</div>
              <div class="sc-name">{{ s.name || s.id }}</div>
              <span class="sc-scope">Skill</span>
            </div>
            <div class="sc-desc">{{ s.description || '项目/用户自定义扩展技能' }}</div>
          </div>
        </template>

        <template v-else>
          <div v-for="m in store.mcps" :key="m.name" class="sub-card mcp-card">
            <div class="sc-top">
              <div class="sc-ico">🔌</div>
              <div class="sc-name">{{ m.name }}</div>
              <span class="mcp-status" style="margin-left:auto;font-size:11px;color:var(--green)">● 在线</span>
            </div>
            <div class="sc-desc">{{ m.description || 'MCP 标准工具接口服务' }}</div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useAppStore } from '../stores/appStore';

const props = defineProps<{
  visible: boolean;
  subType: 'skills' | 'mcp';
}>();

const emit = defineEmits(['close']);
const store = useAppStore();

const title = computed(() => (props.subType === 'skills' ? '技能 Skills' : 'MCP 服务'));
const desc = computed(() => (props.subType === 'skills' ? '已扫描的项目技能与用户扩展技能' : '来自 mcp.json 的服务接口'));
</script>

<style scoped>
#subpage {
  position: absolute;
  inset: 0;
  background: var(--bg0);
  z-index: 50;
  display: none;
  flex-direction: column;
}
#subpage.show {
  display: flex;
}
.sub-head {
  height: 52px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 20px;
  gap: 12px;
  background: var(--bg1);
  flex-shrink: 0;
}
.sub-back {
  background: var(--bg2);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 8px;
  padding: 6px 14px;
  font-size: 12.5px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.sub-back:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.sub-head h2 {
  font-size: 15px;
}
.sub-head .sub-desc {
  font-size: 12px;
  color: var(--text-faint);
}
.sub-body {
  flex: 1;
  overflow-y: auto;
  padding: 22px 26px;
}
.sub-grid {
  max-width: 960px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
}
.sub-card {
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
  transition: 0.15s;
  cursor: pointer;
}
.sub-card:hover {
  border-color: var(--accent);
  transform: translateY(-2px);
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.25);
}
.sub-card .sc-top {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.sub-card .sc-ico {
  width: 34px;
  height: 34px;
  border-radius: 99px;
  background: linear-gradient(135deg, var(--bg3), var(--bg2));
  border: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
}
.sub-card .sc-name {
  font-size: 13.5px;
  font-weight: 600;
}
.sub-card .sc-scope {
  font-size: 10.5px;
  color: var(--text-faint);
  background: var(--bg3);
  border-radius: 7px;
  padding: 1px 7px;
  margin-left: auto;
}
.sub-card .sc-desc {
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.6;
  min-height: 38px;
}
</style>
