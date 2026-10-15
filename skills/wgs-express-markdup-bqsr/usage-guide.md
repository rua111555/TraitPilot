---
name: wgs-express-markdup-bqsr
usage: |
  This skill is NOT invoked directly by users. It is called by the
  wgs-express-pipeline orchestration skill during Stage 2.
---

# MarkDuplicates + BQSR — Usage Guide

## Purpose

Mark PCR/optical duplicates and perform base quality recalibration (BQSR).

## Workflow

```
sorted.bam → MarkDuplicates → marked.bam → BaseRecalibrator → recal.table
                                                                    ↓
                                            marked.BQSR.bam ← ApplyBQSR
```

## Key Parameters

| Parameter | Source | Default |
|-----------|--------|---------|
| `-Xmx` | `run.params.json` `java_xmx` | 32g |
| known indels | `run.params.json` `known_sites.indels` | — |
| known snps | `run.params.json` `known_sites.snps` | — |

## Parabricks / GPU Mode

When `compute_backend: "parabricks"`, replace GATK with `pbrun` (one GPU per sample)
via the `PBRUN()` docker wrapper (rendered by params-propose); container sees one
card, so pbrun uses `--num-gpus 1`:

```bash
PBRUN "${GPU_IDX}" markdup    --in-bam "${INPUT_BAM}" --out-bam "${MARKDUP_BAM}" \
                    --out-duplicate-metrics "${METRICS}" --num-worker-threads "${per_gpu_cpus}" --num-gpus 1
PBRUN "${GPU_IDX}" bqsr       --ref "${REF}" --in-bam "${MARKDUP_BAM}" \
                    --knownSites "${KNOWN_SNPS}" --knownSites "${KNOWN_INDELS}" \
                    --out-recal-file "${RECAL_TABLE}" --num-gpus 1
PBRUN "${GPU_IDX}" applybqsr  --ref "${REF}" --in-bam "${MARKDUP_BAM}" \
                    --in-recal-file "${RECAL_TABLE}" --out-bam "${BQSR_BAM}" \
                    --num-gpus 1
```

- Keep the same output file-name contract (`*.sorted.markdup.bam`, `*.BQSR.bam`, `*.recal.table`).
- ⚠ 4.3.x `fq2bam` does MarkDuplicates + BQSR **by default** — skip `markdup`
  when Stage 1 ran fq2bam (see setup §4.1 for the parameter cheat-sheet).
- 4.3.x flag names differ from GATK: `--knownSites` (camelCase), `--out-duplicate-metrics`,
  `--num-worker-threads`; always check `pbrun <tool> --help`.

## Reference

- GATK Best Practices: Base (Quality Score) Recalibration
- bio-align-files-duplicate-handling — Duplicate handling reference
- bio-variant-calling-gatk-variant-calling — BQSR/DRAGEN-mode decision guidance

## Related Skills

- wgs-express-align-sort — Previous stage
- wgs-express-haplotypecaller — Next stage
- bio-alignment-files-duplicate-handling — Duplicate handling patterns
- bio-variant-calling-gatk-variant-calling — BQSR rationale and alternatives
