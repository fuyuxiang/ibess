import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  define: { __BUILD_TIME__: JSON.stringify(new Date().toISOString().slice(0, 16).replace('T', ' ')) },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'vendor-react': ['react', 'react-dom', 'react-router-dom'],
          'vendor-antd': ['antd', '@ant-design/icons'],
          'vendor-hls': ['hls.js'],
        },
      },
    },
  },
  server: {
    host: true,
    port: 3000,
    proxy: {
      '/api': { target: process.env.SUPERVISION_API_PROXY_TARGET || 'http://127.0.0.1:8000', changeOrigin: false },
      '/hls': { target: process.env.SUPERVISION_HLS_PROXY_TARGET || 'http://localhost:8888', changeOrigin: true, rewrite: path => path.replace(/^\/hls/, '') },
    },
  },
})
