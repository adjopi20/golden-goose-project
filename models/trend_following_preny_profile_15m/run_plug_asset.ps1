param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('ETHUSDC', 'BNBUSDC', 'HYPEUSDT')]
    [string]$Symbol,
    [switch]$BuildMissingCache
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$python = Join-Path $repo '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Missing Python runtime: $python" }

$jobs = @{
    ETHUSDC = @{
        Input = 'storage/ethusdc/ETHUSDC-aggTrades-2024-01-05_to_2026-07-31.parquet'
        Start = '2024-01-06'; End = '2026-07-30'
        CacheStart = '2024-01-05'; CacheEnd = '2026-07-31'
        Cache = 'tmp/cache/ethusdc/ethusdc_pre_ny_submodels_full_24h_orderflow_2024-01-05_to_2026-07-31/candles_1m.parquet'
    }
    BNBUSDC = @{
        Input = 'storage/bnbusdc/BNBUSDC-aggTrades-2025-09-01_to_2026-08-31.parquet'
        Start = '2025-09-02'; End = '2026-08-30'
        CacheStart = '2025-09-01'; CacheEnd = '2026-08-31'
        Cache = 'tmp/cache/bnbusdc/bnbusdc_cp005_2025-09-01_to_2026-08-31_orderflow_v3/candles_1m.parquet'
    }
    HYPEUSDT = @{
        Input = 'storage/hypeusdt/HYPEUSDT-aggTrades-2025-05-31_to_2026-08-31.parquet'
        Start = '2025-06-01'; End = '2026-08-30'
        CacheStart = '2025-05-31'; CacheEnd = '2026-08-31'
        Cache = 'tmp/cache/hypeusdt/hype_daily_profile_2025-05-31_to_2026-08-31/candles_1m.parquet'
    }
}

$job = $jobs[$Symbol]
$inputFile = Join-Path $repo $job.Input
$candleCache = Join-Path $repo $job.Cache
$stem = "$($Symbol.ToLowerInvariant())_preny_15m_baseline_$($job.Start)_to_$($job.End)_v01"
$obs = Join-Path $repo "models/trend_following_preny_profile_15m/runs/$stem"
$bt = Join-Path $repo "models/trend_following_preny_profile_15m/runs/${stem}_backtest"
$audit = Join-Path $repo "models/trend_following_preny_profile_15m/runs/${stem}_audit"
if (-not (Test-Path -LiteralPath $inputFile)) { throw "Missing raw input: $inputFile" }

Push-Location $repo
try {
    $env:PYTHONPATH = '.;apps/orb_live_agent/src'
    $env:SYMBOL = $Symbol
    $env:SESSION_TIMEZONE = 'America/New_York'

    if (-not (Test-Path -LiteralPath $candleCache)) {
        if (-not $BuildMissingCache) {
            throw "Minute cache missing: $candleCache. Rerun with -BuildMissingCache to build it once."
        }
        $env:NY_OPEN_TIME = '09:00'
        $env:ORB_SESSION_START_TIME = '09:00'
        $env:ORB_ENTRY_START_TIME = '09:00'
        $env:PRE_NY_START_TIME = '01:00'
        $env:OVERNIGHT_START_TIME = '17:00'
        $env:SETUP_CUTOFF_TIME = '12:00'
        $cacheDir = Split-Path -Parent $candleCache
        & $python -m orb_live_agent.feature_cache `
            --input $inputFile `
            --start-date $job.CacheStart `
            --end-date $job.CacheEnd `
            --cache-dir $cacheDir
        if ($LASTEXITCODE -ne 0) { throw "$Symbol minute cache build failed" }
        if (-not (Test-Path -LiteralPath $candleCache)) { throw "Cache build did not create $candleCache" }
    }

    if (-not (Test-Path -LiteralPath (Join-Path $obs 'prepared_profiles.jsonl'))) {
        & $python -m models.trend_following_preny_profile_15m.strategy `
            --input $inputFile --symbol $Symbol `
            --start-date $job.Start --end-date $job.End `
            --profile-window pre_ny --confirmation-bars 2 `
            --profile-bins 50 --value-fraction 0.70 --output-dir $obs
        if ($LASTEXITCODE -ne 0) { throw "$Symbol observation failed; do not analyze partial output" }
    }

    if (-not (Test-Path -LiteralPath (Join-Path $bt 'pre_ny/summary.json'))) {
        & $python -m backtest_engine.replay_aggtrades `
            --input $inputFile --observation-dir $obs `
            --start-date $job.Start --end-date $job.End `
            --output-dir $bt --initial-equity 1000 `
            --risk-fraction 0.005 --fee-bps 4 --max-leverage 5 `
            --max-entry-delay-seconds 300
        if ($LASTEXITCODE -ne 0) { throw "$Symbol backtest failed; do not analyze partial output" }
    }

    if (-not (Test-Path -LiteralPath (Join-Path $audit 'summary.json'))) {
        & $python -m models.trend_following_preny_profile_15m.audit_opportunity `
            --observation-dir $obs --backtest-dir $bt `
            --candle-cache $candleCache `
            --start-date $job.Start --end-date $job.End `
            --output-dir $audit
        if ($LASTEXITCODE -ne 0) { throw "$Symbol audit failed; do not analyze partial output" }
    }

    Write-Output "Observation: $obs"
    Write-Output "Backtest:   $bt"
    Write-Output "Audit:      $audit"
}
finally {
    Pop-Location
}
