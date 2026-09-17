'use strict';
/* ============================================================
   聊天列表页（微信 Tab）
   ============================================================ */

function chatPreviewText(conv, msgs) {
  const last = msgs.length ? msgs[msgs.length - 1] : null;
  if (!last) return '';
  if (last.status === 'streaming') return '对方正在输入...';
  if (last.status === 'error') return '[出错了]';
  if (last.type === 'sticker') return '[表情包]';
  return (last.role === 'user' ? '' : '') + last.content;
}

function renderChatList() {
  const wrap = $('#overview-chats');
  if (!wrap) return;
  wrap.innerHTML = '';
  const items = Store.listConversations();

  if (!items.length) {
    wrap.appendChild(h('div', { class: 'empty-state', style: 'padding:30px 20px' },
      emptyIcon('chat'),
      h('div', { text: '还没有聊天记录' }),
      h('div', { style: 'margin-top:8px', text: '去「人格」创建一个 AI 伴侣开始聊天吧' }),
    ));
    return;
  }

  const group = h('div', { class: 'list-group' });
  for (const { cv, contact } of items) {
    const msgs = Store.getMessages(cv.id);
    const preview = chatPreviewText(cv, msgs);
    const row = h('div', { class: 'row chat-item' },
      avatarEl(contact),
      h('div', { class: 'chat-info' },
        h('div', { class: 'chat-name', text: contact.name }),
        h('div', { class: 'chat-preview', text: preview }),
      ),
      h('div', { class: 'chat-time', text: fmtListTime(cv.updatedAt) }),
      h('div', { class: 'badge' + (cv.unread ? ' show' : ''), text: cv.unread > 99 ? '99+' : String(cv.unread) }),
    );
    row.addEventListener('click', () => Chat.open(contact.id));
    group.appendChild(row);
  }
  wrap.appendChild(group);
}
