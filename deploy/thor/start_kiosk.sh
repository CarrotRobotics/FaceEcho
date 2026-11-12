#!/bin/bash
# 在 Jetson 上启动 FaceEcho：(RealSense 喂流) → 网页服务器 → Firefox 全屏 kiosk（自动授权摄像头、开启 /api/affect）
#   bash deploy/thor/start_kiosk.sh            # 全屏
#   KIOSK=0 bash deploy/thor/start_kiosk.sh    # 普通窗口（调试用）
cd "$(dirname "$0")/../.." || exit 1
APP="$(pwd)"; PORT="${PORT:-8002}"; URL="http://localhost:$PORT/?robot=1&autostart=1"
PY="$APP/.venv/bin/python"; [ -x "$PY" ] || PY=python3

# 1) RealSense：找到彩色(YUYV)节点，喂给 v4l2loopback 的 /dev/video10（普通 USB 摄像头会自动跳过）
if command -v v4l2-ctl >/dev/null && [ -e /dev/video10 ] && ! pgrep -f "f v4l2 /dev/video10" >/dev/null; then
  for d in /dev/video*; do
    [ "$d" = /dev/video10 ] && continue
    if v4l2-ctl -d "$d" --info 2>/dev/null | grep -qi realsense && v4l2-ctl -d "$d" --list-formats 2>/dev/null | grep -q YUYV; then
      echo "RealSense 彩色节点：$d → /dev/video10"
      nohup ffmpeg -loglevel error -use_wallclock_as_timestamps 1 -f v4l2 -input_format yuyv422 \
        -video_size 640x480 -framerate 30 -i "$d" -pix_fmt yuyv422 -f v4l2 /dev/video10 >/tmp/faceecho_ffmpeg.log 2>&1 &
      sleep 1; break
    fi
  done
fi

# 2) 网页服务器（若 systemd 服务已在跑就不重复起）
if ! curl -s -o /dev/null "http://localhost:$PORT/"; then
  HOST=127.0.0.1 PORT=$PORT nohup "$PY" app.py >/tmp/faceecho_server.log 2>&1 &
  for i in $(seq 1 30); do curl -s -o /dev/null "http://localhost:$PORT/" && break; sleep 0.5; done
fi

# 3) Firefox 专用 profile：自动允许摄像头（只用于这个本机 kiosk 页面）
#    snap 版 Firefox 只能访问 ~/snap/firefox/common 之类目录，所以 profile 放那里
if snap list firefox >/dev/null 2>&1; then PROF="$HOME/snap/firefox/common/faceecho-kiosk"; else PROF="$HOME/.faceecho-firefox"; fi
mkdir -p "$PROF"
cat > "$PROF/user.js" <<'EOF'
user_pref("media.navigator.permission.disabled", true);
user_pref("permissions.default.camera", 1);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("browser.aboutwelcome.enabled", false);
EOF
ARGS=(--profile "$PROF" --no-remote)
[ "${KIOSK:-1}" = 1 ] && ARGS+=(--kiosk)
exec firefox "${ARGS[@]}" "$URL"
