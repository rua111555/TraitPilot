#!/usr/bin/env bash
# ============================================================================
# skills/_shared/env.sh —— 运行时环境自解析（不写死本机绝对路径）
#
# 让技能脚本在**任意机器**上跑通：
#   · conda 根目录从「用户自己的 conda」推断，而不是写死 /home/<user>/anaconda3
#   · 工具路径从 PATH / conda 环境 / ~/.local/bin 动态解析
#   · 仅在系统 libstdc++ 过旧时才前插 conda lib（新机器上不会遮蔽新库）
#   · 数据根目录可用环境变量覆盖，缺省沿用历史布局
#
# 用法（仓库内脚本）：
#   source "$(dirname "$0")/../../_shared/env.sh"
#   BWA="$(sk_tool bwa)"                  # 从 Agent 环境解析
#   RSCRIPT="$(sk_tool Rscript r-gwas)"   # 从 r-gwas 环境解析
#   sk_ensure_ld_path                     # 按需修正 LD_LIBRARY_PATH
#
# 用法（生成的阶段脚本，拷到 RUN_DIR 后与仓库无相对关系）：
#   把本文件的「环境自解析」小节内联进脚本头部即可——**不含任何本机路径**。
# ============================================================================

# 环境名（与 environment/install.sh 创建的一致；可用 SKILL_ENV_* 覆盖）
SKILL_ENV_AGENT="${SKILL_ENV_AGENT:-Agent}"
SKILL_ENV_R="${SKILL_ENV_R:-r-gwas}"
SKILL_ENV_PHENO="${SKILL_ENV_PHENO:-Phenotype}"

# ── conda 根目录：按优先级探测用户自己的 conda ─────────────────────────────
sk_conda_base() {
  local d
  # 1) 正在运行的 conda 环境
  [ -n "${CONDA_PREFIX:-}" ] && {
    # CONDA_PREFIX 可能是 envs/<name>，回退到 base
    if [ -d "${CONDA_PREFIX}/envs" ] || [ -x "${CONDA_PREFIX}/bin/conda" ]; then
      printf '%s\n' "$CONDA_PREFIX"; return 0
    fi
  }
  # 2) conda 可执行文件自报
  if command -v conda >/dev/null 2>&1; then
    d="$(conda info --base 2>/dev/null)" && [ -n "$d" ] && { printf '%s\n' "$d"; return 0; }
  fi
  # 3) 常见安装位置（用户态优先）
  for d in "${CONDA_ROOT:-}" "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/mambaforge" \
           "$HOME/miniforge3" /opt/conda /usr/local/anaconda3; do
    [ -n "$d" ] && [ -x "$d/bin/conda" ] && { printf '%s\n' "$d"; return 0; }
  done
  return 1
}

# ── 某 conda 环境的 bin 目录 ────────────────────────────────────────────────
sk_env_bin() {  # sk_env_bin <env名>
  local base; base="$(sk_conda_base)" || return 1
  [ -d "$base/envs/$1/bin" ] && printf '%s\n' "$base/envs/$1/bin" || return 1
}

# ── 解析工具绝对路径：PATH → 指定 conda 环境 → base → ~/.local/bin ──────────
sk_tool() {  # sk_tool <工具名> [conda环境名=Agent]
  local name="$1" env="${2:-$SKILL_ENV_AGENT}" p

  # 1) 已在 PATH（含 conda run 激活的环境）
  p="$(command -v "$name" 2>/dev/null)" && { printf '%s\n' "$p"; return 0; }

  # 2) 指定 conda 环境
  p="$(sk_env_bin "$env")/$name" && [ -x "$p" ] && { printf '%s\n' "$p"; return 0; }

  # 3) conda base
  local base; base="$(sk_conda_base)" || base=""
  [ -n "$base" ] && [ -x "$base/bin/$name" ] && { printf '%s\n' "$base/bin/$name"; return 0; }

  # 4) 用户态本地二进制（install.sh 的落点）
  for p in "$HOME/.local/bin/$name" "$HOME/bin/$name"; do
    [ -x "$p" ] && { printf '%s\n' "$p"; return 0; }
  done

  return 1
}

# ── 解析工具，失败即报错退出（希望脚本早失败时用） ─────────────────────────
sk_tool_or_die() {
  local p; p="$(sk_tool "$@")" || {
    echo "✗ 找不到工具 '$1'（环境 ${2:-$SKILL_ENV_AGENT}）——请先跑 environment/check_env.sh" >&2
    exit 1
  }
  printf '%s\n' "$p"
}

# ── LD_LIBRARY_PATH 修正：仅当系统 libstdc++ 缺 GLIBCXX_3.4.29 才前插 conda lib
#    新机器若系统库已够新，则**不动** LD_LIBRARY_PATH，避免遮蔽更新的库。 ─────
sk_ensure_ld_path() {
  local libstdc="" c
  for c in /usr/lib64/libstdc++.so.6 /usr/lib/x86_64-linux-gnu/libstdc++.so.6 \
           /usr/lib/libstdc++.so.6; do
    [ -e "$c" ] && { libstdc="$c"; break; }
  done

  # 系统库已提供所需符号 → 无需干预
  if [ -n "$libstdc" ] && strings "$libstdc" 2>/dev/null | grep -q 'GLIBCXX_3.4.29'; then
    return 0
  fi

  local base; base="$(sk_conda_base)" || return 0
  [ -e "$base/lib/libstdc++.so.6" ] || return 0
  case ":$LD_LIBRARY_PATH:" in
    *":$base/lib:"*) : ;;                       # 已在前插位置则不动
    *) export LD_LIBRARY_PATH="$base/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
  esac
}

# ── 数据根目录（可用环境变量覆盖，缺省沿用历史布局） ───────────────────────
sk_geno_dir()  { printf '%s\n' "${PIG_GENO_DIR:-/data1/chenyx/Pig_geno}"; }
sk_image_dir() { printf '%s\n' "${PIG_IMAGE_DIR:-/data1/chenyx/Pig_image}"; }
sk_db_path()   { printf '%s\n' "${PIPELINE_DB:-$(sk_geno_dir)/pipeline.db}"; }

# ── 交互式确认：是否真的需要跑某层（供生成的脚本判断） ─────────────────────
sk_have() { sk_tool "$@" >/dev/null 2>&1; }
