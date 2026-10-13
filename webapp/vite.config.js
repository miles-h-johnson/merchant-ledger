import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    allowedHosts: true,
    proxy: {
      '/search': {
        target: 'http://localhost:5000',
        changeOrigin: true,
      },
      '/calculate': {
        target: 'http://localhost:5000',
        changeOrigin: true,
      },
    },
  },
})
