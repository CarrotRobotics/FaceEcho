#!/bin/bash
# 在 Jetson 上启动 FaceEcho：(RealSense 喂流) → 网页服务器 → Firefox 全屏 kiosk（自动授权摄像头、开启 /api/affect）
#   bash deploy/thor/start_kiosk.sh            # 全屏
#   KIOSK=0 bash deploy/thor/start_kiosk.sh    # 普通窗口（调试用）
#   HOST=127.0.0.1 bash deploy/thor/start_kiosk.sh  # 只允许本机访问（默认 0.0.0.0，局域网里的 CoreS3 要能连进来）
#   DELEGATE=GPU bash deploy/thor/start_kiosk.sh  # 浏览器能用上 NVIDIA 驱动时再试 GPU（snap 版 Firefox 用不上，默认 CPU）
#
# 第一次运行会弹摄像头权限框：选 RealSenseRGB，勾「记住此决定」。之后页面会自动选中它。
cd "$(dirname "$0")/../.." || exit 1
APP="$(pwd)"; PORT="${PORT:-8002}"; URL="http://localhost:$PORT/?robot=1&autostart=1&delegate=${DELEGATE:-CPU}"
PY="$APP/.venv/bin/python"; [ -x "$PY" ] || PY=python3

# 1) RealSense：找到彩色(YUYV)节点，喂给 v4l2loopback 的 /dev/video10（普通 USB 摄像头会自动跳过）
#    JetPack 自带的 ffmpeg 常常没编 v4l2 输入 → 自动改用 GStreamer
if command -v v4l2-ctl >/dev/null && [ -e /dev/video10 ] && ! pgrep -f "/dev/video10" >/dev/null; then
  for d in /dev/video*; do
    [ "$d" = /dev/video10 ] && continue
    if v4l2-ctl -d "$d" --info 2>/dev/null | grep -qi realsense && v4l2-ctl -d "$d" --list-formats 2>/dev/null | grep -q YUYV; then
      if ffmpeg -hide_banner -devices 2>/dev/null | grep -qi v4l2; then
        echo "RealSense 彩色节点：$d → /dev/video10 (ffmpeg)"
        nohup ffmpeg -loglevel error -use_wallclock_as_timestamps 1 -f v4l2 -input_format yuyv422 \
          -video_size 640x480 -framerate 30 -i "$d" -pix_fmt yuyv422 -f v4l2 /dev/video10 >/tmp/faceecho_ffmpeg.log 2>&1 &
      else
        echo "RealSense 彩色节点：$d → /dev/video10 (GStreamer)"
        nohup gst-launch-1.0 -q v4l2src device="$d" ! video/x-raw,format=YUY2,width=640,height=480,framerate=30/1 \
          ! v4l2sink device=/dev/video10 sync=false >/tmp/faceecho_ffmpeg.log 2>&1 &
      fi
      sleep 2; break
    fi
  done
fi

# 2) 网页服务器（若 systemd 服务已在跑就不重复起）
if ! curl -s -o /dev/null "http://localhost:$PORT/"; then
  HOST="${HOST:-0.0.0.0}" PORT=$PORT nohup "$PY" app.py >/tmp/faceecho_server.log 2>&1 &
  for i in $(seq 1 30); do curl -s -o /dev/null "http://localhost:$PORT/" && break; sleep 0.5; done
fi

# 3) Firefox 专用 profile
#    不自动授权摄像头：自动授权时 Firefox 不暴露设备名、会默认打开 RealSense 的深度节点而失败。
#    第一次手动选 RealSenseRGB 并勾“记住”，以后就自动了。
#    snap 版 Firefox 只能访问 ~/snap/firefox/common 之类目录，所以 profile 放那里
if snap list firefox >/dev/null 2>&1; then PROF="$HOME/snap/firefox/common/faceecho-kiosk"; else PROF="$HOME/.faceecho-firefox"; fi
mkdir -p "$PROF"
cat > "$PROF/user.js" <<'EOF'
user_pref("media.autoplay.default", 0);              // 允许伙伴的语音自动播放（kiosk 自动启动时没有点击）
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("browser.aboutwelcome.enabled", false);
EOF
ARGS=(--profile "$PROF" --no-remote)
[ "${KIOSK:-1}" = 1 ] && ARGS+=(--kiosk)
exec firefox "${ARGS[@]}" "$URL"
