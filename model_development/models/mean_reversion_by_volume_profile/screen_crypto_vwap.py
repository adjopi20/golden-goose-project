"""User-run pipeline: reuse old profiles; build only newly added assets."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

from observe_four_profiles import summarize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--existing-profiles-run", type=Path, required=True)
    parser.add_argument("--universe-config", type=Path, default=Path(__file__).with_name("crypto_universe_config.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    scripts = Path(__file__).resolve().parent
    existing = args.existing_profiles_run.resolve()
    old_cfg = json.loads((existing / "config_snapshot.json").read_text())
    universe = json.loads(args.universe_config.read_text())
    for key in ["profile_bins", "max_internal_gap_seconds", "max_boundary_gap_seconds"]:
        if old_cfg[key] != universe[key]:
            raise ValueError(f"Profile parameters differ; cannot reuse old run: {key}")
    if old_cfg["departure_mode"] != "ever":
        raise ValueError("Existing run must use ever departure mode")
    for symbol, path in old_cfg["assets"].items():
        if symbol in universe["assets"] and universe["assets"][symbol] != path:
            raise ValueError(f"Different source archive for reused symbol: {symbol}")
    for path in universe["assets"].values():
        if not (args.root / path).is_file():
            raise FileNotFoundError(args.root / path)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    additions = {s: p for s, p in universe["assets"].items() if s not in old_cfg["assets"]}
    daily = pd.read_csv(existing / "daily.csv")
    daily = daily.loc[daily.symbol.isin(universe["assets"])]
    manifest = json.loads((existing / "source_manifest.json").read_text())
    manifest = {s: m for s, m in manifest.items() if s in universe["assets"]}
    def run_script(name, *arguments):
        subprocess.run([sys.executable, str(scripts / name), *map(str, arguments)], check=True)
    if additions:
        config_path = out / "additional_assets_config.json"
        config_path.write_text(json.dumps(dict(universe, assets=additions), indent=2), encoding="utf-8")
        extra = out / "additional_profiles"
        print(f"Build profiles ONLY for: {', '.join(additions)}", flush=True)
        run_script("observe_four_profiles.py", "--root", args.root, "--config", config_path,
            "--departure-mode", "ever", "--start", old_cfg["requested_start"],
            "--end", old_cfg["requested_end"], "--output", extra)
        daily = pd.concat([daily, pd.read_csv(extra / "daily.csv")], ignore_index=True)
        manifest.update(json.loads((extra / "source_manifest.json").read_text()))
    if daily.duplicated(["symbol", "profile", "session_date"]).any():
        raise ValueError("Duplicate merged profile observations")
    merged = out / "combined_profiles"
    merged.mkdir(exist_ok=True)
    daily.to_csv(merged / "daily.csv", index=False)
    summarize(daily).to_csv(merged / "summary.csv", index=False)
    (merged / "config_snapshot.json").write_text(json.dumps(dict(old_cfg, assets=universe["assets"]), indent=2), encoding="utf-8")
    (merged / "source_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rr = out / "aligned_rr"
    run_script("backtest_poc_rr.py", "--root", args.root, "--profiles-run", merged,
               "--bias-mode", "aligned", "--all-valid", "--output", rr)
    run_script("rr_report.py", rr)
    run_script("observe_vwap_filters.py", "--root", args.root, "--run", rr,
               "--output", out / "vwap_filters")
    print(f"Review: {out / 'vwap_filters' / 'REPORT.md'}", flush=True)


if __name__ == "__main__":
    main()
