import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [
    vue(),
    tailwindcss(),
  ],
  base: '/dashboard/',
  server: {
    port: 5173,
    proxy: {
      '/admin': {
        target: 'http://127.0.0.1:8650',
        changeOrigin: true,
      },
      '/v1': {
        target: 'http://127.0.0.1:8650',
        changeOrigin: true,
      }
    }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  }
})
