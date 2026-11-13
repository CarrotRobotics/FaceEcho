"""
Aff-Wild2 多任务预处理：cropped_aligned 人脸图 → MediaPipe 52 维 blendshape，并对齐 VA + EXPR 两种标注
=====================================================================================================
在 5090 上跑一次（需要 mediapipe、opencv-python；conda env `face`）。

输入（ABAW Annotations.zip 解压后的目录，脚本会自己在下面找这两个文件夹）：
    {anno_root}/**/VA_Estimation_Challenge/{Train_Set,Validation_Set}/<name>.txt   首行 valence,arousal；无效 = -5
    {anno_root}/**/EXPR_*_Challenge/{Train_Set,Validation_Set}/<name>.txt           首行类别名；每行 0~7，无效 = -1
    {frames_root}/<name>/00001.jpg …                                                 cropped_aligned（第 i 行标注 ↔ 第 i 帧）

输出：{out_root}/{train,val}/<name>.npz
    feat_blend (T,52)  每帧 blendshape（没检测到脸的帧为 0）
    observed   (T,)    1 = 这一帧检测到脸
    va (T,2) + va_valid (T,)   ；   expr (T,)  0~6，-1 = 无标注/Other
划分：任何一个任务里属于 Validation_Set 的视频 → val（避免信息泄漏），其余 → train。

用法：
  python preprocess_affwild2_mt.py --anno_root ~/data/ABAW_Annotations --frames_root ~/data/cropped_aligned \\
      --out_root data/affwild2_mt --model face_landmarker.task --workers 16
  先试 5 个视频：加 --limit 5      断点续跑：已存在的 npz 自动跳过
"""
import argparse, glob, os, sys, time
from multiprocessing import Pool
import numpy as np

SPLITS = {"Train_Set": "train", "Validation_Set": "val"}
_LM = None


def find_task_dir(root, pattern):
    hits = [d for d in glob.glob(os.path.join(root, "**", pattern), recursive=True) if os.path.isdir(d)]
    hits = [d for d in hits if os.path.isdir(os.path.join(d, "Train_Set"))]
    return hits[0] if hits else None


def read_va(path):
    rows = []
    with open(path) as f:
        next(f)
        for line in f:
            p = line.strip().split(",")
            if len(p) >= 2:
                rows.append((float(p[0]), float(p[1])))
    a = np.array(rows, np.float32).reshape(-1, 2)
    valid = ~np.any(a <= -5, axis=1) & np.all(np.abs(a) <= 1.0, axis=1)
    a[~valid] = 0
    return a, valid.astype(np.float32)


def read_expr(path):
    with open(path) as f:
        next(f)
        lab = [int(float(l.strip().split(",")[0])) for l in f if l.strip()]
    e = np.array(lab, np.int64)
    e[(e < 0) | (e > 6)] = -1                       # -1 无效，7 = Other 不用
    return e


def _init(model_path):
    global _LM
    import mediapipe as mp
    from mediapipe.tasks import python
    from mediapipe.tasks.python import vision
    opts = vision.FaceLandmarkerOptions(base_options=python.BaseOptions(model_asset_path=model_path),
                                        running_mode=vision.RunningMode.IMAGE, num_faces=1,
                                        output_face_blendshapes=True)
    _LM = vision.FaceLandmarker.create_from_options(opts)


def blendshapes(img_bgr):
    """返回 52 维 blendshape 或 None。112×112 的 cropped_aligned 脸几乎占满画面，检测不到时加黑边再试一次。"""
    import cv2, mediapipe as mp
    for pad in (0, 0.5):
        im = img_bgr if pad == 0 else cv2.copyMakeBorder(img_bgr, *(int(img_bgr.shape[0] * pad),) * 4,
                                                         cv2.BORDER_CONSTANT, value=0)
        rgb = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)
        res = _LM.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)))
        if res.face_blendshapes:
            return np.array([c.score for c in res.face_blendshapes[0]], np.float32)
    return None


def process(job):
    name, split, va_path, ex_path, frames_root, out_path = job
    import cv2
    va, va_valid = read_va(va_path) if va_path else (None, None)
    ex = read_expr(ex_path) if ex_path else None
    T = max(len(x) for x in (va, ex) if x is not None)
    if va is None: va, va_valid = np.zeros((T, 2), np.float32), np.zeros(T, np.float32)
    if ex is None: ex = np.full(T, -1, np.int64)
    pad = lambda x, v: np.concatenate([x, np.full((T - len(x),) + x.shape[1:], v, x.dtype)]) if len(x) < T else x[:T]
    va, va_valid, ex = pad(va, 0), pad(va_valid, 0), pad(ex, -1)
    feat = np.zeros((T, 52), np.float32); obs = np.zeros(T, np.float32)
    fdir = os.path.join(frames_root, name)
    for i in range(T):
        p = os.path.join(fdir, f"{i+1:05d}.jpg")
        if not os.path.exists(p):
            continue
        img = cv2.imread(p)
        if img is None:
            continue
        b = blendshapes(img)
        if b is not None and len(b) == 52:
            feat[i] = b; obs[i] = 1
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    np.savez_compressed(out_path + ".tmp.npz", feat_blend=feat, observed=obs, va=va, va_valid=va_valid,
                        expr=ex.astype(np.int8))
    os.replace(out_path + ".tmp.npz", out_path)
    return name, split, T, int(obs.sum()), int(va_valid.sum()), int((ex >= 0).sum())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anno_root", required=True)
    ap.add_argument("--frames_root", required=True)
    ap.add_argument("--out_root", default="data/affwild2_mt")
    ap.add_argument("--model", default="face_landmarker.task")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    va_dir = find_task_dir(a.anno_root, "VA_Estimation_Challenge")
    ex_dir = find_task_dir(a.anno_root, "EXPR_*")
    print("VA 标注：", va_dir, "\nEXPR 标注：", ex_dir)
    if not ex_dir:
        sys.exit("没找到 EXPR_*_Challenge（含 Train_Set）——检查 --anno_root")
    if not os.path.isfile(a.model):
        sys.exit(f"没找到 {a.model}（FaceEcho 的 static/models/face_landmarker.task 就是它）")

    vids = {}                                            # name -> {va:path, ex:path, val:bool}
    for task, d in (("va", va_dir), ("ex", ex_dir)):
        if not d: continue
        for sp in SPLITS:
            for p in glob.glob(os.path.join(d, sp, "*.txt")):
                n = os.path.splitext(os.path.basename(p))[0]
                v = vids.setdefault(n, {"va": None, "ex": None, "val": False})
                v[task] = p; v["val"] |= sp == "Validation_Set"
    jobs, miss = [], 0
    for n, v in sorted(vids.items()):
        if not os.path.isdir(os.path.join(a.frames_root, n)):
            miss += 1; continue
        split = "val" if v["val"] else "train"
        out = os.path.join(a.out_root, split, n + ".npz")
        if os.path.exists(out): continue
        jobs.append((n, split, v["va"], v["ex"], a.frames_root, out))
    if a.limit: jobs = jobs[: a.limit]
    print(f"视频共 {len(vids)}，找不到人脸图文件夹 {miss}，待处理 {len(jobs)}（{a.workers} 进程）")

    t0 = time.time(); done = 0
    with Pool(a.workers, initializer=_init, initargs=(a.model,)) as pool:
        for n, sp, T, ob, nva, nex in pool.imap_unordered(process, jobs):
            done += 1
            el = time.time() - t0
            print(f"[{done}/{len(jobs)}] {sp:5s} {n}  帧 {T}  检测到脸 {ob/T:.0%}  VA {nva}  表情 {nex}  "
                  f"（已用 {el/60:.0f} 分，预计还要 {el/done*(len(jobs)-done)/60:.0f} 分）", flush=True)
    print("完成：", a.out_root)


if __name__ == "__main__":
    main()
