<template>
  <!-- 分组是纯容器：不渲内容、不吃指针语义（拖动/吸附由 M3-T02 在外壳上接管），
       半透明底 + 虚线边由 NodeShell 的 .is-group 提供，这里只留一层浅色面 -->
  <div class="nc-group" data-test="node-content-group" aria-hidden="true"></div>
</template>

<script setup lang="ts">
// GroupNode（M3-T01）：分组容器的展示态。子节点靠 metadata.groupId 归属，
// 位置不嵌套（世界坐标平铺），所以这里不需要 slot / 子渲染。
import type { CanvasNodeData } from '../types';

defineOptions({ name: 'GroupNode' });

defineProps<{ node: CanvasNodeData }>();
</script>

<style scoped>
.nc-group {
  width: 100%;
  height: 100%;
  background: rgba(47, 128, 255, 0.04);
  pointer-events: none;
}
</style>
