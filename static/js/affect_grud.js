// FaceEcho · 连续情绪估计器（GRU-D + 安全感知非对称损失，来自 FaceLNN）
// ---------------------------------------------------------------------------
// 浏览器端逐帧推理，与 PyTorch 的 FaceLNN GRUD.forward 数值一致（见 tools/parity_test.mjs）。
// 输入：MediaPipe 52 维 blendshape（检测到脸时）或 null（没检测到 = 丢帧）
// 输出：连续 valence / arousal，以及安全社交距离 d = clamp(c0 + cv·v + ca·max(a,0), d_min, d_max)
// 丢帧时不冻结：按 GRU-D 的机制，输入向“最后观测值→训练均值”衰减、隐状态按距上次观测的时间衰减。

const sigmoid = x => 1 / (1 + Math.exp(-x));

function flat(rows) {            // 2D 数组 -> Float32Array（行主序）
  const R = rows.length, C = rows[0].length, out = new Float32Array(R * C);
  for (let i = 0; i < R; i++) out.set(rows[i], i * C);
  return out;
}
function matvec(W, b, x, rows, cols, out) {   // out = W·x + b
  for (let i = 0; i < rows; i++) {
    let s = b ? b[i] : 0; const o = i * cols;
    for (let j = 0; j < cols; j++) s += W[o + j] * x[j];
    out[i] = s;
  }
  return out;
}

export class SafeAffectEstimator {
  constructor(json) {
    if (json.format !== 'faceecho-grud-v1') throw new Error('权重格式不对：' + json.format);
    const D = this.D = json.in_dim, H = this.H = json.units;
    this.names = json.blendshape_names || null;
    this.meta = json.meta || {};
    this.dist = json.dist;
    this.xMean = Float32Array.from(json.x_mean);
    this.Wgx = flat(json.W_gx.w); this.bgx = Float32Array.from(json.W_gx.b);
    this.Wgh = flat(json.W_gh.w); this.bgh = Float32Array.from(json.W_gh.b);
    this.Wih = flat(json.gru.w_ih); this.Whh = flat(json.gru.w_hh);
    this.bih = Float32Array.from(json.gru.b_ih); this.bhh = Float32Array.from(json.gru.b_hh);
    this.Wo = flat(json.head.w); this.bo = Float32Array.from(json.head.b);
    // 工作缓冲
    this.h = new Float32Array(H); this.xLast = new Float32Array(D);
    this.x = new Float32Array(D); this.xh = new Float32Array(D); this.dvec = new Float32Array(D);
    this.gx = new Float32Array(D); this.gh = new Float32Array(H);
    this.gi = new Float32Array(3 * H); this.gH = new Float32Array(3 * H); this.y = new Float32Array(2);
    this.reset();
  }

  static async load(url) {
    const r = await fetch(url, { cache: 'no-cache' });
    if (!r.ok) throw new Error(`${url} (${r.status})`);
    return new SafeAffectEstimator(await r.json());
  }

  reset() {
    this.h.fill(0); this.xLast.set(this.xMean);
    this.tLastObs = null; this.tPrev = null; this.out = null;
  }

  // 检查 MediaPipe 输出的 categoryName 顺序与训练时一致
  checkOrder(categories) {
    if (!this.names || !categories) return true;
    return categories.length === this.names.length &&
      categories.every((c, i) => c.categoryName === this.names[i]);
  }

  /**
   * 前进一步。
   * @param {Float32Array|number[]|null} scores 52 维 blendshape；null 表示这一帧没观测到脸
   * @param {number} tSec 当前时间（秒，单调递增，如 performance.now()/1000）
   */
  step(scores, tSec) {
    const D = this.D, H = this.H;
    const observed = !!scores;
    // delta = 距上一次“有效观测”的时间（秒）；首步为 0 —— 与训练时 drop_frames 的定义一致
    const delta = this.tLastObs === null ? 0 : Math.max(0, tSec - this.tLastObs);
    this.dvec.fill(delta);
    matvec(this.Wgx, this.bgx, this.dvec, D, D, this.gx);
    matvec(this.Wgh, this.bgh, this.dvec, H, D, this.gh);
    for (let j = 0; j < D; j++) {
      const gx = Math.exp(-Math.max(0, this.gx[j]));
      if (observed) { this.xh[j] = scores[j]; this.xLast[j] = scores[j]; }
      else this.xh[j] = gx * this.xLast[j] + (1 - gx) * this.xMean[j];
    }
    for (let i = 0; i < H; i++) this.h[i] *= Math.exp(-Math.max(0, this.gh[i]));
    // GRUCell（PyTorch 门顺序 r, z, n）
    matvec(this.Wih, this.bih, this.xh, 3 * H, D, this.gi);
    matvec(this.Whh, this.bhh, this.h, 3 * H, H, this.gH);
    for (let i = 0; i < H; i++) {
      const r = sigmoid(this.gi[i] + this.gH[i]);
      const z = sigmoid(this.gi[H + i] + this.gH[H + i]);
      const n = Math.tanh(this.gi[2 * H + i] + r * this.gH[2 * H + i]);
      this.h[i] = (1 - z) * n + z * this.h[i];
    }
    matvec(this.Wo, this.bo, this.h, 2, H, this.y);
    if (observed) this.tLastObs = tSec;
    this.tPrev = tSec;

    const v = this.y[0], a = this.y[1], P = this.dist;
    const distRaw = P.c0 + P.cv * v + P.ca * Math.max(a, 0);
    this.out = {
      v, a, distRaw,
      dist: Math.min(P.d_max, Math.max(P.d_min, distRaw)),
      observed, delta,
      gap: observed ? 0 : delta,          // 已连续多久没看到脸（秒）
    };
    return this.out;
  }
}

// VA -> 伙伴反应类别（给“情绪反馈伙伴”用；阈值可调）
export function vaToMood(v, a) {
  if (v > 0.25) return a > 0.45 ? 'surprise' : 'happy';
  if (v < -0.2) return a > 0.2 ? 'angry' : 'sad';
  if (a > 0.45) return 'surprise';
  return 'neutral';
}
