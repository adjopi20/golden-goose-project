"""Closed-trade performance in R, including all exits before 17:00 NY."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from rr_report import LABELS


def streak(mask):
    longest = current = 0
    for value in mask:
        current = current + 1 if value else 0
        longest = max(longest, current)
    return longest


def divide(a, b):
    return float(a / b) if b else None


def stats(values):
    r = np.asarray(values, dtype=float)
    wins, losses = r[r > 1e-12], r[r < -1e-12]
    curve = np.r_[0., np.cumsum(r)]
    dd = np.maximum.accumulate(curve) - curve
    profit, loss = float(wins.sum()), float(-losses.sum())
    aw = float(wins.mean()) if len(wins) else 0.
    al = float(-losses.mean()) if len(losses) else 0.
    return dict(trades=len(r), winning_closes=len(wins), losing_closes=len(losses),
                breakeven_closes=len(r) - len(wins) - len(losses),
                win_rate_pct=divide(100 * len(wins), len(r)), total_r=float(r.sum()),
                mean_r=float(r.mean()) if len(r) else None,
                median_r=float(np.median(r)) if len(r) else None,
                gross_profit_r=profit, gross_loss_r=loss, profit_factor=divide(profit, loss),
                avg_win_r=aw, avg_loss_r=al, payoff_ratio=divide(aw, al),
                best_trade_r=float(r.max()) if len(r) else None,
                worst_trade_r=float(r.min()) if len(r) else None,
                std_trade_r=float(r.std(ddof=1)) if len(r) > 1 else None,
                max_drawdown_r=float(dd.max()), recovery_factor=divide(float(r.sum()), float(dd.max())),
                max_consecutive_wins=streak(r > 1e-12), max_consecutive_losses=streak(r < -1e-12),
                max_underwater_trade_closes=streak(dd[1:] > 1e-12))


def account_stats(daily_r, equity, risk_percent, risk_cash):
    r = daily_r.to_numpy()
    if risk_percent is not None:
        returns = r * risk_percent / 100
        curve = np.r_[equity, equity * np.cumprod(1 + returns)]
    else:
        curve = np.r_[equity, equity + risk_cash * np.cumsum(r)]
        returns = np.diff(curve) / curve[:-1]
    dd = 1 - curve / np.maximum.accumulate(curve)
    years = len(r) / 365.25
    cagr = (curve[-1] / equity) ** (1 / years) - 1 if years and curve[-1] > 0 and (curve > 0).all() else None
    downside = float(np.sqrt(np.mean(np.minimum(returns, 0) ** 2)))
    result = dict(initial_equity=equity, ending_equity=float(curve[-1]),
                  total_return_pct=100 * (float(curve[-1]) / equity - 1),
                  cagr_pct=100 * cagr if cagr is not None else None,
                  max_drawdown_pct=float(dd.max()) * 100,
                  sharpe_365=divide(float(returns.mean()) * np.sqrt(365), float(returns.std(ddof=1))),
                  sortino_365=divide(float(returns.mean()) * np.sqrt(365), downside),
                  annual_volatility_pct=float(returns.std(ddof=1)) * np.sqrt(365) * 100,
                  calmar=divide(cagr, float(dd.max())) if cagr is not None else None,
                  insolvent=(curve <= 0).any().item())
    return result, curve[1:]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initial-equity", type=float)
    sizing = parser.add_mutually_exclusive_group()
    sizing.add_argument("--risk-percent", type=float)
    sizing.add_argument("--risk-cash", type=float)
    args = parser.parse_args()
    funded = args.initial_equity is not None
    if funded != (args.risk_percent is not None or args.risk_cash is not None):
        raise ValueError("Specify both initial equity and one risk sizing rule")
    if funded and (args.initial_equity <= 0 or (args.risk_cash is not None and args.risk_cash <= 0)
                   or (args.risk_percent is not None and not 0 < args.risk_percent < 100)):
        raise ValueError("Invalid equity/risk input")
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    all_rows = pd.read_csv(args.run / "trades.csv")
    active = all_rows.loc[all_rows.status.isin(["tp_first", "sl_first", "time_exit"])].copy()
    active["duration_minutes"] = (pd.to_datetime(active.exit_timestamp_ny, utc=True, format="ISO8601")
                                  - pd.to_datetime(active.entry_timestamp_ny, utc=True, format="ISO8601")).dt.total_seconds() / 60
    active["modeled_exit"] = np.where(active.status.eq("tp_first"), active.tp,
                                     np.where(active.status.eq("sl_first"), active.sl, active.trigger_trade_price))
    active["fee_r_per_bps_each_side"] = (active.entry + active.modeled_exit) * .0001 / active.risk_price
    assert np.isfinite(active.gross_r).all() and active.risk_price.gt(0).all()
    assert active.duration_minutes.ge(0).all()
    checked_time = active.loc[active.status.eq("time_exit")]
    np.testing.assert_allclose(checked_time.gross_r,
        checked_time.side * (checked_time.trigger_trade_price - checked_time.entry) / checked_time.risk_price,
        rtol=1e-10, atol=1e-10)
    rows, exits, monthly, yearly, directions, fees, curves, account = [], [], [], [], [], [], [], []
    for (symbol, profile), group in active.groupby(["symbol", "profile"], sort=False):
        g = group.sort_values("session_date")
        key = dict(symbol=symbol, profile=profile)
        m = dict(key, **stats(g.gross_r))
        original = all_rows.loc[all_rows.symbol.eq(symbol) & all_rows.profile.eq(profile)]
        valid = original.loc[original.profile_status.eq("valid")]
        index = pd.date_range(valid.session_date.min(), valid.session_date.max(), freq="D")
        daily = g.assign(day=pd.to_datetime(g.session_date)).set_index("day").gross_r.reindex(index, fill_value=0)
        downside = float(np.sqrt(np.mean(np.minimum(daily.to_numpy(), 0) ** 2)))
        m.update(first_trade=g.session_date.min(), last_trade=g.session_date.max(),
                 profile_start=valid.session_date.min(), profile_end=valid.session_date.max(),
                 active_pct_valid_sessions=100 * len(g) / len(valid),
                 average_duration_minutes=float(g.duration_minutes.mean()),
                 median_duration_minutes=float(g.duration_minutes.median()),
                 median_risk_bps=float(g.risk_bps.median()),
                 min_risk_bps=float(g.risk_bps.min()),
                 risk_bps_p05=float(g.risk_bps.quantile(.05)),
                 fee_break_even_bps_each_side=divide(float(g.gross_r.sum()), float(g.fee_r_per_bps_each_side.sum())),
                 sharpe_daily_r_proxy_365=divide(float(daily.mean()) * np.sqrt(365), float(daily.std())),
                 sortino_daily_r_proxy_365=divide(float(daily.mean()) * np.sqrt(365), downside))
        rows.append(m)
        cumulative = daily.cumsum()
        dd = np.maximum.accumulate(np.r_[0, cumulative.values])[1:] - cumulative.values
        c = pd.DataFrame(dict(symbol=symbol, profile=profile, session_date=index.strftime("%Y-%m-%d"),
                              daily_r=daily.values, cumulative_r=cumulative.values, drawdown_r=dd))
        if funded:
            a, eq = account_stats(daily, args.initial_equity, args.risk_percent, args.risk_cash)
            account.append(dict(key, **a))
            c["equity"] = eq
        curves.append(c)
        for status, part in g.groupby("status"):
            exits.append(dict(key, exit_type=status, **stats(part.gross_r)))
        for side, part in g.groupby("side"):
            directions.append(dict(key, direction="long" if side == 1 else "short", **stats(part.gross_r)))
        for period, part in g.groupby(g.session_date.str[:7]):
            monthly.append(dict(key, month=period, **stats(part.gross_r)))
        for period, part in g.groupby(g.session_date.str[:4]):
            yearly.append(dict(key, year=period, **stats(part.gross_r)))
        for fee_bps in [0, 2, 4]:
            adjusted = g.gross_r - fee_bps * g.fee_r_per_bps_each_side
            fees.append(dict(key, hypothetical_fee_bps_each_side=fee_bps, **stats(adjusted)))
    metrics = pd.DataFrame(rows)
    metrics.to_csv(out / "metrics.csv", index=False)
    valid_counts = all_rows.loc[all_rows.profile_status.eq("valid")].groupby("session_date").size()
    combinations = all_rows[["symbol", "profile"]].drop_duplicates().shape[0]
    common_dates = set(valid_counts.loc[valid_counts.eq(combinations)].index)
    common_rows = [dict(symbol=s, profile=p, **stats(g.sort_values("session_date").gross_r))
                   for (s, p), g in active.loc[active.session_date.isin(common_dates)].groupby(["symbol", "profile"], sort=False)]
    pd.DataFrame(common_rows).to_csv(out / "common_dates_metrics.csv", index=False)
    for name, values in [("exit_type_metrics", exits), ("monthly_metrics", monthly),
                         ("yearly_metrics", yearly), ("direction_metrics", directions),
                         ("hypothetical_fee_sensitivity", fees), ("account_metrics", account)]:
        if values:
            pd.DataFrame(values).to_csv(out / f"{name}.csv", index=False)
    curve_df = pd.concat(curves, ignore_index=True)
    curve_df.to_csv(out / "daily_curves.csv", index=False)
    active.to_csv(out / "closed_trades.csv", index=False)
    profiles = list(LABELS)
    fig, axes = plt.subplots(3, 2, figsize=(13, 12), layout="constrained")
    for row, symbol in enumerate(metrics.symbol.unique()):
        for profile in profiles:
            c = curve_df.loc[curve_df.symbol.eq(symbol) & curve_df.profile.eq(profile)]
            dates = pd.to_datetime(c.session_date)
            axes[row, 0].plot(dates, c.cumulative_r, label=LABELS[profile])
            axes[row, 1].plot(dates, -c.drawdown_r)
        axes[row, 0].set_title(f"{symbol}: cumulative closed-trade R")
        axes[row, 1].set_title(f"{symbol}: closed-trade drawdown R")
        for ax in axes[row]:
            ax.axhline(0, color="gray", linewidth=.6)
            ax.grid(alpha=.2)
            ax.tick_params(axis="x", rotation=20)
        axes[row, 0].legend(fontsize=7)
    fig.suptitle("Aligned bias + reversal at 13:00 NY | All positions closed before 17:00 | Gross R")
    fig.savefig(out / "performance_curves.png", dpi=150)
    plt.close(fig)
    lines = ["# Performa semua posisi setelah ditutup sebelum 17:00 NY", "",
        "TP = +1R, SL = −1R; time exit = arah entry × (harga terakhir sebelum 17:00 − entry) / jarak entry–SL.",
        "Win rate di sini mencakup SEMUA entry: trade dengan R positif menang, R negatif kalah, dan nol breakeven. Setiap kombinasi market/profile adalah model terpisah.", "",
        "| Market | Profile (NY) | Trade | Win rate | Total R | Avg R/trade | Profit factor | Max DD R | Losing streak |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for m in metrics.itertuples():
        lines.append(f"| {m.symbol} | {LABELS[m.profile]} | {m.trades} | {m.win_rate_pct:.1f}% | {m.total_r:+.2f} | {m.mean_r:+.4f} | {m.profit_factor:.3f} | {m.max_drawdown_r:.2f} | {m.max_consecutive_losses} |")
    lines += ["", "## Kontribusi posisi yang belum mencapai TP/SL", "",
              "| Market | Profile | Time exits | Total R | Avg R | Positif | Negatif |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for e in exits:
        if e["exit_type"] == "time_exit":
            lines.append(f"| {e['symbol']} | {LABELS[e['profile']]} | {e['trades']} | {e['total_r']:+.2f} | {e['mean_r']:+.4f} | {e['winning_closes']} | {e['losing_closes']} |")
    lines += ["", "![Kurva performa](performance_curves.png)", "",
        "## Definisi dan cakupan", "",
        "Total R adalah penjumlahan unit risiko, bukan return akun dalam persen. Avg R adalah expectancy per trade. Profit factor = jumlah R positif / absolut jumlah R negatif. Payoff = rata-rata kemenangan / rata-rata kerugian.",
        "Drawdown R dihitung dari kurva kumulatif R dengan saldo awal 0R. Ini drawdown berdasarkan trade yang sudah ditutup; drawdown intraday belum diukur. Losing streak memakai semua kerugian, termasuk time exit negatif.",
        "metrics.csv juga menyimpan median/std/best/worst R, winning/losing/breakeven closes, rata-rata durasi, trade frequency, payoff, recovery factor, dan streak. Monthly/yearly metrics serta long/short breakdown tersedia terpisah.",
        "Sharpe/Sortino di metrics.csv adalah proxy dari R harian, annualisasi 365 untuk crypto, target/risk-free nol. Hari tanpa entry dalam cakupan sumber dihitung 0R; tanggal di luar cakupan arsip tidak diperpanjang dengan nol. Sortino memakai root mean square seluruh downside harian. Proxy ini bukan Sharpe akun yang telah ditentukan sizing-nya.",
        "hypothetical_fee_sensitivity.csv memakai SKENARIO 0, 2, dan 4 bps per sisi; bukan fee exchange yang terverifikasi. Fee dalam R = fee_bps × 0.0001 × (entry + modeled exit) / risk_price. Spread, slippage, funding dan gap stop belum dimodelkan.",
        "fee_break_even_bps_each_side adalah fee konstan per sisi yang membuat total hasil tepat nol, dengan asumsi semua entry dan sizing 1R tetap sama. Min dan percentile 5% jarak stop membantu memeriksa dampak stop yang sangat dekat; belum ada batas leverage atau minimum stop.",
        "common_dates_metrics.csv memakai tanggal yang valid pada seluruh 12 kombinasi, untuk perbandingan dengan rentang yang sama.",
        "Hasil utama tetap gross dengan penilaian TP/SL pada level barrier. Sumber arsip dan bias tidak berubah. Model dapat berkorelasi; total R dari 12 kombinasi tidak boleh dijumlahkan sebagai satu portfolio tanpa sizing/alokasi.",
        "Periode tiap market berbeda. Profit factor atau win rate saja tidak mengukur ketahanan hasil; lihat kurva, yearly_metrics.csv, dan drawdown."]
    if funded:
        lines += ["", "Account metrics tersedia di account_metrics.csv. Sizing memakai parameter pengguna, tanpa pembatasan margin/leverage. CAGR dihitung atas hari kalender dalam cakupan arsip. Account DD tetap berdasarkan hasil trade tertutup."]
    else:
        lines += ["", "Return akun %, CAGR, dan Calmar menunggu modal awal serta aturan risiko nominal/persentase. Tidak diasumsikan tanpa input pengguna."]
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "analysis_config.json").write_text(json.dumps(dict(source=str(args.run.resolve()),
        initial_equity=args.initial_equity, risk_percent=args.risk_percent, risk_cash=args.risk_cash,
        annualization=365, fees_main=0, fee_sensitivity_bps_per_side=[0, 2, 4]), indent=2), encoding="utf-8")
    print(metrics[["symbol", "profile", "trades", "win_rate_pct", "total_r", "mean_r", "profit_factor", "max_drawdown_r"]].round(4).to_string(index=False))


if __name__ == "__main__":
    main()
