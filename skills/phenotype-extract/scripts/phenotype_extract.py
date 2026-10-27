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
from typing import NamedTuple

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


class GwasExports(NamedTuple):
    """两个 GWAS 导出文件共用的样本集、顺序与 FID 归属。

    抽成具名字段而不是裸元组：这里每一个字段都对应一个**实测踩过**的静默错误，
    调用方少看一个就会漏掉一类（尤其 ``fid_source`` —— 它是"我们现在到底知不知道
    真实 FID"的唯一凭据，丢了它 PLINK 匹配不上任何个体而退出码仍是 0）。
    """
    pred: dict          # iid -> 表型值（只含成功样本）
    order: list         # iid 列表：fam 行序在前，fam 外的追加在后（GAPIT 与 PLINK 共用）
    not_in_fam: list    # 有值但不在 fam 里的 iid（已保留，排在末尾）
    plink_rows: list    # [(fid, iid), ...] —— PLINK 导出按此逐行写
    fid_source: str     # "fam" = FID 取自 fam；"assumed-iid" = 无 fam，退化为 FID=IID
    dup_fid_iids: list  # 同一 iid 在 fam 里对应**多个不同 FID** 的 iid（见下方说明）


def build_gwas_exports(rows, pcol, fam_path):
    """决定两个 GWAS 导出文件共用的样本集、顺序与 FID。返回 :class:`GwasExports`。

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

    **FID 必须取自 fam（第二轮审计的 critical）**：PLINK/GEMMA 按 ``(FID, IID)``
    **二元组**匹配表型。早期这里只拿到 IID 列，导出时把 FID 硬编码成 IID
    （``f"{iid} {iid} {value}"``）—— 只要 fam 的 FID ≠ IID（多场次/多群体合并的
    fam 是常态），写出的每一行都匹配不上个体。实测（真 plink）：``farmA et3`` 的
    fam 配 ``et3 et3 3.3757`` 的表型文件，plink 报 ``0 phenotype values present
    after --pheno`` + ``Note: No phenotypes present.``，**退出码 0、且不产出结果
    文件**（只有一个 .log）；改成 ``farmA et3 3.3757`` 后同样退出码 0，但
    ``3 phenotype values present`` 并产出 .qassoc。两次退出码一模一样 ——
    整个队列的表型被静默丢弃，没有任何一步会失败。

    故本函数返回 ``plink_rows``（成对的 FID/IID），**没有 fam 时**退化为
    ``FID=IID`` 并把这件事记在 ``fid_source`` 里，由调用方**明说**该假设
    （而不是让它看起来像一次正常的成功导出）。

    另一处实测崩溃（同一轮审计）：fam 里同一个 IID 出现两次（跨场次合并 fam 时
    非常常见）会让旧的不变量断言 —— ``sorted(order) != sorted(pred)`` 用排序后的
    **列表**比较，重复项使长度不等 —— 在门禁之后抛 AssertionError 裸崩，
    磁盘上两个产物都不存在，而 ``*_verify.json`` 却写着 ``blocked=false``。
    现在：按 ``(FID, IID)`` **成对**去重，同一个 IID 落在多个 FID 下时**两条都写**
    （PLINK 会各自匹配到对应个体），同时把该 IID 记进 ``dup_fid_iids`` 让调用方
    警告 —— 而不是静默挑一个，也不是崩。
    """
    pred = {r["Taxa"]: r[pcol] for r in rows
            if r["状态"] == "ok" and r[pcol] == r[pcol]}
    not_in_fam = []
    plink_rows = []
    dup_fid_iids = []
    fid_source = "assumed-iid"
    if fam_path and os.path.isfile(fam_path):
        fam_pairs = C.read_fam(fam_path)          # [(fid, iid), ...]
        fam_iids = [iid for _, iid in fam_pairs]
        not_in_fam = sorted(set(pred) - set(fam_iids))
        seen_pairs = set()
        iid_fids = {}
        for fid, iid in fam_pairs:
            if iid not in pred or (fid, iid) in seen_pairs:
                continue                           # 没预测到 / 完全重复的 fam 行
            seen_pairs.add((fid, iid))
            iid_fids.setdefault(iid, []).append(fid)
            plink_rows.append((fid, iid))
        fid_source = "fam"
        dup_fid_iids = sorted(i for i, fids in iid_fids.items() if len(fids) > 1)
        fam_used = [iid for _, iid in plink_rows]
    else:
        fam_used = None
    order = (fam_used + not_in_fam) if fam_used is not None else sorted(pred)

    if fam_used is None:
        # 无 fam：FID 无从得知，退化为 FID=IID（调用方必须把该假设说出来）
        plink_rows = [(iid, iid) for iid in order]
    else:
        # fam 外的样本（有预测值、但 fam 里没有）：**也要写进 PLINK**，否则两个
        # 导出的样本集又分歧了 —— 那正是 A4 缺陷。但它们的 FID 无从得知（fam 里
        # 没有它们），只能退化为 FID=IID，并由调用方把这点说出来（见 fid_source
        # 与 not_in_fam 的两条警告）。**不变量在第一次运行时就抓到了这个漏写** ——
        # 当时 order 含它们而 plink_rows 不含，本函数尾部当场抛错。
        plink_rows += [(iid, iid) for iid in not_in_fam]

    # 不变量：两个导出的样本**集合**必须一致（顺序可以不同，PLINK 要按 fam 序）。
    # 用 set 比较而不是排序列表 —— 后者在 fam 含重复 IID 时误报（实测裸崩）。
    if set(order) != set(pred):
        raise AssertionError(
            f"内部错误：PLINK 与 GAPIT 的样本集不一致 "
            f"({sorted(set(order) ^ set(pred))}) —— 导出路径被改坏了")
    if {iid for _, iid in plink_rows} != set(order):
        raise AssertionError(
            "内部错误：PLINK 行与 order 的样本集不一致 —— 导出路径被改坏了")
    return GwasExports(pred, order, not_in_fam, plink_rows, fid_source, dup_fid_iids)


def remove_stale_gwas_exports(paths):
    """删掉上一轮留下的 GWAS 产物。返回 ``(删掉的文件名, [(文件名, 原因), ...])``。

    为什么需要它（第二轮审计 critical）：门禁 hard 拦截时早期实现只是 ``sys.exit(1)``，
    **不动磁盘**。若上一轮跑成功过，那两个 GWAS 文件就还在原地 —— 下游
    （`gwas-gemma` / `gwas-pipeline` 直接 `--pheno` 吃它们）照样读得到、rc=0、
    关联分析照跑，读到的却是**上一轮的旧值**；而同一目录下 `*_verify.json` 写着
    ``blocked=true``、明细 CSV 也是新的。rc=1 与磁盘内容互相矛盾，没有任何机制会发现。

    SKILL.md 承诺的是"不写出 GWAS 表型文件"——用户理解成"这次没产出，别用"；
    实现做的却是"这次不写新的"。对**已存在的旧文件**，这两句话完全不同。
    门禁的整个存在意义是"这一批结果值不可信"，那这一批的产物当然也不能留着被消费。

    抽成纯函数是为了可测：内联在 ``main()`` 的 abort 分支里就只能靠跑完整条
    推理管线才碰得到（需要模型与图片），而那正是本项目的缺陷能活下来的原因。
    """
    removed, failed = [], []
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            os.remove(p)
            removed.append(os.path.basename(p))
        except OSError as e:
            # 删不掉**不算静默通过**：必须明说，否则下游仍可能吃到旧值
            failed.append((os.path.basename(p), str(e)))
    return removed, failed


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
        # ⚠️ 必须 exit 非 0。早期这里是裸 `return`（进程退出码 **0**）——于是编排层
        #    按退出码判定时，会把"参数写错/表型名不存在"当成**成功**：同一脚本里
        #    三种失败（门禁拦下=rc 1、缺模型=rc 0、未知表型=rc 0）无法区分，而
        #    "rc=0 却没有 .done、也没有任何产物"最容易被读成"跑过了但没产出"。
        sys.exit(2)

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
        # ⚠️ 同上一处：裸 `return` 给出 rc=0，编排层会当成成功。
        #    而且 outdir 已在上方 makedirs 创建 —— 磁盘上留下一个**空目录**，
        #    更像"跑过了但没产出"，只有非 0 退出码能把它与真实成功区分开。
        sys.exit(2)

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

    # ---- 本轮的两个 GWAS 导出路径（**开工前**就定好，供两处使用）----
    # ⚠️ 之所以提到这里：门禁 hard 拦截时必须**删掉**这两个文件（见下方 abort 分支）。
    #    早期实现只是 `sys.exit(1)` 而不动磁盘 —— 于是**上一轮跑成功留下的**那两个
    #    文件原地不动。下游（gwas-gemma / gwas-pipeline 直接 `--pheno` 吃它们）
    #    照样读得到、rc=0、关联分析照跑，读到的却是上一轮的旧值；而同一目录下
    #    `*_verify.json` 写着 blocked=true、明细 CSV 也是新的。**rc=1 与磁盘内容
    #    互相矛盾，且没有任何机制会发现**。SKILL.md 承诺的是"不写出 GWAS 表型文件"，
    #    用户的理解是"这次没产出，别用"；实现做的却是"这次不写新的"——
    #    对**已存在的旧文件**这两句话完全不同。
    plink_path = os.path.join(outdir, f"{trait.lower()}_phenotype_gwas_plink.txt")
    gapit_path = os.path.join(outdir, f"{trait.lower()}_phenotype_gwas_gapit.csv")

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
        # ---- 删掉**上一轮**留下的 GWAS 产物 ----
        # 只说"本轮不写出"是不够的：这两个文件是下游的唯一入口，若上一轮跑成功过，
        # 它们就还在原地，下游照吃、rc=0、读到的是**旧值**。而门禁的整个存在意义
        # 就是"这一批结果值不可信"—— 那一批的产物当然也不能留着被消费。
        # 实测：上一轮产物在场时按文档跑下游命令，读到的正是上一轮的数。
        # 删不掉（权限等）不算静默通过：明说要人工处理，否则下游仍可能吃到旧值。
        _removed, _failed = remove_stale_gwas_exports([plink_path, gapit_path])
        if _removed:
            print(f"[abort] 已删除上一轮的 GWAS 产物，避免下游读到旧值："
                  f"{'、'.join(_removed)}")
        for _name, _why in _failed:
            print(f"[warn] 陈旧产物删不掉，**下游仍可能读到它**：{_name}（{_why}）")
        sys.exit(1)

    # ---- GWAS 两个导出：样本集、顺序与 FID 由 build_gwas_exports() 统一决定 ----
    # ⚠️ 这段的核心不变量：**两个导出文件的样本集必须完全相同**（PLINK 的 FID/IID
    #    行 与 GAPIT 的 Taxa 行）。它们是下游 GWAS 的唯一入口，若一个含 s4、另一个
    #    不含，同一批表型就有了两份互相矛盾的样本清单，用哪份取决于跑的是 PLINK/GEMMA
    #    还是 GAPIT —— 而没人会去比对两个文件。门禁的 sample_uniqueness 查的是
    #    「同一 Taxa 出现多行」，查不到这个（实测：fam 少一个样本时
    #    PLINK 3 行 / GAPIT 4 行，旧版本两个都照写、rc=0）。
    #    实现细节与理由见 build_gwas_exports() 的 docstring（抽成纯函数是为了可测）。
    ex = build_gwas_exports(rows, pcol, args.fam)

    if ex.not_in_fam:
        # 不拦（有的个体本就没有基因型数据，属正常），但必须说清楚代价：
        # GEMMA 的 -p 是**按行序**对应的，这些样本被追加在 fam 序之后，
        # 位置与基因型文件的行并不对应 —— 靠行序对齐时会错位。
        print(f"\n[warn] 有 {len(ex.not_in_fam)} 个样本算出了表型但**不在 --fam 里**: "
              f"{ex.not_in_fam[:8]}"
              f"\n       它们已写入两个 GWAS 文件（保持两文件样本集一致），"
              f"但排在 fam 行序**之后**。"
              f"\n       ⚠ GEMMA 的 -p 按行序对齐基因型，这些样本的位置与 .fam 行不对应 ——"
              f"\n         若要用它们做关联分析，先把基因型文件按同一顺序子集化，或核对 --fam。",
              flush=True)

    # ---- 样本集守恒：输出样本 ⊆ 基因型 fam 样本集（第二轮审计新增的判据）----
    # 这条判据的**必要性**来自一次实测：表型文件的行与 fam 的个体对不上时，
    # PLINK 既不报错也不产出结果文件（退出码 0），整个队列的表型被静默丢弃。
    # 上游唯一的防线就是这个"集合包含"关系 —— 它必须被明说，而不是靠"两边看起来
    # 都是样本号"来假定。
    if ex.fid_source == "assumed-iid":
        print("\n[warn] 未提供 --fam（或文件不存在）：PLINK 导出**假定 FID = IID**。"
              "\n       若基因型文件的 FID 与 IID 不同（多场次/多群体合并的 fam 是常态），"
              "\n       这些行**匹配不上任何个体** —— plink 会打印「0 phenotype values"
              "\n       present」并**退出码 0、不产出结果文件**，静默丢弃整个队列。"
              "\n       确认无误就把 --fam 指到基因型 .fam，或核对 FID 列。", flush=True)
    if ex.dup_fid_iids:
        print(f"\n[warn] 有 {len(ex.dup_fid_iids)} 个样本号在 fam 里对应**多个 FID**: "
              f"{ex.dup_fid_iids[:8]}"
              f"\n       PLINK 导出已按 (FID, IID) 成对写出（每对一行，各自匹配对应个体）——"
              f"\n       这是刻意的：同一个样本号在不同 FID 下就是不同的个体，合并会丢数据。"
              f"\n       但请核对这确实是你要的（而不是关联表把两个个体混成了一个编号）。",
              flush=True)

    with open(plink_path, "w") as f:
        for fid, iid in ex.plink_rows:
            f.write(f"{fid} {iid} {ex.pred[iid]}\n")
    print(f"[out2] GWAS-PLINK: {plink_path}  ({len(ex.plink_rows)} 行, "
          f"仅含模型预测到的样本；FID 来源: "
          f"{'fam' if ex.fid_source == 'fam' else '**假定 FID=IID**'})")
    if args.fam and os.path.isfile(args.fam):
        print(f"       行序: 与 {os.path.basename(args.fam)} 的行序一致"
              + (f"（另有 {len(ex.not_in_fam)} 个追加在末尾）" if ex.not_in_fam else ""))

    # ---- GWAS-GAPIT: Taxa,trait ----
    # 与 PLINK **同源**：都用 build_gwas_exports() 返回的 order/pred，
    # 而不是从 df 独立取一遍（那样两条路径就会产出不同的样本集）。
    g = pd.DataFrame({"Taxa": ex.order, trait: [ex.pred[i] for i in ex.order]})
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
