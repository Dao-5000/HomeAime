'use strict';
/* ============================================================
   学习档案：她学到了什么 + 她整理了什么（可一键撤回）
   ------------------------------------------------------------
   ★ 2026-09-17 新增。用户拍板："允许她自己整理记忆，但每次改动要留痕、可查、可一键撤回。"
     在这之前，学习成果散在 kv / learned_rules 文件 / 反思表里，用户完全看不到 ——
     "AI 到底有没有在学"只能靠感觉。这个面板把它变成看得见、能反悔的东西。

   后端接口：
     GET  /api/learning/status          → 学到的偏好 / 规矩 / 反思（含"用没用过"）
     GET  /api/memory/audit             → 整理批次 + 明细（含 before/after）
     POST /api/memory/audit/revert      → {batch_id} 整批撤回 / {audit_id} 撤单条
   ============================================================ */
const Learn = {
  contactId: null,
  data: null,

  open(contactId) {
    this.contactId = contactId || (window.Chat && Chat.contactId) || '';
    this.render();
    $('#learn-page').classList.add('open');
  },

  close() {
    $('#learn-page').classList.remove('open');
  },

  _sid() {
    try { return (window.Chat && Chat.sessionId) || _memorySessionId(); }
    catch (e) { return 'default'; }
  },

  _cid() {
    try {
      const c = Store.getContact(this.contactId);
      return (c && (c.name || c.id)) ||
        (window.Chat && Chat.contact && (Chat.contact.name || Chat.contact.id)) || 'default';
    } catch (e) { return 'default'; }
  },

  async render() {
    const body = $('#learn-body');
    body.innerHTML = '';
    body.appendChild(h('div', { class: 'help-box', style: 'margin:2px 2px 12px', text:
      '这里是 TA 为你学到的东西：\n' +
      '· 「学到的」是 TA 从你的批评、立规矩、长期相处里沉淀下来的（会用在之后的每次对话）\n' +
      '· 「整理记录」是 TA 自己动手改过记忆的地方 —— 每一批都可以一键撤回，撤回后恢复原样' }));

    const loading = h('div', { class: 'learn-loading', text: '正在读取…' });
    body.appendChild(loading);

    const sid = encodeURIComponent(this._sid());
    const cid = encodeURIComponent(this._cid());
    let learning = null, audit = null;
    try {
      const [r1, r2] = await Promise.all([
        fetch(`/api/learning/status?session_id=${sid}&character_id=${cid}`),
        fetch(`/api/memory/audit?session_id=${sid}&character_id=${cid}&limit=60`),
      ]);
      learning = await r1.json();
      audit = await r2.json();
    } catch (e) {
      loading.textContent = '读取失败：' + (e && e.message ? e.message : e);
      return;
    }
    this.data = { learning, audit };
    loading.remove();
    this._renderLearned(body, learning);
    this._renderAudit(body, audit);
  },

  _section(body, title, count) {
    const wrap = h('div', { class: 'learn-sec' });
    wrap.appendChild(h('div', { class: 'sec-toolbar' },
      h('span', { class: 'sec-toolbar-title', text: title + (count != null ? `（${count}）` : '') })));
    body.appendChild(wrap);
    return wrap;
  },

  /* ── 上半：她学到了什么 ───────────────────────────── */
  _renderLearned(body, data) {
    data = data || {};
    const styles = data.style_preferences || [];
    const rules = data.rules || [];
    const refl = data.reflections || [];

    if (!styles.length && !rules.length && !refl.length) {
      body.appendChild(h('div', { class: 'help-box', text:
        'TA 还没学到什么。\n\n' +
        '想让它开始学：\n' +
        '· 直接批评回应方式 ——「你别老讲道理」「你这样回我很难受」\n' +
        '· 立一条长期规矩 ——「以后别半夜问我睡没睡」「记住我不吃香菜」' }));
      return;
    }

    if (styles.length) {
      const s = this._section(body, '学到的相处偏好', styles.length);
      for (const t of styles) {
        s.appendChild(h('div', { class: 'learn-item' },
          h('div', { class: 'learn-item-main', text: String(t) })));
      }
    }

    if (rules.length) {
      const s = this._section(body, '记住的规矩', rules.length);
      for (const r of rules) {
        const src = r.source === 'auto' ? '她自己记下的' : '你教的';
        s.appendChild(h('div', { class: 'learn-item' },
          h('div', { class: 'learn-item-main', text: String(r.text || '') }),
          h('div', { class: 'learn-item-meta', text: `#${r.id} · ${src} · ${r.created || ''}` })));
      }
    }

    if (refl.length) {
      const s = this._section(body, '长期相处后形成的理解', refl.length);
      const names = { user_understanding: '对你的理解', relationship_reflection: '关系', strategy_reflection: '相处策略' };
      for (const r of refl) {
        s.appendChild(h('div', { class: 'learn-item' },
          h('div', { class: 'learn-item-meta', text:
            `${names[r.type] || r.type} · 置信度 ${r.confidence != null ? r.confidence : '-'}` +
            (r.used ? ' · 已用过' : ' · 还没用过') }),
          h('div', { class: 'learn-item-main', text: String(r.content || '') })));
      }
    }
  },

  /* ── 下半：她整理了什么（可撤回） ──────────────────── */
  _renderAudit(body, audit) {
    audit = audit || {};
    const batches = audit.batches || [];
    const recent = audit.recent || [];
    const summary = audit.summary || {};

    const opName = { merge: '合并同类项', regrade: '调整重要度', forget: '归档/遗忘',
                     rewrite: '改写内容', rejudge: '纠正重判', revert: '撤回' };
    const total = Object.values(summary).reduce((a, b) => a + (b && b.n ? b.n : 0), 0);

    if (!total) {
      body.appendChild(h('div', { class: 'help-box', text:
        'TA 还没有自己整理过记忆。\n\n' +
        '（当同一件事被反复记下来时，TA 会自动合并；你也可以在记忆页手动触发整理。）' }));
      return;
    }

    const s = this._section(body, '整理记录', total);
    s.appendChild(h('div', { class: 'learn-item-meta', style: 'padding:0 4px 8px', text:
      Object.entries(summary).map(([k, v]) => `${opName[k] || k} ${v.n} 次`).join(' · ') }));

    if (!batches.length) {
      s.appendChild(h('div', { class: 'learn-item-meta', text: '（这批改动没有批次号，无法整批撤回）' }));
    }

    for (const b of batches) {
      const row = h('div', { class: 'learn-item' });
      const head = h('div', { class: 'learn-item-main', text:
        `${b.reverted ? '已撤回：' : ''}${opName[b.op] || b.op} ${b.n} 处 · ${b.ts || ''}` });
      row.appendChild(head);
      row.appendChild(h('div', { class: 'learn-item-meta', text: String(b.reason || '') }));

      if (!b.reverted) {
        const btn = h('button', { class: 'chip', text: '↩ 撤回这一批' });
        btn.addEventListener('click', async () => {
          btn.disabled = true;
          btn.textContent = '撤回中…';
          try {
            const res = await fetch('/api/memory/audit/revert', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ batch_id: b.batch_id }),
            });
            const out = await res.json();
            if (out && out.ok) {
              toast(`已恢复 ${out.n || 0} 处改动`);
              this.render();
            } else {
              toast('撤回失败：' + ((out && (out.why || out.error)) || '未知原因'));
              btn.disabled = false;
              btn.textContent = '↩ 撤回这一批';
            }
          } catch (e) {
            toast('撤回失败：' + (e && e.message ? e.message : e));
            btn.disabled = false;
            btn.textContent = '↩ 撤回这一批';
          }
        });
        row.appendChild(btn);
      }
      s.appendChild(row);
    }

    // 明细（最近改动，看得见"改了什么"）
    if (recent.length) {
      const d = this._section(body, '最近改动明细', recent.length);
      for (const r of recent.slice(0, 40)) {
        const item = h('div', { class: 'learn-item' });
        item.appendChild(h('div', { class: 'learn-item-meta', text:
          `${opName[r.op] || r.op} · 记忆 #${r.memory_id} · ${r.ts || ''}` +
          (r.reverted ? ' · 已撤回' : '') }));
        if (r.before_content || r.after_content) {
          item.appendChild(h('div', { class: 'learn-item-main', text:
            (r.after_content ? '改成：' + r.after_content : '（已失效）') }));
          if (r.before_content && r.before_content !== r.after_content) {
            item.appendChild(h('div', { class: 'learn-item-meta', text: '原来是：' + r.before_content }));
          }
        }
        if (r.before_importance != null && r.after_importance != null &&
            r.before_importance !== r.after_importance) {
          item.appendChild(h('div', { class: 'learn-item-meta', text:
            `重要度 ${r.before_importance} → ${r.after_importance}` }));
        }
        item.appendChild(h('div', { class: 'learn-item-meta', text: String(r.reason || '') }));
        d.appendChild(item);
      }
    }
  },
};

$('#learn-back').addEventListener('click', () => Learn.close());
$('#learn-done').addEventListener('click', () => Learn.close());
