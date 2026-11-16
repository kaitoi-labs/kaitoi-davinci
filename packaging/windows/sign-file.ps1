#Requires -Version 7.3
# sign-file.ps1
# Sign one Windows binary in place with the SSL.com eSigner certificate, then
# fail unless Windows reports a valid, timestamped signature from the expected
# publisher.
#
# CI runs this for kaitoi.exe, and Inno Setup runs it through its SignTool hook
# for the uninstaller and the finished setup. It reads:
#   ES_USERNAME, ES_PASSWORD, ES_CREDENTIAL_ID, ES_TOTP_SECRET  eSigner account
#   CODESIGNTOOL_DIR, CODESIGNTOOL_JAR                          CodeSignTool install
#   WINDOWS_SIGNER_NAME                                         expected certificate name
#
# CodeSignTool exits 0 even when it fails to sign, so its exit code is never
# trusted. The Authenticode check at the end is what decides success.
#
# -VerifyOnly skips signing and only checks the existing signature.

param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"
# Pass arguments to java.exe with standard quoting, so a password containing
# quotes or spaces reaches CodeSignTool intact.
$PSNativeCommandArgumentPassing = "Standard"

function Get-RequiredEnv {
    param([string]$Name)

    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "$Name is not set"
    }
    return $value
}

function Assert-KaitoiSignature {
    param(
        [string]$File,
        [string]$ExpectedSigner
    )

    $signature = Get-AuthenticodeSignature -LiteralPath $File
    if ($signature.Status -ne "Valid") {
        throw "Signature on $File is $($signature.Status): $($signature.StatusMessage)"
    }
    $nameType = [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName
    $signer = $signature.SignerCertificate.GetNameInfo($nameType, $false)
    if ($signer -ne $ExpectedSigner) {
        throw "Signature on $File is from '$signer', expected '$ExpectedSigner'"
    }
    if (-not $signature.TimeStamperCertificate) {
        throw "Signature on $File has no timestamp"
    }
    $timestamper = $signature.TimeStamperCertificate.GetNameInfo($nameType, $false)
    Write-Host "Verified signature on ${File}: $signer, timestamped by $timestamper"
}

# GitHub masks only the exact secret strings, so anything printed from
# CodeSignTool is scrubbed of every credential first, including the
# whitespace-stripped TOTP seed.
function Hide-Secrets {
    param(
        [string]$Text,
        [string[]]$Secrets
    )

    foreach ($secret in $Secrets) {
        $Text = $Text.Replace($secret, "***")
    }
    return $Text
}

# eSigner derives a one-time code from ES_TOTP_SECRET for every signing, and
# may refuse a code it has already accepted. A second signing in the same
# 30-second window waits for the next one, so the wait is never above 31 s.
function Wait-ForFreshOneTimeCode {
    param([string]$MarkerFile)

    if (-not (Test-Path -LiteralPath $MarkerFile)) {
        return
    }
    $lastWindow = [int64](Get-Content -LiteralPath $MarkerFile -Raw)
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    if ([int64][math]::Floor($now / 30) -eq $lastWindow) {
        $wait = (($lastWindow + 1) * 30) - $now + 1
        Write-Host "Waiting $wait s for a fresh eSigner one-time code"
        Start-Sleep -Seconds $wait
    }
}

$expectedSigner = Get-RequiredEnv "WINDOWS_SIGNER_NAME"
$file = (Resolve-Path -LiteralPath $Path).Path

if ($VerifyOnly) {
    Assert-KaitoiSignature -File $file -ExpectedSigner $expectedSigner
    exit 0
}

$username = Get-RequiredEnv "ES_USERNAME"
$password = Get-RequiredEnv "ES_PASSWORD"
$credentialId = Get-RequiredEnv "ES_CREDENTIAL_ID"
$rawTotpSecret = Get-RequiredEnv "ES_TOTP_SECRET"
# Strip the same ASCII whitespace as the workflow's add-mask (LC_ALL=C tr -d '[:space:]'),
# so the masked form and the form passed to CodeSignTool always match.
$totpSecret = $rawTotpSecret -replace "[ \t\r\n\f\v]", ""
# Each secret, plus each line of a multi-line secret, longest first.
$secrets = @($username, $password, $credentialId, $rawTotpSecret, $totpSecret) |
    ForEach-Object { $_; ($_ -split "\r\n|\r|\n") | ForEach-Object { $_.Trim() } } |
    Where-Object { $_ } |
    Sort-Object -Property Length -Descending
$toolDir = Get-RequiredEnv "CODESIGNTOOL_DIR"
$jar = Get-RequiredEnv "CODESIGNTOOL_JAR"
$java = if ($env:JAVA_HOME) { Join-Path $env:JAVA_HOME "bin\java.exe" } else { "java" }

# CodeSignTool picks the signature format from the file extension. Inno Setup
# hands over its uninstaller as uninst.e32.tmp, so anything that is not an
# .exe or .dll is signed as a temporary .exe copy and written back.
$extension = [System.IO.Path]::GetExtension($file).ToLowerInvariant()
$workDir = $null
$target = $file
if ($extension -notin @(".exe", ".dll")) {
    $workDir = Join-Path ([System.IO.Path]::GetTempPath()) ("kaitoi-sign-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $workDir | Out-Null
    $target = Join-Path $workDir "to-sign.exe"
    Copy-Item -LiteralPath $file -Destination $target
}

$tempRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [System.IO.Path]::GetTempPath() }
$windowMarker = Join-Path $tempRoot "kaitoi-esigner-last-window"

try {
    Wait-ForFreshOneTimeCode -MarkerFile $windowMarker
    Set-Content -LiteralPath $windowMarker -Value ([math]::Floor([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() / 30))

    $arguments = @(
        "-jar", $jar, "sign",
        "-username=$username",
        "-password=$password",
        "-credential_id=$credentialId",
        "-totp_secret=$totpSecret",
        "-input_file_path=$target",
        "-override"
    )

    # CodeSignTool reads conf\code_sign_tool.properties from the working
    # directory, and writes Java warnings to stderr that are not failures.
    # Its stdin is closed, so an interactive prompt fails instead of hanging.
    Push-Location -LiteralPath $toolDir
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @($null | & $java @arguments 2>&1 | ForEach-Object { "$_" })
    } finally {
        $ErrorActionPreference = $previousPreference
        Pop-Location
    }

    $output | ForEach-Object { Write-Host (Hide-Secrets -Text $_ -Secrets $secrets) }
    if ($output | Where-Object { $_ -match "^\s*Error" -or $_ -match "Exception" }) {
        throw "CodeSignTool did not sign $file"
    }

    Assert-KaitoiSignature -File $target -ExpectedSigner $expectedSigner

    if ($target -ne $file) {
        Copy-Item -LiteralPath $target -Destination $file -Force
    }
} finally {
    if ($workDir) {
        Remove-Item -LiteralPath $workDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
