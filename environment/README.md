# 环境与部署（ENVIRONMENT）

> **这份文档面向「把 TraitPilot 装到一台新机器上的人」，不是本机备忘。**
> 它列的是**跑通整个项目**所需的环境；`requirements.txt` 只是其中最小的一块（Agent 本体的三个
> pip 包），**远不能代表全貌** —— 18 个技能背后还有两套 conda 环境、若干独立二进制、
> R 包、docker/GPU 栈、参考基因组与数据库。

先跑自检，缺什么一目了然：

```bash
cd agent
bash environment/check_env.sh          # 全量
bash environment/check_env.sh --core   # 只查 Agent 本体（不碰生信管线）
bash environment/check_env.sh --quiet  # 只打印缺失项
```

它按九个块检查并给出安装提示；末尾汇总「核心缺失 / 可选缺失」，核心有缺则返回非零退出码。

或者直接**一键安装**（本文提供的全部环境由脚本落实）：

```bash
bash environment/install.sh --list     # 先看要装什么、什么版本
bash environment/install.sh            # 安装（逐步确认）
bash environment/install.sh --yes      # 无人值守
bash environment/install.sh --only agent   # 只装某一层 / --skip pheno 跳过某层
bash environment/install.sh --cpu      # 无 GPU：torch 装 CPU 版
```

---

## 0. 三分钟速览：这项目到底需要什么

| 层 | 内容 | 必需性 |
|---|---|---|
| **A. Agent 本体** | Python ≥3.10（实测 3.12）+ `requests` / `PyYAML` / `flask`，装在 conda 环境 `Agent` + 一个 LLM API 凭证 | **必需** |
| **B. 平台** | Linux/POSIX：`/bin/bash`、`nohup`、`curl`、`pgrep/pkill`；`sqlite3` 带 FTS5（仅检索/记忆用） | **必需** |
| **C. WGS 工具链** | conda 环境 **`Agent`**：bwa · samtools · bcftools · htslib(tabix) · gatk4 · fastp · plink · numactl · openjdk（+ 本体三件套） | 跑 WGS 管线时必需 |
| **D. GWAS 工具链** | conda 环境 **`r-gwas`**（R 4.3 + rMVP/CMplot/pheatmap/reshape2/bigmemory…）+ 独立二进制 gemma / gcta64 / LDBlockShow / ShowLDSVG / PopLDdecay + Haploview.jar + perl | 跑 GWAS 管线时必需 |
| **E. 表型提取** | conda 环境 **`Phenotype`**：torch/transformers/numpy/pandas/opencv/scipy/pillow… + HF 缓存 + 模型权重 | 跑 phenotype-extract 时必需 |
| **F. 数据** | `pipeline.db`、参考基因组 `ref.fa/.fai/.dict`、FASTQ、表型图片、known-sites VCF | 跑真实数据时必需 |
| **G. GPU 加速（可选）** | NVIDIA 驱动 ≥545 + docker + nvidia-container-toolkit + `clara-parabricks` NGC 镜像 | **可选**，缺了自动回退 CPU |

> 只想要「能对话、能读技能、能跑 shell」→ 只需要 A + B。其余按你要跑哪条管线补。

---

## 1. 依赖全景

### 1.1 Agent 本体（`requirements.txt`，三个包）

| 包 | 导入名 | 用途 | 位置 |
|---|---|---|---|
| `requests>=2.31` | `requests` | Anthropic Messages API 客户端（CLI/Web 都走它） | `skill_agent/client.py:15` |
| `PyYAML>=6.0` | `yaml` | 解析 `SKILL.md` frontmatter | `skill_agent/skills.py:16` |
| `flask>=3.0` | `flask` | 仅 Web 前端（`@app.get/@app.post` 需 ≥2.0） | `webapp/server.py:18` |
| `pytest`（可选） | `pytest` | 仅跑测试 | `tests/*.py` |

装进 conda 虚拟环境 **`Agent`**（与 WGS/GWAS 共用，安装器即如此处理）：

```bash
conda run -n Agent pip install -r requirements.txt
# 跑测试再加： conda run -n Agent pip install pytest
```

**Python 版本**：源码用了 `list[dict]` / `tuple[...]` 内建泛型（`skill_agent/client.py:43`），
所以需要 **≥3.10**；实测在 **3.13.9 / 3.12.13** 上正常。没有 `match` 语句、没有 `tomllib`，
3.10 即可。

### 1.2 平台与命令行工具（非 pip，系统自带）

| 依赖 | 为什么 | 缺了会怎样 |
|---|---|---|
| `/bin/bash`（**绝对路径**） | 所有 Bash 工具都经它执行 | Bash 工具全废 |
| `nohup` | 后台任务（`.done`/`.pid` 轮询机制的基础） | 后台任务起不来，编排管线退化 |
| `curl` | `start_web.sh` 启动后探活 `/api/config` | 只是探活失败，服务照常起 |
| `pgrep`/`pkill`（procps） | `start_web.sh stop/restart` | 只能手动 `kill` |
| `bash >= 4.3` | 编排脚本用 `wait -n` 控样本并发 | 并发池降级/报错。**本机系统 bash 仅 4.2，靠 conda 的 bash** |
| `rg`（ripgrep，可选） | `Grep` 工具加速 | 自动回退纯 Python 实现，只是慢 |
| `sqlite3` + **FTS5** | `--retrieval` 的 Search/RepoMap、`--memory` | 两个功能**自停用**，其余不受影响。FTS5 还需 SQLite ≥3.34（trigram 分词） |

### 1.3 生信工具链 —— conda 环境 `Agent`

技能里的脚本**统一用 `command -v` 运行时探测工具**（`conda run -n Agent` 下工具已在 PATH），
不再写死本机 conda 绝对路径。环境名仍必须叫 **`Agent`**（技能按此名 `conda run`）；
数据路径可用环境变量覆盖（见 §1.6）。

```bash
# 推荐：用一键安装脚本（按本机探测到的 conda 安装，非写死路径）
bash environment/install.sh --only agent

# 或手动最小集
conda create -n Agent -c bioconda -c conda-forge -y \
  bwa=0.7.19 samtools=1.23 bcftools=1.23.1 htslib=1.23 \
  gatk4=4.3.0.0 plink fastp numactl openjdk python=3.12
conda run -n Agent pip install requests PyYAML flask        # harness 本体也装这里
conda install -n Agent -c bioconda multiqc                  # 可选：聚合 fastp QC 报告

# 备选：直接导入本机导出的 yml（复刻最完整，但频道写死清华镜像，新机器上常解不开）
conda env create -f environment/Agent.yml
```

| 工具 | 用于 | 必需性 |
|---|---|---|
| `bwa` | Stage 1 比对 | 必需 |
| `samtools` | 排序/索引/flagstat | 必需 |
| `bcftools` | 过滤后 Ti/Tv 校验 | 必需 |
| `htslib`（提供 `tabix`/`bgzip`） | GVCF/VCF 索引 | 必需 |
| `gatk4`（4.3.0.0，需 Java） | MarkDuplicates / BQSR / HaplotypeCaller / VCF 过滤 | 必需 |
| `plink`（1.9） | GWAS 的 VCF↔PLINK 转换与质控 | 必需 |
| `fastp` | Stage 0.5 FASTQ 质控 | 可选 |
| `numactl` | NUMA 绑定 | 可选 |
| `multiqc` | 聚合 QC 报告 | 可选 |
| `openjdk`（11.x） | GATK 与 Haploview 的运行环境 | 必需 |

> ⚠️ **本机 `Agent` 环境里没有 multiqc**，需要时手动装。
> ⚠️ `numactl` 亦可走系统包（`yum install numactl`）。
> ⚠️ vcftools 本机**缺失**，技能已写明用 `plink --vcf --make-bed` 替代。

### 1.4 GWAS 工具链 —— conda 环境 `r-gwas` + 独立二进制

```bash
conda env create -f environment/r-gwas.yml      # R 4.3.3 + rMVP/CMplot/pheatmap/reshape2/bigmemory
```

| 组件 | 用于 | 必需性 |
|---|---|---|
| **R ≥4.0**（本机 r-gwas 为 4.3.3） | 除 GEMMA 外所有 GWAS 阶段 | 必需。系统 R 3.6 编译新版依赖会失败 |
| `rMVP`（+ `bigmemory`/`RcppArmadillo`/`dplyr`） | S3 多方法关联 | 必需。**`dplyr` 本机 r-gwas 未装** |
| `CMplot`、`pheatmap`、`reshape2` | S1 热图 / S4 曼哈顿·QQ 图 | 必需 |
| `gxx_linux-64`（C++ 工具链） | 编译 rMVP 的 Rcpp 依赖 | 必需。**本机 r-gwas 未装 g++**，装包会报 `GLIBCXX`/C++14 |
| `gemma` | S2 LMM 关联 | 必需（独立二进制，不在 conda） |
| `gcta64` | S1 GRM/PCA | 可选（可用 GEMMA `-gk 2` + plink `--pca` 替代） |
| `vcftools` | VCF→PLINK | 可选（用 plink 替代） |
| `LDBlockShow` / `ShowLDSVG` | S5 区域 LD block | 必需（源码编译安装到 `~/.local/bin`，`command -v` 探测） |
| `Haploview.4.2.jar`（需 Java） | S5 单倍型 Block | 必需（`install.sh` 下载到 `~/.local/bin/Haploview.4.2.jar`，`java -jar` 运行） |
| `PopLDdecay`、`Plot_OnePop.pl`（perl） | S0.5 LD 衰减 | 可选 |
| ImageMagick `convert` / `librsvg` | SVG→PNG | 可选（有 SVG 兜底） |
| **新版 `libstdc++`（GLIBCXX ≥3.4.29）** | LDBlockShow 等新编译二进制的运行期库 | 视系统而定。技能用 `sk_ensure_ld_path`（`skills/_shared/env.sh`）**检测后再决定**是否前插 conda `lib`——系统库够新的机器不会被遮蔽 |

### 1.5 表型提取 —— conda 环境 **`Phenotype`**

`phenotype-extract` 的脚本跑在专用的 conda 环境 **`Phenotype`**（环境名不可改，技能按
`conda run -n Phenotype` 调用），依赖**远超** `requirements.txt`：

```bash
conda create -n Phenotype -c conda-forge python=3.13 pip
conda run -n Phenotype pip install \
  torch==2.14.0 transformers==4.57.6 numpy==2.3.5 pandas==2.3.3 scipy==1.16.3 \
  opencv-python-headless==5.0.0.93 pillow huggingface-hub==0.36.2 openpyxl==3.1.5 xlrd==2.0.2
```

| 包 | 用途 |
|---|---|
| `torch`（CUDA 构建） | 加载 `.pth` 权重、推理 |
| `transformers` | Mask2Former 模型结构/图像处理器（≥4.40） |
| `pillow` | `Mask2FormerImageProcessor` 硬依赖，装 `opencv` 不会带进来 |
| `opencv-python-headless` | 读图、黑底板抠 ROI（提供 `cv2`） |
| `numpy` / `pandas` / `scipy` | 数值、CSV、填洞 |
| `huggingface-hub` | 模型冷启动下载 |
| `openpyxl` / `xlrd` | 读 `.xls` 关联表（NIR 真值） |

外加：

- **HF 缓存**：`facebook/mask2former-swin-small-ade-semantic`（骨架权重，默认离线 `local_files_only`）
- **模型权重**：`skills/phenotype-extract/model/best_model.pth`（**263MB，未随仓库分发**）

```bash
cd skills/phenotype-extract/scripts
conda run -n Phenotype python phenotype_extract.py            # 列出可用表型
conda run -n Phenotype python phenotype_extract.py --pheno IMF # 提取
```

> 运行前建议 `export PYTHONNOUSERSITE=1`：本机 `~/.local/lib/python3.13/site-packages` 会经
> user-site 混入 3.13 环境的 `sys.path`，可能带来意外包。安装器不处理用户目录。

> 权重不只是「放到某个路径」——必须在数据库 `models` 表里**注册**（默认库
> `$PIPELINE_DB` 或 `$GENO_DIR/pipeline.db`（`GENO_DIR` 缺省 `~/genomics_data`）；
> 参考记录 `imf_mask2former`），脚本按 `model_names` 查库取 `model_path`。只放文件不注册 = 找不到模型。

> **数据路径环境变量**（技能与脚本均支持，便于换机器不改代码）：
> `GENO_DIR`、`IMAGE_DIR`、`PIPELINE_DB`。个体路径亦可用脚本参数覆盖
> （`--db/--outdir/--fam/--nir-xls/--corr-xls`）。

### 1.6 数据与外部服务

> 下表「默认位置」是历史布局；实际位置以 `GENO_DIR` / `PIPELINE_DB` 等环境变量为准，
> 或由 `db_query.py` 从数据库读出（推荐：参考基因组/FASTQ 走 DB，勿硬编码）。

| 项 | 默认位置 | 必需性 |
|---|---|---|
| `pipeline.db`（SQLite） | `$PIPELINE_DB` 或 `$GENO_DIR/pipeline.db`（`GENO_DIR` 缺省 `~/genomics_data`） | WGS/GWAS/表型都读它 |
| 参考基因组 | `ref.fa` + `.fai` + `.dict`（技能文档写 `reference/`，本机实为 `03_reference/`；从 DB `reference_genome` 读取） | WGS 形态必需 |
| known-sites VCF（SNP + INDEL） | 见 `run.params.json` | BQSR 必需 |
| FASTQ | `$GENO_DIR/fastq/<编号>/{编号}_1.fq.gz` 等 | WGS 输入 |
| 表型图片 | 由 `sample_images` 表的 `image_path` 指向（`$IMAGE_DIR`） | 表型提取输入 |
| `.fam` 排序基准 | 由 `--fam` / `$FAM_PATH` 指定（与基因型文件对齐） | 表型输出排序（缺则按样本号排） |
| **LLM API 出口** | `config.json` 的 `base_url`（默认 `https://api.anthropic.com`）或自建代理 | **必需** |

### 1.8 外部网络出口（按技能）

| 技能 | 需要的出口 |
|---|---|
| Agent 本体 | `api.anthropic.com`（或你的代理端点） |
| `phenotype-extract` | 冷启动需 `huggingface.co` 或镜像 `HF_ENDPOINT=https://hf-mirror.com` |
| Parabricks | `nvcr.io`（NGC 镜像，需 `docker login`） |

---

## 2. 从零部署（推荐顺序）

```bash
# ── 0) 取代码 ─────────────────────────────────────────────
git clone https://github.com/rua111555/TraitPilot.git ~/TraitPilot
cd ~/TraitPilot        # 仓库根即项目根（含 skills/、run.py、environment/），不要再进子目录

# ── 1) conda（没有就先装 Miniconda，用户态）───────────────
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p ~/miniconda3 && ~/miniconda3/bin/conda init bash

# ── 2) 凭证 ───────────────────────────────────────────────
cp config.json.example config.json
#   编辑填 api_key / base_url / model（也可改用 ANTHROPIC_AUTH_TOKEN 环境变量）

# ── 3) 一键装齐三套环境 + 本地软件 ────────────────────────
bash environment/install.sh --list    # 先看要装什么、什么版本
bash environment/install.sh           # 安装（--yes 无人值守；--cpu 装 CPU 版 torch）

# ── 4) 自检 ──────────────────────────────────────────────
bash environment/check_env.sh

# ── 5) 数据路径：技能已用 command -v 运行时探测工具，通常无需改任何路径 ──
#   数据不在默认位置时，用环境变量覆盖即可（完全不用改代码）：
export GENO_DIR=/your/data/geno  IMAGE_DIR=/your/data/images
export PIPELINE_DB=/your/data/geno/pipeline.db

# ── 6) 数据就位：pipeline.db / ref.fa / FASTQ / 表型图片 / 模型权重(+注册)
#      并把 DB 的 models.model_path 指向本机 best_model.pth（见 §3）
```

> **克隆后仍缺、需自备的文件**（`.gitignore` 排除或本就不随仓库分发）：
>
> | 缺的东西 | 怎么补 |
> |---|---|
> | `config.json`（含凭证） | `cp config.json.example config.json` 后填 `api_key`（也可用 `ANTHROPIC_AUTH_TOKEN`） |
> | `skills/phenotype-extract/model/best_model.pth`（~263MB） | 自行提供；`mkdir -p skills/phenotype-extract/model` 后放入，并在 `pipeline.db` 的 `models` 表注册（`model_path` 指向本机实际路径） |
> | `pipeline.db` 与数据 | 参考基因组 `ref.fa`(+`bwa index`/`faidx`/`.dict`)、FASTQ、表型图片——按 `$GENO_DIR` 布局放置 |

> **无需任何路径改写工具。** 技能里的工具路径是**运行时**用 `command -v` 从你当前的 conda
> 环境探测的，数据/环境位置由上面这批环境变量决定——「读用户自己的环境、自己适配」，因此
> 没有、也不需要 `replace_paths.py` 这类批量替换脚本。

---

## 3. 常见坑（本机实测踩过的）

| 现象 | 根因 | 处理 |
|---|---|---|
| `import numpy` 报 `GLIBCXX_3.4.29 not found` | 系统 libstdc++ 太旧 | 运行前加 `LD_LIBRARY_PATH=<conda>/lib`（要**前插**，追加无效——`/usr/lib64` 在前会先命中旧库） |
| LDBlockShow 起不来（同上 GLIBCXX） | 新编译二进制需要新 libstdc++ | 同上 |
| `wait: -n: invalid option` | 系统 bash 4.2 < 4.3 | 用 conda 里的 bash，或升级系统 bash |
| `library(rMVP)` 编译崩溃 / `C++14 required` | r-gwas 缺 g++ 工具链 | `conda install -n r-gwas -c conda-forge gxx_linux-64` |
| rMVP 报缺 `dplyr` | r-gwas 未装 | `Rscript -e "install.packages('dplyr')"` |
| `library(rMVP)` 在系统 R 上失败 | 系统 R 3.6 | 用 conda 环境 `r-gwas` 的 R ≥4.0 |
| 表型提取找不到模型 | `models.model_path` 指向旧路径，或权重没注册 | 更新 `UPDATE models SET model_path='<本机路径>' WHERE name='imf_mask2former'`；无记录则按 `imf_mask2former` 写法 `INSERT` |
| 表型提取报缺 `PIL` | `Mask2FormerImageProcessor` 需 pillow，装 opencv 不带它 | `conda run -n Phenotype pip install pillow` |
| 表型提取 import 到怪包（cairosvg 等） | Python 3.13 环境的 `sys.path` 含 `~/.local/lib/python3.13/site-packages`（user-site） | 运行前 `export PYTHONNOUSERSITE=1` |
| `FTS5` 不可用 | sqlite 编译未开 FTS5 或版本 <3.34 | 换 conda 的 python；或不用 `--retrieval`/`--memory` |
| Parabricks 容器起不来（`cuda>=12.8`） | 驱动太旧配不上镜像 tag | 见 `skills/wgs-expressive-pipeline/references/parabricks-setup.md` §0.1 兼容表；本机 550.67 只能用 `4.3.2-1` |
| docker 权限不足 | 不在 docker 组 | `sudo usermod -aG docker $USER` 后**重新登录** |
| `gemma`/`gcta64`/`LDBlockShow` `command -v` 找不到 | `~/.local/bin` 不在 PATH | `install.sh` 已写入 shell rc；当前会话 `export PATH="$HOME/.local/bin:$PATH"` 后重登 |
| `Haploview` 报找不到 jar | 未下载 / 不在预期路径 | `java -jar ~/.local/bin/Haploview.4.2.jar`（`install.sh` 会下载） |

---

## 4. 本目录内容

| 文件 | 说明 |
|---|---|
| `README.md` | 本文（部署总览） |
| `install.sh` | **一键安装**：按本目录规格装齐所有虚拟环境 / 软件 / 版本（`--list` 看规格） |
| `check_env.sh` | 环境自检脚本（九块检查 + 安装提示） |
| `Agent.yml` | conda 环境 `Agent` 的完整导出（`conda env export -n Agent --no-builds`） |
| `r-gwas.yml` | conda 环境 `r-gwas` 的完整导出 |

> `*.yml` 是**本机实况导出**，含大量传递依赖且频道写死为清华镜像，**直接导入常解不开**
> （实测 `Agent.yml` 会 `LibMambaUnsatisfiableError`）。因此 `install.sh` 用的是 §1.3/§1.4
> 的**最小集公式**（实测可解），`*.yml` 仅作复刻本机的参考。
