"""
FaceEcho —— 表情识别与虚拟形象 Web 应用（本地服务器）
用 Flask 提供前端页面（静态资源在 static/ 下）。
人脸识别与表情分析全部在访问者浏览器本地运行，服务器只负责发送网页。

启动：
    python app.py                # 默认 http://localhost:8002
    PORT=9000 python app.py      # 换端口
    HTTPS=1 python app.py        # 用自签名 HTTPS（局域网里别的设备要开摄像头时需要）
"""
import os
from flask import Flask, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


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
    kwargs = {"host": "0.0.0.0", "port": port, "debug": False}
    if os.environ.get("HTTPS"):
        # 需要先 pip install pyopenssl
        kwargs["ssl_context"] = "adhoc"
        scheme = "https"
    else:
        scheme = "http"
    print(f"\n  FaceEcho 已启动： {scheme}://localhost:{port}\n")
    app.run(**kwargs)
