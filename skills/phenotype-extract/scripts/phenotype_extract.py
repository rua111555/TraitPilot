# -*- coding: utf-8 -*-
"""
phenotype_extract.py —— 通用表型提取框架（感知数据库模型 + phenotypes/ 插件代码）

加了"表型"功能, 由此脚本 + phenotypes/<新表型>.py 插件提供, 无需新增 SKILL。

两种运行模式:
  1) 列出/建议  运行时不带 --pheno → 展示数据库 models 表与 phenotypes/ 代码插件的就绪情况,
                 并给出建议提取哪个表型(通常为主题中唯一就绪者)。
  2) 提取表型   python phenotype_extract.py --pheno IMF  按所选插件完成:
               database::sample_images(图像) → 加载模型推理 → 插件 compute → CSV → GWAS 两种格式
               {outdir}/{task.name}/ 下生成:
                 {trait_lower}_phenotype.csv                 逐样本明细
                 {trait_lower}_phenotype_gwas_plink.txt      FID IID pheno, 仅含预测样本
                 {trait_lower}_phenotype_gwas_gapit.csv      Taxa,trait (GAPIT)

新增表型步骤(参考 phenotypes/imf.py, ~30 行):
  1) 在 scripts/phenotypes/ 新增 <name>.py, 定义 TASK dict(含 compute), 类标签按各自模型语义;
  2) 把对应模型写入 database 的 models 表(参考 imf_mask2former 记录);
  3) 无需改本脚本。--list 会自动发现并列出该表型。

用法(conda 环境 Phenotype):
    conda run -n Phenotype python phenotype_extract.py            # 列出+建议
    conda run -n Phenotype python phenotype_extract.py --pheno IMF
    conda run -n Phenotype python phenotype_extract.py --pheno IMF --limit 3
"""
import os
import json
import time
import argparse

import numpy as np
import cv2
import pandas as pd

from phenotypes import load_tasks, get_task
from phenotypes import _common as C


def suggest(db_path):
    """感知数据库 models 表与 phenotypes/ 代码插件, 打印就绪情况并给出建议。返回建议的表型名。"""
    db_models = C.db_model_names(db_path)
    db_names = {n for n, _, _ in db_models}
    tasks = load_tasks()

    print("=" * 74)
    print("可用表型插件(phenotypes/ 代码) 与 数据库(models 表) 匹配情况")
    print("=" * 74)
    print(f"{'表型':<8}{'模型就绪':<10}{'状态':<22}说明")
    print("-" * 74)
    ready = []
    for t in tasks:
        need = [n for n in t.get("model_names", [])]
        missing = [n for n in need if n not in db_names]
        col = "模型:%s" % ",".join(need)
        status = ("已就绪" if not missing else "缺模型: %s" % ",".join(missing))
        mark = "●" if not missing else "○"
        ready_tag = not missing
        print(f"{t['name']:<8}{col:<10}{mark} {status:<20}{t.get('title','')}")
        if ready_tag:
            ready.append(t["name"])
    print("-" * 74)
    # 数据库里有、但没有代码插件的模型
    code_covered = {n for t in tasks for n in t.get("model_names", [])}
    for n, task, path in db_models:
        if n not in code_covered:
            print(f"[提示] 数据库有模型但无提取代码插件: {n} — {task or '(无描述)'}")
    # 汇总建议
    print("=" * 74)
    if ready:
        tip = "、".join(ready)
        print(f"建议提取表型: {tip}   →  传 --pheno {ready[0]} 执行")
        return ready[0]
    print("暂无就绪表型。若数据库已有模型: 请在 phenotypes/ 补对应代码插件; "
          "若有代码插件: 请在 models 表注册模型。")
    return None


def main():
    ap = argparse.ArgumentParser(description="通用表型提取: DB图片→模型→CSV→GWAS表型")
    ap.add_argument("--pheno", default=None,
                    help="要提取的表型名(phenotypes/ 插件中的 name)。不带则列出并建议")
    ap.add_argument("--db", default="/data1/chenyx/Pig_geno/pipeline.db")
    ap.add_argument("--outdir", default="/data1/chenyx/Pig_geno/phenotype")
    ap.add_argument("--fam", default="/data1/chenyx/Pig_geno/04_snp_indel/et277.fm.snp.fam",
                    help=".fam 仅作 PLINK 文件样本排序基准(取其在 fam 的相对行序); 缺失则按 et 号排序")
    ap.add_argument("--nir-xls", default="/data1/chenyx/Pig_image/肌内脂肪含量_近红外测定.xls")
    ap.add_argument("--corr-xls",
                    default="/data1/chenyx/Pig_image/0-重要-测序数据编号与屠宰耳号对应情况.xls")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--size", type=int, default=512, help="推理输入分辨率(可用 ckpt 自带值)")
    ap.add_argument("--num-labels", type=int, default=2,
                    help="模型类别数(分割mask类标签数, 与模型训练一致)")
    ap.add_argument("--thresh", type=int, default=30, help="黑底板抠肉 ROI 的灰度阈值(传给插件)")
    ap.add_argument("--limit", type=int, default=0, help=">0 时只处理前 N 个样本(调试)")
    ap.add_argument("--save-masks", action="store_true",
                    help="同时把预测 mask 存到 {outdir}/{pheno}/masks/")
    ap.add_argument("--allow-download", action="store_true",
                    help="允许联网下载骨干权重; 默认离线用本地 HF 缓存")
    args = ap.parse_args()

    if not args.pheno:
        suggest(args.db)
        return

    task = get_task(args.pheno)
    if not task:
        print(f"[err] 未知表型 '{args.pheno}'。可用表型: "
              f"{', '.join(t['name'] for t in load_tasks())}")
        suggest(args.db)
        return

    if not args.allow_download:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")

    outdir = os.path.join(args.outdir, task["name"])
    os.makedirs(outdir, exist_ok=True)
    t0_all = time.time()

    # ---- 模型: 从 DB models 表按插件 model_names 取第一个可用 ----
    ckpt_path = None
    for mname in task.get("model_names", []):
        rec = C.db_model(args.db, mname)
        if rec:
            ckpt_path = rec[0]
            print(f"[model] 使用 DB models.{mname} -> {ckpt_path}")
            break
    if not ckpt_path:
        print(f"[err] 表型 '{task['name']}' 所需的模型在数据库(models 表)未找到: "
              f"{task.get('model_names')}。请先注册模型。")
        return

    model, proc, ckpt = C.load_checkpoint(
        ckpt_path, args.device, args.size,
        num_labels=args.num_labels, offline=not args.allow_download)
    print(f"[model] 已加载 (ckpt test_mIoU={ckpt.get('test_miou')}, epoch={ckpt.get('epoch')})")

    # ---- 样本 & 关联表 ----
    images = C.db_images(args.db)
    if args.limit > 0:
        images = images[:args.limit]
    print(f"[data ] 待处理 {len(images)} 个样本 (来自 {args.db}::sample_images)")
    et2pig, pig2imf = C.parse_xls(args.corr_xls, args.nir_xls)
    print(f"[xls  ] et->猪号 {len(et2pig)} 条, 猪->NIR {len(pig2imf)} 条")
    mappings = {"et2pig": et2pig, "pig2imf": pig2imf}

    # ---- 逐张推理 + 插件 compute ----
    trait, pcol = task["trait"], task.get("pheno_col", task["trait"])
    mask_encode = task.get("mask_encode")
    rows = []
    masks_dir = os.path.join(outdir, "masks") if args.save_masks else None
    if masks_dir:
        os.makedirs(masks_dir, exist_ok=True)
    okn = 0
    for idx, (et, num, path) in enumerate(images, 1):
        t0 = time.time()
        r = {"Taxa": et or f"et{num}",
             "猪号": et2pig.get((et or f"et{num}").lower(), ""),
             "图像文件": os.path.basename(path) if path else "",
             pcol: np.nan}
        ok, err = False, ""
        if not path or not os.path.isfile(path):
            err = "缺图"
        else:
            img = cv2.imread(path)
            if img is None:
                err = "图读取失败"
            else:
                try:
                    seg = C.infer_seg(model, proc, cv2.cvtColor(img, cv2.COLOR_BGR2RGB),
                                      args.device)
                    ctx = {"img": img, "seg": seg, "et": r["Taxa"],
                           "thresh": args.thresh, "mappings": mappings,
                           "device": args.device, "args": args}
                    value, extra = task["compute"](ctx)
                    r[pcol] = round(float(value), 4)
                    r.update({k: v for k, v in extra.items() if k != pcol})
                    if masks_dir and mask_encode:
                        cv2.imwrite(os.path.join(masks_dir, f"{num}_mask.png"),
                                    mask_encode(seg))
                    ok = True
                except Exception as e:
                    err = f"compute/infer失败: {e}"
        note = (f"{time.time()-t0:.2f}s" if ok else ("err:" + err))
        r["状态"] = "ok" if ok else f"fail:{err}"
        rows.append(r)
        print(f"[{idx:>3}/{len(images)}] {r['Taxa']:>6} {note}  {pcol}="
              f"{r[pcol]:.2f}%", flush=True)

    df = pd.DataFrame(rows)
    okn = sum(1 for r in rows if r["状态"] == "ok")
    csv_path = os.path.join(outdir, f"{trait.lower()}_phenotype.csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n[out1] 明细CSV : {csv_path}  ({len(df)} 行)")

    # ---- GWAS-PLINK/GEMMA: 仅含模型预测到的样本, 不做缺失填充 ----
    pred = {r["Taxa"]: r[pcol] for r in rows
            if r["状态"] == "ok" and r[pcol] == r[pcol]}
    if args.fam and os.path.isfile(args.fam):
        fam_order = C.read_fam(args.fam)
        order = [iid for iid in fam_order if iid in pred]
    else:
        order = sorted(pred)
    plink_path = os.path.join(outdir, f"{trait.lower()}_phenotype_gwas_plink.txt")
    with open(plink_path, "w") as f:
        for iid in order:
            f.write(f"{iid} {iid} {pred[iid]}\n")
    print(f"[out2] GWAS-PLINK: {plink_path}  ({len(order)} 行, 仅含模型预测到的样本)")

    # ---- GWAS-GAPIT: Taxa,trait ----
    g = df[["Taxa", pcol]].dropna(subset=[pcol])
    g = g.rename(columns={pcol: trait}).sort_values("Taxa").reset_index(drop=True)
    gapit_path = os.path.join(outdir, f"{trait.lower()}_phenotype_gwas_gapit.csv")
    g.to_csv(gapit_path, index=False, encoding="utf-8-sig")
    print(f"[out3] GWAS-GAPIT: {gapit_path}  ({len(g)} 行)")

    # ---- 汇总 ----
    print("\n========== 汇总 ==========")
    print(f"总耗时 {time.time()-t0_all:.0f}s, 平均 {(time.time()-t0_all)/max(len(images),1):.2f}s/样本")
    print(f"成功 {okn}/{len(images)}   表型列: {pcol}")
    has = df[pcol].dropna()
    if len(has):
        print(f"{pcol}: mean={has.mean():.3f} std={has.std():.3f} "
              f"min={has.min():.3f} max={has.max():.3f}")
    ref = [c for c in df.columns if c.endswith("NIR_%") or c.endswith("_ref")]
    if ref:
        a = df[ref[0]].astype(float).values
        m = ~np.isnan(df[pcol].values) & ~np.isnan(a)
        if m.sum() >= 3:
            r = np.corrcoef(df[pcol].values[m], a[m])[0, 1]
            print(f"对照({ref[0]}): {m.sum()} 条, 预测 vs 参照 r={r:.3f}")


if __name__ == "__main__":
    main()
