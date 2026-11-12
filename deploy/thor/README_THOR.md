# 在 Jetson AGX Thor 上部署 FaceEcho（含 FaceLNN 连续情绪 · 安全距离）

目标环境：Jetson AGX Thor，JetPack 7.x（Ubuntu 24.04、aarch64），桌面已登录，接 USB 摄像头或 RealSense。

整套东西在 Thor 上**完全本地、离线**运行：Flask 只发静态网页，MediaPipe（WASM + WebGL）和 GRU-D 都在本机
Firefox 里跑，3D 形象需要的 three.js 也已放进 `static/vendor/`，不再依赖 CDN。

---

## 第 1 步 · 在 5090 上训练并导出 GRU-D 权重（一次）

数据（Aff-Wild2 / AFEW-VA）和训练都在 5090 上，Thor 只需要导出的 JSON。

```bash
conda activate face
cd ~/FaceLNN
cp /path/to/FaceEcho/tools/train_export_grud.py .
# 主库 Aff-Wild2，工作点 W_DANGER=4（与论文主结果一致）
python train_export_grud.py --data_root data/affwild2 --w_danger 4 --out grud_safety.json
```

结束时会打印验证集（0% / 35% 丢帧）的 CCC、危险违规、过远违规，和论文表里的数对一下，
差得不多就说明导出的就是那个模型。产物：`grud_safety.json`（网页用）+ `grud_safety.pt`（备份）。

> 已经有训练好的 GRUD `state_dict`？不用重训：`python train_export_grud.py --ckpt xxx.pt --out grud_safety.json`
>
> ⚠️ 权重是用有 EULA 的数据训出来的。`.gitignore` 默认不提交它，用 `scp` 拷到 Thor；
> 要放进公开仓库前先确认 Aff-Wild2 / AFEW-VA 的协议是否允许发布衍生模型。

## 第 2 步 · 把工程放到 Thor 上

```bash
# Thor 上
git clone -b lnn-safe-affect-thor https://github.com/CarrotRobotics/FaceEcho.git ~/FaceEcho
# 5090 上，把权重推过去（IP 换成 Thor 的）
scp ~/FaceLNN/grud_safety.json  <user>@<thor-ip>:~/FaceEcho/static/models/
```

## 第 3 步 · 一键安装

```bash
cd ~/FaceEcho
bash deploy/thor/setup_thor.sh                          # 普通 USB 摄像头
bash deploy/thor/setup_thor.sh --realsense              # 用 RealSense 时
bash deploy/thor/setup_thor.sh --realsense --autostart  # 再加开机自动全屏运行
```

脚本做的事：建 `.venv` 装 Flask → 下载 `face_landmarker.task` → 检查 GRU-D 权重 → 装/检查 Firefox
→（可选）v4l2loopback 虚拟摄像头 →（可选）systemd 用户服务 + 登录后自动 kiosk。

## 第 4 步 · 运行

```bash
bash deploy/thor/start_kiosk.sh          # 全屏，自动开摄像头，自动推送 /api/affect
KIOSK=0 bash deploy/thor/start_kiosk.sh  # 普通窗口，调试用（Alt+F4 / Ctrl+Q 退出 kiosk）
```

看到页面下方「🧠 连续情绪 · 安全社交距离」面板显示 **GRU-D 已加载**，就说明新算法在跑。
把脸移出画面：面板标红「丢帧·外推」，距离仍在连续输出（旧版这时会整页冻结）。

## 第 5 步 · 给机器人控制端用

```bash
curl -s http://localhost:8002/api/affect
# {"ok":true,"v":0.31,"a":0.12,"dist":1.31,"observed":true,"gap_s":0,"mood":"happy","age_s":0.04,...}
```

| 字段 | 含义 |
|---|---|
| `v` / `a` | 连续 valence / arousal |
| `dist` | 建议社交距离（米），已钳在 [0.3, 2.5] |
| `observed` / `gap_s` | 这一帧是否看到脸；连续多久没看到 |
| `age_s` | 数据新鲜度：网页挂了/卡了会变大，控制端应设超时（例如 > 0.5 s 视为失效） |

只推数字，不推图像；服务默认只监听 `127.0.0.1`。需要的话可以写个 ROS 2 节点轮询这个接口发布 topic。

---

## 排错

| 现象 | 处理 |
|---|---|
| 状态栏显示「模型就绪(CPU)」 | Firefox 没给 WebGL，MediaPipe 自动退回 CPU，能用但慢。地址栏开 `about:support` 看 WebGL；可试 `MOZ_ENABLE_WAYLAND=1` 或换 X11 会话 |
| 面板「模型未加载」 | `static/models/grud_safety.json` 不在 / 拼错名 |
| 下拉里没有 RealSenseRGB | `lsmod \| grep v4l2loopback`；没加载就按 setup 脚本的提示从源码编；`cat /tmp/faceecho_ffmpeg.log` |
| RealSense 本身不出图 | JetPack 7 上 librealsense / 内核补丁支持仍在完善中，先用 `v4l2-ctl --list-devices` 确认系统认到相机；实在不行先用 USB 摄像头 |
| 端口被占 | `PORT=9000 bash deploy/thor/start_kiosk.sh` |
| 查看服务日志 | `journalctl --user -u faceecho -f` 或 `/tmp/faceecho_server.log` |
| 性能 | `sudo nvpmodel -q` 查看功耗模式；需要满血 `sudo nvpmodel -m 0 && sudo jetson_clocks` |
