#!/usr/bin/env bash
# ============================================================================
# ScailSwap 一键环境安装
#
# 用法：
#   ./setup.sh                     # 只装本项目 Python 依赖（编排层 + API 服务）
#   ./setup.sh --with-comfyui     # 追加：ComfyUI + Wan2.2-Animate 全套模型（口播主引擎，约 40GB）
#   ./setup.sh --with-scail2      # 追加：SCAIL-2 权重（备选引擎：复杂动作/非人角色，约 30GB）
#   ./setup.sh --with-wav2lip     # 追加：可选的 Wav2Lip 口型后处理
#
# 环境要求：Python 3.10+，CUDA 11.8+（自托管推理需要 ≥24GB 显存的 GPU）
# 可用环境变量：
#   COMFYUI_DIR       ComfyUI 安装目录（默认 ./ComfyUI）
#   ANIMATE_VARIANT   Wan2.2-Animate 精度 bf16|int8_convrot（默认 bf16；<32GB 显存用 int8_convrot）
#   SCAIL_VARIANT     SCAIL-2 精度 fp16|fp8_scaled（默认 fp8_scaled）
#   HF_ENDPOINT       Hugging Face 镜像。AutoDL/国内未设置时默认 https://hf-mirror.com
#                     设为空字符串可强制直连官方：HF_ENDPOINT= ./setup.sh --with-comfyui
#   HF_HUB_DISABLE_XET  默认 1：绕过易 401 的 Xet CAS 通道，改走普通 HTTPS 下载
# ============================================================================
set -euo pipefail

# Xet CAS（cas-server.xethub.hf.co）在部分环境（含已 login）仍会 401；默认禁用更稳。
export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"

# AutoDL / 国内机直连 huggingface.co 极慢；未显式设置时自动走镜像。
# 检测：主机名含 autodl、或存在 /etc/autodl 常见标记、或 AUTODL_* 环境变量。
_autodl_like=0
if [[ "$(hostname 2>/dev/null || true)" == *autodl* ]] \
  || [[ -n "${AUTODL_CONTAINER_NAME:-}${AUTODL_REGION:-}" ]] \
  || [[ -d /root/autodl-tmp ]] || [[ -d /autodl-pub ]]; then
  _autodl_like=1
fi
if [ -z "${HF_ENDPOINT+x}" ]; then
  # 变量完全未设置
  if [ "$_autodl_like" = "1" ]; then
    export HF_ENDPOINT="https://hf-mirror.com"
  fi
elif [ -z "${HF_ENDPOINT}" ]; then
  # 显式设为空 → 直连官方，取消 export
  unset HF_ENDPOINT
fi

WITH_COMFYUI=0
WITH_SCAIL2=0
WITH_WAV2LIP=0
for arg in "$@"; do
  case "$arg" in
    --with-comfyui) WITH_COMFYUI=1 ;;
    --with-scail2)  WITH_SCAIL2=1; WITH_COMFYUI=1 ;;
    --with-wav2lip) WITH_WAV2LIP=1 ;;
    *) echo "未知参数：$arg"; exit 1 ;;
  esac
done

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMFYUI_DIR="${COMFYUI_DIR:-$PROJECT_DIR/ComfyUI}"
ANIMATE_VARIANT="${ANIMATE_VARIANT:-bf16}"
SCAIL_VARIANT="${SCAIL_VARIANT:-fp8_scaled}"

echo "==> [1/4] 检查系统依赖"
command -v ffmpeg >/dev/null || { echo "缺少 ffmpeg：sudo apt-get install -y ffmpeg"; exit 1; }
command -v ffprobe >/dev/null || { echo "缺少 ffprobe：sudo apt-get install -y ffmpeg"; exit 1; }
PY_OK=$(python3 -c 'import sys; print(int(sys.version_info >= (3, 10)))')
[ "$PY_OK" = "1" ] || { echo "需要 Python 3.10+"; exit 1; }

echo "==> [2/4] 安装本项目 Python 依赖"
cd "$PROJECT_DIR"
if command -v uv >/dev/null 2>&1; then
  uv sync
  echo "    （uv 环境：后续用 'uv run ...' 或 source .venv/bin/activate）"
else
  python3 -m pip install -r requirements.txt
fi

# --------------------------------------------------------------------------
# 自托管 ComfyUI + Wan2.2-Animate（口播主引擎）；SCAIL-2 仅在 --with-scail2 时追加
# --------------------------------------------------------------------------
if [ "$WITH_COMFYUI" = "1" ]; then
  echo "==> [3/4] 安装 ComfyUI 与 Wan2.2-Animate 模型权重"
  if [ ! -d "$COMFYUI_DIR" ]; then
    git clone https://github.com/comfyanonymous/ComfyUI "$COMFYUI_DIR"
  else
    git -C "$COMFYUI_DIR" pull --ff-only || true
  fi
  # WanAnimateToVideo / SDPose* / RTDETR_detect / SAM3_* 都是 2026 年新增的原生节点，
  # 必须用最新版 ComfyUI。本项目全程只用原生节点，不需要任何 custom_nodes。
  python3 -m pip install -r "$COMFYUI_DIR/requirements.txt"

  # hf CLI（模型下载）。强制升级：旧版 hf-xet 在 cas-server 上易 401。
  python3 -m pip install -U "huggingface_hub[cli]" "hf_xet>=1.1.7"
  # aria2 多连接加速大文件（AutoDL 上通常比单线程 hf 快一个数量级）
  if ! command -v aria2c >/dev/null 2>&1; then
    (apt-get update -qq && apt-get install -y -qq aria2) >/dev/null 2>&1 || true
  fi
  if [ -n "${HF_ENDPOINT:-}" ]; then
    echo "    使用 HF 镜像：HF_ENDPOINT=$HF_ENDPOINT"
  else
    echo "    直连 huggingface.co（国内/AutoDL 建议：export HF_ENDPOINT=https://hf-mirror.com）"
  fi
  if [ "${HF_HUB_DISABLE_XET}" = "1" ]; then
    echo "    已禁用 Xet（HF_HUB_DISABLE_XET=1）"
  fi

  dl() { # dl <repo> <repo内路径> <目标目录> [重命名]
    local repo="$1" file="$2" dest="$3" rename="${4:-}"
    local target="$dest/${rename:-$(basename "$file")}"
    local base_name
    base_name="$(basename "$file")"
    if [ -f "$target" ]; then echo "    已存在，跳过：$target"; return; fi
    mkdir -p "$dest"
    echo "    下载 $repo :: $file"

    # 优先：镜像 + aria2 多连接（AutoDL 上 ~10-50MB/s，远快于直连 ~1MB/s）
    if [ -n "${HF_ENDPOINT:-}" ] && command -v aria2c >/dev/null 2>&1; then
      local url="${HF_ENDPOINT%/}/${repo}/resolve/main/${file}"
      echo "    → aria2c 多连接：$url"
      if aria2c -x 16 -s 16 -k 1M --file-allocation=none \
          --console-log-level=notice --summary-interval=10 \
          -d "$dest" -o "$base_name" "$url"; then
        return 0
      fi
      echo "    ⚠️ aria2 失败，回退 hf download…"
      rm -f "$dest/$base_name" "$dest/${base_name}.aria2"
    fi

    rm -rf "$dest/.hfdl"
    # 先按当前环境下载；若仍走 Xet 并 401，则强制禁用 Xet 重试一次
    if ! HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET}" \
         hf download "$repo" "$file" --local-dir "$dest/.hfdl"; then
      echo "    ⚠️ 首次下载失败，强制禁用 Xet 重试…"
      rm -rf "$dest/.hfdl"
      HF_HUB_DISABLE_XET=1 hf download "$repo" "$file" --local-dir "$dest/.hfdl"
    fi
    # --local-dir 可能保留子目录结构，按相对路径取出
    if [ -f "$dest/.hfdl/$file" ]; then
      mv "$dest/.hfdl/$file" "$target"
    else
      local found
      found="$(find "$dest/.hfdl" -type f -name "$base_name" | head -n1)"
      [ -n "$found" ] || { echo "    ❌ 下载后未找到文件：$file"; rm -rf "$dest/.hfdl"; return 1; }
      mv "$found" "$target"
    fi
    rm -rf "$dest/.hfdl"
  }

  M="$COMFYUI_DIR/models"

  # ---- 共享组件（两个引擎都要用）----
  dl "Comfy-Org/Wan_2.1_ComfyUI_repackaged" "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors" "$M/text_encoders"
  dl "Comfy-Org/Wan_2.1_ComfyUI_repackaged" "split_files/clip_vision/clip_vision_h.safetensors" "$M/clip_vision"
  dl "Comfy-Org/Wan_2.1_ComfyUI_repackaged" "split_files/vae/wan_2.1_vae.safetensors" "$M/vae"
  # lightx2v 蒸馏 LoRA（4~8 步快速采样）
  dl "Kijai/WanVideo_comfy" "Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors" \
     "$M/loras" "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
  # SAM3 跟踪（替换模式的人物掩码，替代官方工作流里交互式的 Points Editor）
  dl "Comfy-Org/sam3.1" "checkpoints/sam3.1_multiplex_fp16.safetensors" "$M/checkpoints"

  # ---- 主引擎：Wan2.2-Animate（口播场景）----
  # bf16 约 32GB；int8_convrot 约 15GB（24GB 显存推荐）
  dl "Comfy-Org/Wan_2.2_ComfyUI_Repackaged" "split_files/diffusion_models/wan2.2_animate_14B_${ANIMATE_VARIANT}.safetensors" "$M/diffusion_models"
  # 官方 relight LoRA：替换模式下让人物与原场景光照色调一致
  dl "Comfy-Org/Wan_2.2_ComfyUI_Repackaged" "split_files/loras/wan2.2_animate_14B_relight_lora_bf16.safetensors" "$M/loras"
  # 原生姿态/人脸预处理：SDPose 全身关键点 + RT-DETR 人体检测
  dl "Comfy-Org/SDPose" "checkpoints/sdpose_wholebody_fp16.safetensors" "$M/checkpoints"
  dl "Comfy-Org/SDPose" "diffusion_models/rt_detr_v4-x-hgnet_fp16.safetensors" "$M/diffusion_models"

  if [ "$ANIMATE_VARIANT" != "bf16" ]; then
    echo "    ⚠️ 使用 ${ANIMATE_VARIANT} 时请在 .env 设置："
    echo "       WANANIMATE_UNET=wan2.2_animate_14B_${ANIMATE_VARIANT}.safetensors"
  fi

  # ---- 备选引擎：SCAIL-2（复杂 3D 动作 / 非人角色）----
  if [ "$WITH_SCAIL2" = "1" ]; then
    echo "    追加下载 SCAIL-2 权重"
    dl "Comfy-Org/SCAIL-2" "diffusion_models/wan2.1_14B_SCAIL_2_${SCAIL_VARIANT}.safetensors" "$M/diffusion_models"
    dl "Comfy-Org/SCAIL-2" "loras/wan2.1_SCAIL_2_DPO_lora_bf16.safetensors" "$M/loras"
    dl "Comfy-Org/SCAIL-2" "loras/wan2.1_SCAIL_2_relight_lora_bf16.safetensors" "$M/loras"
    echo "    ⚠️ 使用 scail2 引擎时请在 .env 设置："
    echo "       SCAILSWAP_ENGINE=scail2"
    echo "       SCAILSWAP_UNET=wan2.1_14B_SCAIL_2_${SCAIL_VARIANT}.safetensors"
  fi

  echo "    ComfyUI 启动命令：python3 $COMFYUI_DIR/main.py --listen 127.0.0.1 --port 8188"
else
  echo "==> [3/4] 跳过 ComfyUI（如需自托管推理请加 --with-comfyui）"
fi

# --------------------------------------------------------------------------
# 可选：Wav2Lip 口型后处理
# --------------------------------------------------------------------------
if [ "$WITH_WAV2LIP" = "1" ]; then
  echo "==> [4/4] 安装 Wav2Lip（可选口型精修）"
  W2L_DIR="$PROJECT_DIR/Wav2Lip"
  [ -d "$W2L_DIR" ] || git clone https://github.com/Rudrabha/Wav2Lip "$W2L_DIR"
  python3 -m pip install -r "$W2L_DIR/requirements.txt" || \
    echo "    ⚠️ Wav2Lip 依赖安装失败（其 requirements 较旧），可手动安装：librosa opencv-python numpy tqdm numba"
  echo "    ⚠️ 权重需手动下载 wav2lip_gan.pth 放到 $W2L_DIR/checkpoints/"
  echo "       然后在 .env 设置：WAV2LIP_DIR=$W2L_DIR"
  echo "       WAV2LIP_CHECKPOINT=$W2L_DIR/checkpoints/wav2lip_gan.pth"
else
  echo "==> [4/4] 跳过 Wav2Lip"
fi

echo ""
echo "✅ 安装完成。下一步："
echo "   1) cp .env.example .env 并按需修改（COMFYUI_URL 等）"
echo "   2) 启动 ComfyUI（自托管推理）：python3 $COMFYUI_DIR/main.py --port 8188"
echo "   3) 一键启动 API 服务：./scripts/start_api.sh"
