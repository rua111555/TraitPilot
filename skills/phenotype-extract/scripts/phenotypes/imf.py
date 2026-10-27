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
    # 出口校验声明：液/气界面上脂肪像素占比的物理值域就是 0~100%
    "validate": {"value_range": [0.0, 100.0]},
}
