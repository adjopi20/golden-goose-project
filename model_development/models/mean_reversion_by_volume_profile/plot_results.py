"""Render descriptive charts from completed observation output."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    out = args.run
    common = json.loads((out / "common_period_summary.json").read_text())
    df = pd.read_csv(out / "daily_observations.csv")
    valid = df.loc[df.status == "valid"].copy()
    subgroup_rows = []
    for (symbol, position), group in valid.groupby(["symbol", "initial_value_area_position"]):
        eligible = group.loc[group.poc_reached.notna(), "poc_reached"]
        subgroup_rows.append(dict(symbol=symbol, initial_value_area_position=position,
                                  eligible_days=len(eligible), reached_days=int(eligible.sum()),
                                  reached_pct=100 * eligible.mean() if len(eligible) else np.nan))
    pd.DataFrame(subgroup_rows).to_csv(out / "value_area_groups.csv", index=False)
    plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(3, 1, figsize=(11, 12), layout="constrained")
    colors = ["#2563eb", "#f97316", "#16a34a"]
    symbols = list(common)
    metric_keys = ["opposite_direction_at_17", "counter_move_025r", "poc_reached"]
    labels = ["Opposite direction at 17:00", "Counter-move >= 0.25 ORB", "POC crossed"]
    x = np.arange(len(symbols))
    for k, (key, label) in enumerate(zip(metric_keys, labels)):
        values = [common[s].get(key, {}).get("pct") for s in symbols]
        if all(v is not None for v in values):
            axes[0].bar(x + (k - 1) * .24, values, width=.24, label=label)
    axes[0].set_xticks(x, symbols)
    axes[0].set_ylim(0, 100)
    axes[0].set_ylabel("% eligible sessions")
    if common[symbols[0]]["valid_days"]:
        s = common[symbols[0]]
        axes[0].set_title(f"Same valid dates across markets: {s['first_valid_session']} to {s['last_valid_session']} (n={s['valid_days']})")
        axes[0].legend(loc="upper left", bbox_to_anchor=(0, 1.27), ncol=3, fontsize=8)
    else:
        axes[0].set_title("No common valid period")
    for symbol, color in zip(symbols, colors):
        group = valid.loc[(valid.symbol == symbol) & valid.counter_move_025r.notna()]
        counts = group.first_counter_move_hour_ny.value_counts().reindex(range(1, 17), fill_value=0)
        axes[1].plot(range(1, 17), counts * 100 / max(1, len(group)), marker="o", label=f"{symbol} (n={len(group)})", color=color)
    axes[1].set_title("First counter-move >= 0.25 ORB: entire available history")
    axes[1].set_xticks(range(1, 17))
    axes[1].set_xlabel("Hour in New York (DST-aware)")
    axes[1].set_ylabel("% eligible sessions with FIRST hit")
    axes[1].legend()
    for symbol, color in zip(symbols, colors):
        group = df.loc[df.symbol == symbol].copy()
        group["year"] = group.session_date.str[:4]
        annual = group.groupby("year").status.apply(lambda values: 100 * values.eq("valid").mean())
        axes[2].plot(annual.index, annual.values, marker="o", label=symbol, color=color)
    axes[2].set_title("Usable sessions / all requested dates: archive availability + conservative quality filter")
    axes[2].set_ylabel("% requested dates usable")
    axes[2].set_ylim(0, 105)
    axes[2].legend()
    fig.suptitle("ORB 09:30 previous day to 01:00 NY | Observe 01:00 to 17:00 NY", fontsize=13)
    fig.savefig(out / "observation_summary.png", dpi=160)
    plt.close(fig)
    report = out / "REPORT.md"
    report.write_text(report.read_text(encoding="utf-8") + "\n## Visualisasi\n\n![Ringkasan observasi](observation_summary.png)\n\nvalue_area_groups.csv memisahkan crossing POC berdasarkan posisi awal di atas, di dalam, atau di bawah value area. Perbedaan jarak awal ke POC juga memengaruhi peluang crossing.\n", encoding="utf-8")


if __name__ == "__main__":
    main()
