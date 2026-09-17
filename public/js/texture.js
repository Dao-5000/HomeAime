'use strict';
/* ============================================================
   对话质感后处理引擎（方式B）—— JS 版
   复用「语气情绪优化方案」的核心逻辑：口癖 / 欲言又止 / 轻量错别字
   对 AI 完整回复做概率加工，让文字更像真人随手打的
   ============================================================ */
const Texture = (() => {
  // ── 口癖（句尾语气词，按情绪）──
  const TAIL = {
    happy:   ['～', '呀', '嘻嘻', '哈哈', '耶', '呢', '啦'],
    warm:    ['呢', '嗯', '啊', '哦', '～'],
    shy:     ['……', '嗯', '啊', '呢……', '吧'],
    excited: ['！', '呀！', '哇', '嗯嗯！', '耶！'],
    sad:     ['……', '呢……', '吧……', '哦……'],
    sulky:   ['。', '哼', '啊', '……'],
    angry:   ['！', '啊！', '哼！'],
    tired:   ['……', '嗯……', '呢……'],
    worried: ['呢', '啊', '哦……', '嗯'],
    jealous: ['哼', '啊', '呢……', '嗯'],
    longing: ['呢', '啊……', '……', '嗯'],
    neutral: ['呢', '嗯', '啊', '哦', '吧'],
  };
  const TAIL_PROB = {
    happy: 0.5, warm: 0.5, shy: 0.55, excited: 0.6, sad: 0.5,
    sulky: 0.4, angry: 0.45, tired: 0.45, worried: 0.4,
    jealous: 0.4, longing: 0.5, neutral: 0.35,
  };

  // ── 欲言又止（省略号，按情绪）──
  const ELLIPSIS_PROB = {
    shy: 0.35, sad: 0.3, longing: 0.3, sulky: 0.35, warm: 0.15,
    worried: 0.18, tired: 0.2, neutral: 0.08, happy: 0.05,
    excited: 0.05, angry: 0.1, jealous: 0.18,
  };

  // ── 情绪映射（本项目枚举 → 质感风格档）──
  const EMOTION_MAP = {
    happy: 'happy', excited: 'excited', tender: 'warm', playful: 'happy',
    calm: 'neutral', worried: 'worried', sad: 'sad', upset: 'sad',
    angry: 'angry', cold: 'sulky', reconciling: 'warm', loving: 'longing',
  };

  const rnd = (arr) => arr[Math.floor(Math.random() * arr.length)];

  function _map(emotion) {
    return EMOTION_MAP[String(emotion || '').toLowerCase()] || 'neutral';
  }

  function _tail(text, style, intensity) {
    // 已有语气词/标点则概率减半
    const last = text.slice(-1);
    let prob = (TAIL_PROB[style] || 0.3) * intensity;
    if ('呀呢啊哦嗯吧～！哈嘻耶…'.includes(last)) prob *= 0.3;
    if (Math.random() > prob) return text;
    let t = text.replace(/[。！？!?…]+$/, '');
    return t + rnd(TAIL[style] || TAIL.neutral);
  }

  function _ellipsis(text, style, intensity) {
    const prob = (ELLIPSIS_PROB[style] || 0.08) * (0.5 + intensity * 0.5);
    if (Math.random() > prob) return text;
    if (text.endsWith('……') || text.includes('……')) return text;
    let t = text.replace(/[。！？!?…]+$/, '');
    return t + '……';
  }

  /**
   * 对 AI 回复做后处理（口癖 + 欲言又止）。
   * emotion: 后端情绪（happy/excited/tender/.../loving），前端拿不到时用 neutral
   */
  function process(text, emotion, intensity) {
    if (!text || !text.trim()) return text;
    const style = _map(emotion);
    intensity = Math.max(0, Math.min(1, Number(intensity) || 0.5));
    let result = text.trim();
    // 只处理最后一句（避免整段都加语气词显得乱）
    const parts = result.split(/(?<=[。！？!?…\n])/);
    if (parts.length) {
      const last = parts[parts.length - 1];
      let processed = _tail(last, style, intensity);
      processed = _ellipsis(processed, style, intensity);
      parts[parts.length - 1] = processed;
      result = parts.join('');
    }
    return result;
  }

  return { process, map: _map };
})();
