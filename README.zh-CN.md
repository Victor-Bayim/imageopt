[English](README.md) | **简体中文**

# imageopt：基于 eBPF 的容器冷启动优化

使用 eBPF 观测容器真实文件访问模式，并生成高质量 eStargz prefetch hints。在受控工作负载上，懒加载冷启动延迟降低 **23–29%**。

## 能力

- eBPF 采集容器作用域内的 OPEN/MMAP 事件
- 确定性候选聚合与质量门控
- 可选的 LLM 辅助路径精炼
- 一键应用 + 严格冷启动基准测试流水线

## 实测结果

| 批次       | 无 Hint  | 有 Hint  | 降低幅度 |
|------------|----------|----------|----------|
| 确定性     | 2.360 s  | 1.677 s  | **28.94%** |
| LLM 辅助   | 2.763 s  | 2.101 s  | **23.96%** |

所有记录均可独立验证。采样为文件级，支持块级后处理。

## 使用

```bash
# 便携验证（无需容器）
python tools/validate_portable.py
python tools/demo_selector.py

# 完整 Linux 流程
python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/run
python3 scripts/oneclick_product.py report --work-dir /tmp/run
```

为腾讯犀牛鸟开源计划开发。需要 Linux、containerd、stargz-snapshotter 以及支持 eBPF 的内核。
