# FaceEcho 伙伴 · M5Stack CoreS3（Wi-Fi）

CoreS3 当机器人的「脸和嘴」：屏幕上显示情绪反馈伙伴的表情、台词和建议距离，喇叭说出台词（日/中/英，跟网页当前语言一致）。
它通过 **Wi-Fi** 读 Thor 上的 `/api/affect`，**不占 Thor 的 USB 口**，USB-C 只用来供电（手机充电头、充电宝、任意 USB 口都行）。

```
Thor：FaceEcho 网页 ──推送──> /api/affect（情绪、台词编号、语言、文字、距离）
                                    ▲ 每 0.15 s 读一次（Wi-Fi）
CoreS3：画表情 / 显示台词 / 距离条；新台词 → 下载 /voice/{lang}/{key}.wav → 喇叭播放
```

## 1. 准备语音（在 5090 上，一次）

`tools/make_voices.py` 会同时生成网页用的 mp3 和 CoreS3 用的 16kHz WAV（需要 `ffmpeg`）：

```bash
cd ~/FaceEcho
pip install edge-tts
python tools/make_voices.py
git add static/voice && git commit -m "Add companion voices" && git push
```

## 2. 填配置

改 `cores3/src/config.h`：Wi-Fi 名称和密码（**必须是 2.4GHz**）、Thor 的 IP（`hostname -I` 的第一个，比如 `192.168.1.105`）。

## 3. 刷机（在 5090 上，用 USB-C 线连 CoreS3）

```bash
conda activate face
pip install platformio
cd ~/FaceEcho/cores3
pio run -t upload          # 第一次会下载 ESP32 工具链，要几分钟
pio device monitor         # 可选：看日志，会打印 CoreS3 拿到的 IP
```

刷不进去时：按住 CoreS3 侧面的复位键约 3 秒（绿灯亮）进入下载模式，再运行 `pio run -t upload`。

## 4. 运行

1. Thor 上照常启动：`bash deploy/thor/start_kiosk.sh`（地址里带 `robot=1`，服务器默认对局域网开放）。
2. CoreS3 插上电，自动连 Wi-Fi，屏幕出现伙伴。
3. 在 5090 或手机浏览器打开 `http://<Thor的IP>:8002/api/affect` 能看到 JSON，就说明 CoreS3 也能连上。

- **触摸屏幕**：静音 / 取消静音
- 屏幕底部：建议距离条，右下角 `face` = 看到脸，`extrap` = 丢帧外推，`MUTE` = 已静音
- 台词框显示 `Thor offline`：网页没开、没勾「发送到机器人」，或者 Thor 的 IP 填错了

## 排错

| 现象 | 处理 |
|---|---|
| 一直显示「Wi-Fi 接続中…」 | Wi-Fi 是 5GHz 或密码错；CoreS3 只支持 2.4GHz |
| `Thor offline` | 5090 上 `curl http://192.168.1.105:8002/api/affect` 试试；Thor 的服务要以 `HOST=0.0.0.0` 启动（start_kiosk 默认如此） |
| 有表情没声音 | 先跑 `make_voices.py` 生成 `.wav`；检查是不是静音（右下角 MUTE）；`config.h` 里 `VOLUME` 调大 |
| 中文显示成方块 | 用的字体是 efontCN_16 / efontJA_16，已包含常用汉字；生僻字可能缺 |

安全提醒：`/api/affect` 只有情绪数字和台词，不含图像；但服务对局域网开放后，同一网络里的人都能读到。只想本机用时 `HOST=127.0.0.1 bash deploy/thor/start_kiosk.sh`（CoreS3 就连不上了）。
