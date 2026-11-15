[English](README.md) | **简体中文**

# imageopt：基于 eBPF 的容器冷启动优化

通过 eBPF 记录容器真实 OPEN/MMAP 模式并生成 eStargz prefetch hints 的冷启动优化。在受控工作负载上，懒加载冷启动时间从 2.360 s 降至 1.677 s（确定性），从 2.763 s 降至 2.101 s（LLM 辅助批次）——分别降低 **28.9%** 和 **24.0%**。

采样器挂载 eBPF 程序，观测容器命名空间内的文件访问事件。这些事件按路径聚合、经过质量门控过滤，然后转成预取列表。可选的 LLM 阶段可在提供模型时对优选路径重排；默认路径完全确定。应用脚本把 hints 注入 eStargz 镜像，严格基准测试在 stargz-snapshotter 下测量冷启动时间。

便携检查（无需容器、无需 API 密钥）覆盖 selector、质量门控、provider 合同和记录验证。完整 Linux 流程需要 containerd/nerdctl、stargz-snapshotter 和支持 eBPF 的内核。采样为文件级；支持块级后处理以供后续分析。

```bash
# 便携检查
python tools/validate_portable.py
python tools/demo_selector.py
python tools/verify_records.py

# 完整流程
python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/run
python3 scripts/oneclick_product.py report --work-dir /tmp/run
```

为腾讯犀牛鸟开源计划开发。
