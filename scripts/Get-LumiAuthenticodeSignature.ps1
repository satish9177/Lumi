param(
  [Parameter(Mandatory = $true)]
  [string]$LiteralPath
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

try {
  $signature = Get-AuthenticodeSignature -LiteralPath $LiteralPath
  $signer = $null
  if ($null -ne $signature.SignerCertificate) {
    $signer = [ordered]@{
      subject = $signature.SignerCertificate.Subject
      issuer = $signature.SignerCertificate.Issuer
      thumbprint = $signature.SignerCertificate.Thumbprint
      notBefore = $signature.SignerCertificate.NotBefore.ToUniversalTime().ToString('o')
      notAfter = $signature.SignerCertificate.NotAfter.ToUniversalTime().ToString('o')
    }
  }

  [ordered]@{
    status = $signature.Status.ToString()
    signerCertificate = $signer
    timeStamperCertificatePresent = ($null -ne $signature.TimeStamperCertificate)
  } | ConvertTo-Json -Compress -Depth 4
} catch {
  [Console]::Error.WriteLine('Authenticode inspection failed.')
  exit 1
}
