# AutoDL 部署操作文档（ScailSwap / pic2video_workflow）

面向 **AutoDL GPU 实例** 的完整部署指南。覆盖：租机选型、磁盘规划、拉代码、
装系统依赖（含 **aria2**）、一键安装 / **手动下载大模型**、配置、启动与验证。

> 代码仓库：`https://github.com/yyyyyyzt/pic2video_workflow`  
> 主引擎：自托管 **Wan2.2-Animate**（经 ComfyUI）+ 本仓库 FastAPI 编排层  
> 模型体积：口播主引擎约 **40GB**；若再装 SCAIL-2 备选引擎再加约 **30GB**

---

## 0. 先看磁盘（AutoDL 必读）

| 目录 | 名称 | 速度 | 说明 |
| :--- | :--- | :--- | :--- |
| `/` | 系统盘 | 一般 | 关机不丢；**保存镜像时会带上**。适合放代码、`.venv`、小配置 |
| `/root/autodl-tmp` | 数据盘 | 快 | 关机不丢；**保存镜像时不会带上**。适合放模型、任务缓存、长视频中间文件 |

**推荐布局（本项目）：**

```text
/root/pic2video_workflow/          # 系统盘：代码 + Python 依赖（可随镜像保存）
/root/autodl-tmp/ComfyUI/          # 数据盘：ComfyUI + 全部模型权重（~40GB+）
/root/autodl-tmp/scailswap-data/   # 数据盘：上传素材 / 分块中间文件 / 输出
```

关机再开机后数据盘内容还在；若你「保存镜像」再开新实例，需要重新下载模型（或从网盘/对象存储再拷一遍）。

---

## 1. 租机建议

| 项 | 建议 |
| :--- | :--- |
| GPU 显存 | **≥24GB**（`int8_convrot`）；**≥32GB** 更稳（默认 `bf16`） |
| 系统 | Ubuntu + CUDA 11.8+（AutoDL 预装 PyTorch 镜像即可） |
| 数据盘 | **≥80GB 可用**（模型 40GB + 中间文件余量） |
| CPU / 内存 | 建议 ≥8 核 / ≥32GB；仅编排层很轻，但 ComfyUI + ffmpeg 会吃内存 |

口播主引擎 `wan_animate` 需要本机 GPU；若暂时没有 GPU，可改用百炼 API（见文末「无 GPU 验证」），但那不是 AutoDL 自托管主路径。

在 AutoDL 控制台为实例打开自定义服务端口（至少）：

- `8188` — ComfyUI
- `8000` — ScailSwap API（Swagger：`/docs`）

---

## 2. 拉取最新代码

```bash
# 建议放到系统盘
cd /root
git clone https://github.com/yyyyyyzt/pic2video_workflow.git
cd pic2video_workflow
git pull origin main
```

已有仓库时：

```bash
cd /root/pic2video_workflow
git fetch origin
git checkout main
git pull origin main
```

---

## 3. 安装系统依赖（ffmpeg / aria2 / 基础工具）

> 用户常说的「arial 库」在本项目里指的是 **aria2**（命令 `aria2c`），用于多连接加速 Hugging Face / 镜像站大文件下载。不是字体库 Arial。

```bash
apt-get update
apt-get install -y ffmpeg aria2 git curl wget ca-certificates

# 校验
ffmpeg -version | head -n1
ffprobe -version | head -n1
aria2c --version | head -n1
python3 --version   # 需要 3.10+
```

国内 / AutoDL 下载 Hugging Face 模型时，建议固定镜像与禁用易 401 的 Xet 通道
（`setup.sh` 在检测到 AutoDL 时也会自动设置；手动下载时请自己 export）：

```bash
export HF_ENDPOINT=https://hf-mirror.com
export HF_HUB_DISABLE_XET=1
```

可写入 `~/.bashrc` 以便每次登录生效：

```bash
echo 'export HF_ENDPOINT=https://hf-mirror.com' >> ~/.bashrc
echo 'export HF_HUB_DISABLE_XET=1' >> ~/.bashrc
source ~/.bashrc
```

可选：安装 `uv`（更快的 Python 包管理；没有也能用 pip）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"   # 或按安装脚本提示 source
```

---

## 4. 两种部署路径（二选一）

| 路径 | 适合 | 说明 |
| :--- | :--- | :--- |
| **A. 一键 `setup.sh`** | 网络稳定、想省事 | 自动装依赖 + clone ComfyUI + 用 aria2/hf 下模型 |
| **B. 手动下模型** | 大文件易断、想用网盘/多机拷贝 | 先装代码与 ComfyUI，再用本文「第 6 节」逐个 `aria2c` |

两种路径最终目录结构应一致。

---

## 5. 路径 A：一键安装（推荐先试）

把 ComfyUI 与模型放到**数据盘**，避免占满系统盘、也方便 IO：

```bash
cd /root/pic2video_workflow

# 显存 ≥32GB：默认 bf16
COMFYUI_DIR=/root/autodl-tmp/ComfyUI ./setup.sh --with-comfyui

# 显存 24~32GB：改用 int8
# ANIMATE_VARIANT=int8_convrot COMFYUI_DIR=/root/autodl-tmp/ComfyUI ./setup.sh --with-comfyui

# 若还要 SCAIL-2 备选引擎（再占 ~30GB）
# COMFYUI_DIR=/root/autodl-tmp/ComfyUI ./setup.sh --with-scail2

# 可选口型后处理（权重仍需手动，见第 6.4 节）
# COMFYUI_DIR=/root/autodl-tmp/ComfyUI ./setup.sh --with-comfyui --with-wav2lip
```

`setup.sh` 在 AutoDL 上会：

1. 安装本项目 Python 依赖（`uv sync` 或 `pip install -r requirements.txt`）
2. clone / 更新 ComfyUI，并安装其 `requirements.txt`
3. 安装/升级 `huggingface_hub[cli]`，尝试 `apt` 安装 **aria2**
4. 默认 `HF_ENDPOINT=https://hf-mirror.com`，用 **aria2 16 连接** 拉模型；失败再回退 `hf download`

若希望系统盘项目目录里也有 `ComfyUI` 入口，可做软链：

```bash
ln -sfn /root/autodl-tmp/ComfyUI /root/pic2video_workflow/ComfyUI
```

跳到 **第 7 节** 配置 `.env`。

---

## 6. 路径 B：手动下载模型（网络不稳 / 断点续传）

### 6.1 只装代码依赖 + ComfyUI（不下模型）

```bash
cd /root/pic2video_workflow
./setup.sh                          # 只装本项目 Python 依赖

export COMFYUI_DIR=/root/autodl-tmp/ComfyUI
if [ ! -d "$COMFYUI_DIR" ]; then
  git clone https://github.com/comfyanonymous/ComfyUI "$COMFYUI_DIR"
else
  git -C "$COMFYUI_DIR" pull --ff-only || true
fi
python3 -m pip install -r "$COMFYUI_DIR/requirements.txt"
python3 -m pip install -U "huggingface_hub[cli]" "hf_xet>=1.1.7"

# 确保 aria2 可用
command -v aria2c >/dev/null || apt-get install -y aria2

ln -sfn "$COMFYUI_DIR" /root/pic2video_workflow/ComfyUI

export HF_ENDPOINT=https://hf-mirror.com
export HF_HUB_DISABLE_XET=1
M="$COMFYUI_DIR/models"
mkdir -p "$M"/{text_encoders,clip_vision,vae,loras,checkpoints,diffusion_models}
```

本项目**只使用 ComfyUI 原生节点**，不需要安装额外 `custom_nodes`。

### 6.2 通用下载函数（复制到终端一次即可）

镜像 URL 规则：

```text
${HF_ENDPOINT}/${repo}/resolve/main/${file}
例：https://hf-mirror.com/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_animate_14B_bf16.safetensors
```

```bash
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

# dl <HuggingFace仓库> <仓库内相对路径> <本地目标目录> [可选：保存文件名]
dl() {
  local repo="$1" file="$2" dest="$3" rename="${4:-}"
  local base_name
  base_name="$(basename "$file")"
  local out_name="${rename:-$base_name}"
  local target="$dest/$out_name"
  if [ -f "$target" ]; then
    echo "已存在，跳过：$target"
    return 0
  fi
  mkdir -p "$dest"
  local url="${HF_ENDPOINT%/}/${repo}/resolve/main/${file}"
  echo "下载 → $url"
  echo "保存 → $target"
  # -x/-s 16 连接；断点续传；不预分配大文件空洞
  aria2c -x 16 -s 16 -k 1M --file-allocation=none \
    --continue=true \
    --max-tries=0 --retry-wait=5 \
    --console-log-level=notice --summary-interval=10 \
    -d "$dest" -o "$out_name" "$url"
}

# 若 aria2 失败，可用 hf CLI 兜底（同样走镜像）
dl_hf() {
  local repo="$1" file="$2" dest="$3" rename="${4:-}"
  local base_name out_name target
  base_name="$(basename "$file")"
  out_name="${rename:-$base_name}"
  target="$dest/$out_name"
  [ -f "$target" ] && { echo "已存在，跳过：$target"; return 0; }
  mkdir -p "$dest" "$dest/.hfdl"
  HF_HUB_DISABLE_XET=1 hf download "$repo" "$file" --local-dir "$dest/.hfdl"
  if [ -f "$dest/.hfdl/$file" ]; then
    mv "$dest/.hfdl/$file" "$target"
  else
    found="$(find "$dest/.hfdl" -type f -name "$base_name" | head -n1)"
    [ -n "$found" ] || { echo "未找到 $file"; return 1; }
    mv "$found" "$target"
  fi
  rm -rf "$dest/.hfdl"
}
```

单文件也可直接写死命令，例如：

```bash
aria2c -x 16 -s 16 -k 1M --file-allocation=none --continue=true \
  -d /root/autodl-tmp/ComfyUI/models/diffusion_models \
  -o wan2.2_animate_14B_bf16.safetensors \
  "https://hf-mirror.com/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_animate_14B_bf16.safetensors"
```

### 6.3 必下模型清单（Wan2.2-Animate 口播主引擎，约 40GB）

先确定精度变量（与 `.env` 一致）：

```bash
# ≥32GB 显存
ANIMATE_VARIANT=bf16
# 24~32GB 显存改用：
# ANIMATE_VARIANT=int8_convrot

M=/root/autodl-tmp/ComfyUI/models
```

按顺序执行（可分多天、可中断后重跑；`dl` 会跳过已存在文件）：

```bash
# ---- 共享组件 ----
dl "Comfy-Org/Wan_2.1_ComfyUI_repackaged" \
  "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors" \
  "$M/text_encoders"

dl "Comfy-Org/Wan_2.1_ComfyUI_repackaged" \
  "split_files/clip_vision/clip_vision_h.safetensors" \
  "$M/clip_vision"

dl "Comfy-Org/Wan_2.1_ComfyUI_repackaged" \
  "split_files/vae/wan_2.1_vae.safetensors" \
  "$M/vae"

# lightx2v 蒸馏 LoRA（注意：保存时去掉 Lightx2v/ 前缀）
dl "Kijai/WanVideo_comfy" \
  "Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors" \
  "$M/loras" \
  "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"

# SAM3 人物掩码
dl "Comfy-Org/sam3.1" \
  "checkpoints/sam3.1_multiplex_fp16.safetensors" \
  "$M/checkpoints"

# ---- Wan2.2-Animate 主模型 + relight LoRA ----
dl "Comfy-Org/Wan_2.2_ComfyUI_Repackaged" \
  "split_files/diffusion_models/wan2.2_animate_14B_${ANIMATE_VARIANT}.safetensors" \
  "$M/diffusion_models"

dl "Comfy-Org/Wan_2.2_ComfyUI_Repackaged" \
  "split_files/loras/wan2.2_animate_14B_relight_lora_bf16.safetensors" \
  "$M/loras"

# ---- 原生预处理：姿态 / 人体检测 ----
dl "Comfy-Org/SDPose" \
  "checkpoints/sdpose_wholebody_fp16.safetensors" \
  "$M/checkpoints"

dl "Comfy-Org/SDPose" \
  "diffusion_models/rt_detr_v4-x-hgnet_fp16.safetensors" \
  "$M/diffusion_models"
```

下载完成后自检：

```bash
ls -lh "$M"/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors
ls -lh "$M"/clip_vision/clip_vision_h.safetensors
ls -lh "$M"/vae/wan_2.1_vae.safetensors
ls -lh "$M"/loras/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors
ls -lh "$M"/loras/wan2.2_animate_14B_relight_lora_bf16.safetensors
ls -lh "$M"/checkpoints/sam3.1_multiplex_fp16.safetensors
ls -lh "$M"/checkpoints/sdpose_wholebody_fp16.safetensors
ls -lh "$M"/diffusion_models/wan2.2_animate_14B_${ANIMATE_VARIANT}.safetensors
ls -lh "$M"/diffusion_models/rt_detr_v4-x-hgnet_fp16.safetensors
```

| 本地路径（相对 `ComfyUI/models/`） | 用途 |
| :--- | :--- |
| `text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors` | 文本编码器 |
| `clip_vision/clip_vision_h.safetensors` | CLIP Vision |
| `vae/wan_2.1_vae.safetensors` | VAE |
| `loras/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors` | 4~8 步蒸馏加速 |
| `loras/wan2.2_animate_14B_relight_lora_bf16.safetensors` | 换人模式重打光 |
| `checkpoints/sam3.1_multiplex_fp16.safetensors` | SAM3 掩码 |
| `checkpoints/sdpose_wholebody_fp16.safetensors` | 全身关键点 |
| `diffusion_models/wan2.2_animate_14B_bf16.safetensors`（或 `int8_convrot`） | 主扩散模型 |
| `diffusion_models/rt_detr_v4-x-hgnet_fp16.safetensors` | 人体检测 |

### 6.4 可选：SCAIL-2 备选引擎（约 +30GB）

仅在需要复杂 3D 动作 / 非人角色时下载：

```bash
SCAIL_VARIANT=fp8_scaled   # 或 fp16

dl "Comfy-Org/SCAIL-2" \
  "diffusion_models/wan2.1_14B_SCAIL_2_${SCAIL_VARIANT}.safetensors" \
  "$M/diffusion_models"

dl "Comfy-Org/SCAIL-2" \
  "loras/wan2.1_SCAIL_2_DPO_lora_bf16.safetensors" \
  "$M/loras"

dl "Comfy-Org/SCAIL-2" \
  "loras/wan2.1_SCAIL_2_relight_lora_bf16.safetensors" \
  "$M/loras"
```

`.env` 需改为：

```bash
SCAILSWAP_ENGINE=scail2
SCAILSWAP_UNET=wan2.1_14B_SCAIL_2_fp8_scaled.safetensors
```

### 6.5 可选：Wav2Lip 口型精修权重

```bash
cd /root/pic2video_workflow
git clone https://github.com/Rudrabha/Wav2Lip Wav2Lip || true
mkdir -p Wav2Lip/checkpoints
# 权重需从 Wav2Lip 官方发布页自行获取 wav2lip_gan.pth
# 放到：/root/pic2video_workflow/Wav2Lip/checkpoints/wav2lip_gan.pth
```

`.env`：

```bash
WAV2LIP_DIR=/root/pic2video_workflow/Wav2Lip
WAV2LIP_CHECKPOINT=/root/pic2video_workflow/Wav2Lip/checkpoints/wav2lip_gan.pth
```

---

## 7. 配置 `.env`

```bash
cd /root/pic2video_workflow
cp .env.example .env
mkdir -p /root/autodl-tmp/scailswap-data
```

按显存与路径至少改这几项（可用 `nano .env` / `vim .env`）：

```bash
SCAILSWAP_ENGINE=wan_animate
SCAILSWAP_DATA_DIR=/root/autodl-tmp/scailswap-data

COMFYUI_URL=http://127.0.0.1:8188
COMFYUI_CHUNK_TIMEOUT=1800

# 默认 bf16；若下的是 int8_convrot，务必改成：
# WANANIMATE_UNET=wan2.2_animate_14B_int8_convrot.safetensors
WANANIMATE_UNET=wan2.2_animate_14B_bf16.safetensors

SCAILSWAP_HOST=0.0.0.0
SCAILSWAP_PORT=8000
```

其余模型文件名与 `setup.sh` / 第 6.3 节下载名一致即可（`.env.example` 已写好默认值）。

---

## 8. 启动服务

建议开 **两个终端**（或 `tmux` / `screen`）。

### 8.1 终端 1：ComfyUI（GPU 推理）

```bash
cd /root/autodl-tmp/ComfyUI
# 或：cd /root/pic2video_workflow/ComfyUI
python3 main.py --listen 127.0.0.1 --port 8188
```

若要从 AutoDL「自定义服务」外网访问 ComfyUI UI，可改为：

```bash
python3 main.py --listen 0.0.0.0 --port 8188
```

看到类似 `To see the GUI go to: http://127.0.0.1:8188` 即表示就绪。

### 8.2 终端 2：ScailSwap API

```bash
cd /root/pic2video_workflow
# 加载 .env 中的端口等（start_api.sh 会读环境；也可先 source）
set -a && source .env && set +a
./scripts/start_api.sh
```

Swagger：`http://127.0.0.1:8000/docs`  
健康检查：浏览器打开 AutoDL 映射后的 `8000` 端口，或：

```bash
curl -s http://127.0.0.1:8000/docs | head
```

### 8.3 后台常驻（可选）

```bash
# tmux 示例
tmux new -s comfy -d "cd /root/autodl-tmp/ComfyUI && python3 main.py --listen 127.0.0.1 --port 8188"
tmux new -s api  -d "cd /root/pic2video_workflow && set -a && source .env && set +a && ./scripts/start_api.sh"
tmux ls
```

---

## 9. 验证是否跑通

准备一张人脸图 `face.jpg` 与一段口播视频 `talk.mp4`（可放数据盘），然后：

```bash
cd /root/pic2video_workflow

curl -X POST http://127.0.0.1:8000/api/v1/jobs \
  -F "source_image=@/root/autodl-tmp/face.jpg" \
  -F "target_video=@/root/autodl-tmp/talk.mp4" \
  -F "prompt=一位对着镜头讲话的人" \
  -F "target_fps=16" -F "output_fps=30" \
  -F 'params_json={"seed": 42, "resolution_tier": 512, "max_duration_seconds": 8}'
# → {"job_id":"...","status":"queued"}

# 轮询
curl http://127.0.0.1:8000/api/v1/jobs/<job_id>

# 完成后下载
curl -o /root/autodl-tmp/final.mp4 \
  http://127.0.0.1:8000/api/v1/jobs/<job_id>/download
```

或用仓库示例脚本：

```bash
python examples/test_api.py \
  --image /root/autodl-tmp/face.jpg \
  --video /root/autodl-tmp/talk.mp4 \
  --prompt "一位对着镜头讲话的人" \
  --output /root/autodl-tmp/final.mp4
```

首次推理会加载多个模型，单块可能要数分钟；`max_duration_seconds=8` 适合冒烟测试。

---

## 10. 常用运维

### 更新代码

```bash
cd /root/pic2video_workflow
git pull origin main
./setup.sh    # 只同步 Python 依赖；模型不会重复下载（已存在会跳过）
```

### 磁盘与中间文件

- 任务数据在 `SCAILSWAP_DATA_DIR`（建议数据盘）
- 长视频务必保持默认 `cleanup_intermediate=true`，否则中间文件会暴涨
- 查看占用：`du -sh /root/autodl-tmp/ComfyUI/models /root/autodl-tmp/scailswap-data`

### 显存 OOM

1. 改用 `ANIMATE_VARIANT=int8_convrot` 并同步 `.env` 的 `WANANIMATE_UNET`
2. `.env` 降低 `WANANIMATE_POSE_BATCH=8`
3. 请求里用 `resolution_tier=512`、`target_fps=16`
4. API 侧检测到 OOM 会自动 `/free` 并重试；仍不行就重启 ComfyUI

### 下载很慢 / 401

```bash
export HF_ENDPOINT=https://hf-mirror.com
export HF_HUB_DISABLE_XET=1
# 确认用的是 aria2c，而不是裸 curl 单线程
command -v aria2c
```

强制直连官方（一般更慢，不推荐 AutoDL）：

```bash
HF_ENDPOINT= ./setup.sh --with-comfyui
```

### ComfyUI 400：`unet_name ... not in list` / `split_files/diffusion_models/...`

报错形如：

```text
unet_name: 'wan2.2_animate_14B_bf16.safetensors' not in
['rt_detr_v4-x-hgnet_fp16.safetensors',
 'split_files/diffusion_models/wan2.2_animate_14B_bf16.safetensors']
```

**原因**：手动 `hf download --local-dir` 时保留了仓库内相对路径，文件落在
`models/diffusion_models/split_files/diffusion_models/...`，而 `.env` 期望的是
扁平文件名 `models/diffusion_models/wan2.2_animate_14B_bf16.safetensors`。

服务本身是好的（`/health` 里 `engine.ok: true`）；这是**模型文件位置**问题。

一键排查（把 `M` 改成你的 ComfyUI models 目录）：

```bash
M=/root/autodl-tmp/ComfyUI/models   # 或 ~/pic2video_workflow/ComfyUI/models
# 1) ComfyUI 实际能看到哪些 diffusion 权重
curl -s http://127.0.0.1:8188/object_info/UNETLoader | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print('\n'.join(d['UNETLoader']['input']['required']['unet_name'][0]))"
# 2) 磁盘上真实路径（找嵌套的 split_files）
find "$M" -type f -name '*.safetensors' | sort
find "$M" -type d -name split_files
# 3) .env 里配置的名字
grep -E 'WANANIMATE_|SCAILSWAP_UNET' /root/pic2video_workflow/.env
```

修复（把嵌套文件挪到对应子目录根下，再重启 ComfyUI）：

```bash
M=/root/autodl-tmp/ComfyUI/models

# 主模型
mv -n "$M/diffusion_models/split_files/diffusion_models/"*.safetensors \
      "$M/diffusion_models/" 2>/dev/null || true
# 其它常见嵌套（按实际 find 结果执行）
mv -n "$M/text_encoders/split_files/text_encoders/"*.safetensors \
      "$M/text_encoders/" 2>/dev/null || true
mv -n "$M/clip_vision/split_files/clip_vision/"*.safetensors \
      "$M/clip_vision/" 2>/dev/null || true
mv -n "$M/vae/split_files/vae/"*.safetensors \
      "$M/vae/" 2>/dev/null || true
mv -n "$M/loras/split_files/loras/"*.safetensors \
      "$M/loras/" 2>/dev/null || true
mv -n "$M/checkpoints/checkpoints/"*.safetensors \
      "$M/checkpoints/" 2>/dev/null || true

# 清掉空的嵌套目录（可选）
find "$M" -type d -name split_files -exec rm -rf {} + 2>/dev/null || true

# 确认扁平名存在
ls -lh "$M/diffusion_models/wan2.2_animate_14B_bf16.safetensors"
# 重启 ComfyUI 后再 curl 一次 UNETLoader，列表里应只有文件名、没有 split_files/ 前缀
```

临时绕过（不推荐，其它模型也可能嵌套）：在 `.env` 写

```bash
WANANIMATE_UNET=split_files/diffusion_models/wan2.2_animate_14B_bf16.safetensors
```

然后重启 API。仍建议按上面把文件挪平。

### 素材 FileNotFoundError：`face.jpg`

```bash
ls -lh /root/autodl-tmp/face.* /root/autodl-tmp/talk.*
# 用真实存在的路径，例如 face.png
python examples/test_api.py --image /root/autodl-tmp/face.png --video /root/autodl-tmp/talk.mp4 ...
```

### 模型放网盘再拷到实例

在能高速访问 HF 的机器上下好后，按第 6.3 节目录结构打包，再传到 AutoDL 数据盘：

```bash
# 示例：整包 models
rsync -avP ./models/ root@<autodl>:/root/autodl-tmp/ComfyUI/models/
# 或 autodl-tmp 内直接解压你上传的 zip/tar
```

---

## 11. 无 GPU 验证（非 AutoDL 主路径）

仅验证编排 / 百炼 API 时，可不装 ComfyUI：

```bash
./setup.sh
cp .env.example .env
# 编辑 .env：
#   SCAILSWAP_ENGINE=dashscope
#   DASHSCOPE_API_KEY=sk-xxxx
#   DASHSCOPE_MODEL=wan2.7-videoedit   # 实验性长视频
./scripts/start_api.sh
```

详见 [README.md](README.md) 与 [DESIGN.md](DESIGN.md)。

---

## 12. 一页速查（口播主引擎最小命令）

```bash
# 依赖
apt-get update && apt-get install -y ffmpeg aria2 git
export HF_ENDPOINT=https://hf-mirror.com
export HF_HUB_DISABLE_XET=1

# 代码
cd /root && git clone https://github.com/yyyyyyzt/pic2video_workflow.git
cd pic2video_workflow && git pull origin main

# 一键（模型进数据盘）
COMFYUI_DIR=/root/autodl-tmp/ComfyUI ./setup.sh --with-comfyui
# 24GB 卡：ANIMATE_VARIANT=int8_convrot COMFYUI_DIR=/root/autodl-tmp/ComfyUI ./setup.sh --with-comfyui

ln -sfn /root/autodl-tmp/ComfyUI ./ComfyUI
cp .env.example .env
# 改 SCAILSWAP_DATA_DIR=/root/autodl-tmp/scailswap-data
# 若 int8：改 WANANIMATE_UNET=wan2.2_animate_14B_int8_convrot.safetensors

# 启动
# 终端1: python3 /root/autodl-tmp/ComfyUI/main.py --listen 127.0.0.1 --port 8188
# 终端2: ./scripts/start_api.sh
```

手动下模型时，复制 **第 6.2 + 6.3** 整段命令即可。
