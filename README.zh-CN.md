[English](README.md) | **简体中文**

# imageopt：基于 eBPF 的容器冷启动优化

使用 eBPF 观测容器真实文件访问模式，并生成高质量 eStargz prefetch hints。在受控工作负载上，懒加载冷启动延迟降低 **23–29%**。

## 实现内容

容器启动过程中很多层是懒加载的。imageopt 捕获容器命名空间内实际发生的 OPEN 与 MMAP 事件：

- 基于 eBPF 的容器作用域文件访问事件采样，开销低。
- 确定性聚合与质量门控选择，筛选最值得预取的文件。
- 可选的 LLM 辅助路径精炼（当外部模型可用时）。
- 完整的应用与基准测试流水线，将生成的 hints 注入 eStargz 镜像，并在 stargz-snapshotter 下测量冷启动时间。

工具产出 hint 文件以及可复现的测量工作流。采样为文件级，支持块级后处理以进行更细粒度分析。

## 实测结果

| 批次       | 无 Hint 懒加载 | 有 Hint 懒加载 | 降低幅度 |
|------------|----------------|----------------|----------|
| 确定性     | 2.360 s        | 1.677 s        | **28.94%** |
| LLM 辅助   | 2.763 s        | 2.101 s        | **23.96%** |

结果来自受控工作负载上的独立批次，并可独立验证。LLM 批次使用了手工导入的响应（无在线 API 调用）。

## 使用

```bash
# 便携验证（无需容器或 API 密钥）
python tools/validate_portable.py
python tools/demo_selector.py

# 完整 Linux 流程
python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/run
python3 scripts/oneclick_product.py report --work-dir /tmp/run
```

为腾讯犀牛鸟开源计划开发。完整执行需要 Linux、containerd/nerdctl、stargz-snapshotter 以及支持 eBPF 的内核。
