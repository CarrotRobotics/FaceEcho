"""
FaceEcho —— 表情识别与虚拟形象 Web 应用（本地服务器）
用 Flask 提供前端页面（静态资源在 static/ 下）。
人脸识别与表情分析全部在访问者浏览器本地运行，服务器只负责发送网页。

启动：
    python app.py                # 默认 http://localhost:8002
    PORT=9000 python app.py      # 换端口
    HTTPS=1 python app.py        # 用自签名 HTTPS（局域网里别的设备要开摄像头时需要）
    HOST=127.0.0.1 python app.py # 只允许本机访问（Jetson 上做机器人时推荐）

机器人接口（可选，网页里勾选「📡 发送到机器人」或用 ?robot=1 打开后才有数据）：
    GET  /api/affect   → 最新的 {v, a, dist, observed, gap_s, mood, age_s}
    POST /api/affect   ← 网页端以 ≤10Hz 推送（只有数字，不含图像）
"""
import os, threading, time
from flask import Flask, send_from_directory, request, jsonify

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

app = Flask(__name__, static_folder=None)
app.json.ensure_ascii = False


@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


_affect = {}
_affect_lock = threading.Lock()


@app.route("/api/affect", methods=["GET", "POST"])
def affect():
    if request.method == "POST":
        d = request.get_json(silent=True) or {}
        keep = {k: d[k] for k in ("v", "a", "dist", "dist_raw", "observed", "gap_s", "mood", "t_client",
                                 "comp_mood", "say_id", "say_line", "say_lang", "say_text") if k in d}
        keep["t_server"] = time.time()
        with _affect_lock:
            _affect.clear(); _affect.update(keep)
        return ("", 204)
    with _affect_lock:
        out = dict(_affect)
    if not out:
        return jsonify({"ok": False, "reason": "暂无数据：网页未打开或未勾选「发送到机器人」"}), 503
    out["age_s"] = round(time.time() - out["t_server"], 3)   # 控制端据此判断数据是否过期
    out["ok"] = True
    return jsonify(out)


@app.route("/<path:path>")
def assets(path):
    # 提供 static/ 目录下的任意文件（js、模型、图片等）
    return send_from_directory(STATIC, path)


@app.after_request
def add_headers(resp):
    # 摄像头/媒体权限相关，允许本页面使用摄像头
    resp.headers["Permissions-Policy"] = "camera=(self)"
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8002))
    kwargs = {"host": os.environ.get("HOST", "0.0.0.0"), "port": port, "debug": False, "threaded": True}
    if os.environ.get("HTTPS"):
        # 需要先 pip install pyopenssl
        kwargs["ssl_context"] = "adhoc"
        scheme = "https"
    else:
        scheme = "http"
    print(f"\n  FaceEcho 已启动： {scheme}://localhost:{port}\n")
    app.run(**kwargs)
