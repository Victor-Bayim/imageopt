# imageopt

Container startup prefetching based on observed OPEN/MMAP file accesses.
The author independently implemented sampling, candidate selection, image
application and evaluation for the Tencent Rhino-bird Open Source Program.

## Try without containers or API keys

Python 3.10+; these checks use the standard library:

```sh
python tools/validate_portable.py
python tools/demo_selector.py
python tools/verify_records.py
python scripts/oneclick_product.py --help
python scripts/oneclick_product.py report --help
```

The 74-test portable suite covers provider contracts, mock/error paths, runtime
forwarding, workload configuration, budgeted selection and quality classification.
The synthetic demo exercises the actual selector and generates
`.work/selector-demo/{hints.yaml,estargz.list,selector_stats.json}`. It checks the
selected hot path, budget and directory rejection.

## Code map

| Concern | Implementation |
| --- | --- |
| CLI, stages, resume | [oneclick_product.py](scripts/oneclick_product.py), [manifest.py](orchestrator/manifest.py), [stages](orchestrator/stages/) |
| Container-scoped OPEN/MMAP | [BPF](sampler/BPF/file_access_ebpf_trace.bpf.c), [collector](sampler/user/sampler_trace_ext.cpp), [ABI](sampler/common/file_event_abi.h) |
| Enrichment and aggregation | [export/enrich](orchestrator/stages/export_enrich.py), [aggregate_candidates.py](optimizer/aggregate_candidates.py) |
| Deterministic selection | [selector](optimizer/tools/select_prefetch_paths.py), [quality_gate.py](optimizer/tools/quality_gate.py) |
| Optional LLM and grounding | [providers](orchestrator/llm_providers/), [llm_optimize](orchestrator/stages/llm_optimize.py), [prepare_preferred_paths.py](optimizer/tools/prepare_preferred_paths.py) |
| Apply and measure | [apply_hints_to_image.sh](scripts/apply_hints_to_image.sh), [coldstart_bench_strict.sh](scripts/coldstart_bench_strict.sh) |
| Rebuild controlled workload | [ready-serving-layout-trap-v5](ready-serving-layout-trap-v5/) |

## Linux workflow

The default mode is deterministic:

```sh
python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/imageopt_run
python3 scripts/oneclick_product.py report --work-dir /tmp/imageopt_run
```

The real flow is preflight → sample → export-enrich → analyze → optional
llm-optimize → explicitly enabled apply/benchmark → report. A working directory
stores manifests and artifacts for resume. Execution/consumption failures are
reported as failures, rather than fabricating successful output names.

Full execution requires Linux, containerd/nerdctl, stargz snapshotter, a test
registry and an eBPF-capable kernel. Native build dependencies include Clang/LLVM,
bpftool, CMake, libbpf, hiredis, redis++, prometheus-cpp, CURL and ZLIB. The BPF
object/skeleton are generated locally and are not committed. Use system bpftool
or BPFT_OVERRIDE when the optional vendored tool is absent.

See [USAGE](docs/USAGE.md), [architecture](docs/ARCHITECTURE.md),
[sampler dependencies](sampler/user/README.md) and [limitations](docs/KNOWN_LIMITATIONS.md).
YAML configuration support may require PyYAML. API credentials come from environment
variables; never commit them or a local `.env`.

The controlled workload includes Go source, Dockerfile, configuration and resource
generators. Its full resource generation writes about 8 GiB locally, and is not
part of the portable checks. Configure your own registry/images and record the
actual host/tool versions for new experiments.

## Preserved results

| Batch | Unhinted lazy | Hinted lazy | Reduction | Evidence |
| --- | ---: | ---: | ---: | --- |
| Deterministic | 2.360 s | 1.677 s | 28.94% | Historical report aggregate |
| LLM manual import | 2.763 s | 2.101 s | 23.96% | Three preserved runs per group |

These are independent batches on a controlled workload, not a rules-vs-LLM
comparison. The LLM-assisted batch imported a manual response; it made no online
API call. Records and full parameters are in [benchmarks/historical](benchmarks/historical/).
`tools/verify_records.py` recomputes nine CSV observations and verifies source hashes.

Sampling is file-level; chunk-aware code provides post-processing/shadow audits.
Results are limited to the measured workload and comparison contract, and do not
establish a universal or production guarantee.
