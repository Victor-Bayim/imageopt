[English](README.md) | **简体中文**

# imageopt：eBPF 容器冷启动

eBPF 看容器里的 OPEN/MMAP，写出 eStargz prefetch hints。受控工作负载，懒加载冷启动：

| 路径 | 之前 | 之后 | 降幅 |
| --- | ---: | ---: | ---: |
| 确定性 | 2.360 s | 1.677 s | **28.9%** |
| LLM 重排 | 2.763 s | 2.101 s | **24.0%** |

采样器挂一个 BPF 程序，范围限定在容器命名空间。事件按路径聚合，过质量门，写成预取列表。默认选择是确定的。接上模型时，LLM 阶段可以重排这个列表。应用脚本把 hints 写进 eStargz 镜像；严格基准在 stargz-snapshotter 下测冷启动。

采样是文件级。块级后处理在仓库里，用于第二遍。便携检查（selector、质量门、provider 合同、记录哈希）只要 Python 3.10 和标准库。完整路径要 Linux、containerd/nerdctl、stargz-snapshotter，以及带 eBPF 的内核。

```bash
python tools/validate_portable.py
python tools/demo_selector.py
python tools/verify_records.py

python3 scripts/oneclick_product.py run <IMAGE> --work-dir /tmp/run
python3 scripts/oneclick_product.py report --work-dir /tmp/run
```

腾讯犀牛鸟开源计划。
