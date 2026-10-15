#!/usr/bin/env bash
# SkillAgent 环境自检 —— 部署到新机器后先跑这个，缺什么一目了然。
#
#   bash environment/check_env.sh              # 全量检查
#   bash environment/check_env.sh --core       # 只查 Agent 本体（不查生信管线）
#   bash environment/check_env.sh --quiet      # 只打印缺失项
#
# 退出码：0 = 核心就绪；1 = 核心有缺（生信/表型缺项只告警不置非零，除非 --strict）
set -u

QUIET=0
CORE_ONLY=0
STRICT=0
for a in "$@"; do
  case "$a" in
    --quiet) QUIET=1 ;;
    --core) CORE_ONLY=1 ;;
    --strict) STRICT=1 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CORE_MISS=0
WARN_MISS=0
OK=0

c_ok()   { OK=$((OK+1));        [ "$QUIET" = 1 ] || printf "  \033[32m✓\033[0m %s\n" "$1"; }
c_bad()  { CORE_MISS=$((CORE_MISS+1)); printf "  \033[31m✗\033[0m %s\n" "$1"; }
c_warn() { WARN_MISS=$((WARN_MISS+1)); [ "$QUIET" = 1 ] || printf "  \033[33m!\033[0m %s\n" "$1"; }
hdr()    { [ "$QUIET" = 1 ] || printf "\n\033[1m%s\033[0m\n" "$1"; }

# have <命令名> <展示名> <安装提示>
have() {
  if command -v "$1" >/dev/null 2>&1; then c_ok "$2 → $(command -v "$1")"
  else c_warn "$2 未找到  |  $3"; fi
}
# need <命令名> <展示名> <安装提示>   —— 缺了算核心失败
need() {
  if command -v "$1" >/dev/null 2>&1; then c_ok "$2 → $(command -v "$1")"
  else c_bad "$2 未找到  |  $3"; fi
}
# py_have <模块> <展示名> <pip 包名> [额外 pip 包名...]
py_have() {
  local mod="$1" label="$2"; shift 2
  local hint="$*"
  if "$PY" -c "import $mod" >/dev/null 2>&1; then
    local v=""
    for pkg in "$@"; do
      v="$("$PY" -c "import importlib.metadata as m;print(m.version('$pkg'))" 2>/dev/null)" && [ -n "$v" ] && break
    done
    [ -z "$v" ] && v="$(PYTHONPATH= "$PY" -c "import $mod;print(getattr($mod,'__version__','?'))" 2>/dev/null)"
    c_ok "$label (${v:-?})"
  else
    c_warn "$label 未安装  |  pip install ${hint:-$mod}"
  fi
}
in_env() {   # in_env <env名> <命令> <展示名> <安装提示>
  if [ -x "$CONDA/envs/$1/bin/$2" ]; then c_ok "$3 ($1)"; else c_warn "$3 未在 $1 环境  |  $4"; fi
}

# ======================================================================
hdr "① 操作系统与基础命令（Agent 本体必需）"
case "$(uname -s)" in
  Linux) c_ok "Linux ($(uname -r))" ;;
  *)     c_bad "非 Linux（本项目依赖 /bin/bash、resource、nohup 等 POSIX 设施）" ;;
esac

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
  c_bad "python3 未找到  |  装 Python ≥3.10（推荐 miniconda3），见 environment/README.md §2"
  PY=python3
else
  PYV="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)"
  PYM="$("$PY" -c 'import sys;print(sys.version_info[1])' 2>/dev/null || echo 0)"
  [ "${PYV%%.*}" -ge 3 ] 2>/dev/null && [ "$PYM" -ge 10 ] 2>/dev/null \
    && c_ok "python3 $PYV ($PY)" \
    || c_bad "python3 版本过低：$PYV（需 ≥3.10）"
fi

need bash     "bash（Agent 全部 Bash 工具经 /bin/bash 执行）" "系统自带"
have nohup    "nohup（后台任务/TaskOutput）"                 "coreutils"
have curl     "curl（start_web.sh 探活）"                    "curl"
have pgrep    "pgrep/pkill（start_web.sh 停启）"             "procps-ng"
have rg       "ripgrep（Grep 工具加速；缺则回退纯 Python）"  "可选，apt/yum install ripgrep"
# bash 版本：编排管线用 `wait -n` 控并发，需 ≥4.3
if [ -n "$(command -v bash)" ]; then
  BASHV="$(bash -c 'echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}')"
  case "$BASHV" in
    4.[0-2]|[0-3].*) c_warn "bash $BASHV 过旧：编排脚本的 \`wait -n\` 并发池需 ≥4.3（装 conda bash 或升级系统 bash）" ;;
    *) c_ok "bash $BASHV（支持 wait -n）" ;;
  esac
fi

hdr "② Agent 本体 Python 依赖（requirements.txt）"
py_have requests requests requests
py_have yaml     PyYAML   PyYAML
py_have flask    flask    flask
if "$PY" -c "import sqlite3;c=sqlite3.connect(':memory:');c.execute('CREATE VIRTUAL TABLE t USING fts5(x)')" >/dev/null 2>&1
then c_ok "sqlite3 + FTS5（#11 Search / M12 记忆需要）"
else c_warn "sqlite3 无 FTS5（--retrieval / --memory 会自停用，不影响其他功能）"; fi

hdr "③ 配置与技能（跑起来的最小条件）"
if [ -f "$ROOT/config.json" ]; then
  c_ok "config.json 存在"
  if "$PY" -c "
import json,sys
d=json.load(open('$ROOT/config.json'))
sys.exit(0 if (d.get('api_key') or '').strip() else 1)" 2>/dev/null
  then c_ok "config.json 已填 api_key"; else c_warn "config.json 的 api_key 为空 | 填好或设 ANTHROPIC_AUTH_TOKEN"; fi
else
  c_warn "config.json 不存在（也可用环境变量 ANTHROPIC_AUTH_TOKEN） | cp config.json.example config.json"
fi
if [ -d "$ROOT/skills" ]; then
  c_ok "skills/ 存在：$(ls -1 "$ROOT/skills" | grep -c . ) 项（含 README）"
else c_bad "skills/ 不存在 —— 技能库缺失，Agent 无技能可用"; fi
if [ -f "$ROOT/skills/phenotype-extract/model/best_model.pth" ]; then
  c_ok "表型模型 best_model.pth 已就位"
else
  c_warn "缺 skills/phenotype-extract/model/best_model.pth (~263MB, 未随仓库分发) | 见 skills/README.md"
fi

[ "$CORE_ONLY" = 1 ] && { printf "\n"; echo "（--core：跳过后面的生信/表型检查）"; } || {

CONDA="${CONDA_PREFIX_BASE:-}"
if [ -z "$CONDA" ]; then
  for d in "$HOME/anaconda3" "$HOME/miniconda3" "$HOME/mambaforge" /opt/conda /usr/local/anaconda3; do
    [ -x "$d/bin/conda" ] && { CONDA="$d"; break; }
  done
fi

hdr "④ 生信工具链 —— conda 环境 Agent"
if [ -z "$CONDA" ]; then
  c_warn "未找到 conda  |  见 environment/README.md §2（生信技能全部依赖 conda 环境 Agent）"
elif [ ! -d "$CONDA/envs/Agent" ]; then
  c_warn "conda 环境 Agent 不存在  |  conda env create -f environment/Agent.yml"
  c_warn "   （导入后路径为 $CONDA/envs/Agent；若与你机器不同，需用 replace_paths.py 改脚本里的绝对路径）"
else
  c_ok "conda 环境 Agent → $CONDA/envs/Agent"
  in_env Agent bwa      "bwa（Stage1 比对）"          "conda install -n Agent -c bioconda bwa"
  in_env Agent samtools "samtools（排序/索引）"        "conda install -n Agent -c bioconda samtools"
  in_env Agent bcftools "bcftools（Ti/Tv 校验）"       "conda install -n Agent -c bioconda bcftools"
  in_env Agent tabix    "tabix（GVCF 索引）"           "conda install -n Agent -c bioconda htslib"
  in_env Agent gatk     "gatk4（MarkDup/BQSR/HC）"     "conda install -n Agent -c bioconda gatk4"
  in_env Agent fastp    "fastp（Stage 0.5 质控，可选）" "conda install -n Agent -c bioconda fastp"
  in_env Agent plink    "plink 1.9（GWAS 格式/质控）"  "conda install -n Agent -c bioconda plink"
  in_env Agent numactl  "numactl（NUMA 绑定，可选）"   "conda install -n Agent -c conda-forge numactl"
  in_env Agent java     "java（GATK/Parabricks 运行）" "conda install -n Agent -c conda-forge openjdk"
fi
c_warn "multiqc 不在 Agent 环境（可选） | conda install -n Agent -c bioconda multiqc"

hdr "⑤ 生信工具链 —— GWAS 独立二进制（不在 conda）"
have gemma       "GEMMA（LMM 关联，必需）"            "下载 gemma 二进制放到 PATH"
have gcta64      "gcta64（GRM/PCA，可选①）"           "下载 gcta64 二进制"
have vcftools    "vcftools（VCF→PLINK，可选②）"       "★本机缺失：用 plink --vcf --make-bed 替代"
have LDBlockShow "LDBlockShow（区域 LD block）"       "源码 make，见 /data1/.../biosoft/LDBlockShow"
have ShowLDSVG   "ShowLDSVG（SVG 精排）"              "随 LDBlockShow 源码分发"
have PopLDdecay  "PopLDdecay（LD 衰减，可选）"        "下载 PopLDdecay 二进制"
have perl        "perl（Plot_OnePop.pl 画衰减曲线）"  "系统自带 / conda install perl"
have java        "java（Haploview 是 jar）"           "openjdk"
have convert     "ImageMagick convert（SVG→PNG，可选）" "yum/apt install ImageMagick"

hdr "⑥ R 环境 —— conda 环境 r-gwas（GEMMA 之外的全部 GWAS 出图/关联）"
if [ -n "$CONDA" ] && [ -x "$CONDA/envs/r-gwas/bin/Rscript" ]; then
  RS="$CONDA/envs/r-gwas/bin/Rscript"
  RV="$("$RS" -e 'cat(as.character(getRversion()))' 2>/dev/null | tail -1)"
  case "${RV%%.*}" in
    4|5|6|7|8|9) c_ok "R $RV (r-gwas)" ;;
    *) c_warn "R $RV < 4.0：新版 rMVP/RcppArmadillo 编译会失败 | 需 R ≥4.0" ;;
  esac
  for p in rMVP CMplot pheatmap reshape2 bigmemory dplyr RcppArmadillo; do
    if "$RS" -e "quit(status=!requireNamespace('$p',quietly=TRUE))" >/dev/null 2>&1
    then c_ok "R 包 $p"; else c_warn "R 包 $p 未装  |  Rscript -e \"install.packages('$p')\""; fi
  done
else
  c_warn "conda 环境 r-gwas 不存在  |  conda env create -f environment/r-gwas.yml"
fi
if [ -n "$CONDA" ] && [ -x "$CONDA/envs/r-gwas/bin/g++" ]; then c_ok "r-gwas 自带 C++ 工具链（编译 RcppArmadillo 用）"
else c_warn "r-gwas 缺 g++ 工具链：装 R 包会因 GLIBCXX 报错 | conda install -n r-gwas -c conda-forge gxx_linux-64"; fi

hdr "⑦ 表型提取（phenotype-extract）—— 注意：跑在 conda base，不是 Agent 环境"
PHPP="${PHENO_PYTHON:-$PY}"
c_ok "用以检查的解释器：$PHPP"
# 技能文档要求的调用前缀：LD_LIBRARY_PATH=<conda>/lib（系统 libstdc++ 太旧，否则 numpy/torch import 失败）
# 必须前插而不是追加 —— 本机 /usr/lib64 在前时会先命中旧 libstdc++
if [ -n "$CONDA" ] && [ -f "$CONDA/lib/libstdc++.so.6" ]; then
  case ":${LD_LIBRARY_PATH:-}:" in
    "$CONDA/lib:"*) : ;;   # 已在最前
    *) export LD_LIBRARY_PATH="$CONDA/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"; c_ok "已前插 LD_LIBRARY_PATH=$CONDA/lib（本机必需，否则 numpy/torch 报 GLIBCXX）" ;;
  esac
fi
for spec in "numpy:numpy:numpy" "pandas:pandas:pandas" "cv2:opencv-python:opencv-python-headless,opencv-python" \
            "scipy:scipy:scipy" "torch:torch:torch" "transformers:transformers:transformers" \
            "huggingface_hub:huggingface-hub:huggingface-hub" \
            "openpyxl:openpyxl:openpyxl" "xlrd:xlrd:xlrd"; do
  m="${spec%%:*}"; rest="${spec#*:}"; label="${rest%%:*}"; pkgs="${rest##*:}"
  PY="$PHPP" py_have "$m" "$label" "$(printf '%s ' ${pkgs//,/ })"
done
if [ -d "$HOME/.cache/huggingface/hub" ] && ls "$HOME/.cache/huggingface/hub" 2>/dev/null | grep -q mask2former
then c_ok "HF 缓存含 mask2former 骨干权重"
else c_warn "HF 缓存缺 facebook/mask2former-swin-small-ade-semantic  |  HF_ENDPOINT=https://hf-mirror.com 冷启动下载（见 environment/README.md §1.7）"; fi
c_warn "★ 注意：技能文档要求 base 环境带 LD_LIBRARY_PATH=\$CONDA/lib 运行（系统 libstdc++ 太旧，否则 numpy/torch 导入失败）"

hdr "⑧ 可选：GPU / Parabricks 加速（无则自动走 gatk-cpu，管线照常跑）"
have nvidia-smi "nvidia-smi（GPU 检测）" "NVIDIA 驱动"
have docker     "docker（Parabricks 容器后端）" "docker-ce"
if [ -n "$(command -v docker)" ]; then
  if docker info >/dev/null 2>&1; then c_ok "docker daemon 可访问（当前用户在 docker 组）"
  else c_warn "docker daemon 不可访问 | sudo usermod -aG docker \$USER 后重新登录"; fi
  if docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -qi parabricks
  then c_ok "本地已有 Parabricks 镜像"; else c_warn "无 Parabricks 镜像 | 见 references/parabricks-setup.md"; fi
fi
have nvidia-ctk "nvidia-container-toolkit" "NVIDIA 官方 repo"

hdr "⑨ 数据与外部服务（按需）"
if [ -n "$CONDA" ]; then
  for cue in "$CONDA/envs/Agent/bin"; do :; done
fi
for d in /data1/chenyx/Pig_geno /data1/chenyx/Pig_image; do
  [ -d "$d" ] && c_ok "数据目录 $d" || c_warn "数据目录 $d 不存在（WGS/GWAS/表型技能默认路径）"
done
[ -f /data1/chenyx/Pig_geno/pipeline.db ] && c_ok "pipeline.db" || c_warn "缺 /data1/chenyx/Pig_geno/pipeline.db"
# 技能文档写的是 .../reference/ref.fa，本机实际在 03_reference/ —— 两处都探
if [ -f /data1/chenyx/Pig_geno/reference/ref.fa ]; then
  c_ok "参考基因组 ref.fa (reference/)"
elif [ -f /data1/chenyx/Pig_geno/03_reference/ref.fa ]; then
  c_warn "参考基因组在 03_reference/，但技能文档写的是 reference/ —— 需改 run.params.json 或建软链"
else
  c_warn "缺参考基因组 ref.fa/.fai/.dict（WGS 形态必需）"
fi
have pdftotext "pdftotext（paper-search PDF 校验）" "poppler-utils"
c_warn "agent-news / paper-search 的脚本不在本仓库，在 /home/chenyuxi/paper_search/（部署时需一并带过去）"

}   # end CORE_ONLY

# ======================================================================
printf "\n\033[1m汇总\033[0m  通过 %d 项；核心缺失 %d 项；可选缺失 %d 项\n" "$OK" "$CORE_MISS" "$WARN_MISS"
if [ "$CORE_MISS" -gt 0 ]; then
  printf "\033[31m核心未就绪：先解决上面标 ✗ 的项，再运行 python3 run.py list\033[0m\n"
  exit 1
fi
printf "\033[32m核心就绪。\033[0m下一步：\n"
printf "  python3 run.py list        # 应列出 21 个技能\n"
printf "  python3 run.py chat -p \"用 gwas-pipeline 说明标准流程，先别跑数据\"\n"
[ "$STRICT" = 1 ] && [ "$WARN_MISS" -gt 0 ] && exit 1
exit 0
