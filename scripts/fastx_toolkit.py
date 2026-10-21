#!/usr/bin/env python3
"""fastx_toolkit.py — 轻量 FASTA/FASTQ 处理工具。

仅依赖标准库，提供四个子命令：

    stats   序列数 / 总长 / N50 / GC / 长度分布
    filter  按长度、GC、N 比例过滤序列
    sample  随机抽取（默认 1%）子集，便于快速试跑
    qc      逐碱基质量统计（FASTQ），输出 read 长度与 Q20/Q30 比例

示例：
    python fastx_toolkit.py stats reads.fq.gz
    python fastx_toolkit.py filter -i in.fq -o out.fq -m 100 -x 10
    python fastx_toolkit.py sample -i in.fq -o sub.fq --frac 0.01 --seed 42
    python fastx_toolkit.py qc reads.fq

多点序列（FASTA 换行）与 gz 压缩输入均可。
"""

from __future__ import annotations

import argparse
import gzip
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Iterator, NamedTuple


class Record(NamedTuple):
    name: str
    seq: str
    qual: str | None


def open_maybe_gz(path: str, mode: str = "rt"):
    """按扩展名透明解压；mode 必须是文本模式。"""
    if path in ("-", "/dev/stdin"):
        return sys.stdin
    if path.endswith(".gz"):
        return gzip.open(path, mode)
    return open(path, mode)


def parse_fastx(path: str) -> Iterator[Record]:
    """流式解析 FASTA/FASTQ，返回 Record 迭代器。"""
    with open_maybe_gz(path) as fh:
        line = fh.readline()
        while line:
            if not line.strip():
                line = fh.readline()
                continue
            marker = line[0]
            if marker == ">":  # FASTA
                name = line[1:].strip()
                parts: list[str] = []
                line = fh.readline()
                while line and line[0] not in ">":
                    parts.append(line.strip())
                    line = fh.readline()
                yield Record(name, "".join(parts), None)
            elif marker == "@":  # FASTQ
                name = line[1:].strip()
                seq = fh.readline().strip()
                fh.readline()  # 跳过 '+'
                qual = fh.readline().strip()
                line = fh.readline()
                yield Record(name, seq, qual)
            else:
                raise ValueError(f"无法识别的记录起始: {line[:40]!r}")


def gc_content(seq: str) -> float:
    if not seq:
        return 0.0
    gc = seq.count("G") + seq.count("C") + seq.count("g") + seq.count("c")
    return gc / len(seq)


def n50(lengths: list[int]) -> int:
    """按长度降序累计到总量一半时的长度。"""
    if not lengths:
        return 0
    half = sum(lengths) / 2
    acc = 0
    for ln in sorted(lengths, reverse=True):
        acc += ln
        if acc >= half:
            return ln
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    lengths: list[int] = []
    gc_sum = 0.0
    n_bases = 0
    hist: Counter[int] = Counter()
    for rec in parse_fastx(args.input):
        ln = len(rec.seq)
        lengths.append(ln)
        gc_sum += gc_content(rec.seq) * ln
        n_bases += rec.seq.upper().count("N")
        hist[ln] += 1
    if not lengths:
        print("没有序列。", file=sys.stderr)
        return 1
    total = sum(lengths)
    print(f"序列数      : {len(lengths)}")
    print(f"总碱基数    : {total}")
    print(f"平均长度    : {total / len(lengths):.1f}")
    print(f"最短/最长   : {min(lengths)} / {max(lengths)}")
    print(f"N50         : {n50(lengths)}")
    print(f"GC 含量     : {gc_sum / total * 100:.2f}%")
    print(f"N 碱基比例  : {n_bases / total * 100:.3f}%")
    if args.hist:
        print("长度分布    : 长度\t条数")
        for ln in sorted(hist):
            print(f"              {ln}\t{hist[ln]}")
    return 0


def cmd_filter(args: argparse.Namespace) -> int:
    kept = dropped = 0
    with open_maybe_gz(args.input) as _:  # 触发路径校验（快速失败）
        pass
    with open(args.output, "w") as out:
        for rec in parse_fastx(args.input):
            ln = len(rec.seq)
            gc = gc_content(rec.seq)
            n_frac = rec.seq.upper().count("N") / ln if ln else 1.0
            ok = (
                ln >= args.min_len
                and (args.max_len is None or ln <= args.max_len)
                and gc >= args.min_gc / 100
                and gc <= args.max_gc / 100
                and n_frac <= args.max_n / 100
            )
            if not ok:
                dropped += 1
                continue
            kept += 1
            if rec.qual is None:
                out.write(f">{rec.name}\n{rec.seq}\n")
            else:
                out.write(f"@{rec.name}\n{rec.seq}\n+\n{rec.qual}\n")
    print(f"保留 {kept} 条，丢弃 {dropped} 条 -> {args.output}")
    return 0


def cmd_sample(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    kept = total = 0
    with open(args.output, "w") as out:
        for rec in parse_fastx(args.input):
            total += 1
            if rng.random() > args.frac:
                continue
            kept += 1
            if rec.qual is None:
                out.write(f">{rec.name}\n{rec.seq}\n")
            else:
                out.write(f"@{rec.name}\n{rec.seq}\n+\n{rec.qual}\n")
    print(f"从 {total} 条中抽样 {kept} 条 ({kept / max(total, 1) * 100:.2f}%) -> {args.output}")
    return 0


def cmd_qc(args: argparse.Namespace) -> int:
    """FASTQ 质量概览：Q20/Q30 比例与碱基质量分布。"""
    offset = 33
    total_bases = q20 = q30 = 0
    pos_sum: Counter[int] = Counter()
    pos_cnt: Counter[int] = Counter()
    n_reads = 0
    for rec in parse_fastx(args.input):
        if rec.qual is None:
            raise ValueError("qc 子命令需要 FASTQ（含质量行）输入")
        n_reads += 1
        for i, ch in enumerate(rec.qual):
            q = ord(ch) - offset
            total_bases += 1
            if q >= 20:
                q20 += 1
            if q >= 30:
                q30 += 1
            pos_sum[i] += q
            pos_cnt[i] += 1
    if not total_bases:
        print("没有碱基。", file=sys.stderr)
        return 1
    print(f"reads       : {n_reads}")
    print(f"总碱基      : {total_bases}")
    print(f"Q20 比例    : {q20 / total_bases * 100:.2f}%")
    print(f"Q30 比例    : {q30 / total_bases * 100:.2f}%")
    print(f"平均质量    : {sum(pos_sum.values()) / total_bases:.2f}")
    print("前 10 位平均质量:")
    for i in sorted(pos_cnt)[:10]:
        print(f"  pos {i + 1:>3}: {pos_sum[i] / pos_cnt[i]:.1f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="轻量 FASTA/FASTQ 处理工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stats", help="基本统计")
    s.add_argument("input")
    s.add_argument("--hist", action="store_true", help="打印长度分布")
    s.set_defaults(func=cmd_stats)

    f = sub.add_parser("filter", help="按条件过滤")
    f.add_argument("-i", "--input", required=True)
    f.add_argument("-o", "--output", required=True)
    f.add_argument("-m", "--min-len", type=int, default=1)
    f.add_argument("-x", "--max-len", type=int, default=None)
    f.add_argument("--min-gc", type=float, default=0.0, help="GC 下限 %%")
    f.add_argument("--max-gc", type=float, default=100.0, help="GC 上限 %%")
    f.add_argument("--max-n", type=float, default=100.0, help="N 比例上限 %%")
    f.set_defaults(func=cmd_filter)

    sp = sub.add_parser("sample", help="随机抽样")
    sp.add_argument("-i", "--input", required=True)
    sp.add_argument("-o", "--output", required=True)
    sp.add_argument("--frac", type=float, default=0.01)
    sp.add_argument("--seed", type=int, default=0)
    sp.set_defaults(func=cmd_sample)

    q = sub.add_parser("qc", help="FASTQ 质量统计")
    q.add_argument("input")
    q.set_defaults(func=cmd_qc)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
