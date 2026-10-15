# 环境与部署（ENVIRONMENT）

> **这份文档面向「把 SkillAgent 装到一台新机器上的人」，不是本机备忘。**
> 它列的是**跑通整个项目**所需的环境；`requirements.txt` 只是其中最小的一块（Agent 本体的三个
> pip 包），**远不能代表全貌** —— 21 个技能背后还有两套 conda 环境、若干独立二进制、
> R 包、docker/GPU 栈、参考基因组与数据库。

先跑自检，缺什么一目了然：

```bash
cd agent
bash environment/check_env.sh          # 全量
bash environment/check_env.sh --core   # 只查 Agent 本体（不碰生信管线）
bash environment/check_env.sh --quiet  # 只打印缺失项
```

它按九个块检查并给出安装提示；末尾汇总「核心缺失 / 可选缺失」，核心有缺则返回非零退出码。

---

## 0. 三分钟速览：这项目到底需要什么

| 层 | 内容 | 必需性 |
|---|---|---|
| **A. Agent 本体** | Python ≥3.10（实测 3.13）+ `requests` / `PyYAML` / `flask` + 一个 LLM API 凭证 | **必需** |
| **B. 平台** | Linux/POSIX：`/bin/bash`、`nohup`、`curl`、`pgrep/pkill`；`sqlite3` 带 FTS5（仅检索/记忆用） | **必需** |
| **C. WGS 工具链** | conda 环境 **`Agent`**：bwa · samtools · bcftools · htslib(tabix) · gatk4 · fastp · plink · numactl · openjdk | 跑 WGS 管线时必需 |
| **D. GWAS 工具链** | conda 环境 **`r-gwas`**（R 4.3 + rMVP/CMplot/pheatmap/reshape2/bigmemory…）+ 独立二进制 gemma / gcta64 / LDBlockShow / ShowLDSVG / PopLDdecay + Haploview.jar + perl | 跑 GWAS 管线时必需 |
| **E. 表型提取** | conda **base** 里的 torch/transformers/numpy/pandas/opencv/scipy + HF 缓存 + 模型权重 + `LD_LIBRARY_PATH` 前缀 | 跑 phenotype-extract 时必需 |
| **F. 数据** | `pipeline.db`、参考基因组 `ref.fa/.fai/.dict`、FASTQ、表型图片、known-sites VCF | 跑真实数据时必需 |
| **G. GPU 加速（可选）** | NVIDIA 驱动 ≥545 + docker + nvidia-container-toolkit + `clara-parabricks` NGC 镜像 | **可选**，缺了自动回退 CPU |
| **H. 三个信息类技能** | `agent-news` / `paper-search` / `fan-files`：脚本**不在本仓库**，需另外放置 | 用它们时必需 |

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

```bash
pip install -r requirements.txt
# 跑测试再加： pip install pytest
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

技能里的脚本**硬编码**了绝对路径（如 `/home/chenyuxi/anaconda3/envs/Agent/bin/gatk`），
环境名必须叫 **`Agent`**，且路径要与脚本一致（换了路径就用 `replace_paths.py` 批量改）。

```bash
# 方式一（推荐，完全复刻本机）：已导出到 environment/Agent.yml
conda env create -f environment/Agent.yml

# 方式二（手动最小集）
conda create -n Agent -c bioconda -c conda-forge -y \
  bwa=0.7.19 samtools=1.23 bcftools=1.23.1 htslib=1.23 \
  gatk4=4.3.0.0 plink fastp numactl openjdk python=3.12
conda install -n Agent -c bioconda multiqc        # 可选：聚合 fastp QC 报告
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
| `LDBlockShow` / `ShowLDSVG` | S5 区域 LD block | 必需（源码编译；本机在 `/data1/yangqinghua/biosoft/LDBlockShow`） |
| `Haploview.4.2.jar`（需 Java） | S5 单倍型 Block | 必需 |
| `PopLDdecay`、`Plot_OnePop.pl`（perl） | S0.5 LD 衰减 | 可选 |
| ImageMagick `convert` / `librsvg` | SVG→PNG | 可选（有 SVG 兜底） |
| **新版 `libstdc++`（GLIBCXX ≥3.4.29）** | LDBlockShow 等新编译二进制的运行期库 | 必需。本机系统 `/usr/lib64` 太旧，故需 `LD_LIBRARY_PATH=<conda>/lib` |

### 1.5 表型提取 —— conda **base**（不是 Agent 环境！）

`phenotype-extract` 的脚本用的是 **base** 解释器，依赖**远超** `requirements.txt`：

```bash
pip install numpy pandas opencv-python scipy torch transformers \
            huggingface-hub openpyxl xlrd
```

| 包 | 用途 |
|---|---|
| `torch`（CUDA 构建） | 加载 `.pth` 权重、推理 |
| `transformers` | Mask2Former 模型结构/图像处理器 |
| `opencv-python` | 读图、黑底板抠 ROI | 
| `numpy` / `pandas` / `scipy` | 数值、CSV、填洞 |
| `huggingface-hub` | 模型冷启动下载 |
| `openpyxl` / `xlrd` | 读 `.xls` 关联表（NIR 真值） |

外加：

- **HF 缓存**：`facebook/mask2former-swin-small-ade-semantic`（骨架权重，默认离线 `local_files_only`）
- **模型权重**：`skills/phenotype-extract/model/best_model.pth`（**263MB，未随仓库分发**）
- **`LD_LIBRARY_PATH=<conda>/lib`**：本机系统 libstdc++ 太旧，不加这前缀 `import numpy/torch` 直接报 `GLIBCXX_3.4.29 not found`

```bash
cd skills/phenotype-extract/scripts
LD_LIBRARY_PATH=/home/chenyuxi/anaconda3/lib python3 phenotype_extract.py            # 列出可用表型
LD_LIBRARY_PATH=/home/chenyuxi/anaconda3/lib python3 phenotype_extract.py --pheno IMF # 提取
```

> 权重不只是「放到某个路径」——必须在数据库 `models` 表里**注册**（默认库
> `/data1/chenyx/Pig_geno/pipeline.db`，参考记录 `imf_mask2former`），脚本按 `model_names`
> 查库取 `model_path`。只放文件不注册 = 找不到模型。

### 1.6 数据与外部服务

| 项 | 默认位置 | 必需性 |
|---|---|---|
| `pipeline.db`（SQLite） | `/data1/chenyx/Pig_geno/pipeline.db` | WGS/GWAS/表型都读它 |
| 参考基因组 | `ref.fa` + `.fai` + `.dict`（**本机在 `03_reference/`，技能文档写的是 `reference/`**） | WGS 形态必需 |
| known-sites VCF（SNP + INDEL） | 见 `run.params.json` | BQSR 必需 |
| FASTQ | `/data1/chenyx/Pig_geno/fastq/<编号>/{编号}_1.fq.gz` 等 | WGS 输入 |
| 表型图片 | 由 `sample_images` 表的 `image_path` 指向（`/data1/chenyx/Pig_image/`） | 表型提取输入 |
| `.fam` 排序基准 | `/data1/chenyx/Pig_geno/04_snp_indel/et277.fm.snp.fam` | 表型输出排序（缺则按 et 号排） |
| **LLM API 出口** | `config.json` 的 `base_url`（默认 `https://api.anthropic.com`）或自建代理 | **必需** |

### 1.7 外部网络出口（按技能）

| 技能 | 需要的出口 |
|---|---|
| Agent 本体 | `api.anthropic.com`（或你的代理端点） |
| `agent-news` | `api.github.com`（Releases REST）、OpenAI RSS、Anthropic /news、`export.arxiv.org`、HF Blog（`hf-mirror.com` 兜底） |
| `paper-search` | OpenAlex（主）、Crossref（降级）、PMC/OA PDF 站点 |
| `phenotype-extract` | 冷启动需 `huggingface.co` 或镜像 `HF_ENDPOINT=https://hf-mirror.com` |
| `fan-files` | `raw.githubusercontent.com` / GitHub Releases（装它的二进制） |
| Parabricks | `nvcr.io`（NGC 镜像，需 `docker login`） |

### 1.8 三个「脚本不在本仓库」的技能

`skills/agent-news/`、`skills/paper-search/`、`skills/fan-files/` 的 SKILL.md 只描述流程，
**真正的脚本/配置在别处**，部署时需一并带过去，否则技能不可运行：

| 技能 | 需要的东西 | 本机位置 |
|---|---|---|
| `agent-news` | `agent_news.py` + `sources.json` + 产物目录 `agent_news/` +（可选）5 天一次 cron | `/home/chenyuxi/paper_search/` |
| `paper-search` | `paper_search.py` + `curate_run.py` + `venues.json`（**缺它脚本直接 FileNotFoundError**）+ `pdftotext` | `/home/chenyuxi/paper_search/` |
| `fan-files` | `fan-files` 二进制（`curl … install.sh` 装）+ 一次 LLM provider 配置 | `~/.local/bin/fan-files` |

---

## 2. 从零部署（推荐顺序）

```bash
# ── 0) 取代码 ─────────────────────────────────────────────
git clone <repo> ~/skill/agent && cd ~/skill/agent

# ── 1) Python + Agent 本体 ────────────────────────────────
#    没有 conda 先装 Miniconda；Agent 本体用哪个解释器都行（base 即可）
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p ~/miniconda3 && ~/miniconda3/bin/conda init bash
pip install -r requirements.txt

# ── 2) 凭证 ───────────────────────────────────────────────
cp config.json.example config.json
#   编辑填 api_key / base_url / model（也可改用 ANTHROPIC_AUTH_TOKEN 环境变量）

# ── 3) 自检（此时应「核心就绪」）────────────────────────────
bash environment/check_env.sh --core
python3 run.py list            # 应列出 21 个技能 → Agent 本体已通

# ── 4) 按需补管线环境 ─────────────────────────────────────
conda env create -f environment/Agent.yml     # WGS
conda env create -f environment/r-gwas.yml    # GWAS
pip install numpy pandas opencv-python scipy torch transformers huggingface-hub openpyxl xlrd   # 表型

# ── 5) 路径对齐（若你的 conda/数据路径与本机不同）───────────
python3 ../replace_paths.py --conda /home/新用户/miniconda3 --data /data1/新路径 --dry-run
python3 ../replace_paths.py --conda /home/新用户/miniconda3 --data /data1/新路径

# ── 6) 数据就位：pipeline.db / ref.fa / FASTQ / 表型图片 / 模型权重(+注册)
# ── 7) 再自检 ────────────────────────────────────────────
bash environment/check_env.sh
```

> 项目根 `agent/` 之外还有一个 `replace_paths.py`（与 `MIGRATE.md`、`conda_install_guide.md`
> 同级），专门用于把技能脚本里硬编码的 conda/数据绝对路径批量换成新机器的。

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
| 表型提取找不到模型 | 权重没在 `models` 表注册 | 按 `imf_mask2former` 记录 `INSERT INTO models(...)` |
| `FTS5` 不可用 | sqlite 编译未开 FTS5 或版本 <3.34 | 换 conda 的 python；或不用 `--retrieval`/`--memory` |
| Parabricks 容器起不来（`cuda>=12.8`） | 驱动太旧配不上镜像 tag | 见 `skills/wgs-expressive-pipeline/references/parabricks-setup.md` §0.1 兼容表；本机 550.67 只能用 `4.3.2-1` |
| docker 权限不足 | 不在 docker 组 | `sudo usermod -aG docker $USER` 后**重新登录** |
| `paper_search.py` 启动即崩 | 缺 `venues.json` | 把它放到脚本同级目录 |

---

## 4. 本目录内容

| 文件 | 说明 |
|---|---|
| `README.md` | 本文（部署总览） |
| `check_env.sh` | 环境自检脚本（九块检查 + 安装提示） |
| `Agent.yml` | conda 环境 `Agent` 的完整导出（`conda env export -n Agent --no-builds`） |
| `r-gwas.yml` | conda 环境 `r-gwas` 的完整导出 |

> `*.yml` 是**本机实况导出**，含大量传递依赖；跨平台导入时若个别包不可解，
> 按 §1.3 / §1.4 的表格手动建环境即可。
