#!/usr/bin/env bash
# ============================================================================
# ScailSwap 一键环境安装
#
# 用法：
#   ./setup.sh                     # 只装本项目 Python 依赖（编排层 + API 服务）
#   ./setup.sh --with-comfyui     # 追加：安装自托管 ComfyUI + 下载 SCAIL-2 全套模型（约 45GB）
#   ./setup.sh --with-wav2lip     # 追加：安装可选的 Wav2Lip 口型后处理
#
# 环境要求：Python 3.10+，CUDA 11.8+（自托管推理需要 ≥24GB 显存的 GPU）
# 可用环境变量：
#   COMFYUI_DIR      ComfyUI 安装目录（默认 ./ComfyUI）
#   SCAIL_VARIANT    扩散模型精度 fp16|fp8_scaled（默认 fp16；显存 <32GB 建议 fp8_scaled）
# ============================================================================
set -euo pipefail

WITH_COMFYUI=0
WITH_WAV2LIP=0
for arg in "$@"; do
  case "$arg" in
    --with-comfyui) WITH_COMFYUI=1 ;;
    --with-wav2lip) WITH_WAV2LIP=1 ;;
    *) echo "未知参数：$arg"; exit 1 ;;
  esac
done

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMFYUI_DIR="${COMFYUI_DIR:-$PROJECT_DIR/ComfyUI}"
SCAIL_VARIANT="${SCAIL_VARIANT:-fp16}"

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
  echo "==> [3/4] 安装 ComfyUI 与 SCAIL-2 模型"
  if [ ! -d "$COMFYUI_DIR" ]; then
    git clone https://github.com/comfyanonymous/ComfyUI "$COMFYUI_DIR"
  else
    git -C "$COMFYUI_DIR" pull --ff-only || true
  fi
  # WanSCAILToVideo / SCAIL2ColoredMask / SAM3 为 2026-06 后加入的原生节点，务必最新版
  python3 -m pip install -r "$COMFYUI_DIR/requirements.txt"

  # （可选）社区长视频自动扩展节点：仅当你想在 ComfyUI 画布里手动跑长视频时需要。
  # 本项目的 API 编排层使用原生节点自建锚定链，不依赖它们。
  mkdir -p "$COMFYUI_DIR/custom_nodes"
  for repo in \
    "https://github.com/Brobert-in-aus/scail-auto-extend" \
    "https://github.com/collbroGTR/comfyui-scail2-infinity"; do
    name="$(basename "$repo")"
    if [ ! -d "$COMFYUI_DIR/custom_nodes/$name" ]; then
      git clone "$repo" "$COMFYUI_DIR/custom_nodes/$name" || echo "    （可选节点 $name 克隆失败，跳过）"
    fi
  done

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
  # 扩散模型（fp16 约 28GB；fp8_scaled 约 15GB，24GB 显存推荐）
  dl "Comfy-Org/SCAIL-2" "diffusion_models/wan2.1_14B_SCAIL_2_${SCAIL_VARIANT}.safetensors" "$M/diffusion_models"
  # DPO LoRA（官方后训练：手部细节 + 口型/眼神同步）
  dl "Comfy-Org/SCAIL-2" "loras/wan2.1_SCAIL_2_DPO_lora_bf16.safetensors" "$M/loras"
  # lightx2v 蒸馏 LoRA（6~8 步快速采样）
  dl "Kijai/WanVideo_comfy" "Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors" \
     "$M/loras" "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
  # 文本编码器 / CLIP Vision / VAE
  dl "Comfy-Org/Wan_2.1_ComfyUI_repackaged" "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors" "$M/text_encoders"
  dl "Comfy-Org/Wan_2.1_ComfyUI_repackaged" "split_files/clip_vision/clip_vision_h.safetensors" "$M/clip_vision"
  dl "Comfy-Org/Wan_2.1_ComfyUI_repackaged" "split_files/vae/wan_2.1_vae.safetensors" "$M/vae"
  # SAM3 跟踪模型（人物掩码）
  dl "Comfy-Org/sam3.1" "checkpoints/sam3.1_multiplex_fp16.safetensors" "$M/checkpoints"

  if [ "$SCAIL_VARIANT" != "fp16" ]; then
    echo "    ⚠️ 使用 ${SCAIL_VARIANT} 模型时，请在 .env 设置："
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
