# Windows release-signing gate review

Status: implementation complete; real signed-artifact validation pending  
Real-account release: **BLOCKED**  
Production certificate acquired: **NO**

## Why this gate exists

The prior package log said `signing with signtool.exe` while Windows reported
the resulting `Lumi.exe` as `NotSigned`. Smart App Control also blocked rebuilt,
reputation-less executables. Builder log text is therefore not release
evidence. Unsigned development output must never be treated as a release or be
used for real-account testing.

## Builder and release split

The installed and retained builder is electron-builder **26.15.3**; there is no
v27 migration. Existing `package:dir` and `package` scripts are unchanged and
may remain unsigned for development. `package:release` performs a signing
preflight before expensive work, runs the same TypeScript/Electron build and
agent-runtime build, invokes electron-builder with `forceCodeSigning: true`,
forces `publish: 'never'` and `release/<version>` output, and then runs
independent Windows verification. Before building it rejects inherited publish,
lifecycle/artifact hooks, custom/file/Azure signing paths, an NSIS script and
disabled executable signing. No global
`build.forceCodeSigning` was added.

## Signing model

Trusted configuration comes only from:

- `LUMI_SIGNING_CERT_SHA1`, normalized by removing whitespace and uppercasing,
  then required to match exactly 40 hexadecimal characters;
- `LUMI_SIGNING_TIMESTAMP_URL`, required explicitly and limited to HTTP(S),
  without credentials, a query string or a fragment, with no builder timestamp
  default allowed;
- optional `LUMI_SIGNING_EXPECTED_SUBJECT`, compared exactly after signing.

The certificate is selected from the Windows certificate store by its full
thumbprint, never a prefix or subject-name search. The signing digest list is
only SHA-256 and timestamping uses the configured RFC 3161 server. The intended
deployment is a public code-signing certificate exposed through a Windows
CNG/KSP provider while its private key stays in the provider HSM (for example,
an SSL.com eSigner store integration). Lumi carries no certificate, key,
password, token, signing service or vendor credential.

## Verification procedure

The gate expects exactly:

1. `release/<version>/win-unpacked/Lumi.exe`;
2. one `release/<version>/Lumi Setup <version>.exe`.

No other top-level `.exe` may exist in the version directory. Both files must
have mtimes at or after the timestamp recorded immediately before the first
build step, and electron-builder's returned artifact list must contain the
canonical installer path. Existing release files are not deleted by the gate.

A fixed PowerShell script receives each literal path as a separate process
argument and returns only structured public certificate facts.
`Get-AuthenticodeSignature` must have status `Valid` and a non-null signer. The
complete normalized signer thumbprint must equal the configured value; a
configured subject must match exactly. A leaf with equal Subject and Issuer is
rejected.

The Microsoft SignTool present in `@electron/windows-sign` is first required to
be Authenticode `Valid` with a Microsoft signer subject. It is then invoked with
a fixed argument array and `shell: false`:

```text
signtool.exe verify /pa /all /tw /v <artifact>
```

Exit zero is not enough. Its line-anchored output must contain exactly one
summary each for one successfully verified signature, zero warnings and zero
errors, plus the expected timestamp lines. Duplicate/conflicting summaries fail.
`/pa` makes Windows' Authenticode trust policy authoritative; `/tw` returns a
warning exit for an absent timestamp. This establishes only that a trusted
timestamp is present and verified by SignTool `/tw`; it does not independently
prove that timestamp's protocol or server. Artifact freshness and the forced
RFC 3161 signing configuration tie the result to this build. Missing tools,
process errors, warnings, invalid chains and non-parseable output all fail
closed. The PowerShell `TimeStamperCertificate` field is not used as proof.

The output-side JSON report records each artifact path, SHA-256, status, signer
subject/thumbprint, certificate validity dates, SignTool `/tw` timestamp result,
freshness-check status, verification time and decision. It contains no
environment dump or credential.
Reports explicitly set `realAccountGateSatisfied: false`.

## Third-party executable finding and policy

Review of `app-builder-lib/out/winPackager.js` confirmed the lead's finding:
`createTransformerForExtraFiles` calls `signIf` for matching files outside the
application output root, while `signApp` walks `resources/app.asar.unpacked`.
With v26 defaults and a real certificate, `.exe` files in the Python/Chromium
runtime would be offered to Lumi signing, replacing vendor signatures.

The release-only `win.signExts` list uses v26's reviewed positive-before-negative
suffix behavior:

```text
Lumi.exe
Lumi Setup <version>.exe
__uninstaller.exe
!.exe
```

Classification:

1. Lumi-owned and must-sign: `Lumi.exe`, the versioned NSIS installer, and the
   temporary NSIS uninstaller before embedding.
2. Third-party and retain provenance: CPython and its launchers, Playwright
   Chromium and helpers, ffmpeg, Node, `elevate.exe`, `.pyd`, `.dll`, and other
   runtime/native dependencies.
3. Actually re-signed under this release policy: only files whose paths end in
   one of the three positive Lumi-owned suffixes. The final application and
   installer are independently verified; the embedded uninstaller is not
   extracted for a separate post-build verification.

This avoids disabling executable signing globally, so `forceCodeSigning`
remains enabled. A future filename collision or new Lumi-owned executable must
be reviewed and the allowlist updated deliberately.

## Residual risks and status

- A private enterprise CA trusted on the build/verification machine can satisfy
  Windows chain policy without proving public trust. Rejecting self-issued leaf
  certificates closes the simplest local self-signed bypass, not every private
  PKI case.
- The release manager must independently confirm a publicly trusted code-signing
  CA and intended Lumi publisher identity. Automation does not claim the
  real-account gate is satisfied.
- The suffix policy depends on the inspected electron-builder v26 behavior;
  builder upgrades require re-review.
- The embedded NSIS uninstaller is signed during construction but not extracted
  and independently verified after embedding.
- Verification and later publication are separate pathname operations. A local
  actor could replace an artifact after the report is written. Publication must
  be a separate step that re-hashes files and consumes the report's SHA-256
  values, ideally from an isolated staging directory.
- SignTool comes from the transitive optional dependency
  `@electron/windows-sign` 1.2.2 via `electron-winstaller`; Lumi does not declare
  it directly, although it is present in `package-lock.json`. Dependency-tree
  changes can therefore make the verifier unavailable, which fails preflight.
- Revocation and timestamp trust depend on Windows trust state and network
  availability at verification; uncertainty fails the command.
- No real production certificate is available here, so successful HSM-backed
  signing, timestamp service interoperability and a passing real artifact could
  not be validated.

The implementation can prove that unsigned and malformed candidates fail. It
cannot yet prove a real production release passes. Accordingly, this work does
not authorize real-account, real-saved-detail or real-form testing.
