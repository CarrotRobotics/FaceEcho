#!/bin/bash
# 一键运行本地 Web 工程
cd "$(dirname "$0")" || exit 1

# 首次运行自动装依赖
if ! python -c "import flask" 2>/dev/null; then
  echo "==> 安装依赖 (Flask)..."
  pip install -r requirements.txt
fi

# 若使用 RealSense：确保虚拟摄像头存在并在喂流（可选，普通网络摄像头不需要）
if command -v v4l2-ctl >/dev/null 2>&1 && v4l2-ctl --list-devices 2>/dev/null | grep -q "Intel(R) RealSense"; then
  if ! v4l2-ctl --list-devices 2>/dev/null | grep -q "RealSenseRGB"; then
    echo "==> 创建 RealSense 虚拟摄像头（需要 sudo 密码）..."
    sudo modprobe v4l2loopback video_nr=10 card_label="RealSenseRGB" exclusive_caps=1
  fi
  if ! pgrep -f "i /dev/video4" >/dev/null 2>&1; then
    echo "==> 启动 RealSense 彩色流喂入虚拟摄像头..."
    nohup ffmpeg -use_wallclock_as_timestamps 1 -f v4l2 -input_format yuyv422 \
      -video_size 640x480 -framerate 30 -i /dev/video4 \
      -pix_fmt yuyv422 -f v4l2 /dev/video10 >/tmp/ffmpeg_cam.log 2>&1 &
  fi
fi

# 首次运行下载识别模型到本地（之后就完全离线，不依赖谷歌）
MODEL="static/models/face_landmarker.task"
if [ ! -s "$MODEL" ]; then
  echo "==> 首次运行：下载识别模型到本地 ($MODEL) ..."
  mkdir -p static/models
  curl -fSL "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task" -o "$MODEL" \
    && echo "    模型下载完成，之后访问不再需要谷歌" \
    || echo "    ！模型下载失败，请手动运行 bash get_model.sh"
fi

echo "==> 启动网页服务器..."
python app.py
