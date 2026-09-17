/*
 * prompt_audit.js —「她现在被哪些规则管着」面板（只读）
 *
 * 为什么有它（用户 2026-09-16 原话）：
 *   「我感觉模型是不是被限制，我让她承认爱我都做不到一直再绕圈子」
 *   真因查出来是角色卡的语言风格预设往她 prompt 里写死了一行
 *   「绝对不用这些词：喜欢你/我爱你/当然/没问题」。
 *   用户要求「做到 App 里能查」—— 于是这里给一个随时可打开的清单，
 *   不用再去翻代码猜"她被什么管着"。
 *
 * 做法：右下角一个常驻小按钮「她现在的规则」→ 点开面板 → fetch
 *   GET /api/pc/prompt/audit?character=<当前角色>
 * 后端 backend/prompt_audit.py 只读组装（不写库、不调模型）。
 *
 * 为什么单独一个文件：App 里其它页面（设置/资料/日志）当时正被另一个会话改着，
 * 单独一个文件 + index.html 一行 <script> 最不容易冲突；不需要时删掉这一行即可。
 */
;(function (window, document) {
  'use strict'

  var API = '/api/pc/prompt/audit'
  var BTN_ID = 'hz-prompt-audit-btn'
  var PANEL_ID = 'hz-prompt-audit-panel'

  function currentCharacter() {
    // 尽量拿"当前正在聊的角色名"：不同版本的前端把当前联系人放在不同地方，逐个兜底。
    try {
      if (window.Store && typeof window.Store.getContacts === 'function') {
        var cs = window.Store.getContacts() || {}
        var id = window.Store.currentContactId || window.currentContactId
        if (id && cs[id]) return cs[id].name || id
      }
    } catch (_) {}
    try {
      if (window.currentContact && window.currentContact.name) return window.currentContact.name
    } catch (_) {}
    try {
      var raw = localStorage.getItem('aiwechat:currentContact') || localStorage.getItem('currentContact')
      if (raw) {
        var j = JSON.parse(raw)
        if (typeof j === 'string') return j
        if (j && j.name) return j.name
      }
    } catch (_) {}
    return ''
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  }

  function kv(obj) {
    var rows = ''
    Object.keys(obj || {}).forEach(function (k) {
      var v = obj[k]
      if (v && typeof v === 'object' && !Array.isArray(v)) {
        rows += '<div class="hz-pa-row"><div class="hz-pa-k">' + esc(k) + '</div><div class="hz-pa-v">' +
          esc(Object.keys(v).map(function (kk) { return kk + '：' + v[kk] }).join('；')) + '</div></div>'
      } else {
        rows += '<div class="hz-pa-row"><div class="hz-pa-k">' + esc(k) + '</div><div class="hz-pa-v">' +
          esc(Array.isArray(v) ? (v.length ? v.join('、') : '（无）') : v) + '</div></div>'
      }
    })
    return rows || '<div class="hz-pa-empty">（无）</div>'
  }

  function section(title, inner, sub) {
    return '<div class="hz-pa-sec"><div class="hz-pa-title">' + esc(title) +
      (sub ? '<span class="hz-pa-sub">' + esc(sub) + '</span>' : '') + '</div>' + inner + '</div>'
  }

  function render(data) {
    var rules = (data.rules || []).map(function (r) {
      return '<div class="hz-pa-rule"><span class="hz-pa-no">' + r.no + '</span>' + esc(r.text) + '</div>'
    }).join('')
    var style = data.style || {}
    var styleInner = kv({
      '预设': (style.label ? style.label + '（' + style.preset + '）' : style.preset),
      '禁用词': (style.forbidden_words && style.forbidden_words.length) ? style.forbidden_words : '（无 —— 2026-09-16 已清空）',
      '场景策略': style.scene_strategy,
      '标志句式': style.signature_patterns,
      '口头禅': style.catchphrases,
      '否认词': style.denial_words,
      '偏好长度': style.prefer_length
    })
    var blocks = (data.blocks || []).map(function (b) {
      return '<div class="hz-pa-row"><div class="hz-pa-k">' + esc(b.name) + '</div><div class="hz-pa-v">' +
        esc(b.chars + ' 字') + '</div></div>'
    }).join('')
    var removed = (data.removed || []).map(function (x) { return '<div class="hz-pa-rule">✂ ' + esc(x) + '</div>' }).join('')

    return '' +
      '<div class="hz-pa-head">' +
        '<div class="hz-pa-h1">她现在的规则<b>' + esc(data.character) + '</b></div>' +
        '<div class="hz-pa-meta">prompt 共 ' + esc(data.prompt_chars) + ' 字 ｜ ' +
          esc((data.rules || []).length) + ' 条全局规范 ｜ 生成于 ' + esc(data.generated_at) + '</div>' +
        '<button class="hz-pa-x" id="hz-pa-close" title="关闭">✕</button>' +
      '</div>' +
      '<div class="hz-pa-body">' +
        section('影响她的开关', kv(data.switches)) +
        section('语言风格预设（真实生效项）', styleInner, style.label || '') +
        section('注入块（她被多少东西管着）', blocks, '共 ' + (data.blocks || []).length + ' 块') +
        section('全局底层交互规范', rules, '共 ' + (data.rules || []).length + ' 条') +
        section('2026-09-16 已经删掉的限制', removed) +
        '<div class="hz-pa-note">' + esc(data.note || '') + '</div>' +
      '</div>'
  }

  function ensureCss() {
    if (document.getElementById('hz-pa-css')) return
    var s = document.createElement('style')
    s.id = 'hz-pa-css'
    s.textContent = [
      '#' + PANEL_ID + '{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:99990;display:flex;',
      'align-items:center;justify-content:center;padding:24px;}',
      '#' + PANEL_ID + ' .hz-pa-box{background:#161614;border:1px solid rgba(255,255,255,.12);border-radius:18px;',
      'width:min(760px,94vw);max-height:86vh;display:flex;flex-direction:column;overflow:hidden;',
      "font-family:inherit;color:#EDEDEA;box-shadow:0 30px 90px rgba(0,0,0,.6);}",
      '#' + PANEL_ID + ' .hz-pa-head{padding:18px 22px 12px;border-bottom:1px solid rgba(255,255,255,.08);position:relative;}',
      '#' + PANEL_ID + ' .hz-pa-h1{font-size:16px;font-weight:600;}',
      '#' + PANEL_ID + ' .hz-pa-h1 b{color:#8FD6A8;margin-left:6px;}',
      '#' + PANEL_ID + ' .hz-pa-meta{font-size:12px;color:#9A9A94;margin-top:6px;}',
      '#' + PANEL_ID + ' .hz-pa-x{position:absolute;right:14px;top:14px;background:transparent;border:none;',
      'color:#9A9A94;font-size:16px;cursor:pointer;}',
      '#' + PANEL_ID + ' .hz-pa-body{padding:8px 22px 22px;overflow:auto;}',
      '#' + PANEL_ID + ' .hz-pa-sec{margin-top:16px;}',
      '#' + PANEL_ID + ' .hz-pa-title{font-size:13px;color:#C9C9C3;margin-bottom:8px;letter-spacing:.02em;}',
      '#' + PANEL_ID + ' .hz-pa-sub{font-size:11px;color:#7F7F79;margin-left:8px;}',
      '#' + PANEL_ID + ' .hz-pa-row{display:flex;gap:10px;padding:5px 0;font-size:13px;line-height:1.6;}',
      '#' + PANEL_ID + ' .hz-pa-k{flex:0 0 44%;color:#9A9A94;}',
      '#' + PANEL_ID + ' .hz-pa-v{flex:1;color:#E7E7E2;word-break:break-word;}',
      '#' + PANEL_ID + ' .hz-pa-rule{font-size:12.5px;line-height:1.72;color:#DDDDD8;padding:4px 0;',
      'border-bottom:1px dashed rgba(255,255,255,.06);}',
      '#' + PANEL_ID + ' .hz-pa-no{display:inline-block;min-width:22px;color:#8FD6A8;}',
      '#' + PANEL_ID + ' .hz-pa-empty,#' + PANEL_ID + ' .hz-pa-note{font-size:12px;color:#8A8A84;}',
      '#' + PANEL_ID + ' .hz-pa-note{margin-top:18px;line-height:1.7;}',
      '#' + BTN_ID + '{position:fixed;right:18px;bottom:18px;z-index:99980;background:rgba(28,28,26,.92);',
      'color:#D8D8D2;border:1px solid rgba(255,255,255,.14);border-radius:999px;padding:9px 16px;font-size:12.5px;',
      'cursor:pointer;backdrop-filter:blur(6px);opacity:.72;}',
      '#' + BTN_ID + ':hover{opacity:1;}'
    ].join('')
    document.head.appendChild(s)
  }

  function close() {
    var p = document.getElementById(PANEL_ID)
    if (p && p.parentNode) p.parentNode.removeChild(p)
  }

  function open() {
    close()
    ensureCss()
    var ch = currentCharacter()
    var url = API + (ch ? ('?character=' + encodeURIComponent(ch)) : '')
    var wrap = document.createElement('div')
    wrap.id = PANEL_ID
    wrap.innerHTML = '<div class="hz-pa-box"><div class="hz-pa-head"><div class="hz-pa-h1">她现在的规则</div>' +
      '<div class="hz-pa-meta">读取中…</div></div></div>'
    wrap.addEventListener('click', function (e) { if (e.target === wrap) close() })
    document.body.appendChild(wrap)
    fetch(url).then(function (r) { return r.json() }).then(function (data) {
      var box = wrap.querySelector('.hz-pa-box')
      box.innerHTML = render(data || {})
      var x = document.getElementById('hz-pa-close')
      if (x) x.addEventListener('click', close)
    }).catch(function (e) {
      var box = wrap.querySelector('.hz-pa-box')
      box.innerHTML = '<div class="hz-pa-head"><div class="hz-pa-h1">读取失败</div>' +
        '<div class="hz-pa-meta">' + esc(e && e.message) + '</div></div>'
    })
  }

  function mountButton() {
    if (document.getElementById(BTN_ID) || !document.body) return
    ensureCss()
    var b = document.createElement('button')
    b.id = BTN_ID
    b.type = 'button'
    b.title = '看看她现在被哪些规则管着（只读）'
    b.textContent = '她现在的规则'
    b.addEventListener('click', open)
    document.body.appendChild(b)
  }

  window.PromptAudit = { open: open, close: close, currentCharacter: currentCharacter,
                         api: API, _render: render }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountButton)
  } else {
    mountButton()
  }
}(window, document))
