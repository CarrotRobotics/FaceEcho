#!/bin/bash
# FaceEcho 在 Jetson AGX Thor（JetPack 7.x / Ubuntu 24.04 / aarch64）上的一键安装
#
#   bash deploy/thor/setup_thor.sh                 # 基础：Python 环境 + 模型 + Firefox
#   bash deploy/thor/setup_thor.sh --realsense     # 另外装 RealSense 虚拟摄像头（v4l2loopback）
#   bash deploy/thor/setup_thor.sh --autostart     # 另外设置开机自启（服务 + 全屏 kiosk）
#
# 可重复执行（幂等）。需要 sudo。
set -e
cd "$(dirname "$0")/../.."
APP="$(pwd)"
WITH_RS=0; AUTOSTART=0
for a in "$@"; do case "$a" in --realsense) WITH_RS=1 ;; --autostart) AUTOSTART=1 ;; esac; done

say(){ printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn(){ printf '\033[1;33m  ! %s\033[0m\n' "$*"; }

say "[1/6] 平台检查"
echo "  arch: $(uname -m)   kernel: $(uname -r)"
head -1 /etc/nv_tegra_release 2>/dev/null || warn "没找到 /etc/nv_tegra_release（不在 Jetson 上？脚本照样可以继续）"
[ "$(uname -m)" = "aarch64" ] || warn "不是 aarch64"

say "[2/6] Python 环境（.venv，只需要 Flask）"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv curl
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "  Flask $(.venv/bin/python -c 'import flask;print(flask.__version__)')"

say "[3/6] 人脸识别模型 face_landmarker.task（需要能访问 storage.googleapis.com，只下一次）"
if [ -s static/models/face_landmarker.task ]; then echo "  已存在"; else bash get_model.sh || warn "下载失败：在能上网的电脑下好后拷到 $APP/static/models/"; fi

say "[4/6] FaceLNN 权重 grud_safety.json"
if [ -s static/models/grud_safety.json ]; then
  echo "  已存在：$(.venv/bin/python -c "import json;m=json.load(open('static/models/grud_safety.json'))['meta'];print(m.get('algorithm'),'| W_DANGER',m.get('w_danger'),'|',m.get('datasets'))")"
else
  warn "没有 static/models/grud_safety.json —— 从 5090 拷过来（见 deploy/thor/README_THOR.md 第 1 步）。没有它也能跑，只是没有连续情绪面板。"
fi

say "[5/6] 浏览器（Firefox：Linux 下能用 v4l2loopback 虚拟摄像头，Chrome 不行）"
if command -v firefox >/dev/null; then echo "  已安装：$(firefox --version 2>/dev/null)"
else sudo snap install firefox || sudo apt-get install -y firefox || warn "Firefox 安装失败，请手动安装"; fi
sudo apt-get install -y -qq v4l-utils >/dev/null || true

if [ "$WITH_RS" = 1 ]; then
  say "[RealSense] v4l2loopback + ffmpeg"
  sudo apt-get install -y -qq ffmpeg v4l2loopback-dkms || true
  # Jetson 内核需要对应的 headers 才能编 DKMS；包名随 L4T 版本可能不同，装不上不致命
  dpkg -l | grep -q nvidia-l4t-kernel-headers || sudo apt-get install -y -qq nvidia-l4t-kernel-headers 2>/dev/null || true
  if sudo modprobe v4l2loopback video_nr=10 card_label="RealSenseRGB" exclusive_caps=1 2>/dev/null; then
    echo "v4l2loopback" | sudo tee /etc/modules-load.d/v4l2loopback.conf >/dev/null
    echo 'options v4l2loopback video_nr=10 card_label="RealSenseRGB" exclusive_caps=1' | sudo tee /etc/modprobe.d/v4l2loopback.conf >/dev/null
    echo "  v4l2loopback 已加载，并设为开机自动加载（/dev/video10 = RealSenseRGB）"
  else
    warn "v4l2loopback 模块加载失败（多半是 DKMS 没针对 Jetson 内核编出来）。"
    warn "可从源码编：git clone https://github.com/umlaeute/v4l2loopback && cd v4l2loopback && make && sudo make install && sudo depmod -a"
    warn "或者直接用普通 USB 摄像头（UVC），完全不需要这一步。"
  fi
fi

if [ "$AUTOSTART" = 1 ]; then
  say "[6/6] 开机自启：systemd 用户服务（网页服务器）+ 桌面登录后全屏 kiosk"
  mkdir -p ~/.config/systemd/user ~/.config/autostart
  cat > ~/.config/systemd/user/faceecho.service <<EOF
[Unit]
Description=FaceEcho web server (local only)
After=network.target

[Service]
WorkingDirectory=$APP
Environment=HOST=127.0.0.1
Environment=PORT=8002
ExecStart=$APP/.venv/bin/python $APP/app.py
Restart=on-failure

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now faceecho.service
  sudo loginctl enable-linger "$USER" || true
  cat > ~/.config/autostart/faceecho-kiosk.desktop <<EOF
[Desktop Entry]
Type=Application
Name=FaceEcho Kiosk
Exec=$APP/deploy/thor/start_kiosk.sh
X-GNOME-Autostart-enabled=true
EOF
  chmod +x "$APP/deploy/thor/start_kiosk.sh"
  # 机器人屏幕不要黑屏/锁屏
  gsettings set org.gnome.desktop.session idle-delay 0 2>/dev/null || true
  gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null || true
  echo "  服务状态：$(systemctl --user is-active faceecho.service)   → 下次登录桌面会自动全屏打开"
else
  say "[6/6] 跳过开机自启（需要的话加 --autostart）"
fi

say "完成。手动启动：bash deploy/thor/start_kiosk.sh    （或 .venv/bin/python app.py 后浏览器开 http://localhost:8002）"
echo "  建议性能模式：sudo nvpmodel -q 查看当前模式；需要满血时 sudo nvpmodel -m 0 && sudo jetson_clocks"
