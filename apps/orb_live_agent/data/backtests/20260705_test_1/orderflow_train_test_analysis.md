# Orderflow Backtest Analysis

Source: `apps\orb_live_agent\data\backtests\20260705_test_1`

- Trades: `152`
- Wins: `26`
- Losses: `126`
- Total R: `-5.53`
- Avg R: `-0.04`

## By direction

| direction | trades | wins | win_rate | avg_r | total_r |
| --- | --- | --- | --- | --- | --- |
| short | 88 | 14 | 15.9% | -0.16 | -14.50 |
| long | 64 | 12 | 18.8% | 0.14 | 8.97 |

## By close_reason

| close_reason | trades | wins | win_rate | avg_r | total_r |
| --- | --- | --- | --- | --- | --- |
| initial_stop | 72 | 0 | 0.0% | -1.17 | -83.98 |
| protected_stop | 54 | 0 | 0.0% | -0.16 | -8.51 |
| overnight_time_invalidation | 6 | 6 | 100.0% | 2.20 | 13.18 |
| runner_trailing_stop | 20 | 20 | 100.0% | 3.69 | 73.77 |

## By volume_expansion_bucket

| volume_expansion_bucket | trades | wins | win_rate | avg_r | total_r |
| --- | --- | --- | --- | --- | --- |
| 0.75..1.5 | 36 | 4 | 11.1% | -0.34 | -12.32 |
| -inf..0.75 | 22 | 3 | 13.6% | -0.39 | -8.53 |
| 1.5..3 | 41 | 6 | 14.6% | -0.11 | -4.56 |
| 8..inf | 17 | 2 | 11.8% | -0.06 | -1.02 |
| 3..8 | 36 | 11 | 30.6% | 0.58 | 20.89 |

## By directional_delta_bucket

| directional_delta_bucket | trades | wins | win_rate | avg_r | total_r |
| --- | --- | --- | --- | --- | --- |
| 0..0.25 | 27 | 4 | 14.8% | -0.16 | -4.33 |
| 0.25..0.6 | 48 | 6 | 12.5% | -0.09 | -4.17 |
| 0.6..inf | 77 | 16 | 20.8% | 0.04 | 2.97 |

## By supportive_bubble_qty_ratio_bucket

| supportive_bubble_qty_ratio_bucket | trades | wins | win_rate | avg_r | total_r |
| --- | --- | --- | --- | --- | --- |
| 5..inf | 64 | 10 | 15.6% | -0.18 | -11.81 |
| -inf..0.5 | 6 | 0 | 0.0% | -0.79 | -4.76 |
| 0.5..1 | 4 | 0 | 0.0% | -0.60 | -2.39 |
| missing | 45 | 8 | 17.8% | -0.02 | -1.08 |
| 2..5 | 18 | 3 | 16.7% | 0.11 | 1.92 |
| 1..2 | 15 | 5 | 33.3% | 0.84 | 12.57 |

## By two_sided_bubbles

| two_sided_bubbles | trades | wins | win_rate | avg_r | total_r |
| --- | --- | --- | --- | --- | --- |
| False | 103 | 18 | 17.5% | -0.10 | -10.01 |
| True | 49 | 8 | 16.3% | 0.09 | 4.48 |

## By max_bubble_side

| max_bubble_side | trades | wins | win_rate | avg_r | total_r |
| --- | --- | --- | --- | --- | --- |
| buy | 41 | 7 | 17.1% | -0.06 | -2.36 |
| sell | 66 | 11 | 16.7% | -0.03 | -2.10 |
| None | 45 | 8 | 17.8% | -0.02 | -1.08 |

## Worst Trades

| entry | exit | dir | reason | R | vol_exp | dir_delta | support_bubble_ratio | two_sided |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2025-12-27T09:51:00-05:00 | 2025-12-27T10:14:00-05:00 | short | initial_stop | -1.56 | 0.92 | 0.99 | inf | False |
| 2026-04-26T09:50:00-04:00 | 2026-04-26T10:20:00-04:00 | short | initial_stop | -1.44 | 0.84 | 1.00 | None | False |
| 2026-01-10T10:06:00-05:00 | 2026-01-10T11:35:00-05:00 | short | initial_stop | -1.44 | 0.89 | 0.29 | None | False |
| 2026-04-04T09:47:00-04:00 | 2026-04-04T11:04:00-04:00 | short | initial_stop | -1.32 | 0.22 | 1.00 | None | False |
| 2024-05-12T09:51:00-04:00 | 2024-05-12T10:30:00-04:00 | long | initial_stop | -1.28 | 1.08 | 0.75 | None | False |
| 2025-12-20T09:46:00-05:00 | 2025-12-20T17:05:00-05:00 | long | initial_stop | -1.26 | 5.75 | 0.94 | inf | False |
| 2026-02-08T10:10:00-05:00 | 2026-02-08T10:16:00-05:00 | long | initial_stop | -1.26 | 1.43 | 0.75 | inf | False |
| 2026-05-04T09:53:00-04:00 | 2026-05-04T10:24:00-04:00 | short | initial_stop | -1.25 | 1.29 | 0.97 | inf | False |
| 2026-01-19T10:00:00-05:00 | 2026-01-19T10:17:00-05:00 | long | initial_stop | -1.25 | 1.96 | 0.68 | None | False |
| 2025-03-22T10:03:00-04:00 | 2025-03-22T10:32:00-04:00 | long | initial_stop | -1.25 | 0.98 | 0.73 | None | False |
| 2025-07-27T09:46:00-04:00 | 2025-07-27T12:11:00-04:00 | short | initial_stop | -1.25 | 0.72 | 0.88 | None | False |
| 2024-09-08T09:57:00-04:00 | 2024-09-08T10:11:00-04:00 | long | initial_stop | -1.24 | 3.47 | 0.85 | inf | False |
| 2024-05-26T09:59:00-04:00 | 2024-05-26T10:28:00-04:00 | long | initial_stop | -1.24 | 2.54 | 0.88 | None | False |
| 2025-08-30T09:45:00-04:00 | 2025-08-30T09:52:00-04:00 | short | initial_stop | -1.23 | 1.00 | 0.85 | inf | False |
| 2026-03-08T10:03:00-04:00 | 2026-03-08T11:29:00-04:00 | long | initial_stop | -1.23 | 0.35 | 0.93 | None | False |
| 2024-08-11T10:13:00-04:00 | 2024-08-11T10:49:00-04:00 | long | initial_stop | -1.23 | 0.53 | 1.00 | None | False |
| 2024-04-20T09:53:00-04:00 | 2024-04-20T10:48:00-04:00 | short | initial_stop | -1.22 | 0.08 | 1.00 | None | False |
| 2024-03-24T09:59:00-04:00 | 2024-03-24T10:18:00-04:00 | short | initial_stop | -1.22 | 1.88 | 0.98 | inf | False |
| 2025-01-12T09:47:00-05:00 | 2025-01-12T10:19:00-05:00 | short | initial_stop | -1.22 | 1.52 | 0.50 | inf | False |
| 2026-02-15T09:53:00-05:00 | 2026-02-15T11:07:00-05:00 | long | initial_stop | -1.21 | 0.95 | 0.98 | inf | False |

## Walk-Forward: Volume Expansion Only

| split | train | test | picked_on_train | train_trades | train_R | test_base_trades | test_base_R | test_keep_trades | test_keep_R |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| WF1 | <= 2024-12-31 | 2025-01-01..2025-06-30 | >= 4 | 19 | 10.11 | 29 | -10.58 | 3 | 2.01 |
| WF2 | <= 2025-06-30 | 2025-07-01..2025-12-31 | >= 4 | 22 | 12.12 | 32 | 0.44 | 9 | 1.90 |
| WF3 | <= 2025-12-31 | 2026-01-01..2026-06-30 | >= 3 | 45 | 14.55 | 30 | 3.79 | 8 | 5.32 |

## OOS Stability: Volume Expansion Only

| rule | test_trades | test_wins | test_win_rate | test_total_R | test_avg_R |
| --- | --- | --- | --- | --- | --- |
| >= 0.75 | 74 | 12 | 16.2% | 2.28 | 0.03 |
| >= 1 | 63 | 11 | 17.5% | 8.80 | 0.14 |
| >= 1.5 | 51 | 10 | 19.6% | 11.95 | 0.23 |
| >= 2 | 39 | 9 | 23.1% | 16.00 | 0.41 |
| >= 2.5 | 32 | 7 | 21.9% | 9.32 | 0.29 |
| >= 3 | 25 | 7 | 28.0% | 13.16 | 0.53 |
| >= 4 | 17 | 3 | 17.6% | 1.34 | 0.08 |
| >= 5 | 13 | 1 | 7.7% | -3.52 | -0.27 |
| >= 8 | 7 | 1 | 14.3% | -0.71 | -0.10 |

## Walk-Forward: Directional Delta Only

| split | train | test | picked_on_train | train_trades | train_R | test_base_trades | test_base_R | test_keep_trades | test_keep_R |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| WF1 | <= 2024-12-31 | 2025-01-01..2025-06-30 | >= 0.9 | 17 | 12.36 | 29 | -10.58 | 2 | -1.38 |
| WF2 | <= 2025-06-30 | 2025-07-01..2025-12-31 | >= 0.9 | 19 | 10.98 | 32 | 0.44 | 6 | 4.95 |
| WF3 | <= 2025-12-31 | 2026-01-01..2026-06-30 | >= 0.9 | 25 | 15.93 | 30 | 3.79 | 9 | -0.78 |

## OOS Stability: Directional Delta Only

| rule | test_trades | test_wins | test_win_rate | test_total_R | test_avg_R |
| --- | --- | --- | --- | --- | --- |
| >= 0 | 91 | 14 | 15.4% | -6.34 | -0.07 |
| >= 0.25 | 72 | 12 | 16.7% | -0.05 | -0.00 |
| >= 0.4 | 59 | 10 | 16.9% | -0.57 | -0.01 |
| >= 0.5 | 51 | 8 | 15.7% | -4.36 | -0.09 |
| >= 0.6 | 43 | 8 | 18.6% | 1.74 | 0.04 |
| >= 0.75 | 33 | 6 | 18.2% | 0.93 | 0.03 |
| >= 0.9 | 17 | 4 | 23.5% | 2.79 | 0.16 |
