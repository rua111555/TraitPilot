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
import sys

# 数据根目录：由环境变量提供（便于换机器/换物种）；缺省为通用的 $HOME/genomics_data 布局。
GENO_DIR  = os.environ.get("GENO_DIR",  os.path.join(os.path.expanduser("~"), "genomics_data"))
IMAGE_DIR = os.environ.get("IMAGE_DIR", os.path.join(GENO_DIR, "images"))
DEF_DB     = os.environ.get("PIPELINE_DB",   os.path.join(GENO_DIR, "pipeline.db"))
DEF_OUTDIR = os.path.join(GENO_DIR, "phenotype")

import numpy as np
import cv2
import pandas as pd

from phenotypes import load_tasks, get_task
from phenotypes import _common as C
from validate import validate_batch


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


def build_gwas_exports(rows, pcol, fam_path):
    """决定两个 GWAS 导出文件共用的样本集与顺序。返回 (pred, order, not_in_fam)。

    **这是 A4 缺陷的修法核心**，抽成纯函数是为了能被测试直接覆盖（原先内联在
    ``main()`` 里，只能靠跑整条推理管线才碰得到 —— 而那条路径需要模型与图片）。

    背景：早期版本两个导出各走各的构造方式 ——

      · PLINK: ``pred = {r["Taxa"]: r[pcol] ...}`` 先按键去重，再用
        ``[iid for iid in fam_order if iid in pred]`` 过滤
      · GAPIT: ``df[["Taxa", pcol]].dropna()`` 保留**全部**行

    于是只要 ``--fam`` 里缺某个样本，PLINK 少一行、GAPIT 多一行，两个文件被
    同时当作权威产物写出，且**没有任何机制会发现**（实测复现）。下游用哪个文件
    取决于跑的是 PLINK/GEMMA 还是 GAPIT，两份样本清单互相矛盾。

    修法不是"加一条检查来发现分歧"，而是**让两边由同一个集合构造**：本函数返回
    唯一的 ``order``，两处导出都用它，分歧在构造上就不可能发生。

    ``--fam`` 的语义按 SKILL.md 的契约是**排序基准**
    （"仅作 PLINK 文件样本排序基准(取其在 fam 的相对行序)"），**不是**成员过滤器：

      · fam 里有、本次没预测到的样本 → 合理地不出现（它们本来就没有表型值）
      · 有预测值、但不在 fam 里的样本 → **保留**，追加在 fam 序之后
        （早期把它们静默丢掉，等于拿排序基准当了过滤器 —— 用户以为只是换了个
        顺序，实际丢了样本）

    追加上去的样本排在末尾，与基因型文件的行不再对应 —— 这一点由调用方
    打印警告说明（GEMMA 的 ``-p`` 是按行序对齐的）。返回 ``not_in_fam`` 就是为了让它能说。
    """
    pred = {r["Taxa"]: r[pcol] for r in rows
            if r["状态"] == "ok" and r[pcol] == r[pcol]}
    not_in_fam = []
    fam_used = None
    if fam_path and os.path.isfile(fam_path):
        fam_order = C.read_fam(fam_path)
        fam_used = [iid for iid in fam_order if iid in pred]
        not_in_fam = sorted(set(pred) - set(fam_order))
    order = (fam_used + not_in_fam) if fam_used is not None else sorted(pred)

    # 不变量：两个导出的样本**集合**必须一致（顺序可以不同，PLINK 要按 fam 序）。
    # 这条断言防的是**将来**有人只改一处导出而又引入分歧。
    if sorted(order) != sorted(pred):
        raise AssertionError(
            f"内部错误：PLINK 与 GAPIT 的样本集不一致 "
            f"({sorted(set(order) ^ set(pred))}) —— 导出路径被改坏了")
    return pred, order, not_in_fam


def main():
    ap = argparse.ArgumentParser(description="通用表型提取: DB图片→模型→CSV→GWAS表型")
    ap.add_argument("--pheno", default=None,
                    help="要提取的表型名(phenotypes/ 插件中的 name)。不带则列出并建议")
    ap.add_argument("--db", default=DEF_DB,
                    help=f"SQLite 数据库(默认 $PIPELINE_DB 或 {DEF_DB})")
    ap.add_argument("--outdir", default=DEF_OUTDIR,
                    help=f"输出根目录(默认 $GENO_DIR/phenotype, 即 {DEF_OUTDIR})")
    ap.add_argument("--fam", default=os.environ.get("FAM_PATH", ""),
                    help=".fam 仅作 PLINK 文件样本排序基准(取其在 fam 的相对行序); "
                         "缺省按样本号自然排序；需要与基因型文件对齐时用 --fam 或 $FAM_PATH")
    ap.add_argument("--nir-xls", default=os.environ.get("NIR_XLS", ""),
                    help="参照真值表(默认 $NIR_XLS；不指定则明细表不含参照列)")
    ap.add_argument("--corr-xls", default=os.environ.get("CORR_XLS", ""),
                    help="样本编号→个体号 关联表(默认 $CORR_XLS)")
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
    # ---- 出口校验门禁 ----
    ap.add_argument("--max-fail-rate", type=float, default=0.05,
                    help="失败样本占比上限(默认 0.05=5%%), 超出则不写出 GWAS 表型文件")
    ap.add_argument("--min-samples", type=int, default=1,
                    help="可产出样本数下限(默认 1), 低于则不写出")
    ap.add_argument("--min-nir-r", type=float, default=None,
                    help="预测 vs 参照真值的最小相关系数(默认不判; 建议 0.6)")
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

    # ---- --fam 的可用性在**开工前**就交代清楚 ----
    # ⚠️ 早期版本把 `args.fam and os.path.isfile(args.fam)` 的判断推迟到导出那一刻，
    #    于是「传了 --fam 但路径写错 / 文件被删」会**静默**退回样本号排序 —— 用户以为
    #    已按基因型文件对齐，实际没有，而 GWAS 里样本顺序错位是个不会报错的错误。
    #    这里在推理开始前（昂贵步骤之前）就告知，让用户能中断重跑。
    if args.fam and not os.path.isfile(args.fam):
        print(f"[warn] --fam 指定的文件不存在: {args.fam}\n"
              f"       PLINK 导出将**退回按样本号自然排序**，而非按该 .fam 的行序 ——\n"
              f"       若下游要靠行序与基因型文件对齐，这个顺序很可能是错的。\n"
              f"       检查路径，或用 $FAM_PATH 环境变量指定。", flush=True)

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
    sample2id, id2ref = C.parse_xls(args.corr_xls, args.nir_xls)
    print(f"[xls  ] 样本->个体 {len(sample2id)} 条, 个体->参照 {len(id2ref)} 条")
    mappings = {"sample2id": sample2id, "id2ref": id2ref}

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
        sid = et or f"sample{num}"
        r = {"Taxa": sid,
             "个体号": sample2id.get(sid.lower(), ""),
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
                    ctx = {"img": img, "seg": seg, "sample": r["Taxa"],
                           "thresh": args.thresh, "mappings": mappings,
                           "device": args.device, "args": args}
                    value, extra = task["compute"](ctx)
                    # 插件必须给出可用数值；NaN/Inf 一律计为失败样本，
                    # 不能留下「状态=ok 但值为 NaN」的矛盾行（那会污染后续统计）
                    fv = float(value)
                    if fv != fv or fv in (float("inf"), float("-inf")):
                        raise C.PhenotypeError(f"插件返回了非有限数值: {value!r}")
                    r[pcol] = round(fv, 4)
                    r.update({k: v for k, v in extra.items() if k != pcol})
                    if masks_dir and mask_encode:
                        cv2.imwrite(os.path.join(masks_dir, f"{num}_mask.png"),
                                    mask_encode(seg))
                    ok = True
                except C.PhenotypeError as e:
                    # 校验性失败（插件明确判定算不出可信值）——原因原样透出
                    err = str(e)
                except Exception as e:
                    err = f"compute/infer失败: {e}"
        note = (f"{time.time()-t0:.2f}s" if ok else ("err:" + err))
        r["状态"] = "ok" if ok else f"fail:{err}"
        rows.append(r)
        v = r[pcol]
        shown = f"{v:.2f}%" if v == v else "-"     # NaN（失败样本）不打印成 nan%
        print(f"[{idx:>3}/{len(images)}] {r['Taxa']:>6} {note}  {pcol}={shown}",
              flush=True)

    df = pd.DataFrame(rows)
    okn = sum(1 for r in rows if r["状态"] == "ok")
    csv_path = os.path.join(outdir, f"{trait.lower()}_phenotype.csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n[out1] 明细CSV : {csv_path}  ({len(df)} 行)")

    # ---- 出口校验门禁（明细 CSV 已落盘，失败时有据可查） ----
    # 校验不过 → 不写 GWAS 表型文件。理由：GWAS 文件是给下游消费的**唯一入口**，
    # 一旦写出，一个静默错误（如全 0 值、抠图失败）就会一路带进关联分析，
    # 事后无法从数值上分辨。明细 CSV 保留全部失败原因，供排查。
    report = validate_batch(rows, pcol, task,
                            max_fail_rate=args.max_fail_rate,
                            min_samples=args.min_samples,
                            min_nir_r=args.min_nir_r)
    print(report.render(f"表型结果校验 — {trait}"))
    # 校验报告写盘失败不应拖垮主流程（报告是辅助产物，不是判据本身）——
    # 但要说出来，不能静默吞掉（否则事后以为有报告可查）
    verify_path = os.path.join(outdir, f"{trait.lower()}_verify.json")
    try:
        with open(verify_path, "w", encoding="utf-8") as f:
            json.dump({"trait": trait, "column": pcol, "n_rows": len(df),
                       "n_ok": okn, **report.as_dict()}, f,
                      ensure_ascii=False, indent=2)
        print(f"[out4] 校验报告: {verify_path}")
    except Exception as e:                                    # noqa: BLE001
        print(f"[warn] 校验报告写盘失败（不影响判定）: {e}")

    if report.blocked:
        print(f"\n[abort] 表型文件**未**写出（明细 CSV 可供排查）："
              f"{', '.join(c.name for c in report.hard_failures)}")
        for c in report.hard_failures:
            print(f"        - {c.name}: {c.detail}")
        sys.exit(1)

    # ---- GWAS 两个导出：样本集与顺序由 build_gwas_exports() 统一决定 ----
    # ⚠️ 这段的核心不变量：**两个导出文件的样本集必须完全相同**（PLINK 的 FID/IID
    #    行 与 GAPIT 的 Taxa 行）。它们是下游 GWAS 的唯一入口，若一个含 s4、另一个
    #    不含，同一批表型就有了两份互相矛盾的样本清单，用哪份取决于跑的是 PLINK/GEMMA
    #    还是 GAPIT —— 而没人会去比对两个文件。门禁的 sample_uniqueness 查的是
    #    「同一 Taxa 出现多行」，查不到这个（实测：fam 少一个样本时
    #    PLINK 3 行 / GAPIT 4 行，旧版本两个都照写、rc=0）。
    #    实现细节与理由见 build_gwas_exports() 的 docstring（抽成纯函数是为了可测）。
    pred, order, not_in_fam = build_gwas_exports(rows, pcol, args.fam)

    if not_in_fam:
        # 不拦（有的个体本就没有基因型数据，属正常），但必须说清楚代价：
        # GEMMA 的 -p 是**按行序**对应的，这些样本被追加在 fam 序之后，
        # 位置与基因型文件的行并不对应 —— 靠行序对齐时会错位。
        print(f"\n[warn] 有 {len(not_in_fam)} 个样本算出了表型但**不在 --fam 里**: "
              f"{not_in_fam[:8]}"
              f"\n       它们已写入两个 GWAS 文件（保持两文件样本集一致），"
              f"但排在 fam 行序**之后**。"
              f"\n       ⚠ GEMMA 的 -p 按行序对齐基因型，这些样本的位置与 .fam 行不对应 ——"
              f"\n         若要用它们做关联分析，先把基因型文件按同一顺序子集化，或核对 --fam。",
              flush=True)

    plink_path = os.path.join(outdir, f"{trait.lower()}_phenotype_gwas_plink.txt")
    with open(plink_path, "w") as f:
        for iid in order:
            f.write(f"{iid} {iid} {pred[iid]}\n")
    print(f"[out2] GWAS-PLINK: {plink_path}  ({len(order)} 行, 仅含模型预测到的样本)")
    if args.fam and os.path.isfile(args.fam):
        print(f"       行序: 与 {os.path.basename(args.fam)} 的行序一致"
              + (f"（另有 {len(not_in_fam)} 个追加在末尾）" if not_in_fam else ""))

    # ---- GWAS-GAPIT: Taxa,trait ----
    # 与 PLINK **同源**：都用 build_gwas_exports() 返回的 order/pred，
    # 而不是从 df 独立取一遍（那样两条路径就会产出不同的样本集）。
    g = pd.DataFrame({"Taxa": order, trait: [pred[i] for i in order]})
    gapit_path = os.path.join(outdir, f"{trait.lower()}_phenotype_gwas_gapit.csv")
    g.to_csv(gapit_path, index=False, encoding="utf-8-sig")
    print(f"[out3] GWAS-GAPIT: {gapit_path}  ({len(g)} 行)")

    # ---- 汇总（统计量已由上面的门禁报告给出，此处只报耗时与产出） ----
    print("\n========== 汇总 ==========")
    print(f"总耗时 {time.time()-t0_all:.0f}s, 平均 {(time.time()-t0_all)/max(len(images),1):.2f}s/样本")
    print(f"成功 {okn}/{len(images)}   表型列: {pcol}")
    rc = [c for c in df.columns if c.endswith("NIR_%") or c.endswith("_ref")]
    if rc:
        a = pd.to_numeric(df[rc[0]], errors="coerce").values
        b = pd.to_numeric(df[pcol], errors="coerce").values
        m = ~np.isnan(a) & ~np.isnan(b)
        if m.sum() >= 3:
            r = np.corrcoef(b[m], a[m])[0, 1]
            print(f"对照({rc[0]}): {m.sum()} 条, 预测 vs 参照 r={r:.3f}")


if __name__ == "__main__":
    main()
