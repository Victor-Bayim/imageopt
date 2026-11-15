[English](ORIGINAL_PROJECT_README.md) | **简体中文**

# imageopt

`imageopt` 是一个围绕容器镜像冷启动优化的 Python 控制面。它把容器启动期间的真实文件访问采样结果转成 eStargz prefetch hints，并把 `sample -> analyze -> llm-optimize -> apply -> benchmark -> report` 串成一个可恢复的单工作目录流程。
当前唯一正式入口是 [`scripts/oneclick_product.py`](../scripts/oneclick_product.py)。`docs/archive/` 下的历史文档已归档，不作为当前事实依据。

## 当前事实

- 唯一正式入口：`scripts/oneclick_product.py`
- 正式 mode：`deterministic`、`llm-auto`、`llm-manual`
- 默认 mode：`deterministic`
- 当前采样边界：文件级采样；chunk-aware 逻辑只存在于 analyze/apply/report 审计层
- LLM 输出合同：JSON-first，最终输出必须是严格 JSON
- 唯一允许的顶层 JSON 结构：
```json
{"P0":[],"P1":[],"P2":[],"P3":[]}
```
- `legacy YAML` 只保留为 manual response 导入兼容格式
- analyze/apply 的质量判断现在是 advisory：低质量、预算裁剪、低置信不会直接终止流程
- 当 LLM 输出在 grounding 后未形成可用路径集合时，当前实现不会采用该结果，而是回退或保留 deterministic hints
- 只有最终无法形成可消费的 `hints.yaml` / `estargz.list`，或执行失败、产物缺失、产物不可消费、benchmark 没有有效样本等情况，相关阶段才会 hard fail
- 当前主实验围绕 `ready-serving-layout-trap-v5` 的 triple benchmark

## 系统功能范围

1. 对目标镜像执行 preflight，确认环境和支持边界。
2. 启动容器并采集启动期文件访问事件，落地到 `raw_events.jsonl`。
3. 导出并补强 OPEN/MMAP 元数据，形成 `events.jsonl` 和 `events_enriched.jsonl`。
4. 聚合候选路径、运行质量审计、生成 deterministic hints。
5. 在需要时把 deterministic baseline 交给 LLM 做再排序或保守裁剪。
6. 把 hints 应用到目标镜像，生成 hinted eStargz 镜像。
7. 运行冷启动 benchmark，并输出统一报告。

## 范围说明

- 当前实验结论限于 `ready-serving-layout-trap-v5` 的 triple benchmark。
- 系统的采样事实来源为 file-level 事件；在存在 image TOC 时，chunk-aware 仅用于对既有文件路径选择结果做 layer/chunk 视角的审计、解释或实验性 shadow 对照。
- 更详细的边界说明见 [`docs/KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.zh-CN.md)。

## 正式工作流

正式 stage 由同一个入口脚本统一驱动：

1. `preflight`
2. `sample`
3. `export-enrich`
4. `analyze`
5. `llm-optimize`，仅在 `llm-auto` 或 `llm-manual` 时进入
6. `apply`，按命令显式启用
7. `benchmark`，按命令显式启用
8. `report`

最常见的正式命令是：
```bash
python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/imageopt_run
python3 scripts/oneclick_product.py report --work-dir /tmp/imageopt_run
```

`run` 会把通过的 stage 记录到 `manifest.json`，后续同一 `work-dir` 下可恢复执行。

## 支持的 mode

### `deterministic`

- 默认模式
- 只依赖采样、聚合、质量审计和 deterministic selector
- 是整个系统的基线输出
- 即使启用 LLM，也会先生成 deterministic baseline

### `llm-auto`

- 在 deterministic baseline 之上调用 LLM provider
- 当前 provider 配置由 `orchestrator/llm.py` 和 `orchestrator/llm_providers/` 管理
- `run --mode llm-auto` 需要有效 API key
- 上游不可用、响应不可解析、合同校验失败，或输出在 grounding 后未形成可用路径集合时，系统会保留 deterministic hints
- `run --mode llm-auto` 不允许静默降级成手工网页模式

### `llm-manual`

- 导出网页端 prompt bundle
- 用户在网页端获得响应后，再通过 `--manual-response-file` 导回
- 导回时优先按严格 JSON 解析
- 如果不是 JSON，会尝试兼容旧 YAML
- grounding 和后处理校验仍然保留

## LLM 合同

当前 prompt 和导入逻辑的关键约束如下：

- 最终输出必须是严格 JSON，不允许 Markdown、解释、代码块
- 顶层只能有 `P0`、`P1`、`P2`、`P3`
- 每个值都必须是绝对文件路径字符串数组
- 路径必须来自输入候选列表
- 不能编造路径
- 不能输出目录
- 不能输出通配符
- 不应把 `/proc`、`/sys`、`/dev`、`/run` 这类噪声路径写入结果
- prompt 中会要求模型在内部做自检，但这些自检内容不得出现在最终输出里

优先级语义：

- `P0`：启动最早期必须访问，极少且关键
- `P1`：紧随启动期访问，对 ready latency 有高影响
- `P2`：有一定价值，但不应挤占 `P0/P1` 预算
- `P3`：低优先级，或者当前不建议预取

选择策略强调：

- 更早访问的路径优先
- 命中次数更高的路径优先
- OPEN/MMAP/mapfiles/dict/open_devino 等多信号一致的路径优先
- 置信度更高的路径优先
- 超大文件需要更保守地处理

## 质量模型

当前 analyze/apply 的质量模型分成两层：

### Advisory

这些情况会进入 `quality_status`、`warnings`、`recommendation`、`blockers` 等结构化字段，但不会仅因为它们就把 stage 改成 `failed`：

- 采样事件偏少
- candidate 偏少
- coverage 偏低
- hints 数量偏少或偏多
- 预算超出但裁剪后仍可继续
- grounding 丢弃了非法路径，但仍保留了可消费结果
- selector confidence 偏低
- early-onehit 比例异常
- mmap/open 比例异常

### Hard fail

这些情况仍然会终止流程：

- 命令本身执行失败
- 必需输入不存在或无法解析
- sample 完全没有可用输出
- analyze 无法生成任何有效 hints
- 最终无法形成可消费的 `hints.yaml` / `estargz.list`
- apply 执行失败
- apply 产物镜像不存在或不可消费
- benchmark 没有有效结果
- lazy benchmark 的 full-pull guard 判定结果无效

对于 LLM stage，如果输出在 grounding 后未形成可用路径集合，当前实现不会采用该结果，而是回退或保留 deterministic hints；这一情况本身不等于整个主流程失败。

## 关键产物

每次正式执行的根目录都是一个 `work-dir`，其中至少会围绕这些文件组织：

- `manifest.json`
  当前 run 的阶段状态、产物路径、pause/resume 信息
- `raw_events.jsonl`
  原始文件访问事件
- `events.jsonl`
  导出后的事件
- `events_enriched.jsonl`
  补强 OPEN/MMAP 元数据后的事件
- `quality_gate.json`
  analyze 阶段质量审计与 advisory 字段
- `selector_stats_hints.json`
  当前 hints 的 selector 统计和质量建议
- `hints.yaml`
  当前实际会被 apply 消费的 hints
- `estargz.list`
  当前实际会被 apply 消费的 prefetch list
- `llm_result.json`
  LLM 阶段结果、fallback 信息、grounding 后状态
- `apply_result.json`
  apply 阶段结果和可消费性校验摘要
- `benchmark_result.json`
  benchmark 汇总结果
- `run_report.json`
  统一结构化报告
- `run_report.md`
  面向阅读的 Markdown 报告

## 支持边界

当前产品策略由源码中的 `enforced_supports_payload()` 给出：

- Linux 单机
- `containerd + nerdctl`
- `root` 或免密 `sudo`
- 默认 source of truth 是文件落盘 file sink
- gated startup backend 顺序在 `auto` 下优先考虑 host launcher static，再考虑 image shell，再考虑 host launcher dynamic experimental

## 快速开始

### 1. 先做 preflight

```bash
python3 scripts/oneclick_product.py preflight <IMAGE> --work-dir /tmp/imageopt_run
```

### 2. 走完整 deterministic 基线

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/imageopt_run \
  --mode deterministic
```

### 3. 生成报告

```bash
python3 scripts/oneclick_product.py report --work-dir /tmp/imageopt_run
```

### 4. 启用 `llm-auto`

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/imageopt_llm_auto \
  --mode llm-auto \
  --llm-provider openai
```

说明：
- 需要可用的 provider 配置和 API key
- 如果 LLM 返回不可用内容，系统会回退到 deterministic hints
- 不会自动切到网页端手工模式

### 5. 启用 `llm-manual`

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/imageopt_llm_manual \
  --mode llm-manual
```

将生成以下文件：
- `prompt.txt`
- `web_llm_ready_to_paste.txt`
- `llm_manual_instructions.md`
- `import_manual_web_llm.sh`
- `resume_manual_web_llm.sh`

网页端返回结果后，恢复命令如下：
```bash
python3 scripts/oneclick_product.py llm-optimize \
  --work-dir /tmp/imageopt_llm_manual \
  --manual-response-file /tmp/imageopt_llm_manual/hints_raw.manual.yaml
```

虽然默认文件名仍是 `hints_raw.manual.yaml`，但当前合同是 JSON-first。

### 6. 在同一次 run 中追加 apply 与 benchmark

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/imageopt_full \
  --mode deterministic \
  --apply \
  --benchmark
```

### 7. 直接跑当前主实验的 triple benchmark

当前主实验关注三组：
1. raw full pull baseline
2. unoptimized eStargz lazy
3. hinted eStargz lazy

直接 benchmark 形式如下：

```bash
python3 scripts/oneclick_product.py benchmark \
  --work-dir /tmp/layout_trap_v5_bench \
  --base-image <RAW_IMAGE> \
  --unopt-image <UNOPT_ESTARGZ_IMAGE> \
  --opt-image <HINTED_ESTARGZ_IMAGE> \
  --workload ready-serving-layout-trap-v5 \
  --mode ready \
  --comparison-mode raw-full-vs-estargz-lazy-triple \
  --runs 3
```

在 `run` 内使用 triple benchmark 时，需要显式提供 `--bench-unopt-image`。

## 当前主实验结论

当前正式实验叙述应围绕 `ready-serving-layout-trap-v5` 的 triple benchmark。

主要结论使用 `runs=3`：
- `baseline_median_full_pull_ready_ms = 453634.0`
- `unoptimized_median_lazy_ready_ms = 2360.0`
- `optimized_median_lazy_ready_ms = 1677.0`
- `unoptimized_vs_raw_improvement_pct = 99.4797568083521`
- `optimized_vs_raw_improvement_pct = 99.6303187150875`
- `optimized_vs_unoptimized_improvement_pct = 28.940677966101696`

这组主要实验结果对应的时间量级分别为 `453.634 s`、`2.360 s` 和 `1.677 s`。因此，`runs=3` 的正式结论应理解为：在当前 workload 和当前 benchmark 合同下，hinted eStargz 的 lazy ready 中位数低于 unoptimized eStargz。

`runs=1 after advisory fix` 只能当作 smoke / regression check：
- `baseline_median_full_pull_ready_ms = 157743.0`
- `unoptimized_median_lazy_ready_ms = 2550.0`
- `optimized_median_lazy_ready_ms = 1175.0`
- `unoptimized_vs_raw_improvement_pct = 98.38344649207889`
- `optimized_vs_raw_improvement_pct = 99.25511750125203`
- `optimized_vs_unoptimized_improvement_pct = 53.92156862745098`

这组修复后的单次回归检查对应的时间量级分别为 `157.743 s`、`2.550 s` 和 `1.175 s`，其用途是确认修复后主实验路径仍可得到可解释结果，而不是替代 `runs=3` 的主要结论。

这两组数据说明：

- lazy eStargz 相比 raw full pull 在该场景下差异显著
- hinted eStargz 在该场景下继续缩短了 lazy ready 时间
- 这些数字只描述当前 workload、当前镜像组合、当前 benchmark 合同下的结果

## 仓库导航

- `scripts/oneclick_product.py`
  唯一正式 CLI 入口
- `orchestrator/stages/`
  各 stage 的正式实现
- `orchestrator/llm.py`
  provider 解析与配置入口
- `orchestrator/llm_providers/`
  provider 适配层
- `optimizer/tools/build_llm_prompt.py`
  prompt 构造器
- `optimizer/tools/prepare_preferred_paths.py`
  grounding 预处理
- `optimizer/tools/select_prefetch_paths.py`
  selector
- `scripts/coldstart_bench_strict.sh`
  benchmark helper，由正式 benchmark stage 调用
- `scripts/apply_hints_to_image.sh`
  apply helper，由正式 apply stage 调用

## 文档

- [`docs/ARCHITECTURE.md`](ARCHITECTURE.zh-CN.md)
  控制面、stage、manifest、artifact、report 的关系
- [`docs/USAGE.md`](USAGE.zh-CN.md)
  CLI 用法、mode、恢复方式、benchmark/apply 调用规则
- [`docs/EXPERIMENTS.md`](EXPERIMENTS.zh-CN.md)
  当前主实验、指标语义、结果与解释边界
- [`docs/IMPLEMENTATION_NOTES.md`](IMPLEMENTATION_NOTES.zh-CN.md)
  prompt、grounding、selector、quality advisory、benchmark 汇总细节
- [`docs/KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.zh-CN.md)
  当前边界、非目标和不应夸大的说法

## 当前边界的一句话总结

这是一个以 `scripts/oneclick_product.py` 为唯一正式入口、以文件级采样为事实基础、以 deterministic baseline 为起点、在当前 `ready-serving-layout-trap-v5` triple benchmark 上已有明确证据的镜像冷启动优化控制面。
