# -*- coding: utf-8 -*-
"""表型提取出口门禁（skills/phenotype-extract/scripts/validate.py）的判据测试。

用户诉求：表型提取的每一步结束之后要对结果值做校验，以免中途的错误传到后面步骤。

为什么单列这一组用例：表型提取的错误是**静默**的 —— 坏掉时产出的不是一个报错，
而是一个落在合法值域内、看起来完全正常的数值（典型：抠图失败 → IMF=0.0%）。
所以只测「好数据能通过」没有任何意义：一个永远返回 pass 的门禁也能过那种测试。
**每条硬性判据都必须配一条阴性对照**，证明坏输入真的被拦下。

跑法：
    /home/chenyuxi/anaconda3/envs/Agent/bin/python -m pytest tests/test_phenotype_validate.py -q
"""
import json
import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHENO_SCRIPTS = os.path.join(ROOT, "skills", "phenotype-extract", "scripts")
sys.path.insert(0, PHENO_SCRIPTS)

from validate import (  # noqa: E402
    GateReport, PhenotypeError, check_value, degenerate_witness, validate_batch,
    value_bounds,
)

PCOL = "IMF_预测_%"

# 「完全声明的插件」：值域 + 退化值见证列的豁免。
# ⚠️ 为什么需要一个常量：门禁对**未声明**的判据一律降级为 warn 并明说"未做该判定"
#    （`value_range` / `degenerate_witness` 同一处置 —— 让缺口在报告里看得见，而不是
#    发一张空头 PASS）。于是"干净批次无告警"的断言必须建在一个**声明完整**的 task 上：
#    用裸 `{"validate": {"value_range": [0.0, 100.0]}}` 时报告里会多出一条
#    "未做退化值判定"，那是**设计如此**，不是缺陷。
#    本表型（IMF）确有退化形态，真实的 IMF 插件声明的是见证列而非豁免 ——
#    这里用 null 只是因为本文件的 fixture 没有面积列；见证列本身的判据
#    由下面 F15 那一组用例覆盖。
DECLARED = {"validate": {"value_range": [0.0, 100.0], "degenerate_witness": None}}

# 与 IMF 插件**真实**声明一致（见证列 = 脂肪面积_px）—— F15 那一组用它。
DECLARED_FOR_IMF = {"validate": {"value_range": [0.0, 100.0],
                                 "degenerate_witness": {"column": "脂肪面积_px",
                                                        "value": 0}}}


def rows_of(values, *, status="ok", ref=None):
    """按主脚本的行结构造测试数据。

    ``values`` 里的 ``None`` 表示该样本**失败**：主脚本对失败样本写 ``NaN`` 值
    并把状态置为 ``fail:<原因>``（而非留着 ok）。造数据必须还原这一点，否则
    「失败率」这类判据在测试里根本触发不了 —— 空洞的阳性测试会掩盖门禁失效。
    """
    out = []
    for i, v in enumerate(values):
        failed = v is None
        r = {"Taxa": f"sample{i}",
             "状态": f"fail:{status}" if failed else status,
             PCOL: (float("nan") if failed else float(v))}
        if ref is not None:
            r["IMF_NIR_%"] = ref[i]
        out.append(r)
    return out


def normal(n=20, center=2.0):
    """一组有真实变异、落在值域内的正常数据（阳性样本）。"""
    return [center + 0.1 * (i % 5) - 0.2 for i in range(n)]


# ============================ 阳性：正常数据应通过 ============================
def test_normal_batch_passes():
    """正常批次：不被拦、无失败告警。

    ⚠️ 这里**必须带参照列**（`ref=`）。早期本用例不带参照，而参照列缺失时
    `nir_agreement` 的旧实现是**整条判据静默消失**（报告里连一行都没有），
    于是"无告警"这个断言其实依赖了一个缺陷。现在该判据会如实登记"未做该判定"
    （见 test_missing_reference_column_is_reported_not_silent），所以"干净批次"
    的语义必须包含"参照在场且一致"——这也是真实用法（本机 161 个样本有 NIR 真值）。
    特意**不给** min_nir_r：默认阈值下即便相关性别扭也只告警，不会把健康批次拦下。
    """
    n = 20
    vals = normal(n)
    ref = [v + 0.001 * ((-1) ** i) for i, v in enumerate(vals)]   # 与预测强相关
    rep = validate_batch(rows_of(vals, ref=ref), PCOL, DECLARED)
    assert not rep.blocked, rep.render()
    assert not rep.warnings, rep.render()


def test_batch_without_reference_passes_but_says_it_did_not_judge():
    """阳性对照的另一半：没给参照时**放行**（--nir-xls 本就可选），但报告里
    必须留着"未做该判定"这一行，不能连提都不提。"""
    rep = validate_batch(rows_of(normal()), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert not rep.blocked, rep.render()
    assert any(c.name == "nir_agreement" for c in rep.warnings), rep.render()


def test_value_bounds_read_from_task():
    task = {"validate": {"value_range": [0.0, 100.0]}}
    assert value_bounds(task) == (0.0, 100.0)
    assert value_bounds({}) is None
    assert value_bounds({"validate": {}}) is None
    # 允许插件直接写顶层 value_range（兼容简写）
    assert value_bounds({"value_range": [1, 5]}) == (1.0, 5.0)
    # 畸形声明不应炸，而是退化为「不判值域」
    assert value_bounds({"validate": {"value_range": "nonsense"}}) is None


def test_check_value_accepts_in_range():
    ok, why = check_value(2.5, (0.0, 100.0))
    assert ok and "2.5" in why


def test_check_value_skips_range_when_undeclared():
    ok, why = check_value(999.0, None)
    assert ok and "未声明" in why


def test_undeclared_value_range_is_warn_not_silent_pass():
    """插件未声明 value_range 时，必须**明说没判定**，而不是以 hard 级 PASS 出现。

    早期行为：仍登记一条 hard 级 "PASS n/n 个成功样本的值在合法范围"，只在尾注
    里带一句"建议补上"。读报告的人会以为值域已被把关 —— 而实际上什么都没断言，
    于是填 1000000（百分比表型不可能）也能过，且报告全绿。这是本项目反复出现的
    "纸面具备"（声称存在、从不生效）形态。
    """
    vals = [1000000.0 + i for i in range(10)]      # 百分比表型的物理不可能值
    rep = validate_batch(rows_of(vals), PCOL)      # 不给 task → 无值域声明
    names_warn = [c.name for c in rep.warnings]
    assert "value_range" in names_warn, "未声明值域应作为 warn 暴露，而不是静默 PASS"
    assert "value_range" not in [c.name for c in rep.checks if c.level == "hard"], (
        "未做任何判定的检查不应登记为 hard")


def test_duplicate_taxa_is_blocked():
    """同一张图/同一个体出现多行 → 必须拦（GWAS 要求一个体一个表型值）。

    rows 是按**图像**逐行生成的，所以同一个体拍两张就有两行。此时：
      · PLINK 导出走 dict（`{r["Taxa"]: v}`）→ 按键去重，重复个体只剩一条
      · GAPIT 导出走 DataFrame → 保留全部行
    两个文件行数/样本集不同，且没有任何机制发现它。更坏的是若两次测量值不同，
    dict 会静默地"最后一张图赢"。
    """
    vals = normal(n=10)
    rows = rows_of(vals)
    # 把 sample0 复制两份（同一个体三张图），值保持一致以免触发别的判据
    rows = rows + [dict(rows[0]), dict(rows[0])]
    rep = validate_batch(rows, PCOL, {"validate": {"value_range": [0.0, 100.0]}})
    assert rep.blocked, "重复个体未被拦截"
    assert "sample_uniqueness" in [c.name for c in rep.hard_failures]


def test_duplicate_taxa_with_conflicting_values_is_reported():
    """重复个体且多次测量值互相矛盾时，说明里要点出矛盾（最危险的一种）。"""
    rows = rows_of(normal(n=10))
    dup = dict(rows[0])
    dup[PCOL] = float(rows[0][PCOL]) + 5.0        # 同一个体、不同的值
    rep = validate_batch(rows + [dup], PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert rep.blocked, "重复个体未被拦截"
    detail = [c.detail for c in rep.hard_failures if c.name == "sample_uniqueness"][0]
    assert "矛盾" in detail, detail


def test_unique_taxa_not_blocked():
    """阳性对照：个体唯一时不拦（否则上面的用例可能被"永远拦"的实现蒙混过关）。"""
    rep = validate_batch(rows_of(normal(n=10)), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert not rep.blocked, rep.render()
    assert "sample_uniqueness" in [c.name for c in rep.checks
                                   if c.level == "hard" and c.passed]


# ============================ 阴性对照：坏数据必须被拦 ============================
def test_negative_out_of_range_is_blocked():
    """值越界必须拦 —— 阴性对照：若门禁永远返回 pass，这条会失败。"""
    vals = normal() + [150.0]          # 150% 的脂肪占比不可能存在
    rep = validate_batch(rows_of(vals), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert rep.blocked, "越界值未被拦截"
    assert "value_range" in [c.name for c in rep.hard_failures]


def test_negative_negative_value_is_blocked():
    """负值必须拦（面积占比不可能为负）。"""
    rep = validate_batch(rows_of(normal() + [-1.0]), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert rep.blocked


def test_negative_all_constant_is_blocked():
    """全部同值 = 模型没真跑。这是最常见也最隐蔽的失败，必须拦。"""
    rep = validate_batch(rows_of([2.0] * 20), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert rep.blocked, "常数输出未被拦截"
    assert "not_constant" in [c.name for c in rep.hard_failures]


def test_negative_all_zero_is_blocked():
    """全 0 是抠图/模型失败的典型伪装（0.0 完全落在 [0,100] 内）。"""
    rep = validate_batch(rows_of([0.0] * 20), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert rep.blocked, "全 0 未被拦截（这正是 max(meat_px,1) 那类兜底值的形态）"


def test_negative_high_fail_rate_is_blocked():
    """失败率超阈值必须拦。"""
    vals = normal(n=17) + [None] * 3   # 3/20 = 15% > 默认 5%
    rep = validate_batch(rows_of(vals), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert rep.blocked
    assert "fail_rate" in [c.name for c in rep.hard_failures]


def test_negative_too_few_samples_is_blocked():
    rep = validate_batch(rows_of([1.0, 2.0, 3.0]), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}},
                         min_samples=10)
    assert rep.blocked
    assert "sample_count" in [c.name for c in rep.hard_failures]


def test_negative_nan_value_claimed_ok_is_blocked():
    """状态声称 ok 却给出 NaN —— 必须被值域检查拦下（NaN 不是合法表型）。

    这是自相矛盾的行：主脚本不该产出它（见 phenotype_extract.py 里对非有限
    数值的拦截），但门禁仍要能识别，防的是将来有人绕过主脚本直接喂数据。
    """
    rows = rows_of(normal(n=19))
    rows.append({"Taxa": "sample_bad", "状态": "ok", PCOL: float("nan")})
    rep = validate_batch(rows, PCOL, {"validate": {"value_range": [0.0, 100.0]}})
    assert "value_range" in [c.name for c in rep.hard_failures]
    assert rep.blocked


# ============================ 分级：warn 不应拦截 ============================
def test_warn_level_does_not_block():
    """NIR 一致性差是告警而非拦截 —— 不应阻止产出。"""
    vals = normal(n=20)
    ref = [3.0] * 20                   # 参照为常数 → 相关性算不出 → warn
    rep = validate_batch(rows_of(vals, ref=ref), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert not rep.blocked, "warn 级检查不应拦截"
    assert rep.warnings, "应至少产生一条告警"


def test_nir_agreement_passes_when_correlated():
    """预测与参照强相关时不应告警。"""
    vals = [1.0 + 0.1 * i for i in range(20)]
    ref = [1.0 + 0.1 * i + 0.01 * ((-1) ** i) for i in range(20)]
    rep = validate_batch(rows_of(vals, ref=ref), PCOL, DECLARED, min_nir_r=0.6)
    assert not rep.blocked
    assert not rep.warnings, rep.render()


def test_nir_agreement_fails_when_anticorrelated():
    """反相关 = 预测与真值不符，**用户显式给了阈值就该拦下**（哪怕值域都合法）。

    分级取决于是否显式指定 --min-nir-r / min_nir_r：显式即视为用户声明的验收
    标准 → hard（拦截）；走默认值则只告警（相关性受数据本身影响，硬拦会变成
    "喊狼来了"）。本用例传了 min_nir_r=0.6，故期望拦截。
    """
    vals = [1.0 + 0.1 * i for i in range(20)]
    ref = [5.0 - 0.1 * i for i in range(20)]      # 反向
    rep = validate_batch(rows_of(vals, ref=ref), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}},
                         min_nir_r=0.6)
    assert rep.blocked, "显式阈值下反相关应被拦截（nir_agreement 为 hard）"
    assert any(c.name == "nir_agreement" for c in rep.hard_failures), rep.render()


def test_nir_agreement_default_threshold_only_warns():
    """未显式给阈值时，反相关只告警不拦截（避免对合法数据喊狼来了）。"""
    vals = [1.0 + 0.1 * i for i in range(20)]
    ref = [5.0 - 0.1 * i for i in range(20)]
    rep = validate_batch(rows_of(vals, ref=ref), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert not rep.blocked, "默认阈值不应拦截"
    assert any(c.name == "nir_agreement" for c in rep.warnings), rep.render()


# ============================ 判据本身的区分度 ============================
def test_gate_has_discrimination():
    """门禁本身要有区分度：好数据 pass、坏数据 block。

    单测「好数据 pass」是无效测试 —— 永远返回 pass 的实现也能过。
    这条同时验两侧，确保上面的阴性对照有意义。
    """
    good = validate_batch(rows_of(normal()), PCOL,
                          {"validate": {"value_range": [0.0, 100.0]}})
    bad = validate_batch(rows_of([0.0] * 20), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert not good.blocked and bad.blocked, \
        "门禁缺少区分度：好数据与坏数据得到相同结论"


def test_report_serializes_to_json():
    """报告要能序列化成 JSON 供编排层解析。"""
    import json
    rep = validate_batch(rows_of([0.0] * 20), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    d = json.loads(json.dumps(rep.as_dict()))
    assert d["blocked"] is True
    assert isinstance(d["checks"], list) and d["checks"]
    assert all({"name", "level", "passed", "detail"} <= set(c) for c in d["checks"])


# ============================ PhenotypeError 契约 ============================
def test_phenotype_error_is_single_class_across_modules():
    """插件抛的与主脚本捕获的必须是**同一个类**。

    若两处各自定义，except 子句对不上，失败会被静默漏掉 —— 正是本模块要防的那类错。
    """
    from phenotypes import _common as C
    assert PhenotypeError is C.PhenotypeError


def test_imf_compute_raises_on_empty_roi():
    """imf 抠图失败时必须抛 PhenotypeError，而不是返回兜底值 0.0。

    这是本次改动的核心：原先 `max(meat_px, 1)` 会让 ROI 全空时算出
    IMF=0.0% —— 一个落在合法值域内、事后无法从数值上识别的假值。
    """
    np = pytest.importorskip("numpy")
    from phenotypes.imf import compute_imf

    # 纯黑图 + 高阈值 → foreground_mask 必然抠不出前景 → meat_px == 0
    img = np.zeros((64, 64, 3), dtype=np.uint8)
    seg = np.zeros((64, 64), dtype=int)
    ctx = {"img": img, "seg": seg, "sample": "sample0",
           "thresh": 30, "mappings": {}}
    with pytest.raises(PhenotypeError):
        compute_imf(ctx)


def test_imf_declares_value_range():
    """IMF 插件必须声明 0~100% 的值域，否则门禁无据可依。"""
    np = pytest.importorskip("numpy")
    from phenotypes.imf import TASK
    assert value_bounds(TASK) == (0.0, 100.0)


# ============ A4：两个 GWAS 导出文件的样本集必须一致 ============
# 背景：PLINK 走 `dict(pred)` 去重 + `[iid for iid in fam_order if iid in pred]` 过滤，
# GAPIT 走 `df[["Taxa", pcol]].dropna()` 保留全部行。`--fam` 里缺某个样本时，
# 两个文件样本集不同（实测 PLINK 3 行 / GAPIT 4 行），却都作为权威产物写出。
# 下游用哪个取决于跑 PLINK/GEMMA 还是 GAPIT —— 两份互相矛盾的样本清单。
from phenotype_extract import build_gwas_exports, remove_stale_gwas_exports  # noqa: E402


def _rows(samples):
    """造 rows（与主脚本同结构）：samples 为 (Taxa, 值) 列表。"""
    return [{"Taxa": s, "状态": "ok", PCOL: float(v), "个体号": ""}
            for s, v in samples]


def _write_fam(tmp_path, iids, name="cohort.fam"):
    p = tmp_path / name
    p.write_text("".join(f"F1 {i} 0 0 0 -9\n" for i in iids))
    return str(p)


def test_gwas_exports_same_sample_set_when_fam_misses_a_sample(tmp_path):
    """★核心阴性对照：fam 少一个样本时两导出仍必须同集合。

    旧实现下这里会是 PLINK 少 s4、GAPIT 多 s4。
    """
    rows = _rows([("s1", 1.0), ("s2", 2.0), ("s3", 3.0), ("s4", 4.0)])
    fam = _write_fam(tmp_path, ["s1", "s2", "s3"])          # 少 s4
    ex = build_gwas_exports(rows, PCOL, fam)
    assert sorted(ex.order) == sorted(ex.pred), \
        f"两导出的样本集不一致：PLINK={sorted(ex.order)} GAPIT={sorted(ex.pred)}"
    assert set(ex.order) == {"s1", "s2", "s3", "s4"}, \
        f"有预测值的样本被静默丢掉：{set(ex.order)}"
    assert ex.not_in_fam == ["s4"], ex.not_in_fam


def test_gwas_exports_keep_samples_absent_from_fam(tmp_path):
    """fam 里没有的样本必须保留（它是排序基准，不是过滤器）。"""
    rows = _rows([("s9", 9.0), ("s8", 8.0)])
    fam = _write_fam(tmp_path, ["s1", "s2"])                # 完全无交集
    ex = build_gwas_exports(rows, PCOL, fam)
    assert sorted(ex.order) == ["s8", "s9"], f"样本被丢了：{ex.order}"
    assert ex.not_in_fam == ["s8", "s9"]
    assert sorted(ex.order) == sorted(ex.pred)


def test_gwas_exports_follow_fam_row_order(tmp_path):
    """fam 序与样本号序不同时，PLINK 侧要按 fam 的行序（而非字典序）。"""
    rows = _rows([("s1", 1.0), ("s2", 2.0), ("s3", 3.0)])
    fam = _write_fam(tmp_path, ["s3", "s1", "s2"])          # 故意与字面序不同
    ex = build_gwas_exports(rows, PCOL, fam)
    assert ex.order == ["s3", "s1", "s2"], f"没按 fam 行序：{ex.order}"
    assert not ex.not_in_fam


def test_gwas_exports_fam_absent_falls_back_to_sorted(tmp_path):
    """没传 --fam（或路径无效）时按样本号排，且仍与 GAPIT 同集合。"""
    rows = _rows([("s3", 3.0), ("s1", 1.0)])
    for fam in (None, str(tmp_path / "nope.fam")):
        ex = build_gwas_exports(rows, PCOL, fam)
        assert ex.order == ["s1", "s3"], ex.order
        assert sorted(ex.order) == sorted(ex.pred)
        assert not ex.not_in_fam


def test_gwas_exports_failed_and_nan_samples_excluded(tmp_path):
    """失败样本与 NaN 值不得进入任一导出（两处一致地排除）。"""
    rows = [{"Taxa": "s1", "状态": "ok", PCOL: 1.0},
            {"Taxa": "s2", "状态": "fail:缺图", PCOL: float("nan")},
            {"Taxa": "s3", "状态": "ok", PCOL: float("nan")}]
    ex = build_gwas_exports(rows, PCOL, None)
    assert ex.order == ["s1"], f"失败/NaN 样本混进了导出：{ex.order}"
    assert list(ex.pred) == ["s1"]


def test_gwas_exports_dedupes_repeated_taxa(tmp_path):
    """同一个体多张图：按键去重，两个导出都只有一条（与 sample_uniqueness 一致）。"""
    rows = [{"Taxa": "s1", "状态": "ok", PCOL: 1.0},
            {"Taxa": "s1", "状态": "ok", PCOL: 1.5},
            {"Taxa": "s2", "状态": "ok", PCOL: 2.0}]
    ex = build_gwas_exports(rows, PCOL, None)
    assert ex.order == ["s1", "s2"], ex.order
    assert len(ex.order) == len(set(ex.order)), "导出里有重复个体"


# ============ 第二轮审计 critical：PLINK 按 (FID, IID) 配对 ============
# 实测（真 plink，本机 plink v1.90b4.6）：fam 写 `farmA et3`、表型文件写
# `et3 et3 3.3757` 时，plink 报「0 phenotype values present after --pheno」+
# 「Note: No phenotypes present.」，**退出码 0 且不产出结果文件**（只有一个 .log）；
# 改成 `farmA et3 3.3757` 后同样退出码 0，但「3 phenotype values present」并产出
# .qassoc。两次退出码一模一样 —— 整个队列的表型被静默丢弃，没有任何一步会失败。
def _write_fam_fid(tmp_path, pairs, name="cohort.fam"):
    """pairs: [(FID, IID), ...] —— 显式控制 FID，区别于 _write_fam 的常量 F1。"""
    p = tmp_path / name
    p.write_text("".join(f"{fid} {iid} 0 0 0 -9\n" for fid, iid in pairs))
    return str(p)


def test_plink_export_uses_real_fid_not_iid(tmp_path):
    """★核心：PLINK 导出必须写 fam 里的**真实 FID**。

    旧实现写 `f"{iid} {iid} {v}"` —— FID 被硬编码成 IID。只要 fam 的 FID≠IID
    （多场次/多群体合并的 fam 是常态），每一行都匹配不上个体，而 plink 退出码
    仍是 0（不报错，只静默丢光）。
    """
    rows = _rows([("et1", 3.1), ("et2", 3.3), ("et3", 3.5)])
    fam = _write_fam_fid(tmp_path, [("farmA", "et1"), ("farmA", "et2"),
                                    ("farmB", "et3")])
    ex = build_gwas_exports(rows, PCOL, fam)
    assert ex.plink_rows == [("farmA", "et1"), ("farmA", "et2"), ("farmB", "et3")], \
        f"FID 没取自 fam：{ex.plink_rows}（旧实现是 ('et1','et1') 这种形态）"
    assert ex.fid_source == "fam", ex.fid_source


def test_plink_export_fid_assumption_is_declared_without_fam(tmp_path):
    """没有 --fam 时 FID 无从得知 → 退化为 FID=IID，但必须**标明这是假定**。

    关键不在退化本身（无 fam 时别无选择），而在调用方能不能知道自己在假定 ——
    否则产出看起来与一次正常导出毫无区别。
    """
    ex = build_gwas_exports(_rows([("et1", 3.1)]), PCOL, None)
    assert ex.plink_rows == [("et1", "et1")], ex.plink_rows
    assert ex.fid_source == "assumed-iid", (
        f"无 fam 时 fid_source={ex.fid_source} —— 调用方无从知道 FID 是假定的")


def test_duplicate_iid_across_different_fids_does_not_crash(tmp_path):
    """★fam 含重复 IID 不得崩（旧实现裸 AssertionError，且崩在门禁之后）。

    跨场次/跨群体合并 fam 时同一个体号出现在多个 FID 下是常态。旧不变量用
    `sorted(order) != sorted(pred)` 比较排序**列表**，重复项使长度不等 → 抛
    AssertionError。那时磁盘上两个产物都不存在，而 `*_verify.json` 已写完并写着
    blocked=false（断言在门禁判定之后才执行）—— 自相矛盾且无产物。
    """
    rows = _rows([("et1", 3.1), ("et2", 3.3)])
    fam = _write_fam_fid(tmp_path, [("farmA", "et1"), ("farmB", "et1"),
                                    ("farmA", "et2")])
    ex = build_gwas_exports(rows, PCOL, fam)          # 不得抛异常
    assert ex.plink_rows == [("farmA", "et1"), ("farmB", "et1"), ("farmA", "et2")], \
        f"同一个体号在多个 FID 下应各写一行：{ex.plink_rows}"
    assert ex.dup_fid_iids == ["et1"], (
        f"dup_fid_iids={ex.dup_fid_iids} —— 没把这个歧义暴露给调用方")


def test_duplicate_identical_fam_rows_are_deduped(tmp_path):
    """完全相同的 fam 行（FID 与 IID 都一样）是纯重复，不该写出两遍。"""
    fam = _write_fam_fid(tmp_path, [("farmA", "et1"), ("farmA", "et1")])
    ex = build_gwas_exports(_rows([("et1", 3.1)]), PCOL, fam)
    assert ex.plink_rows == [("farmA", "et1")], ex.plink_rows
    assert not ex.dup_fid_iids, ex.dup_fid_iids


def test_samples_outside_fam_still_get_plink_rows(tmp_path):
    """★fam 外的样本必须有 PLINK 行 —— 否则两个导出又分歧（A4 缺陷回归）。

    这条是**函数尾部的内部不变量在第一次运行时就抓到的真实漏写**：
    `not_in_fam` 的样本被放进 order（GAPIT 有它们）却没进 plink_rows，
    不变量当场抛错。它们的 FID 无从得知，只能退化为 FID=IID。
    """
    rows = _rows([("s1", 1.0), ("s9", 9.0)])
    fam = _write_fam_fid(tmp_path, [("farmA", "s1")])
    ex = build_gwas_exports(rows, PCOL, fam)
    assert ex.not_in_fam == ["s9"], ex.not_in_fam
    assert ex.plink_rows == [("farmA", "s1"), ("s9", "s9")], (
        f"fam 外样本没有 PLINK 行 → 两导出样本集分歧：{ex.plink_rows}")
    assert {iid for _, iid in ex.plink_rows} == set(ex.order), "导出样本集不一致"


# ============ 第二轮审计 critical：拦截后不得留上一轮的产物 ============
def test_stale_gwas_artifacts_are_removed_on_block(tmp_path):
    """门禁 hard 拦截时必须删掉上一轮留下的 GWAS 产物。

    早期实现只 `sys.exit(1)` 而不动磁盘：上一轮跑成功过的两个文件原地不动，
    下游（gwas-gemma 直接 `--pheno` 吃它们）照样读得到旧值、rc=0，而同目录的
    `*_verify.json` 写着 blocked=true、明细 CSV 是新的 —— rc=1 与磁盘内容互相
    矛盾，没有任何机制会发现。SKILL.md 说"不写出 GWAS 表型文件"，用户理解成
    "这次没产出，别用"，实现做的却是"这次不写新的"—— 对**已存在的旧文件**
    这两句话完全不同。
    """
    plink = tmp_path / "imf_phenotype_gwas_plink.txt"
    gapit = tmp_path / "imf_phenotype_gwas_gapit.csv"
    plink.write_text("et1 et1 99.0\n")
    gapit.write_text("Taxa,IMF\net1,99.0\n")
    removed, failed = remove_stale_gwas_exports([str(plink), str(gapit)])
    assert sorted(removed) == ["imf_phenotype_gwas_gapit.csv",
                               "imf_phenotype_gwas_plink.txt"], removed
    assert not failed, failed
    assert not plink.exists() and not gapit.exists(), "产物还在磁盘上"


def test_stale_cleanup_does_not_claim_absence_is_deletion(tmp_path):
    """阴性对照：本来就不存在的文件不该被报成"已删除"。

    否则报告会说"已清理上一轮产物"，而实际上是"这次没跑出过任何东西" ——
    两种情形对排查的意义完全不同。
    """
    removed, failed = remove_stale_gwas_exports([str(tmp_path / "never.txt")])
    assert removed == [] and failed == [], (removed, failed)


def test_stale_cleanup_reports_undeletable_file(tmp_path, monkeypatch):
    """删不掉时必须**报出来**（而非静默通过）—— 否则下游仍可能吃到旧值。"""
    p = tmp_path / "locked.txt"
    p.write_text("x")

    def boom(_):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(os, "remove", boom)
    removed, failed = remove_stale_gwas_exports([str(p)])
    assert removed == [] and len(failed) == 1, (removed, failed)
    assert failed[0][0] == "locked.txt", failed


# ============ 第二轮审计 major：nir_agreement 不得静默消失 ============
def test_missing_reference_column_is_reported_not_silent():
    """参照列缺失时 `nir_agreement` 必须**出现在报告里**并说明没判。

    早期写法 `if rc and n >= 3:` 让整段不执行：报告里既无 PASS 也无 WARN，
    该判据连一行都不出现，末尾照打"✅ 全部检查通过"、rc=0。读者无从知道有一条
    判定从未运行 —— "报告全绿"与"这条通过了"在他眼里是同一件事。
    """
    rep = validate_batch(rows_of(normal(n=10)), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    names = [c.name for c in rep.checks]
    assert "nir_agreement" in names, f"参照列缺失时判据从报告里消失：{names}"
    c = [c for c in rep.checks if c.name == "nir_agreement"][0]
    assert not c.passed and "没有参照真值列" in c.detail, c.detail


def test_explicit_min_nir_r_that_cannot_run_blocks():
    """★用户**显式**给了 --min-nir-r 却无法判定时，必须 hard 拦住。

    区分点：--nir-xls 按文档是可选的，缺参照本身不是错误（→ warn）；
    但用户显式声明了验收标准，那条标准**不能被静默跳过** —— 与 `min_variants`
    同一原则："想开却没生效必须被看见"。否则他会以为相关性已经把关过。
    """
    rep = validate_batch(rows_of(normal(n=10)), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}},
                         min_nir_r=0.6)
    assert rep.blocked, rep.render()
    assert any(c.name == "nir_agreement" for c in rep.hard_failures), rep.render()


def test_reference_column_present_but_all_empty_is_reported():
    """参照列在、值全空 —— 这正是"忘了 --nir-xls"的真实形态（插件写空列）。"""
    rows = rows_of(normal(n=10), ref=[None] * 10)
    rep = validate_batch(rows, PCOL, {"validate": {"value_range": [0.0, 100.0]}},
                         min_nir_r=0.6)
    names = [c.name for c in rep.checks]
    assert "nir_agreement" in names, f"判据静默消失：{names}"
    assert rep.blocked, "显式声明了阈值却因数据缺失跳过 —— 应拦住"


def test_implicit_nir_threshold_without_reference_only_warns():
    """阳性对照：没显式要求时，缺参照只告警不拦截（--nir-xls 本就可选）。"""
    rep = validate_batch(rows_of(normal(n=10)), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert not rep.blocked, "缺参照不该拦下健康批次（那会逼人把门禁关掉）"
    assert any(c.name == "nir_agreement" for c in rep.warnings), rep.render()


# ============ 第二轮审计 minor：fail_rate 在缺 `状态` 列时是**假 PASS** ============
def test_fail_rate_without_status_column_is_not_a_silent_pass():
    """★缺 `状态` 列时 `fail_rate` 不得发出"0% 失败"的空头 PASS。

    实测过的形态（第二轮审计）：早期实现是
        n_fail = int((df["状态"] != "ok").sum()) if total and "状态" in df.columns else 0
    —— 缺列时 n_fail 取 0、rate 取 0.0，判据于是以 **hard 级**
    `[PASS] fail_rate: 0/3 个样本失败（0.0%）` 出现在报告里。
    它**什么也没断言**，却读起来像"失败率已把关"。

    为什么不是边角：`{trait}_phenotype_gwas_gapit.csv` 正是 `Taxa,<trait>` 两列形态，
    且是下游真正消费的文件（`gwas-gemma/SKILL.md` / `gwas-pipeline/usage-guide.md`），
    而模块 docstring 明写这个 CLI 的用途之一就是"在 GWAS 入口处重新校验一份已存在的
    表型文件"。用户按文档复核 gapit 产物时走的正是这条分支。

    处置与同函数的 `value_range` 一致：前提不满足 → **明说未判**（warn），
    缺口在报告里看得见。断言刻意查 detial 里**说明"不是 0%"的那句** —— 只查
    "有没有这条判据"会被"登记成 PASS 也照样有一条"蒙混过关。
    """
    rows = [{"Taxa": f"s{i}", PCOL: v} for i, v in enumerate(normal(n=6))]
    rep = validate_batch(rows, PCOL, {"validate": {"value_range": [0.0, 100.0]}})
    c = [c for c in rep.checks if c.name == "fail_rate"]
    assert c, f"fail_rate 判据从报告里消失了：{[x.name for x in rep.checks]}"
    c = c[0]
    assert not c.passed, f"缺状态列却报 PASS（正是空头 PASS 的形态）：{c.detail}"
    assert c.level == "warn", f"未判定不该是 hard（会拦下形态不完整的导出）：{c.level}"
    assert "未做失败率判定" in c.detail, c.detail
    assert "0%" in c.detail or "0" in c.detail, (
        f"文案必须点明「这不是失败率为 0」——否则读者仍会读成 0%：{c.detail}")


def test_fail_rate_still_hard_blocks_when_status_column_present():
    """阳性对照：有 `状态` 列时 `fail_rate` 仍按 hard 正常判（别修坏了）。"""
    rep = validate_batch(rows_of(normal(n=17) + [None] * 3), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert rep.blocked, "明细形态下失败率超阈值未被拦下"
    assert "fail_rate" in [c.name for c in rep.hard_failures], rep.render()


def test_fail_rate_on_empty_table_is_reported_not_passed():
    """空表同理：没有样本行 ⇒ 未判，而不是"0/0 失败 = 通过"。"""
    rep = validate_batch([], PCOL, {"validate": {"value_range": [0.0, 100.0]}})
    c = [c for c in rep.checks if c.name == "fail_rate"]
    assert c and not c[0].passed and "空表" in c[0].detail, (
        f"空表下 fail_rate 的登记：{c}")


# ============ 第二轮审计 major：退化图像（过曝/空拍）不得伪装成 0.0 ============
def test_imf_rejects_frame_filled_foreground():
    """★整幅图都亮于阈值时必须抛错，而不是算出 IMF=0.0%。

    `meat_px <= 0` 只覆盖「ROI 恰好为空」一种退化。过曝/空拍/均匀底板的形态是
    **整幅图都亮于阈值** —— 前景 mask 的最大连通域就是整幅画面，min_component
    不触发，meat_px = 全图像素，模型对这类图输出全 0 类 → fat_px = 0 →
    ratio = 0.0，**落在 [0,100] 值域内**，与 max(meat_px,1) 兜底值形态完全一致。
    混在真实批次里时 not_constant（std>0）与 value_range（0.0 合法）都抓不到，
    fail_rate 也看不到它（状态=ok）—— 这一行会以 ok + 0.0 落盘并写进两个 GWAS 文件。
    """
    np = pytest.importorskip("numpy")
    from phenotypes.imf import compute_imf

    img = np.full((64, 64, 3), 240, dtype=np.uint8)    # 均匀亮场：整幅都亮于阈值
    seg = np.zeros((64, 64), dtype=int)                # 模型对这类图输出全 0 类
    ctx = {"img": img, "seg": seg, "sample": "sample0", "thresh": 30, "mappings": {}}
    with pytest.raises(PhenotypeError) as ei:
        compute_imf(ctx)
    assert "整幅画面" in str(ei.value), str(ei.value)


def test_imf_accepts_normal_frame_with_background():
    """阳性对照：真实形态（深底板 + 亮目标）必须照常算出值。

    否则上面那条可能被"永远抛错"的实现蒙混过关 —— 而那会拦下每一张健康照片。
    """
    np = pytest.importorskip("numpy")
    from phenotypes.imf import compute_imf

    img = np.full((64, 64, 3), 20, dtype=np.uint8)     # 深底板
    img[8:56, 8:56] = 200                              # 目标占 75%，四周有背景边
    seg = np.zeros((64, 64), dtype=int)
    seg[20:40, 20:40] = 1                              # 目标内部一片"脂肪"
    ctx = {"img": img, "seg": seg, "sample": "sample0", "thresh": 30, "mappings": {}}
    value, extra = compute_imf(ctx)
    assert 0.0 < value < 100.0, value
    # 不逐像素断言 48×48：foreground_mask 内含形态学开/闭运算，会磨掉四角。
    # 判据是"ROI 确实抠到了目标那块、且没有铺满画幅"。
    assert 0.9 * 48 * 48 <= extra["组织面积_px"] <= 48 * 48, extra
    assert extra["脂肪面积_px"] == 400, extra         # 目标内部 20×20 全是类 1
    assert 0.0 < extra["ROI覆盖率_%"] < 100.0, extra
    assert abs(value - 100.0 * 400 / extra["组织面积_px"]) < 1e-6, (
        f"value={value} 与 ROI/脂肪面积不自洽：{extra}")


# ============ F15 残余（第二轮审计）：ROI 不满幅的退化图 + 门禁不读 extra 列 ============
# 上面两条判据都建在 **ROI** 上（面积为 0 / 覆盖整幅），于是"ROI 抠得完全正常、
# 但分割结果全 0"这一形态整条落空。实测（真模型 + 真图库 162 张）：
#   · 把真图 et3 的目标区过曝吹白 → ROI 覆盖率 71.7441%，与健康 et3 **完全相同**，
#     落在真值批次 47.95%~81.86% 区间**之内** → 前两条判据都看不到它；
#     meat_px=229581（正常）、fat_px=0 → ratio=0.0（落在 [0,100] 内）、状态 ok；
#   · 门禁 6 条判据全 PASS、rc=0、warnings 为空，`0.0` 照写进两个 GWAS 文件。
# **任何覆盖率阈值都救不了**：退化图与健康图的覆盖率区间重叠，阈值要么放过它、
# 要么拦下健康图 —— 这是本质，不是标定没做好。
# 故判**分子为 0**：IMF% = 100·fat_px/meat_px，分母非 0 时分子为 0 ⟺ 结果恰为 0.0。
# 标定（真图 162 张，非模拟）：脂肪面积_px min=799、等于 0 的 **0 张**（零误伤）。
def test_imf_rejects_zero_fat_pixels_within_healthy_roi():
    """★ROI 抠取正常但分割结果全 0 ⇒ 抛错，不得产出 0.0%。

    与 `test_imf_rejects_frame_filled_foreground` 的区别：那张是"ROI 铺满画幅"，
    这张是"ROI 与健康图**一模一样**"。后者才是真正难抓的形态 —— 它证明判据必须
    落在**分子**上，落在 ROI 上的任何量（面积、覆盖率）都区分不开。
    """
    np = pytest.importorskip("numpy")
    from phenotypes.imf import compute_imf

    img = np.full((64, 64, 3), 20, dtype=np.uint8)     # 深底板
    img[8:56, 8:56] = 200                              # 目标占 75% —— 与阳性用例同一形状
    seg = np.zeros((64, 64), dtype=int)                # 但分割结果一个脂肪像素都没有
    ctx = {"img": img, "seg": seg, "sample": "sample0", "thresh": 30, "mappings": {}}
    with pytest.raises(PhenotypeError) as ei:
        compute_imf(ctx)
    msg = str(ei.value)
    assert "脂肪像素数为 0" in msg, msg
    # 报错必须带上"ROI 其实是正常的"这一事实 —— 否则读者会去查抠图参数（查错方向）。
    # ⚠️ 断言必须**真的在判覆盖率的数值**：写成 `A and B or "%" in msg` 时，
    # `or` 让整个条件恒真（`%` 到处都是）—— 一条永远通过的断言正是「纸面具备」。
    assert "ROI 抠取正常" in msg, msg
    assert "56.05%" in msg, f"没把 ROI 覆盖率报出来（读者无从判断抠图是否正常）：{msg}"


def test_imf_declares_degenerate_witness():
    """插件必须把"分子为 0"这条判据**声明**给门禁。

    理由：`compute` 里的 `raise` 只管得住**本次运行**。明细 CSV 一旦落盘就能被
    独立复核、被搬运、被下游消费 —— 那条路径上插件不参与（`validate.py` 作为
    独立 CLI 在 GWAS 入口校验一份**已存在**的 CSV 时正是如此），raise 管不到。
    实测：修复前，一份含 `脂肪面积_px=0`、值 0.0、状态 ok 的明细 CSV 混在 20 个
    健康样本里走 CLI 独立复核 → 6 条判据全 PASS、rc=0、零告警
    （`grep -c '脂肪面积_px' validate.py` = 0 —— 没有一条判据读 extra 列）。
    """
    np = pytest.importorskip("numpy")
    from phenotypes.imf import TASK

    assert degenerate_witness(TASK) == ("脂肪面积_px", 0.0), (
        f"IMF 未把退化见证列声明给门禁：{TASK.get('validate')}")


def _witness_rows(n_ok=20, *, fat=4000, value=None):
    """造明细行：带 `脂肪面积_px` 见证列（主脚本会把 extra 列一并写入 CSV）。"""
    rows = []
    for i in range(n_ok):
        rows.append({"Taxa": f"h{i}", "状态": "ok",
                     PCOL: (2.0 + 0.07 * i if value is None else value(i)),
                     "脂肪面积_px": (fat + 10 * i if callable(fat) else fat),
                     "组织面积_px": 200000, "ROI覆盖率_%": 60.0 + i})
    return rows


def test_gate_blocks_degenerate_witness_row():
    """★门禁必须拦下见证列为退化值的行 —— 这是"门外那条路径"唯一的守卫。

    一行 `脂肪面积_px=0` + 值 0.0 + 状态 ok 混在 20 个健康样本里：`value_range`
    说 0.0 合法、`not_constant` 说整批 std>0、`fail_rate` 说零失败 —— 在**结构上**
    都看不见它。只有去读那个见证列才拦得住。
    """
    rows = _witness_rows()
    rows.insert(0, {"Taxa": "et6", "状态": "ok", PCOL: 0.0,
                    "脂肪面积_px": 0, "组织面积_px": 229581,
                    "ROI覆盖率_%": 71.7441})       # 与健康 et3 的覆盖率完全相同
    rep = validate_batch(rows, PCOL, DECLARED_FOR_IMF)
    assert rep.blocked, rep.render()
    assert "degenerate_witness" in [c.name for c in rep.hard_failures], rep.render()
    detail = [c.detail for c in rep.hard_failures
              if c.name == "degenerate_witness"][0]
    assert "et6" in detail, f"拦下了但没点名是哪一行：{detail}"


def test_gate_degenerate_witness_passes_healthy_batch():
    """阴性对照：同一批数据去掉退化行必须放行。

    没有这条，上面那条可能被"凡值 0 就拦"的实现蒙混过关 —— 而 IMF 恰好可以合法
    取 0.0（真没脂肪），那会拦下真实数据。实测真图库 162 张里没有一张 fat_px=0。
    """
    rep = validate_batch(_witness_rows(), PCOL, DECLARED_FOR_IMF)
    assert not rep.blocked, rep.render()
    c = [c for c in rep.checks if c.name == "degenerate_witness"]
    assert c and c[0].passed and c[0].level == "hard", (
        f"健康批次应判 PASS：{[x.render() for x in c]}")


def test_gate_degenerate_witness_level_follows_declaration():
    """分级沿用 `value_range`/`nir_agreement` 的范式：**声明了才 hard**。

    未声明 → warn 且 `passed=False`（明说"未做该判定"）；显式 null → 一条通过的
    豁免。"忘带声明"与"确无此形态"必须在报告里可分辨 —— 这正是本项目反复出现的
    「纸面具备」形态：沉默被读成没问题。
    """
    rows = _witness_rows()
    rows.insert(0, {"Taxa": "et6", "状态": "ok", PCOL: 0.0, "脂肪面积_px": 0,
                    "组织面积_px": 229581, "ROI覆盖率_%": 71.7441})

    # (a) 插件未声明 → 明说未判（warn），而不是静默通过；也**不得**因此拦下
    rep = validate_batch(rows, PCOL, {"validate": {"value_range": [0.0, 100.0]}})
    c = [x for x in rep.checks if x.name == "degenerate_witness"]
    assert c and c[0].level == "warn" and not c[0].passed, (
        f"未声明时应登记为「未做该判定」：{[x.render() for x in c]}")
    assert "未做退化值判定" in c[0].detail, c[0].detail

    # (b) 显式 null 豁免 → 一条**通过**的登记（与"忘了声明"可分辨）
    rep = validate_batch(rows, PCOL,
                         {"validate": {"value_range": [0.0, 100.0],
                                       "degenerate_witness": None}})
    c = [x for x in rep.checks if x.name == "degenerate_witness"][0]
    assert c.passed and "豁免" in c.detail, c.render()

    # (c) 声明了但见证列不在 CSV（裁剪过的导出）→ 明说未判，不是"没有退化行"
    trim = [{k: v for k, v in r.items() if k != "脂肪面积_px"} for r in rows]
    rep = validate_batch(trim, PCOL, DECLARED_FOR_IMF)
    c = [x for x in rep.checks if x.name == "degenerate_witness"][0]
    assert not c.passed and "见证列" in c.detail, c.render()
    assert not rep.blocked, "列缺失应明说未判，而不是当作有退化行拦下"


def test_gate_degenerate_witness_declaration_malformed_is_reported():
    """声明不可解析（如 `"degenerate_witness": 123`）→ 明说未判。

    这种声明是「**想开却没生效**」—— 与"没开"一样必须在报告里看得见，
    不能当成已判。与 `value_bounds` 对畸形声明的容忍一致（返回 None，不炸）。
    """
    assert degenerate_witness({"validate": {"degenerate_witness": 123}}) is None
    assert degenerate_witness({"validate": {"degenerate_witness": {"value": 0}}}) is None
    assert degenerate_witness({"validate": {"degenerate_witness": ""}}) is None
    # 裸列名简写 → 退化触发值取 0（与 value_bounds 容忍顶层简写同一约定）
    assert degenerate_witness({"validate": {"degenerate_witness": "脂肪面积_px"}}) \
        == ("脂肪面积_px", 0.0)
    assert degenerate_witness({"degenerate_witness": "脂肪面积_px"}) \
        == ("脂肪面积_px", 0.0)

    rep = validate_batch(_witness_rows(), PCOL,
                         {"validate": {"degenerate_witness": 123}})
    c = [x for x in rep.checks if x.name == "degenerate_witness"][0]
    assert not c.passed and "无法解析" in c.detail, c.render()


def test_validate_cli_passes_witness_declaration():
    """独立复核路径：CLI 必须能把见证列声明透传给门禁。

    这是"同一个断点的两端"的第二端 —— 插件不参与，声明只能由命令行重述。
    ⚠️ 关键在**忘带参数**时的行为：不能退化成"确无此形态"（那会把"忘了传"
    静默升级成"检查过了"），而必须是"未做该判定"。
    """
    import subprocess
    import tempfile

    script = os.path.join(PHENO_SCRIPTS, "validate.py")
    with tempfile.TemporaryDirectory() as tmp:
        csv = os.path.join(tmp, "imf_phenotype.csv")
        import csv as _csv
        fields = ["Taxa", "状态", PCOL, "脂肪面积_px"]
        with open(csv, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            w.writerow(fields)
            w.writerow(["et6", "ok", 0.0, 0])              # 退化行
            for i in range(20):
                w.writerow([f"h{i}", "ok", round(2.0 + 0.07 * i, 4), 4000 + 10 * i])

        def run(*extra):
            return subprocess.run(
                [sys.executable, script, "--csv", csv, "--col", PCOL,
                 "--range", "0", "100", "--json", *extra],
                capture_output=True, text=True)

        # (a) 重述见证列 → 拦下（rc=1）
        r = run("--witness", "脂肪面积_px:0")
        assert r.returncode == 1, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"
        out = json.loads(r.stdout)
        assert "degenerate_witness" in out["hard_failures"], out["hard_failures"]

        # (b) **忘带** → 明说未判（warn），**不得**静默通过也**不得**当成豁免
        r = run()
        out = json.loads(r.stdout)
        assert r.returncode == 0, f"忘带参数不该拦下：rc={r.returncode}"
        assert "degenerate_witness" in out["warnings"], (
            f"忘带 --witness 时必须留下「未做该判定」：{out['warnings']}")

        # (c) 显式豁免 → 连告警都不留
        r = run("--no-witness")
        out = json.loads(r.stdout)
        assert "degenerate_witness" not in out["warnings"], (
            f"--no-witness 应清掉未判告警：{out['warnings']}")
        assert r.returncode == 0

