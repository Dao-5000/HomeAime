/**
 * chat_stream.js v2.1
 * 分段气泡 SSE 客户端，挂载到 window.ChatStream 供 chat.js 调用。
 */
;(function (window, document) {
  'use strict'

  var BASE_URL = ''

  var EMOTION_STYLES = {
    happy:       { border: '#FFD700', icon: '😊' },
    excited:     { border: '#FF6B6B', icon: '✨' },
    tender:      { border: '#FFB6C1', icon: '🌸' },
    playful:     { border: '#87CEEB', icon: '😜' },
    calm:        { border: '#B0C4DE', icon: '🌿' },
    worried:     { border: '#DDA0DD', icon: '🥺' },
    sad:         { border: '#778899', icon: '😢' },
    upset:       { border: '#BC8F8F', icon: '😞' },
    angry:       { border: '#DC143C', icon: '😠' },
    cold:        { border: '#708090', icon: '❄️' },
    reconciling: { border: '#98FB98', icon: '🤝' },
    loving:      { border: '#FF69B4', icon: '💕' },
  }

  function sendMessage(opts) {
    opts = opts || {}
    var sessionId   = opts.sessionId   || 'default'
    var characterId = opts.characterId || 'default'
    var characterName = opts.characterName || characterId
    var message     = (opts.message || '').trim()
    // 高好感分段入口也必须携带完整历史；只传 message 会让后端看不到上一轮，
    // 进而重复回答旧话题。调用方传入的数组已包含 system + 最近消息 + 当前用户句。
    var messages    = Array.isArray(opts.messages) && opts.messages.length
      ? opts.messages
      : [{ role: 'user', content: message }]
    var sticker     = !!opts.sticker
    // ★ 前端配置的 API Key。后端 config.api_key() 在打包运行时常为空
    //   （DATA_DIR 被 AI_COMPANION_DATA_DIR 重定向到 userData），
    //   而语义分析这类后端自调用模块正是用 config.api_key()，
    //   拿不到 key 就会静默降级成默认状态，等于意图分析从未生效。
    var key         = opts.key     || ''
    // ★ 单角色大脑：人格设置里配的模型/接口地址，透传给后端让理解层+生成层都跟随
    var model       = opts.model   || ''
    var baseUrl     = opts.baseUrl || ''
    var replyTo     = opts.replyTo || null
    var onTyping    = opts.onTyping  || function () {}
    var onTurn      = opts.onTurn    || function () {}
    var onDone      = opts.onDone    || function () {}
    var onError     = opts.onError   || function () {}
    var onPending   = opts.onPending || function () {}
    var onThinking  = opts.onThinking || function () {}
    var onAgentEvent = opts.onAgentEvent || function () {}
    var timeoutMs   = Math.max(10000, Number(opts.timeoutMs) || 90000)

    if (!message) {
      onError('消息不能为空')
      return Promise.resolve()
    }

    // SSE can finish via a done event, EOF, or an error. Notify chat.js once.
    var settled = false
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null
    var timeoutId = setTimeout(function () {
      if (controller) controller.abort()
      errorOnce('AI 回复超时，请稍后重试')
    }, timeoutMs)
    function doneOnce(emotion) {
      if (settled) return
      settled = true
      clearTimeout(timeoutId)
      onDone(emotion || 'calm')
    }
    function errorOnce(msg) {
      if (settled) return
      settled = true
      clearTimeout(timeoutId)
      onError(msg || '网络连接失败，请稍后重试')
    }

    return fetch(BASE_URL + '/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: controller ? controller.signal : undefined,
      body: JSON.stringify({
        session_id: sessionId,
        character_id: characterId,
        character_name: characterName,
        message: message,
        messages: messages,
        sticker: sticker,
        key: key,
        model: model,
        baseUrl: baseUrl,
        reply_to: replyTo,
        // 语义是「本入口支持语音」，不是「每条都要配音」。
        // 后端会按 should_send_voice() 的概率 + 场景 + 用户偏好决定配不配，
        // 并且一轮最多 2 条；用户说「你打字吧/别发语音」后这里会被识别为
        // 文字偏好，后端就不再配音。
        voice_reply: true,
      }),
    })
      .then(function (res) {
        if (!res.ok) {
          return res.text().then(function (raw) {
            var msg = 'HTTP ' + res.status
            try {
              var parsed = JSON.parse(raw)
              if (parsed && parsed.error) {
                msg = parsed.error.message || parsed.error || msg
              } else if (parsed && parsed.detail) {
                msg = parsed.detail
              }
            } catch (_) {
              if (raw && raw.trim()) msg += '：' + raw.trim().slice(0, 200)
            }
            throw new Error(msg)
          })
        }
        if (!res.body) throw new Error('当前浏览器不支持流式回复，请升级浏览器')
        return readSSE(res.body, onTyping, onTurn, doneOnce, errorOnce, onPending, onThinking)
      })
      .catch(function (err) {
        errorOnce((err && err.message) ? err.message : '网络连接失败，请检查服务和 API Key')
      })
  }

  function readSSE(body, onTyping, onTurn, onDone, onError, onPending, onThinking) {
    var reader = body.getReader()
    var decoder = new TextDecoder('utf-8')
    var buffer = ''
    var completed = false

    function finish(emotion) {
      if (completed) return
      completed = true
      try { reader.cancel() } catch (_) {}
      onDone(emotion || 'calm')
    }

    function fail(msg) {
      if (completed) return
      completed = true
      onError(msg || '网络连接失败，请稍后重试')
    }

    function processLine(source) {
      var line = String(source || '').trim()
      if (!line.startsWith('data:')) return
      var raw = line.slice(5).trim()
      if (!raw) return

      if (raw === '[DONE]') {
        finish('calm')
        return
      }

      var data
      try {
        data = JSON.parse(raw)
      } catch (_) {
        // 忽略不完整或非 JSON 的 SSE 行，不影响后续事件。
        return
      }
      // Callback exceptions must reach pump().catch(), otherwise a render error
      // is silently swallowed and the user sees no reply.
      if (data.type === 'typing') onTyping()
      else if (data.type && String(data.type).indexOf('agent_') === 0) onAgentEvent(data)
      else if (data.type === 'thinking') onThinking(data)
      else if (data.type === 'pending') onPending(data)
      else if (data.type === 'message') onTurn(data)
      else if (data.type === 'done') finish(data.emotion || 'calm')
      else if (data.type === 'error') fail(data.content || '服务端返回错误')
    }

    function pump() {
      return reader.read().then(function (result) {
        if (completed) return
        if (result.done) {
          buffer += decoder.decode()
          if (buffer.trim()) processLine(buffer)
          // 即使后端漏发 done，正常 EOF 也必须释放聊天发送状态。
          if (!completed) finish('calm')
          return
        }

        buffer += decoder.decode(result.value, { stream: true })
        var lines = buffer.split(/\r?\n/)
        buffer = lines.pop() || ''
        for (var i = 0; i < lines.length && !completed; i++) {
          processLine(lines[i])
        }
        if (!completed) return pump()
      }).catch(function (err) {
        fail('连接中断：' + ((err && err.message) || 'network error'))
      })
    }

    return pump()
  }

  function createBubble(turnData, role) {
    turnData = turnData || {}
    var wrap = document.createElement('div')
    wrap.className = 'chat-msg-wrap ' + (role === 'user' ? 'msg-user' : 'msg-ai')

    var bubble = document.createElement('div')
    bubble.className = 'chat-bubble stream-bubble-in'
    bubble.textContent = turnData.content || ''

    var style = EMOTION_STYLES[turnData.emotion] || EMOTION_STYLES.calm
    bubble.style.borderLeftColor = style.border
    bubble.dataset.emotion = turnData.emotion || 'calm'

    if (turnData.audio) {
      try {
        var audio = new Audio(turnData.audio)
        audio.play().catch(function () {})
      } catch (_) {}
    }

    wrap.appendChild(bubble)
    setTimeout(function () { bubble.classList.remove('stream-bubble-in') }, 320)
    return wrap
  }

  function createUserBubble(text) {
    var wrap = document.createElement('div')
    wrap.className = 'chat-msg-wrap msg-user'
    var bubble = document.createElement('div')
    bubble.className = 'chat-bubble stream-bubble-in'
    bubble.textContent = text
    wrap.appendChild(bubble)
    setTimeout(function () { bubble.classList.remove('stream-bubble-in') }, 320)
    return wrap
  }

  function createTypingIndicator() {
    var wrap = document.createElement('div')
    wrap.id = 'stream-typing-indicator'
    wrap.className = 'chat-msg-wrap msg-ai'
    var bubble = document.createElement('div')
    bubble.className = 'chat-bubble typing-dots'
    bubble.innerHTML = '<span></span><span></span><span></span>'
    wrap.appendChild(bubble)
    return wrap
  }

  function removeTypingIndicator() {
    var el = document.getElementById('stream-typing-indicator')
    if (el && el.parentNode) el.parentNode.removeChild(el)
  }

  window.ChatStream = {
    sendMessage: sendMessage,
    createBubble: createBubble,
    createUserBubble: createUserBubble,
    createTypingIndicator: createTypingIndicator,
    removeTypingIndicator: removeTypingIndicator,
    EMOTION_STYLES: EMOTION_STYLES,
  }
}(window, document))
