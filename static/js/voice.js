// 情绪反馈伙伴的声音：只说当前界面语言。
// 1) 优先播放预先生成的音频 static/voice/{lang}/{emo}_{i}.mp3（见 tools/make_voices.py，音质好、完全离线）
// 2) 没有音频时，用浏览器/系统自带的语音合成（Web Speech API）
// 3) 两者都没有就不出声（页面上会提示）
import { getLang } from './i18n.js';

export const clean = s => s.replace(/[\p{Extended_Pictographic}‍️]/gu, '').replace(/[～~]/g, '').trim();

export class CompanionVoice {
  constructor() {
    this.on = true;
    this.voices = [];
    this.manifest = null;
    this.audio = new Audio();
    this.lastT = -1e9;
    this.onchange = null;
    this.hasTTS = 'speechSynthesis' in window;
    if (this.hasTTS) {
      const load = () => { this.voices = speechSynthesis.getVoices(); this.onchange && this.onchange(); };
      load();
      speechSynthesis.addEventListener ? speechSynthesis.addEventListener('voiceschanged', load)
                                       : (speechSynthesis.onvoiceschanged = load);
    }
    fetch('./voice/manifest.json', { cache: 'no-cache' })
      .then(r => (r.ok ? r.json() : null))
      .then(m => { this.manifest = m; this.onchange && this.onchange(); })
      .catch(() => {});
  }

  pickVoice(lang) {
    const vs = this.voices.filter(v => (v.lang || '').toLowerCase().replace('_', '-').startsWith(lang));
    if (lang === 'zh') {
      const cn = vs.find(v => /zh-(cn|hans)/i.test(v.lang.replace('_', '-')));
      if (cn) return cn;
    }
    return vs.find(v => v.localService) || vs[0] || null;
  }

  hasFiles(lang) { return !!(this.manifest && this.manifest[lang] && this.manifest[lang].length); }

  // 当前语言会用哪种方式出声：{kind:'file'|'tts'|'none', name}
  mode(lang = getLang()) {
    if (this.hasFiles(lang)) return { kind: 'file' };
    const v = this.hasTTS && this.pickVoice(lang);
    return v ? { kind: 'tts', name: v.name } : { kind: 'none' };
  }

  stop() {
    try { this.audio.pause(); } catch (e) {}
    if (this.hasTTS) speechSynthesis.cancel();
  }

  // key 形如 'happy_2'（对应音频文件名）；2.5 秒内不重复开口，避免一直念
  say(text, key) {
    if (!this.on) return;
    const now = performance.now();
    if (now - this.lastT < 2500) return;
    this.lastT = now;
    const lang = getLang();
    this.stop();
    if (key && this.manifest && (this.manifest[lang] || []).includes(key)) {
      this.audio.src = `./voice/${lang}/${key}.mp3`;
      this.audio.play().catch(() => {});
      return;
    }
    const v = this.hasTTS && this.pickVoice(lang);
    if (!v) return;
    const u = new SpeechSynthesisUtterance(clean(text));
    u.voice = v; u.lang = v.lang; u.rate = 1.0; u.pitch = 1.15;
    speechSynthesis.speak(u);
  }
}
