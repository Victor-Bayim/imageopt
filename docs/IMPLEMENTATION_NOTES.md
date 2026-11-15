# Implementation Notes

本文件记录当前实现中最关键的收口点。

## 1. 事实基础

当前实现有三个核心原则：

1. 入口统一到 `scripts/oneclick_product.py`
2. deterministic 是所有 LLM 流程的 baseline
3. 质量问题优先作为 advisory 写入结构化字段，而不是直接终止流程

## 2. 采样与后处理边界

当前事实来源顺序是：

1. `raw_events.jsonl`
2. `events.jsonl`
3. `events_enriched.jsonl`
4. `candidates.json`
5. selector 与 LLM 后处理产物

实现上需要区分两件事：

- 采样层只负责收集文件访问事实
- chunk-aware 逻辑只在存在 image TOC 时，对既有文件路径选择结果的后处理视图里出现

这意味着：

- 采样器本身并不直接产出 layer/chunk 视角的访问事实
- image TOC 相关逻辑不会改写原始 sample 事实

## 3. analyze 的实现重点

`run_analyze_stage()` 当前做了四件事：

1. 聚合候选
2. 运行质量审计
3. 运行 deterministic selector
4. 在存在 image TOC 时生成附加的 chunk-aware 审计产物

主要工具链：

- `optimizer/aggregate_candidates.py`
- `optimizer/tools/quality_gate.py`
- `optimizer/tools/select_prefetch_paths.py`
- `optimizer/tools/export_image_toc.py`
- `optimizer/tools/build_toc_shadow.py`
- `optimizer/tools/build_chunk_selector_shadow.py`

analyze 的核心正式输出仍然是：

- `hints.yaml`
- `estargz.list`
- `selector_stats_hints.json`

## 4. 当前质量 advisory 结构

analyze 和 apply 目前都围绕这组字段落地：

```json
{
  "quality_status": "good|warning|poor|unknown",
  "warnings": [],
  "recommendation": "proceed|proceed_with_warning|rerun_sampling|review_hints|benchmark_before_publish|skip_publish",
  "blockers": []
}
```

当前实现约束：

- `warnings` 放质量风险
- `blockers` 只放真正阻断流程的问题
- 只要核心产物已经生成且可消费，stage `status` 就应保持 `passed`
- recommendation 只是建议，不自动替代 stage status

## 5. analyze 为什么不再因为低质量直接失败

当前 `analyze` 的实现把以下情况降级为 advisory：

- candidate pool 偏小
- selected hints 过少或过多
- quality gate `ok=false` 但仍有可用 hints
- identity coverage 偏低
- OPEN identity coverage 偏低
- mmap via=maps ratio 偏高
- budget boundary trimming
- forced-first 触发预算压力
- low-confidence filtering
- early-onehit share 偏高

只有真正没有可用 hints 时，才会进入 blocker 并让 stage fail。

## 6. LLM prompt 的当前收口

当前 prompt 构造在：

- `optimizer/tools/build_llm_prompt.py`
- `orchestrator/stages/llm_optimize.py`

对外合同已经收口到 JSON-first：

```json
{"P0":[],"P1":[],"P2":[],"P3":[]}
```

当前 prompt 明确禁止：

- Markdown
- 代码块
- 解释文字
- 顶层额外字段
- 非绝对路径
- 目录
- 通配符
- 噪声路径

manual prompt 与 auto prompt 的差别是：

- manual 可以携带更多背景说明
- auto 更强调严格、保守、只输出 JSON

但两者最终合同相同。

## 7. `legacy YAML` 现在处于什么位置

当前 `legacy YAML` 只保留在 manual response 导入兼容层。

实现上：

- `_load_manual_response_payload()` 会先尝试 JSON
- JSON 失败后才尝试兼容 YAML
- `_write_manual_instructions()` 会明确告诉用户优先保存严格 JSON

因此：

- 正式文档必须写 JSON-first
- 正式文档应将 JSON 表述为首选合同，YAML 仅作为导入兼容格式

## 8. grounding 与 fallback 的关系

LLM 返回内容不会直接覆盖最终 hints。
当前实现会先经过 grounding。

grounding 负责过滤：

- 非候选路径
- 非绝对路径
- 非 regular candidate
- 受 guard 约束而被跳过的路径
- 容器内不存在的路径
- 重复路径
- 特殊噪声路径

当前行为规则：

- 如果 grounding 后仍有路径，则继续后处理，并把丢弃情况写进 `llm_grounding_audit.json`
- 如果 grounding 后未形成可用路径集合，则不采用该 LLM 输出，并回退到 deterministic hints

这一回退行为本身不等于主流程失败；只有最终无法形成可消费的 `hints.yaml` / `estargz.list`，或执行失败、产物缺失、产物不可消费时，相关阶段才失败。

## 9. `llm-auto` 与 `llm-manual` 的实现差异

### `llm-auto`

- 构造 provider request
- 读取 API 响应
- 结构校验
- grounding
- selector
- 成功则输出 grounded LLM hints
- 失败则保留 deterministic hints

### `llm-manual`

- 导出 `prompt.txt`
- 导出网页端输入 bundle
- 在 `run` 场景下把 stage 置为 `paused`
- 等待 `--manual-response-file` 导回
- 导回后重复结构校验、grounding、selector

## 10. apply 的成功条件

`run_apply_stage()` 当前要求：

- apply helper 返回成功
- `optimized_image.txt` 存在
- 文件中有非空目标镜像引用
- smoke 可消费

apply 成功后，即使存在以下问题，也仍应是 `passed`：

- quality gate warning
- budget trimming
- grounding 丢弃非法路径但仍有有效结果
- selector filtered low-confidence paths
- cleaned prefetch list 偏小

这些问题只进入：

- `quality_status`
- `warnings`
- `recommendation`
- `blockers`

## 11. apply dry-run 的语义

当前 dry-run 不生成真实优化镜像。

因此 apply dry-run 的结果合同是：

- `status = passed`
- `result_kind = dry_run`
- `quality_status = unknown`
- `recommendation = review_hints`
- `blockers` 会说明没有真正产出优化镜像

这不是执行失败，而是有意不产生产物。

## 12. benchmark 的有效性规则

`benchmark_result.json` 的核心不是只有 CSV 存在，而是：

- 是否有足够有效样本
- comparison mode 是否成立
- lazy path 是否被 full-pull guard 判定为无效

当前 benchmark 会在以下情况下失败：

- 所有行都失败
- baseline 或 optimized 缺少足够成功样本
- triple benchmark 下三组中任一关键标签缺乏有效结果
- lazy path 被判定为 full pull，导致比较失效

因此 benchmark 的 `valid=false` 是真正的失败信号，不是 advisory。

## 13. triple benchmark 的摘要逻辑

当 comparison mode 为 `raw-full-vs-estargz-lazy-triple` 时，summary 关注：

- `baseline_median_full_pull_ready_ms`
- `unoptimized_median_lazy_ready_ms`
- `optimized_median_lazy_ready_ms`
- `unoptimized_vs_raw_improvement_pct`
- `optimized_vs_raw_improvement_pct`
- `optimized_vs_unoptimized_improvement_pct`

report 和 CLI summary 都会按这个三组结构解释结果。

## 14. manifest 的暂停/恢复行为

当前 manifest 在加载时会把旧状态 `awaiting_manual_response` 规整为 `paused`。

同时会补齐：

- `pause_reason`
- `resume_inputs.manual_response_file`
- `resume_command`

并且默认把 manual response format 记录为 `json`。

## 15. report 的职责边界

`run_report.json` 和 `run_report.md` 不是新计算事实来源，而是汇总层。

它们负责把分散产物收口成：

- run overall status
- stage overview
- mode / mode result
- startup backend 选择结果
- analyze quality summary
- LLM fallback summary
- apply result summary
- benchmark comparison summary

当前 report 不会重新定义 benchmark 质量，也不会把 analyze/apply 的 advisory 重新提升成 hard fail。

## 16. `chunk-shadow` 的当前定位

`chunk-shadow` selector mode 仍然属于实验性平行产物。

它的意义是：

- 基于 image TOC 对既有文件路径选择结果生成实验性 shadow 对照产物
- 与默认 file selector 做并排观察
- 在 apply/benchmark/publish 时允许显式选用这套平行产物

它不会取代默认 deterministic file selector 的正式地位。
