import { spawn } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import {
  assertSafeInheritedBuildConfig,
  createReleaseBuilderConfig,
  expectedInstallerName,
  readReleaseSigningConfig,
  validatePackageVersion,
} from './release-signing-policy.mjs'
import { assertVerificationToolsAvailable, verifyReleaseArtifacts } from './verify-release-signing.mjs'

const projectDirectory = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const allowedNpmScripts = new Set(['build', 'build:agent-runtime'])

function runNpmScript(scriptName) {
  if (!allowedNpmScripts.has(scriptName)) throw new Error('Unexpected release build step.')
  const isWindows = process.platform === 'win32'
  const command = isWindows
    ? path.join(process.env.SystemRoot || process.env.WINDIR || 'C:\\Windows', 'System32', 'cmd.exe')
    : 'npm'
  const args = isWindows ? ['/d', '/s', '/c', `npm.cmd run ${scriptName}`] : ['run', scriptName]
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: projectDirectory,
      env: process.env,
      shell: false,
      stdio: 'inherit',
      windowsHide: true,
    })
    child.once('error', () => reject(new Error(`Could not start npm script ${scriptName}.`)))
    child.once('exit', (code, signal) => {
      if (code === 0) resolve()
      else reject(new Error(`npm script ${scriptName} failed (${signal || code}).`))
    })
  })
}

export function createElectronBuilderOptions(configuration, { Arch, Platform }, rootDirectory = projectDirectory) {
  return {
    projectDir: rootDirectory,
    targets: Platform.WINDOWS.createTarget(['nsis'], Arch.x64),
    config: configuration,
    publish: 'never',
  }
}

async function buildReleaseWithElectronBuilder(configuration) {
  const electronBuilder = await import('electron-builder')
  return electronBuilder.build(createElectronBuilderOptions(configuration, electronBuilder))
}

async function projectMetadata() {
  return JSON.parse(await readFile(path.join(projectDirectory, 'package.json'), 'utf8'))
}

function canonicalArtifactPath(artifactPath, rootDirectory, platform) {
  const resolved = path.resolve(rootDirectory, artifactPath)
  return platform === 'win32' ? resolved.toUpperCase() : resolved
}

export function assertExpectedInstallerBuilt(
  artifactPaths,
  version,
  rootDirectory = projectDirectory,
  platform = process.platform,
) {
  const expectedPath = path.join(rootDirectory, 'release', version, expectedInstallerName(version))
  const expectedCanonical = canonicalArtifactPath(expectedPath, rootDirectory, platform)
  const found =
    Array.isArray(artifactPaths) &&
    artifactPaths.some(
      (artifactPath) =>
        typeof artifactPath === 'string' &&
        canonicalArtifactPath(artifactPath, rootDirectory, platform) === expectedCanonical,
    )
  if (!found) {
    throw new Error(`electron-builder did not report the expected installer artifact: ${expectedPath}`)
  }
}

export async function runReleasePackaging({
  env = process.env,
  runBuildStep = runNpmScript,
  buildRelease = buildReleaseWithElectronBuilder,
  verifyRelease = verifyReleaseArtifacts,
  checkVerifier = assertVerificationToolsAvailable,
  log = console.log,
} = {}) {
  // This preflight must remain before every expensive or state-changing build step.
  const signingConfig = readReleaseSigningConfig(env)
  const packageJson = await projectMetadata()
  const version = validatePackageVersion(packageJson.version)
  assertSafeInheritedBuildConfig(packageJson.build)
  const builderConfig = createReleaseBuilderConfig(signingConfig, version)
  checkVerifier()
  log(`[release-signing] Release signer thumbprint: ${signingConfig.thumbprint}`)
  if (signingConfig.expectedSubject !== null) {
    log(`[release-signing] Expected signer subject: ${signingConfig.expectedSubject}`)
  }

  const builtAfter = Date.now()
  await runBuildStep('build')
  await runBuildStep('build:agent-runtime')
  const artifactPaths = await buildRelease(builderConfig)
  assertExpectedInstallerBuilt(artifactPaths, version)
  const verification = await verifyRelease({
    projectDir: projectDirectory,
    version,
    signingConfig,
    builtAfter,
  })
  log(`[release-signing] Report: ${path.relative(projectDirectory, verification.reportPath)}`)
  if (!verification.report.pass) throw new Error('Release signature verification failed.')
  return verification
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  runReleasePackaging().catch((error) => {
    console.error(`[release-signing] Release packaging refused or failed: ${error.message}`)
    process.exitCode = 1
  })
}
