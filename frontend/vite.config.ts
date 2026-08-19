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
  },
});
