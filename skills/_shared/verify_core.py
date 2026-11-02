#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_core.py —— 阶段结果校验器的**共享骨架**（WGS / GWAS / 表型共用）

为什么单独抽出来
================
WGS（`skills/wgs-expressive-pipeline/scripts/stage_verify.py`）与 GWAS
（`skills/gwas-pipeline/scripts/gwas_verify.py`）的门禁必须是同一套语义：

  · 判据分 `hard` / `warn` 两级，`hard` 不过即**整阶段失败**（不写 `.done`）；
  · **fail-closed**：工具缺失、样本名单为空、校验器内部异常、报告写不出盘 —— 一律
    判失败，绝不"跳过 = 通过"（见 `Report.need_tool` 的说明）；
  · 报告落盘 `{RUN_DIR}/.verify/{stage}.json`，`pass` / `checks` / `hard_failures`
    是编排层硬门禁要读的键（`skills/wgs-expressive-pipeline/SKILL.md` §2.5）；
  · 人类可读渲染格式一致，编排层与用户看到的是同一种报告。

**为什么不是各写一份**：两套实现必然漂移 —— 本仓库已经把"同一个断点的两端各说各话"
列为根因 D（0.4.0 七条缺陷全属这一类）。共享骨架让 `hard`/`warn` 的语义只有一处定义。

边界
----
本模块只放**与管线无关**的东西：判据容器、进程调用、工具探测、阶段注册。
具体判据（BAM/VCF/FASTQ/PLINK）留在各自的校验器里 —— 那是领域知识，不该共享。

用法
----
    import os, sys
    sys.path.insert(0, os.path.join(<仓库根>, "skills", "_shared"))
    from verify_core import Report, Check, make_stage_registry, run, have, tool

    _STAGES, stage = make_stage_registry()

    @stage("01_bwa_sort")
    def verify_align(args, rep): ...

⚠️ 校验器**不直接从交互式 shell 调用**，而是由阶段脚本的收尾段调用，而阶段脚本本身
已在 `conda run -n <env> bash` 里 —— 故调用处**不要**再套一层 `conda run`（会以 rc=127
失败，见 stage_verify.py 的 docstring）。
"""
from __future__ import annotations

import os
import shutil
import subprocess


# ============================ 阶段注册 ============================

def make_stage_registry():
    """返回 `(dict, decorator)`：`@decorator(name)` 把校验函数登记进 dict。

    ⚠️ 刻意返回**一个 dict**而不是封装成类：现有测试与探针直接引用
    `SV._STAGES`（`monkeypatch.setitem(SV._STAGES, ...)` 注入假阶段、
    `len(SV._STAGES) == 6` 断言阶段数）。保持它是模块级 dict，这些判据才继续有效 ——
    封装成对象会把"阶段数"这类断言悄悄改成永远成立。
    """
    stages: dict[str, callable] = {}

    def stage(name):
        def deco(fn):
            stages[name] = fn
            return fn
        return deco

    return stages, stage


# ============================ 判据 ============================

class Check:
    """一条检查的结论。"""

    __slots__ = ("name", "level", "passed", "detail", "skipped")

    def __init__(self, name, level, passed, detail="", skipped=False):
        self.name = name
        self.level = level
        self.passed = bool(passed)
        self.detail = detail
        self.skipped = skipped

    def as_dict(self):
        return {"name": self.name, "level": self.level, "passed": self.passed,
                "detail": self.detail, "skipped": self.skipped}

    def render(self):
        if self.skipped:
            return f"  [SKIP] {self.name}: {self.detail}"
        mark = "PASS" if self.passed else ("FAIL" if self.level == "hard" else "WARN")
        return f"  [{mark}] {self.name}: {self.detail}"


class Report:
    """一个阶段的校验报告：判据列表 + 跨阶段可复用的实测值。

    ⚠️ `read_totals` / `primary_totals` 是 WGS 的概念（reads 守恒与外部锚）。留在这里
    而不是下沉到子类，是为了让 WGS 的报告 JSON **逐字不变**（下游 `--prev-reads-file`
    直接读这两个键）。GWAS 用 `GwasReport` 子类把它们去掉并换成自己的 `extras`。
    """

    def __init__(self, stage_name, run_dir):
        self.stage = stage_name
        self.run_dir = run_dir
        self.checks: list[Check] = []
        self.missing_tools: set[str] = set()
        # 本阶段观测到的每样本 reads 数，落进 .verify/*.json 供**下一阶段**做守恒比较。
        # 必须真的写出来：早期版本里 Stage 2 会读 prev["read_totals"]，但没有任何地方
        # 写过这个键 —— 那条守恒检查于是永远静默跳过（声称存在、从不生效）。
        self.read_totals: dict[str, int] = {}
        # 每样本的 **read 段数** —— 供下游做**外部锚**（见 stage_verify.py 的 reads_anchor）。
        #
        # ⚠️ 与 read_totals 的区别是本质的，两者**不可互换**：
        #   · read_totals   = BAM 里的**全部记录数**（含 secondary/supplementary），
        #                     用于相邻两阶段之间的"reads 守恒"；
        #   · primary_totals = **每段 read 恰好一条**的那个数（BAM 取 primary 记录，
        #                     FASTQ 侧就是实测条数）—— 只有它具有"与上游输入可比"的语义。
        # 实测（bwa mem 0.7.19，6000 段输入）：全部记录 6300、primary 6000 —— 拿 read_totals
        # 去跟 FASTQ 对账会**系统性多出** supplementary，健康数据反被拦下。
        # 故两者分开命名、分开落盘，从变量名上杜绝混用（同 check_bam 里 idx_* 的做法）。
        self.primary_totals: dict[str, int] = {}

    def add(self, name, level, passed, detail="", skipped=False):
        self.checks.append(Check(name, level, passed, detail, skipped))
        return self

    def skip(self, name, why, level="hard"):
        return self.add(name, level, True, why, skipped=True)

    def need_tool(self, name, purpose):
        """工具缺失时登记为**硬性失败**，而不是跳过。

        为什么不能"跳过 = 通过"：校验的全部价值在于「没验证过的不放行」。
        若工具缺失时判通过，那么任何一台没装 samtools/bcftools 的机器上，
        本门禁会**静默失效** —— 坏数据照常通过，而报告上一切正常。
        那正是这套机制要防的"静默错误"本身，只是换了个层级。

        缺失与"数据有问题"是两回事，所以错误文案明确区分：这里报的是**环境缺工具**，
        让人一眼看出该去装工具，而不是去查数据。
        """
        self.missing_tools.add(name)
        return self.add(
            f"tool[{name}]", "hard", False,
            f"未找到 `{name}` —— 无法完成「{purpose}」。"
            f"校验无法进行即不放行（fail-closed）；请先确保它在 PATH 中"
            f"（conda run -n Agent 下工具应在 PATH；直接跑脚本时可 source "
            f"skills/_shared/env.sh）")

    @property
    def hard_failures(self):
        return [c for c in self.checks if c.level == "hard" and not c.passed]

    @property
    def warnings(self):
        return [c for c in self.checks if c.level == "warn" and not c.passed]

    @property
    def blocked(self):
        return bool(self.hard_failures)

    def as_dict(self):
        return {
            "stage": self.stage,
            "run_dir": self.run_dir,
            "pass": not self.blocked,
            "missing_tools": sorted(self.missing_tools),
            "hard_failures": [c.name for c in self.hard_failures],
            "warnings": [c.name for c in self.warnings],
            # 供下一阶段用 --prev-reads-file 做 reads 守恒比较
            "read_totals": dict(self.read_totals),
            # 供下一阶段做**外部锚**（每段 read 一条的那个数，见 __init__ 的说明）
            "primary_totals": dict(self.primary_totals),
            "checks": [c.as_dict() for c in self.checks],
        }

    def render(self):
        lines = [f"\n========== 阶段校验 {self.stage} =========="]
        lines += [c.render() for c in self.checks]
        if self.missing_tools:
            # 与环境相关的失败单独点出来：让人一眼看出"该装工具"而不是"该查数据"，
            # 免得在一个纯环境问题上翻日志找数据线索
            lines.append(f"🔧 环境缺工具：{', '.join(sorted(self.missing_tools))} —— "
                         f"这是**环境问题**（非数据问题）：装上后重跑本阶段即可，"
                         f"无需改动上游产出")
        if self.blocked:
            lines.append(f"❌ 未通过：{len(self.hard_failures)} 项硬性检查失败 "
                         f"→ 不写 .done（编排层不得推进）："
                         f"{', '.join(c.name for c in self.hard_failures)}")
        elif self.warnings:
            lines.append(f"⚠️  通过，但有 {len(self.warnings)} 项告警："
                         f"{', '.join(c.name for c in self.warnings)}")
        else:
            lines.append("✅ 全部检查通过")
        return "\n".join(lines)


# ============================ 运行时工具 ============================

def tool(name):
    """运行时探测工具路径；找不到返回 None（仓库硬规则：不写死本机路径）。"""
    return shutil.which(name)


def run(cmd, timeout=120):
    """跑一条只读命令，返回 (rc, stdout, stderr)。绝不抛异常。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"超时（>{timeout}s）"
    except FileNotFoundError as e:
        return 127, "", str(e)
    except Exception as e:                                    # noqa: BLE001
        return 1, "", str(e)


def have(path):
    """文件存在**且非空**才算有。

    ⚠️ "存在"必须是 `isfile ∧ size>0`：0 字节文件在 shell 里 `[ -f ]` 为真，
    于是"脚本崩在打开文件之后、写入之前"会被当成产物已就绪。实测踩到过：
    resume 用 `[ -f ]` 判完成、门禁用非空判，重跑永久卡死。
    """
    return bool(path) and os.path.isfile(path) and os.path.getsize(path) > 0


def parse_samples(s):
    """把 `--samples` 解析成样本名列表（`a,b,c` 或 run.params.json 的 sample_names）。

    空串 → `[]`。调用方**必须**对空列表做 fail-closed 处理：各阶段的判据循环写成
    `for s in samples`，名单为空时循环零次、报告变成"全部通过"而实际一条判据都没跑。
    """
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]
