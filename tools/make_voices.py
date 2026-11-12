"""
为「情绪反馈伙伴」预先生成日/中/英三种语言的台词音频（只需跑一次）
=====================================================================
为什么需要它：Thor 上的 snap 版 Firefox 往往没有日语/中文的系统语音，或者音质很机械。
预先生成的 mp3 放进 static/voice/ 后，页面会优先播放它们，离线也能用。

生成用的是 edge-tts（微软在线神经语音，需要联网，**只在生成时联网一次**），在 5090 上跑就行：
    pip install edge-tts
    cd ~/FaceEcho
    python tools/make_voices.py
产物：static/voice/{ja,zh,en}/{happy,surprise,sad,angry,neutral}_{0..3}.mp3（网页用）
      + 同名 .wav（16kHz 单声道，M5Stack CoreS3 用，需要 ffmpeg）+ manifest.json
然后 git commit 推上去，Thor 上 git pull 就有了（文件很小，一共约 1 MB）。

换声音：--ja / --zh / --en 指定声音名（edge-tts --list-voices 可查看）。
只重做某种语言：--langs ja
"""
import argparse, asyncio, json, os, re, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
DEFAULT_VOICES = {"ja": "ja-JP-NanamiNeural", "zh": "zh-CN-XiaoxiaoNeural", "en": "en-US-AriaNeural"}
FFMPEG = None
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def clean(s):
    return EMOJI.sub("", s).replace("～", "").replace("~", "").strip()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--langs", nargs="*", default=["ja", "zh", "en"])
    for k, v in DEFAULT_VOICES.items():
        ap.add_argument(f"--{k}", default=v, help=f"{k} 声音名（默认 {v}）")
    ap.add_argument("--rate", default="+0%", help="语速，如 +10%%")
    a = ap.parse_args()
    try:
        import edge_tts
    except ImportError:
        sys.exit("先安装：pip install edge-tts")

    global FFMPEG
    FFMPEG = shutil.which('ffmpeg')
    if not FFMPEG:
        print('！没找到 ffmpeg：只生成 mp3（网页用）；CoreS3 需要 wav → sudo apt install ffmpeg 后重跑')
    i18n = json.load(open(os.path.join(STATIC, "i18n.json"), encoding="utf-8"))
    man_path = os.path.join(STATIC, "voice", "manifest.json")
    manifest = json.load(open(man_path)) if os.path.exists(man_path) else {}

    for lang in a.langs:
        voice = getattr(a, lang)
        out_dir = os.path.join(STATIC, "voice", lang)
        os.makedirs(out_dir, exist_ok=True)
        keys = []
        for emo, lines in i18n[lang]["lines"].items():
            for i, text in enumerate(lines):
                key = f"{emo}_{i}"
                path = os.path.join(out_dir, key + ".mp3")
                await edge_tts.Communicate(clean(text), voice, rate=a.rate).save(path)
                if FFMPEG:   # M5Stack CoreS3 用：16kHz 单声道 16bit WAV
                    subprocess.run([FFMPEG, '-y', '-loglevel', 'error', '-i', path, '-ar', '16000', '-ac', '1',
                                    '-sample_fmt', 's16', '-map_metadata', '-1', '-fflags', '+bitexact', path[:-4] + '.wav'], check=True)
                keys.append(key)
                print(f"[{lang}] {key}.mp3  ←  {clean(text)}")
        manifest[lang] = keys

    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"\n完成：{man_path}  —— 刷新页面后伙伴就会用这些音频说话。")


if __name__ == "__main__":
    asyncio.run(main())
