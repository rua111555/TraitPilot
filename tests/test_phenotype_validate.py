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
import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHENO_SCRIPTS = os.path.join(ROOT, "skills", "phenotype-extract", "scripts")
sys.path.insert(0, PHENO_SCRIPTS)

from validate import (  # noqa: E402
    GateReport, PhenotypeError, check_value, validate_batch, value_bounds,
)

PCOL = "IMF_预测_%"


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
    rep = validate_batch(rows_of(normal()), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}})
    assert not rep.blocked, rep.render()
    assert not rep.warnings, rep.render()


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
    rep = validate_batch(rows_of(vals, ref=ref), PCOL,
                         {"validate": {"value_range": [0.0, 100.0]}},
                         min_nir_r=0.6)
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
from phenotype_extract import build_gwas_exports  # noqa: E402


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
    pred, order, not_in_fam = build_gwas_exports(rows, PCOL, fam)
    assert sorted(order) == sorted(pred), \
        f"两导出的样本集不一致：PLINK={sorted(order)} GAPIT={sorted(pred)}"
    assert set(order) == {"s1", "s2", "s3", "s4"}, \
        f"有预测值的样本被静默丢掉：{set(order)}"
    assert not_in_fam == ["s4"], not_in_fam


def test_gwas_exports_keep_samples_absent_from_fam(tmp_path):
    """fam 里没有的样本必须保留（它是排序基准，不是过滤器）。"""
    rows = _rows([("s9", 9.0), ("s8", 8.0)])
    fam = _write_fam(tmp_path, ["s1", "s2"])                # 完全无交集
    pred, order, not_in_fam = build_gwas_exports(rows, PCOL, fam)
    assert sorted(order) == ["s8", "s9"], f"样本被丢了：{order}"
    assert not_in_fam == ["s8", "s9"]
    assert sorted(order) == sorted(pred)


def test_gwas_exports_follow_fam_row_order(tmp_path):
    """fam 序与样本号序不同时，PLINK 侧要按 fam 的行序（而非字典序）。"""
    rows = _rows([("s1", 1.0), ("s2", 2.0), ("s3", 3.0)])
    fam = _write_fam(tmp_path, ["s3", "s1", "s2"])          # 故意与字面序不同
    pred, order, not_in_fam = build_gwas_exports(rows, PCOL, fam)
    assert order == ["s3", "s1", "s2"], f"没按 fam 行序：{order}"
    assert not not_in_fam


def test_gwas_exports_fam_absent_falls_back_to_sorted(tmp_path):
    """没传 --fam（或路径无效）时按样本号排，且仍与 GAPIT 同集合。"""
    rows = _rows([("s3", 3.0), ("s1", 1.0)])
    for fam in (None, str(tmp_path / "nope.fam")):
        pred, order, not_in_fam = build_gwas_exports(rows, PCOL, fam)
        assert order == ["s1", "s3"], order
        assert sorted(order) == sorted(pred)
        assert not not_in_fam


def test_gwas_exports_failed_and_nan_samples_excluded(tmp_path):
    """失败样本与 NaN 值不得进入任一导出（两处一致地排除）。"""
    rows = [{"Taxa": "s1", "状态": "ok", PCOL: 1.0},
            {"Taxa": "s2", "状态": "fail:缺图", PCOL: float("nan")},
            {"Taxa": "s3", "状态": "ok", PCOL: float("nan")}]
    pred, order, _ = build_gwas_exports(rows, PCOL, None)
    assert order == ["s1"], f"失败/NaN 样本混进了导出：{order}"
    assert list(pred) == ["s1"]


def test_gwas_exports_dedupes_repeated_taxa(tmp_path):
    """同一个体多张图：按键去重，两个导出都只有一条（与 sample_uniqueness 一致）。"""
    rows = [{"Taxa": "s1", "状态": "ok", PCOL: 1.0},
            {"Taxa": "s1", "状态": "ok", PCOL: 1.5},
            {"Taxa": "s2", "状态": "ok", PCOL: 2.0}]
    pred, order, _ = build_gwas_exports(rows, PCOL, None)
    assert order == ["s1", "s2"], order
    assert len(order) == len(set(order)), "导出里有重复个体"
