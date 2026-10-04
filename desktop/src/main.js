'use strict';

/**
 * 云梦枢 · Electron 主进程。
 *
 * ==================== 它到底做什么 ====================
 * 这个项目**不是静态网站**：控制台由 FastAPI 后端伺服（`/console/`），
 * 后端还要 MySQL 与本地向量库。所以桌面壳的职责只有四件事：
 *
 *   1. 选一个**空闲端口**（绝不写死 8000 —— 用户可能已经占了它）；
 *   2. 拉起本机后端（`python -m uvicorn app.main:app`），日志落到 userData；
 *   3. 轮询 `/health`，就绪后把窗口导航到 `http://127.0.0.1:<port>/console/`；
 *   4. 退出时把后端子进程**整棵树**带走，不留残余。
 *
 * ==================== 刻意不做的三件事 ====================
 *   · 不用 `file://` 加载控制台。本项目前端是**原生 ES Module + importmap**，
 *     走 file:// 会踩模块 MIME / 相对路径 / CORS 一堆问题；后端伺服它本来就是对的。
 *   · 不引入打包器改 `web/`。前端"零构建"是既有的硬约定。
 *   · 不把 `.env` 或任何密钥打进安装包：配置只在仓库根 / userData 下。
 *
 * ==================== 安全立场 ====================
 *   `contextIsolation: true` + `nodeIntegration: false` + `sandbox: true`，
 *   预加载只暴露一组**具名、只读**的能力（见 preload.js），不暴露 ipcRenderer 本身。
 *   这与项目"插件从不执行第三方代码"的立场一致。
 */

const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');

const { resolvePaths, ensureDir } = require('./paths');
const {
  detectPython, choosePort, preferredPort, truncate, sidecarDataEnv, buildBackendEnv, isTruthy,
} = require('./backend-config');
const setupConfig = require('./setup-config');
const { sidecarCandidates, chooseBackend } = require('./sidecar');
const { isPackagedLayout, allowRepoFallback } = require('./packaging');
const { isPortBusy } = require('./port-check');
const { pickFreePort, waitForHealth } = require('./net-utils');
const {
  spawnBackend, pipeOutputTo, killTree, isAlive, healthUrl, consoleUrl,
} = require('./backend-process');

// ------------------------------------------------------------------
//  常量
// ------------------------------------------------------------------
/** 单实例锁的名字（同一台机器上开两个桌面版会抢端口与数据库） */
const SINGLE_INSTANCE_KEY = 'yunmeng-hub-desktop-single-instance';
/** 首次启动最多等多久（要给 ChromaDB 加载本地 ONNX 模型留时间） */
const HEALTH_TIMEOUT_MS = 180000;

// ------------------------------------------------------------------
//  全局状态
// ------------------------------------------------------------------
// ★ 允许用环境变量换一个"用户目录"：自动化验收要在**全新空配置**下跑首启向导，
//   而正常路径会写进用户真实的 `%APPDATA%\云梦枢`。有它才能在不动用户数据的前提下
//   测"第一次打开应用"这条路。
if (process.env.HNE_DESKTOP_PROFILE) {
  try {
    app.setPath('userData', path.resolve(process.env.HNE_DESKTOP_PROFILE));
  } catch {
    /* 设置失败就退回默认目录 */
  }
}

// ★ 形态判定只做一次，之后由 `packaged` 这个变量说话。
//   判据不是 `app.isPackaged` 一条：自测要能"假装打包态"（见 packaging.js）。
let packaged = isPackagedLayout({ resourcesPath: process.resourcesPath, isPackaged: app.isPackaged });

// ------------------------------------------------------------------
//  仓库根 / 安装目录（打包态下它是只读的安装位置，不该往里写任何东西）
// ------------------------------------------------------------------
/**
 * 仓库根。开发态就是仓库；打包态下 Electron 的 `__dirname` 在 asar 里，
 * 所以要回到 `resources/` 才能与"安装目录"这个说法对上。
 */
function computeAppRoot() {
  return packaged
    ? path.resolve(process.resourcesPath || path.resolve(__dirname, '..', '..'))
    : path.resolve(__dirname, '..', '..');
}

const paths = resolvePaths({
  baseDir: __dirname,
  appRoot: computeAppRoot(),
  userDataDir: safeUserDataDir(),
  packaged,
});

let mainWindow = null;
let backendChild = null;
let pumpInFlight = null;
/**
 * 当前处在哪一步：`'setup' | 'pumping' | 'console'`。
 * ★ 这个状态是必要的：菜单和"关于"里要知道现在能不能提到控制台，
 *   而且设置阶段**不该**去碰后端（那时候还没有可用配置）。
 */
let step = 'setup';
/** 供"关于"菜单与加载页展示的一份快照（**不含任何密钥**） */
let state = {
  phase: 'idle',
  message: '尚未开始',
  consoleUrl: null,
  healthUrl: null,
  port: null,
  portSource: null,
  pythonPath: null,
  backendMode: null,
  backendCommand: null,
  backendPid: null,
  logsDir: paths.logsDir,
  backendLogFile: paths.backendLogFile,
  error: null,
  hint: null,
  detail: null,
  startedAt: null,
};

/** userData 在 app 未就绪时不能问 Electron，所以先兜底再在 ready 后校正。 */
function safeUserDataDir() {
  try {
    return app.getPath('userData');
  } catch {
    return path.join(process.env.LOCALAPPDATA || process.env.TEMP || '.', 'yunmeng-hub-desktop');
  }
}

function refreshUserDataPaths() {
  // ★ 顺便把形态判定再确认一次：`app.isPackaged` 在 ready 之后才保证可读，
  //   而 `packaged` 一旦为真就不再回退（避免"两次判定不一致"这种最难查的状态）。
  packaged = packaged || isPackagedLayout({
    resourcesPath: process.resourcesPath,
    isPackaged: app.isPackaged,
  });
  const resolved = resolvePaths({
    baseDir: __dirname,
    appRoot: computeAppRoot(),
    userDataDir: app.getPath('userData'),
    packaged,
  });
  Object.assign(paths, resolved);
  state.logsDir = paths.logsDir;
  state.backendLogFile = paths.backendLogFile;
}

// ------------------------------------------------------------------
//  日志（桌面壳自己的日志，与后端日志分开两个文件）
// ------------------------------------------------------------------
let desktopLogStream = null;

function openDesktopLog() {
  try {
    ensureDir(paths.logsDir);
    desktopLogStream = fs.createWriteStream(paths.desktopLogFile, { flags: 'a' });
  } catch (error) {
    desktopLogStream = null;
    logLine(`[warn] 无法创建桌面日志：${error.message}`);
  }
}

function logLine(line) {
  const stamp = new Date().toISOString();
  const text = `${stamp} ${line}`;
  try {
    if (desktopLogStream) desktopLogStream.write(`${text}\n`);
  } catch {
    /* 日志写失败不能拖垮应用 */
  }
  if (process.env.HNE_DESKTOP_DEBUG) process.stdout.write(`${text}\n`);
}

/** 后端输出：既落盘，也（在调试模式下）回显。 */
function openBackendLog() {
  try {
    ensureDir(paths.logsDir);
    return fs.createWriteStream(paths.backendLogFile, { flags: 'a' });
  } catch (error) {
    logLine(`[warn] 无法创建后端日志：${error.message}`);
    return null;
  }
}

/**
 * 打一行**机器可读**的状态（纯 ASCII）。
 *
 * ★ 为什么不复用上面那些中文日志：日志是 UTF-8 写的，而 Windows PowerShell 5.1 的
 *   `Get-Content` 默认按 ANSI 解 —— 于是验收脚本去匹配中文时会全部落空
 *   （实测：应用明明是打包态、明明进了设置页，脚本却报"没有确认打包形态""没有进设置页"）。
 *   把判据做成 ASCII，就不必让验收脚本去赌编码。
 */
function logState(tag) {
  logLine(`[state] ${tag} packaged=${packaged} repoFallback=${repoFallbackAllowed()}`
    + ` step=${step} phase=${state.phase} port=${state.port ?? '-'} mode=${state.backendMode ?? '-'}`);
}

/** 切换"当前处在哪一步"（唯一入口），并留下一行可读的状态痕迹。 */
function setStep(next) {
  step = next;
  logState('step-change');
}

// ------------------------------------------------------------------
//  状态广播
// ------------------------------------------------------------------
function setState(patch) {
  state = { ...state, ...patch };
  logState('state-change');
  if (mainWindow && !mainWindow.isDestroyed()) {
    try {
      mainWindow.webContents.send('app:state', publicState());
    } catch {
      /* 窗口正在销毁 */
    }
  }
}

/** 给渲染进程的那一份（保证不含任何凭据 —— 这里本来就只放路径与端口）。 */
function publicState() {
  return {
    phase: state.phase,
    message: state.message,
    consoleUrl: state.consoleUrl,
    healthUrl: state.healthUrl,
    port: state.port,
    portSource: state.portSource,
    pythonPath: state.pythonPath,
    backendMode: state.backendMode,
    backendCommand: state.backendCommand,
    backendPid: state.backendPid,
    logsDir: state.logsDir,
    backendLogFile: state.backendLogFile,
    error: state.error,
    hint: state.hint,
    detail: state.detail,
  };
}

// ------------------------------------------------------------------
//  启动流程
// ------------------------------------------------------------------

/** 把失败信息整理成"用户能照着做"的一段话（只说事实，不猜）。 */
function describeStartupFailure(kind, extra = {}) {
  const base = {
    message: '后端没有起来',
    hint: '常见原因：①MySQL 没启动；②仓库根缺少 .env（或里面的 HNE_MYSQL_* 不对）；'
      + '③python 依赖没装（本机缺 uvicorn）；④端口被别的程序占用。',
    detail: [
      `后端日志：${state.backendLogFile}`,
      `桌面日志：${paths.desktopLogFile}`,
      '手动复现：在仓库根执行  python -m uvicorn app.main:app --host 127.0.0.1 --port <端口>',
    ].join('\n'),
  };

  if (kind === 'no-sidecar') {
    // ★ 打包态与开发态的"怎么办"完全不同：装出来的那份里**没有**仓库、
    //   也没有构建脚本，让用户去跑 build_backend.ps1 是错的指引。
    return packaged ? {
      message: '安装目录里缺少后端程序（resources\\backend）',
      hint: '这通常是安装不完整或被安全软件删掉了文件。'
        + '请卸载后重新运行安装包；若反复出现，请检查杀毒软件是否拦截了 resources\\backend 下的文件。',
      detail: [
        '你指定的后端：' + (extra.explicit || '(未指定)'),
        `安装目录：${paths.appRoot}`,
        `后端日志：${state.backendLogFile}`,
        `桌面日志：${paths.desktopLogFile}`,
      ].join('\n'),
    } : {
      message: '你指定的打包后端（HNE_DESKTOP_BACKEND）不存在',
      hint: '请检查这个路径是否正确，或清掉 HNE_DESKTOP_BACKEND 让它自动查找'
        + '（会去找 desktop/dist/backend/backend.exe，找不到再退回本机 Python）。',
      detail: [
        `你指定的是：${extra.explicit || '(空)'}`,
        `后端日志：${state.backendLogFile}`,
        `桌面日志：${paths.desktopLogFile}`,
        '自己构建打包后端：pwsh -File desktop/scripts/build_backend.ps1',
      ].join('\n'),
    };
  }
  if (kind === 'no-python') {
    const explicitFailed = Boolean(extra.strictExplicit);
    return {
      message: explicitFailed
        ? '你指定的 Python 解释器（HNE_DESKTOP_PYTHON）用不了'
        : (packaged
          ? '安装目录里缺少后端程序，也没找到本机 Python'
          : '找不到可用的 Python（需要装了 uvicorn 的解释器）'),
      hint: explicitFailed
        ? '因为设了 HNE_DESKTOP_PYTHON_STRICT，这里**不会**悄悄换用别的解释器。'
          + '请修好这个路径，或清掉 HNE_DESKTOP_PYTHON / HNE_DESKTOP_PYTHON_STRICT 让它自动探测。'
        : (packaged
          ? '安装包本该自带后端（resources\\backend\\backend.exe）。'
            + '请卸载后重装；若杀毒软件拦过它，请先放行再装。'
          : '在仓库根建虚拟环境并安装依赖：python -m venv .venv 然后 '
            + '.venv\\Scripts\\python.exe -m pip install -r requirements.txt。'
            + '也可以设环境变量 HNE_DESKTOP_PYTHON 直接指定解释器路径，'
            + '再用 HNE_DESKTOP_PYTHON_STRICT=1 要求"只用它、不将就"。'),
      detail: extra.candidates
        ? extra.candidates.map((c) => `· ${c.path} → ${c.ok ? '可用' : c.reason}`).join('\n')
        : base.detail,
    };
  }
  if (kind === 'exited') {
    return {
      message: '后端起进程后立刻退出了',
      hint: '最常见的是 MySQL 连不上或 .env 配置不对。日志末尾通常就是真正的原因。',
      detail: `${base.detail}\n退出码：${extra.code === null || extra.code === undefined ? '（信号终止）' : extra.code}`,
    };
  }
  if (kind === 'timeout') {
    return {
      message: `等了 ${Math.round(HEALTH_TIMEOUT_MS / 1000)} 秒，后端仍没有响应健康检查`,
      hint: '首次启动要加载本地 ONNX 嵌入模型（约 80MB，首次可能联网下载），慢是正常的；'
        + '但如果反复超时，请看日志末尾有没有报错。',
      detail: base.detail,
    };
  }
  if (kind === 'spawn') {
    return {
      message: '无法启动后端进程',
      hint: base.hint,
      detail: `${base.detail}\n系统报错：${extra.error || '未知'}`,
    };
  }
  return base;
}

/** 环境变量里的"真"由 backend-config 的 isTruthy 负责（见那里的说明）。 */

// ------------------------------------------------------------------
//  配置来源：开发态用仓库根的 .env，打包/向导态用 userData 下的
// ------------------------------------------------------------------
/**
 * 是否允许"开发态兜底"：仓库根的 `.env` 也算配置来源，并在向导保存后同步到那里。
 *
 * ★ 为什么需要这个开关：
 *   · 开发时它很省事（`npm start` 直接用仓库根的配置，不必过一遍向导）；
 *   · 但它**会掩盖打包版的行为** —— 打包版安装目录里没有 `.env`，用户装完必然进向导。
 *     本轮做"首启向导"的验收时就被它骗了一次：我明明把仓库 `.env` 挪走了，
 *     可向导一保存就把配置**同步回仓库根**，下一个场景于是又走了兜底，
 *     测出来的根本不是打包版的路径。
 *   · 另外它对"想按用户机的方式试一下"的人也有用（HNE_DESKTOP_STRICT_CONFIG=1）。
 *
 * ★★ 阶段 3 补上的一刀（之前只考虑了开发机）：**打包态一律不允许**。
 *   判断逻辑在 `packaging.js`（纯函数、有自测），这里只负责把三个事实喂进去。
 *   不这么做的后果，实测过一次：装出来的那份 `resources/` 恰好是**仓库根的父目录**
 *   关系，于是它会去读 `resources/.env` —— 权限上写不进去（EPERM），
 *   安全上更糟：那个文件若存在，应用会拿**别人的**数据库口令去连库。
 *   "绝不把 .env 打进安装包"只是这条红线的第一半，第二半是"打包态绝不去读它"。
 */
function repoFallbackAllowed() {
  return allowRepoFallback({
    packaged,
    strictConfig: isTruthy(process.env.HNE_DESKTOP_STRICT_CONFIG),
  });
}

/** 这次启动实际用哪个 .env（null = 还没有任何配置）。 */
function resolveBackendEnvFile() {
  const userText = setupConfig.readEnvFile(paths.userEnvFile);
  const userOk = userText !== null && setupConfig.configIsUsable(setupConfig.parseEnvText(userText));
  logLine(`[info] 配置判定：userData ${paths.userEnvFile} 存在=${userText !== null} 可用=${userOk}`
    + (userText !== null && !userOk ? `（缺 ${setupConfig.missingRequired(setupConfig.parseEnvText(userText)).join(',')}）` : ''));
  if (userOk) return paths.userEnvFile;

  if (!repoFallbackAllowed()) {
    logLine('[info] 配置判定：已禁用"仓库根兜底"（HNE_DESKTOP_STRICT_CONFIG=1），不再看仓库根');
    return null;
  }

  const repoText = setupConfig.readEnvFile(paths.envFile);
  const repoOk = repoText !== null && setupConfig.configIsUsable(setupConfig.parseEnvText(repoText));
  logLine(`[info] 配置判定：仓库根 ${paths.envFile} 存在=${repoText !== null} 可用=${repoOk}`);
  if (repoOk) return paths.envFile;
  return null;
}

/** 是否需要先跑首启向导。 */
function needsSetup() {
  // ★ `--setup` 强制进设置页：给"我想改配置/换数据库"用，不必卸载重装。
  if (process.argv.includes('--setup')) {
    logLine('[info] 命令行带了 --setup，强制进入首次设置');
    return true;
  }
  const chosen = resolveBackendEnvFile();
  if (chosen) {
    // ★ 开发态的常见情况：仓库根有 .env（开发者自己维护），于是直接进控制台。
    //   这条兜底是为开发方便；打包版安装目录里**不带 .env**，所以用户装完一定进向导。
    logLine(`[info] 已有可用配置，直接启动：${chosen}`);
    if (chosen === paths.envFile) {
      logLine('[info] （这份来自仓库根 —— 开发态兜底；打包版不会有它）');
    }
    return false;
  }
  logLine('[info] 没有任何可用配置（userData 与仓库根都没有）—— 进入首次设置');
  return true;
}

/**
 * 启动时确定"这次用哪份配置"，并在仓库根没有、userData 有一份时**把它同步到仓库根**。
 *
 * ★ 为什么要同步：开发模式（`npm start`）下后端就是 `python -m uvicorn`，
 *   它的 cwd 是仓库根，读的也是仓库根那份 `.env`。如果向导写到了别处，
 *   就会出现"向导说配置好了，可后端读的还是老配置"这种最难查的不一致。
 *   同步用的是**复制**：userData 那份是权威，仓库根那份只是给开发态用的镜像。
 *   不会覆盖已经存在且可用的仓库根配置。
 */
function ensureInitialConfig() {
  if (!repoFallbackAllowed()) return; // 严格模式：不往仓库根写任何东西

  const userText = setupConfig.readEnvFile(paths.userEnvFile);
  if (userText === null || !setupConfig.configIsUsable(setupConfig.parseEnvText(userText))) return;

  const repoText = setupConfig.readEnvFile(paths.envFile);
  if (repoText !== null && setupConfig.configIsUsable(setupConfig.parseEnvText(repoText))) return;

  try {
    fs.copyFileSync(paths.userEnvFile, paths.envFile);
    logLine('[info] 已把 userData 里的配置同步到仓库根 .env（开发模式下后端读的是后者）');
  } catch (error) {
    logLine(`[warn] 同步配置到仓库根失败：${error.message}`);
  }
}

/** 读这次要用的配置键值（用于注入给后端子进程）。 */
function loadBackendEnvValues() {
  const envFile = resolveBackendEnvFile() || paths.userEnvFile;
  return setupConfig.parseEnvText(setupConfig.readEnvFile(envFile) || '');
}

// ----------------------------------------------------------------------
//  配置自检：用真正的后端起一次、逐项检查组件
// ----------------------------------------------------------------------
/**
 * 用给定的 `.env` 起一次后端并做**深度**自检。
 *
 * ★ 为什么"测试连接"要真的起后端、而不是在 Node 里连一下 MySQL：
 *   本项目的 `/health` 会逐项报告 database / vector_store 的状态，
 *   而这两样正是用户装不上时最可能出问题的地方（口令错、库没建、向量库目录不可写）。
 *   在 Node 里只测 MySQL 会把"向量库挂了"漏掉 —— 那就成了"测试通过但一用就坏"。
 *   ★ 顺带的好处：**不需要引入 mysql2 之类的额外依赖**，桌面壳依旧零 runtime 依赖。
 */
function selfCheckWithEnv(envFile, label) {
  return new Promise((resolve) => {
    const envFileValues = setupConfig.parseEnvText(setupConfig.readEnvFile(envFile) || '');
    const explicitBackend = process.env.HNE_DESKTOP_BACKEND
      ? String(process.env.HNE_DESKTOP_BACKEND).trim() : null;
    const choice = chooseBackend({
      explicit: explicitBackend,
      candidates: explicitBackend ? [explicitBackend] : sidecarCandidates({
        desktopDir: paths.desktopDir,
        appRoot: paths.appRoot,
        resourcesPath: process.resourcesPath,
        execDir: path.dirname(app.getPath('exe')),
        platform: process.platform,
      }),
    });

    if (choice.kind === 'none') {
      resolve({
        ok: false,
        message: '没有可用的后端（既没有打包后端 backend.exe，也没找到本机 Python）',
        detail: '先构建打包后端：pwsh -File desktop/scripts/build_backend.sh（Windows 用 build_backend.ps1）\n'
          + '或按项目 README 在仓库根建好 .venv 并安装 requirements.txt。',
      });
      return;
    }

    const port = 45000 + Math.floor(Math.random() * 3000); // 自检用的临时端口
    const args = choice.kind === 'sidecar'
      ? ['--self-check', '--host', '127.0.0.1', '--port', String(port),
        '--data-dir', paths.userDataDataDir, '--log-dir', paths.logsDir,
        '--env-file', envFile, '--self-check-timeout', '240']
      : ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', String(port)];

    logLine(`[info] 配置自检（${label}）：用 ${choice.kind} 起临时后端，端口 ${port}`);
    logLine(`[info] 自检命令：${choice.command} ${args.join(' ')}`);

    const env = buildBackendEnv({
      baseEnv: process.env,
      fileEnv: envFileValues,
      overrides: { HNE_HOST: '127.0.0.1', HNE_PORT: String(port), HNE_ENV_FILE: envFile },
    });

    let output = '';
    let child;
    try {
      child = spawn(choice.command, args, {
        cwd: choice.kind === 'sidecar' ? path.dirname(choice.command) : paths.appRoot,
        env,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      resolve({ ok: false, message: '无法启动后端进程', detail: String(error && error.message) });
      return;
    }

    const collect = (chunk) => {
      output += chunk;
      if (output.length > 200000) output = output.slice(-100000);
    };
    if (child.stdout) child.stdout.setEncoding('utf8'), child.stdout.on('data', collect);
    if (child.stderr) child.stderr.setEncoding('utf8'), child.stderr.on('data', collect);

    // 开发模式（python -m uvicorn）没有 --self-check，就自己探活
    const probe = choice.kind === 'sidecar' ? null : setInterval(() => {
      fetch(`http://127.0.0.1:${port}/health`)
        .then((r) => r.json())
        .then((payload) => {
          if (payload && payload.status === 'ok') finish(true, payload);
        })
        .catch(() => { /* 还没起来 */ });
    }, 1500);

    let settled = false;
    const timer = setTimeout(() => finish(false, null, `自检超时（${label}）`), 300000);

    function finish(ok, payload, message) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (probe) clearInterval(probe);
      if (child.pid) killTree(child.pid);
      if (ok) {
        const components = (payload && payload.components) || {};
        const bad = Object.entries(components)
          .filter(([, info]) => (info || {}).status !== 'ok')
          .map(([name, info]) => `${name}=${(info || {}).status}`);
        if (bad.length) {
          const summary = summarizeComponentError(components);
          resolve({
            ok: false,
            message: summary ? `有的组件不可用 —— ${summary.split('\n')[0]}` : '后端起来了，但有的组件不可用',
            // 第一行是后端自己的结论（比如 MySQL 拒绝登录），后面才是日志尾部。
            // 页面上这段默认折叠在「查看详情」里，所以这里可以放心留全。
            detail: [summary, '', '后端日志末尾：', tailLines(output, 8)].filter(Boolean).join('\n'),
          });
          return;
        }
        resolve({ ok: true, message: '连接正常：数据库与向量库都已就绪。' });
        return;
      }
      resolve({
        ok: false,
        message: message || '后端没能就绪',
        detail: tailLines(output, 14),
      });
    }

    child.once('exit', (code) => {
      // ★ 这里踩过一个坑（实测抓到）：`exit` 事件**先于** `close` 触发，
      //   而原版把"进程退出了"一律判成失败 —— 于是 sidecar 自检
      //   **组件全 ok、退出码 0**，界面却显示"起不来后端"（详情里只有一条
      //   关停 uvicorn 留下的 CancelledError 噪音）。
      //   现在分情况：退出码非 0 才是失败；退出码 0 交给后面的 `close` 处理器
      //   按后端自己的结论（`[self-check] OK`）判定。
      if (settled) return;
      if (code !== 0) {
        finish(false, null, `后端进程异常退出（退出码 ${code}）`);
      } else if (choice.kind !== 'sidecar') {
        // 非 sidecar（python -m uvicorn）没有自检模式，正常退出就是没起来
        finish(false, null, '后端进程退出了（退出码 0）');
      } else {
        logLine('[info] 自检子进程退出码 0，等 close 事件按后端结论判定');
      }
    });
    if (choice.kind === 'sidecar') {
      // sidecar 自己会探活并退出。
      // ★ 判据用**后端自己的结论**（它打印的 `[self-check] OK  所有组件均为 ok`），
      //   而不是"猜日志里像不像错误"：
      //   自检结束时会刻意关掉 uvicorn，那会留下一条
      //   `Traceback ... asyncio.exceptions.CancelledError` —— 它**不是故障**，
      //   但我的 tailLines 会优先抓 "Error/Traceback"，于是把它当成失败原因报给用户
      //   （实测：组件全 ok、退出码 0，界面却显示"起不来后端"）。
      //   用后端自己的结论 + 退出码，就不会被这类噪音误导。
      child.once('close', (code) => {
        if (settled) return;
        const verdictOk = output.includes('[self-check] OK');
        logLine(`[info] 自检子进程结束：退出码=${code}，后端结论=${verdictOk ? 'ok' : '未报告成功'}`);
        if (code === 0 && verdictOk) finish(true, { status: 'ok', components: {} });
        else if (code === 0) finish(false, null, '自检报告失败（后端组件没全就绪）');
        else finish(false, null, `自检失败（退出码 ${code}）`);
      });
    }
  });
}

/**
 * 从组件状态里挑出**最该给用户看**的那一句，再附上日志尾部。
 *
 * ★ 为什么要挑：原来的 detail 是"日志里最后 14 行像错误的行"，
 *   而用户填错口令时会连续刷出十几行 Traceback（SQLAlchemy 的
 *   `1045 Access denied ... using password: YES`、Background on this error、
 *   asyncio.CancelledError …）。结论被埋在中间，用户根本抓不到重点。
 *   现在把后端**已经算好的结论**（`components.database.message`）提到最前面 ——
 *   那本来就是它自己的判断，比我去猜日志可靠得多。
 */
function summarizeComponentError(components) {
  const bad = Object.entries(components || {})
    .filter(([, info]) => (info || {}).status !== 'ok');
  if (!bad.length) return '';
  const lines = bad.map(([name, info]) => {
    const why = String((info || {}).message || '').split('\n')[0].trim();
    return why ? `${name}：${why}` : `${name}：状态 ${(info || {}).status}`;
  });
  return lines.join('\n');
}

/** 取输出里最后几行"像错误"的内容（日志很长，直接全贴给用户没人看得完）。 */
function tailLines(text, count) {
  const lines = String(text || '').split(/\r?\n/).filter((l) => l.trim());
  const interesting = lines.filter((l) => /ERROR|Error|error|失败|Traceback|Exception/.test(l));
  const picked = (interesting.length ? interesting : lines).slice(-count);
  return picked.join('\n') || '（后端没有输出任何日志）';
}

/**
 * 想优先用哪个端口。
 *
 * ★ 实现在 `backend-config.js` 的 `preferredPort`（那里能单独测）。
 *   这里只做转发，顺便把"默认是 null"这件事在本文件里也写清楚：
 *   **返回值可能是 null，每个使用点都要处理**。
 */
function preferredPortForRun(envFileValues) {
  return preferredPort(envFileValues, process.env);
}

async function startBackend() {
  const envFile = resolveBackendEnvFile() || paths.userEnvFile;
  const envFileValues = loadBackendEnvValues();
  if (Object.keys(envFileValues).length === 0) {
    logLine(`[warn] 没读到任何配置（${envFile}）—— 后端大概率连不上数据库`);
  } else {
    logLine(`[info] 配置来源：${envFile}（${Object.keys(envFileValues).length} 项）`);
  }

  const preferred = preferredPortForRun(envFileValues);
  logLine(`[info] 优先端口：${preferred === null ? '（不指定，用系统分配的空闲端口）' : preferred}`);

  // ---- 情况 1：优先端口上已经有一个健康的后端（用户自己按文档起的）→ 直接用 ----
  // ★ 为什么先探这个而不是先起：用户可能已经开着后端（开发中很常见），
  //   再起一个必然端口冲突；而且"发现已有后端"比"报端口占用"友好得多。
  // ★ 只有用户**显式指定**端口时才这么做：默认（null）时我们不去打扰别人的 8000。
  //
  // ★★ 这里踩过一个坑（用户截图抓到的）：`preferred` 默认是 **null**，
  //    而 `net.connect({ port: null })` 会直接抛
  //    `The "options.port" property must be one of type number or string` ——
  //    于是**默认路径（不指定端口）直接启动失败**，而显式指定端口时一切正常。
  //    我自己的验收脚本恰好都显式传了端口，所以这条新默认路径从没被走过。
  //    教训：**改了默认值，就必须测新的默认值**，不能只测旧的那条。
  if (preferred !== null && await isPortBusy(preferred, '127.0.0.1')) {
    const existing = await waitForHealth({
      url: healthUrl(preferred),
      timeoutMs: 8000,
      intervalMs: 400,
    });
    if (existing.ok) {
      logLine(`[info] ${preferred} 上已有健康的后端，直接使用（不另起进程）`);
      setState({
        phase: 'ready',
        port: preferred,
        portSource: 'existing',
        healthUrl: healthUrl(preferred),
        consoleUrl: consoleUrl(preferred),
        pythonPath: null,
        backendPid: null,
        message: `已连接已在运行的本地后端（端口 ${preferred}）`,
        error: null,
        hint: null,
        detail: null,
      });
      return true;
    }
    logLine(`[warn] ${preferred} 被占用，但健康检查不通过 —— 改用其它端口`);
  }

  // ---- 情况 2：需要我们自己起一个 ----
  const freePort = await pickFreePort({ host: '127.0.0.1' });
  // ★★ 关键：只有**用户显式指定了端口**时才去判断"它是否空闲"。
  //    默认（preferred === null）就直接用系统刚分配的空闲端口。
  //    第一版这里写的是 `preferredIsFree: !(await isPortBusy(preferred, ...))` ——
  //    于是默认路径会把 `null` 传给 `net.connect({ port: null })`，
  //    抛 `The "options.port" property must be one of type number or string`。
  //    上面那句"已有后端"的探测我加了 null 守卫，**却漏了这一句** ——
  //    同一个空值在两处使用，只守了一处。这是本轮最该记住的教训。
  const preferredIsFree = preferred === null
    ? false
    : !(await isPortBusy(preferred, '127.0.0.1'));
  const chosen = choosePort({
    preferredPort: preferred,
    freePort,
    preferredIsFree,
  });
  logLine(`[info] 端口选定 ${chosen.port}（来源 ${chosen.source}）`);

  // ---- 决定用"打包后端"还是"本机 Python" ----
  // ★ 顺序是刻意的：**先找打包后端**。有 backend.exe 就用它（用户机不需要装 Python），
  //   没有才退回本机 Python（开发机、以及四件套验收的正常情况）。
  //
  // ★ 这里踩过一个坑（实测抓到的，两次）：
  //   ① `explicit` 必须传进 sidecarCandidates，否则用户指定的那个路径**永远不会被检查**，
  //      而 chooseBackend 仍会返回 explicitSpecified=true —— 日志里写着"使用打包后端"，
  //      用户指定的却是另一个；
  //   ② 更隐蔽的一层：`HNE_DESKTOP_BACKEND` 是**显式指定**，
  //      所以它必须是**唯一**候选。第一版把"默认候选"也一起交给 chooseBackend，
  //      于是"指定的那个不存在"会**悄悄落到默认的 backend.exe** 上 ——
  //      用户明明指了 A、程序用了 B，还是一句提示都没有。
  //   现在的规则很硬：**指定了，就只认它；它不在就失败。**
  const explicitBackend = process.env.HNE_DESKTOP_BACKEND
    ? String(process.env.HNE_DESKTOP_BACKEND).trim()
    : null;

  const sidecarChoice = chooseBackend({
    explicit: explicitBackend,
    candidates: explicitBackend
      ? [explicitBackend] // 显式指定 = 只认它（不掺默认候选，避免"悄悄用到别的"）
      : sidecarCandidates({
        desktopDir: paths.desktopDir,
        appRoot: paths.appRoot,
        resourcesPath: process.resourcesPath,
        execDir: path.dirname(app.getPath('exe')),
        platform: process.platform,
      }),
  });

  let mode;
  let command;
  let pythonPath = null;

  if (sidecarChoice.kind === 'sidecar') {
    mode = 'sidecar';
    command = sidecarChoice.command;
    logLine(`[info] ${sidecarChoice.reason}`);
  } else if (sidecarChoice.explicitSpecified) {
    // ★ 用户显式指定了后端却找不到 → **明确失败**，不许改去探测本机 Python。
    //   （`explicitSpecified` 是显式字段：第一版我用 reason 字符串前缀来判断，
    //     而调用点在这个分支之前就传了 python:null，判据根本没命中，于是静默回退。）
    const failure = describeStartupFailure('no-sidecar', { explicit: process.env.HNE_DESKTOP_BACKEND });
    logLine(`[error] ${failure.message}`);
    for (const candidate of sidecarChoice.checked) {
      logLine(`        已查找：${candidate}`);
    }
    setState({ phase: 'failed', port: chosen.port, portSource: chosen.source, ...failure });
    return false;
  } else {
    // 退回本机 Python
    const detection = detectPython({ appRoot: paths.appRoot, env: process.env });
    if (!detection.python) {
      const failure = describeStartupFailure('no-python', {
        candidates: detection.candidates,
        strictExplicit: detection.strictExplicit,
      });
      logLine(`[error] ${failure.message}`);
      for (const candidate of detection.candidates) {
        logLine(`        · ${candidate.path} → ${candidate.ok ? '可用' : candidate.reason}`);
      }
      setState({ phase: 'failed', port: chosen.port, portSource: chosen.source, ...failure });
      return false;
    }
    mode = 'python';
    command = detection.python;
    pythonPath = detection.python;
    logLine(`[info] 没有打包后端，改用本机 Python：${detection.python}`);
  }

  // ★ 打包版的数据要落在 userData（安装目录之外），否则"卸载重装不丢数据"不成立。
  //   非打包版**不注入**这些，让它照旧读 .env —— 保证开发/验收时的数据位置一字不变。
  const envOverrides = {
    // ★ 告诉后端用哪份 .env（打包版的后端不在仓库里跑，读不到仓库根那份）。
    //   两种模式都传：这样"向导写的配置"对 python 模式同样生效，行为一致。
    HNE_ENV_FILE: envFile,
    ...(mode === 'sidecar' ? sidecarDataEnv({ dataDir: paths.userDataDataDir }) : {}),
  };

  const spawnOptions = {
    appRoot: paths.appRoot,
    python: command,
    port: chosen.port,
    host: '127.0.0.1',
    mode,
    fileEnv: envFileValues,
    env: process.env,
    envOverrides,
  };
  if (mode === 'sidecar') {
    // sidecar 的 cwd 用资源目录：它不该假设自己站在仓库里
    spawnOptions.cwd = path.dirname(command);
    spawnOptions.extraArgs = [
      '--data-dir', paths.userDataDataDir,
      '--log-dir', paths.logsDir,
      // 显式告诉 sidecar 读哪份配置（双保险：环境变量里也有一份，见 envOverrides）
      '--env-file', envFile,
    ];
  }

  const { child, pid } = spawnBackend(spawnOptions);

  backendChild = child;
  setState({
    phase: 'starting',
    port: chosen.port,
    portSource: chosen.source,
    healthUrl: healthUrl(chosen.port),
    pythonPath,
    backendMode: mode,
    backendCommand: command,
    backendPid: pid,
    message: `${mode === 'sidecar' ? '打包后端' : '本机 Python 后端'}启动中（端口 ${chosen.port}，PID ${pid}）…`,
    error: null,
    hint: null,
    detail: null,
  });
  logLine(`[info] 已启动后端 PID=${pid} port=${chosen.port} mode=${mode}`);

  const backendLogStream = openBackendLog();
  const writers = [desktopLogStream, backendLogStream].filter(Boolean);
  pipeOutputTo(child, (line) => {
    for (const stream of writers) {
      try {
        stream.write(`${line}\n`);
      } catch {
        /* 忽略 */
      }
    }
    if (process.env.HNE_DESKTOP_DEBUG) process.stdout.write(`${line}\n`);
  });

  let exitedEarly = false;
  let exitCode = null;
  child.once('exit', (code, signal) => {
    exitedEarly = true;
    exitCode = signal ? null : code;
    logLine(`[info] 后端进程退出 code=${code} signal=${signal}`);
    if (backendChild === child) backendChild = null;
    if (!app.isQuitting) {
      setState({
        phase: 'stopped',
        message: `后端进程已退出（code=${code === null ? '信号' : code}）`,
      });
    }
  });

  const health = await waitForHealth({
    url: healthUrl(chosen.port),
    timeoutMs: HEALTH_TIMEOUT_MS,
    intervalMs: 700,
    shouldAbort: () => exitedEarly || app.isQuitting,
    onProgress: (info) => {
      setState({
        phase: 'starting',
        message: `等待后端就绪…（第 ${info.attempt} 次检查，${Math.round(info.elapsedMs / 1000)} 秒）`
          + (info.error ? `\n最近一次：${info.error}` : ''),
      });
    },
  });

  if (health.ok) {
    setState({
      phase: 'ready',
      consoleUrl: consoleUrl(chosen.port),
      message: `后端就绪（端口 ${chosen.port}，${(health.elapsedMs / 1000).toFixed(1)} 秒）`,
      error: null,
      hint: null,
      detail: null,
    });
    logLine(`[info] 健康检查通过（${health.attempts} 次尝试，${health.elapsedMs}ms）`);
    return true;
  }

  if (exitedEarly) {
    const failure = describeStartupFailure('exited', { code: exitCode });
    logLine(`[error] 后端提前退出：${failure.message}`);
    setState({ phase: 'failed', ...failure });
    return false;
  }

  if (health.reason === 'abandoned') {
    logLine('[warn] 等待健康检查被中止（应用正在退出）');
    return false;
  }

  const failure = describeStartupFailure('timeout');
  logLine(`[error] 健康检查超时（最后错误：${health.lastError || health.lastStatus || '无'}）`);
  setState({ phase: 'failed', ...failure });
  return false;
}

/** 启动流程的串行化入口（重试按钮与首次启动共用）。 */
function pumpBackend() {
  if (pumpInFlight) return pumpInFlight;
  pumpInFlight = (async () => {
    try {
      return await startBackend();
    } catch (error) {
      const failure = describeStartupFailure('spawn', { error: truncate(String(error && error.message), 300) });
      logLine(`[error] 启动流程异常：${error && error.stack ? error.stack : error}`);
      setState({ phase: 'failed', ...failure });
      return false;
    } finally {
      pumpInFlight = null;
    }
  })();
  return pumpInFlight;
}

// ------------------------------------------------------------------
//  窗口
// ------------------------------------------------------------------
function iconOptions() {
  if (fs.existsSync(paths.packagedIcon)) return { icon: paths.packagedIcon };
  if (fs.existsSync(paths.runtimeIcon)) return { icon: paths.runtimeIcon };
  return {};
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 680,
    show: false,
    backgroundColor: '#0a0d17', // 与「星云暗涌」的 --bg 一致，避免启动时闪白
    title: '云梦枢',
    autoHideMenuBar: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webviewTag: false,
      spellcheck: false,
    },
    ...iconOptions(),
  });

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // ★ 外链一律交给系统浏览器：控制台里如果有指向文档/厂商的链接，
  //   绝不能在 Electron 窗口里打开（那等于把外部页面当成应用的一部分）。
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.loadFile(paths.loadingPage);
  return mainWindow;
}

/**
 * 打开"首次设置"页。
 *
 * ★ 为什么用同一个窗口而不是弹个模态框：设置完要**接着进控制台**，
 *   换个窗口会让用户看到两次闪动。这里先把加载页换成设置页，
 *   设置成功后主进程把它导航到控制台 URL（见 finishSetup）。
 */
function createSetupWindow() {
  mainWindow = new BrowserWindow({
    width: 940,
    height: 780,
    minWidth: 720,
    minHeight: 560,
    show: false,
    backgroundColor: '#0a0d17',
    title: '云梦枢 · 首次设置',
    autoHideMenuBar: true, // 首次设置时菜单是干扰；进控制台后菜单照常
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webviewTag: false,
      spellcheck: false,
    },
    ...iconOptions(),
  });

  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('closed', () => { mainWindow = null; });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });

  setStep('setup');
  mainWindow.loadFile(paths.setupPage);
  return mainWindow;
}

/**
 * 设置页保存后的收尾：把它切回加载页 → 用这份配置起后端 → 进控制台。
 *
 * ★ 这一段的顺序是刻意的：**先用"保存的配置"真的起一次后端**（自检），
 *   成功了才让用户看到"完成"。反过来的话，用户会先看到控制台在转圈、
 *   然后才发现配置是错的 —— 而他不知道该改哪里。
 */
async function finishSetup() {
  ensureInitialConfig();
  setStep('pumping');
  if (mainWindow && !mainWindow.isDestroyed()) {
    await mainWindow.loadFile(paths.loadingPage);
  }
  buildMenu();
  const ok = await pumpBackend();
  if (ok && state.consoleUrl && mainWindow && !mainWindow.isDestroyed()) {
    setStep('console');
    await mainWindow.loadURL(state.consoleUrl);
  }
}

// ------------------------------------------------------------------
//  菜单
// ------------------------------------------------------------------
function openPathSafely(target) {
  try {
    if (!fs.existsSync(target)) {
      dialog.showMessageBoxSync(mainWindow || undefined, {
        type: 'info',
        title: '还没有这个位置',
        message: `${target}\n\n目前还不存在。`,
        buttons: ['知道了'],
      });
      return;
    }
    const error = shell.openPath(target);
    if (error) logLine(`[warn] shell.openPath 失败：${error}`);
  } catch (error) {
    logLine(`[warn] 打开路径失败 ${target}：${error.message}`);
  }
}

function showAbout() {
  const lines = [
    `云梦枢（YunMeng Hub）桌面版 ${app.getVersion()}`,
    '',
    '异构大模型交互式叙事引擎 · 本地单机部署',
    '',
    `Electron ${process.versions.electron}    Chromium ${process.versions.chrome}    Node ${process.versions.node}`,
    `后端端口：${state.port ?? '（未启动）'}${state.portSource ? `（来源 ${state.portSource}）` : ''}`,
    `后端形态：${state.backendMode === 'sidecar' ? '打包后端（backend.exe）'
      : state.backendMode === 'python' ? '本机 Python' : '（使用已在运行的后端 / 未启动）'}`,
    `后端 PID：${state.backendPid ?? '（使用已有后端 / 未启动）'}`,
    '',
    `数据目录：${paths.dataDir}`,
    `日志目录：${paths.logsDir}`,
    '',
    '后端本身仍是标准的 FastAPI 服务，命令行可以照常单独启动。',
  ];
  dialog.showMessageBox(mainWindow || undefined, {
    type: 'info',
    title: '关于 云梦枢',
    message: '云梦枢（YunMeng Hub）',
    detail: lines.join('\n'),
    buttons: ['好'],
    noLink: true,
  });
}

function buildMenu() {
  const template = [
    {
      label: '文件',
      submenu: [
        {
          label: '刷新',
          accelerator: 'CmdOrCtrl+R',
          click: () => mainWindow && mainWindow.reload(),
        },
        {
          label: '重新打开控制台',
          click: () => {
            if (!mainWindow) return;
            if (state.consoleUrl) mainWindow.loadURL(state.consoleUrl);
            else mainWindow.loadFile(paths.loadingPage);
          },
        },
        { type: 'separator' },
        { label: '退出', accelerator: 'Alt+F4', role: 'quit' },
      ],
    },
    {
      label: '视图',
      submenu: [
        { role: 'resetZoom', label: '实际大小' },
        { role: 'zoomIn', label: '放大' },
        { role: 'zoomOut', label: '缩小' },
        { type: 'separator' },
        { role: 'togglefullscreen', label: '全屏' },
        ...(process.env.HNE_DESKTOP_DEBUG
          ? [{ type: 'separator' }, { role: 'toggleDevTools', label: '开发者工具' }]
          : []),
      ],
    },
    {
      label: '数据',
      submenu: [
        { label: '打开数据目录', click: () => openPathSafely(paths.dataDir) },
        { label: '打开日志目录', click: () => openPathSafely(paths.logsDir) },
        { label: '打开后端日志', click: () => openPathSafely(paths.backendLogFile) },
        { type: 'separator' },
        { label: '打开配置文件（.env）', click: () => openPathSafely(paths.userEnvFile) },
        {
          label: '重新运行首次设置（换数据库 / 改密钥）',
          click: () => {
            // 同一个窗口直接换成设置页，设置完再走 finishSetup 回到控制台
            setStep('setup');
            if (mainWindow && !mainWindow.isDestroyed()) mainWindow.loadFile(paths.setupPage);
          },
        },
        { label: '打开配置模板（.env.example）', click: () => openPathSafely(paths.envExampleFile) },
        { type: 'separator' },
        {
          label: '复制日志目录路径',
          click: () => require('electron').clipboard.writeText(paths.logsDir),
        },
      ],
    },
    {
      label: '帮助',
      submenu: [
        { label: '关于 云梦枢', click: showAbout },
        {
          label: '接口文档（Swagger）',
          click: () => {
            if (state.port) shell.openExternal(`http://127.0.0.1:${state.port}/docs`);
          },
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ------------------------------------------------------------------
//  IPC（只暴露加载页真正需要的那几个能力）
// ------------------------------------------------------------------
function registerIpc() {
  ipcMain.handle('app:state', () => publicState());
  ipcMain.handle('app:paths', () => ({
    appRoot: paths.appRoot,
    dataDir: paths.dataDir,
    logsDir: paths.logsDir,
    backendLogFile: paths.backendLogFile,
    envFile: paths.envFile,
    version: app.getVersion(),
  }));
  ipcMain.handle('app:retry', async () => {
    logLine('[info] 用户点了"重试"');
    setState({ phase: 'starting', message: '重新开始…', error: null, hint: null, detail: null });
    await pumpBackend();
    return publicState();
  });
  ipcMain.handle('app:open-path', (_event, target) => {
    // ★ 只允许打开我们自己算出来的这几个路径，不接受渲染进程传任意字符串 ——
    //   否则"打开任意路径"就是一个能被执行的可控原语。
    const allowed = [paths.dataDir, paths.logsDir, paths.backendLogFile, paths.envFile];
    if (!allowed.includes(target)) {
      logLine(`[warn] 拒绝打开未授权的路径：${target}`);
      return false;
    }
    openPathSafely(target);
    return true;
  });
  ipcMain.handle('app:open-external', (_event, url) => {
    if (/^https?:/i.test(String(url))) shell.openExternal(String(url));
    return true;
  });

  // ---- 首启设置页 ----
  ipcMain.handle('setup:fields', () => {
    const existingText = setupConfig.readEnvFile(paths.userEnvFile);
    const parsed = existingText === null ? null : setupConfig.parseEnvText(existingText);
    const missing = setupConfig.missingRequired(parsed);

    // ★ 只回传**非密钥**字段的已有值。"已配过哪几项"用 missing 表达，
    //   口令与密钥**不回传**到页面（少一份风险，用户重填一次即可）。
    const values = setupConfig.defaultValues();
    if (parsed) {
      for (const field of setupConfig.SETUP_FIELDS) {
        if (field.secret) continue;
        if (parsed[field.key]) values[field.key] = parsed[field.key];
      }
    }

    return {
      fields: setupConfig.SETUP_FIELDS.map((f) => ({
        key: f.key,
        label: f.label,
        placeholder: f.placeholder,
        hint: f.hint,
        required: Boolean(f.required),
        secret: Boolean(f.secret),
        numeric: Boolean(f.numeric),
        advanced: Boolean(f.advanced),
        default: f.default,
        // ★★ 这里踩过一个真 bug（用户实测抓到）：第一版发的是
        //   `generate: typeof f.generate === 'function' ? f.key : null` —— 字符串，
        //   而渲染端判断的是 `typeof field.generate === 'function'`，**永远为假**，
        //   于是"随机生成"按钮从来没被创建，提示文字却让人去找它。
        //   根因是**函数过不了 IPC 边界**，能过的只有数据 —— 所以契约必须是布尔量。
        //   契约两头都别改单边：desktop/test/setup-ui.test.js 把这两个文件绑在一起。
        generatable: typeof f.generate === 'function',
      })),
      values,
      existing: parsed !== null,
      missing,
      envFile: paths.userEnvFile,
      dataDir: paths.userDataDataDir,
    };
  });

  ipcMain.handle('setup:generate', (_event, key) => {
    const field = setupConfig.SETUP_FIELDS.find((f) => f.key === String(key));
    if (!field || typeof field.generate !== 'function') return '';
    return field.generate();
  });

  ipcMain.handle('setup:test', async (_event, values) => {
    const checked = setupConfig.validateValues(values || {});
    if (!checked.ok) {
      return { ok: false, message: '还有几项需要填好：', errors: checked.errors };
    }
    const probeFile = path.join(paths.userDataDir, 'config', '.env.probe');
    try {
      setupConfig.writeEnvFile(probeFile, setupConfig.renderEnvFile(values || {}));
      const result = await selfCheckWithEnv(probeFile, '测试连接');
      return { ...result, errors: checked.errors };
    } catch (error) {
      return { ok: false, message: '测试时出错', detail: String(error && error.message) };
    } finally {
      // ★ 探测文件必须在**所有**路径上删掉。原来的清理写在成功的分支里，
      //   于是"测试失败"或"抛异常"都会把它留在用户目录里 ——
      //   用户的 `config\` 里就多出一个 `1.3KB 的 .env.probe`（实测看到过），
      //   里面是**真实的数据库口令**。留着既是困惑也是多余的一份凭据。
      try {
        if (fs.existsSync(probeFile)) {
          fs.unlinkSync(probeFile);
          logLine('[info] 已清理测试用的临时配置文件 .env.probe');
        }
      } catch (error) {
        logLine(`[warn] 清理 ${probeFile} 失败：${error.message}`);
      }
    }
  });

  ipcMain.handle('setup:save', async (_event, values) => {
    const checked = setupConfig.validateValues(values || {});
    if (!checked.ok) {
      return { ok: false, message: '还有几项需要填好：', errors: checked.errors };
    }

    let envFile;
    try {
      const existing = setupConfig.readEnvFile(paths.userEnvFile) || '';
      envFile = setupConfig.writeEnvFile(
        paths.userEnvFile,
        setupConfig.renderEnvFile(values || {}, existing),
      );
      logLine(`[info] 配置已写入 ${envFile}`);
      // ★ 写完立刻自证一遍：把文件读回来解析，确认**每个必填项都非空**。
      //   只在日志里记长度、不记值。这一步能在"写完就没人再看"的链路上
      //   抓住任何"值没落地"的问题（本轮就是靠它定位的）。
      const written = setupConfig.parseEnvText(setupConfig.readEnvFile(envFile) || '');
      const emptyRequired = setupConfig.SETUP_FIELDS
        .filter((f) => f.required && !String(written[f.key] || '').trim())
        .map((f) => f.key);
      logLine(emptyRequired.length
        ? `[error] 写出的配置里必填项为空：${emptyRequired.join(', ')}`
        : `[info] 写出的配置自证通过（${Object.keys(written).length} 个键，必填项都非空）`);
    } catch (error) {
      return {
        ok: false,
        message: '写入配置失败',
        detail: `${error && error.message}\n目标路径：${paths.userEnvFile}`,
      };
    }

    // ★ 写完立刻**真的起一次后端**验证。不通过就把结论回给页面，
    //   而不是让用户以为设置好了、进控制台才发现起不来。
    const result = await selfCheckWithEnv(envFile, '保存前验证');
    if (!result.ok) {
      return {
        ok: false,
        message: '配置已保存，但用这份配置起不来后端 —— 请按下面的原因改一改再试。',
        detail: result.detail || result.message,
      };
    }

    // 验证通过 → 正式启动（异步，不阻塞这个 IPC 返回，让页面先显示"正在进入"）
    setTimeout(() => { finishSetup().catch((error) => logLine(`[error] finishSetup: ${error}`)); }, 50);
    return { ok: true, message: '配置已保存并验证通过，正在进入控制台…' };
  });

  ipcMain.handle('setup:open-config-dir', () => {
    openPathSafely(path.dirname(paths.userEnvFile));
    return true;
  });
}

// ------------------------------------------------------------------
//  生命周期
// ------------------------------------------------------------------
function stopBackend(reason) {
  const pid = backendChild && backendChild.pid;
  if (!pid) {
    // 也可能有 PID 但 child 已置空（exit 时清过）；用 state 兜底只是提示，不去杀陌生 PID
    return;
  }
  logLine(`[info] ${reason}：结束后端进程树 PID=${pid}`);
  killTree(pid);
  backendChild = null;
}

app.isQuitting = false;

// ★ 单实例：两个桌面版会抢端口与数据库连接，第二个直接退出并聚焦第一个
if (!app.requestSingleInstanceLock({ key: SINGLE_INSTANCE_KEY })) {
  app.exit(0);
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    refreshUserDataPaths();
    openDesktopLog();
    logLine(`[info] 云梦枢桌面版启动：appRoot=${paths.appRoot} userData=${paths.userDataDir}`);
    logLine(`[info] 形态判定：packaged=${packaged}（app.isPackaged=${app.isPackaged}）`
      + ` resources=${process.resourcesPath || '(无)'} 允许仓库根兜底=${repoFallbackAllowed()}`);
    // ★ 一行**不含中文**的机器可读状态：验收脚本靠它判断"这次是打包态吗""进了哪一步"。
    //   为什么要单开一行而不是去匹配上面那些中文日志：日志是 UTF-8 写的，
    //   而 Windows PowerShell 5.1 的 `Get-Content` 默认按 ANSI 解 —— 中文会变成乱码
    //   （实测：应用明明是 packaged=true、明明进了设置页，脚本却全部判成"没有"）。
    //   判据做成纯 ASCII，就不必让任何一方去赌编码。
    logState('startup');

    registerIpc();

    // ★ 首次运行：还没有可用配置 → 先进设置页，**不要**去起后端。
    //   否则用户会看到一堆"数据库连不上"，而他根本没有地方能填连接信息。
    if (needsSetup()) {
      logLine('[info] 没有可用配置，进入首次设置');
      createSetupWindow();
      return;
    }

    ensureInitialConfig();
    buildMenu();
    createWindow();
    setStep('pumping');

    const ok = await pumpBackend();
    if (ok && state.consoleUrl && mainWindow && !mainWindow.isDestroyed()) {
      setStep('console');
      await mainWindow.loadURL(state.consoleUrl);
    }
  });

  app.on('window-all-closed', () => {
    // Windows/Linux：关窗即退出（后端随 app 一起收掉）；macOS 保留惯例
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('before-quit', () => {
    app.isQuitting = true;
    stopBackend('应用退出');
  });

  // ★ 兜底：异常退出路径也要收子进程，否则端口会被占住
  app.on('will-quit', () => {
    app.isQuitting = true;
    stopBackend('应用结束');
    if (desktopLogStream) {
      try {
        desktopLogStream.end();
      } catch {
        /* 忽略 */
      }
    }
  });

  process.on('exit', () => {
    const pid = backendChild && backendChild.pid;
    if (pid && isAlive(pid)) killTree(pid);
  });
}
