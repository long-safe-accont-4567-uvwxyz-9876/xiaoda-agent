import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/',
  build: {
    outDir: '../dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // 审计低 L5：主包 801KB 里绝大部分是「首屏必需但极少变动」的框架依赖
        // （vue/pinia/vue-router/i18n、naive-ui）。按库切块后主包只留应用代码，
        // 升级业务代码不会让整个 vendor 缓存失效；three / echarts 已由各视图的
        // 动态 import 自动切出（实测 manualChunks 对它们零收益）。
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('/naive-ui/') || id.includes('/vueuc/')) return 'vendor-naive'
          if (id.includes('/three/')) return 'vendor-three'
          if (id.includes('/echarts/') || id.includes('/zrender/')) return 'vendor-echarts'
          if (id.includes('/vue/') || id.includes('/pinia/') || id.includes('/vue-router/')
            || id.includes('/vue-i18n/') || id.includes('/@intlify/')) return 'vendor-vue'
          return 'vendor'
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8080',
      '/ws': { target: 'ws://localhost:8080', ws: true },
      '/media': 'http://localhost:8080',
    },
  },
})
