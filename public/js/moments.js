'use strict';
/* ============================================================
   朋友圈页（微信风格）：AI 动态流 + 用户发动态 + 点赞/评论
   ============================================================ */

let _moments = [];          // 当前动态列表
let _momentPage = 0;
const _MOMENT_PER = 20;

/* ─── 入口：renderMoments 由 switchTab 调用 ─── */
function renderMoments() {
  _momentPage = 0;
  const wrap = $('#page-moments');
  wrap.innerHTML = '';
  // ★ 只追加类，不要覆盖掉 .page/.active（否则会脱离 tab 切换体系，导致页面重叠 + 背景图常驻）
  wrap.classList.add('moments-page');

  // ── 顶部：朋友圈背景（拉长）+ 用户头像名称 + 发动态入口 ──
  const header = h('div', { class: 'moments-header' });
  header.style.background = 'linear-gradient(160deg, #3A3A38, #141414)';
  header.title = '点击更换朋友圈背景';
  header.style.cursor = 'pointer';

  // ★ 右上角：用户头像 + 名称（微信朋友圈风格）
  const myAv = (Store.getSettings().myAvatar) || '';
  const myNick = (Store.getSettings().meNickname) || '我';
  const userBox = h('div', { class: 'moments-user' });
  const userAvatar = h('div', { class: 'avatar sm moments-user-avatar' });
  if (myAv) {
    userAvatar.appendChild(h('img', { src: myAv, alt: '', style: 'width:100%;height:100%;border-radius:50%;object-fit:cover' }));
  } else {
    userAvatar.style.background = 'rgba(255,255,255,.92)';
    userAvatar.style.color = '#5a5a56';
    userAvatar.style.display = 'flex';
    userAvatar.style.alignItems = 'center';
    userAvatar.style.justifyContent = 'center';
    userAvatar.style.fontWeight = '600';
    userAvatar.textContent = '我';
  }
  userBox.appendChild(userAvatar);
  userBox.appendChild(h('div', { class: 'moments-user-name', text: myNick }));
  header.appendChild(userBox);

  header.appendChild(h('div', { class: 'moments-title', text: '朋友圈' }));
  const publishBtn = h('button', { class: 'moments-publish-btn', text: '📷 发动态' });
  publishBtn.addEventListener('click', (e) => { e.stopPropagation(); _showPublishBox(wrap); });
  header.appendChild(publishBtn);
  // 点击背景更换
  header.addEventListener('click', () => _changeBackground(header));
  wrap.appendChild(header);

  // 异步加载背景图
  _loadBackground(header);

  // ── 动态流容器 ──
  const feed = h('div', { class: 'moments-feed', id: 'moments-feed' });
  feed.appendChild(h('div', { class: 'moments-loading', text: '加载中…' }));
  wrap.appendChild(feed);

  _loadMoments(feed);
}

/* ─── 背景图加载 / 更换 ─── */
async function _loadBackground(header) {
  const sid = _sid();
  try {
    const cid = (window.Chat && Chat.contact && (Chat.contact.name || Chat.contact.id)) || 'default';
    const res = await fetch(`/api/moments/background?session_id=${encodeURIComponent(sid)}&character_name=${encodeURIComponent(cid)}`);
    const d = await res.json();
    if (d.url) {
      header.style.backgroundImage = `url(${d.url})`;
      header.style.backgroundSize = 'cover';
      header.style.backgroundPosition = 'center';
    }
  } catch (e) {}
}

function _changeBackground(header) {
  const inp = h('input', { type: 'file', accept: 'image/*', style: 'display:none' });
  inp.addEventListener('change', async () => {
    const f = inp.files && inp.files[0];
    if (!f) return;
    try {
      const dataUrl = await _fileToDataUrl(f);
      const res = await fetch('/api/moments/background', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: _sid(), character_name: (window.Chat && Chat.contact && (Chat.contact.name || Chat.contact.id)) || 'default', data: dataUrl }),
      });
      const d = await res.json();
      if (d.error) throw new Error(d.error.message);
      header.style.backgroundImage = `url(${d.url})`;
      header.style.backgroundSize = 'cover';
      header.style.backgroundPosition = 'center';
      toast('背景已更新 ✓');
    } catch (e) { toast('背景上传失败：' + (e.message || e)); }
    inp.remove();
  });
  document.body.appendChild(inp);
  inp.click();
}

/* ─── 加载动态流 ─── */
async function _loadMoments(feed, append) {
  const sid = (typeof Session !== 'undefined' && Session.getSessionId) ? Session.getSessionId() : 'default';
  try {
    const cid = (window.Chat && Chat.contact && (Chat.contact.name || Chat.contact.id)) || 'default';
    const res = await fetch(`/api/moments?session_id=${encodeURIComponent(sid)}&character_name=${encodeURIComponent(cid)}&limit=${_MOMENT_PER}&offset=${_momentPage * _MOMENT_PER}`);
    const data = await res.json();
    const batch = data.moments || [];
    _moments = append ? _moments.concat(batch) : batch;
    if (!append) feed.innerHTML = '';
    if (!_moments.length) {
      feed.appendChild(h('div', { class: 'moments-empty', text: '还没有动态，点右上角「发动态」或和 AI 聊天，让它发第一条朋友圈吧～' }));
      return;
    }
    for (const m of batch) feed.appendChild(_buildMomentCard(m));
    if (batch.length === _MOMENT_PER) {
      const more = h('button', { class: 'btn moment-load-more', text: '加载更多', style: 'display:block;margin:14px auto;' });
      more.addEventListener('click', async () => {
        more.remove();
        _momentPage += 1;
        await _loadMoments(feed, true);
      });
      feed.appendChild(more);
    }
  } catch (e) {
    feed.innerHTML = '';
    feed.appendChild(h('div', { class: 'moments-empty', text: '加载失败：' + (e.message || e) }));
  }
}

/* ─── 单条动态卡片 ─── */
function _buildMomentCard(m) {
  const isAI = m.author_type === 'ai';
  const card = h('div', { class: 'moment-card' });

  // 头像 + 名字 + 文案
  const head = h('div', { class: 'moment-head' });
  const avatar = h('div', {
    class: 'avatar sm moment-avatar',
    style: isAI ? 'background:#141414;color:#fff;' : 'background:#8a8a86;color:#fff;',
    text: isAI ? (m.character_id || 'AI').charAt(0) : '我',
  });
  head.appendChild(avatar);
  const body = h('div', { class: 'moment-body' });
  body.appendChild(h('div', { class: 'moment-name', text: isAI ? (m.character_id || 'AI') : '我' }));
  body.appendChild(h('div', { class: 'moment-content', text: m.content || '' }));
  head.appendChild(body);
  card.appendChild(head);

  // 图片（表情包/图片）
  const imgs = m.images || [];
  if (imgs.length) {
    const imgRow = h('div', { class: 'moment-images' });
    for (const url of imgs) {
      const img = h('img', { src: url, class: 'moment-img', alt: '图片' });
      img.addEventListener('click', () => { if (window._showImageLightbox) _showImageLightbox(url); });
      imgRow.appendChild(img);
    }
    card.appendChild(imgRow);
  }

  // 时间 + 点赞 + 评论
  const meta = h('div', { class: 'moment-meta' });
  meta.appendChild(h('span', { class: 'moment-time', text: (m.created_at || '').slice(5, 16) }));
  card.appendChild(meta);

  // 操作栏
  const actions = h('div', { class: 'moment-actions' });
  const likeBtn = h('button', { class: 'moment-action', text: '❤️ ' + (m.likes || 0) });
  likeBtn.addEventListener('click', async () => {
    try {
      const cid = (window.Chat && Chat.contact && (Chat.contact.name || Chat.contact.id)) || 'default';
      const resp = await fetch(`/api/moments/${m.id}/like`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: _sid(), character_name: cid }) });
      const result = await resp.json();
      if (!resp.ok || result.error) throw new Error(result.error?.message || '点赞失败');
      m.likes = Number(result.likes || 0);
      likeBtn.classList.toggle('liked', result.liked === true);
      likeBtn.textContent = '❤️ ' + m.likes;
    } catch (e) { toast('点赞失败'); }
  });
  actions.appendChild(likeBtn);

  const commentBtn = h('button', { class: 'moment-action', text: '💬 评论' });
  commentBtn.addEventListener('click', () => _showCommentBox(card, m));
  actions.appendChild(commentBtn);
  if (!isAI) {
    const deleteBtn = h('button', { class: 'moment-action', text: '删除' });
    deleteBtn.addEventListener('click', async () => {
      if (!confirm('删除这条动态？')) return;
      const cid = (window.Chat && Chat.contact && (Chat.contact.name || Chat.contact.id)) || 'default';
      const res = await fetch(`/api/moments/${m.id}?session_id=${encodeURIComponent(_sid())}&character_name=${encodeURIComponent(cid)}`, { method: 'DELETE' });
      const d = await res.json().catch(() => ({}));
      if (!res.ok || d.error) return toast(d.error?.message || '删除失败');
      toast('已删除');
      renderMoments();
    });
    actions.appendChild(deleteBtn);
  }
  card.appendChild(actions);

  // 评论列表
  if (m.comments && m.comments.length) {
    const cl = h('div', { class: 'moment-comments' });
    for (const c of m.comments) {
      const isAIc = c.author_type === 'ai';
      cl.appendChild(h('div', { class: 'moment-comment', text: (isAIc ? (m.character_id || 'AI') : '我') + '：' + c.content }));
    }
    card.appendChild(cl);
  }

  return card;
}

/* ─── 发动态 ─── */
function _showPublishBox(wrap) {
  const box = h('div', { class: 'moment-publish' });
  const ta = h('textarea', { class: 'moment-publish-input', placeholder: '这一刻的想法…（可以带 emoji）' });
  const pendingImages = [];   // 已上传的图片 URL

  // 图片选择按钮
  const imgBtn = h('button', { class: 'btn', text: '🖼 图片', style: 'align-self:flex-end;' });
  imgBtn.addEventListener('click', async () => {
    const inp = h('input', { type: 'file', accept: 'image/*', style: 'display:none' });
    inp.addEventListener('change', async () => {
      const f = inp.files && inp.files[0];
      if (!f) return;
      try {
        const dataUrl = await _fileToDataUrl(f);
        const res = await fetch('/api/moments/upload', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ data: dataUrl }),
        });
        const d = await res.json();
        if (d.error) throw new Error(d.error.message);
        pendingImages.push(d.url);
        const preview = h('img', { src: d.url, style: 'width:48px;height:48px;object-fit:cover;border-radius:6px;margin:4px;' });
        box.insertBefore(preview, box.querySelector('.moment-publish-btns'));
        toast('图片已添加');
      } catch (e) { toast('图片上传失败：' + (e.message || e)); }
      inp.remove();
    });
    document.body.appendChild(inp);
    inp.click();
  });

  const sendBtn = h('button', { class: 'btn btn-primary', text: '发表', style: 'align-self:flex-end;' });
  sendBtn.addEventListener('click', async () => {
    const content = ta.value.trim();
    if (!content) { toast('写点什么吧'); return; }
    const cid = (window.Chat && Chat.contact && Chat.contact.name) || 'default';
    try {
      const res = await fetch('/api/moments', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: _sid(), character_name: cid, content, images: pendingImages }),
      });
      const d = await res.json();
      if (d.error) throw new Error(d.error.message);
      toast('已发布 ✓');
      renderMoments();
      // AI 反应在后台生成，稍后刷新一次即可看到点赞和评论。
      if (d.ai_reaction_pending) setTimeout(() => renderMoments(), 2500);
    } catch (e) { toast('发布失败：' + (e.message || e)); }
  });

  const btns = h('div', { class: 'moment-publish-btns', style: 'display:flex;gap:6px;align-self:flex-end;' });
  btns.appendChild(imgBtn);
  btns.appendChild(sendBtn);
  box.appendChild(ta);
  box.appendChild(btns);
  wrap.insertBefore(box, wrap.querySelector('.moments-feed'));
}

/* ─── 评论 ─── */
function _showCommentBox(card, m) {
  const existing = card.querySelector('.moment-comment-box');
  if (existing) { existing.remove(); return; }
  const box = h('div', { class: 'moment-comment-box' });
  const inp = h('input', { class: 'moment-comment-input', placeholder: '评论…' });
  const btn = h('button', { class: 'btn', text: '发送' });
  btn.addEventListener('click', async () => {
    const content = inp.value.trim();
    if (!content) return;
    try {
      const res = await fetch(`/api/moments/${m.id}/comment`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, session_id: _sid(), character_name: (window.Chat && Chat.contact && (Chat.contact.name || Chat.contact.id)) || 'default' }),
      });
      const d = await res.json();
      if (d.error) throw new Error(d.error.message);
      toast(d.ai_reply ? 'AI 已回复你的评论' : '已评论');
      renderMoments();
    } catch (e) { toast('评论失败'); }
  });
  box.appendChild(inp);
  box.appendChild(btn);
  card.appendChild(box);
  inp.focus();
}

/* ─── 工具 ─── */
function _sid() {
  return (typeof Session !== 'undefined' && Session.getSessionId) ? Session.getSessionId() : 'default';
}

function _fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}
