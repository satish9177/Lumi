import { createHash } from 'node:crypto'
import { createReadStream, existsSync } from 'node:fs'
import { mkdir, readFile, readdir, stat, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { spawnSync } from 'node:child_process'
import {
  evaluateArtifactSignature,
  evaluateReleaseArtifacts,
  expectedInstallerName,
  normalizeThumbprint,
  parseSignToolVerification,
  readReleaseSigningConfig,
  validatePackageVersion,
} from './release-signing-policy.mjs'

const scriptsDirectory = path.dirname(fileURLToPath(import.meta.url))
const projectDirectory = path.resolve(scriptsDirectory, '..')
const authenticodeScript = path.join(scriptsDirectory, 'Get-LumiAuthenticodeSignature.ps1')
const signToolPath = path.join(
  projectDirectory,
  'node_modules',
  '@electron',
  'windows-sign',
  'vendor',
  'signtool.exe',
)

function fixedWindowsEnvironment() {
  const systemRoot = process.env.SystemRoot || process.env.WINDIR || 'C:\\Windows'
  const environment = {
    SystemRoot: systemRoot,
    WINDIR: systemRoot,
    PSModulePath: path.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'Modules'),
  }
  for (const name of ['TEMP', 'TMP']) {
    if (process.env[name]) environment[name] = process.env[name]
  }
  return environment
}

function parseAuthenticodeOutput(output) {
  let value
  try {
    value = JSON.parse(output.trim())
  } catch {
    throw new Error('authenticode_result_unparseable')
  }
  if (typeof value?.status !== 'string') throw new Error('authenticode_result_unparseable')
  if (value.signerCertificate !== null) {
    const certificate = value.signerCertificate
    for (const field of ['subject', 'issuer', 'thumbprint', 'notBefore', 'notAfter']) {
      if (typeof certificate?.[field] !== 'string') throw new Error('authenticode_result_unparseable')
    }
  }
  return value
}

export function inspectAuthenticode(artifactPath, spawn = spawnSync) {
  if (process.platform !== 'win32') throw new Error('authenticode_requires_windows')
  const systemRoot = process.env.SystemRoot || process.env.WINDIR || 'C:\\Windows'
  const powershellPath = path.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
  const result = spawn(
    powershellPath,
    [
      '-NoLogo',
      '-NoProfile',
      '-NonInteractive',
      '-ExecutionPolicy',
      'Bypass',
      '-File',
      authenticodeScript,
      artifactPath,
    ],
    {
      shell: false,
      encoding: 'utf8',
      windowsHide: true,
      env: fixedWindowsEnvironment(),
      maxBuffer: 1024 * 1024,
    },
  )
  if (result.error || result.status !== 0) throw new Error('authenticode_command_failed')
  return parseAuthenticodeOutput(result.stdout)
}

/**
 * Release preflight: prove the verification tools exist before an expensive build,
 * so a missing verifier is a fast refusal rather than a failure after the build.
 * The release still fails closed later if verification cannot be established.
 */
export function assertVerificationToolsAvailable(
  exists = existsSync,
  platform = process.platform,
  inspect = inspectAuthenticode,
) {
  if (platform !== 'win32') throw new Error('Release verification requires Windows.')
  const systemRoot = process.env.SystemRoot || process.env.WINDIR || 'C:\\Windows'
  const powershellPath = path.join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
  if (!exists(signToolPath)) {
    throw new Error('Release verification tool SignTool was not found; refusing to build a release.')
  }
  if (!exists(powershellPath)) {
    throw new Error('Windows PowerShell was not found; refusing to build a release.')
  }
  let signature
  try {
    signature = inspect(signToolPath)
  } catch {
    throw new Error('SignTool Authenticode integrity could not be established; refusing to build a release.')
  }
  const signerSubject = signature?.signerCertificate?.subject
  if (
    signature?.status !== 'Valid' ||
    typeof signerSubject !== 'string' ||
    !/^CN=Microsoft (?:Corporation|Windows)(?:,|$)/.test(signerSubject)
  ) {
    throw new Error('SignTool is not Validly Authenticode-signed by Microsoft; refusing to build a release.')
  }
}

export function runSignToolVerification(artifactPath, spawn = spawnSync) {
  if (process.platform !== 'win32') {
    return { chainTrusted: false, timestampPresent: false, commandSucceeded: false }
  }
  const result = spawn(signToolPath, ['verify', '/pa', '/all', '/tw', '/v', artifactPath], {
    shell: false,
    encoding: 'utf8',
    windowsHide: true,
    env: fixedWindowsEnvironment(),
    maxBuffer: 1024 * 1024,
  })
  return parseSignToolVerification(result)
}

async function sha256File(filePath) {
  const hash = createHash('sha256')
  for await (const chunk of createReadStream(filePath)) hash.update(chunk)
  return hash.digest('hex').toUpperCase()
}

async function isFile(filePath) {
  try {
    return (await stat(filePath)).isFile()
  } catch {
    return false
  }
}

function reportPathFor(filePath, rootDirectory) {
  const relative = path.relative(rootDirectory, filePath)
  return relative.startsWith('..') ? path.resolve(filePath) : relative.replaceAll(path.sep, '/')
}

async function verifyArtifact(kind, filePath, signingConfig, dependencies, rootDirectory) {
  const startedAt = dependencies.now().toISOString()
  const report = {
    kind,
    path: reportPathFor(filePath, rootDirectory),
    sha256: null,
    signatureStatus: 'Unavailable',
    signerSubject: null,
    signerThumbprint: null,
    certificateNotBefore: null,
    certificateNotAfter: null,
    timestampPresent: false,
    verificationTime: startedAt,
    pass: false,
    errors: [],
  }

  if (!(await dependencies.isFile(filePath))) {
    report.errors.push('artifact_missing')
    return report
  }

  if (dependencies.builtAfter !== undefined) {
    try {
      const artifactStat = await dependencies.statFile(filePath)
      if (!Number.isFinite(artifactStat.mtimeMs) || artifactStat.mtimeMs < dependencies.builtAfter) {
        report.errors.push('stale_artifact')
      }
    } catch {
      report.errors.push('artifact_freshness_check_failed')
    }
  }

  try {
    report.sha256 = await dependencies.sha256File(filePath)
  } catch {
    report.errors.push('artifact_hash_failed')
  }

  let authenticode
  try {
    authenticode = dependencies.inspectAuthenticode(filePath)
    report.signatureStatus = authenticode.status
    report.signerSubject = authenticode.signerCertificate?.subject ?? null
    report.signerThumbprint = normalizeThumbprint(authenticode.signerCertificate?.thumbprint) || null
    report.certificateNotBefore = authenticode.signerCertificate?.notBefore ?? null
    report.certificateNotAfter = authenticode.signerCertificate?.notAfter ?? null
  } catch {
    report.errors.push('authenticode_inspection_failed')
    return report
  }

  let signTool
  try {
    signTool = dependencies.runSignToolVerification(filePath)
  } catch {
    signTool = { commandSucceeded: false, chainTrusted: false, timestampPresent: false }
  }
  const decision = evaluateArtifactSignature({
    authenticode,
    signTool,
    expectedThumbprint: signingConfig.thumbprint,
    expectedSubject: signingConfig.expectedSubject,
  })
  report.timestampPresent = signTool.timestampPresent === true
  report.errors.push(...decision.errors)
  if (report.sha256 !== null) {
    try {
      const hashAfterVerification = await dependencies.sha256File(filePath)
      if (hashAfterVerification !== report.sha256) report.errors.push('artifact_changed_during_verification')
    } catch {
      report.errors.push('artifact_hash_failed_after_verification')
    }
  }
  report.pass = decision.pass && report.sha256 !== null && report.errors.length === 0
  return report
}

export async function verifyReleaseArtifacts({
  projectDir = projectDirectory,
  version,
  signingConfig,
  inspect = inspectAuthenticode,
  signTool = runSignToolVerification,
  hashFile = sha256File,
  fileCheck = isFile,
  fileStat = stat,
  readDirectory = readdir,
  clock = () => new Date(),
  builtAfter,
} = {}) {
  validatePackageVersion(version)
  if (builtAfter !== undefined && (!Number.isFinite(builtAfter) || builtAfter < 0)) {
    throw new Error('builtAfter must be a non-negative epoch-millisecond value.')
  }
  const releaseRoot = path.join(projectDir, 'release', version)
  const expectedInstaller = expectedInstallerName(version)
  let files = []
  let discoveryFailed = false
  try {
    files = await readDirectory(releaseRoot)
  } catch {
    discoveryFailed = true
  }
  const topLevelExecutables = files.filter((file) => typeof file === 'string' && file.toLowerCase().endsWith('.exe'))
  const expectedInstallerPresent = files.includes(expectedInstaller)
  const installerPath = path.join(releaseRoot, expectedInstaller)
  const dependencies = {
    inspectAuthenticode: inspect,
    runSignToolVerification: signTool,
    sha256File: hashFile,
    isFile: fileCheck,
    statFile: fileStat,
    now: clock,
    builtAfter,
  }
  const artifacts = await Promise.all([
    verifyArtifact('application', path.join(releaseRoot, 'win-unpacked', 'Lumi.exe'), signingConfig, dependencies, projectDir),
    verifyArtifact('installer', installerPath, signingConfig, dependencies, projectDir),
  ])

  const errors = []
  if (discoveryFailed) errors.push('release_directory_unreadable')
  if (!expectedInstallerPresent) errors.push('installer_count_not_one')
  if (topLevelExecutables.some((file) => file !== expectedInstaller)) {
    errors.push('unexpected_executable_in_release_dir')
  }
  const verifiedAt = clock().toISOString()
  const pass = errors.length === 0 && evaluateReleaseArtifacts(artifacts)
  const report = {
    schemaVersion: 1,
    pass,
    result: pass ? 'release signature verified' : 'release signature verification failed',
    realAccountGateSatisfied: false,
    realAccountGateNote:
      'A release manager must separately confirm a publicly trusted code-signing CA and the intended Lumi publisher identity.',
    version,
    verifiedAt,
    freshnessChecked: builtAfter !== undefined,
    expectedSigner: {
      thumbprint: signingConfig.thumbprint,
      subject: signingConfig.expectedSubject,
    },
    artifacts,
    errors,
  }

  const reportDirectory = path.join(releaseRoot, 'signing-report')
  await mkdir(reportDirectory, { recursive: true })
  const reportPath = path.join(reportDirectory, 'release-signing-report.json')
  await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8')
  return { report, reportPath }
}

export async function diagnoseArtifact(artifactPath, { inspect = inspectAuthenticode } = {}) {
  if (!(await isFile(artifactPath))) {
    return { pass: false, status: 'Unavailable', errors: ['artifact_missing'] }
  }
  try {
    const authenticode = inspect(path.resolve(artifactPath))
    return {
      pass: false,
      status: authenticode.status,
      errors:
        authenticode.status === 'Valid'
          ? ['expected_release_signer_not_configured_in_diagnostic_mode']
          : ['authenticode_status_not_valid'],
    }
  } catch {
    return { pass: false, status: 'Unavailable', errors: ['authenticode_inspection_failed'] }
  }
}

async function readProjectVersion() {
  const packageJson = JSON.parse(await readFile(path.join(projectDirectory, 'package.json'), 'utf8'))
  return validatePackageVersion(packageJson.version)
}

async function main() {
  if (process.argv[2] === '--artifact' && process.argv.length === 4) {
    const result = await diagnoseArtifact(process.argv[3])
    console.log(`[release-signing] Artifact signature status: ${result.status}; result: FAIL`)
    process.exitCode = 1
    return
  }
  if (process.argv.length !== 2) {
    throw new Error('Usage: node scripts/verify-release-signing.mjs [--artifact <path>]')
  }
  const signingConfig = readReleaseSigningConfig(process.env)
  const version = await readProjectVersion()
  const { report, reportPath } = await verifyReleaseArtifacts({ version, signingConfig })
  console.log(`[release-signing] Report: ${path.relative(projectDirectory, reportPath)}`)
  if (!report.pass) throw new Error('Release signature verification failed.')
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((error) => {
    console.error(`[release-signing] ${error.message}`)
    process.exitCode = 1
  })
}
