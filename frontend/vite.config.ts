import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // In dev the API runs separately on :8000; in production FastAPI serves the built app.
    proxy: { '/api': 'http://localhost:8000' },
  },
})
