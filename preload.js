'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('desktopWin', {
  isDesktop: true,

  isFocused: () => ipcRenderer.sendSync('win-is-focused'),

  minimize:  () => ipcRenderer.send('win-min'),
  toggleMax: () => ipcRenderer.send('win-max'),
  close:     () => ipcRenderer.send('win-close'),

  showNotify: (data) => ipcRenderer.send('show-notify', data),

  // 旧接口保留
  onNotifyReply: (cb) =>
    ipcRenderer.on('notify-reply', (e, data) => cb(data)),

  onNotifyOpen: (cb) =>
    ipcRenderer.on('notify-open', (e, data) => cb(data)),

  // ===== 新增：通知窗快捷回复 =====
  onNotifyQuickReply: (cb) =>
    ipcRenderer.on('notify-quick-reply', (e, data) => cb(data)),

  sendNotifyQuickReplyResult: (data) =>
    ipcRenderer.send('notify-quick-reply-result', data),

  getNotifyConfig: () =>
    ipcRenderer.invoke('get-notify-config'),

  setNotifyConfig: (cfg) =>
    ipcRenderer.invoke('set-notify-config', cfg),

  onWindowState: (cb) =>
    ipcRenderer.on('window-state', (e, state) => cb(state)),

  _sendDebug: (msg) =>
    ipcRenderer.send('debug-log', msg),

  // ===== 独立聊天窗口 =====
  openChatWindow: (opts) => ipcRenderer.invoke('chatwin:open', opts || {}),
  closeChatWindow: () => ipcRenderer.invoke('chatwin:close'),
  isChatWindowOpen: () => ipcRenderer.invoke('chatwin:is-open'),
  minimizeChatWindow: () => ipcRenderer.send('chatwin:minimize'),
  onChatWindowContact: (cb) => ipcRenderer.on('chatwin-contact', (e, d) => cb(d))
})
