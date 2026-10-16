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
标记每个表型是"已就绪 / 缺模型 / 缺代码"，并给出建议（当前建议 `IMF`）。

2. **把建议展示给用户，确认要提取哪个表型**再执行，不要擅自全跑。

3. 执行所选表型（当前为 IMF，默认全量 162 张，约 20s）：

```bash
cd skills/phenotype-extract/scripts     # 仓库根下的相对路径
conda run -n Phenotype python phenotype_extract.py --pheno IMF
```

## 输入

| 来源 | 说明 |
|------|------|
| `database::sample_images` | 表型图片：`sample_name(et号)/sample_number/image_path` |
| `database::models` | 模型注册表：`name/task/model_path/config/metrics`；按插件的 `model_names` 取每个表型所需模型 |
| `phenotypes/<name>.py 插件` | 该表型的计算代码(compute)与元信息 |
| 关联表 xls | et→猪号、猪号→近红外(%)，供明细表加"猪号/参照"列 |

## 输出（`{outdir}/{表型名}/`，`--outdir` 缺省 `$PIG_GENO_DIR/phenotype`，即 `/data1/chenyx/Pig_geno/phenotype/<表型>/`）

| 文件 | 内容 |
|------|------|
| `imf_phenotype.csv` | 明细：Taxa/猪号/图像/面积/IMF_预测_%/NIR真值/状态 |
| `imf_phenotype_gwas_plink.txt` | PLINK/GEMMA：`et1 et1 <IMF>` 无表头，**仅含模型预测到的样本，不做缺失填充** |
| `imf_phenotype_gwas_gapit.csv` | GAPIT：`Taxa,IMF` 表头 |

PLINK 行序与 `et277.fm.snp.fam` 的相对顺序一致（`--fam` 参数仅作排序基准）。

## 新增一个表型（~30 行代码，无需改框架）

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
- **数据路径可用环境变量覆盖**（便于换机器）：`PIPELINE_DB`（默认 `$PIG_GENO_DIR/pipeline.db`）、
  `PIG_GENO_DIR`（默认 `/data1/chenyx/Pig_geno`）、`PIG_IMAGE_DIR`（默认 `/data1/chenyx/Pig_image`）。
  相关 xls/fam 若不在默认位置，用 `--nir-xls` / `--corr-xls` / `--fam` 显式指定。
- `--num-labels`(默认2) 需与具体表型模型的类别数一致；`--thresh`(默认30)为黑底板抠肉 ROI 阈值，传给插件。
- GAPIT/PLINK 两文件只含实际计算到表型的样本（已按用户要求去掉任何 -9/缺失填充与额外样本）。
- 精度说明：图像可见脂肪%≠化学 IMF%，与 NIR 的 RMSE≈0.90；需更高一致先做线性校准 `N≈0.456·P+1.149`。
