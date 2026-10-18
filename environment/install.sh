#!/usr/bin/env bash
# ============================================================================
# SkillAgent 一键环境安装
#
# 本脚本只做一件事：把「跑通整个项目」所需的虚拟环境、软件与版本装到位。
# 不处理网络代理/镜像，不改系统配置，不用 sudo（除 --with-system 明确要求）。
#
#   bash environment/install.sh                 # 全部安装（会先让你确认）
#   bash environment/install.sh --yes           # 无人值守
#   bash environment/install.sh --list          # 只打印「要装什么、什么版本」
#   bash environment/install.sh --only agent    # 只装某一层
#   bash environment/install.sh --skip pheno    # 跳过某一层
#   bash environment/install.sh --conda ~/miniconda3
#
# 安装内容（详见下方各规格表）：
#   ① conda 虚拟环境 Agent      WGS/GWAS 管线 + harness 本体（requests/PyYAML/flask）
#   ② conda 虚拟环境 r-gwas     GWAS 的 R 关联/出图
#   ③ conda 虚拟环境 Phenotype  表型提取的 ML 栈（torch/transformers…）
#   ④ 本地额外软件              独立二进制 + 命令行工具
# ============================================================================
set -euo pipefail

# ────────────────────────────── 版本规格（唯一事实源） ──────────────────────

# 【① conda 虚拟环境 Agent】WGS 管线用。环境名必须是 Agent（技能脚本硬编码此名）。
AGENT_ENV_NAME="Agent"
AGENT_SPEC=(
  "python=3.12"        # 3.12.13
  "bwa=0.7.19"         # 比对
  "samtools=1.23"      # 排序/索引
  "bcftools=1.23.1"    # VCF 处理
  "htslib=1.23"        # 提供 tabix/bgzip
  "gatk4=4.3.0.0"      # 变异检测（需 java）
  "fastp=1.1.0"        # FASTQ 质控
  "plink=1.9"          # GWAS 格式转换/质控
  "numactl=2.0.18"     # NUMA 绑定
  "openjdk=11"         # GATK / Haploview 运行环境
)

# 【① conda 虚拟环境 r-gwas】GWAS 出图与关联用。环境名必须是 r-gwas。
RGWAS_ENV_NAME="r-gwas"
RGWAS_SPEC=(
  "r-base=4.3.3"       # 必须 ≥4.0（系统 R 3.6 编译新版依赖会失败）
  "r-recommended=4.3"
  "gcc_linux-64=16.2.0"    # 编译 RcppArmadillo 用
  "gxx_linux-64=16.2.0"
)
# r-gwas 里另需的 CRAN 包（conda R 频道无对应包，走 install.packages）
RGWAS_CRAN=(
  "rMVP"               # 1.4.6  多方法关联
  "CMplot"             # 4.5.1  曼哈顿/QQ 图
  "pheatmap"           # 1.0.13 热图
  "reshape2"           # 1.4.5
  "bigmemory"          # 4.6.6  rMVP 依赖
  "dplyr"              # 1.2.1
  "RcppArmadillo"      # 15.6.0.1 rMVP 编译依赖
)

# 【② conda 虚拟环境 Phenotype】表型提取（phenotype-extract）专用。
#    环境名必须是 Phenotype（技能脚本按此名 conda run -n Phenotype）。
PHENO_ENV_NAME="Phenotype"
PHENO_SPEC=(
  "python=3.13"
  "pip"
)
# Phenotype 里另需的 pip 包（含精确版本，保证与已跑通的组合一致）
PHENO_PIP=(
  "torch==2.14.0"                 # cu126 构建；无 GPU 用 --cpu 换 CPU 版
  "transformers==4.57.6"          # ≥4.40 才有 Mask2Former
  "numpy==2.3.5"
  "pandas==2.3.3"
  "scipy==1.16.3"
  "opencv-python-headless==5.0.0.93"   # 提供 cv2
  "pillow"                        # Mask2FormerImageProcessor 必需
  "huggingface-hub==0.36.2"
  "openpyxl==3.1.5"
  "xlrd==2.0.2"
)
PHENO_TORCH_INDEX="https://download.pytorch.org/whl/cu126"

# 【harness 本体 pip 依赖】装进 Agent 虚拟环境（Agent 本体与 WGS/GWAS 共用此环境）
HARNESS_PKGS=(
  "requests>=2.31"     # 2.33.1
  "PyYAML>=6.0"        # 6.0.3
  "flask>=3.0"         # 3.1.3 仅 Web 前端
)

# 独立二进制的下载源（curl 取文件）。gemma/gcta64/LDBlockShow/PopLDdecay 为可执行二进制；
# Haploview 是 Java jar，用 `java -jar Haploview.4.2.jar` 运行（见 gwas-ld-block 技能）。
declare -A BIN_URL=(
  [gemma]="https://github.com/genetics-statistics/GEMMA/releases/download/v0.98.5/gemma-0.98.5-linux-static-AMD64.gz"
  [gcta64]="https://yanglab.westlake.edu.cn/software/gcta/bin/gcta-1.95.4-linux-x86_64.zip"
  [LDBlockShow]="https://github.com/BGI-shenzhen/LDBlockShow/archive/refs/tags/v1.41.tar.gz"
  [PopLDdecay]="https://github.com/BGI-shenzhen/PopLDdecay/archive/refs/tags/v3.42.tar.gz"
  [Haploview.4.2.jar]="https://www.broadinstitute.org/ftp/pub/mpg/haploview/Haploview.jar"
)
BIN_DIR="${HOME}/.local/bin"          # 独立二进制落点（须在 PATH 中，装完由 do_verify 写入 shell rc）

# 【③ 本地额外软件 —— 命令行工具】经 conda 装到 base（用户态）
LOCAL_TOOLS=(
  "perl"               # ShowLDSVG.pl / Plot_OnePop.pl 运行
  "imagemagick"        # convert：SVG→PNG
  "poppler"            # pdftotext：PDF 文本校验
  "ripgrep"            # rg：Grep 工具加速
  "bash>=4.3"          # wait -n 并发池（系统 bash 常 <4.3）
  "vcftools"           # VCF→PLINK（可选，plink 可替代）
)

# 期望版本（仅用于 --list 展示与安装后核对）
EXPECT_AGENT="python 3.12.13 | bwa 0.7.19 | samtools 1.23 | bcftools 1.23.1 | gatk4 4.3.0.0 | fastp 1.1.0 | plink 1.9.0b.7.7 | openjdk 11.0.1"
EXPECT_RGWAS="R 4.3.3 | rMVP 1.4.6 | CMplot 4.5.1 | pheatmap 1.0.13 | reshape2 1.4.5 | bigmemory 4.6.6 | dplyr 1.2.1"
EXPECT_PHENO="torch 2.14.0 | transformers 4.57.6 | numpy 2.3.5 | pandas 2.3.3 | scipy 1.16.3 | cv2 5.0.0.93"

# ────────────────────────────── 参数 ────────────────────────────────────────
YES=0; LIST_ONLY=0; CPU=0; WITH_SYSTEM=0; DRY=0
CONDA="${CONDA_PREFIX:-}"
ONLY=""; SKIP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)      YES=1 ;;
    --list)        LIST_ONLY=1 ;;
    --cpu)         CPU=1 ;;
    --with-system) WITH_SYSTEM=1 ;;
    --dry-run)     DRY=1; YES=1 ;;
    --conda)       CONDA="$2"; shift ;;
    --only)        ONLY="$2"; shift ;;
    --skip)        SKIP="$2"; shift ;;
    -h|--help)     sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
  shift
done

c() { printf "\033[%sm%s\033[0m\n" "$1" "$2"; }
step() { c "1;36" "
▶ $*"; }
ok()   { c "32"   "  ✓ $*"; }
warn() { c "33"   "  ! $*"; }
die()  { c "31"   "  ✗ $*"; exit 1; }

want() {  # want <层名> —— 是否应执行该层
  [ -n "$ONLY" ] && [ "$ONLY" != "$1" ] && return 1
  [ -n "$SKIP" ] && [ "$SKIP" = "$1" ] && return 1
  return 0
}
confirm() { [ "$YES" = 1 ] && return 0; read -r -p "  继续？[y/N] " r; [ "${r,,}" = y ]; }

# ────────────────────────────── 0. 打印规格 / 预检 ──────────────────────────
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

print_spec() {
  c "1;37" "SkillAgent 环境安装规格"
  echo
  c "36" "① conda 虚拟环境：$AGENT_ENV_NAME   〔WGS 管线〕"
  echo "   包：${AGENT_SPEC[*]}"
  echo "   期望：$EXPECT_AGENT"
  echo
  c "36" "② conda 虚拟环境：$RGWAS_ENV_NAME   〔GWAS 管线〕"
  echo "   包：${RGWAS_SPEC[*]}"
  echo "   + CRAN：${RGWAS_CRAN[*]}"
  echo "   期望：$EXPECT_RGWAS"
  echo
  c "36" "③ conda 虚拟环境：$PHENO_ENV_NAME   〔表型提取的 ML 栈〕"
  echo "   包：${PHENO_SPEC[*]} + pip ${PHENO_PIP[*]}"
  echo "   期望：$EXPECT_PHENO"
  echo
  c "36" "④ harness 本体 pip 依赖（装进 $AGENT_ENV_NAME 环境）：${HARNESS_PKGS[*]}"
  echo
  c "36" "⑤ 本地额外软件 —— 独立二进制（装到 $BIN_DIR）"
  for k in "${!BIN_URL[@]}"; do echo "   $k  ←  ${BIN_URL[$k]}"; done
  echo
  c "36" "⑥ 本地额外软件 —— 命令行工具（conda base）"
  echo "   ${LOCAL_TOOLS[*]}"
  echo
  c "36" "⑦ 数据（本脚本不装，需另行放置）"
  echo "   pipeline.db · 参考基因组 ref.fa/.fai/.dict · FASTQ · 表型图片 · best_model.pth(263MB)"
  echo "   见 environment/README.md §1.6"
}

if [ "$LIST_ONLY" = 1 ]; then print_spec; exit 0; fi

print_spec
echo
[ "$YES" = 1 ] || confirm || { echo "已取消"; exit 0; }

# 探测 conda
if [ -z "$CONDA" ]; then
  for d in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/mambaforge" /opt/conda; do
    [ -x "$d/bin/conda" ] && { CONDA="$d"; break; }
  done
fi
[ -n "$CONDA" ] && [ -x "$CONDA/bin/conda" ] \
  || die "未找到 conda。请先装 Miniconda（https://repo.anaconda.com/miniconda/），或用 --conda <路径> 指定"
CONDA_BIN="$CONDA/bin/conda"
ok "conda：$CONDA（$("$CONDA_BIN" --version)）"
BASE_PY="$CONDA/bin/python3"; [ -x "$BASE_PY" ] || BASE_PY="$CONDA/bin/python"
ok "base python：$("$BASE_PY" --version 2>&1)"

# ────────────────────────────── 1. conda 环境 Agent ─────────────────────────
do_agent() {
  step "① conda 环境 $AGENT_ENV_NAME（${#AGENT_SPEC[@]} 个包）"
  echo "  ${AGENT_SPEC[*]}"
  if "$CONDA_BIN" env list | awk '{print $1}' | grep -qx "$AGENT_ENV_NAME"; then
    warn "环境已存在，跳过（要重建先 conda env remove -n $AGENT_ENV_NAME）"
  else
    [ "$DRY" = 1 ] && { echo "  [dry-run] conda create"; return; }
    "$CONDA_BIN" create -y -n "$AGENT_ENV_NAME" -c conda-forge -c bioconda --override-channels \
      "${AGENT_SPEC[@]}" >/dev/null || die "创建 $AGENT_ENV_NAME 失败"
    ok "$AGENT_ENV_NAME 创建完成"
  fi
  for t in bwa samtools bcftools tabix gatk fastp plink numactl java; do
    [ -x "$CONDA/envs/$AGENT_ENV_NAME/bin/$t" ] && ok "$t 就位" || warn "$t 缺失"
  done
  # harness 本体（requests/PyYAML/flask）装进 Agent 环境，与管线共用
  local APY="$CONDA/envs/$AGENT_ENV_NAME/bin/python"
  if [ -x "$APY" ]; then
    step "①b harness 本体 pip 依赖（装进 $AGENT_ENV_NAME 环境）"
    if [ "$DRY" = 1 ]; then echo "  [dry-run] pip install ${HARNESS_PKGS[*]}"; else
      "$APY" -m pip install --quiet "${HARNESS_PKGS[@]}" || die "harness pip 安装失败"
      "$APY" -c "import requests,yaml,flask" && ok "requests / PyYAML / flask 就位"
    fi
  else
    warn "Agent 环境无 python，跳过 harness 依赖"
  fi
}

# ────────────────────────────── 3. conda 环境 r-gwas ────────────────────────
do_rgwas() {
  step "③ conda 环境 $RGWAS_ENV_NAME（R + C++ 工具链 + CRAN 包）"
  if "$CONDA_BIN" env list | awk '{print $1}' | grep -qx "$RGWAS_ENV_NAME"; then
    warn "环境已存在，跳过创建"
  else
    [ "$DRY" = 1 ] && { echo "  [dry-run] conda create -n $RGWAS_ENV_NAME ${RGWAS_SPEC[*]}"; return; }
    "$CONDA_BIN" create -y -n "$RGWAS_ENV_NAME" -c conda-forge --override-channels \
      "${RGWAS_SPEC[@]}" >/dev/null || die "创建 $RGWAS_ENV_NAME 失败"
    ok "$RGWAS_ENV_NAME 创建完成"
  fi
  local RS="$CONDA/envs/$RGWAS_ENV_NAME/bin/Rscript"
  [ -x "$RS" ] || { warn "Rscript 不存在，跳过 CRAN 包"; return; }
  echo "  R：$("$RS" -e 'cat(R.version.string)' 2>/dev/null | tail -1)"
  [ "$DRY" = 1 ] && { echo "  [dry-run] install.packages(${RGWAS_CRAN[*]})"; return; }
  for p in "${RGWAS_CRAN[@]}"; do
    "$RS" -e "if(!requireNamespace('$p',quietly=TRUE)) install.packages('$p',repos='https://cloud.r-project.org')" \
      >/dev/null 2>&1 || warn "$p 安装可能失败"
    "$RS" -e "quit(status=!requireNamespace('$p',quietly=TRUE))" >/dev/null 2>&1 \
      && ok "$p" || warn "$p 不可用"
  done
}

# ────────────────────────────── 3. conda 环境 Phenotype（表型 ML 栈） ───────
do_pheno() {
  step "③ conda 环境 $PHENO_ENV_NAME（表型提取的 ML 栈）"
  echo "  ${PHENO_SPEC[*]} + pip: ${PHENO_PIP[*]}"
  if "$CONDA_BIN" env list | awk '{print $1}' | grep -qx "$PHENO_ENV_NAME"; then
    warn "环境已存在，跳过创建（要重建先 conda env remove -n $PHENO_ENV_NAME）"
  else
    [ "$DRY" = 1 ] && { echo "  [dry-run] conda create -n $PHENO_ENV_NAME ${PHENO_SPEC[*]}"; return; }
    "$CONDA_BIN" create -y -n "$PHENO_ENV_NAME" -c conda-forge --override-channels \
      "${PHENO_SPEC[@]}" >/dev/null || die "创建 $PHENO_ENV_NAME 失败"
    ok "$PHENO_ENV_NAME 创建完成"
  fi
  local PPY="$CONDA/envs/$PHENO_ENV_NAME/bin/python"
  [ -x "$PPY" ] || { warn "Phenotype 环境无 python，跳过 pip 包"; return; }
  [ "$DRY" = 1 ] && { echo "  [dry-run] pip install torch(+pytorch 源) 与 ${PHENO_PIP[*]}"; return; }
  # torch 走 pytorch 官方源（该源只托管 torch/numpy 等少数包）；
  # 其余包必须走 PyPI——否则 transformers/pandas/scipy/... 在 pytorch 源里取不到。
  local torch_idx="$PHENO_TORCH_INDEX"
  [ "$CPU" = 1 ] && torch_idx="https://download.pytorch.org/whl/cpu"
  "$PPY" -m pip install --quiet --index-url "$torch_idx" "torch==2.14.0" \
    || die "torch 安装失败（源：$torch_idx）"
  local rest=()
  for p in "${PHENO_PIP[@]}"; do case "$p" in torch==*) ;; *) rest+=("$p");; esac; done
  "$PPY" -m pip install --quiet --index-url https://pypi.org/simple "${rest[@]}" \
    || die "ML 栈安装失败（PyPI 源）"
  for spec in "numpy" "pandas" "scipy" "cv2:opencv-python-headless" "torch" "transformers" \
              "huggingface_hub:huggingface-hub" "openpyxl" "xlrd"; do
    m="${spec%%:*}"; p="${spec##*:}"
    v="$("$PPY" -c "import importlib.metadata as m;print(m.version('$p'))" 2>/dev/null)" \
      && "$PPY" -c "import $m" >/dev/null 2>&1 && ok "$p ${v:-}" || warn "$p 不可导入"
  done
}

# ────────────────────────────── 5. 独立二进制 ───────────────────────────────
do_bins() {
  step "⑤ 本地额外软件 —— 独立二进制（装到 $BIN_DIR）"
  mkdir -p "$BIN_DIR"
  local src="$HOME/.local/src"; mkdir -p "$src"
  if [ "$DRY" = 1 ]; then
    for k in "${!BIN_URL[@]}"; do echo "  [dry-run] $k ← ${BIN_URL[$k]}"; done
    return
  fi

  # GEMMA —— 静态二进制，解压即用
  if ! command -v gemma >/dev/null 2>&1; then
    ( cd "$src" && curl -fL -o gemma.gz "${BIN_URL[gemma]}" \
      && gunzip -f gemma.gz && chmod +x gemma && mv -f gemma "$BIN_DIR/gemma" ) \
      && ok "gemma → $BIN_DIR/gemma" || warn "gemma 下载失败（请自行下载后放入 $BIN_DIR）"
  else ok "gemma 已存在"; fi

  # GCTA —— 静态二进制
  if ! command -v gcta64 >/dev/null 2>&1; then
    ( cd "$src" && curl -fL -o gcta.zip "${BIN_URL[gcta64]}" \
      && unzip -oq gcta.zip && chmod +x gcta-*/gcta64 2>/dev/null \
      && mv -f gcta-*/gcta64 "$BIN_DIR/gcta64" ) \
      && ok "gcta64 → $BIN_DIR/gcta64" || warn "gcta64 下载失败（可自行下载后放入 $BIN_DIR）"
  else ok "gcta64 已存在"; fi

  # LDBlockShow（含 ShowLDSVG.pl）—— 源码编译；运行需新版 libstdc++
  if ! command -v LDBlockShow >/dev/null 2>&1; then
    ( cd "$src" && curl -fL -o ldb.tar.gz "${BIN_URL[LDBlockShow]}" \
      && tar xzf ldb.tar.gz && cd LDBlockShow-1.41/src && make >/dev/null 2>&1 \
      && chmod 755 ../bin/* \
      && ln -sf "$src/LDBlockShow-1.41/bin/LDBlockShow" "$BIN_DIR/LDBlockShow" \
      && ln -sf "$src/LDBlockShow-1.41/bin/ShowLDSVG"   "$BIN_DIR/ShowLDSVG" ) \
      && ok "LDBlockShow / ShowLDSVG → $BIN_DIR" \
      || warn "LDBlockShow 编译失败（需 zlib.h；请自行编译后放入 $BIN_DIR）"
  else ok "LDBlockShow 已存在"; fi

  # PopLDdecay —— 源码编译
  if ! command -v PopLDdecay >/dev/null 2>&1; then
    ( cd "$src" && curl -fL -o pld.tar.gz "${BIN_URL[PopLDdecay]}" \
      && tar xzf pld.tar.gz && cd PopLDdecay-3.42/src && make >/dev/null 2>&1 \
      && mv -f PopLDdecay "$BIN_DIR/PopLDdecay" ) \
      && ok "PopLDdecay → $BIN_DIR" || warn "PopLDdecay 编译失败"
  else ok "PopLDdecay 已存在"; fi

  # Haploview —— Java jar（非可执行二进制），`java -jar Haploview.4.2.jar` 运行
  if [ ! -f "$BIN_DIR/Haploview.4.2.jar" ]; then
    ( cd "$src" && curl -fL --proto '=https,ftp' -o Haploview.4.2.jar "${BIN_URL[Haploview.4.2.jar]}" \
      && mv -f Haploview.4.2.jar "$BIN_DIR/Haploview.4.2.jar" ) \
      && ok "Haploview.4.2.jar → $BIN_DIR/Haploview.4.2.jar（用 java -jar 运行）" \
      || warn "Haploview.4.2.jar 下载失败（请自行下载后放入 $BIN_DIR）"
  else ok "Haploview.4.2.jar 已存在"; fi

  # 独立二进制必须能被 command -v 找到：把 BIN_DIR 落进 shell rc（并立即导出）
  export PATH="$BIN_DIR:$PATH"
  local rc line="export PATH=\"$BIN_DIR:\$PATH\""
  for rc in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    if [ -e "$rc" ] && ! grep -qsF "$line" "$rc"; then echo "$line" >> "$rc"; ok "已写入 $rc：$line"; fi
  done
  warn "gemma/gcta64/LDBlockShow 若报 GLIBCXX_3.4.29：说明系统 libstdc++ 过旧，需前插 conda lib（技能用 sk_ensure_ld_path 自动检测）"
}

# ────────────────────────────── 6. 本地命令行工具 ───────────────────────────
do_tools() {
  step "⑥ 本地额外软件 —— 命令行工具（perl / convert / pdftotext / rg / bash≥4.3 / vcftools）"
  echo "  ${LOCAL_TOOLS[*]}"
  if [ "$DRY" = 1 ]; then echo "  [dry-run] conda install -n base ..."; return; fi
  # 已有则跳过（--with-system 时连系统版也接受）
  local missing=()
  for spec in "${LOCAL_TOOLS[@]}"; do
    cmd="${spec%%[<>=]*}"
    case "$cmd" in
      imagemagick) cmd=convert ;;
      poppler)     cmd=pdftotext ;;
      ripgrep)     cmd=rg ;;
    esac
    command -v "$cmd" >/dev/null 2>&1 && ok "$cmd 已存在（$(command -v "$cmd")）" || missing+=("$spec")
  done
  [ "${#missing[@]}" -eq 0 ] && return
  "$CONDA_BIN" install -y -n base -c conda-forge --override-channels "${missing[@]}" >/dev/null \
    || warn "部分工具安装失败：${missing[*]}"
  for spec in "${missing[@]}"; do ok "已尝试安装 $spec"; done
  warn "conda 装的 bash 在 $CONDA/bin/bash（系统 bash <4.3 时编排脚本用它）"
}

# ────────────────────────────── 7. 收尾 ─────────────────────────────────────
do_verify() {
  step "⑦ 自检"
  if [ -x "$ROOT/environment/check_env.sh" ]; then
    bash "$ROOT/environment/check_env.sh" || warn "自检有核心缺失项，见上"
  else
    warn "未找到 environment/check_env.sh"
  fi
  step "数据/环境位置（用环境变量覆盖，无需改代码）"
  echo "  技能里的工具已用 command -v 运行时探测；数据/环境位置用环境变量指定即可："
  echo "    export GENO_DIR=<数据根>  IMAGE_DIR=<图片根>  PIPELINE_DB=<pipeline.db>"
}

# ────────────────────────────── 主流程 ─────────────────────────────────────
if want agent; then do_agent; fi   # 含 harness 本体 pip 依赖
if want rgwas; then do_rgwas; fi
if want pheno; then do_pheno; fi
if want bins;  then do_bins;  fi
if want tools; then do_tools; fi
do_verify

echo
c "1;32" "安装流程结束。"
echo "下一步："
echo "  python3 run.py list                 # 应列出技能"
echo "  bash environment/check_env.sh       # 复核缺失项"
echo "  数据（pipeline.db / 参考基因组 / 图片 / best_model.pth）需另行放置，见 environment/README.md"
