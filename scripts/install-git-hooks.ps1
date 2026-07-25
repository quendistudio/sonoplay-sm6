# Install repo git hooks (pre-push private-data scan).
# Run once from the repo root:
#   .\scripts\install-git-hooks.ps1

$ErrorActionPreference = 'Stop'
$root = git rev-parse --show-toplevel
if (-not $?) { throw 'Not inside a git repository.' }
Set-Location $root

$hooksPath = 'scripts/githooks'
if (-not (Test-Path -LiteralPath (Join-Path $root $hooksPath 'pre-push'))) {
    throw "Missing hook at $hooksPath/pre-push"
}

git config core.hooksPath $hooksPath
Write-Host "Configured core.hooksPath=$hooksPath"
Write-Host 'pre-push will run: python scripts/check_private_data.py'
