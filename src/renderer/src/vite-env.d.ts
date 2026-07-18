/// <reference types="vite/client" />

import type { LifeLensApi } from '../../shared/contracts'

declare global {
  interface Window {
    lifeLens: LifeLensApi
  }
}

export {}
