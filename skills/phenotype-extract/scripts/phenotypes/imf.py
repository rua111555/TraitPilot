# -*- coding: utf-8 -*-
"""phenotypes/imf.py —— 肌内脂肪含量 IMF% 表型插件

计算: 用 imf_mask2former(Mask2Former, 类1=大理石纹脂肪) 分割脂肪,
      阈值抠出整块组织/肉的 ROI(共享 foreground_mask),
      IMF% = 100 × 脂肪mask∩ROI 像素数 / ROI 像素数。
参考值: 近红外法实测 IMF%(来自 .xls, 经 样本编号→个体号→参照 关联), 作为明细表参照列。

注: IMF 是「肌肉脂肪含量」这一可测性状, 与物种无关——本插件在动物样品上开发验证,
    换物种只需换模型/关联表, 插件代码无需改动。
"""
import numpy as np

from ._common import foreground_mask, PhenotypeError


def compute_imf(ctx):
    """ctx: 见主脚本 phenotype_extract.py (含 img/seg/sample/thresh/mappings)。返回 (value, extra)

    ⚠️ 抠图失败必须**抛错**，不能返回兜底值：ROI 面积为 0 时若除以 max(meat_px, 1)，
    会得到 IMF=0.0% —— 一个落在合法值域内、看起来完全正常的数。这种错会一路带进
    GWAS 且事后无法从数值上识别（分不清「真没脂肪」还是「抠图失败」）。
    因此这里显式判空并抛 PhenotypeError，主脚本会把它记成 fail 行而非数值 0。

    ⚠️ **第二种同形退化**（第二轮审计实测）：`meat_px <= 0` 只覆盖「ROI 恰好为空」
    这一种形态。拍摄事故/占位图的常见形态是**整幅图都亮于阈值**（过曝、空拍、
    均匀底板）—— 此时前景 mask 的最大连通域就是**整幅画面**，`min_component`
    不触发，`meat_px` = 全图像素数，模型对这类图输出全 0 类 → `fat_px = 0` →
    `ratio = 0.0`，**落在 [0,100] 值域内**，与上面那个 max(meat_px,1) 兜底值
    形态完全一致。混在真实批次里时 `not_constant`（std>0）与逐样本 `value_range`
    （0.0 合法）都抓不到，`fail_rate` 也看不到它（状态=ok）—— 于是这一行以
    ok + 0.0 落盘、写进两个 GWAS 文件、rc=0。实测：混 1 张时 not_constant 照过；
    161 张全换成同一张均匀亮场图时 not_constant 仍 PASS（唯一值 2 个）。
    故这里判 **ROI 覆盖整幅画面**：真实拍摄（深底板 + 目标）总有背景边，
    覆盖率恒 < 1；等于 1 意味着根本没有背景 —— 那不是"目标很大"，是这副图
    抠不出目标（阈值与底板假设不成立）。

    **为什么用"恰好等于整幅画面"而不设一个覆盖率阈值常数**：这两类退化图刚好
    被两条判据**无重叠地**分开，不需要任何拍出来的数字 ——
      · 图整体**暗于**阈值（空拍/纯黑底板/欠曝）→ mask 为空 → `meat_px == 0`（已有判据）
      · 图整体**亮于**阈值（过曝/均匀浅色底板）→ mask 填满 → `meat_px == 全图像素`（本判据）
      · 两者之间（有明暗之分）→ 就是一张能抠的图，不该拦
    若改用"覆盖率 > 99%"之类的阈值，就等于把"目标恰好拍满画面"这种**真实**照片
    也判失败 —— 对一个要换物种/换拍摄条件的通用框架，那是拦健康数据。
    `roi ⊆ 画幅`，故 `meat_px >= frame_px` 与 `==` 等价，写成 `>=` 只为表明意图。

    同时把 ROI 覆盖率写进 extra 落盘：它是"这张图抠得对不对"的自明信号
    （实测真值批次 48%~82%，过曝图 100%），此前算出来了却没人写、没人看。

    ⚠️ **第三种同形退化**（第二轮审计 F15 残余，2026-10-28 补）：前两条判据都建在
    **ROI** 上，于是"ROI 不满幅、但分割结果全 0"这一形态**整条落空**。实测复现：
    把真图 et3 的目标区过曝吹白（ROI 覆盖率 71.7441%，与健康 et3 **完全相同**，
    落在真值批次 47.95%~81.86% 区间**之内**）→ `meat_px=229581`（正常）、
    `fat_px=0` → `ratio=0.0`，落在 [0,100] 值域内、状态 ok，与真值 2.1625 同一行；
    门禁 6 条判据全 PASS、rc=0，`0.0` 照写进两个 GWAS 文件。
    **覆盖率在这里救不了你**：退化图与健康图的覆盖率区间重叠，任何覆盖率阈值
    要么放过它、要么拦下健康图（实测 162 张真图覆盖率 47.95%~81.86%，
    退化图 71.74% 正在其中）—— 这是本质，不是标定没做好。

    故判 **分子为 0**：`fat_px == 0` 意味着"ROI 里一个脂肪像素都没有"，而
    IMF% = 100·fat_px/meat_px，分母非 0 时分子为 0 ⟺ 结果恰为 0.0 ——
    **完全等价**，不存在"真没脂肪"与"分割失败"的区分空间（两者都不可信，
    而 0.0 这个数把它们与"测出来就是 0"混在一起，事后无从分辨）。
    实测标定（真模型 + 真图库 162 张，非模拟）：`脂肪面积_px min = 799`，
    等于 0 的 **0 张** ⇒ 该判据对真实数据**零误伤**；两张退化 fixture 均为 0。
    这条同时声明给门禁（`validate.zero_witness`），使**外部/陈旧 CSV**
    在 GWAS 入口被重新校验时也拦得住（插件的 raise 管不到那条路径）。
    """
    img = ctx["img"]       # BGR uint8
    seg = ctx["seg"]       # int 数组类标签 (0=背景,1=脂肪)
    roi = foreground_mask(img, ctx.get("thresh", 30))
    meat_px = int(roi.sum())
    h, w = img.shape[:2]
    frame_px = int(h) * int(w)
    coverage = (meat_px / frame_px) if frame_px else 0.0
    if meat_px <= 0:
        raise PhenotypeError(
            f"前景抠取失败：ROI 面积=0（thresh={ctx.get('thresh')}）；"
            f"图像对比度/底板颜色与阈值不匹配，调 --thresh 后重试")
    if meat_px >= frame_px:
        raise PhenotypeError(
            f"前景抠取失败：ROI 覆盖**整幅画面**（{meat_px}/{frame_px} 像素，"
            f"thresh={ctx.get('thresh')}）—— 整幅图都亮于阈值，抠出的\"前景\" "
            f"就是画面本身。常见原因：过曝/空拍/均匀底板占位图，或底板不是深色"
            f"（阈值假设不成立）。这类图会算出 IMF=0.0%，落在合法值域内、"
            f"事后无法与\"真没脂肪\"区分，故直接判失败")
    fat_px = int((seg == 1)[roi].sum())
    if fat_px <= 0:
        raise PhenotypeError(
            f"分割结果为空：ROI 抠取正常（覆盖 {100.0 * coverage:.2f}%、"
            f"{meat_px}/{frame_px} 像素），但其中**脂肪像素数为 0** → IMF=0.0%。"
            f"ROI 覆盖率落在健康区间内，故前两条退化判据看不到它；而 0.0 又落在"
            f"[0,100] 值域内，与\"真没脂肪\"无法分辨。常见原因：目标区过曝/吹白、"
            f"拍到了空底板、模型与图像不匹配。实测真图库 162 张的脂肪像素数"
            f"最少 799（含此值在内从未为 0），故判失败而非产出 0.0")
    ratio = 100.0 * fat_px / meat_px

    extra = {"组织面积_px": meat_px, "脂肪面积_px": fat_px,
             "ROI覆盖率_%": round(coverage * 100.0, 4)}
    m = ctx.get("mappings") or {}
    ident = m.get("sample2id", {}).get(str(ctx.get("sample", "")).lower(), "")
    ref = m.get("id2ref", {}).get(ident) if ident else None
    extra["IMF_NIR_%"] = None if (ref is None or (isinstance(ref, float) and np.isnan(ref))) else ref
    return ratio, extra


TASK = {
    "name": "IMF",
    "title": "肌内脂肪含量 IMF%（脂肪mask面积 / 组织ROI面积）",
    "description": "Mask2Former 分割大理石纹脂肪(类1) + 阈值抠出组织ROI，按像素面积占比%计表型；"
                   "参照列=近红外实测IMF%（若提供关联表）",
    "model_names": ["imf_mask2former"],
    "trait": "IMF",
    "pheno_col": "IMF_预测_%",
    "compute": compute_imf,
    "mask_encode": lambda seg: np.where(seg == 1, 38, 0).astype(np.uint8),
    # 出口校验声明：
    # · value_range：液/气界面上脂肪像素占比的物理值域就是 0~100%
    # · degenerate_witness：`脂肪面积_px == 0` 的行不是「测出 0%」而是「分割失败」。
    #   上面第 88 行已在本流程内抛错拦下；本声明让**门外那条路径**（独立 CLI 复核
    #   一份已产出的明细 CSV、或下游在 GWAS 入口复核）也拦得住 —— 那里插件不参与，
    #   抛错管不到。实测标定见 compute_imf docstring（真图 162 张 min=799，零误伤）。
    "validate": {"value_range": [0.0, 100.0],
                 "degenerate_witness": {"column": "脂肪面积_px", "value": 0}},
}
