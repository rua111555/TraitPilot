#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pheno_align.py —— 表型与基因型样本对齐（GWAS Stage S0.2）

要解决的问题
============
实际项目里，表型文件（用户给的 Excel/CSV，列名与个体编号由用户自己定）和基因型
数据（PLINK `.fam` / VCF 表头）的样本名**经常对不上**；对不上的原因是多种多样的：
前缀/后缀、大小写、补零、全角字符、前后空格、`FID_IID` 拼接、Excel 把 `001` 读成
`1`……而"谁对应谁"往往只存在于口述或一份手工维护的对应表里，没有生成记录。

本脚本把这件事做成**可复核、可复现**的一步：

    三方输入（表型 / 基因型样本表 / 可选映射表）→ 分档匹配 → 差异全部落盘
    → 生成各下游工具所需的**正确格式** → 等待用户确认 → 下游才允许继续

四条硬规则（用户 2026-11-02 裁定）
--------------------------------
1. **两边多出来的样本都可以接受**（取交集），但对应关系**不能乱**：
   一对多、重复、归一化碰撞一律 hard 失败，绝不自动消解。
2. 重复个体默认**拦下**；只有显式 `--dup-policy mean|first|last` 才聚合。
3. 匹配率过低（默认 < 80%）不拦，但标记 `needs_confirmation`，必须人工确认。
4. **对齐之后必须经用户确认**才能进入下一步：`approve` 写审批凭证，
   `check` 校验凭证与产物哈希（产物变了，旧审批自动失效）。

分档匹配（每一行都记录用了哪一档）
--------------------------------
    map          显式映射表（最可信）
    exact_iid    表型 ID == 基因型 IID
    exact_fid    表型 ID == FID（IID 不唯一时用）
    exact_pair   表型 ID == `FID_IID` / `FID:IID` / `FID IID`
    norm_*       按声明的规范化规则匹配（trim/全角/大小写/前后缀/补零…）
    （模糊候选只写进 unmatched 报告，**绝不自动采用**）

用法
----
    # 1) 对齐（产出报告与所有下游文件）
    python pheno_align.py analyze --pheno IMF表型.xlsx --geno gwas_sub.fam \\
        --id-col Taxa --traits IMF --normalize trim,fullwidth,casefold \\
        --outdir runs/gwas_001/pheno_align

    # 2) 把报告给用户看，用户确认后：
    python pheno_align.py approve --outdir runs/gwas_001/pheno_align --note "用户确认"

    # 3) 下游阶段/脚本开工前校验审批仍然有效
    python pheno_align.py check --outdir runs/gwas_001/pheno_align

退出码：0=无 hard 失败（可能仍需审批）；1=hard 失败；check：0=已审批且未过期。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import datetime

# 判据容器与 WGS/GWAS 门禁共用同一套 hard/warn 语义（skills/_shared/verify_core.py）
_SHARED = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "_shared")
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)
from verify_core import Report  # noqa: E402

STAGE = "00.2_pheno_align"
DEFAULT_MIN_MATCH_RATE = 0.8
APPROVAL_FILE = "approval.json"
PARAMS_FILE = "align.params.json"
REPORT_FILE = "align.report.json"


# --------------------------------------------------------------------------- #
# ID 规范化
# --------------------------------------------------------------------------- #

DEFAULT_RULES = ("trim", "fullwidth", "casefold")
LOSSY_RULES = {"digits-only", "alnum-only"}


def parse_rules(spec):
    """解析 `--normalize`：逗号分隔的规则名，可带 `=参数`。

    返回 (rules, err)。未给时用默认 `trim,fullwidth,casefold`（这三条不改字符集，
    不会把两个不同的人合成一个）；`digits-only`/`strip-prefix` 等"可能丢信息"的
    规则需要显式声明，并会触发碰撞检测。
    """
    if spec is None or spec == "":
        spec = ",".join(DEFAULT_RULES)
    rules = {"trim": False, "fullwidth": False, "casefold": False,
             "strip_prefix": [], "strip_suffix": [], "pad": None,
             "digits_only": False, "alnum_only": False, "dedup_suffix": False,
             "collapse_space": False}
    for tok in str(spec).split(","):
        tok = tok.strip()
        if not tok:
            continue
        name, _, val = tok.partition("=")
        name = name.strip().lower()
        if name == "trim":
            rules["trim"] = True
        elif name in ("fullwidth", "nfkc"):
            rules["fullwidth"] = True
        elif name in ("casefold", "lower", "upper", "casesensitive"):
            rules["casefold"] = True
        elif name in ("strip-prefix", "strip_prefix"):
            rules["strip_prefix"].append(val)
        elif name in ("strip-suffix", "strip_suffix"):
            rules["strip_suffix"].append(val)
        elif name == "pad":
            try:
                rules["pad"] = int(val)
            except ValueError:
                return None, f"--normalize 的 pad 需要整数位数，收到 {val!r}"
        elif name == "digits-only":
            rules["digits_only"] = True
        elif name == "alnum-only":
            rules["alnum_only"] = True
        elif name == "dedup-suffix":
            rules["dedup_suffix"] = True
        elif name == "collapse-space":
            rules["collapse_space"] = True
        else:
            return None, f"未知的规范化规则：{tok!r}"
    return rules, None


def rule_names(rules):
    names = []
    if rules["trim"]:
        names.append("trim")
    if rules["fullwidth"]:
        names.append("fullwidth")
    if rules["casefold"]:
        names.append("casefold")
    for p in rules["strip_prefix"]:
        names.append(f"strip-prefix={p}")
    for s in rules["strip_suffix"]:
        names.append(f"strip-suffix={s}")
    if rules["pad"] is not None:
        names.append(f"pad={rules['pad']}")
    if rules["digits_only"]:
        names.append("digits-only")
    if rules["alnum_only"]:
        names.append("alnum-only")
    if rules["dedup_suffix"]:
        names.append("dedup-suffix")
    if rules["collapse_space"]:
        names.append("collapse-space")
    return names


def normalize(raw, rules):
    """按规则把 ID 归一化。顺序固定：全角→空格→前后缀→大小写→补零→字符集。"""
    s = str(raw if raw is not None else "")
    if rules["fullwidth"]:
        s = unicodedata.normalize("NFKC", s)
    if rules["trim"]:
        s = s.strip()
    if rules["collapse_space"]:
        s = re.sub(r"\s+", " ", s)
    for p in rules["strip_prefix"]:
        if s.lower().startswith(p.lower()):
            s = s[len(p):]
            if rules["trim"]:
                s = s.strip()
    for suf in rules["strip_suffix"]:
        if s.lower().endswith(suf.lower()):
            s = s[:-len(suf)]
            if rules["trim"]:
                s = s.strip()
    if rules["dedup_suffix"]:
        # `et10_et10` → `et10`（PLINK --recode vcf 的 FID_IID 命名）
        parts = re.split(r"[_:\s]", s)
        if len(parts) == 2 and parts[0] and parts[0] == parts[1]:
            s = parts[0]
    if rules["casefold"]:
        s = s.casefold()
    # ⚠️ 先"取字符集"再"补零"：`ET-010` 与 `et10` 在 digits-only + pad=3 下
    #    都应该归一成 `010`。顺序反了会得到 `010` vs `10`，明明能对上却对不上。
    if rules["digits_only"]:
        s = re.sub(r"\D", "", s)
    elif rules["alnum_only"]:
        s = re.sub(r"[^0-9A-Za-z]", "", s)
    if rules["pad"] is not None and s.isdigit():
        s = s.zfill(rules["pad"])
    return s


# --------------------------------------------------------------------------- #
# 输入读取
# --------------------------------------------------------------------------- #

def _cell_str(v):
    """把表格单元格转成**字符串**（ID 绝不能变成数字）。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v).strip()


def _matrix_to_table(matrix, header_row):
    """把二维表转成 (header, rows)；列数不一致直接报错（列错位会静默对错人）。"""
    rows = [r for r in matrix if any(str(c).strip() for c in r)]
    if not rows:
        return None, None, "文件里没有任何数据行"
    hr = int(header_row or 0)
    if hr > 0:
        if hr > len(rows):
            return None, None, f"表头行 --header-row={hr} 超出文件行数（{len(rows)} 行）"
        header = [str(c).strip() for c in rows[hr - 1]]
        body = rows[hr:]
        first_data_line = hr + 1
    else:
        ncol = max(len(r) for r in rows)
        header = [f"col{i + 1}" for i in range(ncol)]
        body = rows
        first_data_line = 1
    for off, r in enumerate(body):
        if len(r) != len(header):
            return None, None, (f"第 {first_data_line + off} 行有 {len(r)} 列，"
                                f"表头是 {len(header)} 列 —— 列错位会让 ID 和性状错位，"
                                f"先修表再跑")
    return header, [[str(c).strip() for c in r] for r in body], None


def _read_xlsx(path, sheet, header_row):
    """xlsx 用 openpyxl 读；**不依赖 pandas**。缺 openpyxl 时给可操作的提示。"""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return None, None, ("读取 .xlsx 需要 openpyxl（Agent 环境已含）："
                            "`conda run -n Agent python -m pip install openpyxl`；"
                            "或先把表另存为 CSV/TSV 再跑")
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb[sheet] if sheet else wb.worksheets[0]
            matrix = [[_cell_str(c) for c in row]
                      for row in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
    except Exception as e:                                    # noqa: BLE001
        return None, None, f"读取 Excel 失败：{e!r}"
    return _matrix_to_table(matrix, header_row)


def _read_delimited(path, header_row):
    """csv/tsv/txt 用**标准库**读；分隔符按前若干行自动判断（逗号/制表符/分号/空白）。

    为什么不用 pandas：本 skill 是轻量预处理，不该让"读一张表"变成整个 pandas 的
    运行期依赖；而且 pandas 会把 `NA`/`N/A` 当缺失、把 `001` 读成 `1`。
    """
    import csv as _csv

    # 编码兼容：中文用户导出的 CSV 常见 GBK/GB18030。先试 UTF-8（含 BOM），再试 GB18030，
    # 最后用 replace 兜底 —— 直接把 GBK 按 UTF-8 读会得到一串替换字符，ID 静默损坏。
    try:
        data = open(path, "rb").read()
    except Exception as e:                                    # noqa: BLE001
        return None, None, f"读取表型文件失败：{e!r}"
    text, enc = None, None
    for e in ("utf-8-sig", "gb18030"):
        try:
            text, enc = data.decode(e), e
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text, enc = data.decode("utf-8", errors="replace"), "utf-8(replace)"
    raw = text.splitlines()
    if not raw:
        return None, None, f"{path} 是空文件"
    sample = "\n".join(raw[:20])
    counts = {",": sample.count(","), "\t": sample.count("\t"),
              ";": sample.count(";")}
    delim = max(counts, key=counts.get)
    if counts[delim] == 0:
        matrix = [ln.split() for ln in raw]
    else:
        matrix = [r for r in _csv.reader(raw, delimiter=delim)]
    return _matrix_to_table(matrix, header_row)


def _read_table(path, header_row=1, sheet=None):
    """读入表型表：xlsx / csv / tsv / txt。所有单元格按**字符串**保留。

    返回 (header, rows, err)：`header` 为列名列表，`rows` 为 list[list[str]]。
    ⚠️ 一律按字符串读：Excel 会把 `001` 读成 `1`、把长数字读成科学计数法。
    依赖：csv/tsv/txt 纯标准库；xlsx 需要 openpyxl（Agent 环境已含）。
    """
    if not os.path.isfile(path):
        return None, None, f"表型文件不存在：{path}"
    low = path.lower()
    if low.endswith((".xlsx", ".xlsm")):
        return _read_xlsx(path, sheet, header_row)
    if low.endswith(".xls"):
        return None, None, ("旧版 .xls 不支持：请用 Excel 另存为 .xlsx，"
                            "或导出 CSV/TSV 再跑")
    return _read_delimited(path, header_row)


def read_geno_source(source):
    """读基因型样本清单：`.fam` / `.vcf(.gz)` / bfile 前缀。

    返回 (list[(fid, iid)], meta, err)。VCF 的样本名只有一个字符串，
    约定 FID = IID = 样本名（并在 meta 里标注，供报告提示）。
    """
    if not source:
        return None, None, "未提供 --geno"
    if source.endswith(".fam") and os.path.isfile(source):
        path, kind = source, "fam"
    elif source.endswith((".vcf", ".vcf.gz")) and os.path.isfile(source):
        path, kind = source, "vcf"
    elif os.path.isfile(source + ".fam"):
        path, kind = source + ".fam", "bfile"
    else:
        return None, None, f"基因型来源无法识别（需要 .fam / .vcf / bfile 前缀）：{source}"

    pairs = []
    if kind in ("fam", "bfile"):
        for ln in open(path, encoding="utf-8", errors="replace"):
            if not ln.strip():
                continue
            toks = ln.split()
            if len(toks) < 2:
                return None, None, f"{path} 有一行不足两列：{ln[:80]!r}"
            pairs.append((toks[0], toks[1]))
    else:
        import gzip
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#CHROM"):
                    toks = line.rstrip("\n").split("\t")
                    pairs = [(t, t) for t in toks[9:]]
                    break
        if not pairs:
            return None, None, f"{path} 里找不到 #CHROM 表头或样本列"
    if not pairs:
        return None, None, f"{path} 里没有任何样本"
    return pairs, {"path": path, "kind": kind,
                   "fid_assumed": kind == "vcf"}, None


def read_mapping(path):
    """读映射表：两列（来源 ID / 目标 ID），首行可带表头。

    返回 (dict, err)。拒绝重复来源与重复目标 —— 那本身就是"对应关系混乱"。
    """
    if not path:
        return {}, None
    header, rows, err = _read_table(path, header_row=0)
    if err:
        return None, err
    if len(header) < 2:
        return None, f"映射表 {path} 至少需要两列（来源 ID / 目标 ID）"
    body = rows
    # 首行若是表头（含 source/target/表型/基因型/ID 等字样），跳过
    if body:
        first = " ".join(str(x).lower() for x in body[0][:2])
        if any(k in first for k in ("source", "target", "pheno", "geno",
                                    "表型", "基因", "个体", "id")):
            body = rows[1:]
    mapping, seen_targets = {}, {}
    for i, r in enumerate(body, 1):
        src = str(r[0]).strip() if r else ""
        dst = str(r[1]).strip() if len(r) > 1 else ""
        if not src and not dst:
            continue
        if not src or not dst:
            return None, f"映射表第 {i} 行缺来源或目标：{r[:2]}"
        if src in mapping:
            return None, f"映射表里来源 ID 重复：{src!r}（第 {i} 行）—— 这就是对应关系混乱"
        mapping[src] = dst
        key = dst.casefold()
        if key in seen_targets:
            return None, (f"映射表里两个来源指向同一个目标：{seen_targets[key]!r} 与 {src!r} "
                          f"→ {dst!r} —— 这就是对应关系混乱")
        seen_targets[key] = src
    return mapping, None


# --------------------------------------------------------------------------- #
# 匹配
# --------------------------------------------------------------------------- #

def _split_pair(s):
    """把 `FID_IID` / `FID:IID` / `FID IID` 拆成 (fid, iid)；拆不开返回 None。"""
    for sep in (":", "_", " "):
        if sep in s:
            a, _, b = s.partition(sep)
            if a and b:
                return a, b
    return None


class GenoIndex:
    """基因型样本索引：同一份数据建多套键，匹配时按可信度顺序查。"""

    def __init__(self, pairs, rules):
        self.pairs = pairs
        self.rules = rules
        self.by_iid = {}
        self.by_fid = {}
        self.by_pair = {}
        self.by_norm_iid = {}
        self.by_norm_pair = {}
        self.collisions = []
        for fid, iid in pairs:
            self.by_iid.setdefault(iid, []).append((fid, iid))
            self.by_fid.setdefault(fid, []).append((fid, iid))
            self.by_pair[f"{fid}_{iid}"] = [(fid, iid)]
            self.by_pair[f"{fid}:{iid}"] = [(fid, iid)]
            self.by_pair[f"{fid} {iid}"] = [(fid, iid)]
            self.by_norm_iid.setdefault(normalize(iid, rules), []).append((fid, iid))
            self.by_norm_pair.setdefault(normalize(f"{fid}_{iid}", rules), []).append((fid, iid))
            self.by_norm_pair.setdefault(normalize(f"{fid} {iid}", rules), []).append((fid, iid))
            self.by_norm_pair.setdefault(normalize(f"{fid}:{iid}", rules), []).append((fid, iid))
        # 归一化碰撞：两个不同的个体被归一成同一个键 —— 取交集时会把两个人混成一个。
        # ⚠️ 要区分两种成因，否则给用户的建议是错的：
        #   · dup_iid ：**原始 IID 就重复**（同一 IID 出现在多个 FID 下）→ 该用 FID_IID/映射表，
        #                "收紧 --normalize" 根本没用；
        #   · norm_iid：原始 ID 不同、被规则归一后撞在一起（A12/B12 → 12）→ 才该收紧规则。
        for key, hits in self.by_norm_iid.items():
            uniq = set(hits)
            if len(uniq) > 1:
                raw = {iid for _, iid in uniq}
                kind = "dup_iid" if len(raw) == 1 else "norm_iid"
                self.collisions.append((kind, key, sorted(uniq)))
        for key, hits in self.by_norm_pair.items():
            uniq = set(hits)
            if len(uniq) > 1:
                self.collisions.append(("norm_pair", key, sorted(uniq)))

    def resolve(self, raw):
        """返回 (matches, rule, reason)。matches 非空且唯一才算成功。"""
        cands = []                                        # [(pair, rule)]
        if raw in self.by_pair:
            cands.append((self.by_pair[raw], "exact_pair"))
        if raw in self.by_iid:
            cands.append((self.by_iid[raw], "exact_iid"))
        if raw in self.by_fid:
            cands.append((self.by_fid[raw], "exact_fid"))
        norm = normalize(raw, self.rules)
        if norm in self.by_norm_pair:
            cands.append((self.by_norm_pair[norm], "norm_pair"))
        if norm in self.by_norm_iid:
            cands.append((self.by_norm_iid[norm], "norm_iid"))

        if not cands:
            return None, None, "no_candidate"
        # 优先精确档；同一档内若指向多个个体 → 歧义
        order = ["exact_pair", "exact_iid", "exact_fid", "norm_pair", "norm_iid"]
        for rule in order:
            for hits, r in cands:
                if r != rule:
                    continue
                uniq = sorted(set(hits))
                if len(uniq) == 1:
                    return uniq[0], rule, None
                return None, rule, f"ambiguous:{rule}:{len(uniq)}"
        return None, None, "no_candidate"


def _suggest(raw, index, limit=3):
    """给没匹配上的表型 ID 找几个"最像"的基因型 ID —— **只建议，不采用**。"""
    norm = normalize(raw, index.rules)
    pool = list(index.by_norm_iid.keys())
    close = difflib.get_close_matches(norm, pool, n=limit, cutoff=0.8)
    return [index.by_norm_iid[k][0][1] for k in close]


# --------------------------------------------------------------------------- #
# 主分析
# --------------------------------------------------------------------------- #

def _fmt_num(v):
    return format(v, ".10g")


def _to_float(s):
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if t.lower() in ("", "na", "nan", "null", "none", ".", "-", "--"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _sha256_file(path, cap=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            if cap and f.tell() > cap:
                break
    return h.hexdigest()


def analyze(args):
    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)
    rep = Report(STAGE, outdir)
    rep.extras = {}

    rules, err = parse_rules(args.normalize)
    if err:
        rep.add("normalize", "hard", False, err)
        return _finish(args, rep, outdir, {}, [], {})
    rep.extras["normalize"] = rule_names(rules)

    # ---- 基因型 ----
    geno, gmeta, err = read_geno_source(args.geno)
    if err:
        rep.add("geno.source", "hard", False, err)
        return _finish(args, rep, outdir, {}, [], {})
    rep.extras.update({"geno_source": gmeta, "n_geno": len(geno)})

    # ---- 表型 ----
    header, rows, err = _read_table(args.pheno, args.header_row, args.sheet)
    if err:
        rep.add("pheno.read", "hard", False, err)
        return _finish(args, rep, outdir, {}, [], {})
    rep.extras.update({"n_pheno_rows": len(rows), "pheno_columns": header})

    id_col, id_inferred, err = _pick_column(header, rows, args.id_col, geno, rules)
    if err:
        rep.add("pheno.id_col", "hard", False, err)
        return _finish(args, rep, outdir, {}, [], {})
    rep.extras["id_col"] = header[id_col]
    rep.extras["id_col_inferred"] = id_inferred

    trait_ids, terr = _pick_value_columns(header, rows, args.traits, id_col)
    if terr:
        rep.add("pheno.traits", "hard", False, terr)
        return _finish(args, rep, outdir, {}, [], {})
    rep.extras["traits"] = [header[i] for i in trait_ids]
    if args.covars:
        covar_ids, cerr = _pick_value_columns(header, rows, args.covars, id_col)
    else:
        covar_ids, cerr = [], None
    if cerr:
        rep.add("pheno.covars", "hard", False, cerr)
        return _finish(args, rep, outdir, {}, [], {})
    rep.extras["covars"] = [header[i] for i in covar_ids]

    # ---- 映射表 ----
    mapping, err = read_mapping(args.map)
    if err:
        rep.add("map.read", "hard", False, err)
        return _finish(args, rep, outdir, {}, [], {})
    rep.extras["map_used"] = bool(mapping)

    # ---- 逐行匹配 ----
    index = GenoIndex(geno, rules)
    for kind, key, hits in index.collisions:
        if kind == "dup_iid":
            rep.add(f"collision.{kind}", "hard", False,
                    f"同一 IID 出现在多个 FID 下：{hits} —— **只凭 IID 无法确定是哪个人**；"
                    f"请改用 FID_IID 形式给 ID，或提供映射表。"
                    f"（这不是归一化的问题，收紧 --normalize 不解决）")
        else:
            rep.add(f"collision.{kind}", "hard", False,
                    f"归一化后两个不同个体撞成同一个键 {key!r}：{hits} —— "
                    f"取交集会把两个人混成一个；请收紧 --normalize（去掉 digits-only 等）")

    matched = {}            # (fid,iid) -> {"rows":[...], "rule":..., "raw":...}
    unmatched_pheno = []
    ambiguous = []
    tiers = {}
    for r_i, row in enumerate(rows, 1):
        raw = str(row[id_col]).strip() if id_col < len(row) else ""
        if not raw:
            unmatched_pheno.append({"row": r_i, "id": "", "reason": "empty_id", "suggest": ""})
            continue
        pair, rule, reason = None, None, None
        if mapping:
            target = mapping.get(raw)
            if target:
                pair, rule, reason = index.resolve(target)
                rule = f"map->{rule}" if pair else "map"
                if not pair:
                    reason = f"map_target_unresolved:{target}"
            else:
                reason = "not_in_map"
        if pair is None and rule is None:
            pair, rule, reason = index.resolve(raw)
        if pair is None:
            if reason and reason.startswith("ambiguous"):
                ambiguous.append({"kind": "pheno_one_to_many", "id": raw,
                                  "detail": reason, "row": r_i})
                unmatched_pheno.append({"row": r_i, "id": raw, "reason": reason,
                                        "suggest": ""})
            else:
                unmatched_pheno.append({"row": r_i, "id": raw,
                                        "reason": reason or "no_candidate",
                                        "suggest": ";".join(_suggest(raw, index))})
            continue
        tiers[rule] = tiers.get(rule, 0) + 1
        rec = matched.setdefault(pair, {"rows": [], "rule": rule, "raw": raw})
        rec["rows"].append((r_i, row))

    lossy = [r for r in rule_names(rules) if r.split("=")[0] in LOSSY_RULES]
    if lossy:
        rep.add("lossy_rule", "warn", True,
                f"使用了会丢字符/改数字的规则：{lossy} —— 已跑碰撞检测，"
                f"仍建议人工抽查 mapping_used.tsv 的前几十行")

    rep.extras["tiers"] = tiers
    rep.extras["n_matched"] = len(matched)
    rep.extras["n_unmatched_pheno"] = len(unmatched_pheno)
    rep.extras["n_ambiguous"] = len(ambiguous)
    rep.extras["n_collisions"] = len(index.collisions)
    rep.extras["duplicates"] = []

    # ---- 重复个体：默认硬拦，显式 dup-policy 才聚合 ----
    multi = {k: v for k, v in matched.items() if len(v["rows"]) > 1}
    if multi and args.dup_policy == "error":
        for k, v in multi.items():
            rep.add(f"dup.{k[1]}", "hard", False,
                    f"个体 {k[0]} {k[1]} 有 {len(v['rows'])} 行表型（第 "
                    f"{[r for r, _ in v['rows']]} 行）—— 默认视为对应关系混乱；"
                    f"确认要聚合请显式给 --dup-policy mean|first|last")
    elif multi:
        rep.add("dup.aggregated", "warn", True,
                f"{len(multi)} 个个体有多行表型，按 --dup-policy={args.dup_policy} 聚合")

    # ---- 表型侧样本集 ----
    pheno_ids = [str(row[id_col]).strip() for row in rows if str(row[id_col]).strip()]
    unique_pheno = set(pheno_ids)
    rep.extras["n_pheno_unique"] = len(unique_pheno)

    # ---- 值检查（性状 / 协变量）----
    trait_values = _collect_values(matched, trait_ids, args.dup_policy, rep, "trait", header)
    covar_values = _collect_values(matched, covar_ids, args.dup_policy, rep, "covar", header,
                                   allow_missing_fill=args.fill_covar)

    # ---- 写出交集与下游格式 ----
    geno_order = [p for p in geno if p in matched]
    unmatched_geno = [{"fid": f, "iid": i, "reason": "no_phenotype_row"}
                      for f, i in geno if (f, i) not in matched]
    rep.extras["n_unmatched_geno"] = len(unmatched_geno)

    written = _write_outputs(outdir, rep, matched, geno_order, trait_ids, covar_ids,
                             header, trait_values, covar_values, unmatched_pheno,
                             unmatched_geno, ambiguous, rules, args)
    if written is None:
        return _finish(args, rep, outdir, {}, unmatched_pheno, ambiguous)

    # ---- 匹配率 ----
    rate = len(matched) / max(len(unique_pheno), 1)
    rep.extras["match_rate"] = round(rate, 4)
    rep.extras["min_match_rate"] = args.min_match_rate
    if len(matched) == 0:
        rep.add("match.none", "hard", False,
                "一个样本都没匹配上 —— 规则/ID 列/来源文件选错了，或两边根本不是同一批人")
    elif rate < args.min_match_rate:
        rep.add("match.low", "warn", False,
                f"匹配率 {rate:.1%} 低于阈值 {args.min_match_rate:.0%} —— "
                f"不阻断，但**必须人工确认**后再继续")
        rep.extras["needs_confirmation"] = True

    # ---- 未匹配提示 ----
    if unmatched_pheno:
        rep.add("unmatched.pheno", "warn", False,
                f"表型侧 {len(unmatched_pheno)} 行没有匹配上（明细见 unmatched_pheno.tsv）")
    if unmatched_geno:
        rep.add("unmatched.geno", "warn", False,
                f"基因型侧 {len(unmatched_geno)} 个样本没有表型（明细见 unmatched_geno.tsv）"
                f"—— 取交集后不参与分析")

    return _finish(args, rep, outdir, written, unmatched_pheno, ambiguous)


def _pick_column(header, rows, spec, geno, rules):
    """选表型里的 ID 列：显式 `--id-col`（列名或 1-based 序号）优先；省略则推断。"""
    if spec not in (None, ""):
        s = str(spec)
        if s.isdigit():
            i = int(s) - 1
            if not (0 <= i < len(header)):
                return None, False, f"--id-col {spec} 超出列数（共 {len(header)} 列）"
            return i, False, None
        if s in header:
            return header.index(s), False, None
        return None, False, f"表型文件里没有名为 {s!r} 的列（实际列：{header}）"

    # 推断：逐列算"能匹配上的比例"（匹配逻辑与主流程一致），取最高且 >= 50%
    index = GenoIndex(geno, rules)
    best = (None, 0.0)
    for i in range(len(header)):
        vals = [str(r[i]).strip() for r in rows if i < len(r) and str(r[i]).strip()]
        if not vals:
            continue
        hit = sum(1 for v in set(vals) if index.resolve(v)[0] is not None)
        rate = hit / max(len(set(vals)), 1)
        if rate > best[1]:
            best = (i, rate)
    if best[0] is None or best[1] < 0.5:
        return None, False, ("没能推断出 ID 列（没有任何一列能与基因型匹配上 50% 以上）—— "
                            "请显式给 --id-col <列名或序号>")
    return best[0], True, None


def _pick_value_columns(header, rows, spec, id_col):
    """选性状/协变量列：显式列表优先；省略时取"看起来是数值"的列。"""
    if spec not in (None, ""):
        out = []
        for tok in str(spec).split(","):
            tok = tok.strip()
            if not tok:
                continue
            if tok.isdigit():
                i = int(tok) - 1
                if not (0 <= i < len(header)):
                    return None, f"列序号 {tok} 超出范围（共 {len(header)} 列）"
                out.append(i)
            elif tok in header:
                out.append(header.index(tok))
            else:
                return None, f"表型文件里没有名为 {tok!r} 的列（实际列：{header}）"
        return out, None

    out = []
    for i in range(len(header)):
        if i == id_col:
            continue
        vals = [str(r[i]).strip() for r in rows if i < len(r) and str(r[i]).strip()]
        if not vals:
            continue
        numeric = sum(1 for v in vals if _to_float(v) is not None)
        if numeric / len(vals) >= 0.8:
            out.append(i)
    if not out:
        return None, ("没有自动识别出数值型性状列 —— 请显式给 --traits <列名,列名>")
    return out, None


def _collect_values(matched, col_ids, dup_policy, rep, kind, header,
                    allow_missing_fill=False):
    """把每个匹配个体的值取出来（按 dup-policy 聚合），并做缺失/常量/值域检查。"""
    out = {}
    for pair, rec in matched.items():
        row = rec["rows"]
        if len(row) > 1:
            if dup_policy == "first":
                row = [row[0]]
            elif dup_policy == "last":
                row = [row[-1]]
            elif dup_policy == "mean":
                pass
            else:
                continue
        vals = {}
        for j, ci in enumerate(col_ids):
            xs = []
            for _, r in row:
                v = _to_float(r[ci]) if ci < len(r) else None
                if v is not None:
                    xs.append(v)
            if not xs:
                vals[ci] = None
            elif dup_policy == "mean" and len(xs) > 1:
                vals[ci] = sum(xs) / len(xs)
            else:
                vals[ci] = xs[0]
        out[pair] = vals

    if kind == "covar" and allow_missing_fill:
        for ci in col_ids:
            xs = [v[ci] for v in out.values() if v[ci] is not None]
            if xs:
                mean = sum(xs) / len(xs)
                for v in out.values():
                    if v[ci] is None:
                        v[ci] = mean

    for ci in col_ids:
        vals = [v[ci] for v in out.values() if v[ci] is not None]
        miss = len(out) - len(vals)
        col_name = header[ci] if 0 <= ci < len(header) else str(ci)
        name = f"{kind}[{col_name}]"
        if not vals:
            rep.add(f"{name}.values", "hard", False,
                    f"这一列在匹配上的样本里**没有任何有效数值** —— 不是可用的{kind}")
            continue
        if miss:
            lvl = "hard" if kind == "covar" else "warn"
            rep.add(f"{name}.missing", lvl, False,
                    f"{miss}/{len(out)} 个样本缺值"
                    + ("（协变量缺值会让下游模型报错；先补全或用 --fill-covar）"
                       if kind == "covar" else ""))
        if len(vals) >= 3 and len(set(vals)) == 1:
            rep.add(f"{name}.constant", "hard", False,
                    f"所有样本的取值都等于 {vals[0]} —— 常量性状没法做关联")
        elif len(vals) < 3:
            rep.add(f"{name}.tiny", "warn", False,
                    f"匹配上的有效样本只有 {len(vals)} 个，未做常量/分布判定")
        lo, hi = min(vals), max(vals)
        s = sorted(vals)
        q1 = s[len(s) // 4]
        q3 = s[(3 * len(s)) // 4]
        iqr = q3 - q1
        if iqr > 0:
            out_lo, out_hi = q1 - 3 * iqr, q3 + 3 * iqr
            outliers = [v for v in vals if v < out_lo or v > out_hi]
            if outliers:
                rep.add(f"{name}.range", "warn", False,
                        f"{len(outliers)} 个值超出 3×IQR 范围 [{out_lo:.4g}, {out_hi:.4g}]"
                        f"（实际 {lo:.4g}~{hi:.4g}），前 5 个：{[round(v, 4) for v in outliers[:5]]}"
                        f" —— 核对量纲/录入错误")
        rep.extras.setdefault("value_range", {})[name] = [round(lo, 6), round(hi, 6)]
    return out


def _write_outputs(outdir, rep, matched, geno_order, trait_ids, covar_ids, header,
                   trait_values, covar_values, unmatched_pheno, unmatched_geno,
                   ambiguous, rules, args):
    """写出 keep / 各下游格式 / 审计文件。返回文件清单（失败返回 None）。"""
    try:
        # keep.txt（交集，按基因型顺序）
        with open(os.path.join(outdir, "keep.txt"), "w", encoding="utf-8") as f:
            for fid, iid in geno_order:
                f.write(f"{fid}\t{iid}\n")

        # mapping_used.tsv
        with open(os.path.join(outdir, "mapping_used.tsv"), "w", encoding="utf-8") as f:
            f.write("pheno_id_raw\tpheno_id_norm\tgeno_fid\tgeno_iid\trule\n")
            for fid, iid in geno_order:
                rec = matched[(fid, iid)]
                f.write(f"{rec['raw']}\t{normalize(rec['raw'], rules)}\t{fid}\t{iid}\t{rec['rule']}\n")

        # pheno_mvp.txt（Taxa + 性状；tab + 表头，按基因型顺序）
        with open(os.path.join(outdir, "pheno_mvp.txt"), "w", encoding="utf-8") as f:
            f.write("\t".join(["Taxa"] + [header[i] for i in trait_ids]) + "\n")
            for fid, iid in geno_order:
                vals = trait_values.get((fid, iid), {})
                f.write("\t".join([iid] + [_fmt_num(vals[i]) if vals.get(i) is not None else "NA"
                                           for i in trait_ids]) + "\n")

        # pheno_gemma_<trait>.txt（⚠️ 单列值、按 fam 顺序 —— GEMMA -p 的格式）
        for i in trait_ids:
            name = re.sub(r"[^0-9A-Za-z_.-]+", "_", header[i])
            with open(os.path.join(outdir, f"pheno_gemma_{name}.txt"), "w",
                      encoding="utf-8") as f:
                for fid, iid in geno_order:
                    v = trait_values.get((fid, iid), {}).get(i)
                    f.write((_fmt_num(v) if v is not None else "NA") + "\n")

        # covar_mvp.txt
        if covar_ids:
            with open(os.path.join(outdir, "covar_mvp.txt"), "w", encoding="utf-8") as f:
                f.write("\t".join(["Taxa"] + [header[i] for i in covar_ids]) + "\n")
                for fid, iid in geno_order:
                    vals = covar_values.get((fid, iid), {})
                    f.write("\t".join([iid] + [_fmt_num(vals[i]) if vals.get(i) is not None
                                               else "NA" for i in covar_ids]) + "\n")

        # pheno_aligned.csv（审计：原始行 + 匹配结果 + 规则）
        with open(os.path.join(outdir, "pheno_aligned.csv"), "w", encoding="utf-8") as f:
            f.write("pheno_id_raw,geno_fid,geno_iid,rule," + ",".join(header) + "\n")
            for fid, iid in geno_order:
                rec = matched[(fid, iid)]
                for _r_i, row in rec["rows"]:
                    esc = [f'"{str(x).replace(chr(34), chr(34) * 2)}"' for x in row]
                    f.write(",".join([rec["raw"], fid, iid, rec["rule"]] + esc) + "\n")

        # 未匹配 / 歧义
        with open(os.path.join(outdir, "unmatched_pheno.tsv"), "w", encoding="utf-8") as f:
            f.write("row\tpheno_id\treason\tsuggestions\n")
            for u in unmatched_pheno:
                f.write(f"{u['row']}\t{u['id']}\t{u['reason']}\t{u['suggest']}\n")
        with open(os.path.join(outdir, "unmatched_geno.tsv"), "w", encoding="utf-8") as f:
            f.write("FID\tIID\treason\n")
            for u in unmatched_geno:
                f.write(f"{u['fid']}\t{u['iid']}\t{u['reason']}\n")
        with open(os.path.join(outdir, "ambiguous.tsv"), "w", encoding="utf-8") as f:
            f.write("kind\tid\tdetail\trow\n")
            for a in ambiguous:
                f.write(f"{a['kind']}\t{a['id']}\t{a['detail']}\t{a.get('row', '')}\n")

        return {"keep": "keep.txt", "mapping": "mapping_used.tsv",
                "pheno_mvp": "pheno_mvp.txt", "n_keep": len(geno_order)}
    except Exception as e:                                    # noqa: BLE001
        rep.add("output.write", "hard", False, f"写对齐产物失败：{e!r}")
        return None


def _finish(args, rep, outdir, written, unmatched_pheno, ambiguous):
    """落盘报告 + 参数 + 人读摘要，返回退出码。"""
    status = "blocked" if rep.blocked else "awaiting_approval"
    params = {
        "stage": STAGE,
        "pheno": os.path.abspath(args.pheno),
        "geno": os.path.abspath(args.geno),
        "map": os.path.abspath(args.map) if args.map else "",
        "id_col": args.id_col or "",
        "traits": args.traits or "",
        "covars": args.covars or "",
        "normalize": args.normalize or "",
        "dup_policy": args.dup_policy,
        "min_match_rate": args.min_match_rate,
        "header_row": args.header_row,
        "sheet": args.sheet or "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "inputs_sha256": {k: _sha256_file(v) for k, v in (
            ("pheno", args.pheno), ("geno", args.geno)) if v and os.path.isfile(v)},
    }
    with open(os.path.join(outdir, PARAMS_FILE), "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    report = rep.as_dict()
    report.pop("read_totals", None)
    report.pop("primary_totals", None)
    report.update({
        "stage": STAGE,
        "status": status,
        "approval_required": True,
        "needs_confirmation": bool(rep.extras.get("needs_confirmation")),
        "outputs": written,
    })
    report.update(rep.extras)
    with open(os.path.join(outdir, REPORT_FILE), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    _write_human_report(outdir, report, params)
    if not (args.json and not rep.blocked):
        print(rep.render())
    print(_human_summary(report))
    return 1 if rep.blocked else 0


def _human_summary(r):
    lines = ["", "──────── 表型-基因型对齐 ────────",
             f"基因型样本 {r.get('n_geno', '?')} / 表型行 {r.get('n_pheno_rows', '?')}"
             f"（唯一 {r.get('n_pheno_unique', '?')}）",
             f"匹配 {r.get('n_matched', 0)}（匹配率 {r.get('match_rate', 0):.1%}）"]
    tiers = r.get("tiers") or {}
    if tiers:
        lines.append("规则分布：" + " / ".join(f"{k} {v}" for k, v in sorted(tiers.items())))
    if r.get("n_unmatched_geno"):
        lines.append(f"基因型独有 {r['n_unmatched_geno']}（不参与分析，见 unmatched_geno.tsv）")
    if r.get("n_unmatched_pheno"):
        lines.append(f"表型独有 {r['n_unmatched_pheno']}（见 unmatched_pheno.tsv，含候选建议）")
    if r.get("n_ambiguous"):
        lines.append(f"歧义 {r['n_ambiguous']}（见 ambiguous.tsv，必须人工处理）")
    lines.append("状态：" + ("❌ 有硬性失败，未产出可用结果" if r["status"] == "blocked"
                             else "⏸ 等待用户确认（approve 后才能进入下一步）"))
    if r["status"] == "blocked":
        lines.append("下一步：先修上面列出的硬性失败（明细在 ambiguous/unmatched 文件里），再重跑 analyze")
    else:
        lines.append("下一步：")
        lines.append("  1) 把本报告给用户看，确认匹配关系无误")
        lines.append('  2) python pheno_align.py approve --outdir <本目录> --note "用户确认"')
    return "\n".join(lines)


def _write_human_report(outdir, r, params):
    lines = ["# 表型-基因型对齐报告", "",
             f"- 表型文件：`{params['pheno']}`（ID 列：{r.get('id_col', '?')}"
             f"{'，自动推断' if r.get('id_col_inferred') else ''}）",
             f"- 基因型来源：`{params['geno']}`（{r.get('geno_source', {}).get('kind', '?')}）",
             f"- 规范化规则：{', '.join(r.get('normalize') or []) or '无'}",
             f"- 重复策略：{params['dup_policy']}", ""]
    counts = [
        ("基因型样本", r.get("n_geno", 0)),
        ("表型行", r.get("n_pheno_rows", 0)),
        ("表型唯一个体", r.get("n_pheno_unique", 0)),
        ("匹配", r.get("n_matched", 0)),
        ("基因型独有", r.get("n_unmatched_geno", 0)),
        ("表型独有", r.get("n_unmatched_pheno", 0)),
        ("歧义", r.get("n_ambiguous", 0)),
    ]
    lines += ["| 项 | 数量 |", "|---|---:|"]
    lines += [f"| {k} | {v} |" for k, v in counts]
    lines += ["", f"- 匹配率：{r.get('match_rate', 0):.2%}（阈值 {r.get('min_match_rate', 0):.0%}）"]
    tiers = r.get("tiers") or {}
    if tiers:
        lines += ["- 规则分布：" + "，".join(f"`{k}` {v}" for k, v in sorted(tiers.items()))]
    fails = r.get("hard_failures") or []
    warns = r.get("warnings") or []
    if fails:
        lines += ["", "## 硬性失败", ""] + [f"- {x}" for x in fails]
    if warns:
        lines += ["", "## 告警", ""] + [f"- {x}" for x in warns]
    lines += ["", "## 产物", "",
              "`keep.txt`、`pheno_mvp.txt`、`pheno_gemma_*.txt`、`mapping_used.tsv`、"
              "`unmatched_pheno.tsv`、`unmatched_geno.tsv`、`ambiguous.tsv`、`pheno_aligned.csv`"]
    with open(os.path.join(outdir, "align_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# 审批：analyze 之后的强制一步
# --------------------------------------------------------------------------- #

def _output_hashes(outdir):
    names = ["keep.txt", "pheno_mvp.txt", "mapping_used.tsv",
             "unmatched_geno.tsv", "unmatched_pheno.tsv"]
    h = {}
    for n in names:
        p = os.path.join(outdir, n)
        h[n] = _sha256_file(p) if os.path.isfile(p) else ""
    for n in sorted(os.listdir(outdir)):
        if n.startswith("pheno_gemma_") and n.endswith(".txt"):
            h[n] = _sha256_file(os.path.join(outdir, n))
    return h


def cmd_approve(args):
    outdir = os.path.abspath(args.outdir)
    if not os.path.isfile(os.path.join(outdir, REPORT_FILE)):
        print(f"✗ {outdir} 里没有 {REPORT_FILE} —— 先跑 analyze", file=sys.stderr)
        return 1
    report = json.load(open(os.path.join(outdir, REPORT_FILE), encoding="utf-8"))
    if report.get("status") == "blocked":
        print("✗ 这次对齐有硬性失败，不能审批 —— 先修问题再跑 analyze", file=sys.stderr)
        return 1
    if not args.note.strip():
        print("✗ approve 必须带 --note（记录谁、基于什么确认的）", file=sys.stderr)
        return 1
    approval = {
        "stage": STAGE,
        "approved_at": datetime.now().isoformat(timespec="seconds"),
        "note": args.note,
        "params_sha256": _sha256_file(os.path.join(outdir, PARAMS_FILE)),
        "outputs_sha256": _output_hashes(outdir),
    }
    with open(os.path.join(outdir, APPROVAL_FILE), "w", encoding="utf-8") as f:
        json.dump(approval, f, ensure_ascii=False, indent=2)
    print(f"✓ 已记录审批：{os.path.join(outdir, APPROVAL_FILE)}")
    return 0


def cmd_check(args):
    outdir = os.path.abspath(args.outdir)
    ap = os.path.join(outdir, APPROVAL_FILE)
    if not os.path.isfile(ap):
        print(f"✗ 还没有审批：{ap} 不存在 —— 对齐结果必须先经用户确认"
              f"（python pheno_align.py approve ...）", file=sys.stderr)
        return 1
    approval = json.load(open(ap, encoding="utf-8"))
    if approval.get("params_sha256") != _sha256_file(os.path.join(outdir, PARAMS_FILE)):
        print("✗ 审批已过期：对齐参数在对齐后被改过，需要重新确认", file=sys.stderr)
        return 1
    if approval.get("outputs_sha256") != _output_hashes(outdir):
        print("✗ 审批已过期：对齐产物在审批后被改动过，需要重新确认", file=sys.stderr)
        return 1
    print("✓ 审批有效（参数与产物哈希均未变化）")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(
        description="表型与基因型样本对齐（GWAS S0.2）：分档匹配 + 差异落盘 + 强制审批")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="执行对齐并产出所有下游文件与报告")
    a.add_argument("--pheno", required=True, help="表型文件（xlsx/csv/tsv/txt）")
    a.add_argument("--geno", required=True,
                   help="基因型样本来源：.fam / .vcf(.gz) / bfile 前缀")
    a.add_argument("--outdir", required=True, help="输出目录")
    a.add_argument("--map", default="", help="可选映射表（两列：来源 ID / 目标 ID）")
    a.add_argument("--id-col", default="", help="表型里的 ID 列（列名或 1-based 序号）")
    a.add_argument("--traits", default="", help="性状列（逗号分隔；省略=自动识别数值列）")
    a.add_argument("--covars", default="", help="协变量列（逗号分隔）")
    a.add_argument("--normalize", default="",
                   help="ID 规范化规则，逗号分隔：trim,fullwidth,casefold,"
                        "strip-prefix=X,strip-suffix=X,pad=N,dedup-suffix,digits-only")
    a.add_argument("--dup-policy", default="error",
                   choices=["error", "mean", "first", "last"],
                   help="同一个个体的多行表型：默认 error（拦下）")
    a.add_argument("--min-match-rate", type=float, default=DEFAULT_MIN_MATCH_RATE,
                   help=f"低于该匹配率标记为必须人工确认（默认 {DEFAULT_MIN_MATCH_RATE}）")
    a.add_argument("--header-row", type=int, default=1, help="表头所在行（1-based，默认 1）")
    a.add_argument("--sheet", default="", help="xlsx 的 sheet 名（默认第一个）")
    a.add_argument("--fill-covar", action="store_true",
                   help="协变量缺值用均值填充（默认缺值即 hard，避免静默引入偏差）")
    a.add_argument("--json", action="store_true", help="以 JSON 输出报告到 stdout")
    a.add_argument("--quiet", action="store_true", help="只在失败时输出")
    a.set_defaults(func=analyze)

    p = sub.add_parser("approve", help="用户确认后写入审批凭证")
    p.add_argument("--outdir", required=True)
    p.add_argument("--note", required=True, help="确认依据（谁、基于什么确认的）")
    p.set_defaults(func=cmd_approve)

    c = sub.add_parser("check", help="校验审批仍有效（下游阶段开工前调用）")
    c.add_argument("--outdir", required=True)
    c.set_defaults(func=cmd_check)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
