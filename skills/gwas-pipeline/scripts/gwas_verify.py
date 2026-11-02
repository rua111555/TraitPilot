#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gwas_verify.py —— GWAS 管线各阶段的**结果值**校验器（阶段间门禁）

它解决什么问题
==============
GWAS 各阶段脚本原本只在末尾 `date > "${RUN_DIR}/.done/<stage>.done"`。`set -e` 能挡住
"工具返回非零"，但挡不住**返回 0 而产物是错的**：

    plink -maf 0.6 把位点全滤光 → 空 bfile，rc=0 → `.done` 照写 → GEMMA/rMVP 拿到 0 个
    位点（或直接报"没有可分析个体"，也可能安静地吐出只有表头的结果）
    样本在 `--keep` 里被丢了一半 → 关联结果少了一半人 → 无任何环节会报错
    `-maf` 参数写错/没传进去 → 过滤没生效，结果看起来完全正常

本脚本把这些"结果值是否可信"变成**可执行判据**，供阶段脚本在写 `.done` 之前调用：
不通过 → 写 `.failed` 而不写 `.done` → 编排层停止推进。

判据分级（与 WGS 门禁同一套语义，见 `skills/_shared/verify_core.py`）
------------------------------------------------------------------
  hard  不过 → 该阶段判失败，**不写 .done**
  warn  不过 → 仍写 .done，但告警进 `.verify/<stage>.json` 与 stdout，编排层汇报时带出

设计约束
--------
- **只解析产物，不重跑重活**：判据读 `.bed/.bim/.fam`、过滤后 `.frq/.lmiss/.hwe`、
  关联结果表与图文件；不替用户重跑 plink/gemma/rMVP。唯一的例外是**数 VCF 行数**，
  且带体积上限（超限则登记"未做该判定"而不是静默跳过）。
- **不变量优先于阈值**：`.bed` 的字节数必须等于 `3 + n_var × ⌈n_ind/4⌉`（PLINK 1
  SNP-major 格式的结构不变量，抓截断，不需要任何工具）；样本集只能减不能凭空变；
  位点数只能减不能增。这些是**等式**，不是经验阈值。
- **阈值必须由调用方声明**（`--maf/--geno/--hwe/--min-snps/--min-samples`，或
  `run.params.json` 的 `filter` / `min_*`）：声明后按 hard 判；未声明则登记一条 warn
  **明说"未做该判定"** —— 不静默通过，也不误拦。
- 工具用 `command -v` 语义的 `tool()` 运行时探测（本文件其实不需要外部工具）。

用法（由阶段脚本在写 .done 前调用）
-----------------------------------
    python "${VERIFIER}" --stage 00_format_convert --run-dir "${RUN_DIR}" \\
        --prefix "${PREFIX}" --vcf "${IN_VCF}"

    python "${VERIFIER}" --stage 00.5_qc_filter --run-dir "${RUN_DIR}" \\
        --prefix "${OUT_PREFIX}" --input-bfile "${IN_BFILE}" \\
        --qc-prefix "${OUT_PREFIX}.qc" --maf 0.01 --geno 0.2 --hwe 1e-6

⚠️ 参数缺省时回落到 `{RUN_DIR}/run.params.json`，键名见 `_PARAM_KEYS`。
退出码：0=通过（含仅告警），1=有 hard 项未过。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import sys

# 判据容器/进程调用/阶段注册与 WGS 门禁**共用同一份**（skills/_shared/verify_core.py）。
# ⚠️ sys.path 注入而非相对 import：本脚本被阶段脚本以绝对路径调用，不在任何包内。
_SHARED = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "_shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)
from verify_core import (  # noqa: E402
    Report, have, make_stage_registry, run, tool,
)

_STAGES, stage = make_stage_registry()

# 从 run.params.json 读取的键（CLI 优先）。集中列出便于将来加 schema 时对账：
# 校验器读的每个键都必须能在 Stage 0 的 schema 里声明，否则就是"读得到、写不出"
# 的纸面通路（WGS 侧踩过：min_variants_indel 只在 warn 文案里教用户怎么写）。
_PARAM_KEYS = ("prefix", "vcf", "input_bfile", "qc_prefix", "filter",
               "min_snps", "min_samples", "keep", "remove",
               # S1–S5（Phase 2/3）
               "bfile", "grm_prefix", "pca_prefix", "assoc", "kinship", "pheno",
               "trait", "methods", "csv_dir", "map", "source", "sig_file",
               "sig_thresh", "images_dir", "region", "region_gt", "out_prefix",
               "top_site", "keep_file")

# 位点数/样本数下限的"显式不做"语义（与 WGS 的 min_variants 一致）：
#   正整数 → 判定并 hard；整数 0 → 明确不做（登记 warn 说明）；其它 → 畸形，hard。
def _parse_floor(raw, label):
    """返回 (value, err)。value=None 表示"未声明"，err 非空表示畸形值。"""
    if raw is None or raw == "":
        return None, None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None, f"{label} 不是整数：{raw!r}"
    if v < 0:
        return None, f"{label} 不能为负：{raw!r}"
    return v, None


class GwasReport(Report):
    """GWAS 报告：去掉 reads 相关字段（那是 WGS 的概念），加上跨阶段可复用的实测值。

    ⚠️ 为什么不直接改基类：WGS 的 `.verify/*.json` 被下游 `--prev-reads-file` 直接读
    `read_totals`/`primary_totals`，改基类会让 WGS 的报告 JSON 变样。子类化让两边
    各自稳定 —— 这也是共享骨架能落地的前提。
    """

    def __init__(self, stage_name, run_dir):
        super().__init__(stage_name, run_dir)
        # 跨阶段对账用的实测值：下一阶段可以比 n_samples / sample_ids_sha / n_variants
        self.extras: dict = {}

    def as_dict(self):
        d = super().as_dict()
        d.pop("read_totals", None)
        d.pop("primary_totals", None)
        d.update(self.extras)
        return d


# --------------------------------------------------------------------------- #
# PLINK 数据集的读取（bfile / ped+map 两种形态都认）
# --------------------------------------------------------------------------- #

def _read_lines(path):
    """逐行读文本（.gz 自动解压）。文件不存在返回 None。"""
    if not have(path):
        return None
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def load_bfile(prefix):
    """读取 `.bed/.bim/.fam` 三件套；不完整则返回 (None, 缺什么)。"""
    need = {ext: prefix + ext for ext in (".bed", ".bim", ".fam")}
    missing = [p for p in need.values() if not have(p)]
    if missing:
        return None, missing

    bim = _read_lines(prefix + ".bim") or []
    fam = _read_lines(prefix + ".fam") or []
    variants = []
    for i, ln in enumerate(bim, 1):
        cols = ln.rstrip("\n").split("\t")
        if len(cols) < 2:
            cols = ln.split()
        if len(cols) != 6:
            return None, [f"{prefix}.bim 第 {i} 行不是 6 列（{len(cols)} 列）"]
        variants.append(cols)
    samples = []
    for i, ln in enumerate(fam, 1):
        cols = ln.rstrip("\n").split("\t")
        if len(cols) < 2:
            cols = ln.split()
        if len(cols) != 6:
            return None, [f"{prefix}.fam 第 {i} 行不是 6 列（{len(cols)} 列）"]
        samples.append((cols[0], cols[1]))
    return {"form": "bfile", "prefix": prefix, "n_var": len(variants),
            "n_ind": len(samples), "variants": variants, "samples": samples}, []


def load_pedmap(prefix):
    """读取 `.ped/.map`；逐行校验 `ped` 的字段数 == 6 + 2×n_var（结构不变量）。"""
    need = {ext: prefix + ext for ext in (".ped", ".map")}
    missing = [p for p in need.values() if not have(p)]
    if missing:
        return None, missing

    maps = _read_lines(prefix + ".map") or []
    for i, ln in enumerate(maps, 1):
        if len(ln.split()) != 4:
            return None, [f"{prefix}.map 第 {i} 行不是 4 列"]
    n_var = len(maps)
    peds = _read_lines(prefix + ".ped") or []
    expect = 6 + 2 * n_var
    samples = []
    for i, ln in enumerate(peds, 1):
        cols = ln.split()
        if len(cols) != expect:
            return None, [f"{prefix}.ped 第 {i} 行有 {len(cols)} 个字段，"
                          f"按 {n_var} 个位点应为 {expect}（6+2×{n_var}）"]
        samples.append((cols[0], cols[1]))
    return {"form": "pedmap", "prefix": prefix, "n_var": n_var,
            "n_ind": len(peds), "samples": samples}, []


def load_dataset(prefix):
    """自动识别 bfile / pedmap。同时存在时以 bfile 为准（下游用 --bfile）。"""
    bf, miss_b = load_bfile(prefix)
    if bf is not None:
        return bf, []
    pm, miss_p = load_pedmap(prefix)
    if pm is not None:
        return pm, []
    return None, (miss_b or []) + (miss_p or [])


def bed_expected_size(n_var, n_ind):
    """PLINK 1 SNP-major `.bed` 的字节数：3 字节魔数 + 每个位点 ⌈n_ind/4⌉ 字节。

    这是**格式定义**推导出的等式，不是经验阈值；已在真实 GWAS 产物上标定
    （20000 SNP × 162 个体 → 820003 字节，实测吻合）。
    """
    return 3 + n_var * math.ceil(n_ind / 4)


BED_MAGIC = b"\x6c\x1b\x01"          # 0x6c 0x1b 0x01 = PLINK 1, SNP-major


def check_bed(rep, prefix, n_var, n_ind, label="bed"):
    """`.bed` 的魔数 + 字节数不变量（抓截断/错位，不需要 plink）。"""
    path = prefix + ".bed"
    if not have(path):
        rep.add(f"{label}.exists", "hard", False, f"文件不存在或为空: {path}")
        return
    with open(path, "rb") as f:
        magic = f.read(3)
    rep.add(f"{label}.magic", "hard", magic == BED_MAGIC,
            f"{os.path.basename(path)} 魔数={magic.hex()}（期望 6c1b01）"
            if magic != BED_MAGIC else "6c1b01（PLINK 1 SNP-major）")
    actual = os.path.getsize(path)
    expect = bed_expected_size(n_var, n_ind)
    rep.add(f"{label}.size", "hard", actual == expect,
            f"{actual} 字节 = 3 + {n_var} 位点 × ⌈{n_ind}/4⌉={math.ceil(n_ind / 4)}"
            f" → 期望 {expect}" if actual == expect else
            f"**字节数不符**：实际 {actual}，按 {n_var} 位点 × {n_ind} 个体应为 {expect}"
            f"（差 {actual - expect:+d} 字节）—— 抓截断/写了一半的 .bed")


def _read_keep_file(path):
    """读 `keep.txt`（FID IID；也接受只有 IID 的单列）。返回 (set, err)。"""
    if not have(path):
        return None, f"keep 文件不存在或为空：{path}"
    pairs = set()
    for ln in (_read_lines(path) or []):
        toks = ln.split()
        if len(toks) >= 2:
            pairs.add((toks[0], toks[1]))
        elif len(toks) == 1:
            pairs.add((toks[0], toks[0]))
    if not pairs:
        return None, f"keep 文件里没有有效样本：{path}"
    return pairs, None


def sample_key(samples):
    """样本集指纹（排序后的 FID\\tIID 的 sha256 前 16 位）——跨阶段对账用。"""
    h = hashlib.sha256()
    for fid, iid in sorted(samples):
        h.update(f"{fid}\t{iid}\n".encode())
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# VCF 侧：样本表头 + 位点数（带体积上限）
# --------------------------------------------------------------------------- #

def vcf_samples(path):
    """读 `#CHROM` 行取样本列表；读不到返回 None（不抛）。"""
    if not have(path):
        return None
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#CHROM"):
                    cols = line.rstrip("\n").split("\t")
                    return cols[9:] if len(cols) > 9 else []
                if not line.startswith("#"):
                    return None
    except Exception:                                        # noqa: BLE001
        return None
    return None


def vcf_variant_count(path, byte_cap=200 * 1024 * 1024):
    """数非注释行。超过 `byte_cap` 的 gz 返回 None —— **如实"未做该判定"**。

    ⚠️ 这里刻意不调 bcftools：GWAS 阶段的执行环境未必有它（plink/gemma 才是必需），
    为一个 warn 级判据引入硬依赖不值得。代价是大 VCF 上不做数量对账，报告里写明。
    """
    if not have(path):
        return None
    size = os.path.getsize(path)
    if path.endswith(".gz") and size > byte_cap:
        return None
    try:
        opener = gzip.open if path.endswith(".gz") else open
        n = 0
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.startswith("#"):
                    n += 1
        return n
    except Exception:                                        # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# 过滤参数
# --------------------------------------------------------------------------- #

def parse_filter(v):
    """把 run.params.json 的 `filter` 解析成 dict。

    支持两种书写形态（都来自既有文档）：
      · dict：`{"geno": 0.2, "maf": 0.01, "hwe": 1e-6}`
      · 字符串：`"genos=0.2 maf=0.01 hwe=1e-6"`（编排层 --filter 的原样）
      以及无键名的三数字形态：`"0.2 0.01 1e-6"`（按 geno/maf/hwe 顺序）
    返回 (dict, err)。畸形值返回 (None, 原因) —— 调用方判 hard，不静默降级。
    """
    if v is None or v == "":
        return {}, None
    if isinstance(v, dict):
        out = {}
        for k, val in v.items():
            k2 = {"genos": "geno", "missing": "geno"}.get(str(k).lower(), str(k).lower())
            if k2 not in ("geno", "maf", "hwe"):
                continue
            try:
                out[k2] = float(val)
            except (TypeError, ValueError):
                return None, f"filter.{k} 不是数字：{val!r}"
        return out, None
    if isinstance(v, str):
        toks = v.replace(",", " ").split()
        kv = {}
        for t in toks:
            if "=" in t:
                k, val = t.split("=", 1)
                k = {"genos": "geno", "missing": "geno"}.get(k.lower(), k.lower())
                if k in ("geno", "maf", "hwe"):
                    try:
                        kv[k] = float(val)
                    except ValueError:
                        return None, f"filter 里的 {k}={val!r} 不是数字"
        if kv:
            return kv, None
        if len(toks) == 3:
            try:
                return {"geno": float(toks[0]), "maf": float(toks[1]),
                        "hwe": float(toks[2])}, None
            except ValueError:
                return None, f"filter 三数字形态无法解析：{v!r}"
        return {}, None
    return None, f"filter 类型不支持：{type(v).__name__}"


# --------------------------------------------------------------------------- #
# 通用判据
# --------------------------------------------------------------------------- #

def check_dataset_core(rep, ds, label):
    """两种形态共用的核心判据：位点数/个体数 > 0、ID 不重复。"""
    rep.add(f"{label}.variants", "hard", ds["n_var"] > 0,
            f"{ds['n_var']} 个位点" if ds["n_var"] > 0 else "**0 个位点** —— 空数据集")
    rep.add(f"{label}.samples", "hard", ds["n_ind"] > 0,
            f"{ds['n_ind']} 个个体" if ds["n_ind"] > 0 else "**0 个个体** —— 空数据集")
    pairs = ds["samples"]
    dup_pairs = len(pairs) - len(set(pairs))
    rep.add(f"{label}.ids_unique", "hard", dup_pairs == 0,
            f"FID/IID 唯一（{len(pairs)} 个）" if dup_pairs == 0 else
            f"有 {dup_pairs} 个重复的 FID/IID —— 下游关联会按 ID 连接，重复会让结果串行")


def _vcf_id_forms():
    """VCF 样本名与 PLINK FID/IID 的常见对应写法。

    ⚠️ 这不是"随便多认几种"——一条真实教训：本项目自己的 GWAS 产物
    （`Pig_geno/gwas_test`）里 `.fam` 是 `FID=IID=et10`，而同目录的 VCF 表头是
    `et10_et10`（PLINK `--recode vcf` 把样本名写成 `FID_IID`）。只比 IID 会把
    **162 个样本全部判成"丢了"**，而数据本身完全正确 —— 那是"拦下好数据"，
    与"放过坏数据"同样是致命的。
    """
    return (
        ("iid", lambda fid, iid: iid),
        ("fid", lambda fid, iid: fid),
        ("fid_iid", lambda fid, iid: f"{fid}_{iid}"),
        ("iid_iid", lambda fid, iid: f"{iid}_{iid}"),
        ("fid:iid", lambda fid, iid: f"{fid}:{iid}"),
    )


def check_samples_match_vcf(rep, ds, vcf):
    """`.fam` 的样本集必须与输入 VCF 的样本集**逐一对上**（允许几种常见命名约定）。

    判定方式是"在某个约定下**集合完全相等**"，不是"看起来差不多"：只要有一个样本
    对不上就判失败，并列出缺/多的前几个。
    """
    vs = vcf_samples(vcf) if vcf else None
    if vs is None:
        rep.add("samples_vs_vcf", "warn", False,
                "未能读取输入 VCF 的样本表头（文件不存在 / 无 #CHROM 行）——"
                "**未做**样本集对账，本阶段不能声称样本没丢")
        return
    want = set(vs)
    forms = {name: {fn(fid, iid) for fid, iid in ds["samples"]}
             for name, fn in _vcf_id_forms()}
    matched = [name for name, s in forms.items() if s == want]
    if matched:
        rep.extras["sample_id_convention"] = matched[0]
        rep.add("samples_vs_vcf", "hard", True,
                f"{len(want)} 个样本逐一匹配（命名约定 `{matched[0]}`）")
        return

    # 没匹配上：用"最接近"的约定给出可诊断的差异，并写明试过哪些约定
    best = min(forms, key=lambda n: len(forms[n] ^ want))
    got = forms[best]
    missing = sorted(want - got)
    extra = sorted(got - want)
    detail = (f"VCF {len(want)} 个 / PLINK {len(got)} 个；按 `{best}` 对账仍有差异"
              f"（已试 {', '.join(forms)}）")
    if missing:
        detail += f"；**丢了 {len(missing)} 个**: {', '.join(missing[:5])}"
    if extra:
        detail += f"；多出 {len(extra)} 个: {', '.join(extra[:5])}"
    rep.add("samples_vs_vcf", "hard", False, detail)


# --------------------------------------------------------------------------- #
# Stage S0：VCF → PLINK
# --------------------------------------------------------------------------- #

@stage("00_format_convert")
def verify_format_convert(args, rep):
    """S0：产出的 PLINK 数据集结构完整、样本一个不丢、位点数不是 0。"""
    prefix = args.prefix
    ds, miss = load_dataset(prefix)
    if ds is None:
        rep.add("dataset.exists", "hard", False,
                "PLINK 数据集不完整：" + ("; ".join(miss) if miss else "未知原因"))
        return

    rep.extras.update({"output_form": ds["form"], "n_variants": ds["n_var"],
                       "n_samples": ds["n_ind"],
                       "sample_ids_sha": sample_key(ds["samples"])})
    check_dataset_core(rep, ds, ds["form"])
    if ds["form"] == "bfile":
        check_bed(rep, prefix, ds["n_var"], ds["n_ind"])
    else:
        rep.add("pedmap.shape", "warn", True,
                "只产出 ped/map（未产出 bfile）—— 后续阶段若用 `--bfile` 会找不到文件")

    check_samples_match_vcf(rep, ds, args.vcf)

    # 位点数与输入 VCF 的对账是 **warn**：S0 不做过滤，理论上应相等，但多等位拆分
    # 与 plink 的 biallelic-only 策略会让数量变化，且 vcftools/plink 两条路径行为不同。
    # 在没实测标定之前不写成 hard（本仓库的规矩：hard 必须有实测依据）。
    n_vcf = vcf_variant_count(args.vcf) if args.vcf else None
    if n_vcf is None:
        rep.add("variants_vs_vcf", "warn", False,
                "未能统计输入 VCF 的位点数（文件不存在或超过体积上限）——**未做该判定**")
    else:
        delta = ds["n_var"] - n_vcf
        rep.add("variants_vs_vcf", "warn", delta == 0,
                f"VCF {n_vcf} → PLINK {ds['n_var']}（{delta:+d}）"
                + ("" if delta == 0 else "；若不是多等位拆分/非双等位位点被丢，查转换日志"))

    _declared_floors(rep, args, ds)


# --------------------------------------------------------------------------- #
# Stage S0.5：QC 过滤
# --------------------------------------------------------------------------- #

def _read_qc_table(path, columns, tag):
    """读 PLINK 的 `.frq/.lmiss/.hwe`；返回行 dict 列表，失败返回 (None, 原因)。"""
    lines = _read_lines(path)
    if lines is None:
        return None, f"{tag} 文件不存在或为空：{path}"
    rows, header = [], None
    for ln in lines:
        toks = ln.split()
        if not toks:
            continue
        if header is None:
            header = toks
            continue
        if len(toks) != len(header):
            return None, f"{tag} 第 {len(rows) + 2} 行列数不符（表头 {len(header)}，本行 {len(toks)}）"
        rows.append(dict(zip(header, toks)))
    if header is None:
        return None, f"{tag} 没有表头：{path}"
    for c in columns:
        if c not in header:
            return None, f"{tag} 表头缺少 {c!r}（实际：{header}）"
    return rows, None


def check_post_filter_qc(rep, qc_prefix, n_var_out, flt):
    """过滤后的 **实测值** 必须与声明的过滤参数一致 —— 证明参数真的生效。

    ⚠️ 为什么必须读过滤后自己的 `.frq/.lmiss/.hwe`，而不是阶段日志里那句
    "N variants remaining"：日志是**自报**，参数没传进去时它照样会打印一个数字。
    这与 WGS 侧"判据读本体、不读派生数据"是同一条原则。
    """
    maf, geno, hwe = flt.get("maf"), flt.get("geno"), flt.get("hwe")

    # --- MAF ---
    rows, err = _read_qc_table(qc_prefix + ".frq", ["SNP", "MAF"], "post-filter .frq")
    if err:
        rep.add("qc.frq", "hard", False, err + "（阶段脚本必须在过滤后补跑 `plink --freq`）")
    else:
        rep.add("qc.frq.rows", "hard", len(rows) == n_var_out,
                f"{len(rows)} 行 == {n_var_out} 个位点" if len(rows) == n_var_out else
                f"**{len(rows)} 行 ≠ {n_var_out} 个位点** —— .frq 可能是过滤前的陈旧产物")
        vals = []
        bad = 0
        for r in rows:
            try:
                vals.append(float(r["MAF"]))
            except (KeyError, ValueError):
                bad += 1
        if bad:
            rep.add("qc.maf", "hard", False, f"{bad} 个位点的 MAF 无法解析（NA/空值）")
        elif maf is None:
            rep.add("qc.maf", "warn", False,
                    f"未声明 --maf 阈值，**未核对** MAF 下限（实测 min={min(vals):.4g}）"
                    if vals else "无位点，无法核对 MAF")
        elif not vals:
            rep.add("qc.maf", "hard", False, "过滤后 .frq 没有任何位点")
        else:
            lo = min(vals)
            rep.add("qc.maf", "hard", lo + 1e-9 >= maf,
                    f"实测 min(MAF)={lo:.6g} ≥ 阈值 {maf:g}（过滤真的生效）" if lo + 1e-9 >= maf
                    else f"**实测 min(MAF)={lo:.6g} < 阈值 {maf:g}** —— `-maf` 没有生效，"
                         f"或 .frq 是过滤前的陈旧产物")

    # --- 缺失率 ---
    rows, err = _read_qc_table(qc_prefix + ".lmiss", ["SNP", "F_MISS"],
                               "post-filter .lmiss")
    if err:
        rep.add("qc.lmiss", "hard", False, err + "（阶段脚本必须在过滤后补跑 `plink --missing`）")
    else:
        rep.add("qc.lmiss.rows", "hard", len(rows) == n_var_out,
                f"{len(rows)} 行" if len(rows) == n_var_out else
                f"**{len(rows)} 行 ≠ {n_var_out} 个位点** —— .lmiss 可能是陈旧产物")
        try:
            hi = max(float(r["F_MISS"]) for r in rows) if rows else 0.0
        except (KeyError, ValueError):
            hi = None
        if hi is None:
            rep.add("qc.geno", "hard", False, "F_MISS 无法解析")
        elif geno is None:
            rep.add("qc.geno", "warn", False,
                    f"未声明 --geno 阈值，**未核对**缺失率上限（实测 max={hi:.4g}）")
        else:
            rep.add("qc.geno", "hard", hi <= geno + 1e-9,
                    f"实测 max(F_MISS)={hi:.6g} ≤ 阈值 {geno:g}" if hi <= geno + 1e-9
                    else f"**实测 max(F_MISS)={hi:.6g} > 阈值 {geno:g}** —— `-geno` 没有生效")

    # --- HWE ---
    rows, err = _read_qc_table(qc_prefix + ".hwe", ["SNP", "TEST", "P"],
                               "post-filter .hwe")
    if err:
        rep.add("qc.hwe", "hard", False, err + "（阶段脚本必须在过滤后补跑 `plink --hardy`）")
    else:
        # PLINK 1.9 在有/无表型时 TEST 列可能写成 ALL / ALL(NP) / AFF 等；
        # 用 startswith("ALL") 兼容，且**只取 ALL**：AFF/UNAFF 行是同一批人的分层，
        # 混进来会让"最不显著的那个"变成另一组人的值。
        all_rows = [r for r in rows if str(r.get("TEST", "")).startswith("ALL")]
        if not all_rows:
            rep.add("qc.hwe", "hard", False,
                    f"post-filter .hwe 里没有 TEST=ALL 行（实际 TEST: "
                    f"{sorted({r.get('TEST') for r in rows})[:5]}）")
        elif hwe is None:
            lo = min(float(r["P"]) for r in all_rows)
            rep.add("qc.hwe", "warn", False,
                    f"未声明 --hwe 阈值，**未核对** HWE 下限（实测 min(P)={lo:.4g}）")
        else:
            try:
                lo = min(float(r["P"]) for r in all_rows)
            except (KeyError, ValueError):
                rep.add("qc.hwe", "hard", False, "HWE 的 P 列无法解析")
                return
            rep.add("qc.hwe", "hard", lo + 1e-12 >= hwe,
                    f"实测 min(P_HWE)={lo:.6g} ≥ 阈值 {hwe:g}" if lo + 1e-12 >= hwe
                    else f"**实测 min(P_HWE)={lo:.6g} < 阈值 {hwe:g}** —— `-hwe` 没有生效，"
                         f"或方向写反（hwe 是**保留 P 大**的位点）")


@stage("00.5_qc_filter")
def verify_qc_filter(args, rep):
    """S0.5：输出 bfile 结构完整、样本不丢、位点只减不增、过滤参数真的生效。"""
    out = args.prefix
    ds, miss = load_dataset(out)
    if ds is None:
        rep.add("dataset.exists", "hard", False,
                "输出 PLINK 数据集不完整：" + ("; ".join(miss) if miss else "未知原因"))
        return
    if ds["form"] != "bfile":
        rep.add("output_form", "hard", False,
                "S0.5 的契约产物是 **bfile**（`--make-bed`），实际只看到 ped/map —— "
                "下游 GEMMA/rMVP 都需要 `--bfile`")
    rep.extras.update({"n_variants": ds["n_var"], "n_samples": ds["n_ind"],
                       "sample_ids_sha": sample_key(ds["samples"]),
                       "output_form": ds["form"]})
    check_dataset_core(rep, ds, "out")
    if ds["form"] == "bfile":
        check_bed(rep, out, ds["n_var"], ds["n_ind"], label="out.bed")

    # --- 与输入比：样本守恒 + 位点只减不增 ---
    in_ds, in_miss = load_dataset(args.input_bfile) if args.input_bfile else (None, [])
    if in_ds is None:
        rep.add("input.exists", "hard", False,
                "读不到 S0.5 的输入数据集（--input-bfile / run.params.json 的 "
                "input_bfile）：" + ("; ".join(in_miss) if in_miss else "未提供"))
    else:
        rep.extras.update({"n_variants_in": in_ds["n_var"],
                           "n_samples_in": in_ds["n_ind"]})
        rep.add("variants_monotonic", "hard", ds["n_var"] <= in_ds["n_var"],
                f"位点 {in_ds['n_var']} → {ds['n_var']}（过滤只能减少）"
                if ds["n_var"] <= in_ds["n_var"] else
                f"**位点数变多了**：{in_ds['n_var']} → {ds['n_var']} —— 输出不是这份输入的过滤结果")
        out_set = set(ds["samples"])
        in_set = set(in_ds["samples"])
        keep_ids = None
        if args.keep_file:
            keep_ids, kerr = _read_keep_file(args.keep_file)
            if kerr:
                rep.add("keep_file", "hard", False, kerr)
            else:
                missing = sorted(keep_ids - out_set)
                extra = sorted(out_set - keep_ids)
                rep.add("out.expected_samples", "hard", not missing and not extra,
                        f"输出样本集 == keep.txt 的 {len(keep_ids)} 个样本"
                        if not missing and not extra else
                        f"**输出与 keep.txt 不一致**：少 {len(missing)} 个（前 3：{missing[:3]}），"
                        f"多 {len(extra)} 个（前 3：{extra[:3]}）—— S0.5 没按 S0.2 的分析集过滤")
                rep.add("samples_subset", "hard", out_set <= in_set,
                        f"输出样本全部来自输入（{len(out_set)}/{len(in_set)}）"
                        if out_set <= in_set else
                        f"**输出里出现了输入没有的样本**：{sorted(out_set - in_set)[:5]}")
        # 没有 keep 文件时：样本默认必须一个不丢；只有参数显式声明 keep/remove 才是预期内减少
        declared = bool(args.keep or args.remove)
        same = (ds["n_ind"] == in_ds["n_ind"] and out_set == in_set)
        level = "warn" if declared else "hard"
        if keep_ids is not None:
            # 有 keep 文件时，"样本减少"是 S0.2 的正常结果，由 out.expected_samples 判
            rep.add("samples_conserved", "warn", True,
                    f"按 keep.txt 取子集：输入 {in_ds['n_ind']} → 分析 {ds['n_ind']} 个样本")
        else:
            rep.add("samples_conserved", level, same or declared,
                    "样本集与输入完全一致" if same else
                    (f"样本数 {in_ds['n_ind']} → {ds['n_ind']}（参数显式声明了 keep/remove，"
                     f"属预期）" if declared else
                     f"**样本集变了**：{in_ds['n_ind']} → {ds['n_ind']}，且 run.params.json "
                     f"没有声明 keep/remove —— 静默丢样本是最难发现的一类错误"))
        if in_ds["n_var"] > 0:
            keep_rate = ds["n_var"] / in_ds["n_var"]
            rep.extras["retention"] = round(keep_rate, 4)
            rep.add("retention", "warn", keep_rate >= 0.5,
                    f"位点保留率 {keep_rate:.1%}" if keep_rate >= 0.5 else
                    f"位点保留率只有 {keep_rate:.1%}（< 50%）—— 确认过滤阈值是否过严")

    # --- 过滤参数生效性（读过滤后自己的实测值）---
    flt, err = _effective_filter(args)
    if err:
        rep.add("filter_params", "hard", False, err)
        flt = {}
    qc_prefix = args.qc_prefix or (out + ".qc")
    check_post_filter_qc(rep, qc_prefix, ds["n_var"], flt)

    _declared_floors(rep, args, ds)


def _declared_floors(rep, args, ds):
    """`--min-snps` / `--min-samples`：声明了才 hard，未声明登记 warn（不静默通过）。"""
    for label, raw, actual in (("min_snps", args.min_snps, ds["n_var"]),
                               ("min_samples", args.min_samples, ds["n_ind"])):
        val, err = _parse_floor(raw, label)
        if err:
            rep.add(label, "hard", False, err + "—— 想开这条判据却没生效，不放行")
        elif val is None:
            rep.add(label, "warn", False,
                    f"未声明 {label}，**未做该判定**（实测 {actual}）—— "
                    f"本阶段不声称已达到预期规模")
        elif val == 0:
            rep.add(label, "warn", False,
                    f"{label}=0：显式声明**不做**该判定（实测 {actual}）")
        else:
            rep.add(label, "hard", actual >= val,
                    f"实测 {actual} ≥ 声明下限 {val}" if actual >= val else
                    f"**实测 {actual} < 声明下限 {val}**")


# --------------------------------------------------------------------------- #
# Phase 2/3：S1–S5 的共用读取与判据
# --------------------------------------------------------------------------- #

def as_pvalue(raw):
    """把 P 值字符串解析成 [0,1] 的 float；不可用（NA/nan/空/越界）返回 None。

    ⚠️ 不把 `0` 当无效：极端效应下 P 会下溢成 0.0（rMVP 实测最小 5.7e-36），
    把它判无效会让**最强信号**反而被算成"缺值"。是否"全 0"由调用方单独判。
    """
    if raw is None:
        return None
    s = str(raw).strip().strip('"')
    if s.lower() in ("", "na", "nan", "null", "none", "."):
        return None
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if 0.0 <= v <= 1.0 else None


def read_assoc(path):
    """读 GEMMA 的 `.assoc.txt`：返回 (header, rows)。

    兼容头部 `##` 元信息行与 `#chr...` 列名行；没有列名行时以第一行数据为列名
    （实测存在这种产物：golden 样本的 assoc 就没有 `##` 段）。
    """
    lines = _read_lines(path) or []
    header, rows = None, []
    for ln in lines:
        if not ln.strip():
            continue
        if ln.startswith("##"):
            continue
        if ln.startswith("#"):
            toks = ln.lstrip("#").strip().split()
            if "p_wald" in toks or "rs" in toks:
                header = toks
            continue
        toks = ln.rstrip().split("\t")
        if len(toks) == 1:
            toks = ln.split()
        if header is None:
            header = toks
            continue
        if len(toks) != len(header):
            continue
        rows.append(dict(zip(header, toks)))
    return header, rows


def read_csv_rows(path):
    """读 rMVP 的 `{trait}.{method}.csv`（带表头）。"""
    import csv as _csv
    lines = _read_lines(path)
    if lines is None:
        return None, None
    rd = list(_csv.reader(lines))
    if not rd:
        return None, None
    header = [c.strip().strip('"') for c in rd[0]]
    rows = [[c.strip().strip('"') for c in r] for r in rd[1:] if any(c.strip() for c in r)]
    return header, rows


def find_p_column(header, trait, method):
    """定位 P 值列：优先 `P.value`，其次 `<trait>.<method>`（rMVP 实测列名）。

    返回 (idx, 说明) 或 (None, 原因)。rMVP 的 CSV 里 P 值列名是**表型名.方法名**
    （实测 `IMF.MLM`），不是 `P.value` —— 只认 `P.value` 会把真实产物判成"缺 P 列"。
    """
    norm = [h.strip().strip('"') for h in header]
    if "P.value" in norm:
        return norm.index("P.value"), "P.value"
    want = f"{trait}.{method}"
    if want in norm:
        return norm.index(want), want
    # 退化：列名以 trait 开头且方法出现在名字里
    for i, h in enumerate(norm):
        if h.lower().startswith(str(trait).lower()) and method.lower() in h.lower():
            return i, h
    return None, f"表头里既没有 P.value 也没有 {want}（实际：{norm}）"


def image_kind(path):
    """按文件头判图片类型；不是已知图片格式返回 None。"""
    with open(path, "rb") as f:
        head = f.read(12)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"GIF8"):
        return "gif"
    return None


def read_eigenvec(path):
    """读 `.eigenvec`（gcta64 无表头，PLINK 有 `#FID IID PC1…` 表头）。

    返回 (ids, n_pc) 或 (None, 原因)。
    """
    lines = _read_lines(path)
    if lines is None:
        return None, f"文件不存在或为空：{path}"
    ids, n_pc = [], None
    for ln in lines:
        if not ln.strip():
            continue
        toks = ln.split()
        if any(t.upper() == "PC1" for t in toks) or toks[0].startswith("#"):
            continue
        if len(toks) < 3:
            return None, f"{os.path.basename(path)} 有一行少于 3 列：{ln[:60]}"
        ids.append(toks[1])
        n_pc = len(toks) - 2
    if not ids:
        return None, f"{os.path.basename(path)} 里没有任何个体"
    return (ids, n_pc), None


def read_eigenval(path):
    vals = []
    for ln in (_read_lines(path) or []):
        try:
            vals.append(float(ln.split()[0]))
        except (IndexError, ValueError):
            continue
    return vals


def parse_region(region):
    """解析 `chr:start-end` / `chr:start:end` → (chr, start, end)。失败返回 None。"""
    if not region:
        return None
    s = str(region).strip().replace(",", "")
    if ":" not in s:
        return None
    chrom, rest = s.split(":", 1)
    parts = rest.replace("-", ":").split(":")
    if len(parts) != 2:
        return None
    try:
        return chrom, int(parts[0]), int(parts[1])
    except ValueError:
        return None


def count_region_variants(prefix, region):
    """从 `.bim` 数区域内的位点数（不调 plink —— 纯文本，零依赖）。"""
    parsed = parse_region(region)
    if parsed is None:
        return None
    chrom, start, end = parsed
    lines = _read_lines(prefix + ".bim")
    if lines is None:
        return None
    n = 0
    for ln in lines:
        toks = ln.split()
        if len(toks) < 4:
            continue
        if str(toks[0]) != str(chrom):
            continue
        try:
            pos = int(toks[3])
        except ValueError:
            continue
        if start <= pos <= end:
            n += 1
    return n


def read_source_pvalues(path):
    """把上游关联结果读成 {SNP: P}（GEMMA assoc 或 rMVP CSV 都认）。

    返回 (pairs, err)。`pairs` 只含 P 值可解析的位点。
    """
    if not have(path):
        return None, f"源结果文件不存在或为空：{path}"
    name = os.path.basename(path).lower()
    if name.endswith(".csv"):
        header, rows = read_csv_rows(path)
        if header is None:
            return None, f"{path} 不是可解析的 CSV"
        # SNP 列：优先 SNP / rs
        idx_snp = next((i for i, h in enumerate(header)
                        if h.upper() in ("SNP", "RS", "ID")), 0)
        idx_p = len(header) - 1
        out = {}
        for r in rows:
            if len(r) <= max(idx_snp, idx_p):
                continue
            p = as_pvalue(r[idx_p])
            if p is not None:
                out[r[idx_snp]] = p
        return (out, None) if out else (None, f"{path} 里没有可解析的 P 值")
    header, rows = read_assoc(path)
    if not header or "p_wald" not in header:
        return None, f"{path} 的列名里没有 p_wald（实际：{header}）"
    idx_snp = header.index("rs") if "rs" in header else 0
    out = {}
    for r in rows:
        p = as_pvalue(r.get("p_wald"))
        if p is not None:
            out[r.get(header[idx_snp])] = p
    return (out, None) if out else (None, f"{path} 里没有可解析的 p_wald")


def _gzip_lines(path):
    """gzip 文本行；文件不存在/解压失败返回 (None, 原因)。"""
    if not have(path):
        return None, f"文件不存在或为空：{path}"
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines(), None
    except Exception as e:                                    # noqa: BLE001
        return None, f"gzip 解压失败：{path}: {e!r}"


def _check_images(rep, images_dir, label="plots", required=True):
    """`images_dir` 下至少一张图，且每张的**文件头**必须是真图片。

    `required=False` 时"没有图"只记 warn —— 给 S1 的回退路径用（没有 gcta64/R 时
    只出 PCA 数值，不产热图；数值产物仍按 hard 判）。
    

    只判"文件存在且非空"不够：R 崩溃时常常留下 0 字节文件，或把错误信息写进
    `.pdf`。实测真实产物里既有 `.pdf`（`%PDF`）也有 `.jpg`（`\\xff\\xd8\\xff`）。
    """
    if not images_dir or not os.path.isdir(images_dir):
        rep.add(f"{label}.dir", "warn" if not required else "hard", False,
                f"图片目录不存在：{images_dir!r}" +
                (" —— 本阶段的核心产物是图，必须有" if required else "（未核对图）"))
        return
    found = [os.path.join(images_dir, f) for f in sorted(os.listdir(images_dir))
             if f.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".gif"))]
    rep.add(f"{label}.count", "warn" if not required else "hard", len(found) >= 1,
            f"{len(found)} 张图" if found else
            ("**一张图都没有** —— 作图步骤没跑或没落盘" if required else "没有图（未核对图）"))
    if len(found) < 2:
        rep.add(f"{label}.manhattan_qq", "warn", False,
                f"只有 {len(found)} 张图：文档要求曼哈顿图 + QQ 图至少各一张")
    bad = [(os.path.basename(p), image_kind(p)) for p in found if image_kind(p) is None]
    rep.add(f"{label}.magic", "hard", not bad,
            f"{len(found)} 张图的文件头都合法" if not bad else
            f"这些文件的文件头不是图片（大概率是 0 字节或错误信息）：{bad[:5]}")
    tiny = [os.path.basename(p) for p in found if os.path.getsize(p) < 1024]
    if tiny:
        rep.add(f"{label}.size", "warn", False,
                f"这些图小于 1KB，可能是空图：{tiny[:5]}")


# --------------------------------------------------------------------------- #
# Stage S1：亲缘矩阵 + PCA
# --------------------------------------------------------------------------- #

@stage("01_kinship_pca")
def verify_kinship_pca(args, rep):
    """S1：GRM 形状正确、亲缘矩阵方阵对称、PCA 每个个体都在且有主成分。"""
    bf = args.bfile or args.input_bfile
    ds, miss = load_dataset(bf) if bf else (None, [])
    if ds is None:
        rep.add("bfile.exists", "hard", False,
                "读不到输入 bfile：" + ("; ".join(miss) if miss else "未提供"))
        return
    rep.extras.update({"n_samples": ds["n_ind"],
                       "sample_ids_sha": sample_key(ds["samples"])})
    check_dataset_core(rep, ds, "in")

    # --- GRM（GCTA 三件套的 .grm.id 与 .grm.gz）---
    grm = args.grm_prefix
    if not grm:
        rep.add("grm", "warn", False, "未提供 --grm-prefix，**未核对** GRM")
    else:
        ids = _read_lines(grm + ".grm.id")
        if ids is None:
            # 回退路径（无 gcta64 → plink --pca）本来就不产 GRM：如实登记"未核对"，
            # 不硬拦（那样回退路径永远过不了门禁），也不假装核过。
            rep.add("grm.id", "warn", False,
                    f"找不到 {grm}.grm.id —— GRM 未产出（gcta64 回退路径下属预期），"
                    f"**本阶段未核对 GRM**")
        else:
            rep.add("grm.id.rows", "hard", len(ids) == ds["n_ind"],
                    f"{len(ids)} 行 == {ds['n_ind']} 个个体" if len(ids) == ds["n_ind"]
                    else f"**{len(ids)} 行 ≠ {ds['n_ind']} 个个体** —— GRM 与输入不是同一批人")
        if ids is None:
            # GRM 整体未产出（回退路径）→ 不再要求 .grm.gz；否则会"承诺未核对、
            # 却仍把缺文件判 hard"，回退路径永远过不了门禁。
            pass
        else:
            lines, err = _gzip_lines(grm + ".grm.gz")
            if err:
                rep.add("grm.gz", "hard", False, err)
            else:
                bad = next((i for i, ln in enumerate(lines, 1)
                            if len(ln.split()) != 4), None)
                rep.add("grm.gz.shape", "hard", bad is None,
                        f"{len(lines)} 行，都是 4 列（V1 V2 值）" if bad is None else
                        f"第 {bad} 行不是 4 列 —— .grm.gz 损坏或不是 GCTA 产物")

    # --- 对称亲缘矩阵（GCTA 输出只有下三角，R 补全后才可判）---
    kt = args.kinship
    if not kt:
        rep.add("kinship", "warn", False,
                "未提供 --kinship（对称矩阵文件），**未核对**亲缘矩阵")
    elif not have(kt):
        rep.add("kinship", "warn", False,
                f"声明了亲缘矩阵但文件不存在：{kt} —— 回退路径下属预期，**未核对**对称性")
    else:
        lines = [ln for ln in (_read_lines(kt) or []) if ln.strip()]
        head = lines[0].split("\t") if "\t" in lines[0] else lines[0].split()
        n = len(head)
        body = lines[1:]
        rep.add("kinship.square", "hard", n == len(body) == ds["n_ind"],
                f"{n}×{len(body)}，输入 {ds['n_ind']} 个个体"
                if n == len(body) == ds["n_ind"] else
                f"**不是 {ds['n_ind']}×{ds['n_ind']} 方阵**（列名 {n}、数据行 {len(body)}）")
        rep.add("kinship.ids", "hard", set(head) == {i for _, i in ds["samples"]},
                "行名集合 == 输入样本集" if set(head) == {i for _, i in ds["samples"]} else
                "**行名集合与输入样本集不一致** —— 个体对不上号")
        # 对称性抽查（全查对 162×162 也就 2.6 万次比较，但大群体用抽样）
        try:
            mat = [[float(x) for x in ln.split("\t")] if "\t" in ln else
                   [float(x) for x in ln.split()] for ln in body]
            pairs = min(n, 200)
            asym = max((abs(mat[i][j] - mat[j][i]) for i in range(pairs)
                        for j in range(pairs)), default=0.0)
            rep.add("kinship.symmetric", "hard", asym <= 1e-6,
                    f"抽样对称性最大偏差 {asym:.2g}" if asym <= 1e-6 else
                    f"**矩阵不对称**（最大偏差 {asym:.4g}）—— 下三角补全那一步可能漏了")
            diag = [mat[i][i] for i in range(n)]
            bad_diag = [d for d in diag if not (0.5 <= d <= 2.0)]
            rep.add("kinship.diag", "warn", not bad_diag,
                    f"对角 {min(diag):.3f}~{max(diag):.3f}（GRM 应接近 1）" if not bad_diag
                    else f"{len(bad_diag)} 个个体的对角值不在 0.5~2.0：{bad_diag[:5]}")
        except (ValueError, IndexError):
            rep.add("kinship.matrix", "hard", False, "亲缘矩阵里有无法解析的数值")

    # --- PCA ---
    pca = args.pca_prefix
    if not pca:
        rep.add("pca", "hard", False,
                "未提供 --pca-prefix —— PCA 是本阶段的**核心产物**（后续协变量），必须有")
    else:
        ev, err = read_eigenvec(pca + ".eigenvec")
        if err:
            rep.add("pca.eigenvec", "hard", False, err)
        else:
            ids, n_pc = ev
            rep.add("pca.rows", "hard", len(ids) == ds["n_ind"],
                    f"{len(ids)} 个体，{n_pc} 个主成分"
                    if len(ids) == ds["n_ind"] else
                    f"**{len(ids)} 行 ≠ {ds['n_ind']} 个个体** —— PCA 掉了个体")
            rep.add("pca.ids", "hard", set(ids) == {i for _, i in ds["samples"]},
                    "个体 ID 集合 == 输入样本集"
                    if set(ids) == {i for _, i in ds["samples"]} else
                    "**个体 ID 集合不一致** —— 协变量会按行序错配到别人身上")
        vals = read_eigenval(pca + ".eigenval")
        if not vals:
            rep.add("pca.eigenval", "hard", False, f"找不到或读不出 {pca}.eigenval")
        else:
            mono = all(vals[i] + 1e-9 >= vals[i + 1] for i in range(len(vals) - 1))
            rep.add("pca.eigenval.order", "hard", mono,
                    f"{len(vals)} 个特征值，降序" if mono else
                    "**特征值不是降序** —— 主成分顺序错了，协变量会取错")
            rep.add("pca.eigenval.positive", "hard", vals[0] > 0,
                    f"PC1 特征值 {vals[0]:.4g}" if vals[0] > 0 else
                    "**PC1 特征值 ≤ 0** —— PCA 没真正算出来")

    _check_images(rep, args.images_dir or os.path.join(args.run_dir, "out"),
                  "plots", required=False)


# --------------------------------------------------------------------------- #
# Stage S2：GEMMA
# --------------------------------------------------------------------------- #

def _assoc_path(args):
    if args.assoc:
        return args.assoc
    if args.prefix:
        return os.path.join(args.run_dir, "output", args.prefix + "_GWAS.assoc.txt")
    return ""


@stage("02_gemma")
def verify_gemma(args, rep):
    """S2：assoc 有 P 列、模型真的拟合了（不是全 nan / beta 全 0）、行数与输入对得上。"""
    path = _assoc_path(args)
    if not path or not have(path):
        rep.add("assoc.exists", "hard", False,
                f"关联结果不存在或为空：{path!r}（--assoc / run.params.json 的 assoc）")
        return
    header, rows = read_assoc(path)
    if not header or "p_wald" not in header:
        rep.add("assoc.header", "hard", False,
                f"结果表没有 p_wald 列（实际列：{header}）—— 不是 GEMMA -lmm 的输出？")
        return
    rep.add("assoc.header", "hard", True, "列名含 p_wald")
    rep.add("assoc.rows", "hard", len(rows) > 0,
            f"{len(rows)} 个位点" if rows else "**0 个位点** —— 关联没有产出")
    if not rows:
        return

    # 1) 模型是否真的拟合：P 值不能整列 nan，beta/se 不能整列 0
    vals = [as_pvalue(r.get("p_wald")) for r in rows]
    valid = [v for v in vals if v is not None]
    frac = len(valid) / len(rows)
    rep.add("assoc.p_valid", "hard", frac >= 0.9,
            f"{len(valid)}/{len(rows)} 个 p_wald 可解析（{frac:.1%}）" if frac >= 0.9 else
            f"**只有 {len(valid)}/{len(rows)}（{frac:.1%}）个 p_wald 可解析** —— "
            f"大量 nan 说明模型根本没拟合出来（表型/协变量/亲缘矩阵对不上）")
    if 0.9 <= frac < 0.99:
        rep.add("assoc.p_valid_ratio", "warn", False,
                f"{1 - frac:.1%} 的位点 p_wald 是 nan（个别位点拟合失败，通常可接受）")

    def _col_floats(name):
        out = []
        for r in rows:
            try:
                out.append(float(r.get(name)))
            except (TypeError, ValueError):
                out.append(None)
        return out

    beta = _col_floats("beta")
    if all(v == 0 for v in beta if v is not None) and any(v is not None for v in beta):
        rep.add("assoc.beta", "hard", False,
                "**beta 整列为 0** —— 模型没有估计出效应（实测真实产物里就是这样："
                "beta=0、se=0、p_wald=nan，最后却被当成正常结果拿去作图）")
    else:
        rep.add("assoc.beta", "hard", True, "beta 不是整列 0")
    se = _col_floats("se")
    if all(v == 0 for v in se if v is not None) and any(v is not None for v in se):
        rep.add("assoc.se", "warn", False, "se 整列为 0（与 beta 全 0 同源，模型可能退化）")

    # 2) 与输入规模对账
    bf = args.bfile or args.input_bfile
    ds, _miss = load_dataset(bf) if bf else (None, [])
    if ds is not None:
        rep.extras.update({"n_variants_in": ds["n_var"], "n_samples": ds["n_ind"]})
        rep.add("assoc.vs_bfile", "hard", len(rows) <= ds["n_var"],
                f"结果 {len(rows)} ≤ 输入 {ds['n_var']} 个位点"
                if len(rows) <= ds["n_var"] else
                f"**结果位点数 {len(rows)} 多于输入 {ds['n_var']}** —— 结果不是这份输入的")
        if len(rows) < 0.5 * ds["n_var"]:
            rep.add("assoc.coverage", "warn", False,
                    f"只分析了 {len(rows)}/{ds['n_var']}（{len(rows)/ds['n_var']:.0%}）个位点 —— "
                    f"查 -maf/-miss 阈值是否过严")

    # 3) 亲缘矩阵（-k）形状
    kin = args.kinship
    if kin and have(kin):
        lines = [ln for ln in (_read_lines(kin) or []) if ln.strip()]
        n = len(lines[0].split()) if lines else 0
        ok = n == len(lines) and (ds is None or n == ds["n_ind"])
        rep.add("kinship.shape", "hard", ok,
                f"亲缘矩阵 {n}×{len(lines)}" if ok else
                f"**亲缘矩阵 {n}×{len(lines)}** —— 与个体数（{ds['n_ind'] if ds else '?'}）不符")

    # 4) GEMMA 的 `##` 元信息（有则对账；没有不扣分，实测有产物不带）
    meta_n = meta_snp = None
    for ln in (_read_lines(path) or []):
        if not ln.startswith("##"):
            continue
        low = ln.lower()
        try:
            if "number of analyzed individuals" in low:
                meta_n = int(ln.split("=")[-1])
            elif "number of analyzed snps" in low:
                meta_snp = int(ln.split("=")[-1])
        except ValueError:
            pass
    if meta_snp is not None:
        rep.add("meta.snps", "hard", meta_snp == len(rows),
                f"元信息声明 {meta_snp} 个位点 == 实际 {len(rows)} 行"
                if meta_snp == len(rows) else
                f"**元信息说分析了 {meta_snp} 个位点，实际只有 {len(rows)} 行** —— 结果被截断")
    if meta_n is not None and ds is not None:
        rep.add("meta.samples", "hard", meta_n <= ds["n_ind"],
                f"分析了 {meta_n}/{ds['n_ind']} 个个体"
                if meta_n <= ds["n_ind"] else
                f"**分析了 {meta_n} 个个体，多于输入的 {ds['n_ind']}** —— 结果不是这份输入的")


# --------------------------------------------------------------------------- #
# Stage S3：rMVP
# --------------------------------------------------------------------------- #

@stage("03_mvp")
def verify_mvp(args, rep):
    """S3：每个 trait×method 都有结果、P 列对得上、三种方法的位点集合一致。"""
    trait = args.trait
    if not trait:
        rep.add("trait", "hard", False,
                "未提供 --trait / run.params.json 的 trait —— 无法定位 {trait}.{method}.csv")
        return
    methods = [m.strip() for m in str(args.methods or "GLM,MLM,FarmCPU").split(",") if m.strip()]
    csv_dir = args.csv_dir or "."
    snp_sets = {}
    for m in methods:
        path = os.path.join(csv_dir, f"{trait}.{m}.csv")
        label = f"{m}.csv"
        if not have(path):
            rep.add(f"{label}.exists", "hard", False, f"找不到 {path}")
            continue
        header, rows = read_csv_rows(path)
        if header is None:
            rep.add(f"{label}.parse", "hard", False, f"{path} 无法解析")
            continue
        rep.add(f"{label}.rows", "hard", len(rows) > 0,
                f"{len(rows)} 个位点" if rows else "**0 个位点**")
        idx, how = find_p_column(header, trait, m)
        if idx is None:
            rep.add(f"{label}.pcol", "hard", False, how)
            continue
        rep.add(f"{label}.pcol", "hard", True, f"P 值列 = 第 {idx + 1} 列（{how}）")
        if idx != len(header) - 1:
            rep.add(f"{label}.pcol.last", "warn", False,
                    f"P 值列不是最后一列（第 {idx + 1}/{len(header)} 列）—— "
                    f"下游按列名取用没问题，但按位置的旧脚本会取错")
        vals = [as_pvalue(r[idx]) for r in rows if len(r) > idx]
        valid = [v for v in vals if v is not None]
        frac = len(valid) / len(rows) if rows else 0
        rep.add(f"{label}.p_valid", "hard", frac >= 0.9,
                f"{len(valid)}/{len(rows)} 个 P 值可解析（{frac:.1%}）" if frac >= 0.9 else
                f"**只有 {len(valid)}/{len(rows)}（{frac:.1%}）个 P 值可解析** —— "
                f"模型没跑完或结果被写坏")
        if valid and len(set(valid)) == 1:
            rep.add(f"{label}.p_constant", "hard", False,
                    f"**P 值全等于 {valid[0]}** —— 关联没有实际进行")
        snp_sets[m] = {r[0] for r in rows if r}

    if len(snp_sets) >= 2:
        first = next(iter(snp_sets))
        same = all(snp_sets[k] == snp_sets[first] for k in snp_sets)
        rep.add("methods.snp_set", "hard", same,
                f"{len(snp_sets)} 种方法的位点集合一致（{len(snp_sets[first])} 个）" if same else
                "**不同方法的位点集合不一致** —— 同一个性状应当用同一份基因型，"
                "集合不同说明某一方法用了别的输入或结果写坏了")

    if args.map and have(args.map):
        mlines = [ln for ln in (_read_lines(args.map) or []) if ln.strip()]
        # rMVP 的 `.geno.map` 带表头（SNP CHROM POS ...）；PLINK `.bim` 不带。
        # 按"首列是不是数字"判表头，而不是按行数差 1 去猜。
        if mlines and not mlines[0].split()[0].isdigit():
            mlines = mlines[1:]
        n_map = len(mlines)
        for m, snps in snp_sets.items():
            if len(snps) > n_map:
                rep.add(f"{m}.vs_map", "hard", False,
                        f"{m} 有 {len(snps)} 个位点，多于 map 的 {n_map} 个")
            elif len(snps) < n_map:
                rep.add(f"{m}.vs_map", "warn", False,
                        f"{m} 有 {len(snps)}/{n_map} 个位点 —— 有缺失位点被跳过")


# --------------------------------------------------------------------------- #
# Stage S4：可视化 + 显著位点清单
# --------------------------------------------------------------------------- #

@stage("04_visualize")
def verify_visualization(args, rep):
    """S4：图是真图；显著位点清单必须能由上游结果**重算出来**。"""
    images_dir = args.images_dir or os.path.join(args.run_dir, "out")
    _check_images(rep, images_dir, "plots")

    # 显著位点清单 × 上游结果：这是本阶段最容易"看起来做完了"的地方
    sig_file, source = args.sig_file, args.source
    if not (sig_file and source):
        rep.add("sig.source", "warn", False,
                "未同时提供 --sig-file 与 --source，**未核对**显著位点清单是否与上游结果一致")
    elif not have(sig_file):
        rep.add("sig.exists", "hard", False, f"显著位点清单不存在或为空：{sig_file}")
    else:
        pairs, err = read_source_pvalues(source)
        if err:
            rep.add("sig.source", "hard", False, err)
        else:
            thresh = float(args.sig_thresh) if args.sig_thresh not in (None, "") else None
            if not thresh or thresh <= 0:
                thresh = 0.05 / max(len(pairs), 1)      # 文档约定：0.05/n
            expect = {snp for snp, p in pairs.items() if p < thresh}
            got = {ln.strip().strip('"') for ln in (_read_lines(sig_file) or []) if ln.strip()}
            rep.add("sig.threshold", "warn", True,
                    f"重算阈值 {thresh:.3g}（= 0.05/{len(pairs)}，或 --sig-thresh 显式给出）")
            rep.add("sig.matches", "hard", got == expect,
                    f"清单 {len(got)} 个 == 按阈值重算的 {len(expect)} 个" if got == expect else
                    f"**显著位点清单与上游结果对不上**：清单 {len(got)} 个、重算 {len(expect)} 个；"
                    f"多出 {len(got - expect)} 个（前 3：{sorted(got - expect)[:3]}），"
                    f"少了 {len(expect - got)} 个（前 3：{sorted(expect - got)[:3]}）")
            rep.extras.update({"n_sig": len(got), "sig_threshold": thresh})

    # 区域基因型（S4 的副产品）
    if args.region and args.bfile:
        n_reg = count_region_variants(args.bfile, args.region)
        rep.add("region.size", "hard", bool(n_reg),
                f"区域 {args.region} 内有 {n_reg} 个位点" if n_reg else
                f"**区域 {args.region} 内 0 个位点** —— 染色体编号或区间写错了？")
        if args.region_gt and n_reg:
            if not have(args.region_gt):
                rep.add("region.genotype", "hard", False,
                        f"声明了区域基因型文件但不存在：{args.region_gt}")
            else:
                n = len([ln for ln in (_read_lines(args.region_gt) or []) if ln.strip()])
                rep.add("region.genotype.rows", "hard", n == n_reg,
                        f"{n} 行 == 区域 {n_reg} 个位点" if n == n_reg else
                        f"**{n} 行 ≠ 区域 {n_reg} 个位点** —— 提取区间与结果不一致")


# --------------------------------------------------------------------------- #
# Stage S5：LD block
# --------------------------------------------------------------------------- #

@stage("05_ld_block")
def verify_ld_block(args, rep):
    """S5：LDBlockShow 真的出了图、位点表非空、区域不过小（工具会除零）。"""
    prefix = args.out_prefix
    if not prefix:
        rep.add("out_prefix", "hard", False, "未提供 --out-prefix")
        return

    # 区域位点数下限 10：LDBlockShow 在过小区间上会 division by zero（错误预防 #10）
    if args.region and args.bfile:
        n_reg = count_region_variants(args.bfile, args.region)
        rep.add("region.size", "hard", n_reg is None or n_reg >= 10,
                f"区域 {args.region} 内 {n_reg} 个位点" if n_reg is not None and n_reg >= 10
                else f"**区域 {args.region} 内只有 {n_reg} 个位点（<10）** —— "
                     f"LDBlockShow 会在过小区间上除零失败")

    svg = prefix + ".svg"
    if not have(svg):
        rep.add("svg.exists", "hard", False, f"找不到 {svg} —— LD 图没有产出")
    else:
        text = open(svg, encoding="utf-8", errors="replace").read()
        rep.add("svg.content", "hard", "<svg" in text,
                f"{os.path.getsize(svg)} 字节，含 <svg" if "<svg" in text else
                "**文件里没有 <svg** —— 不是有效的 SVG（可能是错误信息）")
        if os.path.getsize(svg) < 1024:
            rep.add("svg.size", "warn", False,
                    f"SVG 只有 {os.path.getsize(svg)} 字节，可能是空图")

    png = prefix + ".png"
    if have(png):
        rep.add("png.magic", "hard", image_kind(png) == "png",
                "PNG 文件头合法" if image_kind(png) == "png" else "**PNG 文件头不合法**")
    else:
        rep.add("png", "warn", False, "没有 .png（只要 .svg 可用，不算失败）")

    sites, err = _gzip_lines(prefix + ".site.gz")
    if err:
        rep.add("site.gz", "hard", False, err)
    else:
        rep.add("site.gz.rows", "hard", len(sites) > 0,
                f"{len(sites)} 个位点参与作 LD" if sites else "**位点表为空**")

    blocks, err = _gzip_lines(prefix + ".blocks.gz")
    if err:
        rep.add("blocks.gz", "hard", False, err)
    else:
        body = [ln for ln in blocks if ln.strip() and not ln.upper().lstrip().startswith("CHR")]
        rep.add("blocks.count", "warn", len(body) > 0,
                f"{len(body)} 个 LD block" if body else
                "0 个 LD block —— 可能真实（该区域无强连锁），但值得看一眼")

    if args.top_site and args.source:
        pairs, serr = read_source_pvalues(args.source)
        if serr:
            rep.add("top_site", "warn", False, serr)
        else:
            rep.add("top_site.found", "hard", str(args.top_site) in pairs,
                    f"峰顶位点 {args.top_site} 在上游结果中" if str(args.top_site) in pairs
                    else f"**峰顶位点 {args.top_site} 不在上游结果里** —— 区域/染色体对不上")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def _load_params(run_dir):
    path = os.path.join(run_dir, "run.params.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}


def _fill(args, params):
    """CLI 优先，其次 run.params.json；返回 args（原地补值）。"""
    for key in _PARAM_KEYS:
        if key in ("filter", "keep", "remove"):
            continue
        if not getattr(args, key, ""):
            setattr(args, key, params.get(key, ""))
    if not getattr(args, "filter", ""):
        args.filter = params.get("filter", "")
    if not getattr(args, "keep", ""):
        args.keep = params.get("keep", "")
    if not getattr(args, "remove", ""):
        args.remove = params.get("remove", "")
    return args


def _effective_filter(args):
    """合并过滤阈值：CLI 的 `--maf/--geno/--hwe` 优先于 run.params.json 的 `filter`。

    ⚠️ 早期实现把合并结果写回 `args.filter`，于是 `verify_qc_filter` 里再解析一次时
    只剩 params 那一份 —— **CLI 传的阈值被静默丢掉**（实测：`--maf 0.01` 传了却报
    "未声明 --maf 阈值，未核对"）。这类"参数传了没生效"正是本门禁要防的形态，
    不能让它出现在门禁自己身上。故合并逻辑只在这里做一次。
    """
    flt, err = parse_filter(getattr(args, "filter", ""))
    if err:
        return None, err
    out = dict(flt)
    for name in ("geno", "maf", "hwe"):
        raw = getattr(args, name, None)
        if raw in (None, ""):
            continue
        try:
            out[name] = float(raw)
        except (TypeError, ValueError):
            return None, f"--{name} 不是数字：{raw!r}"
    return out, None


def main():
    ap = argparse.ArgumentParser(description="GWAS 管线阶段结果校验器（写 .done 前的门禁）")
    ap.add_argument("--stage", required=True,
                    help="阶段名（= 脚本名去掉 .sh），如 00_format_convert / 00.5_qc_filter")
    ap.add_argument("--run-dir", required=True, help="RUN_DIR（含 .verify/）")
    ap.add_argument("--prefix", default="", help="本阶段输出前缀（S0/S0.5 的 .bed 等）")
    ap.add_argument("--vcf", default="", help="S0 的输入 VCF（用于样本/位点对账）")
    ap.add_argument("--input-bfile", default="",
                    help="S0.5 的输入 PLINK 前缀（S0 的产物）")
    ap.add_argument("--qc-prefix", default="",
                    help="过滤后 QC 报告前缀（默认 <prefix>.qc，产出 .frq/.lmiss/.hwe）")
    ap.add_argument("--geno", default=None, help="声明的 -geno 阈值（缺失率上限）")
    ap.add_argument("--maf", default=None, help="声明的 -maf 阈值（MAF 下限）")
    ap.add_argument("--hwe", default=None, help="声明的 -hwe 阈值（HWE P 下限）")
    ap.add_argument("--min-snps", default=None,
                    help="位点数下限（正整数判 hard；0 = 显式不做）")
    ap.add_argument("--min-samples", default=None,
                    help="样本数下限（正整数判 hard；0 = 显式不做）")
    # ── S1–S5（Phase 2/3）：路径/名字全部可省略，缺省回落到 run.params.json ──
    ap.add_argument("--bfile", default="", help="输入 PLINK 前缀（S1/S2/S4/S5 的上游 bfile）")
    ap.add_argument("--grm-prefix", default="", help="S1 的 GRM 前缀（.grm.id/.grm.gz）")
    ap.add_argument("--pca-prefix", default="", help="S1 的 PCA 前缀（.eigenvec/.eigenval）")
    ap.add_argument("--assoc", default="", help="S2 的 GEMMA 结果 assoc.txt")
    ap.add_argument("--kinship", default="", help="S2 的 GEMMA 亲缘矩阵 / S1 的对称矩阵")
    ap.add_argument("--pheno", default="", help="表型文件（保留给后续判据）")
    ap.add_argument("--trait", default="", help="S3/S4 的表型名（用于 {trait}.{method}.csv）")
    ap.add_argument("--methods", default="", help="S3 的方法列表，逗号分隔（默认 GLM,MLM,FarmCPU）")
    ap.add_argument("--csv-dir", default="", help="S3 的 CSV 目录（默认当前目录）")
    ap.add_argument("--map", default="", help="S3/S4 的位点表（rMVP 的 .geno.map / PLINK .bim 前缀）")
    ap.add_argument("--source", default="", help="S4/S5 的上游关联结果（assoc.txt 或 rMVP CSV）")
    ap.add_argument("--sig-file", default="", help="S4 的显著位点清单")
    ap.add_argument("--sig-thresh", default=None,
                    help="S4 的显著阈值；省略时按文档约定 0.05/n 重算")
    ap.add_argument("--images-dir", default="", help="S4/S1 的图片目录（默认 {RUN_DIR}/out）")
    ap.add_argument("--region", default="", help="区域，chr:start-end（S4/S5）")
    ap.add_argument("--region-gt", default="", help="S4 的区域基因型文件")
    ap.add_argument("--out-prefix", default="", help="S5 的输出前缀（.svg/.site.gz/.blocks.gz）")
    ap.add_argument("--top-site", default="", help="S5 的峰顶位点（须在上游结果中）")
    ap.add_argument("--keep-file", default="",
                    help="S0.5：S0.2 对齐产出的 keep.txt（FID IID）—— 有它时以它为分析集基准")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出到 stdout")
    ap.add_argument("--quiet", action="store_true", help="只在失败时输出")
    args = ap.parse_args()

    params = _load_params(args.run_dir)
    _fill(args, params)
    # ⚠️ 阈值合并（CLI > run.params.json 的 `filter`）只在 _effective_filter 里做一次。
    #    这里**不要**把 args.filter 改写成解析后的 dict —— 那会让 CLI 的
    #    --maf/--geno/--hwe 被静默丢掉（实测踩到过，见 _effective_filter 的说明）。

    rep = GwasReport(args.stage, args.run_dir)

    fn = _STAGES.get(args.stage)
    if fn is None:
        rep.add("stage_known", "hard", False,
                f"未知阶段 {args.stage!r}；已注册: {sorted(_STAGES)}")
    else:
        try:
            fn(args, rep)
        except Exception as e:                                # noqa: BLE001
            # 校验器自己出错 → 判失败而不是假装通过（与 WGS 门禁同一条 fail-closed）
            rep.add("verifier_internal", "hard", False,
                    f"校验器内部异常（该阶段结果**未经校验**，不放行）: {e!r}")

    # 报告落盘：它是编排层硬门禁的输入（存在 / stage 匹配 / pass=true / checks 非空），
    # 写不进去就必须判失败，否则下游会把"没有报告"当成"没跑过"，用户被迫重跑整个阶段。
    vdir = os.path.join(args.run_dir, ".verify")
    try:
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, f"{args.stage}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(rep.as_dict(), f, ensure_ascii=False, indent=2)
    except Exception as e:                                    # noqa: BLE001
        rep.add("report_persisted", "hard", False,
                f"校验报告无法写入 {vdir}: {e} —— 编排层硬门禁依赖它（检查写权限）")

    if args.json:
        print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=2))
    elif not (args.quiet and not rep.blocked):
        print(rep.render())

    return 1 if rep.blocked else 0


if __name__ == "__main__":
    sys.exit(main())
