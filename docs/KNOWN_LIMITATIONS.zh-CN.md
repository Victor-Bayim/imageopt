[English](KNOWN_LIMITATIONS.md) | **简体中文**

# 已知限制

本文件只列当前源码和测试仍然支持的边界。

## 1. 采样边界

- 当前采样仍然是文件级事实采集。
- image TOC、TOC shadow、chunk shadow 只在存在 image TOC 时出现在后处理和报告层。
- 当前实现不属于按 chunk 的直接采样。

## 2. 适用范围边界

- 当前证据集中在 `ready-serving-layout-trap-v5` 主实验路径。
- 当前结果不应外推到任意 workload。

## 3. LLM 边界

- LLM 不是必需组件。
- 当前默认 mode 仍然是 `deterministic`。
- `llm-auto` 在 provider 不可用、响应不可解析、合同不满足，或 grounding 后未形成可用路径集合时，会回退到 deterministic hints。

## 4. 合同边界

- 当前正式 LLM 合同是严格 JSON。
- `legacy YAML` 只作为 manual response 导入兼容存在。
- 默认 manual response 文件名仍可能保留 `.yaml` 后缀，但这不表示 YAML 是正式首选合同。

## 5. 平台边界

- 当前产品策略只覆盖 Linux 单机。
- 运行时前提是 `containerd + nerdctl`。
- 当前前提是 `root` 或免密 `sudo`。

## 6. 质量语义边界

- analyze/apply 的质量问题现在优先作为 advisory 处理。
- `quality_status=warning` 或 `poor` 不自动等于 stage `failed`。
- LLM 输出在 grounding 后未形成可用路径集合时，当前实现不会采用该结果，而是保留 deterministic hints。
- 只有最终无法形成可消费的 `hints.yaml` / `estargz.list`，或执行失败、产物缺失、产物不可消费、benchmark 无有效结果等情况，相关阶段才会失败。

## 7. benchmark 边界

- 当前主结论只应使用 `ready-serving-layout-trap-v5` 的 triple benchmark。
- `runs=1 after advisory fix` 只能当作 smoke / regression check。
- lazy benchmark 受 full-pull guard 约束；guard 判定无效时，benchmark 应直接失败，而不是继续报告性能对比。

## 8. `chunk-shadow` 边界

- `chunk-shadow` 目前仍是实验性平行产物。
- 该模式依赖存在 image TOC 时的后处理审计结果，不改变原始 file-level 采样事实。
- 它不能替代默认 deterministic file selector 的正式地位。

## 9. 入口边界

- 当前唯一正式入口是 `scripts/oneclick_product.py`。
