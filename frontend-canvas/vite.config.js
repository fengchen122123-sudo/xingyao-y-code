import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 产物由 iframe 以相对路径加载（file:// 或后端子路径），base 必须为 './'
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
