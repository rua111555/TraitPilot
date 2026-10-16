# phenotype-extract 使用手册

通用表型提取：感知数据库模型 + 待提表型代码，按所选表型从数据库图片提取表型 → 明细 CSV + GWAS 表型文件。
当前支持的表型：**IMF**（肌内脂肪含量）。

## 快速开始

```bash
cd /home/chenyuxi/skill/agent/skills/phenotype-extract/scripts

# 1) 查看可提取的表型与就绪状态（会给出建议）
conda run -n Phenotype python phenotype_extract.py

# 2) 提取 IMF（全量 162 张约 20s）
conda run -n Phenotype python phenotype_extract.py --pheno IMF
```

输出在 `/data1/chenyx/Pig_geno/phenotype/IMF/`：

| 文件 | 内容 |
|------|------|
| `imf_phenotype.csv` | 162 个样本明细：Taxa、猪号、图像、肉/脂面积、**IMF_预测_%**、NIR真值 |
| `imf_phenotype_gwas_plink.txt` | **PLINK/GEMMA**：`et1 et1 <IMF>` 无表头，**仅含模型实际预测到的 162 个样本**，顺序与 `et277.fm.snp.fam` 一致，无缺失填充 |
| `imf_phenotype_gwas_gapit.csv` | **GAPIT**：`Taxa,IMF` 表头，与 `IMF表型.xlsx` 命名一致 |

## 常用参数

- 改输出根目录：`--outdir /data1/chenyx/Pig_geno/temp/phenotype`（每个表型自动建子目录）
- 只跑前几个调试：`--limit 3`
- 顺便保存分割 mask：`--save-masks`（输出到 `{outdir}/{表型}/masks/`)
- 图片来自 `sample_images`、模型来自 `models` 表，一般无需改

## 解读与验证

- 全量 162 张约 20s；汇总行 `对照(IMF_NIR_%) r≈0.74` 表示预测与近红外实测的相关性。
- 逐张数值应与既有 `/data1/chenyx/Pig_image/phenotype_results.csv` 的
  `表型_脂肪面积占比_%` 列完全一致（同模型同抠肉逻辑）。
- **精度说明**：图像可见脂肪% ≠ 化学 IMF%，与 NIR 的 RMSE≈0.90；
  若需更高一致先做线性校准 `N ≈ 0.456·P + 1.149`。

## 新增其他表型（不用写新 skill）

按 `SKILL.md` 的"新增一个表型"三步：加 `scripts/phenotypes/<name>.py` 插件 + 在数据库 `models` 表注册模型。
`--list` 会自动发现并建议。参考模板：`scripts/phenotypes/imf.py`。

## 常见问题

- **加载模型很慢/卡在网络**：本机无外网到 huggingface.co。脚本默认离线
  (local_files_only)走本地 HF 缓存，不应慢。若 HF 缓存被清，先用镜像预下载：
  `HF_ENDPOINT=https://hf-mirror.com python -c "from huggingface_hub import snapshot_download; snapshot_download('facebook/mask2former-swin-small-ade-semantic')"`
  或在缓存恢复后用 `--allow-download`。
- **打印 class_predictor 形状不匹配**：正常，迁移学习(num_labels 2 vs 151)后由 checkpoint 覆盖。
- **个别行显示 fail**：多为原图缺失/损坏；该类样本不写入 GWAS 文件（明细 CSV 的 `状态` 列可查）。
- **`--list` 提示某表型缺模型/缺代码**：按提示补数据库记录或代码插件即可。
