# LEGACY accumulation smoke helper, retained for reproduction.
# Current model: use docker compose in live_mvrv/ instead.
param(
    [ValidateSet("cash", "btc")]
    [string]$Position = "cash"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$dataDir = Join-Path $PSScriptRoot "data"
$dockerfile = Join-Path $PSScriptRoot "Dockerfile"
$image = "golden-goose-btc-monitor:local"

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

docker build `
    --quiet `
    --file $dockerfile `
    --tag $image `
    $projectRoot
if ($LASTEXITCODE -ne 0) {
    throw "Docker image build failed."
}

$dataMount = "type=bind,source=$dataDir,target=/app/models/bitcoin_onchain_cycles/data"
docker run `
    --rm `
    --shm-size=256m `
    --mount $dataMount `
    $image `
    --position $Position `
    --timeout 45
if ($LASTEXITCODE -ne 0) {
    throw "Daily monitor failed."
}

Write-Host ""
Write-Host "Latest signal:"
Get-Content -LiteralPath (Join-Path $dataDir "latest_signal.json")
