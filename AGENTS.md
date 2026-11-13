## Live portfolio and GitHub-first deployment — 2026-11-11

The live trading registry contains three Burst Breakout and four Quiet Breakout
submodels. BTC MVRV fixed 0/7 and four-year P5/P95 are signal-only. Pre-NY ETH
is retired from the live registry. Keep the maintained research source and
historical live journals. This section supersedes older live-stage lists.

Use one Binance worker and journal per submodel. Use three verified Futures
accounts: Burst HYPE/INJ/UNI, Quiet core HYPE Stable/ETH/UNI, and Quiet HYPE
Tail. A Binance symbol appears at most once per account so exchange-native
close-position stops have a single owner. The Binance account UID must be
distinct between groups. Each trading journal starts at 100 of its quote
asset and compounds its own realized fills, fees and funding. The current
collateral balance and exchange quantity/notional limits cap executable
sizing. Leverage controls margin use and comes from current venue settings.
If the legal minimum would exceed the budget, record the reason and skip.

Maintain the Telegram engine notifier as a separate Compose project with its
own outbox and read-only journal mounts. Keep it running while trading
workers or market services are stopped. MVRV has its own signal service and
volume. Account credentials and database state are server-only.

Publish a verified source-only commit to GitHub before building or deploying
it on Biznet. Use an explicit staged allowlist or an isolated source-only
release checkout. Review the staged paths, diff and full release tree for
secrets and artifacts before push. Include source, public model contracts and
pin manifests, build and Compose files, tests, AGENTS.md, and maintained
development and infrastructure documentation. Keep API keys, private keys,
secret mounts, env files, account settings, databases, raw data, run outputs,
caches, local reports and logs outside Git. Deployment thresholds may be
committed as aggregate configuration without raw outcomes. Use the exact
published commit for the build and image tag. Preserve server state during
rollout, verify account identity and reconcile existing positions and orders
before switching execution workers. Never arm two versions for the same
account and symbol.

## Live model package layout — 2026-11-11

Place new live strategy packages under
`live_trading/live_model/<model>/<asset>/`. Use the research model family
name and the lowercase venue symbol, including its quote asset. A single
variant can live directly in its asset package. For multiple variants of
one asset, create named variant subfolders. When variants share the decision
flow, keep one `strategy.py` and `contract.json` at the asset level and
pin each variant's configuration in the asset contract. Use independent
strategy packages in variant subfolders when their decision flow or
execution lifecycle warrants separate code.

Register every deployable submodel by a distinct ID and verify its strategy,
configuration, and pinned shared engine release. For independently traded
variants, use a distinct execution identity, fresh journal and external
account settings. The maintained research copy stays in
`model_development/models`. This layout applies to new and explicitly
restructured live packages. Package placement and service activation are
separate steps.

## Live execution checks

Use the shared venue adapter and executor for entries, native stops, and exits.
Keep signals, risk milestones, sizing and exit timing in the model policy.
Before arming a new submodel or execution release, verify the pinned sources,
native account identity, ownership of the symbol, supported position mode,
margin settings, quantity and tick filters, and journal-to-broker positions
and orders. Use a separate journal and a single execution writer per account
and symbol. A feed failure must pause new entries while the executor continues
reconciling native orders and managing exits.

For Binance USD-M one-way mode, only one active close-position stop is
allowed in the same direction. To tighten it, first confirm a native
quantity-based reduce-only stop for the live position. Confirm the old stop
is canceled, recheck exposure, then install and verify the replacement
close-position stop before removing the guard. Verify response identity,
trigger price, quantity and reduce-only or close-position flags. On rejection
or lost response, retain the confirmed protector and reconcile the known
client order ID. Record a lock only after the final stop is confirmed. Block
position additions during the transition and require a verified close-position
stop that covers the added size. Keep native protection through time-exit
submission and cancel leftover orders after checking broker state.

Persist intent and a deterministic client ID before each request. Treat a
definitive venue rejection such as a STOP POST HTTP 400 with Binance -4130
as terminal and retain the structured reason in audit. Query by client ID
after a timeout, malformed response, or restart. A missing query response
alone does not prove rejection or cancellation. Check fills and signed
position before finalizing P&L, including fees and funding, and before
admitting another trade. Keep unresolved-order and definitive stop-rejection
alerts distinct.

Regression tests for order lifecycle changes must reproduce the venue's order
constraints and exercise both directions, partial exits, add races, broker
rejection, lost acknowledgements, restart at each state change, and a stop
firing while canceled. Verify the isolated image and perform read-only broker
preflight before switching live workers. Use venue testnet to verify any new
order semantics when available, and document the tested environment.

## Operational package placement — 2026-11-03

This supersedes the earlier six-candidates-in-live placement. Research copies stay
in model_development/models. Live packages: pre_ny_eth (ETHUSDC) and btc_mvrv
(two related signal-only branches fixed_0_7 and percentile_4y_p5_p95 in one script).
Paper packages: pre_ny_hype (Binance HYPEUSDT), pre_ny_bnb (Binance BNBUSDC), and
volume_profile_reversion (LTCUSDC existing paper_v33). No submodel has an operational
copy in both stages. Promotion moves its package unchanged; state/configuration
are external, and a live account starts fresh.

Each package has one strategy.py and contract.json. BTC also carries its exact
model_config.json. Strategies verify source/configuration plus a pinned shared
engine release before use. utils/model_registry.py owns that common verification;
shared MVRV state/outbox and Telegram IO live in utils/engine/state/mvrv.py and
utils/engine/monitoring/mvrv_notify.py. BTC calculations/configuration/zone and
monitor decisions are frozen inside its one strategy, not mutable research imports.
It rejects funded/trading DBs; it has no exchange executor or portfolio actions.
ETH exposes its existing shared Futures factory. HYPE/BNB expose a factory for the
existing legacy paper manager; this is not conversion to the ETH-only lifecycle.

Current package release: utils/engine/releases/shared_engine_20261103_v3.json
SHA-256: 3ada9a4b42ef211c23fb5ad01694b6e38310d8c027e892e5329b40b18fdd66fe
It pins 52 sources; v2 lifecycle sources and all old manifests stay unchanged.
The original six offline candidate scripts, original loader/release/inventory and
tests were relocated byte-for-byte to model_development/models/legacy_frozen_candidates.
They are historical research sources, not active stage copies; old paths are retired
without runtime aliases or saved-manifest edits. Seven original frozen infrastructure
sources stay at their recorded paths for remaining compatibility callers.

LTC research selection remains v31 baseline_intersection with inspected historical
profit evidence. Existing paper_v33 is preserved, explicitly not certified by those
results. Its changed fill/exit/risk rules were not replaced by v31 during migration.
HYPE research remains ready through the existing global Phase 2 runner; its expired
fixed paper calibration was not renewed. Phase 2 ETH/BNB support is not invented.
No new study/backtest, cache build, account/state migration, notification, service
activation or deployment. Old deployment files remain compatibility configurations;
package placement is not a claim that those historical images run the new packages.

## Research copy and exclusive operational stage — user precedence, 2026-11-02

The maintained research model always remains in model_development/models. A
versioned operational copy may exist in paper_trading OR live_trading, never both
for the same submodel. Promotion moves the entire operational package (strategy,
contract and required model files); it does not remove/replace the research model.
Research-only models need not be copied into an operational stage prematurely.

User-declared stage map: live = BTC MVRV fixed_0_7 and percentile_4y_p5_p95
(Telegram signal-only, NO exchange orders), plus ETHUSDC 15m Pre-NY profile trend.
Paper = HYPEUSDT C1 and BNBUSDC C2 on Binance, plus mean-reversion volume profile.
The user delegated volume-profile selection from existing profitability evidence.
Research reference: LTCUSDC v31 baseline_intersection, supported by audited saved
v31 results and v32 sizing evaluation. This is inspected historical selection,
not a new OOS validation. Paper_v33 has different fill timing/risk and cannot inherit
v31 profitability. No operational sizing/strategy is changed. HYPE's current task
is research preparation only: no paper recalibration, market backtest or activation.

Folder placement, user-declared stage, actual service activation and integration
readiness are distinct facts. Existing six offline frozen candidates are not proof
of six live models. Use the existing notifier for BTC; do not wire those models to
the trading executor. Verify model/contract/data/engine/state/configuration bindings
per package without starting services, sending Telegram messages or deploying.
HYPE research backtests use existing phase_2/backtest.py and the maintained Pre-NY
strategy's prepare_backtest interface; no new backtest front door or market run is
authorized merely by preparing the model for a later requested backtest.

## Minimal scripts and reuse — user precedence, 2026-11-02

- Before adding code, find the maintained script/service that owns the task and update it. Repeated work or protocol tasks must use existing shared runners and calculators; never create a new backtest/observe/evaluate/sweep script per request, model, asset, parameter set or research iteration. Use configuration for variants.
- A model is its strategy decision flow. Keep entry, exit, filters and other strategy-specific decisions in the same maintained strategy script, even when a requested change is substantial. Update that script before considering a new file.
- Model-specific analysis should first extend an existing appropriate script. A separate helper is allowed only for a distinct, concrete responsibility that does not reasonably fit; keep it in that model's folder and keep script count low. Reusable analysis belongs in existing shared components. Do not combine unrelated indicators or move strategy decisions into the engine.
- Do not use temporary scripts or a second workspace implementation as a parallel maintained solution. No migration evidence, staging copies, temporary helpers or unrequested reports are to be added to golden-goose-project. Do not create new rules/protocol documents when an existing maintained document can be updated.
- This supersedes prior migration-helper/evidence practices going forward. It does not authorize deleting existing history, artifacts or data, running a study, deploying services, or changing frozen live strategies without explicit scope.

## Shared execution lifecycle v2 — 2026-11-02

The user requested fixing the lifecycle difference, beyond the previous structural
migration. create_execution in utils/engine/execution/session.py now constructs
the SAME FuturesExecutor for paper and live. Binance/FuturesOrders handles live
IO; PaperFutures implements that same submit/query/cancel/read contract locally
and reuses its order-template validator. Paper/live mode and account DB identity
are checked before processing. The scope is the existing TF-PRENY-ETH-001 contract;
BNB/HYPE/BTC/LTC and old research/paper protocols are not silently reinterpreted.
No model-specific timing, risk, TP/stop or accounting rule was changed.

Paper venue state is durable, single-writer and separate from its account journal.
It consumes complete quote/trade/funding events, requires a stable source epoch,
rejects invalid print identity/time/symbol and unverified gaps, and records event
receipts transactionally. Matching cannot use pre-submission quotes or historical
print times; market orders wait for a new native quote and consume at most its L1
size, expiring the remainder. Limits require directional trade-through, and stop
children wait for a later quote. Fee rates, production instrument metadata, leverage
and max notional are explicit inputs. Queue/deeper-book/liquidation equivalence is
not claimed. Funding accuracy requires supplied funding events; missing funding
events remain an explicit simulation coverage limitation.

Release: utils/engine/releases/shared_engine_20261102_v2.json
SHA-256: 009fde5a7ace2b5eb982228eae66ef46d58b2bbbd2bba80abd8c63126f3dc7fc
It pins 49 sources. The original v1 manifest bytes stay unchanged; its
guard must reject the changed implementation, with no silent hash substitution.
Six original frozen candidates and fourteen frozen sources remain byte-identical.
The source cache/builder and research next-print simulator are unchanged.

Verification: 113 selected tests plus 19 subtests pass, including both mode paths,
restart without resubmission, partial market/TP fills, funding, protective stop,
cancel/close reconciliation, short direction, time exit, writer lock, delayed
decision causality, account isolation, malformed/gapped input rollback, and
venue commit-failure/lost-ACK recovery without phantom orders or resubmission.
No broker/exchange, account migration, live service, market backtest, cache rebuild,
dependency installation or deployment occurred. Existing missing-NATS test limits
are not declared resolved. A model must select this supported contract, verify
its pinned v2 manifest and provide its public events; this pass starts no feed and
does not automatically wire/promote six model packages. Live accounts start fresh.

## Shared Futures lifecycle extension — 2026-11-02

The user explicitly requested fixing paper/live lifecycle divergence after the
structural migration. This supersedes migration-only restrictions for this bounded
execution extension, not model/risk rules, deployment or existing ledger upgrades.
utils.engine.execution.session.create_execution constructs the SAME FuturesExecutor
for paper/live, changing only mode, fresh account identity and venue adapter.
The existing supported Pre-NY ETH Futures contract keeps its validation, order
planning, protection, TP, reconciliation and accounting rules. This does not
silently convert ETH/BNB/HYPE paper protocols, BTC/LTC or legacy research replay.
Unsupported contracts still fail explicitly; six frozen candidates remain unchanged.

PaperFutures reuses the Binance order-template validation and query/cancel interface.
Its local venue state is durable and separate from the engine account journal;
no HTTP, credentials or real-money acknowledgement is used in paper mode. Both
modes retain independent ledger identities; paper DBs cannot be opened as live.
Public quote/trade/funding events use the shared wire contract/source epoch.
Market orders wait for a post-submission quote and fill at measured L1 price up
to displayed depth (IOC); limits need a directional print through their level.
Stops trigger from CONTRACT_PRICE prints and match their child on a later quote.
Fees and instrument metadata are explicit configuration, not inferred from P&L.
Fill/queue/depth equivalence with the exchange is NOT claimed; same lifecycle
implementation is distinct from identical simulated and actual market fills.

The earlier shared_engine_20261102_v1 manifest is immutable evidence. Its source
guard must fail after changing its pinned implementation; no old-pin exemption
or silent rebase is allowed. A new release records this implementation separately.
No model is promoted, service started, live order sent, account DB converted,
market research/backtest run, optional dependency installed or server redeployed.

## Shared paper/live engine — 2026-11-02

The user approved sharing collector/data adapters, monitoring, persistence and
operational execution sources under utils/engine. Root live_engine and infra are
retired after explicit caller migration. The next-print paper simulator is now
utils/engine/adapters/paper_execution.py; exchange IO remains a separate adapter.
Existing paper account/order orchestration and the ETH-specific Futures lifecycle
are preserved as declared legacy contracts, not falsely certified as one universal
execution behavior. Their fill, risk, order, timing and DB rules are not changed.

Public collector events have no account-mode field: one source stream/epoch can
feed independent paper/live consumers. The same production adapters are reused.
The collector now also accepts explicit shared-market ownership for a NEW feed DB;
old server-paper/local-dev identities remain valid and cannot be silently relabeled.
Existing Pre-NY session/profile enrichment is legacy behavior, not a mandatory rule
for every model. No collector, broker, subscription or service is started here.

paper_trading/models/<submodel>/ is the new paper package destination. Its strategy
and model contract travel unchanged on promotion; runtime account configuration is
external. New live accounts start fresh: never copy paper positions, balances,
pending orders, execution cursors or account DBs. Collector source identity can be
shared, but account consumer checkpoints/state remain independent. Existing DB
schema/owner/release guards remain intact. Shared storage code does not mean shared
account state. Model-specific decisions remain in one strategy script.

New model contracts explicitly pin the engine release manifest hash; the manifest
pins engine sources, its non-engine calculation dependencies and the environment.
verify_release checks them before use. This pass does not invent six new model
packages or promote/activate existing candidates. All six original frozen live
candidates and their fourteen release sources/manifest retain exact bytes; those
historical pinned copies are intentional version isolation, not active engines to
deduplicate silently. No original release identity or saved run metadata is rewritten.

Local callers, tests, build COPY sources, Compose base/overlay paths and current
documentation follow the new homes. Old module/CLI paths are retired without shims,
hash exemptions, backdating or silent source substitution. Original source bytes
and before/after/move hashes are in the workspace shared_engine_migration.json bundle.
No model rule, market study/backtest, cache rebuild, account/state upgrade, service
activation, order, dependency installation or deployment is authorized by this move.


Shared-engine verification closure — 2026-11-02:
68 source/build/doc files moved; root live_engine and infra and the superseded
live_trading/collectors/market_data and paper_trading/execution.py paths are absent.
All fourteen frozen candidate sources/release and the shared observation-cache
source digest remain unchanged. 34 complete moved Python ASTs match after explicit
import normalization; only collector run/store intentionally add shared-market
ownership and pass the configured identity to storage. No old DB is relabeled.

Operational suite: 246 passed, 2 skipped, 30 failed and 19 subtests passed. Every
remaining failure is ModuleNotFoundError for nats; no assertions were weakened or
dependencies installed. The six new shared feed/state/promotion/release tests pass.
No broker, exchange, real account, maintenance upgrade or deployment was contacted.
The old pure-runtime boundary issue is not declared resolved by moving directories.

Engine manifest: utils/engine/releases/shared_engine_20261102_v1.json
SHA-256: 9bf87cc949d634c658b2d5a0d950815beeab65633518b767551f809d830b56cb
It pins 47 source files and the current local calculation environment.
Optional NATS/cryptography service requirements remain declared separately; this
is not certification that all production-service dependencies exist locally.
Models must explicitly pin this manifest digest and call verify_release before
constructing services. Existing historical workers are compatibility consumers;
they are not silently promoted or newly certified as parity-complete packages.
The manifest explicitly records lifecycle_parity_across_modes=false. Both old
execution contracts now use maintained shared-engine source paths, but their
different lifecycle/venue restrictions are retained until a separately measured
behavior migration; merely selecting a different adapter does not convert an
unsupported existing model contract. New paper package home is created without
copying six strategies or activating anything. Live state always starts fresh.

Shared-engine final research/import checks — 2026-11-02:
31 targeted observer/Phase 2/CLI/clock tests plus 5 subtests pass after correcting
one stale observer fixture path to the maintained VWAP strategy. The fixture's
old parents[2]/model_development path accidentally doubled the folder name; no
production model logic, assertion, source/hash resume guard or cache identity was
changed. Together with the operational suite this is 277 distinct passing tests
and 24 passing subtests, 2 skips and 30 missing-NATS failures; not a clean full
repository or market validation claim. Batch/resume tests beyond these selected
cases are not newly certified. No NATS/crypto dependency was installed.

Final static checks: 358 Python sources parse, no executable imports of retired
operational packages, 107 Docker COPY sources exist, and all declared standalone/
base-overlay Compose contexts resolve to the same project root. Manifest digest
9bf87cc949d634c658b2d5a0d950815beeab65633518b767551f809d830b56cb verifies
all 47 pinned sources/environment; all fourteen historical frozen source hashes,
release manifest and measured observation-cache builder identity remain intact.
The six frozen local candidates are preserved, not promoted or duplicated into
paper packages. Existing runtime source imports follow the shared engine without
automatic order/state migration. Paper/live lifecycle parity remains explicitly
false for the legacy contracts. No service/broker/real account was contacted and
no deployment, market backtest or cache rebuild occurred.

## Remaining legacy-root relocation — 2026-11-01

Root market_data, models, analytics and apps are retired by explicit user request.
Public collector/store/broker code now lives in live_trading/collectors/market_data;
its existing server-paper constraints, DB schema/owner identifiers and calculations
remain unchanged. This is a local source/build-reference migration, not deployment.

All old models contents are moved under model_development/models. Existing maintained
Pre-NY and VWAP strategies are not overwritten: each old family is retained under
its family's legacy/ directory, including old operational support and evidence.
These are historical compatibility implementations, not new maintained strategies
or new phase runners. Other families retain their structure below the new root.
Local callers use explicit new namespaces. Historical run/release/inventory metadata,
data, credentials and state bytes are not rewritten. Old exact-path/hash resume can
fail; no shim, alias, exemption or automatic source substitution is introduced.

Cash-flow-aware daily portfolio reporting from analytics/backtest_report.py is
retained as phase_3/portfolio_report.py; it differs from Phase 2 trade metrics.
Uncalled analytics trade_log/performance_metrics are retired with exact source bytes
in the workspace migration bundle. Legacy ORB feature preparation, configuration
and triggers now live in phase_1/legacy_orb; the two old Pre-NY callers follow it.
Uncalled ORB execution/AI-state support modules are retired, not deployed. Existing
ORB data/profiles/rules/environment are moved verbatim, without loading credentials.
This does not add ORB support, migrate its pipeline to CandleContext or run a cache
build. Known absent ORB/btc_pny dependencies remain absent. The previously declared
statistical_jump_model solver source is also absent in this checkout at audit time;
no substitute solver is created. Its declared path follows the new models root.

All six frozen live candidates, their fourteen pinned source hashes and release.json
remain unchanged. Operational image entrypoints/build sources follow moved local
files; service names, volumes, DB ownership, schema and account rules stay unchanged.
No image build, service activation, order, backtest, market study, cache rebuild,
sizing application or deployment is performed. Earlier source/path retention notes
are superseded only for the explicit files and moves recorded in the workspace
remaining_roots_retirement.json bundle.


Remaining-root verification closure — 2026-11-02:
7,096 files (1,862,212,965 original bytes) were moved with per-file SHA-256
verification. Root models/market_data/analytics/apps are absent. Original data,
state, credentials and historical artifact metadata retain their exact bytes.
351 active Python sources parse; no executable import uses the retired packages.
102 local Docker COPY sources resolve. Standalone Compose contexts follow their
new depth; the two infra Phase 2 overlays retain the first/base file's unchanged
relative context. No Docker build or Compose service was executed.

The initial focused suite had 149 passes, 2 skips, 6 failures and 24 subtests.
Two stale fixture/reference failures were corrected and the three affected tests
pass (151 unique passes across those checks, not a claim of a clean full suite).
Original AST expectation hashes and release manifest stay unchanged. Frozen-origin
tests explicitly locate moved bytes and require their original release hash before
AST comparison; this is test evidence, not a runtime/resume alias or exemption.
Four remaining failures: two require the unavailable nats package; two old paper
fixtures mock paper_worker.prepare_context although SessionObserver calls its own
runtime.observer binding. The worker/observer/preparation source bytes are unchanged
by this migration; no evaluation/trading logic or test assertions were relaxed.
The previously documented pure-runtime boundary failure remains outside this pass.
Existing XIRR warnings report undefined/bracketless cash-flow roots, not new math.

The missing statistical_jump_model solver, absent ORB/btc_pny implementations and
old exact-path/hash reproducibility remain explicit limits. No solver/dependency
was installed or recreated. All fourteen frozen code hashes plus release.json
remain identical. Exact before source bytes, moves and checks are in workspace
work/model_development_restructure_20261101/remaining_roots_retirement.json and
remaining_roots_verification.json. No market study/backtest, data cleanup/cache
rebuild, model-rule change, order, service/state activation or deployment occurred.

## Research/evaluation root retirement — 2026-11-01

The user authorized eliminating root research/ and post_modelling_evaluation/.
The maintained observer/batch remain in model_development/phase_1/; duplicate
root observer implementations are retired, not replaced by forwarding shims.
Shared raw validation, measured OHLCV caching/aggregation and exact-print lookup
now live in model_development/observation_cache.py and are reused across phases.
Its source bytes/cache-builder digest are unchanged, preserving current measured
cache identity; raw/cache data are not moved or rebuilt by this relocation.

The statistical-jump calculator and its tests/docs now live in Phase 3 alongside
the other evaluators. evaluate.py remains the public dispatcher and retains native
flags, selected-run requirements and render mode. Its declared existing numerical
solver remains models/statistical_jump_model/model.py; no solver/formula/default
change or duplicate numerical implementation is added. phase identifiers such as
post_modelling_evaluation are schema values and remain stable, not folder paths.

Existing runs/ and chart_exports/ from the retired evaluation folder are moved
under model_development/phase_3/ byte-for-byte, including every run.json, Parquet
and PNG. Historical path/hash/inspection evidence is not rewritten. Select the
new run folder explicitly for later diagnostics/rendering; immutable provenance
can still name its original location. Source/read/render contracts are unchanged.

Local imports, code provenance, compatibility model launchers, tests and current
documentation follow the new homes. Old root CLI/module paths are retired. No old
hash/path resume exemption or auto-substitution is added; an old exact-path resume
may fail rather than pretend the source path is unchanged. Exact removed source
bytes, before/after hashes and artifact moves are in the workspace migration bundle.
Earlier source-path retention promises are superseded only for these approved roots.

No market study/backtest, source-data cleanup, cache rebuild, model-rule change,
checkpoint, automatic phase transition, service/state change or deployment.

## Approved trading-core retirement — 2026-11-01

The user approved utils/closed_candle_context.py for the shared closed-candle
availability/evaluation boundary and utils/market_session.py for the NY/DST clock.
Paper-execution calculations now belong to paper_trading/execution.py, reused by
research/replay and legacy paper callers. Model development still has independent
observe/backtest/evaluate phases; paper and live remain separate major stages.

The root trading_core folder is retired after caller migration. Legacy Pre-NY
research calculations are preserved as indicator/legacy_preny.py because HYPE C1
still uses its historical ATR initialization; standard MA/ATR/ADX APIs remain
separate and unchanged. The pure profile primitive is indicator/value_area.py;
it does not replace the differently specified indicator/volume_profile.py API.
Wire validation reuses the existing byte-identical pinned live contracts module.

All six local frozen live candidates already embed their model-specific indicator/
profile/session calculations. Three Pre-NY candidates each contain the same three
indicator functions (9 AST matches). They do not import mutable indicator/utils/
paper/research implementations. No second set of live submodel scripts is created.
The user accepts redeploying models from scratch; this does not authorize replacing
or redeploying the live infrastructure. Frozen source/release hashes, operational
state/configuration, service activation and server infrastructure remain unchanged.
Local import and Docker build-source references follow the new file homes only.

CandleContext currently gates Phase 1 VWAP, Phase 2 VWAP/HYPE, and Phase 3 jump;
Kelly/MC outcome diagnostics do not need it. Existing operational paper/live paths
do not automatically acquire this boundary by moving it into utils. It controls
available input, aggregation/warmup and evaluation timing, not entry/exit rules.
Main-project defaults use the new utility. Explicit external legacy cores retain
their original clock and its imports through source-checked binding; no fallback
to another checkout, timing backdating, source/hash exemption or silent resume.

Historic manifests retain original identities. Exact old-path/hash resumes can
fail after retirement; exact before bytes and the relocation map are in the
workspace migration bundle. No old results, source caches or SQLite state are
rewritten. Earlier original-path preservation promises are superseded only for
the approved files/callers in this retirement. No new trading rule, study, market
backtest, sizing application or deployment is introduced.

## Three major stages — user clarification, 2026-11-01

The approved top-level lifecycle is model_development -> paper_trading ->
live_trading. Within model_development, the existing independent Phase 1 observe,
Phase 2 backtest and Phase 3 evaluate remain intact. Major stages do not imply
an automatic transition, shared mutable live calculations, activation or deployment.

paper_trading/ is now an approved structural destination. This pass creates its
README only: no engine, state, service, model or execution rule is moved. Existing
trading_core remains unchanged while its destination map is discussed. Its legacy
indicators cannot be removed solely because three local frozen Pre-NY candidates
embed equivalent calculations: maintained Pre-NY research and existing old paper
workers still import them. Six local frozen candidates have no mutable indicator/
trading_core imports; nine embedded Pre-NY function AST comparisons match after
normalizing the finite-value helper name. All 14 frozen release hashes match.
This verifies local sources, not actual server deployment or operational migration.

CandleContext is currently a research/backtest boundary used by Phase 1 VWAP,
Phase 2 VWAP/HYPE and Phase 3 jump diagnosis. Kelly/MC outcome diagnostics do not
need it. Existing operational paper/live paths do not use this class; their own
bar/session logic is distinct. It gates data availability, aggregation, warmup
and model evaluation; it does not choose trading rules, but can prevent or delay
evaluation and therefore affects which information a model can use and when.
A reusable utils/closed_candle_context.py destination is proposed, not approved
or implemented by this note. NY session clock and this data boundary are different
components. No indicator formula is substituted or called institutionally certified.

## Phase 3 risk-tool relocation — 2026-11-01

The user authorized eliminating the root risk_research folder. Maintained Kelly,
Monte Carlo/stress and sizing calculators now live in model_development/phase_3/.
kelly.py and monte_carlo.py replace forwarding wrappers with the existing math.
Stress/scenario/block helpers and its sizing-study validation are consolidated
in monte_carlo.py. Independent volatility_sizing.py and indicator_sizing.py remain
separate; ledger_io.py owns validated common ledger input/output and receipts.
evaluate.py remains the sole documented public runner; it dispatches these local
calculators, preserves native task flags and explicit selected-run behavior.

Configs, the context data file, tool docs and tests move into Phase 3. Config
path_root is rebased to the SAME project root; internal config/data references
follow the new home. Dataset paths, cache directories, output locations, seeds,
formulas, sizing choices and calibration defaults are unchanged. Old reports,
run/evidence manifests and existing caches stay in place and are not rewritten.
New receipts capture the actual Phase 3 code identity. Old risk_research.* module
commands are retired; exact-path/hash historical reproducibility needs its
original source environment, not a silent namespace/hash exemption. Exact before
bytes/hashes and the relocation map are in the workspace migration bundle.

No study, real-data evaluation, backtest, automatic phase transition, sizing
application, deployment or live state change is authorized by this relocation.
The jump calculator/solver remain at their current declared paths; this move does
not incorporate or migrate them. Earlier no-relocation/original-caller-hash notes
are superseded only for the explicit Phase 3 import/path changes recorded here.

## Backtest consolidation — 2026-11-01

The user explicitly authorized eliminating the root backtest_engine folder.
Replay, metrics, exit-policy and legacy path-audit functions are now maintained
in model_development/phase_2/backtest.py. Phase 2 orchestration helpers remain
internal; backtest.py is the only public phase runner. Model decisions stay in
their strategy scripts. Actual fill/accounting math still reuses the unchanged
trading_core.paper_execution. Worker Config imports now go directly to that core.

All local executable callers/tests and local paper Docker build references are
adjusted. This does not activate, rebuild or deploy services. Existing remote
images/checkouts are not checked or switched. Existing support remains VWAP and
Pre-NY HYPE C1 on their declared interfaces; prepared-signal compatibility also
serves old Pre-NY workflows and the ORB app's shared metrics writer. No ETH/BNB
adapter, trading rule, sizing or new backtest is introduced.

The unused merge_candle_caches.py front door is retired. Its source and every
removed/modified file's exact original bytes/hashes are in the workspace migration
bundle, not another active engine. The uncalled research_exit_variants.py imports
absent legacy models; its exact source is archived under _archive/
backtest_retirement_20261101 because historical selected-exit evidence names it.
The moved path-audit cache helper retains its existing external legacy dependency
and does not imply those absent legacy models are now supported.

Historical run/evidence/release manifests are immutable and retain original paths
and hashes. They are historical identities, not promises that retired paths still
resolve. Exact-path resume depending on retired files or modified caller hashes
must fail explicitly; no exemption, shim or automatic source substitution is added.
Current frozen live release files/manifest and operational state stay unchanged.
Earlier instructions promising original caller hashes are superseded only for the
explicit import/path changes recorded in this migration bundle. No result artifact
or historical metadata is deleted or rewritten.

## Phase 1 implementation home — 2026-11-01

The maintained observer and batch helper now live in model_development/phase_1/.
observe.py is the public runner; observe_batch.py is its internal helper. Phase 2
imports the maintained schema/instrument helpers. The shared validated cache stays
in research/observation_cache.py because Phase 1/2/3 use the same implementation.

research/observe.py and research/observe_batch.py remain byte-for-byte frozen
compatibility sources for historical callers and exact-hash resume. They are not
parallel maintenance targets. Existing model compatibility helpers and historical
evaluation callers remain unchanged; new Phase 1 commands use the maintained runner.
No model/core path substitution or hash exemption is added to resume. Continue an
old run with its original paths; moving a model is not permission to resume it.

Relocation changes only bootstrap, explicit imports, project-root depth and cache
provenance path. Execution, validation, schema, fees, configuration and resume
checks are unchanged. Verification is static AST/source/hash comparison and
import/CLI-help checks only. No research, replay/backtest, data/state movement,
artifact deletion, deployment or automatic phase transition.

﻿# Current scope precedence

## Active task scope — migration only, clarified 2026-11-01

The user explicitly clarified that this task is migration only. Continue checking
structure, paths, imports, compatibility references and documentation. Do not
interpret missing Phase 1/ETH/BNB adapters, dynamic sizing, partial-exit support,
or any engine limitation as authorization to implement a new capability. Do not
choose/reconfirm trading or sizing rules, run new research/backtests, or alter
model behavior as part of this migration. Static source/path/hash checks are
appropriate. Previous synthetic parity results are historical evidence, not new
runs requested by this clarification. Preserve original artifacts, exact-hash
legacy/resume sources and operational service/state paths.

# Global research protocol

Before research/model/observe work, read the maintained global protocol folder:
`C:/Users/adjop/OneDrive/Documents/ChatGPT/GOLDEN GOOSE PROJECT/research_designs/research_protocol/README.md`
and its PROTOCOL.md, OUTPUT_CONTRACT.md, RESEARCH_CONVENTIONS.md and
TECHNICAL_INDICATOR_STANDARD.md. These are the single maintained reference.

- New models: one folder under this project's `models/<model_name>/`.
- One script per strategy decision flow; shared indicators/execution remain reusable.
- Exactly one strategy MD per model; update it for all rules and iterations.
  A second MD only if a demonstrated system content limit prevents continuation.
- Record every iteration ITER-001 etc. Preserve its parent, rules/config, evidence
  or not-run status. User-selected milestones alone become numbered checkpoints
  CHECKPOINT-001 etc.; preserve IDs and references in the same strategy MD.
- Protocol v4: fixed-risk fee-only OBSERVE leads with all unique closed trades
  in the declared development window, gross/net. SELECTION uses internal rolling
  8-month train / 2-month validation, step 2. FINAL OOS is separately reserved
  before inspection or consists of new data after freeze; fold labels alone do not
  imply training or untouched OOS. No forced terminal close; open outcomes excluded.
- One folder per run, master Parquet plus minimal JSON metadata; no automatic
  derivative reports. Typed position-block evidence records qualifying setups
  blocked by existing positions; no general rejection rationale log.
- The VWAP model is `models/mean_reversion_by_vwap`. Its shared research APIs
  currently come from the executor checkout named in its sole strategy MD.
  Do not silently substitute/migrate this project's legacy indicator formulas.

- Any outcome-driven asset/timeframe/parameter/filter/direction/exit/sizing choice is selection. Keep real tail events in primary accounting; do not patch a filter solely to avoid a known rare loss. Preserve development/OOS boundaries and inspection history. See canonical PROTOCOL.md v4; CLI stage enforcement/automatic selection is not implied by this documentation.


## Approved migration path precedence — 2026-11-01

New research model destination: model_development/models/<model_name>/.
New public entrypoint destinations: model_development/phase_1/observe.py,
model_development/phase_2/backtest.py, model_development/phase_3/evaluate.py.
The initial wrappers/dispatcher reuse existing implementations and native task
contracts. Dynamic Phase 2 --model/data/grid and unified Phase 3 --run/Parquet
adaptation are NOT implemented by this foundation. Existing root paths remain
compatibility implementations. Do not delete/move them or their artifacts yet.

live_trading/ is a separate operational destination. Six model decision/calculation
scripts will be frozen after dependency audit and parity; current inventory is NOT
a frozen release. No mutable research imports in frozen live model calculations.
Shared collector/execution/state versions must also be pinned. Preserve signal,
paper and real modes, state/accounts, historical formulas and deployment paths.
No server, service, credentials or operational config was changed here.

The single canonical protocol remains the folder linked above. The new
model_development/protocol/README.md is only a pointer. This path-precedence
record supersedes old destination paths above; evidence/accounting rules remain.


## Phase 2 dynamic adapter — 2026-11-01

The initial --model limitation above is superseded for the explicit single-source
prepared-intention adapter. model_development/phase_2/backtest.py now dynamically
loads a script exposing prepare_backtest(request). HYPE C1 is migrated; its old
strategy.py --backtest command delegates to this global adapter. Candidate rules,
features/calibration and stop/exit remain in the model. Execution/fees/compounding
still use backtest_engine.replay_aggtrades.run_phase2 and existing paper_execution.

This is NOT universal model support: VWAP's stateful observer/absolute target and
uncapped holding horizon need their own shared adapter. General grids, multiple
raw files/assets and the full global multi-job contract remain pending. Unknown
flags and observer-only models fail explicitly. Phase 3 Parquet adaptation and
six-script live consolidation are still pending. Live/server paths stay intact.

Eight foundation/dynamic-adapter tests and six HYPE Phase 2 tests passed, including
the 205-day synthetic CLI. Native-engine parity covers fill/fees/quantity/equity;
no full-market replay or new performance evidence. Canonical details remain in
the single PROTOCOL.md implementation record; no protocol copy is created here.


## Shared research lifecycle — 2026-11-01

Supersedes the earlier requirement for a separate VWAP adapter: one Session in
model_development/lifecycle.py now drives both prepared intentions (HYPE C1) and
prepared-candle models (VWAP). The same global Phase 2 --model front door loads
the supported interface. Strategies/calibration stay in their existing script;
all sizing/fill/partial/fee/exit mechanics still use existing paper_execution.

Model owns pending/accepted setup state; Portfolio owns executed positions and
equity. State identities are checked. Existing accept_fill and record_exit plus
optional on_execution receive actual entry/partial/exit and rejected/cancelled
events. No same-print entry/exit or backdated delayed decision. Absolute model
targets and uncapped holding are preserved. Submodel equity stays independent.
New output retains three files with per-account identities/metrics and labelled
equity curves. Failed chart/master generation cannot become a complete run.

The raw data remain strictly validated and chronological. No live/paper service,
model formula, cache builder, historical result or artifact is changed. Phase 1
still uses its existing implementation until explicit migration/parity. General
grids/assets/multi-file, dynamic trailing/limit protocols, persistent state and
Phase 3 Parquet adapters remain unsupported. This is research infrastructure,
not proof of market edge, exchange fills or deployment readiness.


## Phase 2 batch/grid — 2026-11-01

backtest.py is the main orchestrator: --batch --model <script> --input <assets...>
--grid-file <json>. Internal batch.py reuses the same model loader, lifecycle,
execution engine and metrics, with independent equity per asset/config/submodel.
No model-specific backtest runner or global HYPE/VWAP rule is added.

One new run folder still contains only trades.parquet, run.json, equity_curve.png.
Per-account IDs/metrics and model/execution grid configs are recorded; the image
separates P&L currency units and preserves each account's coverage. No pooling,
automatic selection, extra job folders or artifact cleanup. Failed jobs/charts/
source checks leave the parent failed; prior completed rows remain explicit.

Current limits: one selected model and one strict raw file per instrument. Multiple
assets/configurations are supported; same-asset split files, resume/record-gap,
general execution costs, trailing/limit protocols and unadapted legacy models are
still unsupported. Phase 1/live and Phase 3 migration status otherwise unchanged.
Canonical methodology is still the single protocol folder referenced above.


## Independent phases and explicit Phase 3 --run — 2026-11-01

Each phase has its own user-invoked orchestrator: Phase 1 observe.py, Phase 2
backtest.py, Phase 3 evaluate.py. There is no automatic transition between them.
The user decides whether/when to run any phase or apply a sizing estimate.

evaluate.py now accepts an explicitly selected completed run.json/trades.parquet
through --run for kelly/monte-carlo. Account/configuration/submodel selection must
be unambiguous, including batch accounts. Source outcomes and their gross/net/
fees remain intact and hash-checked. Computation never starts observation,
backtest or trading. Other task --run adapters remain unsupported; legacy flags
after -- (including jump render --run) still work.

Kelly and MC reuse factored helpers in the existing risk_research calculators;
their numerical kernels and legacy CLIs remain compatible. New exit-UTC calendar
bins retain multiple trades/day; this is not parity with old NY-entry-session
studies. Phase 2 MC defaults to observed net account-return pairs. Phase 1 MC
requires explicit capital/risk/leverage for a labelled conditional ledger study,
not a backtest. No automatic policy export/application or model promotion.

This new output is one NEW folder with run.json only: results/parameters/seed,
source/code identities, account, gross/net/fee totals, censoring, scope and status.
No raw dataset, source copy, paths, CSV or extra reports are generated. Live
sources/services and original runs are untouched. Canonical details remain in
the single maintained PROTOCOL.md/OUTPUT_CONTRACT.md, not a copied protocol here.


## Phase 3 jump --run precedence — 2026-11-01

Statistical jump now also accepts the explicit selected Phase 1/2 --run contract
through model_development/phase_3/evaluate.py, reusing the existing calculator
and shared run_input reader. Require explicit market/provenance/window/inspection
arguments and exactly one account; reject bounds dropping selected closed trades.
Retain unknowns, source fields, gross/net/fees, hashes and censoring. Phase 2 chart
uses original capital/monetary P&L at observed quantities; Phase 1 remains R.
Default solver/formulas and legacy CLI/render stay compatible. No automatic
phase transition, sizing application, entry filter or live migration. Other
non-Kelly/MC/jump tasks still use native inputs. Canonical PROTOCOL.md and
OUTPUT_CONTRACT.md record the output/coverage/cache contracts and evidence limits.


## Local live consolidation precedence — 2026-11-01

live_trading/live_model now contains six single-file OFFLINE candidates for
Pre-NY ETH/BNB/HYPE, BTC fixed_0_7/percentile_4y_p5_p95 and LTC paper_v33.
Calculations are isolated from mutable research imports. Shared execution/state/
public collector behavior is pinned once under frozen_v1, with code/environment
checks in release.json/load_model. Existing operational callers and all 57
inventoried source files remain unchanged; no service or deployment migration.
HYPE operational calibration binding, collector/service wiring, complete shadow/
market parity and state migration remain pending. BTC candidates retain original
50% source-total allocation in dedicated DBs; legacy dual-account DB fingerprints
and LTC code locks must not be bypassed. Preserve research/live separation and
independent phase invocation. See canonical PROTOCOL.md's newest record and
live_trading/README.md for exact test scope and remaining limits. Legacy -W error
regression has existing SQLite cleanup-warning failures; do not claim it clean.


## VWAP research migration precedence — 2026-11-01

Maintained VWAP home: model_development/models/mean_reversion_by_vwap/ (one script,
one full-history strategy MD). Main-project shared research APIs are
indicator.research.vwap, indicator.research.cvd and trading_core.candles. Global
Phase 1/2 default shared-core to the main project; explicit old cores still work.
Legacy indicator.vwap is unchanged. Old VWAP model/grid/tests and executor sources
stay frozen for exact-hash historical resume, not active parallel maintenance.
Do not replace them with a shim or silently permit cross-path/source resume.
See canonical PROTOCOL.md's newest migration record. Other indicators/models are
not automatically relocated. No deployment, artifact deletion or new checkpoint.


## Pre-NY/indicator migration precedence — 2026-11-01

Maintained Pre-NY research family: model_development/models/
trend_following_preny_profile_15m/strategy.py, one decision/calibration script and
one full-history RULES.md. The global Phase 2 runner calls prepare_backtest; HYPE
C1 remains the supported frozen configuration. No new Phase 1 or ETH/BNB adapter.
Shared standard APIs now use indicator.research.moving_average / atr / adx /
order_flow; trend is optional composition. Keep Pre-NY's legacy ATR unchanged.
Old models/runtime/indicators/tests remain frozen compatibility evidence; all live
sources and artifacts stay intact. See newest canonical PROTOCOL.md record.
