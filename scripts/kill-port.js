/**
 * 启动前清理端口脚本
 * npm run dev 或 npm run kill-port 手动调用
 */
const { exec } = require('child_process');
const os = require('os');

const PORT = 3000;

function killPort(port) {
  return new Promise((resolve) => {
    const platform = os.platform();

    if (platform === 'win32') {
      exec(
        `netstat -ano | findstr :${port}`,
        (err, stdout) => {
          if (err || !stdout) {
            console.log(`[kill-port] 端口 ${port} 没有占用进程`);
            return resolve();
          }

          const lines = stdout.trim().split('\n');
          const pids = new Set();

          for (const line of lines) {
            const parts = line.trim().split(/\s+/);
            const pid = parts[parts.length - 1];
            if (pid && /^\d+$/.test(pid) && pid !== '0') {
              pids.add(pid);
            }
          }

          if (pids.size === 0) {
            console.log(`[kill-port] 端口 ${port} 没有找到 PID`);
            return resolve();
          }

          console.log(`[kill-port] 发现 PID: ${[...pids].join(', ')}，正在杀掉...`);

          let done = 0;
          for (const pid of pids) {
            exec(`taskkill /PID ${pid} /F`, (e) => {
              if (e) {
                console.warn(`[kill-port] 杀掉 PID ${pid} 失败:`, e.message);
              } else {
                console.log(`[kill-port] PID ${pid} 已清除`);
              }
              done++;
              if (done === pids.size) resolve();
            });
          }
        }
      );
    } else {
      exec(`lsof -ti:${port}`, (err, stdout) => {
        if (err || !stdout) {
          console.log(`[kill-port] 端口 ${port} 没有占用进程`);
          return resolve();
        }
        const pids = stdout.trim().split('\n').filter(Boolean);
        console.log(`[kill-port] 发现 PID: ${pids.join(', ')}，正在杀掉...`);
        exec(`kill -9 ${pids.join(' ')}`, () => {
          console.log(`[kill-port] 端口 ${port} 已清除`);
          resolve();
        });
      });
    }
  });
}

killPort(PORT).then(() => {
  console.log('[kill-port] 完成');
  process.exit(0);
});
