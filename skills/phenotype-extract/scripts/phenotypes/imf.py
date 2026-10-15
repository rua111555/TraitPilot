# -*- coding: utf-8 -*-
"""phenotypes/imf.py —— 肌内脂肪含量 IMF% 表型插件

计算: 用 imf_mask2former(Mask2Former, 类1=大理石纹脂肪) 分割脂肪,
      黑底板阈值抠整块肉 ROI(共享 meat_mask),
      IMF% = 100 × 脂肪mask∩ROI 像素数 / ROI 像素数。
参考值: 近红外法实测 IMF%(来自 .xls, 经 et→猪号→NIR 关联), 作为明细表对照列。
"""
import numpy as np

from ._common import meat_mask


def compute_imf(ctx):
    """ctx: 见主脚本 phenotype_extract.py (含 img/seg/et/thresh/mappings)。返回 (value, extra)"""
    img = ctx["img"]       # BGR uint8
    seg = ctx["seg"]       # int 数组类标签 (0=背景,1=脂肪)
    roi = meat_mask(img, ctx.get("thresh", 30))
    meat_px = int(roi.sum())
    fat_px = int((seg == 1)[roi].sum())
    ratio = 100.0 * fat_px / max(meat_px, 1)

    extra = {"猪肉面积_px": meat_px, "脂肪面积_px": fat_px}
    m = ctx.get("mappings") or {}
    pig = m.get("et2pig", {}).get(str(ctx.get("et", "")).lower(), "")
    nir = m.get("pig2imf", {}).get(pig) if pig else None
    extra["IMF_NIR_%"] = None if (nir is None or (isinstance(nir, float) and np.isnan(nir))) else nir
    return ratio, extra


TASK = {
    "name": "IMF",
    "title": "肌内脂肪含量 IMF%（脂肪mask面积 / 整块肉ROI面积）",
    "description": "Mask2Former 分割大理石纹脂肪(类1) + 黑底板阈值抠整块肉ROI，按像素面积占比%计表型；"
                   "外部参照列=近红外实测IMF%",
    "model_names": ["imf_mask2former"],
    "trait": "IMF",
    "pheno_col": "IMF_预测_%",
    "compute": compute_imf,
    "mask_encode": lambda seg: np.where(seg == 1, 38, 0).astype(np.uint8),
}
