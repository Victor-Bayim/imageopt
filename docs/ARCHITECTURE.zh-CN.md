[English](ARCHITECTURE.md) | **简体中文**

# 架构

本文件只描述当前源码和测试仍然支持的控制面结构。
`docs/archive/` 下的历史文档已归档，不作为当前事实依据。

## 1. 单一正式入口

当前唯一正式入口是：

- `scripts/oneclick_product.py`

这个入口负责：

- 解析子命令和 run mode
- 处理 `work-dir`
- 维护 `manifest.json`
- 在失败、暂停、恢复之间统一阶段状态
- 串联 sample/analyze/llm/apply/benchmark/report

辅助脚本仍然存在，但都属于 stage 内部实现细节：

- `scripts/apply_hints_to_image.sh`
- `scripts/coldstart_bench_strict.sh`

## 2. 当前控制面结构

顶层模块分工如下：

- `scripts/oneclick_product.py`
  CLI、run 主链、resume 行为、阶段收口
- `orchestrator/stages/`
  正式 stage 实现
- `orchestrator/manifest.py`
  `manifest.json` 的读写和状态规范
- `orchestrator/stages/report.py`
  统一读取 stage 产物并汇总到 `run_report.json` / `run_report.md`
- `optimizer/tools/`
  analyze 与 LLM 相关的工具脚本

## 3. Stage 图

正式 stage 关系如下：

```text
preflight
  -> sample
  -> export-enrich
  -> analyze
  -> llm-optimize   (only for llm-auto / llm-manual)
  -> apply          (optional)
  -> benchmark      (optional)
  -> report
```

`run` 命令会在同一个 `work-dir` 中复用已经 `passed` 的 stage。

## 4. 各 stage 职责

### preflight

职责：

- 运行环境检查
- 评估当前镜像与启动模式是否在支持边界内
- 写出 `env_check.json` 与 `product_policy.json`

关键来源：

- `orchestrator/stages/preflight.py`
- `orchestrator/product_policy.py`

当前产品策略明确约束：

- Linux 单机
- `containerd + nerdctl`
- `root` 或免密 `sudo`
- 默认 source of truth 为 file sink

### sample

职责：

- 启动容器
- 启动采样器
- 等待 readiness
- 抓取原始文件访问事件
- 写出运行态诊断

主要产物：

- `runtime_state.json`
- `raw_events.jsonl`
- `sampler.log`
- `sampler_metrics.prom`

硬失败示例：

- 容器启动失败
- 采样器启动失败
- readiness 永远不成立
- 最终没有可用 sample 输出

### export-enrich

职责：

- 把原始 file-sink 事件转成结构化事件
- 为 OPEN/MMAP 事件补强额外元数据
- 输出后续 analyze 所需的 enriched 视图

主要产物：

- `events.jsonl`
- `events_enriched.jsonl`
- `cwd_snapshot.json`
- `maps_paths.txt`
- `mapfiles_devino.json`
- `devino_dict.json`

### analyze

职责：

- 聚合候选路径
- 运行质量审计
- 执行 deterministic selector
- 生成 `hints.yaml` 与 `estargz.list`
- 在存在 image TOC 时，对既有文件路径选择结果生成 chunk-aware 审计视图

主要产物：

- `candidates.json`
- `aggregate_stats.json`
- `quality_gate.json`
- `ranked.yaml`
- `hints.yaml`
- `estargz.list`
- `selector_stats_default.json`
- `selector_stats_hints.json`

可选 chunk-aware 审计产物：

- `image_toc.json`
- `toc_shadow.json`
- `chunk_selector_shadow.json`
- `ranked_chunk_shadow.yaml`
- `hints_chunk_shadow.yaml`
- `estargz_chunk_shadow.list`
- `selector_stats_chunk_shadow.json`

重要边界：

- 采样事实仍来自文件级事件
- image TOC 只用于对既有文件路径选择结果做后处理审计、额外解释视图和实验性 shadow 对照

### llm-optimize

职责：

- 在 deterministic baseline 之上构造 prompt
- 处理 `llm-auto` 和 `llm-manual`
- 对 LLM 返回进行结构校验、grounding、再选择
- 当 LLM 输出不可用或 grounding 后未形成可用路径集合时，保留 deterministic 结果

主要产物：

- `prompt.txt`
- `web_llm_ready_to_paste.txt`
- `body.json`
- `llm_response.json`
- `hints_raw.yaml`
- `preferred_paths.txt`
- `llm_grounding_audit.json`
- `llm_result.json`
- `llm_manual_instructions.md`
- `import_manual_web_llm.sh`

manual 模式暂停时还会有：

- `resume_manual_web_llm.sh`

### apply

职责：

- 读取当前 `hints.yaml` / `estargz.list`
- 运行 apply helper
- 检查目标镜像是否生成
- 进行可消费性校验
- 输出结构化 apply 结果

主要产物：

- `env_check_apply.json`
- `apply_policy.json`
- `apply.log`
- `apply_result.json`
- `optimized_image.txt`

`chunk-shadow` 模式下会生成平行命名的 `_chunk_shadow` 产物；这些产物属于基于 image TOC 的实验性 shadow 对照，不改变默认主路径。

### benchmark

职责：

- 调用 benchmark helper
- 汇总 baseline / optimized，或 triple 比较下的三组结果
- 根据有效样本数和 lazy full-pull guard 判定 benchmark 是否有效

主要产物：

- `env_check_benchmark_base.json`
- `env_check_benchmark_opt.json`
- `benchmark_policy.json`
- `coldstart_compare.csv`
- `benchmark_result.json`

### report

职责：

- 读取 manifest 和各阶段产物
- 推断当前 mode 与 mode result
- 汇总 startup、quality、LLM、apply、benchmark 信息
- 产出最终报告

主要产物：

- `run_report.json`
- `run_report.md`

## 5. Mode 结构

当前 mode 只有三种：

- `deterministic`
- `llm-auto`
- `llm-manual`

控制面约束如下：

- `deterministic` 不进入 LLM stage
- `llm-auto` 必须走 provider API
- `llm-manual` 允许先暂停再导回
- 所有 LLM mode 都建立在 deterministic baseline 之上

## 6. LLM 合同与数据流

当前 LLM 数据流是：

1. analyze 先生成 deterministic `hints.yaml`
2. `build_llm_prompt.py` 读取 `candidates.json` 和 `selector_stats_default.json`
3. prompt 要求模型输出严格 JSON
4. LLM 输出经结构校验
5. grounding 过滤掉非法路径、非候选路径、噪声路径、不存在路径、重复路径
6. grounding 后仍有可用路径时，进入后续选择
7. 如果 grounding 后未形成可用路径集合，则不采用该 LLM 输出，并回退到 deterministic 结果

当前对外合同：

```json
{"P0":[],"P1":[],"P2":[],"P3":[]}
```

语义要点：

- 只允许四个顶层键
- 每个值是绝对路径字符串数组
- 路径必须来自候选列表
- `legacy YAML` 只保留导入兼容，不是正式首选合同

## 7. Quality 模型

当前 analyze/apply 的质量模型分成两类：

### Advisory

这类问题会进入结构化字段，但不会仅因它们而把 stage 变成 `failed`：

- 事件量偏少
- candidate 偏少
- 覆盖度偏低
- hints 偏少或偏多
- 预算被裁剪
- grounding 丢弃部分非法路径，但仍有可用路径
- selector 低置信
- early-onehit 比例偏高
- mmap/open 证据结构异常

相关字段：

- `quality_status`
- `warnings`
- `recommendation`
- `blockers`

### Hard fail

这类问题仍然终止阶段：

- 核心命令执行失败
- 必需输入缺失或不可解析
- sample 没有可用输出
- analyze 没有生成任何有效 hints
- 最终无法形成可消费的 `hints.yaml` / `estargz.list`
- apply 目标镜像缺失或不可消费
- benchmark 没有有效结果
- lazy full-pull guard 判定结果无效

对于 LLM 阶段，grounding 后未形成可用路径集合只会导致该次 LLM 输出被放弃，并保留 deterministic hints；只有最终无法形成可消费 hints 产物时，相关阶段才失败。

## 8. Manifest 语义

`manifest.json` 是跨 stage 的主索引。

它保存：

- `run_id`
- `work_dir`
- `status`
- `options`
- `artifacts`
- `stages`

stage 状态集合来自 `ManifestStore`：

- `pending`
- `in_progress`
- `passed`
- `failed`
- `paused`
- `skipped`
- `cancelled`

对于 manual web flow：

- 旧的 `awaiting_manual_response` 在加载时会被规整为 `paused`
- `resume_inputs` 默认记录 `manual_response_file`
- 当前默认格式会写成 `json`

## 9. 报告模型

`run_report.json` / `run_report.md` 会汇总：

- overall status
- mode / mode result
- startup requested/used/backend
- sample workload / benchmark workload
- analyze 的 advisory 字段
- LLM fallback 或 grounding 结果
- apply 的可消费性与 advisory 字段
- benchmark 的 comparison mode、validity、invalid reasons

## 10. Chunk-aware 信息在当前架构中的位置

当前 chunk-aware 能力来自 analyze/report 阶段在存在 image TOC 时执行的 TOC 审计与 shadow selector。

它们的用途是：

- 估算选中路径覆盖的 layer/chunk
- 观察路径之间的 chunk overlap
- 生成 `chunk-shadow` 平行产物做对照
- 在 report 中解释路径选择的 chunk 视角代价

这些能力不改变采样事实来源，也不构成默认主路径。

## 11. 当前主实验在架构中的位置

当前实验叙述应围绕：

- workload：`ready-serving-layout-trap-v5`
- benchmark contract：`raw-full-vs-estargz-lazy-triple`
- 三组：raw full pull baseline、unoptimized eStargz lazy、hinted eStargz lazy

这组实验属于 benchmark/report 层的正式比较合同，而不是采样层的新事实模型。
