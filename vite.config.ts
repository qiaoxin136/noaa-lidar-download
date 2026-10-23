import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    proxy: {
      '/noaa-api': {
        target: 'https://coast.noaa.gov/dataviewer/api/v1',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/noaa-api/, ''),
      },
    },
  },
})
