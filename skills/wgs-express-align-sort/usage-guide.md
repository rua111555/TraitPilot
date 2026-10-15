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
3. **Use conda absolute paths** for all tools — sub-shells may lose PATH.

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
- Parabricks generates the Read Group automatically.
- Still run `gzip -t` preflight — corrupted FASTQ breaks fq2bam too.
- Verify exact flags with `PBRUN 0 fq2bam --help`.

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
