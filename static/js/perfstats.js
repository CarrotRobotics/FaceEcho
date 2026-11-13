// 逐帧性能记录：延迟、帧间隔、丢帧（看不到脸）片段。用于论文的部署实验（E1/E2）。
// 只在内存里记录，最多 MAX 行（30 fps 时约 20 分钟），超出后丢最早的。
const MAX = 36000;
export const GAP_BINS = [0.2, 0.5, 1, 2];   // 丢帧片段长度分箱 (s)：<0.2, 0.2–0.5, 0.5–1, 1–2, ≥2

export class PerfLog {
  constructor() { this.reset(); }
  reset() { this.rows = []; this.t0 = null; this.lastT = null; this.cur = null; }
  // 每个新视频帧开始时调用
  begin(tMs) {
    if (this.t0 === null) this.t0 = tMs;
    this.cur = { t: (tMs - this.t0) / 1000, frame_dt: this.lastT === null ? NaN : tMs - this.lastT,
                 mp_ms: NaN, grud_ms: NaN, total_ms: NaN, observed: 0, gap: NaN,
                 v: NaN, a: NaN, dist: NaN, react_busy: 0, _start: tMs };
    this.lastT = tMs;
  }
  mp(ms) { if (this.cur) this.cur.mp_ms = ms; }
  grud(ms, o) {
    if (!this.cur) return;
    this.cur.grud_ms = ms;
    if (o) { this.cur.observed = o.observed ? 1 : 0; this.cur.gap = o.gap; this.cur.v = o.v; this.cur.a = o.a; this.cur.dist = o.dist; }
  }
  end(tMs, busy) {
    const c = this.cur; if (!c) return;
    c.total_ms = tMs - c._start; c.react_busy = busy ? 1 : 0; delete c._start;
    this.rows.push(c); if (this.rows.length > MAX) this.rows.shift();
    this.cur = null;
  }
  stats(lastSec = Infinity) {
    const R = this.rows; if (!R.length) return null;
    const tEnd = R[R.length - 1].t, rs = lastSec === Infinity ? R : R.filter(r => r.t >= tEnd - lastSec);
    const q = (k) => pct(rs.map(r => r[k]).filter(Number.isFinite));
    const obs = rs.filter(r => r.observed).length;
    // 丢帧片段：连续 observed=0 的帧，长度 = 从片段前最后一次看到脸到重新看到脸
    const runs = []; let start = null;
    for (let i = 0; i < rs.length; i++) {
      if (!rs[i].observed) { if (start === null) start = i ? rs[i - 1].t : rs[i].t; }
      else if (start !== null) { runs.push(rs[i].t - start); start = null; }
    }
    if (start !== null) runs.push(rs[rs.length - 1].t - start);   // 结尾还在丢帧（未结束）
    const hist = new Array(GAP_BINS.length + 1).fill(0);
    for (const d of runs) { let b = GAP_BINS.findIndex(x => d < x); hist[b < 0 ? GAP_BINS.length : b]++; }
    const fdt = q('frame_dt');
    return { n: rs.length, dur: rs.length ? rs[rs.length - 1].t - rs[0].t : 0,
             mp: q('mp_ms'), grud: q('grud_ms'), total: q('total_ms'), frame_dt: fdt,
             fps: fdt.p50 ? 1000 / fdt.p50 : NaN,
             obsRatio: obs / rs.length, runs: runs.length, runMax: runs.length ? Math.max(...runs) : 0, hist };
  }
  csv() {
    const cols = ['t', 'frame_dt', 'mp_ms', 'grud_ms', 'total_ms', 'observed', 'gap', 'v', 'a', 'dist', 'react_busy'];
    const f = (x) => (typeof x === 'number' ? (Number.isFinite(x) ? +x.toFixed(4) : '') : x);
    return cols.join(',') + '\n' + this.rows.map(r => cols.map(c => f(r[c])).join(',')).join('\n') + '\n';
  }
}

function pct(a) {
  if (!a.length) return { p50: NaN, p95: NaN, max: NaN, mean: NaN };
  const s = a.slice().sort((x, y) => x - y), at = (p) => s[Math.min(s.length - 1, Math.floor(p * (s.length - 1) + 0.5))];
  return { p50: at(0.5), p95: at(0.95), max: s[s.length - 1], mean: s.reduce((x, y) => x + y, 0) / s.length };
}
