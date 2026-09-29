param(
    [ValidateSet('All','BTCUSDC','ETHUSDC','BNBUSDC','HYPEUSDT')][string]$Symbol = 'All',
    [string]$OutputRoot = 'models/trend_following_preny_profile_15m/runs/four_asset_selected_partial_time_exit_v01',
    [double]$InitialEquity = 1000,
    [switch]$PrepareOnly
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $repo '.venv/Scripts/python.exe'
$context = 'models/trend_following_preny_profile_15m/runs/four_asset_stateful_effort_result_v01'
$jobs = @{
    BTCUSDC = 'storage/btcusdc/BTCUSDC-aggTrades-2024-01-05_to_2026-08-31.parquet'
    ETHUSDC = 'storage/ethusdc/ETHUSDC-aggTrades-2024-01-05_to_2026-08-31.parquet'
    BNBUSDC = 'storage/bnbusdc/BNBUSDC-aggTrades-2025-09-01_to_2026-08-31.parquet'
    HYPEUSDT = 'storage/hypeusdt/HYPEUSDT-aggTrades-2025-05-31_to_2026-08-31.parquet'
}
$symbols = if ($Symbol -eq 'All') { @('BTCUSDC','ETHUSDC','BNBUSDC','HYPEUSDT') } else { @($Symbol) }
Push-Location $repo
try {
    $env:PYTHONPATH = '.;apps/orb_live_agent/src'
    foreach ($asset in $symbols) {
        $inputFile = $jobs[$asset]
        if (-not (Test-Path -LiteralPath $inputFile)) { throw "Missing raw input: $inputFile" }
        $assetRoot = Join-Path $OutputRoot $asset.ToLowerInvariant()
        $prepared = Join-Path $assetRoot 'prepared'
        $backtest = Join-Path $assetRoot 'backtest'
        $preparedManifest = Join-Path $prepared 'manifest.json'
        if (-not (Test-Path -LiteralPath $preparedManifest)) {
            $extra = @()
            if ($asset -eq 'ETHUSDC') {
                $cache = 'tmp/cache/ethusdc/eth_preny_selected_exit_2026-08'
                $minuteCache = Join-Path $cache 'candles_1m.parquet'
                if (-not (Test-Path -LiteralPath (Join-Path $cache 'manifest.json'))) {
                    $env:SYMBOL = 'ETHUSDC'
                    $env:SESSION_TIMEZONE = 'America/New_York'
                    $env:NY_OPEN_TIME = '09:00'
                    $env:ORB_SESSION_START_TIME = '09:00'
                    $env:ORB_ENTRY_START_TIME = '09:00'
                    $env:PRE_NY_START_TIME = '01:00'
                    $env:OVERNIGHT_START_TIME = '17:00'
                    $env:SETUP_CUTOFF_TIME = '12:00'
                    & $python -m orb_live_agent.feature_cache --input $inputFile `
                        --start-date 2026-08-01 --end-date 2026-08-31 --cache-dir $cache
                    if ($LASTEXITCODE -ne 0) { throw 'ETH August cache failed' }
                }
                if (-not (Test-Path -LiteralPath $minuteCache)) { throw 'ETH August minute cache incomplete' }
                $augObs = Join-Path $assetRoot 'eth_august_observation'
                if (-not (Test-Path -LiteralPath (Join-Path $augObs 'pre_ny/summary.json'))) {
                    & $python -m models.trend_following_preny_profile_15m.strategy `
                        --input $inputFile --symbol ETHUSDC --start-date 2026-07-31 --end-date 2026-08-30 `
                        --profile-window pre_ny --confirmation-bars 2 --profile-bins 50 `
                        --value-fraction 0.70 --output-dir $augObs
                    if ($LASTEXITCODE -ne 0) { throw 'ETH August observation failed' }
                }
                $extra = @('--eth-aug-observation', $augObs, '--eth-aug-minute-cache', $minuteCache)
            }
            & $python -m models.trend_following_preny_profile_15m.prepare_selected_entries `
                --symbol $asset --context-dir $context --output-dir $prepared @extra
            if ($LASTEXITCODE -ne 0) { throw "$asset entry preparation failed" }
        }
        $manifest = Get-Content -LiteralPath $preparedManifest -Raw | ConvertFrom-Json
        $hash = (Get-FileHash -LiteralPath (Join-Path $context 'candidate_context.jsonl') -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($manifest.source_context_sha256 -ne $hash) { throw 'Context changed; use a fresh OutputRoot' }
        if ($PrepareOnly) { continue }
        if (Test-Path -LiteralPath (Join-Path $backtest 'complete.json')) {
            Write-Output "$asset already complete. Use a fresh -OutputRoot to change configuration."
            $old = Get-Content -LiteralPath (Join-Path $backtest 'sweep_manifest.json') -Raw | ConvertFrom-Json
            $fileInfo = Get-Item -LiteralPath $inputFile
            if ($old.source_size -ne $fileInfo.Length) { throw 'Raw input changed; use a fresh OutputRoot' }
            foreach ($property in $old.signals.PSObject.Properties) {
                $current = (Get-FileHash -LiteralPath $property.Value.path -Algorithm SHA256).Hash.ToLowerInvariant()
                if ($current -ne $property.Value.sha256) { throw 'Signals changed; use a fresh OutputRoot' }
            }
            if (@($old.variants | Where-Object { $_.config.initial_equity -ne $InitialEquity -or $_.config.risk_fraction -ne 0.005 }).Count) {
                throw 'Completed output used different capital/risk. Use a fresh OutputRoot.'
            }
            continue
        }
        & $python -m backtest_engine.replay_aggtrades `
            --input $inputFile --observation-dir (Join-Path $prepared 'signals') `
            --grid (Join-Path $prepared 'grid.json') --output-dir $backtest `
            --start-date $manifest.start_date --end-date 2026-08-30 `
            --initial-equity $InitialEquity --risk-fraction 0.005 --fee-bps 4 `
            --max-leverage 5 --max-entry-delay-seconds 300 --plot-equity
        if ($LASTEXITCODE -ne 0) { throw "$asset replay failed; do not analyze incomplete output" }
    }
    if (-not $PrepareOnly) {
        & $python -m models.trend_following_preny_profile_15m.prepare_selected_entries --summarize-root $OutputRoot
        if ($LASTEXITCODE -ne 0) { throw 'Comparison summary failed' }
    }
    Write-Output "Outputs: $OutputRoot"
}
finally { Pop-Location }
