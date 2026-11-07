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

Layout: collectors/ holds public input, adapters/ holds paper fill or exchange IO,
execution/ owns the existing order/account lifecycles, state/ owns persistence,
monitoring/ owns health/alerts, operations/ owns guarded maintenance and deploy/
holds local build/service templates. Importing any module starts no service.

New paper packages: paper_trading/models/<submodel>/strategy.py plus contract.json
and any existing rules/calibration documents. Pin both the engine manifest path
and its SHA-256 in contract.json; call utils.engine.release.verify_release with
that expected digest before constructing services. Promotion moves the package
without changing strategy/contract bytes; choose a NEW live account/database and
the exchange adapter in external operational configuration. No promotion tool or
automatic activation is supplied. A model chooses only an explicitly supported
execution contract; moving modules does not create new model/venue support.

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

