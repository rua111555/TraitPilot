---
name: phenotype-extract
description: >
  通用表型提取工具。感知数据库(models 表)中的分割模型 与 phenotypes/ 下的表型提取代码插件，
  列出并建议可提取的表型(如 IMF 肌内脂肪含量)，按所选表型：从 database::sample_images 读表型图片 →
  加载对应模型推理 → 插件计算表型值 → 输出明细 CSV → 再转换为 GWAS 表型格式
  (PLINK/GEMMA 的 FID/IID/pheno + GAPIT 的 Taxa,trait)。
  新增表型只需加代码插件 + 注册模型，无需新增 skill。曾用名 imf-phenotype-gwas。
allowed-tools:
  - Read
  - Write
  - Bash
  - Grep
---

# 通用表型提取 → GWAS 表型格式

从数据库读取表型图片，用注册在数据库里的分割模型 + 插件里的表型计算代码提取表型，
输出明细 CSV + GWAS 表型文件。**当前唯一插件为 IMF**，框架本身与表型无关、可扩展。

## 工作流（每次调用都先让工具自查再确认）

1. **先列可用表型并让工具给建议**（不加载模型，秒级）：

先 `cd` 到**本 skill 的 `scripts/` 目录**（即 `skills/phenotype-extract/scripts`，相对于仓库根；
脚本靠同目录的 `phenotypes/` 包做导入，必须在其中运行）：

```bash
cd skills/phenotype-extract/scripts     # 仓库根下的相对路径
conda run -n Phenotype python phenotype_extract.py
```

输出会展示：数据库 `models` 表(已有模型) × `phenotypes/` 代码插件 的两两匹配，
标记每个表型是"已就绪 / 缺模型 / 缺代码"，并给出建议（当前唯一插件为 `IMF`）。

2. **把建议展示给用户，确认要提取哪个表型**再执行，不要擅自全跑。

3. 执行所选表型（当前为 IMF，默认跑数据库 `sample_images` 里的全部样本）：

```bash
cd skills/phenotype-extract/scripts     # 仓库根下的相对路径
conda run -n Phenotype python phenotype_extract.py --pheno IMF
```

## 输入

| 来源 | 说明 |
|------|------|
| `database::sample_images` | 表型图片：`sample_name(样本名/编号)/sample_number/image_path` |
| `database::models` | 模型注册表：`name/task/model_path/config/metrics`；按插件的 `model_names` 取每个表型所需模型 |
| `phenotypes/<name>.py 插件` | 该表型的计算代码(compute)与元信息 |
| 关联表 xls | 样本编号→个体号、个体号→参照值(%)，供明细表加"个体号/参照"列（可选） |

## 输出（`{outdir}/{表型名}/`，`--outdir` 缺省 `$GENO_DIR/phenotype`）

| 文件 | 内容 |
|------|------|
| `imf_phenotype.csv` | 明细：Taxa/个体号/图像/组织与脂肪像素面积/IMF_预测_%/参照(NIR)真值/状态 |
| `imf_phenotype_gwas_plink.txt` | PLINK/GEMMA：`<样本> <样本> <IMF>` 无表头，**仅含模型预测到的样本，不做缺失填充** |
| `imf_phenotype_gwas_gapit.csv` | GAPIT：`Taxa,IMF` 表头 |
| `imf_verify.json` | **出口校验报告**：每条判据的结论与实测值 |

PLINK 行序与 `--fam` 指定的基因型 `.fam` 的相对顺序一致（`--fam`/`$FAM_PATH` 仅作排序基准，缺省按样本号排）。

## 出口校验门禁（结果值校验）

**表型提取的错误大多是静默的** —— 抠图失败、模型没加载对、阈值与数据不匹配，产出的
往往不是一个报错，而是一个**看起来合理的数值**（典型：抠图失败时若用 `max(meat_px,1)`
兜底，会得到 `IMF=0.0%`，落在合法值域内）。这种错在明细表里翻不出来，却会一路带进
GWAS，事后没人能判断那个 0.0 是"真的没脂肪"还是"抠图失败"。

因此提取流程**在写 GWAS 表型文件之前**做出口校验（`scripts/validate.py`）：

| 判据 | 级别 | 含义 |
|------|:----:|------|
| `value_range` | hard¹ | 值须落在插件声明的物理值域内（IMF 为 `[0,100]`） |
| `fail_rate` | hard | 失败样本占比 ≤5%（`--max-fail-rate` 可调） |
| `not_constant` | hard | 不能所有样本同值 —— 那是模型没真跑的最常见形态 |
| `sample_count` | hard | 可产出样本数 ≥ `--min-samples` |
| `sample_uniqueness` | hard | 同一 `Taxa` 只能有**一个**成功测量值。按图像一行的固有风险：同一个体拍多张时，PLINK 导出按键去重、GAPIT 导出保留全部行 → **两个 GWAS 文件静默分歧**；多次测量值互相矛盾时更无从取舍。拦截信息里会区分"有重复个体"与"重复个体间值互相矛盾" |
| `nir_agreement` | warn | 预测 vs 参照真值的相关系数 ≥ `--min-nir-r`（建议 0.6） |

> ¹ **`value_range` 的级别取决于插件是否声明值域**：插件写了 `validate.value_range`
> 才是 hard；**没写则降级为 warn 并明说"未做该判定"**（物理不可能的值不会被拦，但报告里
> 看得见这件事没做）。一个没声明值域的新插件不会因此静默失去越界保护而不留痕迹 ——
> 但也**别把值域当常量**：它由插件按自己的表型语义声明（IMF 是 `[0,100]`，别的表型可能是
> 比率、计数或负值）。

**拦截行为**：hard 判据未过 → **不写出 GWAS 表型文件**（`imf_phenotype_gwas_*`），
退出码 1；但**明细 CSV 与校验报告仍写出**，供排查根因。warn 未过 → 照常产出，仅告警。

```bash
# 需要更严的样本量下限（如 GWAS 前）：
conda run -n Phenotype python phenotype_extract.py --pheno IMF --min-samples 100
# 有参照真值时，把相关性纳入门禁：
conda run -n Phenotype python phenotype_extract.py --pheno IMF --min-nir-r 0.6
```

> **插件契约**：`compute(ctx)` 在「算不出可信值」时必须抛 `PhenotypeError`，**不得返回
> 兜底数值**（兜底值通常落在合法值域内，会伪装成正常结果）。插件通过 `TASK` 的
> `validate.value_range` 声明值域。

**独立复核**（同一个门禁也可事后对任意明细 CSV 跑，供 GWAS 入口复用）：

```bash
conda run -n Phenotype python scripts/validate.py \
    --csv "$GENO_DIR/phenotype/IMF/imf_phenotype.csv" \
    --col 'IMF_预测_%' --range 0 100 --min-nir-r 0.6 --json
# 退出码 0=通过；1=应拦截（JSON 里 hard_failures 给出原因）
```

新增表型（~30 行代码，无需改框架）

1. **写代码插件** `scripts/phenotypes/<name>.py`（参考 `imf.py`），定义顶层 `TASK`：
   - `name`/`title`/`description`：随即自动出现在 `--list` 建议里
   - `model_names`：需要的 DB 模型名列表
   - `trait`/`pheno_col`：GWAS 列名 / CSV 表型列名
   - `compute(ctx) -> (值, 额外列 dict)`：ctx 含 `img(BGR)`、`seg(类标签mask)`、`et`、`thresh`、`mappings`、`device`
   - 可选 `mask_encode(seg)` 定义预测 mask 存盘编码
2. **在数据库注册模型**（参考 `models` 表里 `imf_mask2former` 记录的写法）：
   `INSERT INTO models(name,task,framework,model_path,config_json,metrics_json,version) VALUES(...)`
3. **无需改动**主脚本/此 skill。下次 `--list` 自动发现并建议。
   若只加了代码没注册模型 → `--list` 标"缺模型"；反之标"缺代码"，均有提示。

## 注意事项

- 跑在 conda 虚拟环境 **`Phenotype`**（torch/transformers/openCV 等 ML 栈）；**不是** conda base，也不是 `Agent` 环境。
- **数据路径可用环境变量覆盖**（便于换机器）：`PIPELINE_DB`（默认 `$GENO_DIR/pipeline.db`）、
  `GENO_DIR`（默认 `~/genomics_data`）、`IMAGE_DIR`（默认 `$GENO_DIR/images`）。
  相关 xls/fam 若不在默认位置，用 `--nir-xls` / `--corr-xls` / `--fam` 显式指定。
- `--num-labels`(默认2) 需与具体表型模型的类别数一致；`--thresh`(默认30)为黑底板抠肉 ROI 阈值，传给插件。
- GAPIT/PLINK 两文件只含实际计算到表型的样本（已按用户要求去掉任何 -9/缺失填充与额外样本）。
- 精度说明：图像可见脂肪%≠化学 IMF%，与 NIR 的 RMSE≈0.90；需更高一致先做线性校准 `N≈0.456·P+1.149`。
