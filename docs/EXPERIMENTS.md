**English** | [简体中文](EXPERIMENTS.zh-CN.md)

# Experiments

## 1. Main experiment

- workload：`ready-serving-layout-trap-v5`
- benchmark contract：`raw-full-vs-estargz-lazy-triple`

The three comparison groups are fixed:

1. raw full pull baseline
2. unoptimized eStargz lazy
3. hinted eStargz lazy

## 2. Metric semantics

The main experiment uses three readiness metrics:

- `baseline_median_full_pull_ready_ms`
  Complete time from pulling the raw baseline to readiness
- `unoptimized_median_lazy_ready_ms`
  eStargz lazy readiness time without hints
- `optimized_median_lazy_ready_ms`
  eStargz lazy readiness time with hints

- Baseline measures full-pull readiness
- Unoptimized and optimized groups measure lazy readiness
- Improvement percentages are computed from the three medians

## 3. Main conclusions use `runs=3`

The main conclusions use this `runs=3` dataset:

| metric | value |
| --- | ---: |
| baseline_median_full_pull_ready_ms | 453634.0 |
| unoptimized_median_lazy_ready_ms | 2360.0 |
| optimized_median_lazy_ready_ms | 1677.0 |
| unoptimized_vs_raw_improvement_pct | 99.4797568083521 |
| optimized_vs_raw_improvement_pct | 99.6303187150875 |
| optimized_vs_unoptimized_improvement_pct | 28.940677966101696 |

## 4. Interpreting `runs=3`

- `baseline_median_full_pull_ready_ms = 453634.0` equals `453.634 s`: median raw full-pull readiness is on the order of hundreds of seconds.
- `unoptimized_median_lazy_ready_ms = 2360.0` equals `2.360 s`: unhinted eStargz median lazy readiness is on the order of seconds.
- `optimized_median_lazy_ready_ms = 1677.0` equals `1.677 s`: hints further reduce median lazy readiness under these conditions.
- For this workload and comparison contract, lazy eStargz substantially reduces readiness time relative to raw full pull.
- For this workload and contract, hinted eStargz has lower median lazy readiness than unoptimized eStargz.
- For this workload and contract, the median improvement of hinted over unoptimized eStargz is `28.940677966101696%`.

## 5. `runs=1 after advisory fix` is a smoke/regression check only

| metric | value |
| --- | ---: |
| baseline_median_full_pull_ready_ms | 157743.0 |
| unoptimized_median_lazy_ready_ms | 2550.0 |
| optimized_median_lazy_ready_ms | 1175.0 |
| unoptimized_vs_raw_improvement_pct | 98.38344649207889 |
| optimized_vs_raw_improvement_pct | 99.25511750125203 |
| optimized_vs_unoptimized_improvement_pct | 53.92156862745098 |

- `baseline_median_full_pull_ready_ms = 157743.0` equals `157.743 s`, `unoptimized_median_lazy_ready_ms = 2550.0` equals `2.550 s`, and `optimized_median_lazy_ready_ms = 1175.0` equals `1.175 s`.
- At least one smoke/regression result is available for the main experiment after the advisory fix
- This smoke result does not reverse the directional observation from the triple benchmark

## 6. Basis of the main experiment

The current source and tests support:

- `ready-serving-layout-trap-v5` workload
- `raw-full-vs-estargz-lazy-triple` comparison mode
- Both direct `benchmark` and `run --benchmark` execution

The three groups cover:

- The complete pull cost of the raw baseline
- A lazy baseline without hints
- Lazy results with hints

The three-way comparison distinguishes:

- Benefits of the lazy snapshotter itself
- The additional effect of hints on lazy readiness

> For `ready-serving-layout-trap-v5`, the main experiment uses `raw-full-vs-estargz-lazy-triple` to compare raw full pull baseline, unoptimized eStargz lazy and hinted eStargz lazy. With `runs=3`, medians are `453634.0 ms`, `2360.0 ms` and `1677.0 ms`, or `453.634 s`, `2.360 s` and `1.677 s`. Under these conditions, improvement is `99.4797568083521%` for unoptimized versus raw, `99.6303187150875%` for optimized versus raw, and `28.940677966101696%` for optimized versus unoptimized.

## 9. Reproduction conditions

Specify the following when reproducing the main experiment:

- workload：`ready-serving-layout-trap-v5`
- mode：`ready`
- comparison mode：`raw-full-vs-estargz-lazy-triple`
- baseline image：raw baseline image
- unoptimized image: eStargz image without hints
- optimized image: eStargz image with hints
- benchmark runs: `3` for the main conclusions

## 10. Experiment scope

- Main-result and smoke datasets are available for the `ready-serving-layout-trap-v5` triple benchmark
- These observations apply to the current workload, image combination and benchmark contract
- Broader experiments require new evidence supported by source, tests and verification commands
