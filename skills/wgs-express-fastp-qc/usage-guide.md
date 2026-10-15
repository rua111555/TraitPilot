---
name: wgs-express-fastp-qc
usage: |
  This skill is invoked by the wgs-expressive-pipeline orchestration skill during
  Stage 0.5 (optional FASTQ QC), or independently by users who need FASTQ
  preprocessing without running the full WGS pipeline.
---

# fastp QC — Usage Guide

## 概述

fastp 是一个现代化的 FASTQ 一站式预处理工具，单次 C++ 运行完成：
**接头修剪 + 质量过滤 + poly-G 切除 + 碱基校正 + QC 报告**。

在 WGS 管线中作为 Stage 0.5（可选步骤），在参数提案之后、BWA 比对之前执行。
也可独立使用用于任何 FASTQ 数据的清洗。

## 前提条件

```bash
# 安装 fastp
conda install -n Agent -c bioconda fastp

# 验证
fastp --version
# 期望: fastp 0.23.4

# MultiQC（可选，用于聚合报告）
conda install -n Agent -c bioconda multiqc
```

## 在 WGS 管线中的位置

```
参数提案 (Stage 0)
    ↓
fastp QC (Stage 0.5，可选)   ← 本 skill
    ↓
BWA 比对 (Stage 1)
    ↓
...
```

**数据流**：
- 原始 FASTQ 从数据库 `fastq_samples` 表读取 → `read1_path` / `read2_path`
- 清洗后 FASTQ 输出到 `{RUN_DIR}/out/{sample}.clean_1.fq.gz`
- 清洗后文件**不入数据库**，只作为下一阶段的文件输入

## 示例 Prompt

### 作为 WGS 管线步骤
> "把 fastp QC 加进管线，在 BWA 之前先清洗一下数据"

> "重新跑 fastp QC，参数用 Q20、最短 36bp、3' 滑窗修剪"

### 独立使用
> "帮我把这批 FASTQ 用 fastp 清洗一下"

> "NovaSeq 数据，需要 poly-G 切除"

> "处理完用 MultiQC 聚合所有报告"

## 关键参数（WGS 推荐值）

| 参数 | 作用 | WGS 推荐值 |
|------|------|-----------|
| `-q` | 合格碱基质量阈值 | 20 |
| `-u` | 不合格碱基%上限 | 40 |
| `-l` | 最短 read 长度 | 36 |
| `--cut_right` | 3' 滑窗质量修剪 | 开启 |
| `--cut_window_size` | 滑窗大小 | 4 |
| `--cut_mean_quality` | 滑窗平均质量 | 20 |
| `-w` / `--thread` | 线程数 | CPU 核数 - 2 |
| `--detect_adapter_for_pe` | PE 额外序列匹配 | 开启 |
| `--trim_poly_g` | Poly-G 切除 | 2色仪器开启 |
| `--correction` | 重叠碱基校正 | 可选 |

## 常见错误

| 症状 | 原因 | 解决方法 |
|------|------|---------|
| 变异检测假阴性 | 使用了 `--dedup` 去除了真实变异 reads | WGS 禁用 `--dedup` |
| 接头未切除（SE） | SE 无 overlap | 显式提供 `--adapter_sequence` |
| Poly-G 残留 | 4色仪器或自动检测漏了 | 显式加 `--trim_poly_g` |

## 与传统流程对比

| 任务 | 传统 | fastp |
|------|------|-------|
| QC 报告 | FastQC | 内置 HTML/JSON |
| 接头切除 | Cutadapt | Overlap 分析（无需序列） |
| 质量修剪/过滤 | Trimmomatic | 内置 |
| Poly-G 切除 | 手动 | 自动（2色仪器） |
| 速度 | 3 次 I/O 遍历 | 1 次 I/O 遍历 |

## 资源

- [fastp GitHub](https://github.com/OpenGene/fastp)
- Chen S. 2018. fastp: an ultra-fast all-in-one FASTQ preprocessor. Bioinformatics 34(17):i884-i890.
- [MultiQC](https://multiqc.info/)

## 相关 Skill

- wgs-express-params-propose — 前一阶段：参数提案
- wgs-express-align-sort — 后一阶段：BWA 比对
