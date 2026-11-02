# -*- coding: utf-8 -*-
"""`gwas-pheno-align` 的判据测试：分档匹配、差异落盘、强制审批。

设计约束（用户 2026-11-02 裁定）：
  1. 两边多出来的样本**都可以接受**（取交集），但对应关系不能乱 ——
     一对多 / 重复 / 归一化碰撞必须 hard。
  2. 重复个体默认拦下，显式 `--dup-policy` 才聚合。
  3. 匹配率过低不拦，但标记必须人工确认。
  4. 对齐之后**必须用户确认**（approve/check）才能进入下一步。

为什么这些测试值得写：对齐错一位，GWAS 结果就全错，而且**数量上看不出来**。
所以每个用例都同时断言"数字"和"内容"（keep 的顺序、GEMMA 单列文件的取值顺序）。
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "gwas-pheno-align", "scripts", "pheno_align.py")
PY = sys.executable


def run(*args, cwd=None):
    p = subprocess.run([PY, SCRIPT] + [str(a) for a in args],
                       cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def write_fam(path, pairs):
    with open(path, "w", encoding="utf-8") as f:
        for fid, iid in pairs:
            f.write(f"{fid} {iid} 0 0 0 -9\n")


def write_pheno(path, header, rows):
    with open(path, "w", encoding="utf-8") as f:
        f.write(",".join(header) + "\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")


def report(outdir):
    with open(os.path.join(outdir, "align.report.json"), encoding="utf-8") as f:
        return json.load(f)


def read_lines(path):
    with open(path, encoding="utf-8") as f:
        return [ln.rstrip("\n") for ln in f if ln.strip()]


GENO5 = [("F1", "et10"), ("F2", "et101"), ("F3", "et102"), ("F4", "et103"), ("F5", "et231")]


# --------------------------------------------------------------------------- #
# 基本匹配与输出格式
# --------------------------------------------------------------------------- #

def test_exact_match_keep_order_and_gemma_column(tmp_path):
    """★核心：交集按**基因型顺序**写；GEMMA 文件必须单列、顺序与 fam 一致。

    历史事故：把三列 PLINK 表型喂给 GEMMA `-p`（只吃单列值）→ 全 nan 还显示成功。
    这里直接断言内容：keep 的顺序 == fam 过滤后的顺序，gemma 文件第 i 行 == 第 i 个个体的值。
    """
    write_fam(tmp_path / "g.fam", GENO5)
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF", "sex"],
                [["et102", "1.81", "1"], ["et10", "2.32", "2"], ["et101", "3.22", "1"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 0, o

    assert read_lines(out / "keep.txt") == ["F1\tet10", "F2\tet101", "F3\tet102"]
    # fam 顺序是 et10/et101/et102/et103/et231 → 解析出的值必须是这个顺序
    assert read_lines(out / "pheno_gemma_IMF.txt") == ["2.32", "3.22", "1.81"]
    mvp = read_lines(out / "pheno_mvp.txt")
    assert mvp[0] == "Taxa\tIMF"
    assert mvp[1] == "et10\t2.32" and mvp[2] == "et101\t3.22"
    r = report(out)
    assert r["n_matched"] == 3 and r["n_unmatched_geno"] == 2
    assert r["status"] == "awaiting_approval" and r["approval_required"] is True


def test_extra_genotype_is_reported_but_not_blocked(tmp_path):
    """基因型多出来的样本可以接受：取交集、落盘、不阻断。"""
    write_fam(tmp_path / "g.fam", GENO5)
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"],
                [["et10", "2.3"], ["et101", "3.2"], ["et102", "1.8"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 0, o
    r = report(out)
    assert r["n_matched"] == 3 and r["n_unmatched_geno"] == 2
    missing = read_lines(out / "unmatched_geno.tsv")
    assert "F4\tet103\tno_phenotype_row" in missing
    assert "F5\tet231\tno_phenotype_row" in missing


def test_extra_phenotype_is_reported_with_suggestions(tmp_path):
    write_fam(tmp_path / "g.fam", GENO5)
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"],
                [["et10", "2.3"], ["et101", "3.2"], ["et102", "1.8"], ["et999", "4.0"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 0, o
    r = report(out)
    assert r["n_matched"] == 3 and r["n_unmatched_pheno"] == 1
    rows = read_lines(out / "unmatched_pheno.tsv")
    assert any("et999" in ln and "no_candidate" in ln for ln in rows)


def test_prefix_case_and_pad_rules(tmp_path):
    """`ET-010` / `et10` 在 digits-only + pad=3 下必须匹配。"""
    write_fam(tmp_path / "g.fam", GENO5)
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"],
                [["ET-010", "2.3"], ["ET-101", "3.2"], ["et102", "1.8"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF",
                "--normalize", "digits-only,pad=3", "--outdir", out)
    assert rc == 0, o
    r = report(out)
    assert r["n_matched"] == 3
    assert set(r["tiers"]) <= {"exact_iid", "norm_iid"}


def test_fullwidth_and_spaces_are_normalized(tmp_path):
    write_fam(tmp_path / "g.fam", [("F1", "et10"), ("F2", "et101")])
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"],
                [["ｅｔ１０ ", "2.3"], [" ET101", "3.2"]])   # 全角 + 空格
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 0, o
    assert report(out)["n_matched"] == 2


# --------------------------------------------------------------------------- #
# "对应关系混乱"必须 hard
# --------------------------------------------------------------------------- #

def test_duplicate_rows_block_by_default(tmp_path):
    write_fam(tmp_path / "g.fam", GENO5)
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"],
                [["et10", "2.3"], ["et10", "2.4"], ["et101", "3.2"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 1, o
    r = report(out)
    assert any(f.startswith("dup.") for f in r["hard_failures"]), r["hard_failures"]


def test_duplicate_rows_with_mean_policy(tmp_path):
    write_fam(tmp_path / "g.fam", [("F1", "et10"), ("F2", "et101")])
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"],
                [["et10", "2.0"], ["et10", "4.0"], ["et101", "3.0"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--dup-policy", "mean",
                "--outdir", out)
    assert rc == 0, o
    assert read_lines(out / "pheno_gemma_IMF.txt") == ["3", "3"]


def test_one_id_to_two_individuals_is_hard(tmp_path):
    """同一个 IID 出现在两个 FID 下：只给 IID 无法判断是哪个人 → 必须停。"""
    write_fam(tmp_path / "g.fam", [("F1", "et10"), ("F2", "et10"), ("F3", "et101")])
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"], [["et10", "2.3"], ["et101", "3.2"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 1, o
    r = report(out)
    assert r["n_ambiguous"] >= 1
    assert any("ambiguous" in ln for ln in read_lines(out / "unmatched_pheno.tsv"))


def test_normalization_collision_is_hard(tmp_path):
    """digits-only 把 A12 / B12 撞成同一个键 → 取交集会把两个人混成一个。"""
    write_fam(tmp_path / "g.fam", [("F1", "A12"), ("F2", "B12"), ("F3", "C13")])
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"],
                [["A12", "2.3"], ["B12", "3.2"], ["C13", "1.1"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF",
                "--normalize", "digits-only", "--outdir", out)
    assert rc == 1, o
    r = report(out)
    assert any(f.startswith("collision.") for f in r["hard_failures"]), r["hard_failures"]


# --------------------------------------------------------------------------- #
# 映射表
# --------------------------------------------------------------------------- #

def test_mapping_file_resolves_and_header_is_skipped(tmp_path):
    write_fam(tmp_path / "g.fam", GENO5)
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"],
                [["P001", "2.3"], ["P002", "3.2"]])
    m = tmp_path / "map.csv"
    m.write_text("source,target\nP001,et10\nP002,et101\n", encoding="utf-8")
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--map", m, "--outdir", out)
    assert rc == 0, o
    r = report(out)
    assert r["n_matched"] == 2 and r["map_used"] is True
    assert read_lines(out / "keep.txt") == ["F1\tet10", "F2\tet101"]
    assert read_lines(out / "mapping_used.tsv")[1].split("\t")[4].startswith("map")


def test_mapping_target_missing_is_reported(tmp_path):
    """映射表里指向一个不存在的基因型 ID：该行落进 unmatched（带原因），其余照常匹配。"""
    write_fam(tmp_path / "g.fam", GENO5)
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"], [["P001", "2.3"], ["P002", "3.2"]])
    m = tmp_path / "map.csv"
    m.write_text("P001,et10\nP002,et999\n", encoding="utf-8")
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--map", m, "--outdir", out)
    assert rc == 0, o
    r = report(out)
    assert r["n_matched"] == 1 and r["n_unmatched_pheno"] == 1
    rows = read_lines(out / "unmatched_pheno.tsv")
    assert any("map_target_unresolved" in ln for ln in rows), rows


def test_duplicate_mapping_target_is_hard(tmp_path):
    write_fam(tmp_path / "g.fam", GENO5)
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"], [["P1", "2.3"], ["P2", "3.2"]])
    m = tmp_path / "map.csv"
    m.write_text("P1,et10\nP2,et10\n", encoding="utf-8")   # 两个来源 → 同一目标
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--map", m, "--outdir", out)
    assert rc == 1, o
    assert "map.read" in report(out)["hard_failures"]


# --------------------------------------------------------------------------- #
# 匹配率 / 硬失败 / 值检查
# --------------------------------------------------------------------------- #

def test_low_match_rate_marks_needs_confirmation(tmp_path):
    write_fam(tmp_path / "g.fam", GENO5)
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"],
                [["et10", "2.3"], ["x1", "1"], ["x2", "2"], ["x3", "3"], ["x4", "4"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 0, o
    r = report(out)
    assert r["needs_confirmation"] is True and "match.low" in r["warnings"]


def test_zero_match_is_hard(tmp_path):
    write_fam(tmp_path / "g.fam", GENO5)
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"], [["zzz", "2.3"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 1, o
    assert "match.none" in report(out)["hard_failures"]


def test_constant_trait_is_hard(tmp_path):
    write_fam(tmp_path / "g.fam", [("F1", "a"), ("F2", "b"), ("F3", "c")])
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"],
                [["a", "2.0"], ["b", "2.0"], ["c", "2.0"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 1, o
    assert any("constant" in f for f in report(out)["hard_failures"])


def test_non_numeric_trait_is_hard(tmp_path):
    write_fam(tmp_path / "g.fam", [("F1", "a"), ("F2", "b"), ("F3", "c")])
    write_pheno(tmp_path / "p.csv", ["Taxa", "note"],
                [["a", "good"], ["b", "bad"], ["c", "ok"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "note", "--outdir", out)
    assert rc == 1, o
    assert any("trait[note].values" in f for f in report(out)["hard_failures"])


def test_covariate_missing_is_hard_unless_filled(tmp_path):
    write_fam(tmp_path / "g.fam", [("F1", "a"), ("F2", "b"), ("F3", "c")])
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF", "sex"],
                [["a", "1.0", "1"], ["b", "2.0", ""], ["c", "3.0", "2"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--covars", "sex", "--outdir", out)
    assert rc == 1, o
    assert any("covar[sex].missing" in f for f in report(out)["hard_failures"])
    out2 = tmp_path / "out2"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--covars", "sex",
                "--fill-covar", "--outdir", out2)
    assert rc == 0, o
    assert read_lines(out2 / "covar_mvp.txt")[0] == "Taxa\tsex"


def test_id_col_inference_and_unknown_column(tmp_path):
    write_fam(tmp_path / "g.fam", [("F1", "et10"), ("F2", "et101")])
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"], [["et10", "2.3"], ["et101", "3.2"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--traits", "IMF", "--outdir", out)
    assert rc == 0, o
    r = report(out)
    assert r["id_col_inferred"] is True and r["id_col"] == "Taxa"

    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "nope", "--traits", "IMF", "--outdir", tmp_path / "out2")
    assert rc == 1 and "pheno.id_col" in report(tmp_path / "out2")["hard_failures"]


# --------------------------------------------------------------------------- #
# 其它输入格式
# --------------------------------------------------------------------------- #

def test_xlsx_input(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Taxa", "IMF"])
    ws.append(["et10", 2.3])
    ws.append(["et101", 3.2])
    xlsx = tmp_path / "p.xlsx"
    wb.save(xlsx)
    write_fam(tmp_path / "g.fam", [("F1", "et10"), ("F2", "et101")])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", xlsx, "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 0, o
    assert read_lines(out / "keep.txt") == ["F1\tet10", "F2\tet101"]


def test_vcf_genotype_source(tmp_path):
    vcf = tmp_path / "g.vcf"
    vcf.write_text("##fileformat=VCFv4.2\n"
                   "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tet10\tet101\n",
                   encoding="utf-8")
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"], [["et10", "2.3"], ["et101", "3.2"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", vcf,
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 0, o
    assert report(out)["geno_source"]["kind"] == "vcf"
    assert read_lines(out / "keep.txt") == ["et10\tet10", "et101\tet101"]


# --------------------------------------------------------------------------- #
# 强制审批
# --------------------------------------------------------------------------- #

def _analyze_ok(tmp_path):
    write_fam(tmp_path / "g.fam", [("F1", "et10"), ("F2", "et101")])
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"], [["et10", "2.3"], ["et101", "3.2"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 0, o
    return out


def test_approval_is_required_and_hashes_guard_against_edits(tmp_path):
    """★强制审批：没 approve 时 check 失败；approve 后通过；产物被改后再次失败。"""
    out = _analyze_ok(tmp_path)
    rc, _ = run("check", "--outdir", out)
    assert rc == 1, "未审批却通过了 check —— 强制确认没有落地"
    rc, _ = run("approve", "--outdir", out, "--note", "用户确认")
    assert rc == 0
    rc, _ = run("check", "--outdir", out)
    assert rc == 0
    with open(out / "keep.txt", "a", encoding="utf-8") as f:
        f.write("X\tY\n")                     # 审批后篡改产物
    rc, o = run("check", "--outdir", out)
    assert rc == 1, "产物被改后审批竟然仍有效"


def test_approve_blocked_run_is_refused(tmp_path):
    write_fam(tmp_path / "g.fam", [("F1", "et10")])
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"], [["zzz", "2.3"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 1
    rc, o = run("approve", "--outdir", out, "--note", "x")
    assert rc == 1, "被 hard 拦下的对齐竟然可以审批"


# --------------------------------------------------------------------------- #
# 可移植性：不绑机器、不绑 cwd、不引入重依赖
# --------------------------------------------------------------------------- #

def test_no_pandas_dependency_and_no_hardcoded_paths():
    """轻量预处理不该背 pandas；脚本里也不许出现本机绝对路径。"""
    src = open(SCRIPT, encoding="utf-8").read()
    assert "import pandas" not in src, "重新引入了 pandas 依赖（读表已改纯标准库）"
    for bad in ("/home/", "/data1", "anaconda", "/tmp/"):
        assert bad not in src, f"脚本里出现了本机路径片段：{bad!r}"


def test_runs_from_any_working_directory(tmp_path):
    """脚本在工作目录之外也能跑（路径全靠参数传入，不依赖 cwd）。"""
    work = tmp_path / "elsewhere"
    work.mkdir()
    write_fam(tmp_path / "g.fam", [("F1", "et10"), ("F2", "et101")])
    write_pheno(tmp_path / "p.csv", ["Taxa", "IMF"], [["et10", "2.3"], ["et101", "3.2"]])
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", tmp_path / "p.csv", "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out, cwd=work)
    assert rc == 0, o
    assert read_lines(out / "keep.txt") == ["F1\tet10", "F2\tet101"]


def test_tsv_and_space_delimited_inputs(tmp_path):
    write_fam(tmp_path / "g.fam", [("F1", "et10"), ("F2", "et101")])
    tsv = tmp_path / "p.tsv"
    tsv.write_text("Taxa\tIMF\nET10\t2.3\net101\t3.2\n", encoding="utf-8")
    out = tmp_path / "out_tsv"
    rc, o = run("analyze", "--pheno", tsv, "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 0, o
    assert report(out)["n_matched"] == 2

    txt = tmp_path / "p.txt"
    txt.write_text("Taxa IMF\net10 2.3\net101 3.2\n", encoding="utf-8")
    out2 = tmp_path / "out_txt"
    rc, o = run("analyze", "--pheno", txt, "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out2)
    assert rc == 0, o
    assert report(out2)["n_matched"] == 2


def test_header_row_two_is_supported(tmp_path):
    write_fam(tmp_path / "g.fam", [("F1", "et10"), ("F2", "et101")])
    p = tmp_path / "p.csv"
    p.write_text("导出时间,2026-11-02\nTaxa,IMF\net10,2.3\net101,3.2\n", encoding="utf-8")
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", p, "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--header-row", "2",
                "--outdir", out)
    assert rc == 0, o
    assert read_lines(out / "keep.txt") == ["F1\tet10", "F2\tet101"]


def test_ragged_rows_are_rejected(tmp_path):
    """某行列数与表头不一致 → 直接报错（列错位会静默把性状对到错误的人）。"""
    write_fam(tmp_path / "g.fam", [("F1", "et10"), ("F2", "et101")])
    p = tmp_path / "p.csv"
    p.write_text("Taxa,IMF\n,et10,2.3\net101,3.2\n", encoding="utf-8")  # 首行多一列
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", p, "--geno", tmp_path / "g.fam",
                "--id-col", "Taxa", "--traits", "IMF", "--outdir", out)
    assert rc == 1, o
    assert "pheno.read" in report(out)["hard_failures"]


def test_gbk_encoded_csv_is_read(tmp_path):
    """中文用户导出的 CSV 常是 GBK：按 UTF-8 硬读会得到替换字符，ID 静默损坏。"""
    write_fam(tmp_path / "g.fam", [("F1", "et10"), ("F2", "et101")])
    p = tmp_path / "p.csv"
    p.write_bytes("个体号,IMF\net10,2.3\net101,3.2\n".encode("gb18030"))
    out = tmp_path / "out"
    rc, o = run("analyze", "--pheno", p, "--geno", tmp_path / "g.fam",
                "--id-col", "个体号", "--traits", "IMF", "--outdir", out)
    assert rc == 0, o
    assert report(out)["n_matched"] == 2
    assert read_lines(out / "keep.txt") == ["F1\tet10", "F2\tet101"]
