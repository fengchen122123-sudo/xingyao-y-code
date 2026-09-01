import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    // Windows 上默认 forks pool 的 worker 启动经常超时（jsdom 环境冷启动慢），
    // threads pool 稳定；限制并发 worker 数避免资源争抢。
    // Vitest 4 起原 poolOptions.threads 的选项平铺为 test 顶层选项
    //（minWorkers 已移除，按需启动保底 1 个 worker）
    pool: 'threads',
    maxWorkers: 2,
  },
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // 主包经 RightPanel 懒加载 hljs 拆分后已无超限同步依赖；剩余唯一超过
    // 500 kB 的 chunk 是动态 import 的 mermaid 包（约 663 kB，出现图表才
    // 按需下载，不影响首屏），属预期体积，阈值调至 700 关闭噪音告警。
    chunkSizeWarningLimit: 700,
  },
});
