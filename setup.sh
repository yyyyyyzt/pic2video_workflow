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
# ============================================================================
set -euo pipefail

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
# 自托管 ComfyUI + SCAIL-2 模型（长视频主引擎）
# --------------------------------------------------------------------------
if [ "$WITH_COMFYUI" = "1" ]; then
  echo "==> [3/4] 安装 ComfyUI 与模型权重"
  if [ ! -d "$COMFYUI_DIR" ]; then
    git clone https://github.com/comfyanonymous/ComfyUI "$COMFYUI_DIR"
  else
    git -C "$COMFYUI_DIR" pull --ff-only || true
  fi
  # WanAnimateToVideo / SDPose* / RTDETR_detect / SAM3_* 都是 2026 年新增的原生节点，
  # 必须用最新版 ComfyUI。本项目全程只用原生节点，不需要任何 custom_nodes。
  python3 -m pip install -r "$COMFYUI_DIR/requirements.txt"

  # hf CLI（模型下载）
  command -v hf >/dev/null 2>&1 || python3 -m pip install -U "huggingface_hub[cli]"

  dl() { # dl <repo> <repo内路径> <目标目录> [重命名]
    local repo="$1" file="$2" dest="$3" rename="${4:-}"
    local target="$dest/${rename:-$(basename "$file")}"
    if [ -f "$target" ]; then echo "    已存在，跳过：$target"; return; fi
    mkdir -p "$dest"
    echo "    下载 $repo :: $file"
    hf download "$repo" "$file" --local-dir "$dest/.hfdl" >/dev/null
    mv "$dest/.hfdl/$file" "$target"
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
