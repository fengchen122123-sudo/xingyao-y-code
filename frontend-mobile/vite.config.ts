import { fileURLToPath, URL } from 'node:url';
import vue from '@vitejs/plugin-vue';
import { defineConfig } from 'vitest/config';

// 星瑶新版移动端：独立于桌面 frontend/dist 的第二个前端子应用。
// 生产由 core/lan_server.py 在 /m/ 路径下托管（base 必须与之一致）；
// dev 模式直连本机 lan_server 端口调试（LAN_PORT 可用环境变量覆盖）。
const LAN_PORT = process.env.LAN_PORT || '8765';

export default defineConfig({
  base: '/m/',
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    pool: 'threads',
    maxWorkers: 2,
  },
  server: {
    port: 5175,
    strictPort: true,
    proxy: {
      // dev 下 lan_server 尚未启动时这些请求会失败，仅用于联调已启动的桌面端
      '/mobile-shim.js': { target: `http://localhost:${LAN_PORT}`, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 700,
  },
});
