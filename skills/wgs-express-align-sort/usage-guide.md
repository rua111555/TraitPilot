---
name: wgs-express-align-sort
usage: |
  This skill is NOT invoked directly by users. It is called by the
  wgs-express-pipeline orchestration skill during Stage 1.
---

# BWA MEM + samtools sort — Usage Guide

## Purpose

Align paired-end reads to the reference genome using BWA MEM and sort the output
to coordinate-sorted BAM files. This is Stage 1 of the WGS expressive pipeline.

## How It Works

1. Read `run.params.json` for all parameters (samples, reference, threads, etc.)
2. For each sample: BWA MEM → SAM file → samtools sort → BAM → index
3. Runs multiple samples in parallel with NUMA affinity binding
4. Writes `.done` marker and updates database on completion

## Key File Paths

- **Output BAM**: `{RUN_DIR}/out/{sample_name}.sorted.bam`
- **Log**: `{RUN_DIR}/logs/01_bwa_sort.log`
- **Script**: `{RUN_DIR}/scripts/01_bwa_sort.sh`

## Critical Rules

1. **Never pipe BWA into samtools** — BWA stderr contains progress info that
   corrupts the SAM stream. Always write BWA output to a temp SAM file first.
2. **Always add Read Group (`-R`)** — GATK tools require `@RG` in the BAM header.
   Format: `@RG\tID:{sample}\tSM:{sample}\tLB:{sample}\tPL:ILLUMINA`
3. **Probe tools with `command -v`** — never hardcode machine-specific conda
   absolute paths (repo hard rule #5.1). Stage scripts run under
   `conda run -n Agent bash`, so the tools are already on `PATH`:
   ```bash
   resolve_tool() { command -v "$1" || { echo "✗ 找不到 $1 —— 请激活 conda 环境 Agent" >&2; exit 1; }; }
   BWA="$(resolve_tool bwa)"
   SAMTOOLS="$(resolve_tool samtools)"
   ```
   ⚠ 旧版本这里写的是「Use conda absolute paths for all tools」—— 那会把某台机器的
   conda 路径固化进脚本，换机即失效，且与仓库硬规则 #5.1 直接冲突。

## Parabricks / GPU Mode

When `run.params.json` sets `compute_backend: "parabricks"` (GPU present +
Parabricks ready + smoke test passed), replace BWA+samtools with **one command**
inside the `PBRUN()` docker wrapper (rendered by params-propose):
`PBRUN <host_gpu_index> fq2bam ... --num-gpus 1`

```bash
# $1 = host GPU index; container sees only that card → pbrun uses --num-gpus 1
PBRUN "${GPU_IDX}" fq2bam \
    --ref "${REF_FASTA}" \
    --in-fq "${R1}" "${R2}" \
    --out-bam "${BAM}" \
    --read-group-sm "${sample_name}" --read-group-lb "${sample_name}" --read-group-pl ILLUMINA \
    --num-cpu "${per_gpu_cpus}" \
    --num-gpus 1 \
    --low-memory --bwa-nstreams 1 \
    --tmp-dir "${TMP_DIR}"
```

- One GPU per sample; `${GPU_IDX} = i % GPU_COUNT`.
- ⚠ On 4090 24GB + full-genome ref, `--low-memory --bwa-nstreams 1` is **required** —
  default streams OOM loading the bwa index (`cudaSafeCall ... out of memory`).
- 4.3.x fq2bam already runs MarkDuplicates + BQSR by default (see setup §4.1).
- Output file name must stay `{sample}.sorted.bam` to honor the Stage 2/3 input contract.
- ⚠ **必须显式传 `--read-group-sm "${sample_name}"`** —— 见下方说明。
- Still run `gzip -t` preflight — corrupted FASTQ breaks fq2bam too.
- Verify exact flags with `PBRUN 0 fq2bam --help`.

> ⚠ **`--read-group-sm` 是必需项，不是可选优化**（本机实跑 `pbrun fq2bam` 复核）。
> 旧版本此处写的是「Parabricks generates the Read Group automatically」—— **那句话是错的**：
> 不传 `--read-group-sm` 时 fq2bam 自动生成的是**写死的常量读组**
> `@RG\tID:<prefix>.1\tLB:lib1\tPL:bar\tSM:sample`，其中 `SM:sample` 与真实样本号无关，
> **每个样本都是同一个 `sample`**。后果是连锁的，且全部以"健康数据被拦"的面貌出现：
> · Stage 1 门禁 `bam[s].readgroup` 是 **hard**（要求 `SM == 文件名里的样本号`）→ 每样本必拦；
> · 该 `SM:sample` 会**继承到下游** GVCF 的样本列，Stage 3 的 `gvcf[s].samples` /
>   `gvcf[s].identity` 两条 hard 判据连锁失败；
> · 整个队列都是 `SM:sample` 时 Stage 4 `CombineGVCFs` 会合并失败或把多样本压成一个。
> 而 `compute_backend=parabricks` 是 GPU+冒烟通过时的**默认路径** —— 即不传此参数时默认路径全阶段不可用。
> `--read-group-pl` 用 `ILLUMINA` 与 CPU 路径的 `RG_STR` 保持一致；
> `--read-group-lb` 传样本号，使同一样本的多个文库可被正确识别。
> 完整论证见 [`SKILL.md`](SKILL.md) 的「Parabricks 模式」段。

## 错误预防清单（从实际运行经验总结）

| # | 错误描述 — 日志关键信息 | 违反后果 |
|---|--------------------------|----------|
| 1 | **FASTQ gzip 文件损坏** — BWA 报 `invalid compressed data--format violated` / `the 2nd file has fewer sequences` / `paired reads have different names`，子进程以非零码退出，`Subprocess failed` | 该样本 BWA 失败，无 BAM 产出 |
| 2 | **补跑脚本并发池缺陷** — 主跑时 `wait`+`unset` 数组索引错乱导致部分样本被跳过（Stage 1 首次运行跳过 18/161） | 部分样本未处理，需补跑 |

**应对 FASTQ 损坏**：启动前先批量做 `gzip -t` 完整性检查，R1/R2 需配对一致；发现损坏需重新获取原始数据，无法从管线侧修复。


## Quality Checks

After completion, verify with:
```bash
samtools flagstat {BAM} | head -5
samtools view -H {BAM} | grep '@RG'
```

## Related Skills

- wgs-express-server-detect — Server detection and parallelism computation
- wgs-express-params-propose — Parameter proposal and database queries
- wgs-express-markdup-bqsr — Next stage: MarkDuplicates + BQSR
- bio-read-alignment-bowtie2-alignment — Alternative aligner (Bowtie2)
- bio-alignment-files-sam-bam-basics — SAM/BAM format reference
