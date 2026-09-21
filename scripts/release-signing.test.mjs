import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { WinPackager } from 'app-builder-lib'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  assertSafeInheritedBuildConfig,
  createReleaseBuilderConfig,
  evaluateArtifactSignature,
  expectedInstallerName,
  normalizeThumbprint,
  parseSignToolVerification,
  readReleaseSigningConfig,
} from './release-signing-policy.mjs'
import {
  assertExpectedInstallerBuilt,
  createElectronBuilderOptions,
  runReleasePackaging,
} from './package-release.mjs'
import { assertVerificationToolsAvailable, verifyReleaseArtifacts } from './verify-release-signing.mjs'

// TEST ONLY: data fixtures, not certificates or production signing identities.
const EXPECTED_THUMBPRINT = 'AB'.repeat(20)
const OTHER_THUMBPRINT = 'CD'.repeat(20)
const EXPECTED_SUBJECT = 'CN=TEST ONLY Lumi Signing Fixture, O=TEST ONLY'
const VERSION = '0.1.0'
const temporaryDirectories = []
const GENUINE_SIGNTOOL_OUTPUT = `
Verifying: C:\\release\\Lumi.exe
The signature is timestamped: Mon Sep 21 12:00:00 2026
Timestamp Verified by:
    Issued to: TEST ONLY Timestamp CA
Successfully verified: C:\\release\\Lumi.exe
Number of signatures successfully Verified: 1
Number of warnings: 0
Number of errors: 0
`

function validAuthenticode(overrides = {}) {
  return {
    status: 'Valid',
    signerCertificate: {
      subject: EXPECTED_SUBJECT,
      issuer: 'CN=Public Code Signing CA, O=Public CA',
      thumbprint: EXPECTED_THUMBPRINT,
      notBefore: '2026-01-01T00:00:00.000Z',
      notAfter: '2027-01-01T00:00:00.000Z',
    },
    timeStamperCertificatePresent: true,
    ...overrides,
  }
}

const validSignTool = Object.freeze({
  commandSucceeded: true,
  chainTrusted: true,
  timestampPresent: true,
})

function decide(authenticode, signTool = validSignTool, expectedThumbprint = EXPECTED_THUMBPRINT) {
  return evaluateArtifactSignature({
    authenticode,
    signTool,
    expectedThumbprint,
    expectedSubject: EXPECTED_SUBJECT,
  })
}

async function releaseFixture() {
  const directory = await mkdtemp(path.join(tmpdir(), 'lumi-release-signing-'))
  temporaryDirectories.push(directory)
  const releaseRoot = path.join(directory, 'release', VERSION)
  await mkdir(path.join(releaseRoot, 'win-unpacked'), { recursive: true })
  await writeFile(path.join(releaseRoot, 'win-unpacked', 'Lumi.exe'), 'application')
  await writeFile(path.join(releaseRoot, `Lumi Setup ${VERSION}.exe`), 'installer')
  return directory
}

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map((directory) => rm(directory, { recursive: true, force: true })))
})

describe('release signing policy', () => {
  it.each(['NotSigned', 'UnknownError'])(`rejects Authenticode status %s`, (status) => {
    expect(decide(validAuthenticode({ status })).pass).toBe(false)
    expect(decide(validAuthenticode({ status })).errors).toContain('authenticode_status_not_valid')
  })

  it('rejects an invalid or untrusted certificate chain', () => {
    const result = decide(validAuthenticode(), {
      commandSucceeded: false,
      chainTrusted: false,
      timestampPresent: true,
    })
    expect(result.pass).toBe(false)
    expect(result.errors).toContain('trusted_chain_verification_failed')
  })

  it('rejects a missing signer certificate', () => {
    const result = decide(validAuthenticode({ signerCertificate: null }))
    expect(result.errors).toContain('signer_certificate_missing')
    expect(result.pass).toBe(false)
  })

  it('rejects a wrong full thumbprint and a matching prefix', () => {
    expect(decide(validAuthenticode(), validSignTool, OTHER_THUMBPRINT).errors).toContain(
      'signer_thumbprint_mismatch',
    )
    expect(decide(validAuthenticode(), validSignTool, EXPECTED_THUMBPRINT.slice(0, 12)).pass).toBe(false)
  })

  it('normalizes whitespace and case but still requires an exact full thumbprint', () => {
    const spacedLowercase = EXPECTED_THUMBPRINT.toLowerCase().match(/.{1,4}/g).join(' ')
    const config = readReleaseSigningConfig({
      LUMI_SIGNING_CERT_SHA1: spacedLowercase,
      LUMI_SIGNING_TIMESTAMP_URL: 'https://timestamp.example.test/rfc3161',
      LUMI_SIGNING_EXPECTED_SUBJECT: EXPECTED_SUBJECT,
    })
    expect(config.thumbprint).toBe(EXPECTED_THUMBPRINT)
    expect(
      decide(
        validAuthenticode({
          signerCertificate: { ...validAuthenticode().signerCertificate, thumbprint: spacedLowercase },
        }),
      ).pass,
    ).toBe(true)
    expect(() =>
      readReleaseSigningConfig({
        LUMI_SIGNING_CERT_SHA1: EXPECTED_THUMBPRINT.slice(0, 12),
        LUMI_SIGNING_TIMESTAMP_URL: 'https://timestamp.example.test/rfc3161',
      }),
    ).toThrow(/40 hexadecimal/)
  })

  it('rejects a missing trusted timestamp', () => {
    const result = decide(validAuthenticode(), {
      commandSucceeded: true,
      chainTrusted: true,
      timestampPresent: false,
    })
    expect(result.errors).toContain('trusted_timestamp_missing')
    expect(result.pass).toBe(false)
  })

  it('accepts Valid plus a trusted chain, SignTool-verified timestamp evidence, and exact signer identity', () => {
    expect(decide(validAuthenticode()).pass).toBe(true)
  })

  it('rejects self-issued signer certificates even when Windows trusts them', () => {
    const certificate = validAuthenticode().signerCertificate
    const selfIssuedSubject = 'CN=TEST ONLY Self-Issued Signing Fixture, O=TEST ONLY'
    const result = evaluateArtifactSignature({
      authenticode: validAuthenticode({
        signerCertificate: { ...certificate, subject: selfIssuedSubject, issuer: selfIssuedSubject },
      }),
      signTool: validSignTool,
      expectedThumbprint: EXPECTED_THUMBPRINT,
      expectedSubject: null,
    })
    expect(result.errors).toContain('self_issued_signer_rejected')
    expect(result.pass).toBe(false)
  })

  it('fails closed when SignTool fails or its output is not parseable', () => {
    expect(parseSignToolVerification({ status: 1, stdout: '', stderr: 'failure' }).commandSucceeded).toBe(false)
    expect(parseSignToolVerification({ status: 0, stdout: 'ambiguous success', stderr: '' })).toEqual({
      commandSucceeded: false,
      chainTrusted: false,
      timestampPresent: false,
    })
  })

  it('parses only one exact, line-anchored SignTool summary', () => {
    expect(parseSignToolVerification({ status: 0, stdout: GENUINE_SIGNTOOL_OUTPUT, stderr: '' })).toEqual({
      commandSucceeded: true,
      chainTrusted: true,
      timestampPresent: true,
    })
    expect(
      parseSignToolVerification({
        status: 0,
        stdout: GENUINE_SIGNTOOL_OUTPUT.replace('Verified: 1', 'Verified: 10'),
        stderr: '',
      }).commandSucceeded,
    ).toBe(false)
    expect(
      parseSignToolVerification({
        status: 0,
        stdout: `${GENUINE_SIGNTOOL_OUTPUT}\nNumber of warnings: 0`,
        stderr: '',
      }).commandSucceeded,
    ).toBe(false)
    const counterfeitTimestamp = GENUINE_SIGNTOOL_OUTPUT.replace(
      'The signature is timestamped:',
      'certificate subject contains The signature is timestamped:',
    ).replace('Timestamp Verified by:', 'certificate subject contains Timestamp Verified by:')
    expect(
      parseSignToolVerification({ status: 0, stdout: counterfeitTimestamp, stderr: '' }).timestampPresent,
    ).toBe(false)
  })

  it('requires explicit HTTP(S) timestamp configuration without embedded credentials or URL tokens', () => {
    expect(() => readReleaseSigningConfig({ LUMI_SIGNING_CERT_SHA1: EXPECTED_THUMBPRINT })).toThrow(
      /LUMI_SIGNING_TIMESTAMP_URL is required/,
    )
    expect(() =>
      readReleaseSigningConfig({
        LUMI_SIGNING_CERT_SHA1: EXPECTED_THUMBPRINT,
        LUMI_SIGNING_TIMESTAMP_URL: 'ftp://timestamp.example.test/',
      }),
    ).toThrow(/HTTP or HTTPS/)
    expect(() =>
      readReleaseSigningConfig({
        LUMI_SIGNING_CERT_SHA1: EXPECTED_THUMBPRINT,
        LUMI_SIGNING_TIMESTAMP_URL: 'https://user:password@timestamp.example.test/',
      }),
    ).toThrow(/without credentials/)
    for (const suffix of ['?token=TEST_ONLY', '#TEST_ONLY']) {
      expect(() =>
        readReleaseSigningConfig({
          LUMI_SIGNING_CERT_SHA1: EXPECTED_THUMBPRINT,
          LUMI_SIGNING_TIMESTAMP_URL: `https://timestamp.example.test/rfc3161${suffix}`,
        }),
      ).toThrow(/query string, or a fragment/)
    }
  })

  it('rejects unsafe inherited electron-builder release hooks and signing overrides', async () => {
    const forbidden = [
      ['publish', []],
      ['afterSign', 'hook.mjs'],
      ['afterAllArtifactBuild', 'hook.mjs'],
      ['artifactBuildCompleted', 'hook.mjs'],
      ['nsis.script', 'installer.nsi'],
      ['win.azureSignOptions', {}],
      ['win.signtoolOptions.sign', 'sign-hook.mjs'],
      ['win.sign', 'sign-hook.mjs'],
      ['win.certificateFile', 'certificate.pfx'],
      ['win.certificatePassword', 'TEST ONLY'],
      ['win.signtoolOptions.certificateFile', 'certificate.pfx'],
      ['win.signtoolOptions.certificatePassword', 'TEST ONLY'],
      ['win.signAndEditExecutable', false],
      ['win.signExecutable', false],
    ]
    const configWith = (dottedKey, value) => {
      const result = {}
      const parts = dottedKey.split('.')
      let current = result
      for (const part of parts.slice(0, -1)) current = current[part] = {}
      current[parts.at(-1)] = value
      return result
    }
    for (const [key, value] of forbidden) {
      expect(() => assertSafeInheritedBuildConfig(configWith(key, value)), key).toThrow(`build.${key}`)
    }
    const packageJson = JSON.parse(await readFile(path.resolve('package.json'), 'utf8'))
    expect(() => assertSafeInheritedBuildConfig(packageJson.build)).not.toThrow()
  })

  it('builds a release-only SHA-256 configuration with forceCodeSigning and vendor EXE exclusion', () => {
    const config = createReleaseBuilderConfig(
      {
        thumbprint: EXPECTED_THUMBPRINT,
        timestampUrl: 'https://timestamp.example.test/rfc3161',
        expectedSubject: null,
      },
      VERSION,
    )
    expect(config.forceCodeSigning).toBe(true)
    expect(config.directories.output).toBe(`release/${VERSION}`)
    expect(config.win.signtoolOptions).toEqual({
      certificateSha1: EXPECTED_THUMBPRINT,
      signingHashAlgorithms: ['sha256'],
      rfc3161TimeStampServer: 'https://timestamp.example.test/rfc3161',
    })
    expect(config.win.signExts).toEqual([
      'Lumi.exe',
      `Lumi Setup ${VERSION}.exe`,
      '__uninstaller.exe',
      '!.exe',
    ])
    const shouldSign = (file) =>
      WinPackager.prototype.shouldSignFile.call(
        { platformSpecificBuildOptions: { signExts: config.win.signExts } },
        file,
        true,
      )
    expect(shouldSign('C:\\release\\win-unpacked\\Lumi.exe')).toBe(true)
    expect(shouldSign(`C:\\release\\Lumi Setup ${VERSION}.exe`)).toBe(true)
    expect(shouldSign('C:\\release\\Lumi Setup 0.1.0.__uninstaller.exe')).toBe(true)
    expect(shouldSign('C:\\release\\resources\\agent-runtime\\python\\python.exe')).toBe(false)
    expect(shouldSign('C:\\release\\resources\\agent-runtime\\chromium\\chrome.exe')).toBe(false)
  })
})

describe('release artifact verification', () => {
  const signingConfig = {
    thumbprint: EXPECTED_THUMBPRINT,
    timestampUrl: 'https://timestamp.example.test/rfc3161',
    expectedSubject: EXPECTED_SUBJECT,
  }

  it('fails the release when the installer is unsigned', async () => {
    const projectDir = await releaseFixture()
    const result = await verifyReleaseArtifacts({
      projectDir,
      version: VERSION,
      signingConfig,
      inspect: (file) =>
        file.endsWith('Lumi.exe') ? validAuthenticode() : validAuthenticode({ status: 'NotSigned', signerCertificate: null }),
      signTool: (file) => (file.endsWith('Lumi.exe') ? validSignTool : { ...validSignTool, timestampPresent: false }),
    })
    expect(result.report.pass).toBe(false)
    expect(result.report.artifacts.find((artifact) => artifact.kind === 'installer').signatureStatus).toBe('NotSigned')
  })

  it('fails when Lumi.exe is valid but the installer has the wrong signer', async () => {
    const projectDir = await releaseFixture()
    const result = await verifyReleaseArtifacts({
      projectDir,
      version: VERSION,
      signingConfig,
      inspect: (file) =>
        file.endsWith('Lumi.exe')
          ? validAuthenticode()
          : validAuthenticode({
              signerCertificate: { ...validAuthenticode().signerCertificate, thumbprint: OTHER_THUMBPRINT },
            }),
      signTool: () => validSignTool,
    })
    expect(result.report.pass).toBe(false)
    expect(result.report.artifacts.find((artifact) => artifact.kind === 'installer').errors).toContain(
      'signer_thumbprint_mismatch',
    )
  })

  it('fails and reports when the verification command itself throws', async () => {
    const projectDir = await releaseFixture()
    const result = await verifyReleaseArtifacts({
      projectDir,
      version: VERSION,
      signingConfig,
      inspect: () => validAuthenticode(),
      signTool: () => {
        throw new Error('TEST ONLY command failure')
      },
    })
    expect(result.report.pass).toBe(false)
    expect(result.report.artifacts.every((artifact) => artifact.errors.includes('trusted_chain_verification_failed'))).toBe(
      true,
    )
  })

  it('writes a passing report only when both fresh expected artifacts pass', async () => {
    const projectDir = await releaseFixture()
    const result = await verifyReleaseArtifacts({
      projectDir,
      version: VERSION,
      signingConfig,
      inspect: () => validAuthenticode(),
      signTool: () => validSignTool,
      clock: () => new Date('2026-09-21T12:00:00.000Z'),
      builtAfter: 0,
    })
    expect(result.report.pass).toBe(true)
    expect(result.report.freshnessChecked).toBe(true)
    expect(result.report.realAccountGateSatisfied).toBe(false)
    const written = JSON.parse(await readFile(result.reportPath, 'utf8'))
    expect(written.artifacts).toHaveLength(2)
    expect(written.artifacts.every((artifact) => artifact.sha256 && artifact.timestampPresent)).toBe(true)
  })

  it('never copies unrelated environment secrets into the JSON report', async () => {
    const sentinel = 'SENTINEL-REPORT-SECRET-DO-NOT-PRINT'
    const previousWinPassword = process.env.WIN_CSC_KEY_PASSWORD
    const previousCscPassword = process.env.CSC_KEY_PASSWORD
    process.env.WIN_CSC_KEY_PASSWORD = sentinel
    process.env.CSC_KEY_PASSWORD = sentinel
    try {
      const projectDir = await releaseFixture()
      const result = await verifyReleaseArtifacts({
        projectDir,
        version: VERSION,
        signingConfig,
        inspect: () => validAuthenticode(),
        signTool: () => validSignTool,
      })
      expect(await readFile(result.reportPath, 'utf8')).not.toContain(sentinel)
    } finally {
      if (previousWinPassword === undefined) delete process.env.WIN_CSC_KEY_PASSWORD
      else process.env.WIN_CSC_KEY_PASSWORD = previousWinPassword
      if (previousCscPassword === undefined) delete process.env.CSC_KEY_PASSWORD
      else process.env.CSC_KEY_PASSWORD = previousCscPassword
    }
  })

  it('fails closed when an expected artifact is missing', async () => {
    const projectDir = await releaseFixture()
    await rm(path.join(projectDir, 'release', VERSION, 'win-unpacked', 'Lumi.exe'))
    const result = await verifyReleaseArtifacts({
      projectDir,
      version: VERSION,
      signingConfig,
      inspect: () => validAuthenticode(),
      signTool: () => validSignTool,
    })
    expect(result.report.pass).toBe(false)
    expect(result.report.artifacts.find((artifact) => artifact.kind === 'application').errors).toContain(
      'artifact_missing',
    )
  })

  it('rejects any extra top-level executable beside the exact installer', async () => {
    const projectDir = await releaseFixture()
    await writeFile(path.join(projectDir, 'release', VERSION, 'old-installer.exe'), 'stale installer')
    const result = await verifyReleaseArtifacts({
      projectDir,
      version: VERSION,
      signingConfig,
      inspect: () => validAuthenticode(),
      signTool: () => validSignTool,
    })
    expect(result.report.pass).toBe(false)
    expect(result.report.errors).toContain('unexpected_executable_in_release_dir')
  })

  it('rejects both expected artifacts when their mtimes predate the build start', async () => {
    const projectDir = await releaseFixture()
    const result = await verifyReleaseArtifacts({
      projectDir,
      version: VERSION,
      signingConfig,
      inspect: () => validAuthenticode(),
      signTool: () => validSignTool,
      builtAfter: Date.now() + 60_000,
    })
    expect(result.report.pass).toBe(false)
    expect(result.report.freshnessChecked).toBe(true)
    expect(result.report.artifacts.every((artifact) => artifact.errors.includes('stale_artifact'))).toBe(true)
  })
})

describe('release wrapper boundary', () => {
  const expectedBuiltInstaller = path.resolve('release', VERSION, expectedInstallerName(VERSION))

  it('refuses missing signing credentials before spawning any build step', async () => {
    const runBuildStep = vi.fn()
    const buildRelease = vi.fn()
    await expect(
      runReleasePackaging({ env: {}, runBuildStep, buildRelease, log: vi.fn() }),
    ).rejects.toThrow(/LUMI_SIGNING_CERT_SHA1/)
    expect(runBuildStep).not.toHaveBeenCalled()
    expect(buildRelease).not.toHaveBeenCalled()
  })

  it('refuses before any build step when the verification tool is unavailable', async () => {
    const runBuildStep = vi.fn()
    const buildRelease = vi.fn()
    await expect(
      runReleasePackaging({
        env: {
          LUMI_SIGNING_CERT_SHA1: EXPECTED_THUMBPRINT,
          LUMI_SIGNING_TIMESTAMP_URL: 'https://timestamp.example.test/rfc3161',
        },
        runBuildStep,
        buildRelease,
        checkVerifier: () => assertVerificationToolsAvailable(() => false, 'win32'),
        log: vi.fn(),
      }),
    ).rejects.toThrow(/SignTool was not found/)
    expect(runBuildStep).not.toHaveBeenCalled()
    expect(buildRelease).not.toHaveBeenCalled()
  })

  it('requires the vendored SignTool itself to be Valid and Microsoft-signed', () => {
    const microsoftCertificate = {
      ...validAuthenticode().signerCertificate,
      subject: 'CN=Microsoft Corporation, O=Microsoft Corporation, C=US',
    }
    expect(() =>
      assertVerificationToolsAvailable(
        () => true,
        'win32',
        () => validAuthenticode({ signerCertificate: microsoftCertificate }),
      ),
    ).not.toThrow()
    expect(() =>
      assertVerificationToolsAvailable(
        () => true,
        'win32',
        () => validAuthenticode({ status: 'NotSigned', signerCertificate: null }),
      ),
    ).toThrow(/not Validly Authenticode-signed by Microsoft/)
    expect(() =>
      assertVerificationToolsAvailable(() => true, 'win32', () => validAuthenticode()),
    ).toThrow(/not Validly Authenticode-signed by Microsoft/)
  })

  it('hard-disables electron-builder publishing', () => {
    const createTarget = vi.fn(() => new Map())
    const options = createElectronBuilderOptions(
      { forceCodeSigning: true },
      { Arch: { x64: 'x64' }, Platform: { WINDOWS: { createTarget } } },
      'C:\\TEST ONLY\\Lumi',
    )
    expect(options.publish).toBe('never')
    expect(createTarget).toHaveBeenCalledWith(['nsis'], 'x64')
  })

  it('requires electron-builder to report the exact canonical installer path', async () => {
    expect(() =>
      assertExpectedInstallerBuilt(
        [expectedBuiltInstaller.toUpperCase()],
        VERSION,
        path.resolve('.'),
        'win32',
      ),
    ).not.toThrow()

    const verifyRelease = vi.fn()
    await expect(
      runReleasePackaging({
        env: {
          LUMI_SIGNING_CERT_SHA1: EXPECTED_THUMBPRINT,
          LUMI_SIGNING_TIMESTAMP_URL: 'https://timestamp.example.test/rfc3161',
        },
        runBuildStep: vi.fn().mockResolvedValue(undefined),
        buildRelease: vi.fn().mockResolvedValue([]),
        verifyRelease,
        checkVerifier: vi.fn(),
        log: vi.fn(),
      }),
    ).rejects.toThrow(/did not report the expected installer artifact/)
    expect(verifyRelease).not.toHaveBeenCalled()
  })

  it('does not expose unrelated environment secrets in logs, builder config, or report', async () => {
    const sentinel = 'SENTINEL-RELEASE-SECRET-DO-NOT-PRINT'
    const logs = []
    let capturedBuilderConfig
    const verification = {
      reportPath: path.join('release', VERSION, 'signing-report', 'release-signing-report.json'),
      report: { pass: true, artifacts: [] },
    }
    const verifyRelease = vi.fn().mockResolvedValue(verification)
    await runReleasePackaging({
      env: {
        LUMI_SIGNING_CERT_SHA1: EXPECTED_THUMBPRINT,
        LUMI_SIGNING_TIMESTAMP_URL: 'https://timestamp.example.test/rfc3161',
        WIN_CSC_KEY_PASSWORD: sentinel,
        CSC_KEY_PASSWORD: sentinel,
        SOME_OTHER_SECRET: sentinel,
      },
      runBuildStep: vi.fn().mockResolvedValue(undefined),
      buildRelease: vi.fn(async (config) => {
        capturedBuilderConfig = config
        return [expectedBuiltInstaller]
      }),
      verifyRelease,
      checkVerifier: vi.fn(),
      log: (message) => logs.push(message),
    })
    expect(JSON.stringify({ logs, capturedBuilderConfig, report: verification.report })).not.toContain(sentinel)
    const verificationInput = verifyRelease.mock.calls[0][0]
    expect(verificationInput.builtAfter).toEqual(expect.any(Number))
    expect(verificationInput.builtAfter).toBeLessThanOrEqual(Date.now())
  })

  it('leaves development package scripts unsigned-capable and unchanged', async () => {
    const packageJson = JSON.parse(await readFile(path.resolve('package.json'), 'utf8'))
    expect(packageJson.scripts.package).toBe(
      'npm run build && npm run build:agent-runtime && electron-builder --win --x64',
    )
    expect(packageJson.scripts['package:dir']).toBe(
      'npm run build && npm run build:agent-runtime && electron-builder --win --x64 --dir',
    )
    expect(packageJson.scripts.package).not.toContain('forceCodeSigning')
    expect(packageJson.scripts['package:dir']).not.toContain('forceCodeSigning')
    expect(packageJson.build.forceCodeSigning).toBeUndefined()
  })
})
