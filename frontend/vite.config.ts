import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:5555',
        ws: true,
      },
      '/bridge': {
        target: 'http://localhost:8080',
        rewrite: (path: string) => path.replace(/^\/bridge/, ''),
      },
    },
  },
})
