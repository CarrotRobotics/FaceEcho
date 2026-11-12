// FaceEcho 多语言（日/中/英）。文字都在 static/i18n.json。
// 语言优先级：URL ?lang=ja|zh|en  >  上次选择（localStorage）  >  日语
let DICT = {}, lang = 'ja';
const listeners = [];

export async function initI18n() {
  DICT = await (await fetch('./i18n.json', { cache: 'no-cache' })).json();
  let saved = null;
  try { saved = localStorage.getItem('faceecho.lang'); } catch (e) {}
  const q = new URLSearchParams(location.search).get('lang');
  lang = [q, saved, 'ja'].find(x => x && DICT[x]);
  document.documentElement.lang = lang === 'zh' ? 'zh-CN' : lang;
}

// t('key', {name: value}) —— 缺词时退回中文，再退回 key 本身
export function t(k, p = {}) {
  let s = (DICT[lang] && DICT[lang][k]) ?? (DICT.zh && DICT.zh[k]) ?? k;
  if (typeof s === 'string') for (const n in p) s = s.split('{' + n + '}').join(p[n]);
  return s;
}

export const getLang = () => lang;
export const onLang = f => listeners.push(f);

export function setLang(l) {
  if (!DICT[l] || l === lang) return;
  lang = l;
  try { localStorage.setItem('faceecho.lang', l); } catch (e) {}
  document.documentElement.lang = l === 'zh' ? 'zh-CN' : l;
  applyStatic();
  listeners.forEach(f => f(l));
}

// 带 data-i18n（纯文本）/ data-i18n-html（含标签）的元素按当前语言重写
export function applyStatic(root = document) {
  root.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  root.querySelectorAll('[data-i18n-html]').forEach(el => { el.innerHTML = t(el.dataset.i18nHtml); });
  document.title = t('title');
}
