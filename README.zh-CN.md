[English](README.md) | **简体中文**

# imageopt

基于实际 OPEN/MMAP 文件访问的容器启动预取优化。
作者在腾讯犀牛鸟开源人才培养计划中独立实现了采样、候选选择、镜像应用与评测。

## 无需容器或 API 密钥即可体验

需要 Python 3.10+；以下检查仅使用标准库：

```sh
python tools/validate_portable.py
python tools/demo_selector.py
python tools/verify_records.py
python scripts/oneclick_product.py --help
python scripts/oneclick_product.py report --help
```

74 项可移植测试覆盖 provider 合同、模拟与异常路径、运行时参数透传、工作负载配置、预算约束选择和质量分类。
合成演示运行真实 selector，生成 `.work/selector-demo/{hints.yaml,estargz.list,selector_stats.json}`，检查热路径选择、预算和目录拒绝行为。

## 代码导航

| 功能 | 实现 |
| --- | --- |
| CLI、阶段与恢复 | [oneclick_product.py](scripts/oneclick_product.py)、[manifest.py](orchestrator/manifest.py)、[stages](orchestrator/stages) |
| 容器范围内的 OPEN/MMAP | [BPF](sampler/BPF/file_access_ebpf_trace.bpf.c)、[collector](sampler/user/sampler_trace_ext.cpp)、[ABI](sampler/common/file_event_abi.h) |
| 元数据补强与聚合 | [export/enrich](orchestrator/stages/export_enrich.py)、[aggregate_candidates.py](optimizer/aggregate_candidates.py) |
| 确定性选择 | [selector](optimizer/tools/select_prefetch_paths.py)、[quality_gate.py](optimizer/tools/quality_gate.py) |
| 可选 LLM 与 grounding | [providers](orchestrator/llm_providers)、[llm_optimize](orchestrator/stages/llm_optimize.py)、[prepare_preferred_paths.py](optimizer/tools/prepare_preferred_paths.py) |
| 应用与测量 | [apply_hints_to_image.sh](scripts/apply_hints_to_image.sh)、[coldstart_bench_strict.sh](scripts/coldstart_bench_strict.sh) |
| 重建受控工作负载 | [ready-serving-layout-trap-v5](ready-serving-layout-trap-v5) |

## Linux 工作流

默认模式为 deterministic：

```sh
python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/imageopt_run
python3 scripts/oneclick_product.py report --work-dir /tmp/imageopt_run
```

执行流程为 preflight → sample → export-enrich → analyze → 可选 llm-optimize → 显式启用的 apply/benchmark → report。
工作目录保存 manifest 和产物以支持恢复。执行或消费失败会报告失败，不会虚构成功的输出名称。

完整执行需要 Linux、containerd/nerdctl、stargz snapshotter、测试镜像仓库，以及支持 eBPF 的内核。
原生构建依赖包括 Clang/LLVM、bpftool、CMake、libbpf、hiredis、redis++、prometheus-cpp、CURL 和 ZLIB。
BPF 对象与 skeleton 在本地生成，不提交到仓库。缺少可选内置工具时，可使用系统 bpftool 或 BPFT_OVERRIDE。

参见[使用说明](docs/USAGE.zh-CN.md)、[架构](docs/ARCHITECTURE.zh-CN.md)、[采样器依赖](sampler/user/README.zh-CN.md)和[限制](docs/KNOWN_LIMITATIONS.zh-CN.md)。
YAML 配置可能需要 PyYAML。API 凭据通过环境变量提供；不要提交凭据或本地 `.env`。

受控工作负载包含 Go 源码、Dockerfile、配置与资源生成器。完整资源生成会在本地写入约 8 GiB，不属于可移植检查的一部分。
新实验需配置自己的镜像仓库与镜像，并记录实际主机和工具版本。

## 归档结果

| 批次 | 未加 hints 的 lazy | 加 hints 的 lazy | 降幅 | 依据 |
| --- | ---: | ---: | ---: | --- |
| Deterministic | 2.360 s | 1.677 s | 28.94% | 历史报告汇总 |
| LLM 手工导入 | 2.763 s | 2.101 s | 23.96% | 每组保留三次运行 |

两者是受控工作负载上的独立批次，不构成规则与 LLM 的直接对比。
LLM 辅助批次使用手工响应导入，没有在线 API 调用。记录和完整参数见 [benchmarks/historical](benchmarks/historical)。
`tools/verify_records.py` 复算九条 CSV 观测并校验源码哈希。

采样为文件级；chunk-aware 代码提供后处理与 shadow 审计。
结果限于测量的工作负载与比较合同，不构成通用或生产环境保证。
