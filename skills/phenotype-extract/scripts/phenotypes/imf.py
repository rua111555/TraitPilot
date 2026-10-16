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

from ._common import foreground_mask


def compute_imf(ctx):
    """ctx: 见主脚本 phenotype_extract.py (含 img/seg/sample/thresh/mappings)。返回 (value, extra)"""
    img = ctx["img"]       # BGR uint8
    seg = ctx["seg"]       # int 数组类标签 (0=背景,1=脂肪)
    roi = foreground_mask(img, ctx.get("thresh", 30))
    meat_px = int(roi.sum())
    fat_px = int((seg == 1)[roi].sum())
    ratio = 100.0 * fat_px / max(meat_px, 1)

    extra = {"组织面积_px": meat_px, "脂肪面积_px": fat_px}
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
}
