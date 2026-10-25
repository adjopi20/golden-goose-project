# LTCUSDC intersection — paper v33

**Prepared locally. No VPS deployment, Git push, real order, or paper service has been started by this package.**
The operator pushes to GitHub first, pulls the reviewed commit on the existing
paper VPS, builds the image, and explicitly starts only `ltc-paper`.

## Frozen contract

- Binance USD-M perpetual **LTCUSDC**, quantity-weighted previous NY-open profile.
- Profile: previous 09:30 NY to current 09:30 NY, left inclusive/right exclusive,
  50 bins, 70% value area, using `indicator.volume_profile.build_basic_volume_profile`.
  NY daylight-saving transitions produce 23/25-hour periods, matching v31.
- Opening price: first public trade at/after 09:30, at most 60 seconds late.
- Opening must be **strictly outside** the frozen value area. Below VAL: long
  upon the first VAL touch/cross. Above VAH: short upon the first VAH touch/cross.
- Distance to frozen POC must be at least **1% of actual entry price**.
- One first-touch candidate per NY period. A rejected/missed first candidate
  consumes the opportunity. No re-entry, delta, CVD, candle/body, ATR or sizing boost.
- TP: frozen POC. SL: symmetric around actual entry, **reward:risk = 1:1**.
- Risk budget: **2.5% of current closed-trade equity**, compounding. Initial
  virtual equity: **10,000 USDC**. Notional cap: **5x equity**. Fee: **4 bps per side**.
- Time exit: first public trade at/after the following 09:30 NY.

This reuses `live_engine` feed, transactional store and order manager, and the
unchanged `trading_core.paper_execution` cash/fill/sizing engine. The small model
adapter resolves a symmetric SL against the frozen absolute POC at actual fill.

**Execution difference from v31:** v31 fills at the triggering public print and
uses the last print before the time boundary. This forward paper implementation
fills entry on the first subsequent trade with a later timestamp. TP/SL also
trigger first and fill on the next public print; time exit is at/after the boundary.
No exact historical return replication is claimed. Distance/geometry are checked
again at the actual entry; a failed check cancels that day's candidate.

Warm-up uses raw public aggTrades, never candle volume as a substitute. It rebuilds
the frozen profile and notes whether the day's first touch was already missed.
Bootstrap/repair/stale prints cannot create entries. Pending unfilled entries are
cancelled during recovery; already-open paper positions are replayed through
recovered prints so their exits remain auditable. ID gaps must be repaired before
the ledger advances. An unrecoverable gap stops the worker; it does not reset equity.
Profile boundaries tolerate 60 seconds and internal trade gaps 300 seconds, matching
the research checks. A new observation gap over 300 seconds blocks further entries.
Source-quality problems discovered later do not retroactively erase trades.

## 1. Local check, then GitHub (PowerShell)

From your local repository:

```powershell
Set-Location 'C:\Users\adjop\OneDrive\Documents\golden-goose-project'
.\.venv\Scripts\python.exe -m unittest discover -s models/mean_reversion_by_volume_profile/paper_v33 -p test_worker.py -v
if ($LASTEXITCODE -ne 0) { throw 'Paper tests failed' }
git status --short
git add -- models/mean_reversion_by_volume_profile/paper_v33
git diff --cached --stat
git diff --cached -- models/mean_reversion_by_volume_profile/paper_v33
```

After reviewing the staged files, commit/push them using your normal branch/PR
workflow. Check `git diff --cached --name-only` first: a plain `git commit` also
includes any unrelated files you previously staged. This package does not choose
or change your branch, commit other changes, or push automatically.

```powershell
git diff --cached --name-only
git commit --only -m "Add LTCUSDC intersection paper runner with 2.5 percent risk" -- models/mean_reversion_by_volume_profile/paper_v33
git push
```

The `--only` path limits this commit to the new runner even if other work was
already staged. If your branch has no upstream, follow Git's suggested push
command for your branch.

## 2. Pull and build on the paper VPS (Bash)

Use the existing **paper VPS**, which local SSH alias `devbox` identifies as
157.230.244.232. These are instructions, not a claim that connectivity or the
current server checkout was verified. Do not use the separate live ETH VPS.

```bash
ssh devbox
# cd into the existing golden-goose-project checkout on this server.
git status --short
git pull --ff-only
git log -1 --oneline
docker compose -f models/mean_reversion_by_volume_profile/paper_v33/compose.yaml config --quiet
docker compose -f models/mean_reversion_by_volume_profile/paper_v33/compose.yaml build ltc-paper
```

The build itself runs the 16 offline model tests. It copies only the public-feed
worker and required shared modules into a new image. It does not rebuild or
restart the project's other services. Docker Compose project name and volume are
dedicated to `mr-ltcusdc-paper`; no exchange credentials or host port are used.

## 3. Explicitly start (operator only)

```bash
docker compose -f models/mean_reversion_by_volume_profile/paper_v33/compose.yaml up -d --no-deps --no-build ltc-paper
docker compose -f models/mean_reversion_by_volume_profile/paper_v33/compose.yaml ps
docker compose -f models/mean_reversion_by_volume_profile/paper_v33/compose.yaml logs --tail=60 ltc-paper
docker compose -f models/mean_reversion_by_volume_profile/paper_v33/compose.yaml exec -T ltc-paper python -m models.mean_reversion_by_volume_profile.paper_v33.worker --database /data/ltcusdc.sqlite --action status
```

Do not interpret `Up` as ready to trade. Check `healthy: true`, `runtime.phase:
RUNNING`, recent `feed_age_ms`, the session's `quality: valid`, and its
`consumed`/`location` flags. Warm-up may take time. A consumed candidate waits for
the next NY period; the worker never forces an entry to prove that it works.
No email/Telegram alerts are configured. Docker health is inspection, not a
notification or an instruction to reset a ledger.

## Stop and resume

```bash
docker compose -f models/mean_reversion_by_volume_profile/paper_v33/compose.yaml stop ltc-paper
docker compose -f models/mean_reversion_by_volume_profile/paper_v33/compose.yaml up -d --no-deps --no-build ltc-paper
```

The named volume keeps equity, positions, first candidates, source identity and
append-only fill/trade journal. **Do not run `down -v` or delete the database.**
One OS writer lock prevents concurrent workers on the same ledger. Source/config
changes are rejected for an existing ledger; create a new explicit version for
rule/execution changes, retaining the old ledger. The bounded raw ring retains
the previous profile plus current session; session summaries and trade journal
are retained. If downtime exceeds Binance's public retention, inspect/reconcile
the existing position and missed period before creating any replacement account.

## What these checks do and do not establish

The unit tests verify profile parity with the shared builder, exclusion of future
data, NY DST boundaries, long/short brackets, next-print entry, fees, compounding,
duplicate/gap handling, recovery, actual-fill distance, inside-VA rejection,
stale-feed rejection, same-ms REST pagination and transactional rollback.

There has been **no Docker image build or live REST/WebSocket test on the VPS**.
Public-print execution does not model spread/BBO, market impact, funding,
liquidation, exchange lot rounding or guaranteed stop execution. The 2.5% budget
is planned price-to-SL risk before fees and gaps, not a guaranteed maximum loss.
Paper equity should be evaluated from this new forward ledger; the historical
+75.43% result is not a forward forecast. No live-money promotion is implemented.

Official API references: [aggregate trades REST](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Compressed-Aggregate-Trades-List)
and [aggregate trade stream](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Aggregate-Trade-Streams).
