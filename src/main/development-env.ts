import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { parse } from 'dotenv'

const DEVELOPMENT_ENV_FILES = ['.env', '.env.local'] as const

export interface DevelopmentEnvironmentBootstrapOptions {
  /** Project root used by `npm run dev`; never used in packaged builds. */
  cwd?: string
  /** Injectable for tests. The real main process passes `process.env`. */
  environment?: NodeJS.ProcessEnv
  /** electron-vite sets this only for `electron-vite dev`. */
  isDevelopment?: boolean
}

/**
 * Loads development-only main-process configuration without making it part of
 * Vite's renderer environment. Process variables inherited at launch always
 * win; `.env.local` can override values sourced from `.env`.
 */
export function bootstrapDevelopmentEnvironment({
  cwd = process.cwd(),
  environment = process.env,
  isDevelopment = process.env.NODE_ENV_ELECTRON_VITE === 'development'
}: DevelopmentEnvironmentBootstrapOptions = {}): void {
  if (!isDevelopment) return

  const inheritedKeys = new Set(Object.keys(environment))
  for (const fileName of DEVELOPMENT_ENV_FILES) {
    const filePath = resolve(cwd, fileName)
    if (!existsSync(filePath)) continue

    const values = parse(readFileSync(filePath))
    for (const [key, value] of Object.entries(values)) {
      if (!inheritedKeys.has(key)) {
        environment[key] = value
      }
    }
  }
}

// This module is imported only by the Electron main entry point. It performs
// no logging: configuration values, including secrets, must never be emitted.
bootstrapDevelopmentEnvironment()
