#!/bin/bash
# 单独下载识别模型到本地（只需在能访问谷歌的机器上跑一次）
cd "$(dirname "$0")" || exit 1
mkdir -p static/models
echo "下载 face_landmarker.task ..."
curl -fSL "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task" \
  -o static/models/face_landmarker.task
if [ -s static/models/face_landmarker.task ]; then
  echo "完成：static/models/face_landmarker.task ($(du -h static/models/face_landmarker.task | cut -f1))"
  echo "现在项目已完全离线，访客不再需要谷歌。"
else
  echo "下载失败。请在能访问 storage.googleapis.com 的网络下重试。"
fi
