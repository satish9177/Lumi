const THUMBPRINT_PATTERN = /^[0-9A-F]{40}$/

export class ReleaseSigningError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'ReleaseSigningError'
    this.code = code
  }
}

export function normalizeThumbprint(value) {
  return typeof value === 'string' ? value.replace(/\s/g, '').toUpperCase() : ''
}

export function readReleaseSigningConfig(env) {
  const thumbprint = normalizeThumbprint(env.LUMI_SIGNING_CERT_SHA1)
  if (!THUMBPRINT_PATTERN.test(thumbprint)) {
    throw new ReleaseSigningError(
      'invalid_signing_thumbprint',
      'LUMI_SIGNING_CERT_SHA1 is required and must normalize to exactly 40 hexadecimal characters.',
    )
  }

  const timestampValue = env.LUMI_SIGNING_TIMESTAMP_URL
  if (typeof timestampValue !== 'string' || timestampValue.trim() === '') {
    throw new ReleaseSigningError(
      'missing_timestamp_url',
      'LUMI_SIGNING_TIMESTAMP_URL is required; no default timestamp service is used.',
    )
  }

  let timestampUrl
  try {
    timestampUrl = new URL(timestampValue.trim())
  } catch {
    throw new ReleaseSigningError(
      'invalid_timestamp_url',
      'LUMI_SIGNING_TIMESTAMP_URL must be an absolute HTTP or HTTPS URL.',
    )
  }
  if (
    !['http:', 'https:'].includes(timestampUrl.protocol) ||
    timestampUrl.username ||
    timestampUrl.password ||
    timestampUrl.search ||
    timestampUrl.hash
  ) {
    throw new ReleaseSigningError(
      'invalid_timestamp_url',
      'LUMI_SIGNING_TIMESTAMP_URL must be an HTTP or HTTPS URL without credentials, a query string, or a fragment.',
    )
  }

  let expectedSubject = null
  if (env.LUMI_SIGNING_EXPECTED_SUBJECT !== undefined) {
    if (
      typeof env.LUMI_SIGNING_EXPECTED_SUBJECT !== 'string' ||
      env.LUMI_SIGNING_EXPECTED_SUBJECT === '' ||
      env.LUMI_SIGNING_EXPECTED_SUBJECT.trim() !== env.LUMI_SIGNING_EXPECTED_SUBJECT
    ) {
      throw new ReleaseSigningError(
        'invalid_expected_subject',
        'LUMI_SIGNING_EXPECTED_SUBJECT, when set, must be a non-empty exact certificate subject without outer whitespace.',
      )
    }
    expectedSubject = env.LUMI_SIGNING_EXPECTED_SUBJECT
  }

  return Object.freeze({
    thumbprint,
    timestampUrl: timestampUrl.toString(),
    expectedSubject,
  })
}

export function validatePackageVersion(version) {
  if (typeof version !== 'string' || !/^[0-9A-Za-z][0-9A-Za-z.+-]*$/.test(version)) {
    throw new ReleaseSigningError('invalid_package_version', 'package.json contains an invalid release version.')
  }
  return version
}

export function expectedInstallerName(version) {
  return `Lumi Setup ${validatePackageVersion(version)}.exe`
}

function hasOwnPath(value, pathSegments) {
  let current = value
  for (const segment of pathSegments) {
    if (current === null || typeof current !== 'object' || !Object.hasOwn(current, segment)) return false
    current = current[segment]
  }
  return true
}

export function assertSafeInheritedBuildConfig(buildConfig) {
  const config = buildConfig ?? {}
  const forbiddenPaths = [
    ['publish'],
    ['afterSign'],
    ['afterAllArtifactBuild'],
    ['artifactBuildCompleted'],
    ['nsis', 'script'],
    ['win', 'azureSignOptions'],
    ['win', 'signtoolOptions', 'sign'],
    ['win', 'sign'],
    ['win', 'certificateFile'],
    ['win', 'certificatePassword'],
    ['win', 'signtoolOptions', 'certificateFile'],
    ['win', 'signtoolOptions', 'certificatePassword'],
  ]
  for (const segments of forbiddenPaths) {
    if (hasOwnPath(config, segments)) {
      const key = `build.${segments.join('.')}`
      throw new ReleaseSigningError(
        'unsafe_inherited_builder_config',
        `${key} is not allowed on the release-signing path.`,
      )
    }
  }
  for (const key of ['signAndEditExecutable', 'signExecutable']) {
    if (config?.win?.[key] === false) {
      throw new ReleaseSigningError(
        'unsafe_inherited_builder_config',
        `build.win.${key} must not be false on the release-signing path.`,
      )
    }
  }
}

export function createReleaseBuilderConfig(signingConfig, version) {
  const installerName = expectedInstallerName(version)
  return {
    forceCodeSigning: true,
    directories: {
      output: `release/${validatePackageVersion(version)}`,
    },
    win: {
      // electron-builder v26 checks positive suffixes before negative suffixes.
      // This signs only Lumi-owned executables and preserves signatures on vendor EXEs.
      signExts: ['Lumi.exe', installerName, '__uninstaller.exe', '!.exe'],
      signtoolOptions: {
        certificateSha1: signingConfig.thumbprint,
        signingHashAlgorithms: ['sha256'],
        rfc3161TimeStampServer: signingConfig.timestampUrl,
      },
    },
    nsis: {
      artifactName: `Lumi Setup ${validatePackageVersion(version)}.\${ext}`,
    },
  }
}

function sameDistinguishedName(left, right) {
  return left.trim().toUpperCase() === right.trim().toUpperCase()
}

export function parseSignToolVerification(result) {
  if (result?.error || !Number.isInteger(result?.status)) {
    return { chainTrusted: false, timestampPresent: false, commandSucceeded: false }
  }

  const output = `${result.stdout ?? ''}\n${result.stderr ?? ''}`
  const parseExactlyOneSummary = (label) => {
    const pattern = new RegExp(`^[ \\t]*${label}:[ \\t]*(\\d+)[ \\t]*\\r?$`, 'gim')
    const matches = [...output.matchAll(pattern)]
    return matches.length === 1 ? Number.parseInt(matches[0][1], 10) : null
  }
  const verifiedCount = parseExactlyOneSummary('Number of signatures successfully Verified')
  const warningCount = parseExactlyOneSummary('Number of warnings')
  const errorCount = parseExactlyOneSummary('Number of errors')
  const summaryIsValid =
    /^[ \t]*Successfully verified:[^\r\n]*\r?$/im.test(output) &&
    verifiedCount === 1 &&
    warningCount === 0 &&
    errorCount === 0
  const timestampPresent =
    /^[ \t]*The signature is timestamped:[^\r\n]*\r?$/im.test(output) &&
    /^[ \t]*Timestamp Verified by:[ \t]*\r?$/im.test(output)
  const commandSucceeded = result.status === 0 && summaryIsValid

  return {
    chainTrusted: commandSucceeded,
    timestampPresent: commandSucceeded && timestampPresent,
    commandSucceeded,
  }
}

export function evaluateArtifactSignature({ authenticode, signTool, expectedThumbprint, expectedSubject = null }) {
  const errors = []
  if (authenticode?.status !== 'Valid') {
    errors.push('authenticode_status_not_valid')
  }

  const certificate = authenticode?.signerCertificate ?? null
  if (certificate === null) {
    errors.push('signer_certificate_missing')
  }

  const actualThumbprint = normalizeThumbprint(certificate?.thumbprint)
  if (!THUMBPRINT_PATTERN.test(actualThumbprint) || actualThumbprint !== expectedThumbprint) {
    errors.push('signer_thumbprint_mismatch')
  }
  if (expectedSubject !== null && certificate?.subject !== expectedSubject) {
    errors.push('signer_subject_mismatch')
  }
  if (
    typeof certificate?.subject === 'string' &&
    typeof certificate?.issuer === 'string' &&
    sameDistinguishedName(certificate.subject, certificate.issuer)
  ) {
    errors.push('self_issued_signer_rejected')
  }
  if (signTool?.commandSucceeded !== true || signTool?.chainTrusted !== true) {
    errors.push('trusted_chain_verification_failed')
  }
  if (signTool?.timestampPresent !== true) {
    errors.push('trusted_timestamp_missing')
  }

  return {
    pass: errors.length === 0,
    errors,
    signerThumbprint: actualThumbprint || null,
  }
}

export function evaluateReleaseArtifacts(artifacts) {
  return artifacts.length === 2 && artifacts.every((artifact) => artifact.pass === true)
}
