# Experiments

## 1. 当前主实验

- workload：`ready-serving-layout-trap-v5`
- benchmark contract：`raw-full-vs-estargz-lazy-triple`

三组比较对象固定为：

1. raw full pull baseline
2. unoptimized eStargz lazy
3. hinted eStargz lazy

## 2. 指标语义

当前主实验关注三类 ready 指标：

- `baseline_median_full_pull_ready_ms`
  raw baseline 从拉取到 ready 的完整时间
- `unoptimized_median_lazy_ready_ms`
  未加 hints 的 eStargz lazy ready 时间
- `optimized_median_lazy_ready_ms`
  加 hints 的 eStargz lazy ready 时间

- baseline 看 full pull ready
- unoptimized 和 optimized 看 lazy ready
- 改善百分比以这三组中位数为基础计算

## 3. 主要结论只使用 `runs=3`

当前主结论必须使用下面这组 `runs=3` 数据：

| metric | value |
| --- | ---: |
| baseline_median_full_pull_ready_ms | 453634.0 |
| unoptimized_median_lazy_ready_ms | 2360.0 |
| optimized_median_lazy_ready_ms | 1677.0 |
| unoptimized_vs_raw_improvement_pct | 99.4797568083521 |
| optimized_vs_raw_improvement_pct | 99.6303187150875 |
| optimized_vs_unoptimized_improvement_pct | 28.940677966101696 |

## 4. `runs=3` 的保守解读

- `baseline_median_full_pull_ready_ms = 453634.0` 对应 `453.634 s`，说明 raw baseline 的完整拉取到 ready 中位时间处于百秒量级。
- `unoptimized_median_lazy_ready_ms = 2360.0` 对应 `2.360 s`，说明未加 hints 的 eStargz lazy ready 中位时间处于秒级。
- `optimized_median_lazy_ready_ms = 1677.0` 对应 `1.677 s`，说明在该实验条件下，加 hints 后的 lazy ready 中位时间进一步缩短。
- 在该 workload 和该比较合同下，lazy eStargz 相比 raw full pull 的 ready 时间缩短非常明显。
- 在该 workload 和该比较合同下，hinted eStargz 的 lazy ready 中位数低于 unoptimized eStargz。
- 在该 workload 和该比较合同下，hinted eStargz 相比 unoptimized eStargz 的中位数改善为 `28.940677966101696%`。

## 5. `runs=1 after advisory fix` 只作 smoke / regression check

| metric | value |
| --- | ---: |
| baseline_median_full_pull_ready_ms | 157743.0 |
| unoptimized_median_lazy_ready_ms | 2550.0 |
| optimized_median_lazy_ready_ms | 1175.0 |
| unoptimized_vs_raw_improvement_pct | 98.38344649207889 |
| optimized_vs_raw_improvement_pct | 99.25511750125203 |
| optimized_vs_unoptimized_improvement_pct | 53.92156862745098 |

- `baseline_median_full_pull_ready_ms = 157743.0` 对应 `157.743 s`，`unoptimized_median_lazy_ready_ms = 2550.0` 对应 `2.550 s`，`optimized_median_lazy_ready_ms = 1175.0` 对应 `1.175 s`。
- advisory fix 之后，主实验路径至少还有一次 smoke / regression 结果可用
- 该 smoke 结果没有推翻 triple benchmark 的方向性观察

## 6. 当前主实验的依据

当前源码和测试都明确支持：

- `ready-serving-layout-trap-v5` workload
- `raw-full-vs-estargz-lazy-triple` comparison mode
- direct `benchmark` 和 `run --benchmark` 两种触发路径

三组比较正好覆盖：

- raw baseline 的完整拉取代价
- 未加 hints 的 lazy baseline
- 加 hints 之后的 lazy 结果

这比只做两组对比更能区分：

- lazy snapshotter 本身的收益
- hints 对 lazy ready 的额外影响

> 在 `ready-serving-layout-trap-v5` 场景下，当前主实验使用 `raw-full-vs-estargz-lazy-triple` 比较合同，对 raw full pull baseline、unoptimized eStargz lazy 和 hinted eStargz lazy 三组进行比较。`runs=3` 的中位数结果分别为 `453634.0 ms`、`2360.0 ms` 和 `1677.0 ms`，即 `453.634 s`、`2.360 s` 和 `1.677 s`。在该实验条件下，unoptimized 相对 raw 的改善为 `99.4797568083521%`，optimized 相对 raw 的改善为 `99.6303187150875%`，optimized 相对 unoptimized 的改善为 `28.940677966101696%`。

## 9. 复现实验时需要注意的条件

如果要复现当前主实验，应明确：

- workload：`ready-serving-layout-trap-v5`
- mode：`ready`
- comparison mode：`raw-full-vs-estargz-lazy-triple`
- baseline image：raw baseline image
- unoptimized image：未加 hints 的 eStargz image
- optimized image：加 hints 的 eStargz image
- benchmark runs：主结论使用 `3`

## 10. 当前实验边界

- 在 `ready-serving-layout-trap-v5` 上，triple benchmark 已经有主结论数据和 smoke 数据
- 该结论属于当前 workload、当前镜像组合、当前 benchmark 合同下的观察结果
- 后续若要扩展实验面，需要新增当前源码、测试和验证命令都能支撑的新证据
