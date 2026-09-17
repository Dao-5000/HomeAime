'use strict';
/**
 * AI 微信 · 本地服务器（零依赖，仅需 Node.js 18+）
 *
 * 职责：
 *  1. 托管 public/ 目录下的网页（微信风格的 AI 聊天界面）
 *  2. 代理 DeepSeek API（文本，流式）
 *  3. 消息里带图片时，自动路由到视觉模型（通义千问 qwen-vl，流式）
 *
 * 启动：node server.js
 * 手机访问：手机和电脑连同一个 Wi-Fi，Safari 打开终端里显示的 http://<电脑IP>:3000
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');

const ROOT = __dirname;
const PUBLIC = path.join(ROOT, 'public');
const LIB_DIR = path.join(ROOT, '记忆库');
const PORT = Number(process.env.PORT || 3000);
const DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions';
const DASHSCOPE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions';

/** 记忆库文件名白名单：只允许安全的 .txt/.md 文件名 */
function safeLibName(name) {
  let n = String(name || '').trim().replace(/^\.+/, '');
  n = n.replace(/[\\/]/g, '_').replace(/[^\w\u4e00-\u9fa5.\- ]/g, '_');
  if (!/\.(txt|md)$/i.test(n)) n += '.txt';
  return n;
}

/**
 * 直接读取「记忆库/」文件夹，把里面的 .txt/.md 记忆文本拼成一段系统提示词。
 * 这样用户在磁盘记忆库界面粘贴/放入的文本，聊天时 AI 能直接读到，无需手动"导入"。
 */
function readLibraryMemoryBlock() {
  let files = [];
  try {
    files = fs.readdirSync(LIB_DIR).filter((f) => /\.(txt|md)$/i.test(f));
  } catch (_) {
    return '';
  }
  if (!files.length) return '';
  const MAX_FILE = 6000;   // 单文件上限，防止超大文件撑爆上下文
  const MAX_TOTAL = 14000; // 记忆总字数上限
  const parts = [];
  let total = 0;
  for (const f of files.slice(0, 20)) {
    try {
      const content = fs.readFileSync(path.join(LIB_DIR, f), 'utf8').slice(0, MAX_FILE);
      if (!content.trim()) continue;
      const title = f.replace(/\.(txt|md)$/i, '');
      parts.push(title + '：\n' + content.trim());
      total += content.length;
      if (total >= MAX_TOTAL) break;
    } catch (_) { /* 跳过读不了的文件 */ }
  }
  if (!parts.length) return '';
  return '【磁盘记忆库（项目「记忆库」文件夹里的记忆文本，聊天中自然运用，不要生硬复述）】\n' + parts.join('\n\n');
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
  '.ico': 'image/x-icon',
};

/* ---------- 配置读取 ---------- */
function readConfig() {
  try {
    return JSON.parse(fs.readFileSync(path.join(ROOT, 'config.json'), 'utf8'));
  } catch (_) {
    return {};
  }
}

function readServerKey() {
  if (process.env.DEEPSEEK_API_KEY && process.env.DEEPSEEK_API_KEY.trim()) {
    return process.env.DEEPSEEK_API_KEY.trim();
  }
  const k = readConfig().apiKey;
  return (typeof k === 'string' && k.trim()) ? k.trim() : '';
}

function readVisionKey() {
  if (process.env.DASHSCOPE_API_KEY && process.env.DASHSCOPE_API_KEY.trim()) {
    return process.env.DASHSCOPE_API_KEY.trim();
  }
  const k = readConfig().visionApiKey;
  return (typeof k === 'string' && k.trim()) ? k.trim() : '';
}

function readVisionModel() {
  const m = readConfig().visionModel;
  return (typeof m === 'string' && m.trim()) ? m.trim() : 'qwen-vl-max';
}

/* ---------- 工具 ---------- */
function getLanIps() {
  const ips = [];
  // 极端环境（网络栈异常）下 os.networkInterfaces() 可能抛异常，不能让它变成未捕获异常崩掉服务器
  let interfaces;
  try {
    interfaces = os.networkInterfaces();
  } catch (_) {
    return ips;
  }
  for (const nets of Object.values(interfaces)) {
    for (const net of nets || []) {
      if (net.family === 'IPv4' && !net.internal) ips.push(net.address);
    }
  }
  return ips;
}

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  return Buffer.concat(chunks).toString('utf8');
}

/** 把上游 SSE 流原样转发给浏览器，同时检测 finish_reason / [DONE] 标记
 *  在流末尾追加一条 __meta__ 行通知前端是否被截断（用于 UI 提示和"续写"按钮）。 */
async function relayStream(res, upstream) {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  let sawDone = false;
  let lastFinishReason = null;
  let sawAnyData = false;
  // 简易缓冲：把已写入响应的 chunk 也保留一份用于解析 finish_reason
  // 注意：Node 18+ fetch().body 的 ReadableStream 在 for await 时产出的是 Web 标准 Uint8Array
  // （不是 Node Buffer），所以不能用 chunk.toString('utf8')（会按 char code 输出逗号分隔的数字），
  // 必须用 TextDecoder 显式按 UTF-8 解码
  const decoder = new TextDecoder('utf-8');
  let parseBuf = '';
  for await (const chunk of upstream.body) {
    res.write(chunk);
    const added = typeof chunk === 'string' ? chunk : decoder.decode(chunk);
    parseBuf += added;
    sawAnyData = true;
    // 完整行才解析（避免误判跨 chunk 的不完整 JSON）
    let nl;
    while ((nl = parseBuf.indexOf('\n')) !== -1) {
      const line = parseBuf.slice(0, nl);
      parseBuf = parseBuf.slice(nl + 1);
      if (!line.startsWith('data:')) continue;
      const payload = line.slice(5).trim();
      if (payload === '[DONE]') { sawDone = true; continue; }
      // 尝试解析，失败说明跨 chunk 截断 → 跳过
      try {
        const j = JSON.parse(payload);
        const c0 = j.choices && j.choices[0];
        if (c0 && c0.finish_reason) lastFinishReason = c0.finish_reason;
      } catch (_) { /* 跨 chunk 切到 JSON 中间，留给下一 chunk */ }
    }
  }
  // 流结束后解析残留缓冲（最后一行可能缺换行符）
  if (parseBuf.trim()) {
    const tail = parseBuf.trim();
    if (tail.startsWith('data:') && tail.slice(5).trim() !== '[DONE]') {
      try {
        const j = JSON.parse(tail.slice(5).trim());
        const c0 = j.choices && j.choices[0];
        if (c0 && c0.finish_reason) lastFinishReason = c0.finish_reason;
      } catch (_) { /* 忽略 */ }
    }
  }
  // 把诊断信息追加到客户端（不是 SSE 标准事件，是项目自定义）
  const meta = {
    _meta: true,
    sawDone,                         // 是否真的看到 [DONE] 标记
    sawAnyData,                      // 上游是否真的有任何响应
    finish_reason: lastFinishReason,  // 上游最终结束原因：stop=自然 / length=被长度限制截断 / content_filter=被安全过滤
  };
  res.write('data: ' + JSON.stringify(meta) + '\n\n');
  res.end();
}

/* ---------- 服务器 ---------- */
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');

  // ---- API：服务器配置状态（供设置页提示） ----
  if (req.method === 'GET' && url.pathname === '/api/config') {
    return sendJson(res, 200, {
      serverKey: !!readServerKey(),
      visionKey: !!readVisionKey(),
    });
  }

  // ---- API：聊天代理（文本走 DeepSeek，图片走视觉模型） ----
  if (req.method === 'POST' && url.pathname === '/api/chat') {
    let body;
    try {
      body = JSON.parse(await readBody(req));
    } catch (_) {
      return sendJson(res, 400, { error: { message: '请求格式错误' } });
    }

    const model = typeof body.model === 'string' && body.model ? body.model : 'deepseek-chat';
    const messages = Array.isArray(body.messages) ? body.messages : [];
    if (!messages.length) return sendJson(res, 400, { error: { message: '没有消息内容' } });
    if (messages.length > 40) return sendJson(res, 400, { error: { message: '消息条数超出限制' } });

    // 直接读取磁盘「记忆库/」文件夹，注入到系统提示词（用户在此界面放入的记忆文本，聊天中 AI 直接可读）
    const libBlock = readLibraryMemoryBlock();
    if (libBlock) {
      let injected = false;
      for (let i = 0; i < messages.length; i++) {
        if (messages[i] && messages[i].role === 'system') {
          messages[i].content = (messages[i].content || '') + '\n\n' + libBlock;
          injected = true;
          break;
        }
      }
      if (!injected) messages.unshift({ role: 'system', content: libBlock });
    }

    const hasImage = messages.some((m) => m && m.image);

    let endpoint, authKey, payload;

    if (hasImage) {
      // ---- 视觉模型路由（通义千问 qwen-vl） ----
      const vkey = (typeof body.visionKey === 'string' && body.visionKey.trim())
        ? body.visionKey.trim() : readVisionKey();
      if (!vkey) {
        return sendJson(res, 400, {
          error: {
            message: '图片消息需要视觉模型 Key：请在 App 设置页「视觉模型」填写（通义千问 DashScope），或在电脑 config.json 配置 visionApiKey',
          },
        });
      }
      const vmodel = (typeof body.visionModel === 'string' && body.visionModel.trim())
        ? body.visionModel.trim() : readVisionModel();
      endpoint = DASHSCOPE_URL;
      authKey = vkey;
      payload = {
        model: vmodel,
        messages: messages.map((m) => m.image
          ? {
              role: m.role,
              content: [
                { type: 'text', text: m.content || '请描述这张图片并回复我' },
                { type: 'image_url', image_url: { url: m.image } },
              ],
            }
          : { role: m.role, content: m.content }),
        stream: true,
        temperature: 0.7,
      };
    } else {
      // ---- 文本路由（OpenAI 兼容接口，默认 DeepSeek；可用 body.baseUrl 指定其他服务商） ----
      const key = (typeof body.key === 'string' && body.key.trim())
        ? body.key.trim() : readServerKey();
      if (!key) {
        return sendJson(res, 401, {
          error: { message: '尚未配置 API Key：请在 App 设置页填写，或在电脑的 config.json 中填写后重启服务器' },
        });
      }
      const baseUrl = (typeof body.baseUrl === 'string' && body.baseUrl.trim())
        ? body.baseUrl.trim().replace(/\/+$/, '') : DEEPSEEK_URL;
      endpoint = baseUrl + '/chat/completions';
      authKey = key;
      // 透传 body 里的 temperature / top_p（允许主动消息微调），
      // 范围限制在 [0, 2] / [0, 1] 防止客户端乱传；默认 0.7 比 0.8 更稳定，减少"提前停"
      const reqTemp = Number(body.temperature);
      const reqTopP = Number(body.top_p);
      const reqMaxTokens = Number(body.max_tokens);
      payload = {
        model, messages, stream: true,
        temperature: (isFinite(reqTemp) && reqTemp >= 0 && reqTemp <= 2) ? reqTemp : 0.7,
        top_p: (isFinite(reqTopP) && reqTopP >= 0 && reqTopP <= 1) ? reqTopP : 1,
        // 显式给足输出空间，避免某些情况下模型因输出预算不足而提前收尾
        max_tokens: (isFinite(reqMaxTokens) && reqMaxTokens > 0 && reqMaxTokens <= 8192) ? reqMaxTokens : 1024,
      };
    }

    try {
      const upstream = await fetch(endpoint, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + authKey,
        },
        body: JSON.stringify(payload),
      });

      if (!upstream.ok) {
        let detail = '';
        try {
          const j = await upstream.json();
          detail = (j && j.error && j.error.message) ? j.error.message : JSON.stringify(j);
        } catch (_) {
          detail = await upstream.text();
        }
        return sendJson(res, upstream.status, {
          error: { message: '模型返回错误（' + upstream.status + '）：' + detail },
        });
      }

      return relayStream(res, upstream);
    } catch (err) {
      return sendJson(res, 502, { error: { message: '无法连接模型服务：' + (err.message || err) } });
    }
  }

  // ---- 磁盘记忆库（项目根目录 /记忆库 文件夹） ----
  if (url.pathname === '/api/library' || url.pathname === '/api/library/write' || url.pathname === '/api/library/delete') {
    if (req.method === 'GET' && url.pathname === '/api/library') {
      let files = [];
      try {
        files = fs.readdirSync(LIB_DIR)
          .filter((f) => /\.(txt|md)$/i.test(f))
          .map((f) => {
            const st = fs.statSync(path.join(LIB_DIR, f));
            return { name: f, size: st.size, mtime: st.mtimeMs };
          })
          .sort((a, b) => b.mtime - a.mtime);
      } catch (_) { /* 文件夹不存在时返回空列表 */ }
      return sendJson(res, 200, { dir: LIB_DIR, files });
    }
    if (req.method === 'POST') {
      let body;
      try { body = JSON.parse(await readBody(req)); } catch (_) { return sendJson(res, 400, { error: { message: '请求格式错误' } }); }
      const name = safeLibName(body.name || '');
      const fp = path.join(LIB_DIR, name);
      if (!fp.startsWith(LIB_DIR)) return sendJson(res, 403, { error: { message: 'Forbidden' } });
      const isDelete = url.pathname === '/api/library/delete' || body.action === 'delete';
      if (isDelete) {
        try { fs.unlinkSync(fp); return sendJson(res, 200, { ok: true }); }
        catch (_) { return sendJson(res, 404, { error: { message: '文件不存在' } }); }
      }
      // 写文件（创建/覆盖）
      const content = String(body.content || '');
      if (content.length > 500 * 1024) return sendJson(res, 400, { error: { message: '内容过大（限 500KB）' } });
      try {
        fs.writeFileSync(fp, content, 'utf8');
        return sendJson(res, 200, { ok: true, name });
      } catch (e) {
        return sendJson(res, 500, { error: { message: '写入失败：' + e.message } });
      }
    }
    return sendJson(res, 405, { error: { message: 'Method Not Allowed' } });
  }

  // 读取记忆库文件
  if (req.method === 'GET' && url.pathname === '/api/library/read') {
    const name = safeLibName(url.searchParams.get('name') || '');
    const fp = path.join(LIB_DIR, name);
    if (!fp.startsWith(LIB_DIR)) return sendJson(res, 403, { error: { message: 'Forbidden' } });
    try {
      const content = fs.readFileSync(fp, 'utf8');
      return sendJson(res, 200, { name, content: content.slice(0, 200000) });
    } catch (_) {
      return sendJson(res, 404, { error: { message: '文件不存在' } });
    }
  }

  // ---- 静态文件 ----
  let pathname;
  try {
    pathname = decodeURIComponent(url.pathname);
  } catch (_) {
    return sendJson(res, 400, { error: { message: 'Bad Request' } });
  }
  if (pathname === '/') pathname = '/index.html';
  const filePath = path.normalize(path.join(PUBLIC, pathname));
  if (!filePath.startsWith(PUBLIC)) return sendJson(res, 403, { error: { message: 'Forbidden' } });

  fs.readFile(filePath, (err, data) => {
    if (err) return sendJson(res, 404, { error: { message: 'Not Found' } });
    const ext = path.extname(filePath).toLowerCase();
    res.writeHead(200, {
      'Content-Type': MIME[ext] || 'application/octet-stream',
      'Cache-Control': 'no-cache',
    });
    res.end(data);
  });
});

server.listen(PORT, '0.0.0.0', () => {
  try { fs.mkdirSync(LIB_DIR, { recursive: true }); } catch (_) { /* 忽略 */ }
  console.log('');
  console.log('==================================================');
  console.log('  AI 伴侣 服务器已启动 (端口 ' + PORT + ')');
  console.log('--------------------------------------------------');
  console.log('  本机访问:    http://localhost:' + PORT);
  getLanIps().forEach((ip) => {
    console.log('  iPhone 访问: http://' + ip + ':' + PORT + '  ← 手机连同一个 Wi-Fi');
  });
  console.log('  磁盘记忆库:  ' + LIB_DIR + '  ← 可放入/导出 .txt 记忆文件');
  if (!readServerKey()) {
    console.log('  ⚠ 未配置 DeepSeek Key：编辑 config.json 的 apiKey，或打开 App 在【设置】页填写');
  }
  if (!readVisionKey()) {
    console.log('  ⚠ 未配置视觉模型 Key（发图片用）：可选，见 config.example.json 的 visionApiKey');
  }
  console.log('  按 Ctrl+C 停止服务器');
  console.log('==================================================');
  console.log('');
});
