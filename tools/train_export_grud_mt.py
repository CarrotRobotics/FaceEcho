"""
多任务 GRU-D：连续情绪(VA, 安全感知损失) + 表情分类(Aff-Wild2 EXPR 7 类)，训练并导出给 FaceEcho
==========================================================================================
在 5090 上跑（conda env `face`）。输入是 tools/preprocess_affwild2_mt.py 生成的 npz：
    {data_root}/{train,val}/*.npz  含 feat_blend(T,52) va(T,2) va_valid(T) expr(T) observed(T)
也兼容旧的 VA-only npz（feat_blend, va, valid）——这类片段只参与 VA 损失。

模型：同一个 GRU-D 主干（64 隐单元，与论文一致）+ 两个头
    VA 头   ：safety_loss = 非对称距离项(×W_DANGER) + 0.3·VA-MSE   —— 与论文定稿完全相同
    EXPR 头 ：7 类（Neutral, Anger, Disgust, Fear, Happiness, Sadness, Surprise；Other 不用），
              类别加权交叉熵（Aff-Wild2 的 Neutral 远多于其他类）
总损失 = safety_loss + λ_expr · CE

用法：
  conda activate face && cd ~/FaceLNN
  python train_export_grud_mt.py --data_root data/affwild2_mt --w_danger 4 --out grud_safety.json
  # 产物：grud_safety.json（带表情头，FaceEcho 直接用）+ grud_safety_mt.pt
验证集打印：VA CCC、危险违规（和论文表对照）、表情 macro-F1（ABAW 官方指标）与各类 F1。
"""
import argparse, glob, json, os, sys, time
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from train_export_grud import (GRUD, BLENDSHAPE_NAMES, D_MIN, D_MAX, DIST_C0, DIST_CV, DIST_CA, MARGIN,
                               dist_raw, ccc)

EXPR_CLASSES = ["neutral", "anger", "disgust", "fear", "happiness", "sadness", "surprise"]
N_EXPR = len(EXPR_CLASSES)


class GRUD_MT(GRUD):
    """GRU-D 主干 + VA 头（继承自 GRUD.head）+ 表情头。state_dict 与单任务 GRUD 兼容（多了 expr_head.*）。"""
    def __init__(self, in_dim, units=64):
        super().__init__(in_dim, units)
        self.expr_head = nn.Linear(units, N_EXPR)

    def forward(self, feat, dt, mask, delta=None):
        B, T, D = feat.shape
        if delta is None:
            delta = dt
        if delta.dim() == 2:
            delta = delta.unsqueeze(-1).expand(B, T, D)
        h = feat.new_zeros(B, self.H)
        x_last = self.x_mean.expand(B, D).clone()
        va, ex = [], []
        for t in range(T):
            m = mask[:, t].unsqueeze(-1)
            gx = torch.exp(-torch.relu(self.W_gx(delta[:, t])))
            gh = torch.exp(-torch.relu(self.W_gh(delta[:, t])))
            x_hat = m * feat[:, t] + (1 - m) * (gx * x_last + (1 - gx) * self.x_mean)
            x_last = torch.where(m.bool(), feat[:, t], x_last)
            h = self.gru(x_hat, gh * h)
            va.append(self.head(h)); ex.append(self.expr_head(h))
        return torch.stack(va, 1), torch.stack(ex, 1)


# ----------------------------- 数据 -----------------------------
def load_clip(f):
    z = np.load(f)
    feat = z["feat_blend"].astype(np.float32)
    T = len(feat)
    va = z["va"].astype(np.float32) if "va" in z.files else np.zeros((T, 2), np.float32)
    if "va_valid" in z.files:
        va_valid = z["va_valid"].astype(np.float32)
    elif "valid" in z.files:
        va_valid = z["valid"].astype(np.float32)
    else:
        va_valid = np.zeros(T, np.float32)
    expr = z["expr"].astype(np.int64) if "expr" in z.files else np.full(T, -1, np.int64)
    expr[(expr < 0) | (expr >= N_EXPR)] = -1          # Other(7) 和无效(-1) 都不参与
    observed = z["observed"].astype(np.float32) if "observed" in z.files else np.ones(T, np.float32)
    return feat, va, va_valid, expr, observed


class Clips(Dataset):
    def __init__(self, roots, split, seq_len=128, crops=4, min_label=0.3):
        self.L, self.crops, self.items = seq_len, crops, []
        files = sorted(sum([glob.glob(os.path.join(r, split, "*.npz")) for r in roots], []))
        if not files:
            raise FileNotFoundError(f"{roots} 下没有 {split}/*.npz —— 先跑 tools/preprocess_affwild2_mt.py")
        for f in files:
            c = load_clip(f)
            lab = np.maximum(c[2], (c[3] >= 0).astype(np.float32))
            if c[0].shape[1] != 52 or len(c[0]) < 16 or lab.mean() < min_label or c[4].mean() < 0.3:
                continue
            self.items.append(c)
        n_expr = sum(int((c[3] >= 0).sum()) for c in self.items)
        n_va = sum(int(c[2].sum()) for c in self.items)
        print(f"[{split}] 片段 {len(self.items)}/{len(files)}  VA 帧 {n_va}  表情帧 {n_expr}")

    def __len__(self):
        return len(self.items) * self.crops

    def __getitem__(self, i):
        feat, va, vv, ex, ob = self.items[i % len(self.items)]
        T = len(feat)
        if T >= self.L:
            s = np.random.randint(0, T - self.L + 1); sl = slice(s, s + self.L)
            return feat[sl], va[sl], vv[sl], ex[sl], ob[sl]
        reps = int(np.ceil(self.L / T)); tile = lambda x: np.concatenate([x] * reps, 0)[: self.L]
        return tile(feat), tile(va), tile(vv), tile(ex), tile(ob)

    def expr_counts(self):
        cnt = np.zeros(N_EXPR)
        for c in self.items:
            e = c[3][c[3] >= 0]; cnt += np.bincount(e, minlength=N_EXPR)
        return cnt


class EvalWindows(Dataset):
    """验证用：每个视频按顺序切成不重叠的窗口（不随机），整段都评测，结果可复现。"""
    def __init__(self, clips, win=512):
        self.w = []
        for feat, va, vv, ex, ob in clips.items:
            for s in range(0, len(feat), win):
                if len(feat) - s >= 16:
                    sl = slice(s, s + win); self.w.append((feat[sl], va[sl], vv[sl], ex[sl], ob[sl]))
    def __len__(self): return len(self.w)
    def __getitem__(self, i): return self.w[i]


def pad_batch(batch):
    """不同长度的窗口补齐到同一长度（补的部分 observed=0、无标签，不参与评测）。"""
    L = max(len(b[0]) for b in batch); out = []
    for feat, va, vv, ex, ob in batch:
        n = L - len(feat)
        if n:
            feat = np.concatenate([feat, np.zeros((n, feat.shape[1]), feat.dtype)]); va = np.concatenate([va, np.zeros((n, 2), va.dtype)])
            vv = np.concatenate([vv, np.zeros(n, vv.dtype)]); ex = np.concatenate([ex, np.full(n, -1, ex.dtype)])
            ob = np.concatenate([ob, np.zeros(n, ob.dtype)])
        out.append((feat, va, vv, ex, ob))
    return out


def make_collate(rate, fps, seed=None):
    """丢帧协议（与论文相同）叠加数据里本来就没检测到脸的帧：mask = 观测到 且 没被丢。"""
    cnt = [0]
    def collate(batch):
        if seed is None:
            rng = np.random.RandomState(np.random.randint(1 << 30))
        else:                                   # 评测：固定随机数，每次评测丢的是同一批帧
            rng = np.random.RandomState(seed + cnt[0]); cnt[0] += 1
            batch = pad_batch(batch)
        F, V, VV, E, M, DT, DL = [], [], [], [], [], [], []
        for feat, va, vv, ex, ob in batch:
            T = len(feat); d = np.full(T, 1.0 / fps, np.float32)
            keep = (rng.rand(T) >= rate) & (ob > 0.5)
            keep[0] = keep[0] or ob[0] > 0.5
            m = keep.astype(np.float32); f = feat.copy(); f[~keep] = 0.0
            dt2 = np.zeros(T, np.float32); carry = 0.0
            for t in range(T):
                carry += d[t]
                if keep[t]: dt2[t] = carry; carry = 0.0
                else: dt2[t] = d[t]
            dl = np.zeros(T, np.float32)
            for t in range(1, T):
                dl[t] = d[t] + (0.0 if m[t - 1] else dl[t - 1])
            F.append(f); V.append(va); VV.append(vv); E.append(ex); M.append(m); DT.append(dt2); DL.append(dl)
        to = lambda x, dt=None: torch.tensor(np.array(x)) if dt is None else torch.tensor(np.array(x), dtype=dt)
        return to(F), to(V), to(VV), to(E, torch.long), to(M), to(DT), to(DL)
    return collate


# ----------------------------- 损失 / 指标 -----------------------------
def safety_loss(pred, true, valid, w_danger):
    err = dist_raw(true) - dist_raw(pred)
    d_term = ((w_danger * torch.relu(err) ** 2 + torch.relu(-err) ** 2) * valid).sum() / (valid.sum() + 1e-8)
    w = valid.unsqueeze(-1)
    mse = (((pred - true) ** 2) * w).sum() / (w.sum() * 2 + 1e-8)
    return d_term + 0.3 * mse


def f1_scores(pred, true, n):
    f1 = []
    for k in range(n):
        tp = np.sum((pred == k) & (true == k)); fp = np.sum((pred == k) & (true != k)); fn = np.sum((pred != k) & (true == k))
        f1.append(2 * tp / (2 * tp + fp + fn) if tp + fp + fn > 0 else 0.0)
    return np.array(f1)


# 网页上的 5 类：开心 / 惊讶 / 伤心 / 生气 / 平静（生气含厌恶，惊讶含恐惧）
MERGE5 = {0: 4, 1: 3, 2: 3, 3: 1, 4: 0, 5: 2, 6: 1}
NAMES5 = ["happy", "surprise", "sad", "angry", "neutral"]


@torch.no_grad()
def evaluate(model, ds, fps, drop, dev, batch=64, bias=None, want_logits=False):
    model.eval()
    P, G, EP, EG, LG = [], [], [], [], []
    for F, V, VV, E, M, DT, DL in DataLoader(ds, batch, shuffle=False, collate_fn=make_collate(drop, fps, seed=1234)):
        va, lg = model(F.to(dev), DT.to(dev), M.to(dev), DL.to(dev))
        va, lg = va.cpu().numpy(), lg.cpu().numpy()
        if bias is not None: lg = lg + bias
        ex = lg.argmax(-1)
        if want_logits: LG.append(lg[E.numpy() >= 0])
        sel = VV.numpy() > 0.5
        if drop > 0: sel &= M.numpy() < 0.5
        P.append(va[sel]); G.append(V.numpy()[sel])
        es = E.numpy() >= 0
        EP.append(ex[es]); EG.append(E.numpy()[es])
    out = {}
    if sum(len(p) for p in P):
        P, G = np.concatenate(P), np.concatenate(G)
        d_hat = np.clip(DIST_C0 + DIST_CV * P[:, 0] + DIST_CA * np.clip(P[:, 1], 0, None), D_MIN, D_MAX)
        d_true = np.clip(DIST_C0 + DIST_CV * G[:, 0] + DIST_CA * np.clip(G[:, 1], 0, None), D_MIN, D_MAX)
        c = ccc(P, G)
        out.update(ccc_v=float(c[0]), ccc_a=float(c[1]), danger_viol=float((d_hat < d_true - MARGIN).mean()),
                   far_viol=float((d_hat > d_true + MARGIN).mean()), dist_mae=float(np.abs(d_hat - d_true).mean()))
    if sum(len(e) for e in EG):
        EP, EG = np.concatenate(EP), np.concatenate(EG)
        f7 = f1_scores(EP, EG, N_EXPR)
        m = np.vectorize(MERGE5.get)
        f5 = f1_scores(m(EP), m(EG), 5)
        out.update(expr_f1=float(f7.mean()), expr_acc=float((EP == EG).mean()),
                   expr_f1_per_class={k: round(float(v), 3) for k, v in zip(EXPR_CLASSES, f7)},
                   expr5_f1=float(f5.mean()), expr5_f1_per_class={k: round(float(v), 3) for k, v in zip(NAMES5, f5)})
    if want_logits:
        return out, (np.concatenate(LG) if LG else None), (EG if len(EG) else None)
    return out


def calibrate_bias(logits, labels, rounds=3, grid=np.linspace(-3, 3, 31)):
    """给表情头的 7 个输出各加一个偏置，让网页 5 类的 macro-F1 最大（逐类坐标搜索）。
    只改 expr_head.bias，不改主干和 VA 头。注意：在验证集上调，验证集上的分数会偏乐观。"""
    m = np.vectorize(MERGE5.get); g5 = m(labels)
    score = lambda b: f1_scores(m((logits + b).argmax(-1)), g5, 5).mean()
    b = np.zeros(N_EXPR, np.float32); best = score(b)
    for _ in range(rounds):
        for k in range(1, N_EXPR):                 # neutral 固定为 0 作参照
            for v in grid:
                c = b.copy(); c[k] = v; s = score(c)
                if s > best + 1e-6: best, b = s, c
    return b, best


def export_json(model, path, meta):
    sd = {k: v.detach().cpu().float().numpy() for k, v in model.state_dict().items()}
    r = lambda a, n=7: np.round(a.astype(np.float64), n).tolist()
    obj = {
        "format": "faceecho-grud-v1", "in_dim": int(sd["x_mean"].shape[0]), "units": int(sd["gru.weight_hh"].shape[1]),
        "blendshape_names": BLENDSHAPE_NAMES, "time_unit": "s", "x_mean": r(sd["x_mean"]),
        "W_gx": {"w": r(sd["W_gx.weight"]), "b": r(sd["W_gx.bias"])},
        "W_gh": {"w": r(sd["W_gh.weight"]), "b": r(sd["W_gh.bias"])},
        "gru": {"w_ih": r(sd["gru.weight_ih"]), "w_hh": r(sd["gru.weight_hh"]),
                "b_ih": r(sd["gru.bias_ih"]), "b_hh": r(sd["gru.bias_hh"])},
        "head": {"w": r(sd["head.weight"]), "b": r(sd["head.bias"])},
        "expr_head": {"w": r(sd["expr_head.weight"]), "b": r(sd["expr_head.bias"]), "classes": EXPR_CLASSES},
        "dist": {"c0": DIST_C0, "cv": DIST_CV, "ca": DIST_CA, "d_min": D_MIN, "d_max": D_MAX},
        "meta": meta,
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    print(f"[导出] {path}  ({os.path.getsize(path)/1024:.0f} KB)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_root", nargs="+", required=True)
    ap.add_argument("--out", default="grud_safety.json")
    ap.add_argument("--w_danger", type=float, default=4.0)
    ap.add_argument("--lambda_expr", type=float, default=1.0, help="表情损失的权重")
    ap.add_argument("--units", type=int, default=64)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--lr", type=float, default=3e-3)
    ap.add_argument("--seq_len", type=int, default=256)
    ap.add_argument("--crops", type=int, default=12, help="每个视频每轮随机截几段")
    ap.add_argument("--eval_every", type=int, default=2)
    ap.add_argument("--no_calib", action="store_true", help="不做表情偏置校准")
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--train_drop", type=float, default=0.35)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    torch.manual_seed(a.seed); np.random.seed(a.seed)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    tr = Clips(a.data_root, "train", a.seq_len, a.crops)
    va = EvalWindows(Clips(a.data_root, "val", a.seq_len, 1))
    print(f"[val] 评测窗口 {len(va)} 个（整段顺序切分，固定丢帧随机数）")
    model = GRUD_MT(52, a.units).to(dev)
    model.set_mean(np.concatenate([c[0][c[4] > 0.5] for c in tr.items]).mean(0))

    cnt = tr.expr_counts()
    w = 1.0 / np.sqrt(np.maximum(cnt, 1)); w = w / w.mean()
    print("表情各类帧数：", {k: int(v) for k, v in zip(EXPR_CLASSES, cnt)})
    ce = nn.CrossEntropyLoss(weight=torch.tensor(w, dtype=torch.float32, device=dev), ignore_index=-1)

    loader = DataLoader(tr, a.batch, shuffle=True, collate_fn=make_collate(a.train_drop, a.fps), drop_last=True,
                        num_workers=4, persistent_workers=True)
    opt = torch.optim.Adam(model.parameters(), a.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, a.epochs)
    best, best_state, best_ep, t0 = -1, None, 0, time.time()
    for ep in range(a.epochs):
        model.train(); tot = [0.0, 0.0]
        for F, V, VV, E, M, DT, DL in loader:
            F, V, VV, E, M, DT, DL = [x.to(dev) for x in (F, V, VV, E, M, DT, DL)]
            opt.zero_grad()
            pva, pex = model(F, DT, M, DL)
            l_va = safety_loss(pva, V, VV, a.w_danger) if VV.sum() > 0 else pva.sum() * 0
            l_ex = ce(pex.reshape(-1, N_EXPR), E.reshape(-1)) if (E >= 0).any() else pex.sum() * 0
            (l_va + a.lambda_expr * l_ex).backward()
            nn.utils.clip_grad_norm_(model.parameters(), 5.0); opt.step()
            tot[0] += l_va.item(); tot[1] += l_ex.item()
        sched.step()
        if ep == 0 or (ep + 1) % a.eval_every == 0 or ep == a.epochs - 1:
            r = evaluate(model, va, a.fps, 0.0, dev)
            score = r.get("expr5_f1", 0) + r.get("ccc_v", 0) + r.get("ccc_a", 0)
            mark = ""
            if score > best:
                best = score; best_ep = ep + 1; mark = "  ★"
                best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
            print(f"  epoch {ep+1:3d}/{a.epochs}  loss VA {tot[0]/len(loader):.4f}  EXPR {tot[1]/len(loader):.4f}  "
                  f"| val CCC v/a {r.get('ccc_v',0):.3f}/{r.get('ccc_a',0):.3f}  表情F1 7类 {r.get('expr_f1',0):.3f} "
                  f"5类 {r.get('expr5_f1',0):.3f}  ({time.time()-t0:.0f}s){mark}", flush=True)
    model.load_state_dict(best_state)
    print(f"\n[选用] 第 {best_ep} 轮（验证集 5类F1 + CCC_v + CCC_a 最高）")

    bias = None
    if not a.no_calib:
        r0, lg, lab = evaluate(model, va, a.fps, 0.0, dev, want_logits=True)
        if lg is not None:
            bias, f5 = calibrate_bias(lg, lab)
            print(f"[校准] 表情偏置 {dict(zip(EXPR_CLASSES, np.round(bias, 2).tolist()))}")
            print(f"[校准] 网页5类 macro-F1 {r0['expr5_f1']:.3f} → {f5:.3f}（在验证集上调的，偏乐观）")
            with torch.no_grad():
                model.expr_head.bias += torch.tensor(bias, device=model.expr_head.bias.device)

    meta = {"algorithm": "GRU-D + safety-aware asymmetric loss + expression head (multi-task)", "w_danger": a.w_danger,
            "lambda_expr": a.lambda_expr, "datasets": a.data_root, "train_drop": a.train_drop, "epochs": a.epochs,
            "best_epoch": best_ep, "expr_bias_calibrated": bias is not None,
            "exported": time.strftime("%Y-%m-%d %H:%M:%S")}
    for drop in (0.0, 0.35):
        r = evaluate(model, va, a.fps, drop, dev)
        meta[f"val_drop{int(drop*100)}"] = r
        print(f"\n[val drop={drop:.2f}]")
        if "ccc_v" in r:
            print(f"  VA：CCC v/a = {r['ccc_v']:.3f}/{r['ccc_a']:.3f}   危险违规 = {r['danger_viol']:.3f}   "
                  f"过远违规 = {r['far_viol']:.3f}   距离MAE = {r['dist_mae']:.3f}")
        if "expr_f1" in r:
            print(f"  表情(7类)：macro-F1 = {r['expr_f1']:.3f}   准确率 = {r['expr_acc']:.3f}")
            print(f"    各类 F1：{r['expr_f1_per_class']}")
            print(f"  网页 5 类：macro-F1 = {r['expr5_f1']:.3f}   {r['expr5_f1_per_class']}")

    model = model.cpu()
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    torch.save(model.state_dict(), os.path.splitext(a.out)[0] + "_mt.pt")
    export_json(model, a.out, meta)


if __name__ == "__main__":
    main()
