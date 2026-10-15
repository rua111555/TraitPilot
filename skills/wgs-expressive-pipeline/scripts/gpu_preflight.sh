#!/bin/bash
# GPU / Parabricks 环境预检 —— 供 wgs-express-server-detect（S0）与
# wgs-express-params-propose（Stage 0）判断能否启用 Parabricks 路径。
#
# 输出：JSON 到 stdout（含 gpu_detection + parabricks_ready）。
# 任意一步失败不中断，全部汇总；判定见下方 PARABRICKS_READY 逻辑。
# 所有 nvidia-smi / docker 调用都带 timeout，共享 GPU 服务器上偶发卡顿也会快速失败。
#
# 用法：
#   bash .claude/skills/wgs-expressive-pipeline/scripts/gpu_preflight.sh
# 可选环境变量：
#   PB_IMAGE=镜像tag    手动指定要检测的镜像（缺省自动在本地 docker images 中查找 parabricks）
set -u

NS="timeout 15 nvidia-smi"
DK="timeout 15 docker"

# ---------- 1. GPU 检测 ----------
GPU_COUNT=0
GPU_MODEL=""
GPU_VRAM_GB=0
COMPUTE_CAP=""
GPU_UTIL_PCT=0
GPU_MEM_USED_GB=0
DRIVER=""
CUDA_VERSION=""
GPU_CLASS="unknown"

if command -v nvidia-smi >/dev/null 2>&1 && $NS -L >/dev/null 2>&1; then
    GPU_COUNT=$($NS -L | wc -l)
    DRIVER=$($NS --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)
    CUDA_VERSION=$($NS 2>/dev/null | grep -oP 'CUDA Version: \K[0-9.]+' | head -1)
    GPU_MODEL=$($NS --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    V=$($NS --query-gpu=memory.total --format=csv,noheader 2>/dev/null | head -1)
    GPU_VRAM_GB=$(echo "$V" | grep -oP '\d+' | head -1)
    [ -n "$GPU_VRAM_GB" ] && GPU_VRAM_GB=$((GPU_VRAM_GB / 1024))
    COMPUTE_CAP=$($NS --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1)
    U=$($NS --query-gpu=utilization.gpu --format=csv,noheader 2>/dev/null | head -1)
    GPU_UTIL_PCT=$(echo "$U" | grep -oP '\d+' | head -1)
    M=$($NS --query-gpu=memory.used --format=csv,noheader 2>/dev/null | head -1)
    GPU_MEM_USED_GB=$(echo "$M" | grep -oP '\d+' | head -1)
    [ -n "$GPU_MEM_USED_GB" ] && GPU_MEM_USED_GB=$(awk "BEGIN{printf \"%.3f\", $GPU_MEM_USED_GB/1024}")

    case "$GPU_MODEL" in
        *V100*|*T4*|*A100*|*A800*|*H100*|*H200*|*L4*|*L40*|*L20*|*GB2*|*GH2*|*A10*|*A16*|*A40*|*RTX\ A6000*|*RTX\ A5000*) GPU_CLASS="datacenter" ;;
        *GeForce*|*GTX*|*RTX\ 4*|*RTX\ 5*) GPU_CLASS="consumer" ;;
        *) GPU_CLASS="unknown" ;;
    esac
fi

GPU_PRESENT="false"
[ "$GPU_COUNT" -gt 0 ] 2>/dev/null && GPU_PRESENT="true"
# 兜底：nvidia-smi 超时/空字段时给默认值，保证 JSON 合法
[ -z "$GPU_VRAM_GB" ] && GPU_VRAM_GB=0
[ -z "$GPU_UTIL_PCT" ] && GPU_UTIL_PCT=0
[ -z "$GPU_MEM_USED_GB" ] && GPU_MEM_USED_GB=0
[ -z "$COMPUTE_CAP" ] && COMPUTE_CAP=""
[ -z "$GPU_MODEL" ] && GPU_MODEL="unknown"

# ---------- 2. docker 可用性 ----------
DOCKER_OK="false"
DOCKER_MSG=""
if command -v docker >/dev/null 2>&1; then
    if $DK info >/dev/null 2>&1; then
        DOCKER_OK="true"
    else
        DOCKER_MSG="docker daemon 无权限：请『sudo usermod -aG docker chenyuxi』后重新登录（参考 references/parabricks-setup.md §1.1）"
    fi
else
    DOCKER_MSG="未找到 docker 命令"
fi

# ---------- 3. nvidia-container-toolkit ----------
TOOLKIT_OK="false"
if command -v nvidia-ctk >/dev/null 2>&1 || command -v nvidia-container-cli >/dev/null 2>&1; then
    TOOLKIT_OK="true"
fi
# daemon 里注册 nvidia runtime 检查（无 docker 权限时保持 unknown）
NVIDIA_RUNTIME_OK="unknown"
if [ "$DOCKER_OK" = "true" ]; then
    NVIDIA_RUNTIME_OK="false"
    $DK info 2>/dev/null | grep -qiE 'Runtimes:.*\bnvidia\b' && NVIDIA_RUNTIME_OK="true"
fi

# ---------- 4. 本地 parabricks 镜像 ----------
PB_IMAGE="${PB_IMAGE:-}"
if [ -z "$PB_IMAGE" ] && [ "$DOCKER_OK" = "true" ]; then
    # docker 的 reference= 过滤器在此环境不匹配本地镜像，改为全量+grep；
    # 优先取 nvcr.io 规范镜像中版本最高的；无则退回任意 parabricks 镜像
    PB_IMAGE=$($DK images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
        | grep -i 'parabricks' | grep -i 'nvcr.io' | sort -V | tail -1)
    [ -z "$PB_IMAGE" ] && PB_IMAGE=$($DK images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -i 'parabricks' | head -1)
fi
IMAGE_OK="false"
[ -n "$PB_IMAGE" ] && IMAGE_OK="true"

# ---------- 5. 容器内 pbrun version（GPU 可见性 + license 初判） ----------
PBRUN_VERSION=""
PB_MSG=""
PB_READY="false"
if [ "$DOCKER_OK" = "true" ] && [ "$IMAGE_OK" = "true" ]; then
    OUT=$(timeout 120 $DK run --rm --gpus all -u "$(id -u):$(id -g)" -w "$(pwd)" "$PB_IMAGE" pbrun version 2>&1)
    RC=$?
    PBRUN_VERSION=$(echo "$OUT" | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)
    if [ $RC -eq 0 ] && { [ -n "$PBRUN_VERSION" ] || echo "$OUT" | grep -qiE 'version|pbrun'; }; then
        PB_READY="true"
        if echo "$OUT" | grep -qiE "licen[cse]"; then
            if echo "$OUT" | grep -qiE "(no|invalid|expired|not.*found|fail).*licen|licen.*(error|invalid|expired)"; then
                PB_READY="false"
                PB_MSG="pbrun 提示 license 问题，请按 parabricks-setup.md §3 配置"
            fi
        fi
        [ -n "$PBRUN_VERSION" ] || PBRUN_VERSION="detected"
    else
        PB_MSG="容器内 pbrun version 失败(rc=$RC)，输出: $(echo "$OUT" | head -3 | tr '\n' ' ')"
    fi
elif [ -z "$PB_IMAGE" ]; then
    PB_MSG="本地无 parabricks 镜像，请先 docker pull（parabricks-setup.md §1.3）"
fi

PARABRICKS_READY="false"
if [ "$GPU_PRESENT" = "true" ] && [ "$DOCKER_OK" = "true" ] && [ "$TOOLKIT_OK" = "true" ] && [ "$IMAGE_OK" = "true" ] && [ "$PB_READY" = "true" ]; then
    PARABRICKS_READY="true"
fi

# ---------- 6. 汇总 JSON ----------
cat <<EOF
{
  "gpu_detection": {
    "gpu_present": ${GPU_PRESENT},
    "gpu_count": ${GPU_COUNT},
    "gpu_models": ["${GPU_MODEL}"],
    "gpu_vram_gb": ${GPU_VRAM_GB},
    "compute_cap": "${COMPUTE_CAP}",
    "gpu_util_pct": ${GPU_UTIL_PCT},
    "gpu_mem_used_gb": ${GPU_MEM_USED_GB},
    "driver_version": "${DRIVER}",
    "cuda_version": "${CUDA_VERSION}",
    "gpu_class": "${GPU_CLASS}"
  },
  "docker_ok": ${DOCKER_OK},
  "docker_msg": "${DOCKER_MSG}",
  "toolkit_ok": ${TOOLKIT_OK},
  "nvidia_runtime_ok": "${NVIDIA_RUNTIME_OK}",
  "parabricks_image": "${PB_IMAGE}",
  "pbrun_version": "${PBRUN_VERSION}",
  "parabricks_ready": ${PARABRICKS_READY},
  "parabricks_msg": "${PB_MSG}"
}
EOF
