import { mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { bootstrapDevelopmentEnvironment } from './development-env'

const temporaryFolders: string[] = []

afterEach(async () => {
  await Promise.all(temporaryFolders.splice(0).map((folder) => rm(folder, { recursive: true, force: true })))
})

async function createEnvironmentFolder(files: Record<string, string>): Promise<string> {
  const folder = join(tmpdir(), `lumi-development-env-${crypto.randomUUID()}`)
  temporaryFolders.push(folder)
  await mkdir(folder)
  await Promise.all(Object.entries(files).map(([name, contents]) => writeFile(join(folder, name), contents)))
  return folder
}

describe('main-process development environment bootstrap', () => {
  it('loads OPENAI_API_KEY from .env and lets .env.local override .env', async () => {
    const cwd = await createEnvironmentFolder({
      '.env': 'OPENAI_API_KEY=from-env\nLUMI_REASONING_EFFORT=low\n',
      '.env.local': 'LUMI_REASONING_EFFORT=high\n'
    })
    const environment: NodeJS.ProcessEnv = {}

    bootstrapDevelopmentEnvironment({ cwd, environment, isDevelopment: true })

    expect(environment.OPENAI_API_KEY).toBe('from-env')
    expect(environment.LUMI_REASONING_EFFORT).toBe('high')
  })

  it('does not overwrite an environment value inherited at launch', async () => {
    const cwd = await createEnvironmentFolder({ '.env': 'OPENAI_API_KEY=from-file\n' })
    const environment: NodeJS.ProcessEnv = { OPENAI_API_KEY: 'from-process' }

    bootstrapDevelopmentEnvironment({ cwd, environment, isDevelopment: true })

    expect(environment.OPENAI_API_KEY).toBe('from-process')
  })

  it('does not read local env files outside development', async () => {
    const cwd = await createEnvironmentFolder({ '.env': 'OPENAI_API_KEY=from-file\n' })
    const environment: NodeJS.ProcessEnv = {}

    bootstrapDevelopmentEnvironment({ cwd, environment, isDevelopment: false })

    expect(environment.OPENAI_API_KEY).toBeUndefined()
  })
})
