import { resolve } from 'node:path'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
    build: {
      lib: {
        // The vision worker is a second main-scope entry: it runs in an Electron
        // utilityProcess, so it must be built as its own CommonJS bundle.
        entry: {
          index: resolve('src/main/index.ts'),
          'vision-worker': resolve('src/main/vision-worker.ts')
        },
        formats: ['cjs'],
        fileName: (_format, entryName) => `${entryName}.cjs`
      }
    }
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    build: {
      lib: {
        entry: resolve('src/preload/index.ts'),
        formats: ['cjs'],
        fileName: 'index.cjs'
      },
      rollupOptions: {
        output: {
          inlineDynamicImports: true
        }
      }
    }
  },
  renderer: {
    resolve: {
      alias: {
        '@renderer': resolve('src/renderer/src')
      }
    },
    plugins: [react()]
  }
})
