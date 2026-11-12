"""
FaceEcho × FaceLNN —— 训练并导出「GRU-D + 安全感知非对称损失」到浏览器端
========================================================================
在你的 5090（~/FaceLNN，conda env `face`）上跑。产物是一个 JSON 权重文件，
复制到 FaceEcho 的 static/models/grud_safety.json 后，网页就会启用「连续情绪 + 安全距离」。

算法与 LNN V1 定稿一致（multiseed_experiment.py 的设定）：
  - 骨干：GRU-D（64 隐单元），输入 MediaPipe 52 维 blendshape（与 FaceEcho 浏览器端同一模型、同一顺序）
  - 损失：safety_loss = 距离空间非对称项（欠距离/太近 × W_DANGER） + 0.3 × VA-MSE
  - 距离映射：d = clamp(1.6 − 0.95·v + 0.85·max(a,0), 0.3, 2.5)
  - 训练时按 --train_drop 随机丢帧（mask / delta 走 GRU-D 原生路径）
  - 工作点：Aff-Wild2 用 W_DANGER=4；AFEW-VA 用 16

本脚本自包含（不 import 你 FaceLNN 里的文件），只依赖预处理好的 npz：
  {data_root}/{train,val}/*.npz，每个含 feat_blend (T,52), va (T,2), 可选 valid (T,)

用法：
  conda activate face
  cd ~/FaceLNN
  # 1) 训练 + 导出（推荐：Aff-Wild2 主库，W=4）
  python train_export_grud.py --data_root data/affwild2 --w_danger 4 --out grud_safety.json
  # 2) 两个库合在一起训（可选，泛化更好）
  python train_export_grud.py --data_root data/affwild2 data/afewva --w_danger 4 --out grud_safety.json
  # 3) 已有训练好的 GRUD state_dict（与 FaceLNN/models.py 的 GRUD 键名一致）→ 直接导出，不重训
  python train_export_grud.py --ckpt my_grud.pt --out grud_safety.json
然后把 grud_safety.json 拷到 FaceEcho/static/models/ 即可（Thor 上同理）。
"""
import argparse, glob, json, os, time
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

LABEL_DIM = 2
D_MIN, D_MAX = 0.3, 2.5
DIST_C0, DIST_CV, DIST_CA = 1.6, -0.95, 0.85     # d = c0 + cv·v + ca·max(a,0)
MARGIN = 0.15

# MediaPipe FaceLandmarker blendshape 顺序（Python 与 JS 相同；index 0 = _neutral）
BLENDSHAPE_NAMES = [
    "_neutral", "browDownLeft", "browDownRight", "browInnerUp", "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight", "eyeBlinkLeft", "eyeBlinkRight",
    "eyeLookDownLeft", "eyeLookDownRight", "eyeLookInLeft", "eyeLookInRight", "eyeLookOutLeft",
    "eyeLookOutRight", "eyeLookUpLeft", "eyeLookUpRight", "eyeSquintLeft", "eyeSquintRight",
    "eyeWideLeft", "eyeWideRight", "jawForward", "jawLeft", "jawOpen", "jawRight", "mouthClose",
    "mouthDimpleLeft", "mouthDimpleRight", "mouthFrownLeft", "mouthFrownRight", "mouthFunnel",
    "mouthLeft", "mouthLowerDownLeft", "mouthLowerDownRight", "mouthPressLeft", "mouthPressRight",
    "mouthPucker", "mouthRight", "mouthRollLower", "mouthRollUpper", "mouthShrugLower",
    "mouthShrugUpper", "mouthSmileLeft", "mouthSmileRight", "mouthStretchLeft", "mouthStretchRight",
    "mouthUpperUpLeft", "mouthUpperUpRight", "noseSneerLeft", "noseSneerRight",
]
assert len(BLENDSHAPE_NAMES) == 52


# ----------------------------- 模型（与 FaceLNN/models.py 的 GRUD 键名一致） -----------------------------
class GRUD(nn.Module):
    def __init__(self, in_dim, units=64):
        super().__init__()
        self.D, self.H = in_dim, units
        self.gru = nn.GRUCell(in_dim, units)
        self.head = nn.Linear(units, LABEL_DIM)
        self.W_gx = nn.Linear(in_dim, in_dim)
        self.W_gh = nn.Linear(in_dim, units)
        self.register_buffer("x_mean", torch.zeros(in_dim))

    def set_mean(self, mean):
        self.x_mean.copy_(torch.as_tensor(mean, dtype=self.x_mean.dtype))

    def forward(self, feat, dt, mask, delta=None):
        B, T, D = feat.shape
        if delta is None:
            delta = dt
        if delta.dim() == 2:
            delta = delta.unsqueeze(-1).expand(B, T, D)
        h = feat.new_zeros(B, self.H)
        x_last = self.x_mean.expand(B, D).clone()
        preds = []
        for t in range(T):
            m = mask[:, t].unsqueeze(-1)
            gx = torch.exp(-torch.relu(self.W_gx(delta[:, t])))
            gh = torch.exp(-torch.relu(self.W_gh(delta[:, t])))
            x_hat = m * feat[:, t] + (1 - m) * (gx * x_last + (1 - gx) * self.x_mean)
            x_last = torch.where(m.bool(), feat[:, t], x_last)
            h = gh * h
            h = self.gru(x_hat, h)
            preds.append(self.head(h))
        return torch.stack(preds, 1)


# ----------------------------- 数据 -----------------------------
class NPZClips(Dataset):
    def __init__(self, roots, split, seq_len=128, min_valid=0.5):
        self.L = seq_len
        self.items = []
        files = []
        for r in roots:
            files += sorted(glob.glob(os.path.join(r, split, "*.npz")))
        if not files:
            raise FileNotFoundError(f"{roots} 下没有 {split}/*.npz —— 先用 preprocess_mediapipe.py 预处理")
        kept = 0
        for f in files:
            z = np.load(f)
            feat = z["feat_blend"].astype(np.float32)
            va = z["va"].astype(np.float32)
            valid = z["valid"].astype(np.float32) if "valid" in z.files else np.ones(len(va), np.float32)
            if feat.shape[1] != 52 or valid.mean() < min_valid or len(feat) < 8:
                continue
            self.items.append((feat, va, valid)); kept += 1
        print(f"[{split}] 保留 {kept}/{len(files)} 片段")

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        feat, va, valid = self.items[i]
        T = len(feat)
        if T >= self.L:
            s = np.random.randint(0, T - self.L + 1)
            sl = slice(s, s + self.L)
            return feat[sl], va[sl], valid[sl]
        reps = int(np.ceil(self.L / T))
        tile = lambda x: np.concatenate([x] * reps, 0)[: self.L]
        return tile(feat), tile(va), tile(valid)


def drop_frames(feat, dt, rate, rng):
    """与 FaceLNN/data.py 相同的丢帧协议：mask、并入后的 dt、距上次有效观测的 delta（秒）。"""
    T = feat.shape[0]
    keep = rng.rand(T) >= rate
    keep[0] = True
    feat = feat.copy(); feat[~keep] = 0.0
    mask = keep.astype(np.float32)
    out = np.zeros(T, np.float32); carry = 0.0
    for t in range(T):
        carry += dt[t]
        if keep[t]:
            out[t] = carry; carry = 0.0
        else:
            out[t] = dt[t]
    delta = np.zeros(T, np.float32)
    for t in range(1, T):
        delta[t] = dt[t] + (0.0 if mask[t - 1] else delta[t - 1])
    return feat, mask, out, delta


def make_collate(rate, fps):
    def collate(batch):
        rng = np.random.RandomState(np.random.randint(1 << 30))
        F, V, M, DT, DL, VAL = [], [], [], [], [], []
        for feat, va, valid in batch:
            d = np.full(len(feat), 1.0 / fps, np.float32)
            f2, m, dt2, dl = drop_frames(feat, d, rate, rng)
            F.append(f2); V.append(va); M.append(m); DT.append(dt2); DL.append(dl); VAL.append(valid)
        to = lambda x: torch.tensor(np.array(x))
        return to(F), to(V), to(M), to(DT), to(DL), to(VAL)
    return collate


# ----------------------------- 损失 / 指标 -----------------------------
def dist_raw(va):
    return DIST_C0 + DIST_CV * va[..., 0] + DIST_CA * torch.clamp(va[..., 1], min=0)


def masked_mse(pred, va, valid):
    w = valid.unsqueeze(-1)
    return (((pred - va) ** 2) * w).sum() / (w.sum() * 2 + 1e-8)


def safety_loss(pred, true, valid, w_danger):
    err = dist_raw(true) - dist_raw(pred)                 # >0：估近了（危险方向）
    d_term = ((w_danger * torch.relu(err) ** 2 + torch.relu(-err) ** 2) * valid).sum() / (valid.sum() + 1e-8)
    return d_term + 0.3 * masked_mse(pred, true, valid)


def ccc(p, t):
    out = []
    for k in range(p.shape[-1]):
        a, b = p[:, k], t[:, k]
        cov = ((a - a.mean()) * (b - b.mean())).mean()
        out.append(2 * cov / (a.var() + b.var() + (a.mean() - b.mean()) ** 2 + 1e-8))
    return np.array(out)


@torch.no_grad()
def evaluate(model, ds, fps, drop, dev, batch=16):
    model.eval()
    P, G, SEL = [], [], []
    for F, V, M, DT, DL, VAL in DataLoader(ds, batch, shuffle=False, collate_fn=make_collate(drop, fps)):
        p = model(F.to(dev), DT.to(dev), M.to(dev), DL.to(dev)).cpu().numpy()
        valid = VAL.numpy() > 0.5
        sel = valid & (M.numpy() < 0.5) if drop > 0 else valid
        P.append(p[sel]); G.append(V.numpy()[sel]); SEL.append(sel.sum())
    P, G = np.concatenate(P), np.concatenate(G)
    d_hat = np.clip(DIST_C0 + DIST_CV * P[:, 0] + DIST_CA * np.clip(P[:, 1], 0, None), D_MIN, D_MAX)
    d_true = np.clip(DIST_C0 + DIST_CV * G[:, 0] + DIST_CA * np.clip(G[:, 1], 0, None), D_MIN, D_MAX)
    return dict(ccc_v=float(ccc(P, G)[0]), ccc_a=float(ccc(P, G)[1]),
                danger_viol=float((d_hat < d_true - MARGIN).mean()),
                far_viol=float((d_hat > d_true + MARGIN).mean()),
                dist_mae=float(np.abs(d_hat - d_true).mean()), n=int(sum(SEL)))


# ----------------------------- 导出 -----------------------------
def export_json(model, path, meta):
    sd = {k: v.detach().cpu().float().numpy() for k, v in model.state_dict().items()}
    r = lambda a, n=7: np.round(a.astype(np.float64), n).tolist()
    obj = {
        "format": "faceecho-grud-v1",
        "in_dim": int(sd["x_mean"].shape[0]),
        "units": int(sd["gru.weight_hh"].shape[1]),
        "blendshape_names": BLENDSHAPE_NAMES,
        "time_unit": "s",
        "x_mean": r(sd["x_mean"]),
        "W_gx": {"w": r(sd["W_gx.weight"]), "b": r(sd["W_gx.bias"])},
        "W_gh": {"w": r(sd["W_gh.weight"]), "b": r(sd["W_gh.bias"])},
        "gru": {"w_ih": r(sd["gru.weight_ih"]), "w_hh": r(sd["gru.weight_hh"]),
                "b_ih": r(sd["gru.bias_ih"]), "b_hh": r(sd["gru.bias_hh"])},
        "head": {"w": r(sd["head.weight"]), "b": r(sd["head.bias"])},
        "dist": {"c0": DIST_C0, "cv": DIST_CV, "ca": DIST_CA, "d_min": D_MIN, "d_max": D_MAX},
        "meta": meta,
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    print(f"[导出] {path}  ({os.path.getsize(path)/1024:.0f} KB)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_root", nargs="*", default=[], help="一个或多个预处理目录（含 train/val）")
    ap.add_argument("--ckpt", default=None, help="已有 GRUD state_dict(.pt)，给了就跳过训练直接导出")
    ap.add_argument("--out", default="grud_safety.json")
    ap.add_argument("--w_danger", type=float, default=4.0)
    ap.add_argument("--units", type=int, default=64)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--lr", type=float, default=3e-3)
    ap.add_argument("--seq_len", type=int, default=128)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--train_drop", type=float, default=0.35)
    ap.add_argument("--min_valid", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    torch.manual_seed(a.seed); np.random.seed(a.seed)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = GRUD(52, a.units).to(dev)
    meta = {"algorithm": "GRU-D + safety-aware asymmetric loss (FaceLNN)", "w_danger": a.w_danger,
            "datasets": a.data_root, "train_drop": a.train_drop, "epochs": a.epochs,
            "exported": time.strftime("%Y-%m-%d %H:%M:%S")}

    if a.ckpt:
        sd = torch.load(a.ckpt, map_location="cpu")
        if isinstance(sd, nn.Module):
            sd = sd.state_dict()
        model = GRUD(sd["x_mean"].shape[0], sd["gru.weight_hh"].shape[1])
        model.load_state_dict(sd)
        meta["source_ckpt"] = os.path.basename(a.ckpt)
        export_json(model, a.out, meta)
        return

    if not a.data_root:
        ap.error("需要 --data_root（训练）或 --ckpt（直接导出）")
    tr = NPZClips(a.data_root, "train", a.seq_len, a.min_valid)
    va = NPZClips(a.data_root, "val", a.seq_len, a.min_valid)
    model.set_mean(np.concatenate([f for f, _, _ in tr.items]).mean(0))

    loader = DataLoader(tr, a.batch, shuffle=True, collate_fn=make_collate(a.train_drop, a.fps), drop_last=True)
    opt = torch.optim.Adam(model.parameters(), a.lr)
    t0 = time.time()
    for ep in range(a.epochs):
        model.train(); tot = 0.0
        for F, V, M, DT, DL, VAL in loader:
            F, V, M, DT, DL, VAL = [x.to(dev) for x in (F, V, M, DT, DL, VAL)]
            opt.zero_grad()
            loss = safety_loss(model(F, DT, M, DL), V, VAL, a.w_danger)
            loss.backward(); opt.step(); tot += loss.item()
        if ep == 0 or (ep + 1) % 10 == 0:
            print(f"  epoch {ep+1:3d}/{a.epochs}  loss {tot/len(loader):.4f}  ({time.time()-t0:.0f}s)")

    for drop in (0.0, 0.35):
        r = evaluate(model, va, a.fps, drop, dev, a.batch)
        meta[f"val_drop{int(drop*100)}"] = r
        print(f"[val drop={drop:.2f}] CCC v/a={r['ccc_v']:.3f}/{r['ccc_a']:.3f}  "
              f"危险违规={r['danger_viol']:.3f}  过远违规={r['far_viol']:.3f}  距离MAE={r['dist_mae']:.3f}")

    model = model.cpu()
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    torch.save(model.state_dict(), os.path.splitext(a.out)[0] + ".pt")
    export_json(model, a.out, meta)


if __name__ == "__main__":
    main()
