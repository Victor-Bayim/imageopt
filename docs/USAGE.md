# Usage

本文件只覆盖当前 `scripts/oneclick_product.py` 的正式用法。

## 1. 唯一正式入口

```bash
python3 scripts/oneclick_product.py <subcommand> ...
```

正式子命令包括：

- `preflight`
- `sample`
- `export-enrich`
- `analyze`
- `llm-optimize`
- `apply`
- `publish`
- `benchmark`
- `report`
- `run`

最常用的是 `run` 和 `report`。

## 2. `work-dir`

所有正式 stage 都围绕一个 `work-dir` 工作。

如果没有显式传入，默认目录类似：

```text
optimizer/work/<timestamp>
```

`work-dir` 中通常会保留：

- `manifest.json`
- 各 stage 的 JSON 产物
- `hints.yaml`
- `estargz.list`
- `run_report.json`
- `run_report.md`

## 3. 最常见的命令

### 最小 deterministic 基线

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/imageopt_run \
  --mode deterministic
```

### 跑完后单独生成报告

```bash
python3 scripts/oneclick_product.py report \
  --work-dir /tmp/imageopt_run
```

### 分阶段执行

```bash
python3 scripts/oneclick_product.py preflight <IMAGE> --work-dir /tmp/imageopt_stage
python3 scripts/oneclick_product.py sample <IMAGE> --work-dir /tmp/imageopt_stage
python3 scripts/oneclick_product.py export-enrich --work-dir /tmp/imageopt_stage
python3 scripts/oneclick_product.py analyze --work-dir /tmp/imageopt_stage
python3 scripts/oneclick_product.py report --work-dir /tmp/imageopt_stage
```

## 4. `run` 的参数优先级

`run` 支持 `--config` 与 `--preset`。
当前优先级固定为：

```text
CLI explicit args > config > preset > built-in defaults
```

注意：

- `preset` 只提供默认值
- `config` 也只提供默认值
- 它们不会隐式启用 `--apply` 或 `--benchmark`
- 显式 CLI 参数永远优先

## 5. 当前 mode

### `deterministic`

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/run_det \
  --mode deterministic
```

特点：

- 不进入 LLM stage
- 直接消费 analyze 产出的 deterministic hints

### `llm-auto`

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/run_auto \
  --mode llm-auto \
  --llm-provider openai
```

特点：

- 需要有效 provider 配置
- `run --mode llm-auto` 在缺少 API key 时会报错
- 当 LLM 输出不可用，或在 grounding 后未形成可用路径集合时，系统会保留 deterministic hints
- 不会自动切成手工网页导入

### `llm-manual`

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/run_manual \
  --mode llm-manual
```

特点：

- 会暂停在 manual web flow
- 输出 prompt bundle
- 等待用户导回手工响应

## 6. `llm-manual` 当前流程

### 导出网页端输入

```bash
python3 scripts/oneclick_product.py run <IMAGE> \
  --work-dir /tmp/run_manual \
  --mode llm-manual
```

输出中会提示：

- `web_llm_ready_to_paste.txt`
- `llm_manual_instructions.md`
- `resume_manual_web_llm.sh`

### 导回网页端响应

当前合同是 JSON-first。
推荐导回命令：

```bash
python3 scripts/oneclick_product.py llm-optimize \
  --work-dir /tmp/run_manual \
  --manual-response-file /tmp/run_manual/hints_raw.manual.yaml
```

注意：

- 默认文件名仍然是 `hints_raw.manual.yaml`
- 但内容应该优先保存成严格 JSON
- 如果内容不是 JSON，系统会尝试兼容旧 YAML

当前唯一允许的顶层结构：

```json
{"P0":[],"P1":[],"P2":[],"P3":[]}
```

要求：

- 只允许顶层 `P0/P1/P2/P3`
- 数组元素必须是绝对文件路径
- 路径必须来自候选列表
- 不允许 Markdown、解释、代码块、目录、通配符

## 7. `run` 与独立子命令的关系

`run` 会自动串联：

1. `preflight`
2. `sample`
3. `export-enrich`
4. `analyze`
5. `llm-optimize`，仅在 `llm-auto` 或 `llm-manual`
6. `apply`，仅在显式 `--apply`
7. `benchmark`，仅在显式 `--benchmark`
8. `report`

独立子命令适合：

- 调试单个 stage
- 重跑失败阶段
- 在现有 `work-dir` 上手工继续

## 8. `sample`

示例：

```bash
python3 scripts/oneclick_product.py sample <IMAGE> \
  --work-dir /tmp/sample_only \
  --workload ready-serving-layout-trap-v5
```

常用参数：

- `--startup-mode auto|legacy|gated`
- `--startup-backend-preference auto|prefer-host-launcher|prefer-image-shell`
- `--duration`
- `--workload`
- `--inject-io`
- `--ready-url`
- `--ready-exec`
- `--ready-log-regex`
- `--model-host`
- `--model-dst`
- `--model-name`

容器运行时参数也可直接透传：

- `--container-gpus`
- `--container-ipc`
- `--container-env`
- `--container-mount`
- `--container-entrypoint`
- `--container-cmd`

## 9. `analyze`

示例：

```bash
python3 scripts/oneclick_product.py analyze \
  --work-dir /tmp/sample_only
```

常用参数：

- `--min-events`
- `--max-mmap-maps-ratio`
- `--size-budget-percent`
- `--size-budget-mb`
- `--min-hits`
- `--p0-max`
- `--early-onehit-ratio`
- `--early-onehit-max`
- `--early-onehit-max-size-kb`
- `--selector-mode file|chunk-shadow`

当前 `--allow-low-fidelity` 只是保留的兼容开关。
低质量发现现在默认写进 advisory 字段，而不是直接停止 analyze。
`chunk-shadow` 为基于 image TOC 的实验性 shadow 对照模式，不改变 file-level 采样事实来源，也不是默认主路径。

## 10. `llm-optimize`

直接运行 LLM stage：

```bash
python3 scripts/oneclick_product.py llm-optimize \
  --work-dir /tmp/run_after_analyze \
  --llm-provider openai
```

手工网页导出：

```bash
python3 scripts/oneclick_product.py llm-optimize \
  --work-dir /tmp/run_after_analyze \
  --manual-web
```

手工网页导入：

```bash
python3 scripts/oneclick_product.py llm-optimize \
  --work-dir /tmp/run_after_analyze \
  --manual-response-file /tmp/run_after_analyze/hints_raw.manual.yaml
```

如果导入结果在 grounding 后未形成可用路径集合，当前实现不会采用该 LLM 输出，而是保留 deterministic hints；这一情况本身不等于整个主流程失败。

## 11. `apply`

示例：

```bash
python3 scripts/oneclick_product.py apply \
  --work-dir /tmp/run_after_analyze \
  --src-image <IMAGE> \
  --dst-image <OPT_IMAGE>
```

如果不传 `--dst-image`，在 `run --apply` 中会自动生成：

- `<src-tag>-optimized`
- 或 `:optimized`

当前 apply 成功条件：

- optimize 命令成功
- 目标镜像产物存在
- smoke 可消费

如果这些成立，即使 quality warning 很多，`apply` 仍应是 `passed`。

dry-run：

```bash
python3 scripts/oneclick_product.py apply \
  --work-dir /tmp/run_after_analyze \
  --src-image <IMAGE> \
  --dry-run
```

dry-run 不会生成真正可用的优化镜像，因此结果会带 `quality_status=unknown`，并给出建议而不是可发布结论。

## 12. `benchmark`

### 直接 benchmark 两组比较

```bash
python3 scripts/oneclick_product.py benchmark \
  --work-dir /tmp/bench_two_way \
  --base-image <BASE_IMAGE> \
  --opt-image <OPT_IMAGE>
```

### 当前主实验的 triple benchmark

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

重要规则：

- `--comparison-mode raw-full-vs-estargz-lazy-triple` 必须配 `--unopt-image`
- `run --benchmark --bench-comparison-mode raw-full-vs-estargz-lazy-triple` 必须配 `--bench-unopt-image`

benchmark 相关常用参数：

- `--mode struct|ready`
- `--workload`
- `--runs`
- `--snapshotter`
- `--base-snapshotter`
- `--opt-snapshotter`
- `--pull-policy`
- `--cold-mode local-cold|registry-cold-strict`

benchmark 也支持对应的容器运行时参数：

- `--container-gpus`
- `--container-ipc`
- `--container-env`
- `--container-mount`
- `--container-entrypoint`
- `--container-cmd`

## 13. `report`

```bash
python3 scripts/oneclick_product.py report \
  --work-dir /tmp/imageopt_run
```

报告会汇总：

- overall status
- mode / mode result
- startup backend
- analyze advisory 字段
- LLM fallback 或 grounding 状态
- apply 可消费性
- benchmark validity 与 invalid reasons

## 14. 当前 `ready-serving-layout-trap-v5` 主实验的推荐调用

```bash
python3 scripts/oneclick_product.py run <RAW_IMAGE> \
  --work-dir /tmp/layout_trap_v5_run \
  --workload ready-serving-layout-trap-v5 \
  --mode deterministic \
  --apply \
  --benchmark \
  --bench-mode ready \
  --bench-comparison-mode raw-full-vs-estargz-lazy-triple \
  --bench-unopt-image <UNOPT_ESTARGZ_IMAGE>
```

## 15. Preset 与 workload

当前 `run` 内的 preset 仍然存在：

- `tfpy-half-plus-two-io1g`
- `tf-serving-efficientnet-v2-b0`
- `tf-serving-half-plus-two`

它们只是 CLI 默认值集合。

当前 workload 选择中包含：

- `none`
- `ready-serving-bootstrap-v2`
- `ready-serving-layout-trap-v3`
- `ready-serving-layout-trap-v4`
- `ready-serving-layout-trap-v5`
- `tfpy-half-plus-two-io1g`
- `tf-serving-efficientnet-v2-b0`
- `tf-serving-half-plus-two`

## 16. 恢复和暂停

当 `llm-manual` 进入网页端暂停时：

- `manifest.json` 中的 `llm_optimize` stage 会是 `paused`
- `resume_inputs.manual_response_file.format` 会记录为 `json`
- `resume_command` 会指向 `resume_manual_web_llm.sh` 或等价的 CLI 命令

当 `run` 在同一 `work-dir` 重启时：

- 已 `passed` 的 stage 会被跳过
- `paused` 或 `failed` 的后续 stage 会继续处理

## 17. 建议优先检查的文件

如果需要核对一次 run 是否符合当前合同，建议优先检查：

1. `manifest.json`
2. `quality_gate.json`
3. `selector_stats_hints.json`
4. `llm_result.json`
5. `apply_result.json`
6. `benchmark_result.json`
7. `run_report.json`
8. `run_report.md`
