'use strict';
/* ============================================================
   Electron 主进程：
   1. 拉起本地 FastAPI 后端
   2. 等后端端口就绪后创建窗口
   3. 退出时连同后端子进程一起结束
   4. 桌面消息弹窗（仿微信通知条，单通知位，翻滚替换）
   5. 窗口状态主动推送给渲染进程（聚焦/最小化，比渲染进程自己监听更可靠）
   ============================================================ */
const { app, BrowserWindow, Menu, ipcMain, screen, globalShortcut, shell } = require('electron');
const { spawn, exec } = require('child_process');
const path = require('path');
const http = require('http');
const fs = require('fs');
const os = require('os');
const net = require('net');
const { execSync } = require('child_process');

/* ---------- 调试日志：同时输出到控制台和桌面文件 ---------- */
const DEBUG_LOG_PATH = path.join(os.homedir(), 'Desktop', 'ai_notify_debug.log');
try { fs.writeFileSync(DEBUG_LOG_PATH, '=== 调试日志启动 ' + new Date().toISOString() + ' ===\n'); } catch(_) {}
function debugLog(...args) {
  const line = '[' + new Date().toISOString() + '] ' + args.map(a => {
    if (typeof a === 'object') { try { return JSON.stringify(a); } catch(_) { return String(a); } }
    return String(a);
  }).join(' ') + '\n';
  try { fs.appendFileSync(DEBUG_LOG_PATH, line); } catch(_) {}
}
const origLog = console.log.bind(console);
console.log = function(...args) { debugLog('[MAIN]', ...args); origLog(...args); };
const origErr = console.error.bind(console);
console.error = function(...args) { debugLog('[MAIN ERROR]', ...args); origErr(...args); };

// 渲染进程日志通过IPC发过来写文件
ipcMain.on('debug-log', (event, msg) => {
  debugLog('[RENDER]', msg);
});

const PORT = Number(process.env.PORT) || 32123;
const URL = 'http://127.0.0.1:' + PORT;
let backendProc = null;
let mainWindow = null;
let isQuitting = false;
let notifyWindow = null;
let notifyReady = false;
let pendingNotify = null;
let notifyPos = null;
let lastNotifyTime = 0;
const NOTIFY_THRESHOLD = 15000;
let unreadCount = 0;
let backendRestartCount = 0;      // 后端崩溃自动重启计数
const MAX_BACKEND_RESTARTS = 3;   // 崩溃自动重启上限

function root(...p) {
  return path.join(app.getAppPath(), ...p);
}

// ─────────────────────────────────────────
// 工具：清理可能残留的本项目后端进程
// 策略：优先只杀本项目特征进程（pc_backend.exe / 运行 run.py 的 python），
//       绝不杀 Electron 主程序自身（打包后进程名 = AI伴侣.exe）；
//       不要无差别 taskkill 掉占用 3000 端口的任意程序；仅当端口仍被占用时才按 PID 兜底，并打印日志说明杀了谁。
// ─────────────────────────────────────────
function killPortProcess(port) {
  return new Promise((resolve) => {
    const platform = os.platform();

    if (platform !== 'win32') {
      // macOS/Linux：先查占用端口的进程，只杀本项目特征进程，查不到特征才按端口兜底
      exec(`lsof -ti:${port}`, (err, stdout) => {
        if (err || !stdout) return resolve();
        const pids = stdout.trim().split('\n').filter(Boolean);
        if (pids.length === 0) return resolve();
        exec(`lsof -p ${pids.join(' -p ')} | awk 'NR>1 {print $1, $2}'`, (err2, out2) => {
          const matched = new Set();
          for (const line of (out2 || '').split('\n')) {
            const m = line.trim().split(/\s+/);
            if (m.length >= 2 && /python|pc_backend/i.test(m[0])) matched.add(m[1]);
          }
          const toKill = matched.size ? Array.from(matched) : pids;
          exec(`kill -9 ${toKill.join(' ')}`, () => {
            console.log('[Backend] 已结束占用端口 ' + port + ' 的进程: ' + toKill.join(', '));
            resolve();
          });
        });
      });
      return;
    }

    // Windows：先杀本项目特征进程（pc_backend.exe / 运行 run.py 的 python）
    // ★ 注意：绝不能把「AI伴侣.exe」（Electron 主程序自身）列为清理对象——
    //   打包版主进程名正是 AI伴侣.exe，若包含它会在启动时把自己的进程强杀导致闪退。
    //   同时排除当前进程 PID，双保险。
    const ps = "Get-CimInstance Win32_Process | Where-Object { (($_.Name -eq 'pc_backend.exe') -or (($_.Name -match '^python') -and ($_.CommandLine -like '*run.py*'))) -and $_.ProcessId -ne " + process.pid + " } | ForEach-Object { Write-Output ('kill:' + $_.ProcessId + ':' + $_.Name); taskkill /F /PID $_.ProcessId 2>$null | Out-Null }";
    exec(`powershell -NoProfile -Command "${ps}"`, (killErr, stdout) => {
      const killed = (stdout || '').split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      if (killErr) console.warn('[Backend] 枚举本项目进程失败:', killErr.message);
      console.log('[Backend] 已结束本项目特征进程: ' + (killed.length ? killed.join('; ') : '无'));

      // 兜底：确认端口仍被占用才按 PID 强杀（此时才允许动非本项目进程），并记录日志
      // ★ 只认 LISTENING 行且本地端口精确等于目标端口，避免 findstr :3000 误匹配 :30000~:30009、
      //   也避免误杀 ESTABLISHED 出站连接（浏览器等客户端进程）；并排除自身 PID。
      exec(`netstat -ano`, (err2, stdout2) => {
        if (err2 || !stdout2) return resolve();
        const pids = new Set();
        for (const line of stdout2.split('\n')) {
          if (!/LISTENING/i.test(line)) continue;
          const parts = line.trim().split(/\s+/);
          const localAddr = parts[1] || '';
          const localPort = localAddr.split(':').pop();
          if (localPort !== String(port)) continue;
          const pid = parts[parts.length - 1];
          if (pid && /^\d+$/.test(pid) && pid !== '0' && pid !== String(process.pid)) pids.add(pid);
        }
        if (pids.size === 0) return resolve();
        let done = 0;
        for (const pid of pids) {
          exec(`taskkill /PID ${pid} /F`, (e3) => {
            console.log('[Backend] 端口 ' + port + ' 仍被 PID=' + pid + ' 占用（非本项目特征进程），已按端口兜底强制结束' + (e3 ? '，失败：' + e3.message : ''));
            done++;
            if (done === pids.size) resolve();
          });
        }
      });
    });
  });
}

// ─────────────────────────────────────────
// 工具：等待端口空闲
// ─────────────────────────────────────────
function waitPortFree(port, timeout) {
  timeout = timeout || 8000;
  return new Promise((resolve, reject) => {
    const start = Date.now();

    function check() {
      const sock = new net.Socket();
      sock.setTimeout(500);

      sock.on('connect', () => {
        sock.destroy();
        if (Date.now() - start > timeout) {
          reject(new Error('端口 ' + port + ' 超时仍被占用'));
        } else {
          setTimeout(check, 400);
        }
      });

      sock.on('error', () => {
        sock.destroy();
        resolve();
      });

      sock.on('timeout', () => {
        sock.destroy();
        resolve();
      });

      sock.connect(port, '127.0.0.1');
    }

    check();
  });
}

// ─────────────────────────────────────────
// 工具：等待端口可用（后端启动完成）
// ─────────────────────────────────────────
function waitPortReady(port, timeout) {
  // ★ 3GB onefile 后端首次/更新后启动要解压 30~120 秒，30 秒窗口远远不够——
  //   曾导致「启动失败超时」弹窗（后端其实还在解压，进程没死，electron 先放弃了）。
  //   放宽到 3 分钟；端口提前就绪会立即返回，正常启动速度不受影响。
  timeout = timeout || 180000;
  return new Promise((resolve, reject) => {
    const start = Date.now();

    function check() {
      const sock = new net.Socket();
      sock.setTimeout(500);

      sock.on('connect', () => {
        sock.destroy();
        resolve();
      });

      sock.on('error', () => {
        sock.destroy();
        if (Date.now() - start > timeout) {
          reject(new Error('等待端口 ' + port + ' 超时'));
        } else {
          setTimeout(check, 500);
        }
      });

      sock.on('timeout', () => {
        sock.destroy();
        setTimeout(check, 500);
      });

      sock.connect(port, '127.0.0.1');
    }

    setTimeout(check, 500);
  });
}

async function restartBackend(failLabel) {
  // 先清理端口，再启动后端（startBackend 内部也会清一次，这里先确保端口释放）
  await killPortProcess(PORT).catch(() => {});
  await waitPortFree(PORT).catch(() => {});
  try {
    await startBackend();
    return true;
  } catch (e) {
    console.error(failLabel || '[Backend] 重启失败:', e && e.message || e);
    return false;
  }
}

function showBackendDownDialog(detail) {
  try {
    const { dialog } = require('electron');
    const opts = {
      type: 'warning',
      title: '本地后端已退出',
      message: '本地后端已退出',
      detail: (detail || '') + '\n\n选择“重试”立即重新启动后端，或“退出”关闭应用。',
      buttons: ['重试', '退出应用'],
      defaultId: 0,
      cancelId: 1,
    };
    const btnIdx = (mainWindow && !mainWindow.isDestroyed())
      ? dialog.showMessageBoxSync(mainWindow, opts)
      : dialog.showMessageBoxSync(opts);
    if (btnIdx === 0) {
      backendRestartCount = 0;
      restartBackend('[Backend] 手动重试启动失败:').catch(() => {});
    } else {
      killBackend().finally(() => app.quit());
    }
  } catch (e) {
    console.error('[Backend] 弹窗提示失败:', e);
  }
}

/* ★ 界面自动同步（2026-09-14）
   打包版后端托管的是**应用目录里的 resources\public**，不是项目根的 public\。
   以前只有 tools\HomeHimeLauncher.ps1 会镜像它 —— 所以「直接双击 HomeAime.exe」启动时，
   改了 public\ 下的界面文件（如 profile.js 新增档位）根本不生效（实测踩过这个坑）。
   这里让壳自己镜像：按 大小/mtime 增量拷贝；**不删除**目标端多余文件（保守，避免误删）。
   只对开发机（硬编码路径存在）生效，其他机器静默跳过。 */
const PUB_SRC = '<PROJECT_ROOT>\\public';

function syncPublicDir() {
  try {
    if (!app.isPackaged) return;
    const dst = path.join(process.resourcesPath, 'public');
    if (!fs.existsSync(PUB_SRC) || !fs.existsSync(path.dirname(dst))) return;
    let copied = 0;
    const walk = (rel) => {
      const sDir = path.join(PUB_SRC, rel);
      const dDir = path.join(dst, rel);
      if (!fs.existsSync(dDir)) fs.mkdirSync(dDir, { recursive: true });
      for (const name of fs.readdirSync(sDir)) {
        if (name === '_preview8090.js') continue;      // 调试文件，与启动器 robocopy 的排除项一致
        const s = path.join(sDir, name);
        const d = path.join(dDir, name);
        const st = fs.statSync(s);
        if (st.isDirectory()) { walk(path.join(rel, name)); continue; }
        let need = true;
        if (fs.existsSync(d)) {
          const dt = fs.statSync(d);
          need = (st.size !== dt.size) || (st.mtimeMs > dt.mtimeMs + 1000);
        }
        if (need) { fs.copyFileSync(s, d); copied++; }
      }
    };
    walk('');
    if (copied) console.log('[UI] 已同步 ' + copied + ' 个界面文件到打包目录');
  } catch (e) {
    console.warn('[UI] 界面自动同步失败（沿用现有版本）:', e && e.message);
  }
}

async function startBackend() {
  console.log('[Backend] 检查端口 ' + PORT + '...');

  // 先杀掉可能残留的进程（包括 pc_backend.exe 和任何占用端口的 python）
  await killPortProcess(PORT);
  await waitPortFree(PORT).catch((e) => {
    console.warn('[Backend] 端口 ' + PORT + ' 清理超时，继续尝试启动:', e.message);
  });

  console.log('[Backend] 端口 ' + PORT + ' 已清理，启动后端...');

  if (app.isPackaged) {
    syncPublicDir();   // ★ 先把项目 public\ 镜像进来，再起后端（后端只托管 resources\public）
    const exe = path.join(process.resourcesPath, 'backend', 'pc_backend.exe');
    // ★ 启动即最新（2026-09-09）：开发机重新打包后（backend/dist/pc_backend.exe），
    //   双击桌面图标直接生效——这里检测打包产物比包体内的新就自动替换，
    //   无需手动拷贝 exe。路径不存在（其他机器）时静默跳过。
    try {
      const srcExe = '<PROJECT_ROOT>\\backend\\dist\\pc_backend.exe';
      if (fs.existsSync(srcExe)) {
        const sStat = fs.statSync(srcExe);
        const dStat = fs.existsSync(exe) ? fs.statSync(exe) : null;
        if (!dStat || sStat.mtimeMs > dStat.mtimeMs) {
          console.log('[Backend] 发现更新版后端（' + Math.round(sStat.size / 1048576) + 'MB），自动替换...');
          fs.copyFileSync(srcExe, exe);
          console.log('[Backend] 后端已同步为最新打包版本');
        }
      }
    } catch (syncErr) {
      console.warn('[Backend] 后端自动同步失败（沿用现有版本）:', syncErr.message);
    }
    backendProc = spawn(exe, [], {
      cwd: path.dirname(exe),
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: Object.assign({}, process.env, {
        PORT: String(PORT),
        AI_COMPANION_DATA_DIR: process.env.AI_COMPANION_DATA_DIR || path.join(app.getPath('userData'), 'data'),
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
      }),
    });
  } else {
    const venvPy = root('backend', 'venv', 'Scripts', 'python.exe');
    const fs2 = require('fs');
    const cmd = fs2.existsSync(venvPy) ? venvPy : 'python';
    backendProc = spawn(cmd, [root('run.py')], {
      cwd: root(),
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: Object.assign({}, process.env, {
        PORT: String(PORT),
        AI_COMPANION_DATA_DIR: process.env.AI_COMPANION_DATA_DIR || (app.isPackaged ? path.join(app.getPath('userData'), 'data') : root('backend', 'data')),
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
      }),
    });
  }

  // 输出后端日志到 Electron 控制台（后端已强制 UTF-8 输出，统一按 utf8 解码）
  if (backendProc.stdout) {
    backendProc.stdout.on('data', (d) => {
      console.log('[Python] ' + d.toString('utf8').trim());
    });
  }
  if (backendProc.stderr) {
    backendProc.stderr.on('data', (d) => {
      console.error('[Python ERR] ' + d.toString('utf8').trim());
    });
  }
  backendProc.on('exit', (code) => {
    console.log('[Backend] 进程退出，code=' + code);
    if (isQuitting) return;
    backendProc = null;
    // 看护：意外退出时自动重启（限 3 次），仍失败则弹窗提示，而不是只打日志
    if (backendRestartCount < MAX_BACKEND_RESTARTS) {
      backendRestartCount++;
      console.log('[Backend] 后端意外退出，自动重启 ' + backendRestartCount + '/' + MAX_BACKEND_RESTARTS + ' 次...');
      restartBackend('[Backend] 自动重启失败:')
        .catch((e) => {
          console.error('[Backend] 自动重启失败:', e);
        });
    } else {
      console.log('[Backend] 后端重启次数已达上限（' + MAX_BACKEND_RESTARTS + '），停止自动重启');
      showBackendDownDialog('本地后端已退出（已自动重启 ' + MAX_BACKEND_RESTARTS + ' 次仍未成功）。\n请检查后端是否被安全软件拦截，或选择重试。');
    }
  });

  // 等待后端就绪（3GB onefile 解压 + torch 导入可能要 1~2 分钟，显式给足 3 分钟窗口）
  await waitPortReady(PORT, 180000);
  backendRestartCount = 0;   // 后端稳定运行后，重置崩溃重启计数
  console.log('[Backend] 启动完成，端口 ' + PORT + ' 就绪');
}

/* ---------- CosyVoice 本地 TTS 服务（9881）拉起/看护 ----------
   与 backend/tts.py 的 COSYVOICE_API 约定一致；打包版不跑 run.py，
   必须由主进程自己拉起。开发版 run.py 也会拉起，这里检测已监听则跳过（幂等）。 */
const COSYVOICE_DIR  = process.env.COSYVOICE_DIR  || '<COSYVOICE_HOME>';
const COSYVOICE_PY   = process.env.COSYVOICE_PY   || path.join(COSYVOICE_DIR, '.venv', 'Scripts', 'python.exe');
const COSYVOICE_PORT = Number(process.env.COSYVOICE_PORT) || 9881;
let cosyvoiceProc = null;

function probePort(port, timeoutMs) {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    sock.setTimeout(timeoutMs || 1500);
    sock.on('connect', () => { sock.destroy(); resolve(true); });
    sock.on('error', () => { sock.destroy(); resolve(false); });
    sock.on('timeout', () => { sock.destroy(); resolve(false); });
    sock.connect(port, '127.0.0.1');
  });
}

async function ensureCosyVoice() {
  try {
    // ★ 竞争窗口：pc_backend.exe（打包了 run.py）启动时也会检测并拉起 9881。
    //   这里先等 2 秒再探测，避免双方同时拉起重复服务（重复无害但多余）。
    await new Promise((r) => setTimeout(r, 2000));
    if (await probePort(COSYVOICE_PORT)) {
      console.log('[CosyVoice] 服务已在运行 (' + COSYVOICE_PORT + ')');
      return;
    }
    if (!fs.existsSync(COSYVOICE_DIR) || !fs.existsSync(COSYVOICE_PY)) {
      console.log('[CosyVoice] 未部署（' + COSYVOICE_DIR + ' 不存在），跳过拉起，语音走 edge-tts 兜底');
      return;
    }
    console.log('[CosyVoice] 启动本地 TTS 服务 (' + COSYVOICE_PORT + ')...');
    cosyvoiceProc = spawn(COSYVOICE_PY, ['-m', 'uvicorn', 'server:app', '--host', '127.0.0.1', '--port', String(COSYVOICE_PORT)], {
      cwd: COSYVOICE_DIR,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' }),
    });
    if (cosyvoiceProc.stdout) cosyvoiceProc.stdout.on('data', (d) => { console.log('[CosyVoice] ' + String(d).trim().slice(0, 300)); });
    if (cosyvoiceProc.stderr) cosyvoiceProc.stderr.on('data', (d) => { console.log('[CosyVoice ERR] ' + String(d).trim().slice(0, 300)); });
    cosyvoiceProc.on('exit', (code) => { console.log('[CosyVoice] 服务退出 code=' + code); cosyvoiceProc = null; });
    // uvicorn 秒级监听；模型是懒加载（首次推理才加载），这里等端口即可
    await waitPortReady(COSYVOICE_PORT, 45000)
      .catch(() => console.warn('[CosyVoice] 等待端口超时，继续（模型首次推理时加载）'));
  } catch (e) {
    console.error('[CosyVoice] 拉起失败(静默):', e && e.message || e);
  }
}

async function killCosyVoice() {
  if (cosyvoiceProc) {
    try {
      cosyvoiceProc.kill('SIGTERM');
      if (process.platform === 'win32') {
        require('child_process').execSync(`taskkill /F /PID ${cosyvoiceProc.pid} /T`, { timeout: 3000 });
      }
    } catch (_) {}
    cosyvoiceProc = null;
  }
}

async function killBackend() {
  isQuitting = true;
  console.log('[Cleanup] 开始清理后端进程...');

  // 0. 先清理 CosyVoice（独立进程，别让它残留占 9881）
  await killCosyVoice();

  // 1. 先用句柄杀
  if (backendProc) {
    try {
      backendProc.kill('SIGTERM');
      // Windows 上 SIGTERM 可能不生效，再补一刀
      if (process.platform === 'win32') {
        require('child_process').execSync(
          `taskkill /F /PID ${backendProc.pid} /T`,
          { timeout: 3000 }
        );
      }
    } catch (e) {
      console.log('[Electron] killBackend by handle failed:', e.message);
    }
    backendProc = null;
  }

  // 2. 兜底：按端口强杀（确保端口释放）
  // ★ 只杀 LISTENING 且本地端口精确等于目标端口的 PID，避免 findstr :3000 误匹配 :30000~:30009、
  //   也避免误杀 ESTABLISHED 出站连接（浏览器等客户端进程）；并排除自身 PID。
  try {
    if (process.platform === 'win32') {
      const result = require('child_process').execSync(
        `netstat -ano`,
        { timeout: 3000, encoding: 'utf8' }
      );
      const killed = new Set();
      for (const line of result.split('\n')) {
        if (!/LISTENING/i.test(line)) continue;
        const parts = line.trim().split(/\s+/);
        const localAddr = parts[1] || '';
        const localPort = localAddr.split(':').pop();
        if (localPort !== String(PORT)) continue;
        const pid = parts[parts.length - 1];
        if (pid && /^\d+$/.test(pid) && pid !== '0' && pid !== String(process.pid) && !killed.has(pid)) {
          try {
            require('child_process').execSync(
              `taskkill /F /PID ${pid}`,
              { timeout: 3000 }
            );
            killed.add(pid);
            console.log(`[Electron] 已杀掉占用端口 ${PORT} 的进程 PID=${pid}`);
          } catch (e2) {}
        }
      }
    }
  } catch (e) {
    // netstat 没找到占用进程，正常情况
  }

  console.log('[Electron] killBackend 完成');
}

function waitForBackend(retries) {
  return new Promise((resolve, reject) => {
    const tryOnce = (left) => {
      const req = http.get(URL + '/api/config', (res) => {
        res.resume();
        resolve();
      });
      req.on('error', () => {
        if (left <= 0) return reject(new Error('后端启动超时'));
        setTimeout(() => tryOnce(left - 1), 500);
      });
      req.setTimeout(2000, () => { req.destroy(); });
    };
    tryOnce(retries || 360);   // 360×500ms = 3 分钟（与 waitPortReady 窗口对齐）
  });
}

ipcMain.on('win-min', () => { if (mainWindow) mainWindow.minimize(); });
ipcMain.on('win-max', () => { if (mainWindow) { if (mainWindow.isMaximized()) mainWindow.unmaximize(); else mainWindow.maximize(); } });
ipcMain.on('win-close', () => { if (mainWindow) mainWindow.close(); });
ipcMain.on('win-is-focused', (e) => { e.returnValue = !!(mainWindow && mainWindow.isFocused()); });

/* ---------- 弹窗配置读写 ---------- */
function notifyConfigPath() {
  return path.join(app.getPath('userData'), 'notify_config.json');
}
function loadNotifyConfig() {
  try {
    return JSON.parse(fs.readFileSync(notifyConfigPath(), 'utf-8'));
  } catch (_) {
    return { enabled: true, autoHide: 5, sound: 'ding', volume: 70, threshold: 15 };
  }
}
function saveNotifyConfig(cfg) {
  try {
    fs.writeFileSync(notifyConfigPath(), JSON.stringify(cfg, null, 2), 'utf-8');
  } catch (_) {}
}

/* ============ 独立聊天窗口（可拖出 / 可调整大小 / 记住尺寸） ============ */
let chatWindow = null;
function _chatBoundsFile() { return path.join(app.getPath('userData'), 'chat_window.json'); }
function _loadChatBounds() { try { return JSON.parse(fs.readFileSync(_chatBoundsFile(), 'utf8')); } catch (_) { return null; } }
function _saveChatBounds(b) { try { fs.writeFileSync(_chatBoundsFile(), JSON.stringify(b)); } catch (_) {} }
function openChatWindow(opts) {
  opts = opts || {};
  if (chatWindow && !chatWindow.isDestroyed()) {
    try { if (opts.contactId) chatWindow.webContents.send('chatwin-contact', opts); } catch (_) {}
    chatWindow.show(); chatWindow.focus();
    return chatWindow;
  }
  const saved = _loadChatBounds() || {};
  const w = Math.max(320, opts.width || saved.width || 430);
  const h = Math.max(420, opts.height || saved.height || 680);
  const bx = (typeof saved.x === 'number') ? saved.x : undefined;
  const by = (typeof saved.y === 'number') ? saved.y : undefined;
  chatWindow = new BrowserWindow({
    width: w, height: h, x: bx, y: by, minWidth: 320, minHeight: 420,
    frame: false, resizable: true, autoHideMenuBar: true, backgroundColor: '#121020',
    title: '聊天',
    webPreferences: { contextIsolation: true, nodeIntegration: false, preload: path.join(__dirname, 'preload.js'), backgroundThrottling: false },
  });
  const q = ['/?popout=chat'];
  if (opts.contactId) q.push('contact=' + encodeURIComponent(opts.contactId));
  if (opts.name) q.push('name=' + encodeURIComponent(opts.name));
  chatWindow.loadURL('http://127.0.0.1:' + PORT + q.join('&'));
  const persist = () => { try { if (chatWindow && !chatWindow.isDestroyed() && !chatWindow.isMinimized()) _saveChatBounds(chatWindow.getBounds()); } catch (_) {} };
  let _t = null; const deb = () => { clearTimeout(_t); _t = setTimeout(persist, 400); };
  chatWindow.on('resize', deb); chatWindow.on('move', deb);
  chatWindow.on('close', persist);
  chatWindow.on('closed', () => { persist(); chatWindow = null; });
  return chatWindow;
}
ipcMain.handle('chatwin:open', (e, opts) => { openChatWindow(opts); return true; });
ipcMain.handle('chatwin:close', () => { if (chatWindow && !chatWindow.isDestroyed()) chatWindow.close(); return true; });
ipcMain.handle('chatwin:is-open', () => !!(chatWindow && !chatWindow.isDestroyed()));
ipcMain.on('chatwin:minimize', () => { if (chatWindow && !chatWindow.isDestroyed()) chatWindow.minimize(); });
ipcMain.handle('get-notify-config', () => loadNotifyConfig());
ipcMain.handle('set-notify-config', (e, patch) => {
  const cfg = Object.assign(loadNotifyConfig(), patch || {});
  saveNotifyConfig(cfg);
  return cfg;
});

/* ---------- 桌面消息弹窗 ---------- */
function getDefaultNotifyPos() {
  const display = screen.getPrimaryDisplay();
  const { width, height } = display.workAreaSize;
  return { x: width - 440 - 20, y: height - 130 - 20 };
}
function isPosVisible(pos) {
  if (!pos) return false;
  for (const d of screen.getAllDisplays()) {
    const b = d.bounds;
    if (pos.x >= b.x && pos.x <= b.x + b.width - 100 &&
        pos.y >= b.y && pos.y <= b.y + b.height - 50) {
      return true;
    }
  }
  return false;
}
function createNotifyWindow() {
  if (notifyWindow) return;
  const cfg = loadNotifyConfig();
  const def = getDefaultNotifyPos();
  notifyPos = isPosVisible(cfg.position) ? cfg.position : def;
  if (!isPosVisible(cfg.position)) {
    cfg.position = def;
    saveNotifyConfig(cfg);
  }

  notifyWindow = new BrowserWindow({
    width: 420,
    height: 135,
    x: notifyPos.x,
    y: notifyPos.y,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    show: false,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
    },
  });
  // 开发模式下 process.resourcesPath 不存在，用 app.getAppPath()（项目根）代替
  notifyWindow.loadFile(path.join(app.isPackaged ? process.resourcesPath : app.getAppPath(), 'public', 'notify.html'));
  notifyWindow.webContents.on('did-fail-load', (e, code, desc) => {
    console.error('[DEBUG notify.html] load failed:', code, desc);
  });
  notifyWindow.webContents.on('did-finish-load', () => {
    console.log('[DEBUG notify.html] load success');
    notifyReady = true;
    if (pendingNotify) {
      const d = pendingNotify;
      pendingNotify = null;
      doShowNotify(d);
    }
  });
  notifyWindow.on('closed', () => { notifyWindow = null; notifyReady = false; });
}

function showNotify(data, skipFocusCheck) {
  const cfg = loadNotifyConfig();
  console.log('[DEBUG showNotify] called, skipFocusCheck=', skipFocusCheck, 'enabled=', cfg.enabled !== false, 'data=', JSON.stringify(data).slice(0,100));
  if (cfg.enabled === false) { console.log('[DEBUG showNotify] BLOCKED: disabled'); return; }
  // 修正：只有主窗口"可见、未最小化且聚焦"时才阻止弹窗
  const activelyVisible = mainWindow &&
    mainWindow.isVisible() &&
    !mainWindow.isMinimized() &&
    mainWindow.isFocused();
  console.log('[DEBUG showNotify] activelyVisible=', activelyVisible, 'isVisible=', mainWindow?.isVisible(), 'isMinimized=', mainWindow?.isMinimized(), 'isFocused=', mainWindow?.isFocused());
  if (!skipFocusCheck && activelyVisible) {
    console.log('[DEBUG showNotify] BLOCKED: activelyVisible');
    return;
  }
  if (!notifyWindow) createNotifyWindow();
  const now = Date.now();
  const threshold = (cfg.threshold || NOTIFY_THRESHOLD) * 1000;
  const isConsecutive = (now - lastNotifyTime) < threshold;
  lastNotifyTime = now;

  if (isConsecutive) {
    unreadCount++;
  } else {
    unreadCount = 1;
  }
  if (process.platform === 'win32' && mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.flashFrame(true);
  }
  if (app.setBadgeCount) {
    try { app.setBadgeCount(unreadCount > 1 ? unreadCount : 0); } catch (_) {}
  }

  const payload = Object.assign({}, data, {
    time: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
    autoHideMs: (cfg.autoHide || 5) * 1000,
    msgCount: unreadCount,
    bgColor: cfg.bgColor || '',
    bgOpacity: cfg.bgOpacity != null ? cfg.bgOpacity : null,
    textColor: cfg.textColor || '',
    borderColor: cfg.borderColor || '',
    bgImage: cfg.bgImage || '',
    width: cfg.width || null,
    height: cfg.height || null,
    fontSize: cfg.fontSize || null,
    sound: cfg.sound || 'ding',
    volume: cfg.volume != null ? cfg.volume : 70,
    customSound: cfg.customSound || '',
  });

  if (!notifyReady) {
    pendingNotify = payload;
    return;
  }
  doShowNotify(payload);
}

function doShowNotify(payload) {
  if (!notifyWindow) return;
  if (!isPosVisible(notifyPos)) {
    notifyPos = getDefaultNotifyPos();
    const cfg = loadNotifyConfig();
    cfg.position = notifyPos;
    saveNotifyConfig(cfg);
  }
  notifyWindow.setPosition(notifyPos.x, notifyPos.y);
  notifyWindow.show();
  notifyWindow.webContents.send('show-notify', payload);
}

ipcMain.on('show-notify', (event, data) => {
  console.log('[DEBUG show-notify IPC] received, notifyReady=', notifyReady, 'notifyWindow=', !!notifyWindow, 'data=', JSON.stringify(data).slice(0,150));
  showNotify(data, data && data._test);
});
ipcMain.on('notify-hidden', () => {
  if (notifyWindow) notifyWindow.hide();
});
// ========== 通知窗快捷回复 ==========

// notify.html → 主进程 → 主窗口渲染进程
// 注意：这里只转发，绝对不能 show/focus 主窗口
ipcMain.on('notify-quick-reply', (event, data) => {
  if (!mainWindow || mainWindow.isDestroyed()) return;

  mainWindow.webContents.send('notify-quick-reply', data);
});

// 主窗口渲染进程 → 主进程 → notify.html
ipcMain.on('notify-quick-reply-result', (event, data) => {
  if (!notifyWindow || notifyWindow.isDestroyed()) return;

  notifyWindow.webContents.send('notify-quick-reply-result', data);
});

// 打开聊天：明确恢复窗口并聚焦
ipcMain.on('notify-open', (event, data) => {
  if (mainWindow) {
    mainWindow.show();

    if (mainWindow.isMinimized()) {
      mainWindow.restore();
    }

    mainWindow.focus();
    mainWindow.webContents.send('notify-open', data);
  }
});

// 弹窗拖动
let dragStartPos = null;
ipcMain.on('notify-drag-start', () => {
  if (notifyWindow) dragStartPos = notifyWindow.getPosition();
});
ipcMain.on('notify-drag-move', (event, { dx, dy }) => {
  if (notifyWindow && dragStartPos) {
    notifyWindow.setPosition(dragStartPos[0] + dx, dragStartPos[1] + dy);
  }
});
ipcMain.on('notify-drag-end', () => {
  if (notifyWindow) {
    const pos = notifyWindow.getPosition();
    notifyPos = { x: pos[0], y: pos[1] };
    const cfg = loadNotifyConfig();
    cfg.position = notifyPos;
    saveNotifyConfig(cfg);
  }
  dragStartPos = null;
});

/* ---------- 推送窗口状态到渲染进程（权威状态） ---------- */
function pushWindowState() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const state = {
    focused: mainWindow.isFocused(),
    minimized: mainWindow.isMinimized(),
    visible: mainWindow.isVisible(),
  };
  try { mainWindow.webContents.send('window-state', state); } catch (_) {}
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1080,
    height: 760,
    minWidth: 420,
    minHeight: 560,
    title: 'HomeAime',
    icon: root('public', 'icons', 'HomeAime.png'),
    backgroundColor: '#EDEDED',
    frame: false,
    autoHideMenuBar: true,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'),
      backgroundThrottling: false,  // 关键：最小化/隐藏后不挂起渲染进程，否则WebSocket消息收不到
    },
  });
  Menu.setApplicationMenu(null);

  // ★ 外部链接用系统默认浏览器打开（如设置页的「没有 Key？点这里」跳转百炼官网）
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) {
      shell.openExternal(url);
    }
    return { action: 'deny' };
  });

  // ★ 渲染进程崩溃 / 加载失败 兜底：避免静默闪退，改为明确报错并写日志
  mainWindow.webContents.on('did-fail-load', (event, errorCode, errorDescription, validatedURL) => {
    debugLog('[MAIN ERROR] did-fail-load', errorCode, errorDescription, validatedURL);
    try {
      const { dialog } = require('electron');
      dialog.showErrorBox('页面加载失败',
        '无法加载本地页面：' + validatedURL + '\n错误：' + errorDescription + ' (' + errorCode + ')');
    } catch (_) {}
  });
  mainWindow.webContents.on('render-process-gone', (event, details) => {
    debugLog('[MAIN ERROR] render-process-gone', JSON.stringify(details));
    try {
      const { dialog } = require('electron');
      dialog.showErrorBox('渲染进程崩溃', '原因：' + (details && details.reason) + '\n详情：' + (details && details.exitCode));
    } catch (_) {}
  });
  mainWindow.on('unresponsive', () => debugLog('[MAIN ERROR] mainWindow unresponsive'));

  mainWindow.loadURL(URL);
  // 窗口状态变化时主动推给渲染进程
  mainWindow.on('focus', () => { unreadCount = 0; if (app.setBadgeCount) { try { app.setBadgeCount(0); } catch (_) {} } mainWindow.flashFrame(false); pushWindowState(); });
  mainWindow.on('blur', () => pushWindowState());
  mainWindow.on('minimize', () => pushWindowState());
  mainWindow.on('restore', () => pushWindowState());
  mainWindow.on('show', () => pushWindowState());
  mainWindow.on('hide', () => pushWindowState());
  mainWindow.webContents.on('did-finish-load', () => pushWindowState());
  mainWindow.on('close', () => {
    if (notifyWindow) {
      try { notifyWindow.destroy(); } catch (_) {}
      notifyWindow = null;
    }
  });
  mainWindow.on('closed', () => { mainWindow = null; });
}

function toggleMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return;

  if (mainWindow.isVisible() && mainWindow.isFocused() && !mainWindow.isMinimized()) {
    mainWindow.hide();
    return;
  }

  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    try {
      await startBackend();
      // ★ 拉起本地 CosyVoice TTS 服务（9881）——桌面版语音断网可用；已运行则跳过
      await ensureCosyVoice();
    } catch (e) {
      const { dialog } = require('electron');
      dialog.showErrorBox('后端启动失败',
        '本地后端（' + (app.isPackaged ? 'pc_backend.exe' : 'run.py') +
        '）未能启动。\n\n' + String(e.message || e));
      app.quit();
      return;
    }
    createWindow();
    const registered = globalShortcut.register('CommandOrControl+Escape', toggleMainWindow);
    if (!registered) console.warn('[Shortcut] 注册 Ctrl+Esc 失败');
  });

  app.on('window-all-closed', () => {
    killBackend().finally(() => app.quit());
  });

  // before-quit：先阻止退出，清理完再真正退出
  app.on('before-quit', (event) => {
    if (isQuitting) return;
    event.preventDefault();
    globalShortcut.unregister('CommandOrControl+Escape');
    killBackend().finally(() => app.quit());
  });

  // 兜底：进程收到终止信号时也清理
  process.on('SIGINT', async () => { await killBackend(); process.exit(0); });
  process.on('SIGTERM', async () => { await killBackend(); process.exit(0); });
}
