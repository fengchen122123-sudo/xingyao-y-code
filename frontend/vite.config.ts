import { defineConfig } from 'vitest/config';
import vue from '@vitejs/plugin-vue';

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
    chunkSizeWarningLimit: 700,
    // 入口主包曾达 1.37 MB：Vue 运行时与 xterm 全部随入口静态打入，而 xterm 只有
    // 终端面板使用。按依赖分组拆出 vendor 块，再用全局 maxSize 兜底，保证任何
    // 输出块都落在 700 kB 阈值以内。
    // tags: ['$initial'] 限定只捕获入口静态依赖——mermaid/pdfjs 等是按需动态
    // import 懒加载的，不能被并回首屏块，否则首屏白拉几百 KB。
    rolldownOptions: {
      output: {
        codeSplitting: {
          maxSize: 600 * 1024,
          groups: [
            {
              name: 'vendor-vue',
              test: /node_modules[\\/](@vue|vue|pinia)[\\/]/,
              priority: 40,
              tags: ['$initial' as const],
            },
            {
              name: 'vendor-xterm',
              test: /node_modules[\\/]@xterm[\\/]/,
              priority: 40,
              tags: ['$initial' as const],
            },
            {
              name: 'vendor',
              test: /node_modules[\\/]/,
              priority: 10,
              minSize: 20 * 1024,
              tags: ['$initial' as const],
            },
            {
              // 应用自身代码（组件/store/工具）体量已接近 2 MB 源码，单块压缩后仍
              // 超过 700 kB 阈值。按 800 kB 源码上限切块，让入口包回到阈值内；
              // 这些仍是入口的静态依赖，由打包器保证执行顺序，运行语义不变。
              name: 'app',
              test: (id: string) => !id.includes('node_modules') && /[\\/]src[\\/]/.test(id),
              priority: 5,
              maxSize: 800 * 1024,
            },
          ],
        },
      },
    },
  },
});
