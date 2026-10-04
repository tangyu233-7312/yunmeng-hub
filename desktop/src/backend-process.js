'use strict';

/**
 * 后端（uvicorn）子进程的启动与**清理**。
 *
 * ★ 这一节是本轮最容易留下脏状态的地方：如果只 kill 直接子进程，
 *   `python -m uvicorn` 自己 fork 出来的进程会活下来占住端口，
 *   下一次启动就变成"端口被占 / 数据库连不上"这类难查的故障。
 *   Windows 上必须按**进程树**杀（`taskkill /T`）。
 *
 * ★ 为什么 `isAlive` 用 `process.kill(pid, 0)` 而不是 `tasklist`：
 *   `tasklist` 的输出受系统语言与编码影响（中文 Windows 表头不同），去解析字符串很脆；
 *   而"发 0 号信号"是操作系统级的判据，且不依赖任何输出。
 */

const { spawn, spawnSync } = require('node:child_process');
const { detectPython, buildBackendEnv, truncate } = require('./backend-config');

const IS_WINDOWS = process.platform === 'win32';

/**
 * 进程还活着吗？
 * @param {number} pid
 * @returns {boolean}
 */
function isAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM = 进程存在但不属于我（也算活着）；ESRCH = 真的没了
    return Boolean(error && error.code === 'EPERM');
  }
}

/**
 * 结束一棵进程树。**幂等**：进程已经没了就返回 false，不报错。
 *
 * @param {number} pid
 * @param {object} [options]
 * @param {typeof spawnSync} [options.spawnSyncImpl]
 * @returns {boolean} 是否真的执行了结束动作
 */
function killTree(pid, options = {}) {
  if (!isAlive(pid)) return false;
  const spawnSyncImpl = options.spawnSyncImpl || spawnSync;
  const platform = options.platform || process.platform;

  if (platform === 'win32') {
    // /T 连带子进程，/F 强制 —— uvicorn 的 reload/worker 都会一并带走
    spawnSyncImpl('taskkill', ['/pid', String(pid), '/T', '/F'], {
      stdio: 'ignore',
      windowsHide: true,
      timeout: 15000,
    });
  }
  // 双保险（也是非 Windows 的唯一手段）：直接对进程树根发终止信号
  try {
    process.kill(pid, 'SIGTERM');
  } catch {
    /* 已经退出 */
  }
  return true;
}

/**
 * 启动后端。
 *
 * 两种模式（由调用方的 `mode` 决定）：
 *   · `'python'`（默认）—— `python -m uvicorn app.main:app --host --port`（阶段 1 的路径，开发态用）
 *   · `'sidecar'`      —— 直接跑打包好的 `backend.exe --host --port --data-dir …`（阶段 2，用户机用）
 *
 * ★ 为什么两种都留着：打包产物**不入库**，所以任何一份 clone 下来的代码都只有 python 模式可跑。
 *   如果只支持 sidecar，开发与四件套验收就没法在同一份代码上进行了。
 *
 * @param {object} options
 * @param {string} options.appRoot
 * @param {string} options.python            可执行文件（python 或 backend.exe）
 * @param {number} options.port
 * @param {string} [options.host]
 * @param {string} [options.mode]            'python' | 'sidecar'
 * @param {string[]} [options.args]           完全自定义参数（会覆盖上面的默认参数）
 * @param {string[]} [options.extraArgs]      在默认参数之后追加
 * @param {NodeJS.ProcessEnv} [options.fileEnv]   .env 里读出来的值
 * @param {NodeJS.ProcessEnv} [options.env]       基础环境
 * @param {NodeJS.ProcessEnv} [options.envOverrides] 最高优先级的注入（端口/数据目录等）
 * @param {typeof spawn} [options.spawnImpl]
 * @returns {{ pid: number|undefined, child: import('node:child_process').ChildProcess, args: string[] }}
 */
function spawnBackend(options) {
  const host = options.host || '127.0.0.1';
  const spawnImpl = options.spawnImpl || spawn;
  const mode = options.mode || 'python';

  const env = buildBackendEnv({
    baseEnv: options.env || process.env,
    fileEnv: options.fileEnv || {},
    overrides: {
      HNE_HOST: host,
      HNE_PORT: String(options.port),
      ...(options.envOverrides || {}),
    },
  });

  let args;
  if (Array.isArray(options.args)) {
    args = options.args.slice();
  } else if (mode === 'sidecar') {
    args = ['--host', host, '--port', String(options.port)];
  } else {
    args = [
      '-m', 'uvicorn', 'app.main:app',
      '--host', host,
      '--port', String(options.port),
    ];
  }
  if (Array.isArray(options.extraArgs)) args = args.concat(options.extraArgs);

  const child = spawnImpl(options.python, args, {
    cwd: options.cwd || options.appRoot,
    env,
    windowsHide: true,
    // ★ 必须 pipe：后端日志是排障的唯一线索，要落盘。
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  return { pid: child.pid, child, args };
}

/**
 * 把子进程的输出同时写进日志文件与控制台。
 *
 * ★ 为什么要落盘：桌面版出错时用户看到的是窗口，而我们需要的是一份能贴出来的文本。
 *   日志落在 userData/logs/backend.log（安装目录之外），卸载重装不会丢。
 *
 * @param {import('node:child_process').ChildProcess} child
 * @param {(line: string) => void} writeLine
 */
function pipeOutputTo(child, writeLine) {
  const wire = (stream, label) => {
    if (!stream) return;
    stream.setEncoding('utf8');
    let buffered = '';
    stream.on('data', (chunk) => {
      buffered += chunk;
      const lines = buffered.split(/\r?\n/);
      buffered = lines.pop() || '';
      for (const line of lines) writeLine(`${label} ${line}`);
    });
    stream.on('end', () => {
      if (buffered) writeLine(`${label} ${buffered}`);
      buffered = '';
    });
  };
  wire(child.stdout, '[out]');
  wire(child.stderr, '[err]');
}

/**
 * 探测 `/health` 用的地址。
 * @param {number} port
 * @param {string} [host]
 */
function healthUrl(port, host = '127.0.0.1') {
  return `http://${host}:${port}/health`;
}

/**
 * 控制台地址（带尾斜杠，避免 FastAPI 的 307 重定向多一次往返）。
 * @param {number} port
 * @param {string} [host]
 */
function consoleUrl(port, host = '127.0.0.1') {
  return `http://${host}:${port}/console/`;
}

module.exports = {
  isAlive,
  killTree,
  spawnBackend,
  pipeOutputTo,
  healthUrl,
  consoleUrl,
  detectPython,
  buildBackendEnv,
  truncate,
};
