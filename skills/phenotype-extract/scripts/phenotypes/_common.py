# -*- coding: utf-8 -*-
"""phenotypes/_common.py —— 表型提取插件的共享工具（模型加载 / 前景抠取 / 数据库 / 关联表）"""
import os
import json
import sqlite3
import argparse

import numpy as np
import pandas as pd
import cv2


# ============================ 共享工具: 抠前景目标 ROI ============================
def foreground_mask(img, thresh=30, min_component=2000):
    """从纯色底板照片抠掉背景, 得到目标物体的二值 mask(最大连通域 + 填洞)。img=BGR。
    thresh 为灰度阈值: 亮于底板的目标保留(黑底板+浅色目标场景的缺省)。"""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    m = (gray > thresh).astype(np.uint8)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE,
                         cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN,
                         cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n <= 1:
        return np.zeros(gray.shape, dtype=bool)
    areas = stats[1:, cv2.CC_STAT_AREA]
    best = 1 + int(np.argmax(areas))
    if areas[best - 1] < min_component:
        return np.zeros(gray.shape, dtype=bool)
    roi = labels == best
    return fill_holes(roi)


def fill_holes(binary):
    from scipy import ndimage
    return ndimage.binary_fill_holes(binary)


# ============================ 共享工具: 语义分割模型 ============================
def build_model(args, offline=True):
    from transformers import (Mask2FormerConfig,
                              Mask2FormerForUniversalSegmentation,
                              Mask2FormerImageProcessor)
    kwargs = {} if not offline else {"local_files_only": True}
    proc = Mask2FormerImageProcessor.from_pretrained(
        args.model, size=(args.size, args.size), ignore_index=255, **kwargs)
    cfg = Mask2FormerConfig.from_pretrained(args.model, **kwargs)
    cfg.num_labels = args.num_labels
    model = Mask2FormerForUniversalSegmentation.from_pretrained(
        args.model, config=cfg, ignore_mismatched_sizes=True, **kwargs)
    model.to(args.device)
    return proc, model


def move_inputs(inputs, device):
    import torch
    out = {}
    for k, v in inputs.items():
        if torch.is_tensor(v):
            out[k] = v.to(device)
        else:
            out[k] = [t.to(device) for t in v]
    return out


def load_checkpoint(ckpt_path, device, size=512, num_labels=2, offline=True):
    """加载 checkpoint, 返回 (model, proc, meta)。model 输出类别 mask(0..num_labels-1)。"""
    import torch
    ckpt = torch.load(ckpt_path, map_location=device, weights_only=False)
    if "model_state" not in ckpt:
        raise RuntimeError(f"checkpoint 缺少 model_state: {ckpt_path}")
    saved = argparse.Namespace(**{k: v for k, v in ckpt["args"].items()})
    saved.device = device
    saved.size = size
    saved.num_labels = num_labels
    proc, model = build_model(saved, offline=offline)
    model.load_state_dict(ckpt["model_state"])
    model.to(device).eval()
    return model, proc, ckpt


def infer_seg(model, proc, img_rgb, device):
    """单图语义分割, 返回原始尺寸的类别标签数组(int)。"""
    import torch
    h, w = img_rgb.shape[:2]
    inputs = proc(images=[img_rgb], return_tensors="pt")
    inputs = move_inputs(inputs, device)
    with torch.no_grad():
        out = model(**inputs)
        seg = proc.post_process_semantic_segmentation(
            out, target_sizes=[(h, w)])[0].cpu().numpy()
    return seg


# ============================ 数据库 & 关联表 ============================
def db_images(db_path):
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT sample_name, sample_number, image_path FROM sample_images "
        "ORDER BY sample_number").fetchall()
    con.close()
    return rows  # [(样本名, 编号, path)]


def db_model(db_path, name):
    """按 name 查 models 表, 返回 (model_path, config, metrics); 找不到返回 None。"""
    con = sqlite3.connect(db_path)
    r = con.execute(
        "SELECT model_path, config_json, metrics_json FROM models WHERE name=?",
        (name,)).fetchone()
    con.close()
    if not r:
        return None
    return (r[0],
            json.loads(r[1]) if r[1] else {},
            json.loads(r[2]) if r[2] else {})


def db_model_names(db_path):
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT name, COALESCE(task,''), COALESCE(model_path,'') FROM models "
        "ORDER BY name").fetchall()
    con.close()
    return rows  # [(name, task, model_path)]


def _num_frac(series):
    v = series.dropna().astype(str).str.strip()
    if len(v) == 0:
        return 0.0
    return sum(s.replace(".", "", 1).isdigit() for s in v) / len(v)


def _pick_col(df, *, key_hints=(), want="id"):
    """在表头里选列：`id`=个体标识列（含「号/个体/编号/ID」，且值多为非数字，排除样本列）；
    `sample`=样本编号列（含「测序/样本/sample」）。返回列名或 None。不限定物种/用词。"""
    cols = list(df.columns)
    def has(c, kws): return any(k in str(c) for k in kws)
    if want == "sample":
        cands = [c for c in cols if has(c, ("测序", "样本", "sample"))]
        return cands[0] if cands else None
    cands = [c for c in cols
             if (has(c, ("号", "个体", "编号")) or str(c).upper() in ("ID", "ANIMAL_ID"))
             and not has(c, ("测序", "样本", "sample"))]
    if not cands:
        return None
    # 标识列的值多为非数字（如 A13）；越像行号（纯数字）越不可能是标识列
    return min(cands, key=lambda c: _num_frac(df[c]))


def parse_xls(corr_xls, nir_xls):
    """样本编号->个体号 (sample2id), 个体号->参照值 (id2ref, 缺省为近红外 IMF%)。失败时返回空映射。

    列名按关键词启发式匹配, 以兼容不同来源的关联表（不限定物种/术语）：
      · 个体号列: 含「号 / 个体 / 编号 / ID」，取非数字值比例最高者（排除样本列）
      · 样本编号列: 含「测序 / 样本 / sample」
      · 参照值列: 含「肌内脂肪 / IMF / 近红外 / 参照」
    """
    sample2id, id2ref = {}, {}
    try:
        corr = pd.read_excel(corr_xls)
        col_et = _pick_col(corr, want="sample")
        col_id = _pick_col(corr, want="id")
        if col_id is not None and col_et is not None and col_id != col_et:
            t = corr[[col_id, col_et]].dropna()
            t[col_et] = t[col_et].astype(str).str.strip().str.lower()
            t[col_id] = t[col_id].astype(str).str.strip().str.upper()
            sample2id = dict(zip(t[col_et], t[col_id]))
    except Exception as e:
        print(f"[warn] 对应表解析失败: {e}")
    try:
        nir = pd.read_excel(nir_xls)
        key = _pick_col(nir, want="id")
        ref = None
        for c in nir.columns:
            s = str(c)
            if "肌内脂肪" in s or "IMF" in s.upper() or "近红外" in s or "参照" in s:
                ref = c
                break
        if key is not None and ref is not None and key != ref:
            nir[key] = nir[key].astype(str).str.strip().str.upper()
            id2ref = dict(zip(nir[key], pd.to_numeric(nir[ref], errors="coerce")))
    except Exception as e:
        print(f"[warn] 参照值表解析失败: {e}")
    return sample2id, id2ref


def read_fam(fam_path):
    """返回按 fam 行序排列的 IID 列表。"""
    iids = []
    with open(fam_path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                iids.append(parts[1])
    return iids
