/* ============================================================
   Homeaime · 记忆海 v2（flow-mem.js）
   力导向「神经元图谱」：
     · 节点 = 一条记忆；半径/光晕 = 重要性；色相 = 类型 + 逐条抖动（不再单调）
     · 连线 = 文本相似度；线上有「神经脉冲」沿曲线流动
     · 星野 + 星云 + 暗角；高重要度节点常显标签；进场从中心炸开 + 冲击波
     · 滚轮缩放翻滚 / 空白拖拽平移 / 拖动单个节点（拖后钉住）
     · 点击节点 → 详情卡（类型·星级·时间·正文 + 在列表中查看/编辑/删除）
   列表视图保留，编辑删除也能在列表里做。
   回滚：删掉 index.html 里 flow-mem.js / flow-mem.css 两行引用。
   ============================================================ */
'use strict';
(function () {
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const ease = (t) => 1 - Math.pow(1 - t, 3);

  const TYPE_HUE = { '事实': 340, '情节': 265, '偏好': 26, '通用': 205 };

  function readCard(el) {
    const head = el.querySelector('.mem-card-head');
    const clone = el.cloneNode(true);
    clone.querySelectorAll('.mem-card-head,.mem-card-foot,button,input').forEach((n) => n.remove());
    const text = (clone.textContent || '').replace(/\s+/g, ' ').trim();
    const headTxt = ((head && head.textContent) || '').replace(/\s+/g, ' ');
    const stars = (headTxt.match(/★/g) || []).length || 3;
    let type = '通用';
    if (/事实/.test(headTxt)) type = '事实';
    else if (/情节|episode/i.test(headTxt)) type = '情节';
    else if (/偏好|喜好/.test(headTxt)) type = '偏好';
    const tm = headTxt.match(/\d+\s*(分钟|小时|天)前|\d{1,2}\/\d{1,2}/);
    return { text: text || '（无内容）', stars, type, time: tm ? tm[0] : '', el };
  }

  function tokens(s) {
    const set = new Set();
    const t = String(s).toLowerCase();
    (t.match(/[a-z0-9]{2,}/g) || []).forEach((w) => set.add(w));
    const cjk = t.replace(/[^\u4e00-\u9fff]/g, '');
    for (let i = 0; i + 1 < cjk.length; i++) set.add(cjk.slice(i, i + 2));
    return set;
  }
  function sim(a, b) { let n = 0; a.forEach((x) => { if (b.has(x)) n++; }); return n / Math.max(1, a.size + b.size - n); }
  function mulberry(seed) { return function () { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }

  function Sea(host) {
    this.host = host;
    const cv = document.createElement('canvas');
    cv.className = 'sea-canvas';
    this.cv = cv; this.ctx = cv.getContext('2d');
    this.nodes = []; this.links = [];
    this.scale = 1; this.tScale = 1;
    this.px = 0; this.py = 0; this.tpx = 0; this.tpy = 0;
    this.hot = null; this.selected = null; this.dragN = null;
    this.t0 = performance.now();
    this.stars = this._makeStars();
    this._bind();
    this._loop = this._loop.bind(this);
    requestAnimationFrame(this._loop);
  }

  Sea.prototype._makeStars = function () {
    const rnd = mulberry(20260914); const out = [];
    for (let i = 0; i < 110; i++) out.push({ x: rnd(), y: rnd(), r: .4 + rnd() * 1.5, ph: rnd() * 6.28, sp: .6 + rnd() * 1.6 });
    return out;
  };

  Sea.prototype.setData = function (items) {
    const w = this.cv.clientWidth || 900, h = this.cv.clientHeight || 520;
    const prev = new Map(this.nodes.map((n) => [n.key, n]));
    const nodes = items.map((it, i) => {
      const ang = (i / Math.max(1, items.length)) * Math.PI * 2 + (i % 2 ? .5 : 0);
      const rad = 60 + Math.random() * 130;
      const key = it.text.slice(0, 40) + it.stars;
      const hue = (TYPE_HUE[it.type] != null ? TYPE_HUE[it.type] : 205) + ((i * 37) % 23) - 11;
      const base = {
        key, text: it.text, stars: it.stars, type: it.type, time: it.time, el: it.el, hue,
        r: 8 + it.stars * 3.2, phase: Math.random() * 6.28,
        ox: w / 2 + Math.cos(ang) * rad, oy: h / 2 + Math.sin(ang) * rad,
        born: performance.now() + i * 45,
      };
      const old = prev.get(key);
      if (old) { Object.assign(old, base); if (!old.x) old.x = base.ox; if (!old.y) old.y = base.oy; return old; }
      return Object.assign(base, { x: base.ox, y: base.oy, vx: 0, vy: 0, pin: false });
    });
    nodes.forEach((n) => { n.tk = tokens(n.text); });
    const links = []; const seen = new Set();
    nodes.forEach((a, i) => {
      const near = nodes.map((b, j) => ({ j, s: i === j ? -1 : sim(a.tk, b.tk) }))
        .filter((o) => o.s > 0).sort((x, y) => y.s - x.s).slice(0, 2);
      near.forEach((o) => {
        const k = i < o.j ? i + '-' + o.j : o.j + '-' + i;
        if (seen.has(k)) return; seen.add(k);
        links.push({ a: nodes[i], b: nodes[o.j], s: o.s, ph: Math.random() });
      });
      const same = nodes.find((b, j) => j !== i && b.type === a.type);
      if (same) {
        const ib = nodes.indexOf(same);
        const k = i < ib ? i + '-' + ib : ib + '-' + i;
        if (!seen.has(k)) { seen.add(k); links.push({ a, b: same, s: .1, ph: Math.random() }); }
      }
    });
    this.nodes = nodes; this.links = links; this.selected = null; this.hot = null;
    try { this._hideCard(); } catch (_) {}
    this.wave = performance.now();
    try { for (let i = 0; i < 260; i++) this._step(1); this._draw(true); } catch (_) {}
  };

  Sea.prototype._world = function (e) {
    const r = this.cv.getBoundingClientRect();
    return {
      x: (e.clientX - r.left - this.px - r.width / 2) / this.scale + r.width / 2,
      y: (e.clientY - r.top - this.py - r.height / 2) / this.scale + r.height / 2,
    };
  };
  Sea.prototype._hit = function (p) {
    let hit = null, best = 1e9;
    this.nodes.forEach((n) => { const d = Math.hypot(n.x - p.x, n.y - p.y); if (d < n.r + 13 && d < best) { best = d; hit = n; } });
    return hit;
  };
  Sea.prototype._bind = function () {
    const cv = this.cv, self = this;
    let panning = false, lx = 0, ly = 0;
    cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      self.tScale = clamp(self.tScale * (e.deltaY > 0 ? .88 : 1.14), .4, 3.2);
    }, { passive: false });
    cv.addEventListener('mousedown', (e) => {
      const n = self._hit(self._world(e));
      if (n) { self.dragN = n; n.pin = true; n.vx = n.vy = 0; cv.classList.add('grabbing'); }
      else { panning = true; lx = e.clientX; ly = e.clientY; cv.classList.add('grab'); }
    });
    window.addEventListener('mouseup', () => { self.dragN = null; panning = false; cv.classList.remove('grab', 'grabbing'); });
    cv.addEventListener('mousemove', (e) => {
      const p = self._world(e);
      if (self.dragN) {
        self.dragN.x = clamp(p.x, 20, cv.clientWidth - 20);
        self.dragN.y = clamp(p.y, 20, cv.clientHeight - 20);
        self.dragN.vx = self.dragN.vy = 0;
        return;
      }
      if (panning) {
        self.tpx += e.clientX - lx; self.tpy += e.clientY - ly;
        self.px = self.tpx; self.py = self.tpy;
        lx = e.clientX; ly = e.clientY; return;
      }
      self.hot = self._hit(p);
      cv.style.cursor = self.hot ? 'pointer' : '';
    });
    cv.addEventListener('mouseleave', () => { self.hot = null; });
    cv.addEventListener('click', () => {
      if (self.hot) { self.selected = self.hot; self._showCard(self.hot); }
      else { self.selected = null; self._hideCard(); }
    });
  };

  /* 详情卡里的动作 → 转发给列表里那条记忆原本的按钮 */
  Sea.prototype._cardAction = function (n, kind) {
    if (kind === 'list') {
      this.selected = null; this._hideCard();
      const m = document.querySelector('#page-memory .sea-modes button[data-m="list"]');
      if (m) m.click();
      setTimeout(() => {
        try {
          n.el.scrollIntoView({ block: 'center', behavior: 'smooth' });
          n.el.style.transition = 'box-shadow .3s';
          n.el.style.boxShadow = '0 0 0 2px rgba(255,143,168,.85)';
          setTimeout(() => { n.el.style.boxShadow = ''; }, 1700);
        } catch (_) {}
      }, 380);
      return;
    }
    const foot = n.el && n.el.querySelector('.mem-card-foot');
    if (!foot) return;
    const btns = $$('button', foot);
    const target = kind === 'edit'
      ? btns.find((b) => /编辑/.test(b.textContent || ''))
      : btns.find((b) => /删除|移除/.test(b.textContent || ''));
    if (target) { try { target.click(); } catch (_) {} }
  };
  Sea.prototype._showCard = function (n) {
    const box = document.querySelector('.sea-card');
    if (!box) return;
    box.innerHTML =
      '<div class="sc-type"><span class="sc-dot" style="background:hsl(' + n.hue + ',85%,66%)"></span>' + n.type
      + '<span class="sc-stars">' + '★'.repeat(n.stars) + '☆'.repeat(5 - n.stars) + '</span>'
      + (n.time ? '<span class="sc-time">' + n.time + '</span>' : '') + '</div>'
      + '<div class="sc-text">' + n.text + '</div>'
      + '<div class="sc-acts">'
      + '<button class="sc-b" data-a="list">在列表中查看</button>'
      + '<button class="sc-b" data-a="edit">编辑</button>'
      + '<button class="sc-b danger" data-a="del">删除</button>'
      + '</div>';
    box.classList.add('on');
    clearTimeout(this._hideT);
    this._hideT = setTimeout(() => this._hideCard(), 15000);
    box.onmouseenter = () => clearTimeout(this._hideT);
    box.onmouseleave = () => { this._hideT = setTimeout(() => this._hideCard(), 4500); };
    const self2 = this;
    if (!box.querySelector('.sc-x')) { var _x = document.createElement('button'); _x.className = 'sc-x'; _x.type = 'button'; _x.textContent = '✕'; _x.addEventListener('click', function (ev) { ev.stopPropagation(); self2._hideCard(); }); box.appendChild(_x); }
    $$('.sc-b', box).forEach((b) => b.addEventListener('click', (ev) => {
      ev.stopPropagation();
      this._cardAction(n, b.dataset.a);
    }));
  };
  Sea.prototype._hideCard = function () { clearTimeout(this._hideT); this.selected = null; const b = document.querySelector('.sea-card'); if (b) b.classList.remove('on'); };

  Sea.prototype._step = function (dt) {
    const w = this.cv.clientWidth || 900, h = this.cv.clientHeight || 520;
    const ns = this.nodes;
    for (let i = 0; i < ns.length; i++) {
      const a = ns[i];
      for (let j = i + 1; j < ns.length; j++) {
        const b = ns[j];
        let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy;
        if (d2 < 1) { d2 = 1; dx = Math.random() - .5; dy = Math.random() - .5; }
        const d = Math.sqrt(d2);
        const f = (2100 + (a.r + b.r) * 52) / d2;
        const ux = dx / d, uy = dy / d;
        a.vx -= ux * f * dt; a.vy -= uy * f * dt;
        b.vx += ux * f * dt; b.vy += uy * f * dt;
      }
    }
    this.links.forEach((L) => {
      const dx = L.b.x - L.a.x, dy = L.b.y - L.a.y, d = Math.max(1, Math.hypot(dx, dy));
      const want = 112 + (L.a.r + L.b.r) * 2.4;
      const f = (d - want) * 0.013 * (0.5 + L.s);
      const ux = dx / d, uy = dy / d;
      L.a.vx += ux * f * dt; L.a.vy += uy * f * dt;
      L.b.vx -= ux * f * dt; L.b.vy -= uy * f * dt;
    });
    ns.forEach((n) => {
      if (n.pin) { n.vx = n.vy = 0; return; }
      n.vx += (w / 2 - n.x) * 0.0017 * dt;
      n.vy += (h / 2 - n.y) * 0.0017 * dt;
      n.vx *= .9; n.vy *= .9;
      n.vx = clamp(n.vx, -2.4, 2.4); n.vy = clamp(n.vy, -2.4, 2.4);
      n.x = clamp(n.x + n.vx, 22, w - 22); n.y = clamp(n.y + n.vy, 22, h - 22);
    });
  };

  Sea.prototype._draw = function (forceEnt) {
    const cv = this.cv, ctx = this.ctx, w = cv.clientWidth || 900, h = cv.clientHeight || 520;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const now = performance.now(), t = (now - this.t0) / 1000;

    for (let k = 0; k < 4; k++) {
      const gx = w * (.28 + .22 * Math.sin(t * .1 + k * 1.9) + k * .1);
      const gy = h * (.38 + .24 * Math.cos(t * .09 + k * 1.4));
      const grd = ctx.createRadialGradient(gx, gy, 0, gx, gy, Math.max(w, h) * .5);
      grd.addColorStop(0, ['rgba(255,143,168,.13)', 'rgba(167,139,250,.12)', 'rgba(255,195,155,.09)', 'rgba(127,178,217,.10)'][k]);
      grd.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = grd; ctx.fillRect(0, 0, w, h);
    }
    this.stars.forEach((s) => {
      const a = .18 + .5 * (.5 + .5 * Math.sin(t * s.sp + s.ph));
      ctx.fillStyle = 'rgba(244,241,248,' + a.toFixed(3) + ')';
      ctx.beginPath(); ctx.arc(s.x * w, s.y * h, s.r, 0, 6.283); ctx.fill();
    });
    const vg = ctx.createRadialGradient(w / 2, h / 2, Math.min(w, h) * .22, w / 2, h / 2, Math.max(w, h) * .72);
    vg.addColorStop(0, 'rgba(0,0,0,0)'); vg.addColorStop(1, 'rgba(6,3,14,.55)');
    ctx.fillStyle = vg; ctx.fillRect(0, 0, w, h);

    ctx.save();
    ctx.translate(this.px + w / 2, this.py + h / 2);
    ctx.scale(this.scale, this.scale);
    ctx.translate(-w / 2, -h / 2);

    if (this.wave && now - this.wave < 1100) {
      const p = (now - this.wave) / 1100;
      ctx.strokeStyle = 'rgba(255,143,168,' + (0.5 * (1 - p)).toFixed(3) + ')';
      ctx.lineWidth = 2.4 * (1 - p) + .4;
      ctx.beginPath(); ctx.arc(w / 2, h / 2, 30 + ease(p) * Math.min(w, h) * .62, 0, 6.283); ctx.stroke();
    }

    this.links.forEach((L) => {
      const hi = (this.hot && (this.hot === L.a || this.hot === L.b)) || (this.selected && (this.selected === L.a || this.selected === L.b));
      const mx = (L.a.x + L.b.x) / 2, my = (L.a.y + L.b.y) / 2;
      const nx = -(L.b.y - L.a.y), ny = (L.b.x - L.a.x), nl = Math.max(1, Math.hypot(nx, ny));
      const bow = 16 * Math.sin(t * .6 + nl);
      const cx = mx + (nx / nl) * bow, cy = my + (ny / nl) * bow;
      ctx.beginPath(); ctx.moveTo(L.a.x, L.a.y); ctx.quadraticCurveTo(cx, cy, L.b.x, L.b.y);
      ctx.strokeStyle = hi ? 'rgba(255,143,168,.85)' : 'rgba(255,143,168,' + (0.10 + L.s * 0.5).toFixed(3) + ')';
      ctx.lineWidth = hi ? 1.8 : 0.9 + L.s;
      ctx.stroke();
      const cnt = L.s > .28 ? 2 : 1;
      for (let q = 0; q < cnt; q++) {
        const tt = ((t * (.16 + L.s * .22) + L.ph + q * .5) % 1), omt = 1 - tt;
        const px = omt * omt * L.a.x + 2 * omt * tt * cx + tt * tt * L.b.x;
        const py = omt * omt * L.a.y + 2 * omt * tt * cy + tt * tt * L.b.y;
        const rr = 1.5 + L.s * 2.2;
        const gg = ctx.createRadialGradient(px, py, 0, px, py, rr * 3.4);
        gg.addColorStop(0, 'hsla(' + L.a.hue + ',90%,72%,.95)');
        gg.addColorStop(1, 'hsla(' + L.a.hue + ',90%,72%,0)');
        ctx.fillStyle = gg;
        ctx.beginPath(); ctx.arc(px, py, rr * 3.4, 0, 6.283); ctx.fill();
      }
    });

    this.nodes.forEach((n) => {
      const born = n.born || 0;
      const ent = forceEnt ? 1 : (born > now ? 0 : Math.min(1, (now - born) / 900));
      if (ent <= 0) return;
      const e = ease(ent);
      const x = w / 2 + (n.x - w / 2) * e, y = h / 2 + (n.y - h / 2) * e;
      const pulse = 1 + .1 * Math.sin(t * 1.7 + n.phase);
      const r = n.r * pulse * (n === this.hot ? 1.3 : 1) * (.4 + .6 * e);
      const hue = n.hue;
      const halo = ctx.createRadialGradient(x, y, 0, x, y, r * 3.2);
      halo.addColorStop(0, 'hsla(' + hue + ',95%,72%,' + (.30 * e).toFixed(3) + ')');
      halo.addColorStop(.45, 'hsla(' + hue + ',92%,62%,.10)');
      halo.addColorStop(1, 'hsla(' + hue + ',92%,62%,0)');
      ctx.fillStyle = halo; ctx.beginPath(); ctx.arc(x, y, r * 3.2, 0, 6.283); ctx.fill();
      /* 球体：底部阴影 → 主渐变 → 亮边 → 镜面高光（玻璃弹珠感） */
      const shade = ctx.createRadialGradient(x, y + r * .5, r * .1, x, y, r * 1.02);
      shade.addColorStop(0, 'hsla(' + hue + ',84%,26%,' + (.85 * e).toFixed(3) + ')');
      shade.addColorStop(1, 'hsla(' + hue + ',80%,40%,0)');
      ctx.fillStyle = shade; ctx.beginPath(); ctx.arc(x, y, r, 0, 6.283); ctx.fill();
      const sph = ctx.createRadialGradient(x - r * .36, y - r * .42, r * .06, x + r * .1, y + r * .12, r * 1.34);
      sph.addColorStop(0, 'hsla(' + hue + ',100%,95%,' + e.toFixed(3) + ')');
      sph.addColorStop(.3, 'hsla(' + hue + ',97%,76%,' + e.toFixed(3) + ')');
      sph.addColorStop(.72, 'hsla(' + hue + ',88%,56%,' + e.toFixed(3) + ')');
      sph.addColorStop(1, 'hsla(' + hue + ',78%,38%,' + e.toFixed(3) + ')');
      ctx.fillStyle = sph; ctx.beginPath(); ctx.arc(x, y, r, 0, 6.283); ctx.fill();
      const lw = Math.max(1, r * .09);
      ctx.strokeStyle = 'hsla(' + hue + ',100%,85%,' + (.5 * e).toFixed(3) + ')';
      ctx.lineWidth = lw;
      ctx.beginPath(); ctx.arc(x, y, r - lw * .5, 0, 6.283); ctx.stroke();
      ctx.fillStyle = 'rgba(255,255,255,' + (.82 * e).toFixed(3) + ')';
      ctx.beginPath();
      if (ctx.ellipse) ctx.ellipse(x - r * .35, y - r * .4, r * .23, r * .15, -.55, 0, 6.283);
      else ctx.arc(x - r * .35, y - r * .4, r * .18, 0, 6.283);
      ctx.fill();
      if (n.stars >= 4) {
        const pr = r + 6 + 5 * (.5 + .5 * Math.sin(t * 1.4 + n.phase));
        ctx.strokeStyle = 'hsla(' + hue + ',92%,72%,' + (.34 * e).toFixed(3) + ')';
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.arc(x, y, pr, 0, 6.283); ctx.stroke();
      }
      if (n.pin) {
        ctx.setLineDash([3, 3]); ctx.strokeStyle = 'rgba(255,255,255,.35)'; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(x, y, r + 4, 0, 6.283); ctx.stroke(); ctx.setLineDash([]);
      }
      if (n === this.selected) {
        ctx.strokeStyle = 'rgba(255,255,255,.92)'; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(x, y, r + 8, 0, 6.283); ctx.stroke();
      }
      if (n === this.hot || n.stars >= 4) {
        const txt = n.text.slice(0, n === this.hot ? 26 : 12) + (n.text.length > 12 ? '…' : '');
        ctx.font = (n === this.hot ? '700 12px' : '600 10.5px') + ' "Noto Sans SC", system-ui, sans-serif';
        const tw = ctx.measureText(txt).width;
        ctx.fillStyle = n === this.hot ? 'rgba(18,16,32,.92)' : 'rgba(18,16,32,.55)';
        const bx = x - tw / 2 - 8, by = y + r + 7;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(bx, by, tw + 16, 21, 8); else ctx.rect(bx, by, tw + 16, 21);
        ctx.fill();
        ctx.fillStyle = n === this.hot ? '#F4F1F8' : 'rgba(244,241,248,.8)';
        ctx.fillText(txt, x - tw / 2, by + 15);
      }
    });
    ctx.restore();
  };

  Sea.prototype._loop = function () {
    this.scale += (this.tScale - this.scale) * .12;
    this.px += (this.tpx - this.px) * .18;
    this.py += (this.tpy - this.py) * .18;
    if (this.nodes.length) { this._step(1); this._draw(); }
    requestAnimationFrame(this._loop);
  };

  /* ============================================================
     挂载
     ============================================================ */
  let sea = null;
  let mode = 'sea';
  function seaH() { return '100%'; }

  function buildShell(body) {
    let bar = body.querySelector('.sea-modes');
    if (!bar) {
      bar = document.createElement('div');
      bar.className = 'sea-modes';
      bar.innerHTML = '<button data-m="sea" class="on">记忆海</button><button data-m="list">列表</button>'
        + '<span class="sea-stats"></span>'
        + '<span class="sea-hint">滚轮缩放 · 拖背景平移 · 拖节点摆位 · 悬停高亮 · 点击看详情</span>';
      body.insertBefore(bar, body.firstChild);
      bar.addEventListener('click', (e) => {
        const b = e.target.closest('button[data-m]'); if (!b) return;
        mode = b.dataset.m;
        bar.querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
        applyMode();
      });
    }
    let wrap = body.querySelector('.sea-wrap');
    if (!wrap) {
      wrap = document.createElement('div');
      wrap.className = 'sea-wrap';
      const card = document.createElement("div"); card.className = "sea-card"; document.body.appendChild(card); void card;
      // 工具条（搜索/角色/重评）要排在记忆海上面，否则会被海挤到屏幕外
      const _tb = body.querySelector('.mem-toolbar');
      body.insertBefore(wrap, _tb ? _tb.nextSibling : bar.nextSibling);
      sea = new Sea(wrap);
      wrap.insertBefore(sea.cv, wrap.firstChild);
      if (window.ResizeObserver) new ResizeObserver(() => { try { sea.cv.style.height = seaH(); } catch (_) {} }).observe(wrap);
    }
    sea.cv.style.height = seaH();
  }

  function applyMode() {
    const sec = document.getElementById('page-memory'); if (sec) sec.classList.toggle('mem-sea', mode === 'sea');
    const list = $('#page-memory .mem-list');
    const wrap = $('#page-memory .sea-wrap');
    const hero = $('#page-memory .mem-hero');
    if (list) list.style.display = mode === 'sea' ? 'none' : '';
    if (wrap) wrap.style.display = mode === 'sea' ? '' : 'none';
    if (hero) hero.style.display = mode === 'sea' ? '' : '';
    if (mode !== 'sea') $$('.mem-stream > .mem-card').forEach((c) => { c.classList.remove('hz-pending'); c.classList.add('in'); });
    if (mode === 'sea' && sea) { sea.cv.style.height = seaH(); try { for (let i = 0; i < 60; i++) sea._step(1); sea._draw(true); } catch (_) {} }
  }

  function enhance() {
    const body = $('#page-memory .mem-body');
    if (!body) return;
    const list = body.querySelector('.mem-list');
    if (!list) return;
    const cards = $$(':scope > .mem-card', list);
    buildShell(body);
    const items = cards.map(readCard);
    try {
      const st = document.querySelector('#page-memory .sea-stats');
      if (st) {
        const sum = items.reduce((a, b) => a + b.stars, 0);
        const avg = items.length ? (sum / items.length).toFixed(1) : '0';
        st.innerHTML = '<b>' + items.length + '</b> 条 · 平均 <b>' + avg + '</b> 星';
      }
    } catch (_) {}
    if (sea) {
      const sig = items.map((i) => i.text.slice(0, 24) + i.stars).join('|');
      if (sea.__sig !== sig) { sea.__sig = sig; sea.setData(items); }
    }
    applyMode();
  }

  function hook() {
    const orig = window.hzShowView;
    if (typeof orig === 'function' && !orig.__hzSea) {
      const w = function () { const r = orig.apply(this, arguments); setTimeout(() => { try { enhance(); } catch (_) {} }, 260); return r; };
      w.__hzSea = true; w.__hzOrig = orig; window.hzShowView = w;
    }
    window.addEventListener('resize', () => { try { if (sea) { sea.cv.style.height = seaH(); for (let i = 0; i < 60; i++) sea._step(1); sea._draw(true); } } catch (_) {} });
    setInterval(() => { try { if ($('#page-memory .mem-list')) enhance(); } catch (_) {} }, 2200);
    /* 滚动/切页时收起详情卡，避免它悬在空海面上 */
    document.addEventListener('scroll', () => { try { if (sea) sea._hideCard(); } catch (_) {} }, true);
    window.addEventListener('blur', () => { try { if (sea) sea._hideCard(); } catch (_) {} });
  }

  function boot() { hook(); setTimeout(() => { try { enhance(); } catch (_) {} }, 900); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(boot, 700));
  else setTimeout(boot, 700);
})();
