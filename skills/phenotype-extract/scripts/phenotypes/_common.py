# -*- coding: utf-8 -*-
"""phenotypes/_common.py —— 表型提取插件的共享工具（模型加载 / 抠肉ROI / 数据库 / 关联表）"""
import os
import json
import sqlite3
import argparse

import numpy as np
import pandas as pd
import cv2


# ============================ 共享工具: 抠整块肉 ROI ============================
def meat_mask(img, thresh=30, min_component=2000):
    """从黑底板照片抠掉底板, 得到整块肉的二值 mask(最大连通域 + 填洞)。img=BGR。"""
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
    return rows  # [(et号, number, path)]


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


def parse_xls(corr_xls, nir_xls):
    """et号->猪号, 猪号->近红外IMF%。失败时返回空映射。"""
    et2pig, pig2imf = {}, {}
    try:
        corr = pd.read_excel(corr_xls)
        col_pig = col_et = None
        for c in corr.columns:
            s = str(c)
            if "猪号" in s:
                col_pig = c
            elif "测序" in s or "et" in s.lower():
                col_et = c
        if col_pig and col_et:
            t = corr[[col_pig, col_et]].dropna()
            t[col_et] = t[col_et].astype(str).str.strip().str.lower()
            t[col_pig] = t[col_pig].astype(str).str.strip().str.upper()
            t = t[t[col_et].str.match(r"^et\d+$")]
            et2pig = dict(zip(t[col_et], t[col_pig]))
    except Exception as e:
        print(f"[warn] 对应表解析失败: {e}")
    try:
        nir = pd.read_excel(nir_xls)
        ear = imf = None
        for c in nir.columns:
            s = str(c)
            if "耳号" in s:
                ear = c
            elif "肌内脂肪" in s or "IMF" in s.upper():
                imf = c
        if ear and imf:
            nir[ear] = nir[ear].astype(str).str.strip().str.upper()
            pig2imf = dict(zip(nir[ear], pd.to_numeric(nir[imf], errors="coerce")))
    except Exception as e:
        print(f"[warn] NIR表解析失败: {e}")
    return et2pig, pig2imf


def read_fam(fam_path):
    """返回按 fam 行序排列的 IID 列表。"""
    iids = []
    with open(fam_path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                iids.append(parts[1])
    return iids
