# Phase 0 — capture the actual paper deployment

Architecture reference: [TRADING_PLATFORM_V1.md](../../docs/architecture/TRADING_PLATFORM_V1.md).

This helper records the current running image identities and runtime source hashes, takes consistent SQLite backups, and exports small reconstruction inputs for the latest stored NY session. It does not restart/build containers, warm up the feed, submit trades, or edit the active database. It uses Python's SQLite online backup API while collection continues.

## Server command

After committing/pushing these helper files and pulling that commit on the server, run from the repository root:

```bash
bash infra/ops/capture_phase0.sh
```

The output is `/tmp/golden-goose-phase0/phase0_<UTC timestamp>_<PID>.tar.gz`. The command prints its exact path and SHA256. Two backups are also left in the containers' persistent `/data/phase0_*` folders. No copies are deleted automatically.

Download the archive to the laptop; a copy on the VPS is not an off-host backup. In laptop PowerShell, replace the filename with the printed name:

```powershell
scp root@157.230.244.232:/tmp/golden-goose-phase0/phase0_EXACT_NAME.tar.gz ./phase0_EXACT_NAME.tar.gz
Get-FileHash .\phase0_EXACT_NAME.tar.gz -Algorithm SHA256
```

Preserve private permissions. The archive contains virtual account state and recent logs; it is an operational artifact, not a file to commit to Git. Store it under an ignored `tmp/` or `reports/` directory when placed inside the repository.

## Evidence and interpretation

- Host Git SHA is separate from actual running image ID. Pulling newer helpers does not make an old container run newer strategy code.
- `source_inventory.json` fingerprints relevant source/configuration files inside each container. This detects local/server differences even when image tags have been reused.
- `python_packages.json` records installed versions. No environment variables or arbitrary process arguments are exported.
- `snapshot.sqlite` is a complete integrity-checked paper database backup, including journal, positions, intentions, coverage and saved profiles.
- `paper_state.json` is a readable state export from the backup.
- `fixture_index.json` and compressed minute files include the latest stored session's evaluations, frozen profiles and all available causal indicator warmup. History is not arbitrarily truncated because recursive indicators may change.
- A fixture reconstructs inputs from the current DB. It is not proof that those inputs exactly match what was available at the historical decision; later backfill/corrections can matter.
- Raw trades are not stored by the current worker. This capture cannot demonstrate exact stop/fill replay, bid/ask realism or recovery across a historical tick gap.
- `SESSION_LOCKED` and `NOT_READY` are captured as evidence. They are not silently converted into successful evaluator samples.
- A missing session/profile is reported, rather than fabricated. A failed helper produces a failed/incomplete manifest; do not analyze it as a completed capture.

## Laptop inventory

Use a new output folder for every capture:

```powershell
.\.venv\Scripts\python.exe infra/ops/phase0_snapshot.py `
  --root . --inventory-only --output-dir "tmp/phase0/local_2026-10-04"
```

## Completion gate

Phase 0 is complete only after the server artifact is received and checked:

1. Both manifests complete; copied archive hash matches the server hash.
2. Runtime/container identities and local differences are documented.
3. Both database backups pass integrity checks; restore is tested on a separate copy.
4. Existing candidate/lock/rejection behavior is documented from saved evaluations/intents.
5. Usable evaluator fixtures are selected and baseline expectations frozen against the correct deployed code.
6. Missing raw-event evidence is recorded as a limitation; execution recovery gets dedicated controlled tests later.

Preparing the tool and passing synthetic tests do not mean the server deployment has passed these gates.
