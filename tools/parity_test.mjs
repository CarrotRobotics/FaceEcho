// 数值一致性测试：PyTorch GRUD vs 浏览器 JS 引擎
// 用法：node tools/parity_test.mjs <weights.json> <ref.json>
import fs from 'node:fs';
import { SafeAffectEstimator } from '../static/js/affect_grud.js';
const [w, ref] = process.argv.slice(2).map(p => JSON.parse(fs.readFileSync(p, 'utf8')));
const est = new SafeAffectEstimator(w);
let maxErr = 0;
ref.t.forEach((t, i) => {
  const o = est.step(ref.mask[i] ? ref.x[i] : null, t);
  maxErr = Math.max(maxErr, Math.abs(o.v - ref.y[i][0]), Math.abs(o.a - ref.y[i][1]));
});
console.log(`steps=${ref.t.length}  max|JS-PyTorch|=${maxErr.toExponential(2)}`);
process.exit(maxErr < 1e-4 ? 0 : 1);
