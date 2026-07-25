# Research Tracks

| Track | Horizon | Purpose |
| --- | --- | --- |
| `paper_replication` | 2013-12-07 to 2025-04-12 | Reproduce the published rules and tables. |
| `oos_validation` | 2025-04-13 onward | Evaluate frozen rules on data unseen by the paper. |
| `portfolio_allocation` | Paper horizon for design; OOS only after freezing | Compare static DCA, binary timing, and variable BTC allocation. |

Do not tune rules in `oos_validation`. Any model selected using post-paper data
is no longer an out-of-sample test.
