param(
    [ValidateSet('prepare', 'all')][string]$Stage = 'all',
    [ValidateSet('BTCUSDC','ETHUSDC','BNBUSDC','HYPEUSDT')]
    [string[]]$Symbols = @('BTCUSDC','ETHUSDC','BNBUSDC','HYPEUSDT')
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $repo '.venv/Scripts/python.exe'
$inputs = @{
    BTCUSDC = 'storage/btcusdc/BTCUSDC-aggTrades-2024-01-05_to_2026-08-31.parquet'
    ETHUSDC = 'storage/ethusdc/ETHUSDC-aggTrades-2024-01-05_to_2026-07-31.parquet'
    BNBUSDC = 'storage/bnbusdc/BNBUSDC-aggTrades-2025-09-01_to_2026-08-31.parquet'
    HYPEUSDT = 'storage/hypeusdt/HYPEUSDT-aggTrades-2025-05-31_to_2026-08-31.parquet'
}
$arguments = @('-m','models.trend_following_preny_profile_15m.replay_challenger',
    '--study-dir', (Join-Path $PSScriptRoot 'runs/four_asset_plug_study_v01'),
    '--output-dir', (Join-Path $PSScriptRoot 'runs/four_asset_preny_delta_challenger_v01'),
    '--stage', $Stage)
foreach ($symbol in $Symbols) {
    $inputFile = Join-Path $repo $inputs[$symbol]
    if (-not (Test-Path -LiteralPath $inputFile)) { throw "Missing raw file: $inputFile" }
    $arguments += @('--dataset',$symbol,$inputFile)
}
Push-Location $repo
try {
    $env:PYTHONPATH = '.'
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Challenger failed; do not analyze incomplete output. Rerun resumes completed raw replays.' }
}
finally { Pop-Location }
