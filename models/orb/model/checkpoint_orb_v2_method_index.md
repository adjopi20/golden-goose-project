# ORB V2 Method Checkpoint Index

Current isolated checkpoints:

## Setup-Evaluation Variations

| Checkpoint | File | Role |
|---|---|---|
| strictBias_breakout_meanReversion | `models/orb/model/checkpoint_strictBias_breakout_meanReversion.md` | strict-bias fallback/reference |
| strictBias_breakout_bothBias_meanReversion | `models/orb/model/checkpoint_strictBias_breakout_bothBias_meanReversion.md` | combined variant, higher total R but more churn |
| ai_assisted_main_benchmark | `models/orb/model/checkpoint_ai_assisted_main_benchmark.md` | active AI-assisted benchmark; setup decisions are AI-walked, deterministic replay validates execution |
| ai_prominent_candle_p95_setup_variation | `models/orb/model/checkpoint_ai_prominent_candle_p95_setup_variation.md` | latest setup-evaluation variation; p95 individual bubbles plus prominent-candle/armed-breakout must-enter class |

## Parameter / Exit Variations

These change exits, protection, TP distance, or trailing behavior. They are not separate setup-evaluation logic.

| Checkpoint | File | Role |
|---|---|---|
| strictBias_breakout_bothBias_meanReversion_1p5R_protection | `models/orb/model/checkpoint_strictBias_breakout_bothBias_meanReversion_1p5R_protection.md` | third variant; both-bias MR with 3R trend TP1, +1.5R protection, tighter runner trail |
| exit_scenario_variants_top5_may25_may31 | `models/orb/model/checkpoint_exit_scenario_variants_top5_may25_may31.md` | comparison catalog of the five best deterministic compounded exit scenarios |

Do not contaminate these checkpoints.

When testing future sessions, choose one checkpoint explicitly and write results under a new isolated run folder.

Recommended naming:

```text
models/orb/runs/<timestamp>_strictBias_breakout_meanReversion_<scope>/
models/orb/runs/<timestamp>_strictBias_breakout_bothBias_meanReversion_<scope>/
models/orb/runs/<timestamp>_strictBias_breakout_bothBias_meanReversion_1p5R_protection_<scope>/
models/orb/runs/<timestamp>_ai_assisted_main_benchmark_<scope>/
models/orb/runs/<timestamp>_ai_prominent_candle_p95_setup_variation_<scope>/
```

Both remain research checkpoints, not proven edges.

The top-5 exit scenario catalog is a comparison set, not a method replacement.
