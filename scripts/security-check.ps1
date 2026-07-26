$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$sensitivePatterns = @(
    "-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----",
    "gh[pousr]_[A-Za-z0-9]{20,}",
    "github_pat_[A-Za-z0-9_]{20,}",
    "AKIA[0-9A-Z]{16}",
    "(password|passwd|secret|api[_-]?key)[[:space:]]*[:=][[:space:]]*['\x22]?[A-Za-z0-9_+/@.-]{8,}",
    "https?://[^/[:space:]@:]+:[^@[:space:]/]+@",
    "cloudflared|[.]dpdns[.]org|/opt/yiwen-assistant"
)
$combinedPattern = ($sensitivePatterns -join "|")
$forbiddenPathPattern = "(^|/)([.]env|chat-auth[.]json|session[.]json|[^/]+[.]pem|id_rsa|id_ed25519)$|(^|/)[.]state/"

$violations = @()
$trackedPaths = @(git ls-files)
$forbiddenTracked = @($trackedPaths | Where-Object { $_ -match $forbiddenPathPattern -and $_ -ne ".env.example" })
if ($forbiddenTracked.Count -gt 0) {
    $violations += "Forbidden tracked paths: $($forbiddenTracked -join ', ')"
}

$workingMatches = @(
    git grep -I -n -E -e $combinedPattern -- . 2>$null |
        Where-Object { $_ -notmatch "^scripts/security-check[.]ps1:" }
)
if ($LASTEXITCODE -eq 0 -and $workingMatches.Count -gt 0) {
    $violations += "Sensitive content in tracked working tree:"
    $violations += $workingMatches
}

$historyPaths = @(git rev-list --objects --all)
$forbiddenHistory = @(
    $historyPaths |
        ForEach-Object { ($_ -split " ", 2)[1] } |
        Where-Object { $_ -and $_ -match $forbiddenPathPattern -and $_ -ne ".env.example" }
)
if ($forbiddenHistory.Count -gt 0) {
    $violations += "Forbidden paths in Git history: $($forbiddenHistory -join ', ')"
}

foreach ($commit in @(git rev-list --all)) {
    $matches = @(
        git grep -I -n -E -e $combinedPattern $commit -- . 2>$null |
            Where-Object { $_ -notmatch ":scripts/security-check[.]ps1:" }
    )
    if ($LASTEXITCODE -eq 0 -and $matches.Count -gt 0) {
        $violations += "Sensitive content in commit $($commit.Substring(0, 12)):"
        $violations += $matches
    }
}

if ($violations.Count -gt 0) {
    $violations | ForEach-Object { Write-Error $_ }
    exit 1
}

Write-Host "Security check passed: no tracked runtime state, private keys, credentials, VPS endpoints, or common token formats were found."
