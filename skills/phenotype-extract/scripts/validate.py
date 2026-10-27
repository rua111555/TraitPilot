# -*- coding: utf-8 -*-
"""validate.py —— 表型提取的出口校验门禁

为什么需要它：表型提取的错误大多是**静默**的 —— 抠图失败、模型没加载对、
阈值与数据不匹配，产出的往往不是一个报错，而是一个**看起来合理的数值**
（例如 IMF=0.0%）。这种错误在明细表里翻不出来，却会一路带进 GWAS，
最终污染关联结果。事后没人能判断那个 0.0 是「真的没脂肪」还是「抠图失败」。

本模块把「结果值是否可信」从口头约定变成**可执行的门禁**：

  单样本级
    · value_range    值必须落在插件声明的物理值域内（如 IMF ∈ [0,100]）
                     —— **插件未声明值域时降级为 warn** 并明说"未做该判定"（见 check_value_range）
    · 值本身不得是 NaN/Inf（"ok" 行却拿不到数 = 逻辑错误）

  整批级
    · fail_rate          失败样本占比不得超过阈值（默认 5%）
    · not_constant       不能所有样本都取同一个值（= 模型没真跑，最常见）
    · nir_agreement      有参照真值时，预测 vs 参照相关性不得低于阈值
    · sample_count       通过校验的样本数不得低于下限
    · sample_uniqueness  同一个 Taxa 只能有一个成功测量值（**hard**）：按图像一行时，
                         同一个体拍多张会让 PLINK 导出按键去重、GAPIT 导出保留全部行，
                         两个 GWAS 文件**静默分歧**；多次测量值互相矛盾时更是无从取舍

判定分两级：
  · **hard** 不过 → 拒绝写出 GWAS 表型文件（明细 CSV 仍然写出，供排查），退出码非 0
  · **warn** 不过 → 照常写出，但把警告打到 stdout/JSON，并把状态透给编排层

设计约束：本模块**不 import 任何 ML 依赖**（torch/transformers/cv2），
纯 numpy/pandas —— 这样它既能被提取主脚本调用，也能作为独立 CLI 在
GWAS 阶段的入口处重新校验一份已存在的表型文件（同一个断点的两端）。

⚠️ 为守住这条约束，`PhenotypeError` 从 ``phenotypes/_errors.py`` 导入 ——
那是只放异常类、零依赖的模块。**不要**改成从 ``phenotypes._common`` 导入：
后者顶层 `import cv2`，会把 opencv 拖进本模块（实测：屏蔽 cv2 后 import
本模块直接 ModuleNotFoundError，与"校验未通过"完全无关，排查方向被带偏）。
"""
import argparse
import json
import math
import os
import sys

import numpy as np
import pandas as pd

# 异常类来自零依赖模块（**不是** phenotypes._common —— 它会 import cv2）。
# scripts/ 不在包路径上（无 __init__.py），故把本目录插进 sys.path。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from phenotypes._errors import PhenotypeError  # noqa: E402,F401  （对外 re-export）


# ============================ 单条检查结果 ============================
class Check:
    """一条检查的结论。level: hard（不过即拦截）/ warn（不过仅告警）。"""

    __slots__ = ("name", "level", "passed", "detail")

    def __init__(self, name, level, passed, detail=""):
        self.name = name
        self.level = level
        self.passed = bool(passed)
        self.detail = detail

    def as_dict(self):
        return {"name": self.name, "level": self.level,
                "passed": self.passed, "detail": self.detail}

    def render(self):
        mark = "PASS" if self.passed else ("FAIL" if self.level == "hard" else "WARN")
        return f"  [{mark}] {self.name}: {self.detail}"


class GateReport:
    """整批校验报告。"""

    def __init__(self, checks=None):
        self.checks = list(checks or [])

    def add(self, name, level, passed, detail=""):
        self.checks.append(Check(name, level, passed, detail))
        return self

    @property
    def ok(self):
        """全部门禁通过（hard 与 warn 都过）。"""
        return all(c.passed for c in self.checks)

    @property
    def hard_failures(self):
        return [c for c in self.checks if c.level == "hard" and not c.passed]

    @property
    def warnings(self):
        return [c for c in self.checks if c.level == "warn" and not c.passed]

    @property
    def blocked(self):
        """是否应拦截产出。"""
        return bool(self.hard_failures)

    def as_dict(self):
        return {
            "blocked": self.blocked,
            "ok": self.ok,
            "checks": [c.as_dict() for c in self.checks],
            "hard_failures": [c.name for c in self.hard_failures],
            "warnings": [c.name for c in self.warnings],
        }

    def render(self, title="表型结果校验"):
        lines = [f"\n========== {title} =========="]
        lines += [c.render() for c in self.checks]
        if self.blocked:
            lines.append(f"❌ 拦截：{len(self.hard_failures)} 项硬性检查未通过，"
                         f"不写出 GWAS 表型文件（明细 CSV 保留供排查）")
        elif self.warnings:
            lines.append(f"⚠️  放行但有 {len(self.warnings)} 项告警，请人工确认")
        else:
            lines.append("✅ 全部检查通过")
        return "\n".join(lines)


# ============================ 单样本校验 ============================
def value_bounds(task):
    """从 TASK 读取插件声明的物理值域，返回 (lo, hi) 或 None。

    支持 ``validate.value_range``，也接受插件直接写 ``value_range``。
    """
    spec = task.get("validate") or {}
    rng = spec.get("value_range", task.get("value_range"))
    if not rng:
        return None
    try:
        lo, hi = rng
        return float(lo), float(hi)
    except (TypeError, ValueError):
        return None


def check_value(value, rng):
    """返回 (是否通过, 说明)。rng 为 None 时不判值域。"""
    if value is None:
        return False, "值为 None"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False, f"值不可转成数值: {value!r}"
    if math.isnan(v) or math.isinf(v):
        return False, f"值为 {v}（NaN/Inf 不是合法表型）"
    if rng is None:
        # 走到这里说明调用方仍在用"未声明值域"的路径（validate_batch 已在插件
        # 未声明时降级为 warn 并明说未判定）。此处返回通过是**单值**语义：
        # 值本身是有限实数，只是没有范围可比。
        return True, f"{v:.4g}（未声明值域，仅校验其为有限实数）"
    lo, hi = rng
    if v < lo or v > hi:
        return False, f"{v:.4g} 越界，允许范围 [{lo:g}, {hi:g}]"
    return True, f"{v:.4g} ∈ [{lo:g}, {hi:g}]"


# ============================ 整批校验 ============================
def _ref_col(df, pcol):
    """找参照真值列（IMF_NIR_% / *_ref 等）。"""
    for c in df.columns:
        if c == pcol:
            continue
        if str(c).endswith("NIR_%") or str(c).endswith("_ref"):
            return c
    return None


def validate_batch(rows, pcol, task=None, *, max_fail_rate=0.05,
                   min_samples=1, min_nir_r=None, title=None):
    """对一批提取结果做出口校验。

    rows: list[dict]，每行至少含 ``Taxa``、``状态``、``pcol``（与主脚本一致）
    pcol:  表型值所在列名
    task:  插件 TASK dict（读 value_range 声明）；可传 None 表示不判值域

    返回 GateReport。
    """
    task = task or {}
    report = GateReport()
    df = pd.DataFrame(rows)
    total = len(df)

    # ---- 1. 单样本值域 ----
    # ⚠️ 进入本段的三个前提（`total` 非空 / 列存在 / 有 `状态` 列）任一不成立时，
    #    早期实现是**整条判据静默消失** —— 报告里连一行都没有，末尾却照样打
    #    "✅ 全部检查通过"。实测：喂一个只有 `Taxa,IMF_预测_%` 两列的外部 CSV
    #    （正是 CLI 文档里"在 GWAS 入口处重新校验一份已存在的表型文件"的用法），
    #    1000000 这种百分比表型不可能的值照过、rc=0，报告读起来像"值域已把关"。
    #    这里改成**明说为什么没判**，让缺口在报告里看得见（而不是靠读者知道
    #    "报告里没出现 value_range 就意味着没判"）。
    rng_ready = bool(total) and pcol in df.columns and "状态" in df.columns
    if not rng_ready:
        _why = ("CSV 里没有 `状态` 列 —— 无法区分「推理成功的样本」与「失败样本」，"
                "故**未做值域判定**（失败样本的值不该参与越界统计）。"
                "本门禁的明细 CSV（`*_phenotype.csv`）自带该列；"
                "外部/裁剪过的 CSV 请补上，或改在提取流程内校验"
                if (total and pcol in df.columns) else
                f"CSV 里没有 `{pcol}` 列，**未做值域判定**")
        report.add("value_range", "warn", False, _why)
    else:
        rng = value_bounds(task)
        oks = df[df["状态"] == "ok"]
        bad = []
        for _, r in oks.iterrows():
            good, why = check_value(r[pcol], rng)
            if not good:
                bad.append(f"{r['Taxa']}({why})")
        if rng is None:
            # ⚠️ 插件没声明值域时，这条判据**什么也没断言** —— 但仍以 hard 级
            #    "PASS 10/10" 出现在报告里，读报告的人会以为值域已被把关。
            #    一个没写 value_range 的新插件于是静默失去越界保护，而报告全绿。
            #    实测：填 1000000..1000009（百分比表型不可能的值）仍 `[PASS] value_range`。
            #    这里降级为 warn 并**明说未判定**，让缺口看得见（这正是本项目
            #    反复出现的"纸面具备"形态：声称存在、实际从不生效）。
            report.add("value_range", "warn", False,
                       f"{len(oks)} 个成功样本**未做值域判定** —— "
                       f"插件未声明 validate.value_range（物理不可能的值不会被拦）。"
                       f"请在该表型插件的 TASK 里补上，如 "
                       f'"validate": {{"value_range": [lo, hi]}}')
        else:
            report.add(
                "value_range", "hard", not bad,
                f"{len(oks) - len(bad)}/{len(oks)} 个成功样本的值在合法范围"
                + (f"；越界: {'; '.join(bad[:5])}" if bad else ""))

    # ---- 2. 失败率 ----
    n_fail = int((df["状态"] != "ok").sum()) if total and "状态" in df.columns else 0
    rate = (n_fail / total) if total else 0.0
    report.add(
        "fail_rate", "hard", rate <= max_fail_rate,
        f"{n_fail}/{total} 个样本失败（{rate:.1%}），阈值 ≤{max_fail_rate:.0%}"
        + ("" if rate <= max_fail_rate else "；先查明细 CSV 的『状态』列"))

    # ---- 3. 非常数（防模型没真跑）----
    vals = pd.to_numeric(df[pcol], errors="coerce").dropna() if (total and pcol in df.columns) else pd.Series(dtype=float)
    n = len(vals)
    if n >= 3:
        std = float(vals.std(ddof=0))
        uniq = int(vals.nunique())
        report.add(
            "not_constant", "hard", std > 0,
            f"n={n} 个有效值，std={std:.4g}，唯一值 {uniq} 个"
            + ("；**所有样本同值**，模型很可能没真正推理" if std == 0 else ""))
    else:
        report.add("not_constant", "warn", False,
                   f"有效值仅 {n} 个（<3），无法判断是否为常数输出")

    # ---- 4. 参照一致性 ----
    rc = _ref_col(df, pcol) if total else None
    if rc and n >= 3:
        a = pd.to_numeric(df[rc], errors="coerce").values
        b = pd.to_numeric(df[pcol], errors="coerce").values
        m = ~np.isnan(a) & ~np.isnan(b)
        if m.sum() >= 3:
            r = float(np.corrcoef(b[m], a[m])[0, 1]) if np.std(b[m]) and np.std(a[m]) else float("nan")
            if math.isnan(r):
                report.add("nir_agreement", "warn", False,
                           f"{int(m.sum())} 条参照，预测或参照为常数，无法算相关")
            else:
                # 分级取决于**用户有没有明说**：
                #   · 显式给了 --min-nir-r → 这是他声明的验收标准，没达到就该拦（hard）
                #   · 没给（走默认 0.6）→ 相关性受数据本身影响（参照本身的噪声、
                #     批次大小、非线性），拿一个用户从未要求的阈值去拦截会变成
                #     "喊狼来了"，最终被绕过而彻底失去保护 —— 故只告警（warn）
                # 早期版本**一律 warn**，于是"模型加载错、预测与真值反相关"这种
                # 最强的整批异常信号也照放（实测 r=0.382 仍旧 EXIT=0）。
                explicit = min_nir_r is not None
                thr = float(min_nir_r) if explicit else 0.6
                report.add(
                    "nir_agreement", "hard" if explicit else "warn", r >= thr,
                    f"预测 vs 参照 r={r:.3f}（{int(m.sum())} 条，阈值 ≥{thr}）"
                    + ("" if r >= thr else
                       "；预测与真值不符，请先确认模型/阈值/关联表"
                       + ("（阈值由 --min-nir-r 指定，未达到即拦截）" if explicit else
                          "（默认阈值仅告警；确认过就加 --min-nir-r 让它变成硬性拦截）")))
        else:
            report.add("nir_agreement", "warn", False, f"参照非空仅 {int(m.sum())} 条（<3），跳过")

    # ---- 5. 产出样本数 ----
    report.add(
        "sample_count", "hard", n >= min_samples,
        f"可产出 {n} 个样本，下限 {min_samples}"
        + ("" if n >= min_samples else "；样本太少，不足以做关联分析（也可用 --min-samples 覆盖）"))

    # ---- 6. 样本唯一性（rows 是**按图像**一行，不是按个体）----
    #
    # ⚠️ 这条不是"锦上添花"：上面所有整批检查（值域/失败率/非常数/样本数）都作用在
    #    DataFrame 的行上，而 rows 是按**图像**逐行生成的（主脚本 `for idx, (et, num,
    #    path) in enumerate(images, 1)`）—— 同一个体拍两张就有两行。于是上面的
    #    `sample_count` 数的是"算出了数的图像数"，不是"可做关联的个体数"。
    #
    #    更糟的是两个 GWAS 导出用**不同**的构造方式：
    #      · PLINK 走 `pred = {r["Taxa"]: ...}` —— dict 键去重，重复个体只剩一条
    #      · GAPIT 走 `df[["Taxa", pcol]]`     —— 保留全部行
    #    同一次运行产出的两个文件行数不同、样本集不同，而没有任何机制发现它。
    #    GWAS 要求一个个体一个表型值，所以这里判 hard：与其让 dict 静默挑一个
    #    （"最后一张图赢"），不如把歧义摆在用户面前让他决定怎么合并。
    if total and "Taxa" in df.columns:
        ok_rows = df[df["状态"] == "ok"] if "状态" in df.columns else df
        n_ok_rows = len(ok_rows)
        n_uniq = int(ok_rows["Taxa"].nunique())
        dup = n_ok_rows - n_uniq
        if n_ok_rows:
            # 重复且**值不一致**时危害最大（同一个体两个互斥的表型值）
            dup_conflict = 0
            if dup and pcol in df.columns:
                for _, grp in ok_rows.groupby("Taxa"):
                    if len(grp) > 1:
                        v = pd.to_numeric(grp[pcol], errors="coerce").dropna()
                        if v.nunique() > 1:
                            dup_conflict += 1
            report.add(
                "sample_uniqueness", "hard", dup == 0,
                f"{n_uniq} 个不同个体 / {n_ok_rows} 张成功图像"
                + (f"；**有 {dup} 张是重复个体**"
                   + (f"，其中 {dup_conflict} 个的多次测量值**互相矛盾**" if dup_conflict else "")
                   + "。PLINK 导出按键去重、GAPIT 导出保留全部行 —— 两个文件会不一致。"
                   "同一个体只应有一个表型值：请先在明细 CSV 里合并或择一"
                   if dup else ""))

    if title:
        report.title = title
    return report


# ============================ 独立 CLI：复核一份已产出的表型文件 ============================
def _cli():
    ap = argparse.ArgumentParser(
        description="表型结果校验门禁：复核一份已产出的表型文件（供提取主脚本或 GWAS 入口调用）")
    ap.add_argument("--csv", required=True, help="明细 CSV（phenotype_extract.py 的 [out1] 产物）")
    ap.add_argument("--col", required=True, help="表型值列名，如 IMF_预测_%")
    ap.add_argument("--range", nargs=2, type=float, default=None, metavar=("LO", "HI"),
                    help="值域（不给则只做整批检查）")
    ap.add_argument("--min-samples", type=int, default=1)
    ap.add_argument("--max-fail-rate", type=float, default=0.05)
    ap.add_argument("--min-nir-r", type=float, default=None)
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（供编排层解析）")
    args = ap.parse_args()

    if not os.path.isfile(args.csv):
        msg = {"error": f"明细 CSV 不存在: {args.csv}"}
        print(json.dumps(msg, ensure_ascii=False) if args.json else f"[err] {msg['error']}")
        return 1

    df = pd.read_csv(args.csv)
    if args.col not in df.columns:
        msg = {"error": f"列 {args.col!r} 不在 CSV 中", "columns": list(df.columns)}
        print(json.dumps(msg, ensure_ascii=False) if args.json else f"[err] {msg['error']}")
        return 1

    task = {"validate": {"value_range": args.range}} if args.range else {}
    rep = validate_batch(df.to_dict("records"), args.col, task,
                         max_fail_rate=args.max_fail_rate,
                         min_samples=args.min_samples,
                         min_nir_r=args.min_nir_r)
    if args.json:
        print(json.dumps(rep.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(rep.render(f"表型结果校验 — {os.path.basename(args.csv)}"))
    return 1 if rep.blocked else 0


if __name__ == "__main__":
    sys.exit(_cli())
