#pragma once
// ======== 改成你自己的 ========
#define WIFI_SSID   "your-2.4GHz-wifi"     // CoreS3 只支持 2.4GHz
#define WIFI_PASS   "your-password"
#define THOR_HOST   "192.168.1.105"        // Thor 的局域网 IP（hostname -I 第一个）
#define THOR_PORT   8002
// ==============================
#define POLL_MS     150                    // 多久向 Thor 要一次情绪
#define STALE_S     1.5f                   // 数据超过这么久没更新就显示“离线”
#define VOLUME      200                    // 喇叭音量 0~255
